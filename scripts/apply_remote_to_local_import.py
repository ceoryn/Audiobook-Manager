#!/usr/bin/env python3
"""Resume an approved, staged remote-to-local M4B import without overwrites.

Without --approved this command prints a summary and changes nothing. It only
accepts proposed_copy_to_destination records from a reviewed dry-run plan.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from audiobook_manager.probe import probe_media


REMOTE_STREAM = r"""
import hashlib
import json
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
relative = Path(sys.argv[2])
expected_size = int(sys.argv[3])
expected_mtime = int(sys.argv[4])
path = (root / relative).resolve()
if not path.is_relative_to(root) or not path.is_file():
    raise SystemExit('source path is unavailable or escapes the remote root')
before = path.stat()
if before.st_size != expected_size or before.st_mtime_ns != expected_mtime:
    raise SystemExit('remote source fingerprint changed before copy')
digest = hashlib.sha256()
sent = 0
with path.open('rb') as reader:
    while block := reader.read(1024 * 1024):
        sys.stdout.buffer.write(block)
        digest.update(block)
        sent += len(block)
sys.stdout.buffer.flush()
after = path.stat()
if (sent != expected_size or after.st_size != expected_size or
        after.st_mtime_ns != expected_mtime):
    raise SystemExit('remote source changed during copy')
print(json.dumps({'sha256': digest.hexdigest(), 'size': sent,
                  'mtime_ns': after.st_mtime_ns}), file=sys.stderr)
