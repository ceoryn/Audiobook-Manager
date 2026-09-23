#!/usr/bin/env python3
"""Plan and apply a safe English-series and cover-only output repair.

The planner records every source, destination, and filesystem fingerprint. The
executor first creates and validates all canonical M4Bs, then moves obsolete
book directories and cover-only artifacts into recoverable quarantine. It
never writes to the source library and never permanently deletes a file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.probe import probe_media


AUDIO_SUFFIXES = frozenset({".m4b", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"})
ART_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _run(command: list[str], *, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or f"command exited {completed.returncode}")
    return completed


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


def _file_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or not _inside(resolved, root):
        raise ValueError(f"unsafe or missing file: {path}")
    stat = resolved.stat()
    return {
        "relative_path": str(resolved.relative_to(root)),
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
    }


def _directory_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_dir() or not _inside(resolved, root) or resolved == root:
        raise ValueError(f"unsafe or missing directory: {path}")
    if any(item.is_symlink() for item in resolved.rglob("*")):
        raise ValueError(f"repair directory contains a symbolic link: {path}")
    return {
        "relative_path": str(resolved.relative_to(root)),
        "files": [
            _file_record(item, root)
            for item in sorted(resolved.rglob("*"))
            if item.is_file()
        ],
    }


def _media_details(path: Path) -> dict[str, Any]:
    probe = probe_media(path)
    stream_payload = json.loads(_run([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name:stream_disposition=attached_pic",
        "-of", "json", "--", str(path),
    ]).stdout)
    videos = [stream for stream in stream_payload.get("streams", []) if stream.get("codec_type") == "video"]
    return {
        "codec": probe.codec_name,
        "duration_seconds": round(float(probe.duration_seconds or 0), 3),
        "chapters": len(probe.chapters),
        "video_streams": len(videos),
        "attached_pictures": sum(int((stream.get("disposition") or {}).get("attached_pic") or 0) for stream in videos),
    }


def _audio_md5(path: Path) -> str:
    output = _run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-map", "0:a:0", "-c", "copy", "-f", "md5", "-",
    ]).stdout.strip()
    if not output.startswith("MD5="):
        raise RuntimeError(f"could not fingerprint audio stream: {path}")
    return output


def _cover_only_directories(output_root: Path) -> list[Path]:
    rows: list[Path] = []
    for directory in output_root.rglob("*"):
        if not directory.is_dir():
            continue
        relative = directory.relative_to(output_root)
        if not relative.parts or relative.parts[0].startswith("_") or directory.name.startswith("audiobook-manager-"):
            continue
        files = [item for item in directory.iterdir() if item.is_file()]
        if (
            files
            and any(item.suffix.casefold() in ART_SUFFIXES for item in files)
            and not any(item.is_file() and item.suffix.casefold() in AUDIO_SUFFIXES for item in directory.rglob("*"))
        ):
            rows.append(directory.resolve())
    return sorted(rows)


def _source_books(source_series: Path, first: int, last: int) -> list[tuple[int, Path]]:
    discovered: dict[int, Path] = {}
    for directory in source_series.iterdir():
        if not directory.is_dir():
            continue
        match = re.search(r"\bbook\s+(\d+)\b", directory.name, flags=re.I)
        if not match:
            continue
        number = int(match.group(1))
        candidates = [item for item in directory.iterdir() if item.is_file() and item.suffix.casefold() == ".m4b"]
        if number in discovered or len(candidates) != 1:
            raise ValueError(f"volume {number} does not resolve to exactly one source M4B")
        discovered[number] = candidates[0].resolve()
    expected = list(range(first, last + 1))
    if sorted(number for number in discovered if first <= number <= last) != expected:
        raise ValueError(f"source series does not contain exactly volumes {first} through {last}")
    return [(number, discovered[number]) for number in expected]


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    source_root = args.source_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    database = args.database.expanduser().resolve()
    source_series = (source_root / args.source_series_directory).resolve()
    if not source_root.is_dir() or not source_series.is_dir() or not output_root.is_dir() or not database.is_file():
        raise ValueError("source, source-series, output, or database path is unavailable")
    if not _inside(source_series, source_root):
        raise ValueError("source series directory escapes source root")
    run = _latest_run(database)
    if run["status"] != "complete" or not run["finished_at"]:
        raise ValueError("output repair requires a completed processing run")

    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    quarantine_root = output_root / "_quarantine" / f"output-hygiene-run-{run['id']}-{stamp}"
    canonical_root = output_root / args.canonical_author / args.canonical_series
    books: list[dict[str, Any]] = []
    for number, source in _source_books(source_series, args.first_volume, args.last_volume):
        title = f"{args.canonical_series} Vol. {number}"
        directory = canonical_root / f"{number:02d} - {title}"
        target = directory / f"{args.canonical_author} - {title}.m4b"
        cover = directory / "cover.jpg"
        if target.exists() or cover.exists():
            raise ValueError(f"canonical repair target already exists: {directory}")
        details = _media_details(source)
        if details["codec"] != "aac" or not details["duration_seconds"] or not details["chapters"]:
            raise ValueError(f"source is not a readable chaptered AAC M4B: {source}")
        books.append({
            "volume": number,
            "title": title,
            "author": args.canonical_author,
            "series": args.canonical_series,
            "source": str(source),
            "source_record": _file_record(source, source_root),
            "source_media": details,
            "target": str(target.resolve()),
            "cover": str(cover.resolve()),
        })

    marker = args.series_marker.casefold()
    old_book_directories = sorted({
        path.parent.resolve()
        for path in output_root.rglob("*.m4b")
        if not path.relative_to(output_root).parts[0].startswith("_")
        and marker in str(path.relative_to(output_root)).casefold()
    })
    cover_only = _cover_only_directories(output_root)
    if any(_inside(target, source) or _inside(source, target)
           for source in old_book_directories for target in cover_only):
        raise ValueError("cover-only and obsolete-book repair directories overlap")

    quarantine: list[dict[str, Any]] = []
    for reason, directories in (
        ("obsolete_or_duplicate_series_output", old_book_directories),
        ("cover_only_incomplete_output", cover_only),
    ):
        for directory in directories:
            relative = directory.relative_to(output_root)
            quarantine.append({
                "reason": reason,
                "source": str(directory),
                "destination": str((quarantine_root / reason / relative).resolve()),
                "record": _directory_record(directory, output_root),
            })

    sources = [item["source"] for item in quarantine]
    destinations = [item["destination"] for item in quarantine]
    if len(sources) != len(set(sources)) or len(destinations) != len(set(destinations)):
        raise ValueError("repair plan contains duplicate directory paths")
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "dry-run",
        "actions_applied": 0,
        "roots": {
            "source": str(source_root), "output": str(output_root), "database": str(database),
            "source_series": str(source_series), "quarantine": str(quarantine_root.resolve()),
        },
        "process_run": run,
        "canonical": {
            "author": args.canonical_author,
            "series": args.canonical_series,
            "language": "eng",
        },
        "summary": {
            "canonical_books_to_create": len(books),
            "obsolete_series_directories_to_quarantine": len(old_book_directories),
            "cover_only_directories_to_quarantine": len(cover_only),
            "source_library_writes": 0,
            "permanent_deletions": 0,
        },
        "canonical_books": books,
        "quarantine_directories": quarantine,
    }


def _validate_directory_record(item: dict[str, Any], output_root: Path) -> Path:
    source = Path(str(item["source"])).resolve()
    expected = item["record"]
    current = _directory_record(source, output_root)
    if current != expected:
        raise ValueError(f"repair directory changed after planning: {source}")
    destination = Path(str(item["destination"])).resolve()
    if destination.exists():
        raise ValueError(f"quarantine destination already exists: {destination}")
    return source


def _validate_book(book: dict[str, Any], source_root: Path, output_root: Path,
                   *, allow_existing: bool) -> tuple[Path, Path, Path]:
    source = Path(str(book["source"])).resolve()
    target = Path(str(book["target"])).resolve()
    cover = Path(str(book["cover"])).resolve()
    if not _inside(source, source_root) or not _inside(target, output_root) or not _inside(cover, output_root):
        raise ValueError("canonical book path escapes configured roots")
    if _file_record(source, source_root) != book["source_record"]:
        raise ValueError(f"source changed after planning: {source}")
    if _media_details(source) != book["source_media"]:
        raise ValueError(f"source media changed after planning: {source}")
    if not allow_existing and (target.exists() or cover.exists()):
        raise ValueError(f"canonical target unexpectedly exists: {target.parent}")
    return source, target, cover


def _validate_canonical(book: dict[str, Any], source: Path, target: Path, cover: Path) -> None:
    source_details = book["source_media"]
    output = probe_media(target)
    if output.codec_name != "aac" or abs(float(output.duration_seconds or 0) - float(source_details["duration_seconds"])) > 1.0:
        raise RuntimeError(f"canonical duration or codec validation failed: {target}")
    if len(output.chapters) != int(source_details["chapters"]):
        raise RuntimeError(f"canonical chapter validation failed: {target}")
    if _media_details(target)["video_streams"] != int(source_details["video_streams"]):
        raise RuntimeError(f"canonical cover-stream validation failed: {target}")
    expected_title = str(book["title"])
    if output.tags.get("title") != expected_title or output.tags.get("artist") != str(book["author"]):
        raise RuntimeError(f"canonical English metadata validation failed: {target}")
    if _audio_md5(source) != _audio_md5(target):
        raise RuntimeError(f"canonical audio stream differs from source: {target}")
    if not cover.is_file() or cover.stat().st_size < 1000:
        raise RuntimeError(f"canonical cover extraction failed: {cover}")
    magic = cover.read_bytes()[:8]
    if not (magic.startswith(b"\xff\xd8\xff") or magic == b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(f"canonical cover file is not a JPEG or PNG: {cover}")


def _create_canonical(book: dict[str, Any], source: Path, target: Path, cover: Path,
                      output_root: Path, canonical: dict[str, Any]) -> bool:
    if target.exists() and cover.exists():
        _validate_canonical(book, source, target, cover)
        return False
    if target.exists() or cover.exists():
        raise ValueError(f"partial canonical target already exists: {target.parent}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="audiobook-manager-retag-", dir=output_root) as temporary:
        staged = Path(temporary) / "output.m4b"
        staged_cover = Path(temporary) / "cover.jpg"
        title = str(book["title"])
        author = str(canonical["author"])
        series = str(canonical["series"])
        volume = str(book["volume"])
        _run([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
            "-map", "0:a:0", "-map", "0:v?", "-map_metadata", "0", "-map_chapters", "0",
            "-c", "copy", "-movflags", "+faststart",
            "-metadata", f"title={title}", "-metadata", f"album={title}",
            "-metadata", f"artist={author}", "-metadata", f"album_artist={author}",
            "-metadata", f"author={author}", "-metadata", f"grouping={series}",
            "-metadata", f"series={series}", "-metadata", f"track={volume}",
            "-metadata:s:a:0", "language=eng", str(staged),
        ])
        _run([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(staged),
            "-map", "0:v:0", "-frames:v", "1", str(staged_cover),
        ])
        # Validate staged files before publishing either one.
        published_book = {**book, "target": str(staged), "cover": str(staged_cover)}
        _validate_canonical(published_book, source, staged, staged_cover)
        try:
            os.link(staged, target)
            os.link(staged_cover, cover)
        except FileExistsError as error:
            raise RuntimeError(f"canonical destination changed during publication: {target.parent}") from error
    _validate_canonical(book, source, target, cover)
    return True


def _prune_empty(start: Path, output_root: Path) -> list[str]:
    removed: list[str] = []
    current = start.resolve()
    while current != output_root:
        try:
            current.rmdir()
        except OSError:
            break
        removed.append(str(current))
        current = current.parent
    return removed


def apply_plan(plan: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    if plan.get("mode") != "dry-run" or plan.get("actions_applied") != 0:
        raise ValueError("only an unapplied dry-run plan can be executed")
    roots = plan["roots"]
    source_root = Path(str(roots["source"])).resolve()
    output_root = Path(str(roots["output"])).resolve()
    database = Path(str(roots["database"])).resolve()
    quarantine_root = Path(str(roots["quarantine"])).resolve()
    if not source_root.is_dir() or not output_root.is_dir() or not database.is_file():
        raise ValueError("repair roots are unavailable")
    if not _inside(quarantine_root, output_root) or quarantine_root.relative_to(output_root).parts[0] != "_quarantine":
        raise ValueError("quarantine root must remain beneath output/_quarantine")
    latest = _latest_run(database)
    expected_run = plan["process_run"]
    if latest["status"] != "complete" or latest["id"] != expected_run["id"] or latest["finished_at"] != expected_run["finished_at"]:
        raise ValueError("repair plan is stale or a processing run is active")

    checked_books = [
        (*_validate_book(book, source_root, output_root, allow_existing=True), book)
        for book in plan["canonical_books"]
    ]
    checked_directories: list[tuple[Path, Path, dict[str, Any]]] = []
    for item in plan["quarantine_directories"]:
        source = _validate_directory_record(item, output_root)
        destination = Path(str(item["destination"])).resolve()
        if not _inside(destination, quarantine_root):
            raise ValueError(f"quarantine destination escapes its root: {destination}")
        checked_directories.append((source, destination, item))

    result: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "applied",
        "source_library_writes": False,
        "permanent_deletions": 0,
        "canonical_books_planned": len(checked_books),
        "canonical_books_created": 0,
        "canonical_books_already_valid": 0,
        "directories_quarantined": 0,
        "empty_directories_pruned": [],
        "canonical_outputs": [],
        "quarantined_directories": [],
    }
    for source, target, cover, book in checked_books:
        created = _create_canonical(book, source, target, cover, output_root, plan["canonical"])
        result["canonical_books_created" if created else "canonical_books_already_valid"] += 1
        result["canonical_outputs"].append(str(target))

    quarantine_root.mkdir(parents=True, exist_ok=True)
    for source, destination, item in checked_directories:
        if not source.is_dir() or destination.exists():
            raise RuntimeError(f"repair directory changed after preflight: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent = source.parent
        source.rename(destination)
        result["directories_quarantined"] += 1
        result["quarantined_directories"].append({
            "source": str(source), "destination": str(destination), "reason": item["reason"],
        })
        result["empty_directories_pruned"].extend(_prune_empty(parent, output_root))

    log = quarantine_root / "repair-log.json"
    log.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result, log


def reconcile_database(
    plan: dict[str, Any], *, backup_path: Path, report_path: Path
) -> dict[str, Any]:
    """Point the completed run at validated canonical outputs and mark them reusable."""
    roots = plan["roots"]
    source_root = Path(str(roots["source"])).resolve()
    output_root = Path(str(roots["output"])).resolve()
    database = Path(str(roots["database"])).resolve()
    quarantine_root = Path(str(roots["quarantine"])).resolve()
    backup_path = backup_path.expanduser().resolve()
    report_path = report_path.expanduser().resolve()
    if backup_path.exists() or report_path.exists():
        raise ValueError("database backup or reconciliation report already exists")
    latest = _latest_run(database)
    expected_run = plan["process_run"]
    if latest["id"] != expected_run["id"] or latest["status"] != "complete":
        raise ValueError("database no longer matches the completed repair run")
    repair_log = quarantine_root / "repair-log.json"
    if not repair_log.is_file():
        raise ValueError("the applied filesystem repair log is missing")
    applied = json.loads(repair_log.read_text(encoding="utf-8"))
    expected_outputs = {str(Path(str(book["target"])).resolve()) for book in plan["canonical_books"]}
    if applied.get("mode") != "applied" or set(applied.get("canonical_outputs") or []) != expected_outputs:
        raise ValueError("the applied repair log does not match the dry-run plan")

    checked: list[tuple[dict[str, Any], Path]] = []
    for book in plan["canonical_books"]:
        source, target, cover = _validate_book(
            book, source_root, output_root, allow_existing=True
        )
        if not target.is_file() or not cover.is_file():
            raise ValueError(f"canonical output is incomplete: {target.parent}")
        _validate_canonical(book, source, target, cover)
        checked.append((book, target))

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    reconciled_at = datetime.now().astimezone().isoformat(timespec="seconds")
    updates: list[dict[str, Any]] = []
    with sqlite3.connect(database, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        with sqlite3.connect(backup_path) as backup:
            connection.backup(backup)

        run_id = int(expected_run["id"])
        active_rows = connection.execute(
            """SELECT book_id, state, files_json, output_path
                 FROM detected_books WHERE run_id=? AND state!='superseded'""",
            (run_id,),
        ).fetchall()
        claimed_books: set[str] = set()
        prepared: list[tuple[sqlite3.Row, dict[str, Any], Path]] = []
        for book, target in checked:
            relative_source = str(book["source_record"]["relative_path"])
            matching = [
                row for row in active_rows
                if relative_source in json.loads(row["files_json"])
            ]
            if len(matching) != 1:
                raise ValueError(
                    f"source volume maps to {len(matching)} active database records: {relative_source}"
                )
            row = matching[0]
            book_id = str(row["book_id"])
            if book_id in claimed_books:
                raise ValueError(f"multiple canonical volumes map to database book {book_id}")
            claimed_books.add(book_id)
            other_owner = connection.execute(
                "SELECT book_id FROM output_claims WHERE run_id=? AND output_path=? AND book_id!=?",
                (run_id, str(target), book_id),
            ).fetchone()
            if other_owner is not None:
                raise ValueError(
                    f"canonical output is claimed by another identity: {other_owner['book_id']}"
                )
            prepared.append((row, book, target))

        for row, book, target in prepared:
            book_id = str(row["book_id"])
            metadata = {
                "title": str(book["title"]),
                "authors": [str(book["author"])],
                "series": str(book["series"]),
                "series_position": str(book["volume"]),
                "language": str(plan["canonical"].get("language") or "eng"),
                "_metadata_source": "verified_output_repair",
                "_verified_output": True,
                "_verified_at": reconciled_at,
            }
            connection.execute(
                """UPDATE detected_books
                      SET state='complete', metadata_json=?, confidence=1, output_path=?,
                          failure=NULL, quarantine_path=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE run_id=? AND book_id=?""",
                (json.dumps(metadata, sort_keys=True), str(target), run_id, book_id),
            )
            connection.execute(
                "DELETE FROM output_claims WHERE run_id=? AND book_id=?", (run_id, book_id)
            )
            connection.execute(
                "INSERT INTO output_claims(run_id, output_path, book_id) VALUES (?, ?, ?)",
                (run_id, str(target), book_id),
            )
            connection.execute(
                """INSERT INTO process_events(run_id, level, event, book_id, detail)
                   VALUES (?, 'info', 'output_hygiene_reconciled', ?, ?)""",
                (run_id, book_id, str(target)),
            )
            updates.append({
                "book_id": book_id,
                "previous_state": str(row["state"]),
                "previous_output": row["output_path"],
                "canonical_output": str(target),
            })

        rows = connection.execute(
            "SELECT state, metadata_json FROM detected_books WHERE run_id=? AND state!='superseded'",
            (run_id,),
        ).fetchall()
        counts: dict[str, int] = {}
        review_count = 0
        for row in rows:
            state = str(row["state"])
            counts[state] = counts.get(state, 0) + 1
            raw_metadata = row["metadata_json"]
            if raw_metadata and json.loads(raw_metadata).get("_needs_metadata_review"):
                review_count += 1
        raw_summary = connection.execute(
            "SELECT summary_json FROM process_runs WHERE id=?", (run_id,)
        ).fetchone()["summary_json"]
        summary = json.loads(raw_summary or "{}")
        summary.update({
            "books_discovered": len(rows),
            "books_successfully_processed": counts.get("complete", 0),
            "books_quarantined": counts.get("quarantined", 0),
            "failures": counts.get("failed", 0),
            "metadata_unresolved": counts.get("identified", 0),
            "metadata_retry": counts.get("metadata_retry", 0),
            "books_needing_metadata_review": review_count,
        })
        connection.execute(
            "UPDATE process_runs SET summary_json=? WHERE id=?",
            (json.dumps(summary, sort_keys=True), run_id),
        )

    result = {
        "mode": "database_reconciled",
        "reconciled_at": reconciled_at,
        "run_id": int(expected_run["id"]),
        "records_updated": len(updates),
        "database_backup": str(backup_path),
        "filesystem_changes": 0,
        "source_library_writes": 0,
        "updates": updates,
    }
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def render_markdown(plan: dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = [
        "# Output hygiene repair — dry run", "",
        "No source file was changed and no output action was applied.", "",
        f"- Canonical English M4Bs to create: **{summary['canonical_books_to_create']}**",
        f"- Obsolete series directories to quarantine: **{summary['obsolete_series_directories_to_quarantine']}**",
        f"- Cover-only directories to quarantine: **{summary['cover_only_directories_to_quarantine']}**",
        "- Audio re-encodes: **0**", "- Permanent deletions: **0**", "",
        "## Canonical books", "",
    ]
    lines.extend(
        f"- Volume {item['volume']}: `{item['source']}` → `{item['target']}` "
        f"({item['source_media']['chapters']} chapters)"
        for item in plan["canonical_books"]
    )
    lines += ["", "## Quarantine directories", ""]
    lines.extend(
        f"- `{item['source']}` → `{item['destination']}` — {item['reason']}"
        for item in plan["quarantine_directories"]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--source-root", type=Path, required=True)
    plan.add_argument("--output-root", type=Path, required=True)
    plan.add_argument("--database", type=Path, required=True)
    plan.add_argument("--source-series-directory", required=True)
    plan.add_argument("--canonical-author", required=True)
    plan.add_argument("--canonical-series", required=True)
    plan.add_argument("--series-marker", required=True)
    plan.add_argument("--first-volume", type=int, default=1)
    plan.add_argument("--last-volume", type=int, required=True)
    plan.add_argument("--json", type=Path, required=True)
    plan.add_argument("--markdown", type=Path, required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan-json", type=Path, required=True)
    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--plan-json", type=Path, required=True)
    reconcile.add_argument("--backup", type=Path, required=True)
    reconcile.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "plan":
        plan = build_plan(args)
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.markdown.write_text(render_markdown(plan), encoding="utf-8")
        print(json.dumps({"mode": "dry-run", **plan["summary"], "json": str(args.json), "markdown": str(args.markdown)}, indent=2))
        return 0
    plan = json.loads(args.plan_json.expanduser().resolve().read_text(encoding="utf-8"))
    if args.command == "reconcile":
        result = reconcile_database(plan, backup_path=args.backup, report_path=args.report)
        print(json.dumps(result, indent=2))
        return 0
    result, log = apply_plan(plan)
    print(json.dumps({
        "mode": result["mode"],
        "canonical_books_created": result["canonical_books_created"],
        "canonical_books_already_valid": result["canonical_books_already_valid"],
        "directories_quarantined": result["directories_quarantined"],
        "log": str(log),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
