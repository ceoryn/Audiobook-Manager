#!/usr/bin/env python3
"""Execute an approved, preflighted missing-source import one book at a time."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import uuid
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.archive_imports import sync_archive_imports, validate_imported_output
from audiobook_manager.conversion import convert_to_m4b
from audiobook_manager.database import StateDatabase
from audiobook_manager.detection import detect_books
from audiobook_manager.models import ScannedFile
from audiobook_manager.output import plan_output
from audiobook_manager.probe import is_aac_container, probe_media


ARCHIVE_ACTION = "extract_to_separate_staging_then_build_m4b"
LOOSE_ACTION = "remux_extensionless_aac_preserving_chapters"


def validate_plan(plan: dict[str, Any]) -> tuple[Path, Path]:
    if plan.get("mode") != "dry-run" or plan.get("actions_applied") != 0:
        raise ValueError("only an unapplied missing-source dry run can be executed")
    source = Path(str(plan["source_root"])).resolve()
    output = Path(str(plan["output_root"])).resolve()
    if not source.is_dir() or not output.is_dir() or source == output:
        raise ValueError("source or output root is unavailable")
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError("source and output must be separate, non-nested directories")
    seen: set[Path] = set()
    for operation in plan.get("operations") or []:
        if operation["action"] not in {ARCHIVE_ACTION, LOOSE_ACTION}:
            continue
        raw = operation.get("archive") or operation.get("source")
        path = Path(str(raw)).resolve()
        target = Path(str(operation["output"])).resolve()
        metadata = operation["metadata"]
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError(f"source is missing or unsafe: {path}")
        if not target.is_relative_to(output) or target != plan_output(output, metadata).audio:
            raise ValueError(f"output path does not match reviewed metadata: {target}")
        if operation.get("cover") and Path(str(operation["cover_output"])).resolve() != plan_output(output, metadata).cover:
            raise ValueError("cover path does not match reviewed metadata")
        if target in seen:
            raise ValueError(f"multiple operations target the same output: {target}")
        seen.add(target)
    return source, output


def _check_source(path: Path, operation: dict[str, Any]) -> None:
    stat = path.stat()
    if stat.st_size != operation["size"] or stat.st_mtime_ns != operation["mtime_ns"]:
        raise ValueError("source size or modification time changed since the dry run")


def _check_free_space(output_root: Path, bytes_needed: int) -> None:
    available = shutil.disk_usage(output_root).free
    # Source extraction, conversion staging, and a little operational reserve.
    if available < bytes_needed * 2 + 1024**3:
        raise OSError("insufficient output-disk space for safe staged conversion")


def _quarantine_new_output(output: Path, output_root: Path,
                           cover: Path | None = None) -> Path:
    """Keep a newly built but unregistered file recoverable after validation fails."""
    archive = output_root / "_quarantine" / f"failed-import-{uuid.uuid4().hex[:12]}"
    target = archive / output.relative_to(output_root)
    target.parent.mkdir(parents=True, exist_ok=False)
    output.rename(target)
    if cover and cover.is_file():
        cover.rename(target.parent / cover.name)
    current = output.parent
    while current != output_root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent
    return target


def import_archive(
    operation: dict[str, Any], *, source_root: Path, output_root: Path,
    database: StateDatabase,
) -> Path:
    archive = Path(str(operation["archive"])).resolve()
    output = Path(str(operation["output"])).resolve()
    if not archive.is_relative_to(source_root) or not output.is_relative_to(output_root):
        raise ValueError("archive import path escapes a configured root")
    _check_source(archive, operation)
    existing = {str(row["archive_relative_path"]): row for row in
                database.archive_imports(source_root, output_root)}
    relative = archive.relative_to(source_root)
    cover_spec = operation.get("cover")
    cover_target = Path(str(operation["cover_output"])).resolve() if cover_spec else None
    if cover_target and (not cover_target.is_relative_to(output_root)
                         or cover_target != plan_output(output_root, operation["metadata"]).cover):
        raise ValueError("planned cover path escapes its book output directory")
    if relative.as_posix() in existing:
        row = existing[relative.as_posix()]
        expected_members = [{"name": item["name"], "crc32": item["crc32"],
                             "size": item["size"]} for item in operation["members_in_order"]]
        if cover_spec:
            expected_members.append({"name": cover_spec["name"], "crc32": cover_spec["crc32"],
                                     "size": cover_spec["size"], "role": "cover"})
        if (row["size"] != operation["size"] or row["modified_ns"] != operation["mtime_ns"]
                or row["members"] != expected_members):
            raise ValueError("registered archive provenance differs from the dry run")
        if Path(str(row["output_path"])).resolve() == output and output.is_file():
            valid, detail = validate_imported_output(
                output, metadata=dict(row["metadata"]),
                duration_seconds=float(row["duration_seconds"]),
                chapter_count=int(row["chapter_count"]),
            )
            if valid:
                if cover_spec and (not cover_target or not cover_target.is_file()):
                    raise ValueError("registered archive cover is missing")
                return output
            raise ValueError(f"registered output needs review: {detail}")
        raise ValueError("archive is registered to a different or missing output")
    if output.exists():
        raise FileExistsError(f"unregistered output collision; preserve for review: {output}")
    if cover_target and cover_target.exists():
        raise FileExistsError(f"unregistered cover collision; preserve for review: {cover_target}")
    members = operation["members_in_order"]
    if not members:
        raise ValueError("archive import has no planned audio members")
    planned_names = [str(item["name"]) for item in members]
    if len(set(planned_names)) != len(planned_names):
        raise ValueError("duplicate planned archive member")
    _check_free_space(output_root, sum(int(item["size"]) for item in members) +
                      (int(cover_spec["size"]) if cover_spec else 0))
    cover_payload: bytes | None = None
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        archive_names = [item.filename for item in infos if not item.is_dir() and
                         Path(item.filename).suffix.casefold() in {".mp3", ".m4a", ".m4b", ".mp4"}]
        if len(archive_names) != len(set(archive_names)) or set(archive_names) != set(planned_names):
            raise ValueError("archive audio membership changed since the dry run")
        if cover_spec:
            info = zipped.getinfo(cover_spec["name"])
            if (info.flag_bits & 1 or info.CRC != cover_spec["crc32"]
                    or info.file_size != cover_spec["size"] or info.file_size > 10_000_000):
                raise ValueError("archive cover fingerprint changed")
            with zipped.open(info) as reader:
                cover_payload = reader.read(10_000_001)
            if not cover_payload.startswith(b"\xff\xd8\xff") or len(cover_payload) != info.file_size:
                raise ValueError("archive cover is not a complete JPEG")
        with tempfile.TemporaryDirectory(prefix="audiobook-archive-import-", dir=output_root) as directory:
            staging = Path(directory)
            inputs: list[Path] = []
            for index, member in enumerate(members, 1):
                info = zipped.getinfo(member["name"])
                if info.flag_bits & 1 or info.CRC != member["crc32"] or info.file_size != member["size"]:
                    raise ValueError(f"archive member fingerprint changed: {member['name']}")
                target = staging / f"track-{index:04d}{Path(info.filename).suffix.casefold()}"
                with zipped.open(info) as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                probe = probe_media(target)
                if not probe.codec_name or not probe.duration_seconds or abs(
                    probe.duration_seconds - float(member["duration_seconds"])
                ) > max(2.0, float(member["duration_seconds"]) * 0.002):
                    raise ValueError(f"archive member audio changed: {member['name']}")
                inputs.append(target)
            result = convert_to_m4b(
                inputs=inputs, library_root=staging, output_root=output_root,
                metadata=operation["metadata"], destination=output,
            )
    try:
        valid, detail = validate_imported_output(
            result, metadata=operation["metadata"],
            duration_seconds=float(operation["expected_duration_seconds"]),
            chapter_count=len(members),
        )
        if not valid:
            raise RuntimeError(detail)
        if cover_target and cover_payload is not None:
            with cover_target.open("xb") as writer:
                writer.write(cover_payload)
        database.register_archive_import(
            source=source_root, destination=output_root, archive_relative_path=relative,
            size=int(operation["size"]), modified_ns=int(operation["mtime_ns"]),
            members=[{"name": item["name"], "crc32": item["crc32"], "size": item["size"]}
                     for item in members] +
                    ([{"name": cover_spec["name"], "crc32": cover_spec["crc32"],
                       "size": cover_spec["size"], "role": "cover"}] if cover_spec else []),
            metadata=dict(operation["metadata"]), output_path=result,
            duration_seconds=float(operation["expected_duration_seconds"]),
            chapter_count=len(members),
        )
    except (OSError, RuntimeError, ValueError) as error:
        quarantined = _quarantine_new_output(result, output_root, cover_target)
        raise RuntimeError(f"new output quarantined at {quarantined}: {error}") from error
    return result


def import_loose(
    operation: dict[str, Any], *, source_root: Path, output_root: Path,
    database: StateDatabase, run_id: int,
) -> Path:
    source = Path(str(operation["source"])).resolve()
    output = Path(str(operation["output"])).resolve()
    if not source.is_relative_to(source_root) or not output.is_relative_to(output_root):
        raise ValueError("loose import path escapes a configured root")
    _check_source(source, operation)
    probe = probe_media(source)
    if not is_aac_container(probe) or len(probe.chapters) != operation["expected_chapters"]:
        raise ValueError("extensionless source no longer has the planned AAC chapters")
    if abs((probe.duration_seconds or 0) - operation["expected_duration_seconds"]) > 5:
        raise ValueError("extensionless source duration changed")
    if output.exists():
        relative = source.relative_to(source_root).as_posix()
        prior = next((row for row in database.process_books(run_id)
                      if row["state"] == "complete" and row["output_path"] == str(output)
                      and row["files"] == [relative]
                      and (row["metadata"] or {}).get("_verified_output") is True), None)
        if prior is None:
            raise FileExistsError(f"unregistered output collision requires review: {output}")
        valid, detail = validate_imported_output(
            output, metadata=operation["metadata"],
            duration_seconds=float(operation["expected_duration_seconds"]),
            chapter_count=int(operation["expected_chapters"]),
        )
        if valid:
            _register_loose(database, run_id, source, source_root, output, operation["metadata"])
            return output
        raise FileExistsError(f"output collision requires review: {detail}")
    _check_free_space(output_root, source.stat().st_size)
    result = convert_to_m4b(
        inputs=[source], library_root=source_root, output_root=output_root,
        metadata=operation["metadata"], destination=output, copy_audio=True,
    )
    try:
        valid, detail = validate_imported_output(
            result, metadata=operation["metadata"],
            duration_seconds=float(operation["expected_duration_seconds"]),
            chapter_count=int(operation["expected_chapters"]),
        )
        if not valid:
            raise RuntimeError(detail)
        _register_loose(database, run_id, source, source_root, output, operation["metadata"])
    except (OSError, RuntimeError, ValueError) as error:
        quarantined = _quarantine_new_output(result, output_root)
        raise RuntimeError(f"new output quarantined at {quarantined}: {error}") from error
    return result


def _register_loose(
    database: StateDatabase, run_id: int, source: Path, source_root: Path,
    output: Path, metadata: dict[str, object],
) -> None:
    """Use normal detector identity so later scans revalidate this source normally."""
    probe = probe_media(source)
    stat = source.stat()
    relative = source.relative_to(source_root)
    item = ScannedFile(source, relative, stat.st_size, stat.st_mtime_ns, probe)
    groups = list(detect_books([item]))
    if len(groups) != 1 or len(groups[0].files) != 1:
        raise ValueError("extensionless source has no unique detector identity")
    book_id = groups[0].key
    old = next((row for row in database.process_books(run_id) if row["book_id"] == book_id), None)
    if old is not None and old["state"] != "superseded" and old["files"] != [relative.as_posix()]:
        raise ValueError("extensionless detector identity conflicts with an existing book")
    fingerprint = hashlib.sha256(
        f"{relative}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    ).hexdigest()
    evidence = [{"kind": "verified_loose_import", "explanation":
                 "source AAC chapters, duration, and output embedded identity were verified"}]
    database.store_detected_book(
        run_id=run_id, book_id=book_id, state="identified",
        source_fingerprint=fingerprint, classification="verified_loose_import",
        files=[relative.as_posix()], evidence=evidence,
    )
    owner = database.reserve_output_claim(run_id, book_id, output)
    if owner is not None:
        raise ValueError(f"output is claimed by another detected book: {owner}")
    verified = dict(metadata)
    verified["_verified_output"] = True
    database.store_book_identification(
        run_id=run_id, book_id=book_id, state="metadata_matched", metadata=verified,
        candidates=[], evidence=evidence, confidence=1,
    )
    database.adopt_verified_output(
        run_id=run_id, book_id=book_id, metadata=verified, output_path=output,
    )
    database.log_event(run_id, "verified_loose_import", book_id=book_id,
                       detail=str(relative))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--approve", action="store_true", help="authorize writing verified outputs")
    parser.add_argument("--max-books", type=int, default=0, help="optional bounded batch size")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    source, output = validate_plan(plan)
    operations = [item for item in plan["operations"] if item["action"] in
                  {ARCHIVE_ACTION, LOOSE_ACTION}]
    if not args.approve:
        print(json.dumps({"mode": "dry-run", "books": len(operations),
                          "plan": str(args.plan.resolve()), "actions_applied": 0}, indent=2))
        return
    if args.max_books < 0:
        raise ValueError("max-books must be nonnegative")
    database_path = args.database.resolve()
    if not database_path.is_file():
        raise ValueError("existing process database is required")
    results: list[dict[str, str]] = []
    with StateDatabase(database_path) as database:
        latest = database.latest_process_status()
        if (not latest or int(latest["id"]) != int(plan["expected_run_id"])
                or latest["status"] != "complete"):
            raise ValueError("the missing-source plan belongs to a different process run")
        if Path(str(latest["source_root"])).resolve() != source or Path(str(latest["destination_root"])).resolve() != output:
            raise ValueError("process run roots do not match the reviewed plan")
        registered_archives = {str(row["archive_relative_path"]) for row in
                               database.archive_imports(source, output)}
        verified_loose = {
            (str(row["output_path"]), str(row["files"][0]))
            for row in database.process_books(int(latest["id"]))
            if row["state"] == "complete" and row["output_path"]
            and len(row["files"]) == 1
            and (row["metadata"] or {}).get("_verified_output") is True
        }
        new_attempts = 0
        for operation in operations:
            if operation["action"] == ARCHIVE_ACTION:
                already = str(Path(operation["archive"]).resolve().relative_to(source)) in registered_archives
            else:
                already = (str(Path(operation["output"])),
                           str(Path(operation["source"]).resolve().relative_to(source))) in verified_loose
            if args.max_books and new_attempts >= args.max_books and not already:
                break
            if not already:
                new_attempts += 1
            label = str(operation["metadata"]["title"])
            try:
                if operation["action"] == ARCHIVE_ACTION:
                    target = import_archive(operation, source_root=source, output_root=output,
                                            database=database)
                else:
                    target = import_loose(operation, source_root=source, output_root=output,
                                          database=database, run_id=int(latest["id"]))
                status = "already_imported" if already else "complete"
                results.append({"book": label, "status": status, "output": str(target)})
                print(f"{status}: {label}: {target}", flush=True)
            except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
                results.append({"book": label, "status": "needs_review", "reason": str(error)})
                print(f"Needs review {label}: {error}", flush=True)
        sync_archive_imports(database, run_id=int(latest["id"]), source=source, destination=output)
        books = [row for row in database.process_books(int(latest["id"]))
                 if row["state"] != "superseded"]
        counts = Counter(str(row["state"]) for row in books)
        summary = dict(latest["summary"])
        summary.update(books_discovered=len(books), books_successfully_processed=counts["complete"],
                       books_quarantined=counts["quarantined"], failures=counts["failed"])
        database.update_process_run(int(latest["id"]), str(latest["status"]), summary)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    log = args.plan.resolve().parent / f"missing-source-import-applied-{stamp}.json"
    log.write_text(json.dumps({"plan": str(args.plan.resolve()), "results": results}, indent=2) + "\n")
    print(json.dumps({"complete": sum(row["status"] == "complete" for row in results),
                      "already_imported": sum(row["status"] == "already_imported" for row in results),
                      "needs_review": sum(row["status"] == "needs_review" for row in results),
                      "log": str(log)}, indent=2))


if __name__ == "__main__":
    main()
