#!/usr/bin/env python3
"""Revalidate and execute an approved targeted problem-book repair plan."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.database import StateDatabase
from audiobook_manager.executor import _existing_output_is_compatible, execute_book
from audiobook_manager.probe import ProbeError, probe_media


MEDIA_SUFFIXES = frozenset({".m4b", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"})
COVER_NAMES = frozenset({"cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "folder.jpg", "folder.png"})


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _latest_run(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM process_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("process database contains no runs")
        return dict(row)
    finally:
        connection.close()


def _book(connection: sqlite3.Connection, run_id: int, book_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM detected_books WHERE run_id=? AND book_id=?",
        (run_id, book_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"planned book record is missing: {book_id}")
    return row


def _sidecars(source: Path, destination: Path) -> list[tuple[Path, Path]]:
    remaining = [
        path for path in source.parent.iterdir()
        if path.is_file() and path.resolve() != source and path.suffix.casefold() in MEDIA_SUFFIXES
    ]
    if remaining:
        return []
    result: list[tuple[Path, Path]] = []
    for path in source.parent.iterdir():
        if path.is_file() and path.name.casefold() in COVER_NAMES:
            target = destination.parent / path.name
            if target.exists():
                raise ValueError(f"quarantine sidecar already exists: {target}")
            result.append((path, target))
    return result


def validate_plan(plan: dict[str, Any]) -> tuple[Path, Path, Path, int]:
    if plan.get("mode") != "dry-run" or plan.get("actions_applied") != 0:
        raise ValueError("only an unapplied dry-run plan can be executed")
    roots = plan.get("roots") or {}
    database = Path(str(roots.get("database") or "")).resolve()
    source_root = Path(str(roots.get("source") or "")).resolve()
    output_root = Path(str(roots.get("output") or "")).resolve()
    if not database.is_file() or not source_root.is_dir() or not output_root.is_dir():
        raise ValueError("a configured repair root is unavailable")
    expected = plan.get("process_run") or {}
    latest = _latest_run(database)
    if (
        expected.get("status") != "complete"
        or latest.get("status") != "complete"
        or int(expected.get("id") or -1) != int(latest["id"])
        or expected.get("finished_at") != latest.get("finished_at")
    ):
        raise ValueError("targeted repair plan is stale or its run is not complete")
    quarantine_root = Path(
        str((plan.get("safety") or {}).get("quarantine_root_if_approved") or "")
    ).resolve()
    if (
        not _inside(quarantine_root, output_root)
        or quarantine_root == output_root
        or quarantine_root.relative_to(output_root).parts[0] != "_quarantine"
    ):
        raise ValueError("quarantine root must be beneath output/_quarantine")

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        seen_outputs: set[str] = set()
        for operation in plan.get("repair_operations") or []:
            book_id = str(operation["book_id"])
            row = _book(connection, int(latest["id"]), book_id)
            if row["state"] == "superseded":
                raise ValueError(f"planned repair book was superseded: {book_id}")
            output = Path(str(operation["output"])).resolve()
            if not _inside(output, output_root) or _inside(output, quarantine_root):
                raise ValueError(f"unsafe planned output: {output}")
            if str(output) in seen_outputs:
                raise ValueError(f"two repair operations target one output: {output}")
            seen_outputs.add(str(output))
            files = list(map(str, operation.get("selected_source_files") or []))
            if not files:
                raise ValueError(f"empty source selection: {book_id}")
            known = {
                *json.loads(row["files_json"]),
                *json.loads(row["alternate_files_json"]),
                *json.loads(row["problem_files_json"]),
            }
            if not set(files) <= set(map(str, known)):
                raise ValueError(f"source selection changed: {book_id}")
            for relative in files:
                source = (source_root / relative).resolve()
                if not _inside(source, source_root) or not source.is_file():
                    raise ValueError(f"repair source is missing or unsafe: {source}")
                try:
                    probe = probe_media(source, timeout_seconds=120)
                except (OSError, ProbeError) as error:
                    raise ValueError(f"repair source is unreadable: {source}: {error}") from error
                if not probe.codec_name or not probe.duration_seconds:
                    raise ValueError(f"repair source has no readable audio: {source}")
            blocker = operation.get("blocking_output_quarantine")
            if blocker:
                destination = Path(str(blocker)).resolve()
                expected_destination = (quarantine_root / output.relative_to(output_root)).resolve()
                if destination != expected_destination:
                    raise ValueError(f"blocking-output destination changed: {output}")
            prior = operation.get("prior_output_quarantine")
            if prior:
                old_output = Path(str(prior["source"])).resolve()
                old_destination = Path(str(prior["destination"])).resolve()
                if (old_output == output or not _inside(old_output, output_root)
                        or str(old_output) != row["output_path"] or not old_output.is_file()
                        or old_destination != (quarantine_root / old_output.relative_to(output_root)).resolve()
                        or old_destination.exists()):
                    raise ValueError(f"superseded output changed since the dry run: {old_output}")
        for merge in plan.get("record_merges") or []:
            source = _book(connection, int(latest["id"]), str(merge["book_id"]))
            target = _book(connection, int(latest["id"]), str(merge["into"]))
            if source["state"] == "superseded":
                continue
            if target["state"] != "complete" or not target["output_path"]:
                raise ValueError(f"merge target is not complete: {merge['into']}")
    finally:
        connection.close()
    return database, source_root, output_root, int(latest["id"])


def _backup_database(database: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(database)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


def _atomic_log(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _archive_output(
    source: Path, destination: Path, output_root: Path,
    result: dict[str, Any], record: dict[str, Any], label: str, log_path: Path,
) -> None:
    if not source.is_file() or destination.exists():
        raise ValueError(f"output changed before quarantine: {source}")
    sidecars = _sidecars(source, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    record["pending_quarantine_move"] = {"source": str(source), "destination": str(destination)}
    _atomic_log(log_path, result)
    source.rename(destination)
    result["blocking_outputs_moved"] += 1
    record[label] = str(destination)
    record.pop("pending_quarantine_move", None)
    _atomic_log(log_path, result)
    for sidecar, target in sidecars:
        target.parent.mkdir(parents=True, exist_ok=True)
        sidecar.rename(target)
        result["sidecars_moved"] += 1
    current = source.parent
    while current != output_root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _prepare_book(
    database: Path, run_id: int, operation: dict[str, Any]
) -> None:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            row = _book(connection, run_id, str(operation["book_id"]))
            selected = list(map(str, operation["selected_source_files"]))
            previous = [
                *json.loads(row["files_json"]),
                *json.loads(row["alternate_files_json"]),
            ]
            alternates = list(dict.fromkeys(
                str(value) for value in previous if str(value) not in set(selected)
            ))
            connection.execute(
                """UPDATE detected_books
                      SET state='metadata_matched', classification=?, files_json=?,
                          alternate_files_json=?, metadata_json=?, confidence=1.0,
                          output_path=NULL, failure=NULL, quarantine_path=NULL,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE run_id=? AND book_id=?""",
                (
                    operation["classification"],
                    json.dumps(selected),
                    json.dumps(alternates),
                    json.dumps(operation["metadata"], ensure_ascii=False, sort_keys=True),
                    run_id,
                    operation["book_id"],
                ),
            )
            connection.execute(
                "DELETE FROM output_claims WHERE run_id=? AND book_id=?",
                (run_id, operation["book_id"]),
            )
    finally:
        connection.close()


def _merge_record(database: Path, run_id: int, merge: dict[str, Any]) -> bool:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            source = _book(connection, run_id, str(merge["book_id"]))
            if source["state"] == "superseded":
                return False
            target = _book(connection, run_id, str(merge["into"]))
            alternates = list(dict.fromkeys([
                *map(str, json.loads(target["alternate_files_json"])),
                *map(str, merge["files_attached_as_alternates"]),
            ]))
            connection.execute(
                "UPDATE detected_books SET alternate_files_json=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND book_id=?",
                (json.dumps(alternates), run_id, merge["into"]),
            )
            connection.execute(
                """UPDATE detected_books
                      SET state='superseded', output_path=NULL,
                          failure=?, updated_at=CURRENT_TIMESTAMP
                    WHERE run_id=? AND book_id=?""",
                (f"merged into {merge['into']} by approved repair plan", run_id, merge["book_id"]),
            )
            connection.execute(
                "DELETE FROM output_claims WHERE run_id=? AND book_id=?",
                (run_id, merge["book_id"]),
            )
            return True
    finally:
        connection.close()


def _refresh_summary(database: Path, run_id: int) -> dict[str, int]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            counts = Counter(
                str(row["state"])
                for row in connection.execute(
                    "SELECT state FROM detected_books WHERE run_id=? AND state!='superseded'",
                    (run_id,),
                )
            )
            raw = connection.execute(
                "SELECT summary_json FROM process_runs WHERE id=?", (run_id,)
            ).fetchone()
            summary = json.loads(raw["summary_json"] or "{}")
            summary.update(
                {
                    "books_discovered": sum(counts.values()),
                    "books_successfully_processed": counts["complete"],
                    "books_quarantined": counts["quarantined"],
                    "failures": counts["failed"],
                    "books_needing_metadata_review": counts["local_metadata"],
                }
            )
            connection.execute(
                "UPDATE process_runs SET summary_json=? WHERE id=?",
                (json.dumps(summary, sort_keys=True), run_id),
            )
            return dict(counts)
    finally:
        connection.close()


def execute(plan: dict[str, Any], *, approve: bool) -> tuple[dict[str, Any], Path | None]:
    database, source_root, output_root, run_id = validate_plan(plan)
    quarantine_root = Path(plan["safety"]["quarantine_root_if_approved"]).resolve()
    result: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "applied" if approve else "preflight-only",
        "source_library_writes": False,
        "permanent_deletions": 0,
        "run_id": run_id,
        "database_backup": None,
        "operations_planned": len(plan.get("repair_operations") or []),
        "operations_complete": 0,
        "operations_failed": 0,
        "blocking_outputs_moved": 0,
        "sidecars_moved": 0,
        "records_merged": 0,
        "operations": [],
    }
    if not approve:
        return result, None

    quarantine_root.mkdir(parents=True, exist_ok=True)
    backup = quarantine_root / f"library-state-before-targeted-repair-run-{run_id}.sqlite3"
    if not backup.exists():
        _backup_database(database, backup)
    result["database_backup"] = str(backup)
    log_path = quarantine_root / "targeted-repair-log.json"
    _atomic_log(log_path, result)

    for merge in plan.get("record_merges") or []:
        if _merge_record(database, run_id, merge):
            result["records_merged"] += 1
            _atomic_log(log_path, result)

    for operation in plan.get("repair_operations") or []:
        book_id = str(operation["book_id"])
        output = Path(str(operation["output"])).resolve()
        record = {"book_id": book_id, "output": str(output), "status": "started"}
        result["operations"].append(record)
        _atomic_log(log_path, result)
        try:
            compatible = False
            if output.is_file():
                compatible, detail = _existing_output_is_compatible(
                    output,
                    source_root,
                    list(map(str, operation["selected_source_files"])),
                    dict(operation["metadata"]),
                )
                record["existing_output_check"] = detail
            if output.is_file() and not compatible:
                planned_quarantine = operation.get("blocking_output_quarantine")
                if not planned_quarantine:
                    raise ValueError(
                        "an incompatible output appeared after the dry run; "
                        "generate a new plan before continuing"
                    )
                destination = Path(str(planned_quarantine)).resolve()
                _archive_output(output, destination, output_root, result, record,
                                "blocking_output_moved_to", log_path)
            prior = operation.get("prior_output_quarantine")
            if prior:
                _archive_output(Path(prior["source"]), Path(prior["destination"]),
                                output_root, result, record,
                                "superseded_output_moved_to", log_path)
            _prepare_book(database, run_id, operation)
            with StateDatabase(database) as state:
                created = execute_book(state, run_id, book_id)
            record.update({"status": "complete", "created_or_adopted": created})
            result["operations_complete"] += 1
        except (OSError, RuntimeError, ValueError) as error:
            record.update({"status": "failed", "error": str(error)})
            result["operations_failed"] += 1
        _atomic_log(log_path, result)

    result["final_states"] = _refresh_summary(database, run_id)
    _atomic_log(log_path, result)
    return result, log_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", type=Path, required=True)
    parser.add_argument("--approve", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = json.loads(args.plan_json.expanduser().resolve().read_text(encoding="utf-8"))
    result, log = execute(plan, approve=args.approve)
    print(json.dumps({
        "mode": result["mode"],
        "operations_planned": result["operations_planned"],
        "operations_complete": result["operations_complete"],
        "operations_failed": result["operations_failed"],
        "blocking_outputs_moved": result["blocking_outputs_moved"],
        "records_merged": result["records_merged"],
        "log": str(log) if log else None,
    }, indent=2))
    return 1 if result["operations_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
