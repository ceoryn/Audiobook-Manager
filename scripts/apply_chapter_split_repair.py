#!/usr/bin/env python3
"""Apply an explicitly approved chapter/split repair plan safely.

The executor performs a complete preflight before changing media.  Replaced
generated outputs and redundant legacy split outputs are copied to the plan's
external archive, verified byte-for-byte, and only then removed from the clean
output tree.  Source media is never written and no audiobook is permanently
deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.probe import probe_media


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _snapshot(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or not _inside(resolved, root):
        raise ValueError(f"missing or unsafe planned file: {path}")
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "relative_path": str(resolved.relative_to(root)),
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
    }


def _matches_snapshot(path: Path, expected: dict[str, Any], root: Path) -> bool:
    try:
        return _snapshot(path, root) == {
            key: expected[key] for key in ("path", "relative_path", "size", "modified_ns")
        }
    except (OSError, ValueError, KeyError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, timeout: float = 1800.0) -> str:
    completed = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=timeout
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or f"command exited {completed.returncode}")
    return completed.stdout


def _audio_md5(path: Path) -> str:
    output = _run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-map", "0:a:0", "-c", "copy", "-f", "md5", "-",
    ]).strip()
    if not output.startswith("MD5="):
        raise RuntimeError(f"could not fingerprint compressed audio stream: {path}")
    return output


def _latest_run(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT id,status,started_at,finished_at FROM process_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("process database contains no runs")
        return dict(row)
    finally:
        connection.close()


def _nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _duration_ok(left: float, right: float) -> bool:
    return bool(left and right and abs(left - right) <= max(5.0, max(left, right) * 0.005))


def validate_plan(plan: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    if plan.get("mode") != "dry-run" or plan.get("actions_applied") != 0:
        raise ValueError("only an unapplied dry-run plan can be executed")
    roots = plan.get("roots") or {}
    database = Path(str(roots.get("database") or "")).resolve()
    source_root = Path(str(roots.get("source") or "")).resolve()
    output_root = Path(str(roots.get("output") or "")).resolve()
    archive_root = Path(str(roots.get("archive") or "")).resolve()
    if not database.is_file() or not source_root.is_dir() or not output_root.is_dir():
        raise ValueError("a configured repair root is unavailable")
    if _inside(archive_root, source_root) or _inside(archive_root, output_root):
        raise ValueError("archive must be outside both the source and clean output trees")
    expected_run = plan.get("process_run") or {}
    latest = _latest_run(database)
    if (
        latest.get("status") != "complete"
        or int(latest.get("id") or -1) != int(expected_run.get("id") or -2)
        or latest.get("finished_at") != expected_run.get("finished_at")
    ):
        raise ValueError("the repair plan is stale or a processing run is active")
    needed = int(plan["summary"]["chapter_flattened_bytes_to_archive"]) + int(
        plan["summary"]["verified_split_bytes_to_archive"]
    )
    free = shutil.disk_usage(_nearest_existing(archive_root)).free
    if free < needed + 10 * 2**30:
        raise ValueError(
            f"archive disk needs at least {(needed + 10 * 2**30) / 2**30:.2f} GiB free"
        )
    return database, source_root, output_root, archive_root


def _validate_chapter_item(
    item: dict[str, Any], *, source_root: Path, output_root: Path, archive_root: Path
) -> str:
    source = Path(item["source"]["path"]).resolve()
    output = Path(item["current_output"]["path"]).resolve()
    archive = Path(item["archive_destination"]).resolve()
    if not _inside(source, source_root) or not _inside(output, output_root):
        raise ValueError("chapter repair path escapes its configured tree")
    expected_archive = (
        archive_root / "replaced-chapter-flattened" / item["current_output"]["relative_path"]
    ).resolve()
    if archive != expected_archive:
        raise ValueError(f"chapter archive path does not match plan: {archive}")
    if not _matches_snapshot(source, item["source"], source_root):
        raise ValueError(f"source changed after planning: {source}")
    if _matches_snapshot(output, item["current_output"], output_root):
        if archive.exists():
            if (
                archive.is_file()
                and archive.stat().st_size == output.stat().st_size
                and _sha256(archive) == _sha256(output)
            ):
                return "pending_archive_exists"
            raise ValueError(f"archive destination conflicts with current output: {archive}")
        return "pending"
    if output.is_file() and archive.is_file():
        probe = probe_media(output)
        if (
            probe.codec_name == "aac"
            and len(probe.chapters) == int(item["source_chapters"])
            and _duration_ok(float(probe.duration_seconds or 0), float(item["source_duration_seconds"]))
        ):
            return "already_repaired"
    raise ValueError(f"current output changed after planning: {output}")


def _validate_split_group(
    group: dict[str, Any], *, output_root: Path, archive_root: Path
) -> list[str]:
    reference = group["reference_whole_output"]
    reference_path = Path(reference["path"]).resolve()
    if not _matches_snapshot(reference_path, reference["snapshot"], output_root):
        raise ValueError(f"whole-book reference changed after planning: {reference_path}")
    reference_probe = probe_media(reference_path)
    if (
        reference_probe.codec_name != "aac"
        or len(reference_probe.chapters) < int(group["unique_part_count"])
        or not _duration_ok(
            float(reference_probe.duration_seconds or 0),
            float(group["unique_part_duration_seconds"]),
        )
    ):
        raise ValueError(f"whole-book reference no longer validates: {reference_path}")
    destinations = list(group["archive_destinations"])
    if len(destinations) != len(group["legacy_files"]):
        raise ValueError("split group has inconsistent file and destination counts")
    statuses: list[str] = []
    for item, destination_value in zip(group["legacy_files"], destinations, strict=True):
        source = Path(item["path"]).resolve()
        destination = Path(destination_value).resolve()
        expected = (archive_root / "redundant-split-outputs" / item["relative_path"]).resolve()
        if destination != expected or not _inside(source, output_root):
            raise ValueError("split output path escapes its configured tree")
        if _matches_snapshot(source, item, output_root):
            if destination.exists():
                raise ValueError(f"split archive destination unexpectedly exists: {destination}")
            for companion in item.get("companion_files") or []:
                path = Path(companion["path"]).resolve()
                if not _matches_snapshot(path, companion, output_root):
                    raise ValueError(f"split companion changed after planning: {path}")
                companion_destination = (
                    archive_root / "redundant-split-outputs" / companion["relative_path"]
                ).resolve()
                if companion_destination.exists():
                    raise ValueError(
                        f"split companion archive destination exists: {companion_destination}"
                    )
            statuses.append("pending")
        elif not source.exists() and destination.is_file():
            statuses.append("already_archived")
        else:
            raise ValueError(f"split output changed after planning: {source}")
    return statuses


def preflight(plan: dict[str, Any]) -> dict[str, Any]:
    database, source_root, output_root, archive_root = validate_plan(plan)
    chapter_statuses = [
        _validate_chapter_item(
            item, source_root=source_root, output_root=output_root, archive_root=archive_root
        )
        for item in plan["chapter_repairs"]
    ]
    split_statuses = [
        _validate_split_group(group, output_root=output_root, archive_root=archive_root)
        for group in plan["verified_split_sets"]
    ]
    return {
        "database": database,
        "source_root": source_root,
        "output_root": output_root,
        "archive_root": archive_root,
        "chapter_statuses": chapter_statuses,
        "split_statuses": split_statuses,
    }


def _metadata_args(metadata: dict[str, Any]) -> list[str]:
    title = str(metadata.get("title") or "Untitled").strip()
    authors = metadata.get("authors") or []
    author = ", ".join(str(value).strip() for value in authors if str(value).strip())
    args = [
        "-metadata", f"title={title}", "-metadata", f"album={title}",
        "-metadata", f"artist={author}", "-metadata", f"album_artist={author}",
        "-metadata", f"author={author}", "-metadata", "media_type=2",
    ]
    series = metadata.get("series")
    series_name = series.get("name") if isinstance(series, dict) else series
    if series_name:
        args += ["-metadata", f"grouping={series_name}", "-metadata", f"series={series_name}"]
    position = metadata.get("series_position") or metadata.get("volume")
    if position:
        args += ["-metadata", f"track={position}"]
    optional = {
        "description": "comment", "asin": "ASIN", "publisher": "publisher",
        "publish_year": "date", "language": "language",
    }
    for key, tag in optional.items():
        if metadata.get(key):
            args += ["-metadata", f"{tag}={metadata[key]}"]
    return args


def _stage_repair(item: dict[str, Any], output_root: Path, staging_root: Path) -> Path:
    source = Path(item["source"]["path"]).resolve()
    staged = (staging_root / item["current_output"]["relative_path"]).resolve()
    staged.parent.mkdir(parents=True, exist_ok=True)
    if staged.exists():
        staged.unlink()
    _run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
        "-map", "0:a:0", "-map", "0:v?", "-map_metadata", "0", "-map_chapters", "0",
        "-c", "copy", *_metadata_args(item["metadata"]),
        "-f", "mp4", str(staged),
    ])
    probe = probe_media(staged)
    if (
        probe.codec_name != "aac"
        or len(probe.chapters) != int(item["source_chapters"])
        or not _duration_ok(float(probe.duration_seconds or 0), float(item["source_duration_seconds"]))
        or _audio_md5(source) != _audio_md5(staged)
    ):
        raise RuntimeError(f"staged chapter repair failed validation: {staged}")
    return staged


def _archive_verified(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if (
            destination.stat().st_size == source.stat().st_size
            and _sha256(destination) == _sha256(source)
        ):
            source.unlink()
            return
        raise RuntimeError(f"existing archive does not match repair source: {destination}")
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        partial.unlink()
    source_digest = hashlib.sha256()
    with source.open("rb") as incoming, partial.open("xb") as outgoing:
        for chunk in iter(lambda: incoming.read(8 * 1024 * 1024), b""):
            source_digest.update(chunk)
            outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    shutil.copystat(source, partial)
    if (
        partial.stat().st_size != source.stat().st_size
        or _sha256(partial) != source_digest.hexdigest()
    ):
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"archive copy verification failed: {source}")
    os.replace(partial, destination)
    source.unlink()


def _repair_one(item: dict[str, Any], *, output_root: Path, staging_root: Path) -> None:
    current = Path(item["current_output"]["path"]).resolve()
    archive = Path(item["archive_destination"]).resolve()
    staged = _stage_repair(item, output_root, staging_root)
    _archive_verified(current, archive)
    try:
        os.replace(staged, current)
        published = probe_media(current)
        if (
            len(published.chapters) != int(item["source_chapters"])
            or not _duration_ok(
                float(published.duration_seconds or 0), float(item["source_duration_seconds"])
            )
        ):
            raise RuntimeError(f"published chapter repair failed validation: {current}")
    except BaseException:
        if not current.exists() and archive.is_file():
            shutil.copy2(archive, current)
        raise


def _prune_empty(start: Path, output_root: Path) -> list[str]:
    removed: list[str] = []
    current = start.resolve()
    while current != output_root and _inside(current, output_root):
        try:
            current.rmdir()
        except OSError:
            break
        removed.append(str(current))
        current = current.parent
    return removed


def _backup_database(database: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    source_connection = sqlite3.connect(database)
    backup_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()


def _reconcile_database(plan: dict[str, Any], database: Path) -> int:
    run_id = int(plan["process_run"]["id"])
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for item in plan["chapter_repairs"]:
            metadata = dict(item["metadata"])
            metadata["_verified_output"] = True
            metadata["_verified_at"] = now
            connection.execute(
                "UPDATE detected_books SET metadata_json=?,confidence=1,updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND book_id=? AND state='complete'",
                (json.dumps(metadata, sort_keys=True), run_id, item["book_id"]),
            )
            connection.execute(
                "INSERT INTO process_events(run_id,level,event,book_id,detail) VALUES (?,'info','chapters_restored',?,?)",
                (run_id, item["book_id"], item["current_output"]["path"]),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return len(plan["chapter_repairs"])


def apply_plan(plan: dict[str, Any], *, workers: int = 2) -> tuple[dict[str, Any], Path]:
    if workers < 1 or workers > 4:
        raise ValueError("workers must be between 1 and 4")
    checked = preflight(plan)
    database: Path = checked["database"]
    output_root: Path = checked["output_root"]
    archive_root: Path = checked["archive_root"]
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    staging_root = output_root / "_repair-staging" / f"chapter-split-{stamp}"
    archive_root.mkdir(parents=True, exist_ok=True)
    backup = archive_root / f"library-state-before-chapter-repair-{stamp}.sqlite3"
    _backup_database(database, backup)
    result: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "applied",
        "source_library_writes": False,
        "permanent_deletions": 0,
        "database_backup": str(backup),
        "chapter_outputs_repaired": 0,
        "chapter_outputs_already_repaired": 0,
        "split_outputs_archived": 0,
        "split_outputs_already_archived": 0,
        "empty_directories_pruned": [],
        "completed_chapter_book_ids": [],
    }
    try:
        chapter_total = len(plan["chapter_repairs"])
        pending: list[dict[str, Any]] = []
        for item, status in zip(
            plan["chapter_repairs"], checked["chapter_statuses"], strict=True
        ):
            if status == "already_repaired":
                result["chapter_outputs_already_repaired"] += 1
                result["completed_chapter_book_ids"].append(item["book_id"])
            else:
                pending.append(item)
        completed = result["chapter_outputs_already_repaired"]
        if completed:
            print(f"[chapters {completed}/{chapter_total}] resumed verified repairs", flush=True)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chapter-repair") as pool:
            futures = {
                pool.submit(
                    _repair_one, item, output_root=output_root, staging_root=staging_root
                ): item
                for item in pending
            }
            for future in as_completed(futures):
                item = futures[future]
                future.result()
                result["chapter_outputs_repaired"] += 1
                result["completed_chapter_book_ids"].append(item["book_id"])
                completed += 1
                print(
                    f"[chapters {completed}/{chapter_total}] {item.get('author')} — {item.get('title')}",
                    flush=True,
                )
        split_total = len(plan["verified_split_sets"])
        for group_index, (group, statuses) in enumerate(zip(
            plan["verified_split_sets"], checked["split_statuses"], strict=True
        ), 1):
            print(
                f"[split set {group_index}/{split_total}] {group.get('author')} — {group.get('part_identity')}",
                flush=True,
            )
            for item, destination_value, status in zip(
                group["legacy_files"], group["archive_destinations"], statuses, strict=True
            ):
                if status == "already_archived":
                    result["split_outputs_already_archived"] += 1
                    continue
                media = Path(item["path"]).resolve()
                parent = media.parent
                _archive_verified(media, Path(destination_value).resolve())
                for companion in item.get("companion_files") or []:
                    source = Path(companion["path"]).resolve()
                    destination = (
                        archive_root / "redundant-split-outputs" / companion["relative_path"]
                    ).resolve()
                    _archive_verified(source, destination)
                result["empty_directories_pruned"].extend(_prune_empty(parent, output_root))
                result["split_outputs_archived"] += 1
        result["database_rows_verified"] = _reconcile_database(plan, database)
    finally:
        if staging_root.is_dir():
            shutil.rmtree(staging_root)
            _prune_empty(staging_root.parent, output_root)
    log = archive_root / f"chapter-split-repair-log-{stamp}.json"
    log.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result, log


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--approved", action="store_true",
        help="confirm the user approved this exact dry-run plan and archive root",
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = json.loads(args.plan.expanduser().resolve().read_text(encoding="utf-8"))
    if args.preflight_only:
        checked = preflight(plan)
        print(json.dumps({
            "preflight": "passed",
            "chapter_repairs": len(checked["chapter_statuses"]),
            "split_sets": len(checked["split_statuses"]),
        }, indent=2))
        return 0
    if not args.approved:
        raise ValueError("execution requires --approved after explicit user approval")
    result, log = apply_plan(plan, workers=args.workers)
    print(json.dumps({**result, "log": str(log)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
