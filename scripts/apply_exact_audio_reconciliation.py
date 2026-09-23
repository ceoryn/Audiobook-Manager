#!/usr/bin/env python3
"""Revalidate and apply an approved exact-audio reconciliation plan."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _latest_run(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT id,status,finished_at FROM process_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("process database contains no runs")
        return dict(row)
    finally:
        connection.close()


def _stream_hash(path: Path) -> str:
    completed = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
            "-map", "0:a:0", "-c", "copy", "-f", "hash", "-hash", "sha256", "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode or not value.startswith("SHA256="):
        raise ValueError(f"could not hash primary audio stream: {path}")
    return value.split("=", 1)[1].strip().casefold()


def validate(plan: dict[str, Any]) -> tuple[Path, Path, list[dict[str, Any]]]:
    if plan.get("mode") != "dry-run" or plan.get("actions_applied") != 0:
        raise ValueError("only an unapplied dry-run plan can be executed")
    roots = plan.get("roots") or {}
    output_root = Path(str(roots.get("output") or "")).resolve()
    database = Path(str(roots.get("database") or "")).resolve()
    if not output_root.is_dir() or not database.is_file():
        raise ValueError("the output or database root is unavailable")
    run = plan.get("process_run") or {}
    latest = _latest_run(database)
    if (
        run.get("status") != "complete"
        or latest.get("status") != "complete"
        or int(run.get("id") or -1) != int(latest["id"])
        or run.get("finished_at") != latest.get("finished_at")
    ):
        raise ValueError("the exact-audio plan is stale; generate a fresh audit")
    quarantine_root = Path(
        str((plan.get("safety") or {}).get("quarantine_root_if_approved") or "")
    ).resolve()
    if not _inside(quarantine_root, output_root) or quarantine_root == output_root:
        raise ValueError("quarantine root must remain beneath the output root")
    relative = quarantine_root.relative_to(output_root)
    if not relative.parts or relative.parts[0] != "_quarantine":
        raise ValueError("quarantine root must be beneath output/_quarantine")
    operations = list(plan.get("quarantine_operations") or [])
    sources = [str(Path(item["source"]).resolve()) for item in operations]
    destinations = [str(Path(item["destination"]).resolve()) for item in operations]
    if len(sources) != len(set(sources)) or len(destinations) != len(set(destinations)):
        raise ValueError("plan contains duplicate sources or destinations")
    return output_root, quarantine_root, operations


def preflight(
    operation: dict[str, Any], *, output_root: Path, quarantine_root: Path,
    hashes: dict[Path, str] | None = None,
) -> dict[str, Any]:
    if operation.get("action") != "quarantine_exact_audio_duplicate":
        raise ValueError(f"unsupported action: {operation.get('action')!r}")
    source = Path(operation["source"]).resolve()
    reference = Path(operation["reference_output"]).resolve()
    destination = Path(operation["destination"]).resolve()
    relative = Path(str(operation["relative_path"]))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"unsafe relative path: {relative}")
    if destination != (quarantine_root / relative).resolve():
        raise ValueError(f"destination does not match quarantine layout: {source}")
    for path in (source, reference):
        if not _inside(path, output_root) or _inside(path, quarantine_root):
            raise ValueError(f"media path is outside the clean output tree: {path}")
    if source == reference or not reference.is_file():
        raise ValueError(f"invalid current reference output: {reference}")
    already_moved = False
    if source.is_file() and destination.exists():
        raise ValueError(f"both source and destination exist: {source}")
    if source.is_file():
        media_path = source
    elif destination.is_file():
        media_path = destination
        already_moved = True
    else:
        raise ValueError(f"planned untracked output is missing: {source}")
    expected = str(operation.get("audio_stream_sha256") or "").casefold()
    if not expected:
        raise ValueError(f"exact encoded-audio digest is missing: {source}")
    if hashes is not None:
        if hashes.get(media_path) != expected or hashes.get(reference) != expected:
            raise ValueError(f"exact encoded-audio identity changed: {source}")
    return {
        "source": str(source),
        "media_path": str(media_path),
        "destination": str(destination),
        "reference_output": str(reference),
        "reference_book_id": operation["reference_book_id"],
        "audio_stream_sha256": expected,
        "already_quarantined": already_moved,
    }


def execute(
    plan: dict[str, Any], *, approve: bool, workers: int = 8
) -> tuple[dict[str, Any], Path | None]:
    output_root, quarantine_root, operations = validate(plan)
    basic = [
        preflight(item, output_root=output_root, quarantine_root=quarantine_root)
        for item in operations
    ]
    paths = {
        Path(item[key]).resolve()
        for item in basic
        for key in ("media_path", "reference_output")
    }
    hashes: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_stream_hash, path): path for path in paths}
        for future in as_completed(futures):
            hashes[futures[future]] = future.result()
    checked = [
        preflight(
            item,
            output_root=output_root,
            quarantine_root=quarantine_root,
            hashes=hashes,
        )
        for item in operations
    ]
    sources = {item["source"] for item in checked if not item["already_quarantined"]}
    references = {item["reference_output"] for item in checked}
    if sources & references:
        raise ValueError("a planned source is also required as a current reference")
    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "applied" if approve else "preflight-only",
        "source_library_writes": False,
        "permanent_deletions": 0,
        "operations_planned": len(checked),
        "operations_moved": 0,
        "operations_already_quarantined": sum(x["already_quarantined"] for x in checked),
        "operations": checked,
    }
    if not approve:
        return result, None
    quarantine_root.mkdir(parents=True, exist_ok=True)
    for item in checked:
        if item["already_quarantined"]:
            continue
        source = Path(item["source"])
        destination = Path(item["destination"])
        if not source.is_file() or destination.exists():
            raise RuntimeError(f"filesystem changed after preflight: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        result["operations_moved"] += 1
    log_path = quarantine_root / "exact-audio-reconciliation-log.json"
    log_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result, log_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", type=Path, required=True)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    plan = json.loads(args.plan_json.resolve().read_text(encoding="utf-8"))
    result, log_path = execute(plan, approve=args.approve, workers=args.workers)
    print(json.dumps({
        "mode": result["mode"],
        "operations_planned": result["operations_planned"],
        "operations_moved": result["operations_moved"],
        "operations_already_quarantined": result["operations_already_quarantined"],
        "log": str(log_path) if log_path else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