"""

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while block := reader.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def save_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def validated_paths(plan: dict[str, Any], item: dict[str, Any], root: Path) -> tuple[str, Path]:
    relative = PurePosixPath(item["remote_source"])
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("unsafe relative remote source path")
    target = Path(item["destination_path"])
    if target.suffix.casefold() != ".m4b" or not target.is_absolute():
        raise ValueError("destination must be an absolute M4B path")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("destination escapes the configured audiobook root")
    if str(root.resolve()) != str(Path(plan["destination_root"]).resolve()):
        raise ValueError("destination root does not match the approved plan")
    fingerprint = item["source_fingerprint"]
    if (fingerprint.get("path") != relative.as_posix() or
            fingerprint.get("size") != item["source_bytes"] or
            not isinstance(fingerprint.get("mtime_ns"), int)):
        raise ValueError("source fingerprint is incomplete or inconsistent")
    return relative.as_posix(), target


def remote_command(plan: dict[str, Any], item: dict[str, Any], relative: str,
                   control_socket: Path, known_hosts: Path) -> list[str]:
    fingerprint = item["source_fingerprint"]
    remote = "python3 -c " + shlex.quote(REMOTE_STREAM) + " " + " ".join(
        shlex.quote(str(value)) for value in
        (plan["remote_root"], relative, fingerprint["size"], fingerprint["mtime_ns"]))
    return ["ssh", "-F", "/dev/null", "-S", str(control_socket),
            "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", plan["remote_host"], remote]


def copy_and_verify(plan: dict[str, Any], item: dict[str, Any], stage: Path,
                    relative: str, control_socket: Path, known_hosts: Path) -> str:
    command = remote_command(plan, item, relative, control_socket, known_hosts)
    digest = hashlib.sha256()
    copied = 0
    with stage.open("xb") as writer:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert process.stdout is not None and process.stderr is not None
            while block := process.stdout.read(1024 * 1024):
                writer.write(block)
                digest.update(block)
                copied += len(block)
            stderr = process.stderr.read().decode("utf-8", errors="replace")
            status = process.wait()
            writer.flush()
            os.fsync(writer.fileno())
        except BaseException:
            process.terminate()
            process.wait()
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    if status:
        raise RuntimeError(f"remote copy failed: {stderr[-1000:]}")
    try:
        result = json.loads(stderr.splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"remote checksum response missing: {stderr[-1000:]}") from exc
    if copied != item["source_bytes"] or result.get("size") != copied:
        raise ValueError("copy size differs from approved source size")
    if result.get("sha256") != digest.hexdigest():
        raise ValueError("remote and destination SHA-256 checksums differ")
    if result.get("mtime_ns") != item["source_fingerprint"]["mtime_ns"]:
        raise ValueError("remote source modification time changed")
    probe = probe_media(stage)
    actual = probe.duration_seconds or 0
    expected = item["duration_seconds"]
    if not probe.codec_name or actual <= 0 or abs(actual - expected) > max(2.0, expected * 0.001):
        raise ValueError("staged M4B codec or duration differs from the approved probe")
    return digest.hexdigest()


def already_imported(target: Path, item: dict[str, Any], ledger: dict[str, Any]) -> bool:
    record = ledger.get(str(target))
    return bool(record and target.is_file() and
                record.get("source_fingerprint") == item["source_fingerprint"] and
                target.stat().st_size == item["source_bytes"] and
                sha256_file(target) == record.get("sha256"))


def publish(stage: Path, target: Path) -> None:
    """Hard-link on the same filesystem for atomic no-overwrite publication."""
    if stage.stat().st_dev != target.parent.stat().st_dev:
        raise ValueError("staging and destination are not on the same filesystem")
    os.link(stage, target)
    stage.unlink()


def run(plan: dict[str, Any], root: Path, stage_root: Path, ledger_path: Path,
        control_socket: Path, known_hosts: Path, *, max_books: int | None = None) -> dict[str, int]:
    if not root.is_dir() or not root.parent.is_mount():
        raise ValueError("destination filesystem and audiobook root must be present")
    if os.statvfs(root).f_flag & os.ST_RDONLY:
        raise ValueError("destination is mounted read-only; no import can start")
    if stage_root.exists() and (stage_root.is_symlink() or
                                stage_root.stat().st_dev != root.stat().st_dev):
        raise ValueError("staging root is unsafe or on a different filesystem")
    if not stage_root.resolve().is_relative_to(root.parent.resolve()):
        raise ValueError("staging root escapes the destination filesystem")
    if not control_socket.exists() or not known_hosts.is_file():
        raise ValueError("SSH control socket or pinned known-hosts file is unavailable")
    stage_root.mkdir(parents=True, exist_ok=True)
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    if not isinstance(ledger, dict):
        raise ValueError("import ledger must be a JSON object")
    reserve_bytes = (plan.get("summary") or {}).get("reserve_bytes")
    if isinstance(reserve_bytes, bool) or not isinstance(reserve_bytes, int) or reserve_bytes < 0:
        raise ValueError("approved plan has an invalid destination reserve")
    selected = [item for item in plan["operations"] if item["action"] == "proposed_copy_to_destination"]
    counts: Counter[str] = Counter()
    for number, item in enumerate(selected, 1):
        if max_books is not None and counts["copied"] >= max_books:
            break
        relative, target = validated_paths(plan, item, root)
        if already_imported(target, item, ledger):
            counts["already_imported"] += 1
            print(f"[{number}/{len(selected)}] Already imported: {item['metadata']['title']}", flush=True)
            continue
        if target.exists():
            counts["held_existing_target"] += 1
            print(f"[{number}/{len(selected)}] Held (target exists): {target}", flush=True)
            continue
        if shutil.disk_usage(root).free - item["source_bytes"] < reserve_bytes:
            counts["held_low_space"] += 1
            print(f"[{number}/{len(selected)}] Held (low disk space): {target}", flush=True)
            break
        stage = stage_root / f"{item['asin']}-{uuid.uuid4().hex}.m4b"
        print(f"[{number}/{len(selected)}] Copying {item['metadata']['title']} "
              f"({item['source_bytes']/2**30:.2f} GiB)", flush=True)
        try:
            digest = copy_and_verify(plan, item, stage, relative, control_socket, known_hosts)
            if target.exists():
                raise FileExistsError(f"destination appeared during copy: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            publish(stage, target)
            ledger[str(target)] = {"remote_source": relative,
                                   "source_fingerprint": item["source_fingerprint"],
                                   "sha256": digest, "size": item["source_bytes"],
                                   "asin": item["asin"], "imported_at": datetime.now().astimezone().isoformat()}
            save_json_atomic(ledger_path, ledger)
            counts["copied"] += 1
            print(f"[{number}/{len(selected)}] Verified and published: {target}", flush=True)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            counts["failed_or_held"] += 1
            print(f"[{number}/{len(selected)}] Held: {exc}; staged file: {stage}",
                  file=sys.stderr, flush=True)
    return dict(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--control-socket", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--ledger", type=Path,
                        default=Path("reports/remote-to-local-import-ledger.json"))
    parser.add_argument("--max-books", type=int, help="Stop after this many newly copied books")
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if plan.get("mode") != "dry_run_no_media_writes":
        parser.error("expected a dry-run remote-to-local plan")
    count = sum(item["action"] == "proposed_copy_to_destination" for item in plan["operations"])
    if not args.approved:
        print(f"Dry run only: {count} proposed copies. No media changed.")
        return
    if args.max_books is not None and args.max_books < 1:
        parser.error("--max-books must be positive")
    root = args.destination_root.resolve()
    stage_root = root.parent / ".audiobook-manager-staging"
    lock_path = args.ledger.with_suffix(args.ledger.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("another remote-to-local import is running") from exc
        result = run(plan, root, stage_root, args.ledger, args.control_socket,
                     args.known_hosts, max_books=args.max_books)
        print(json.dumps({"result": result, "ledger": str(args.ledger)}, indent=2))


if __name__ == "__main__":
    main()
