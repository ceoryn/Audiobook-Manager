#!/usr/bin/env python3
"""Revalidate and apply an approved blocking-output quarantine plan."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.executor import _existing_output_is_compatible
from audiobook_manager.probe import probe_media


MEDIA_SUFFIXES = frozenset({".m4b", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"})
COVER_NAMES = frozenset({"cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "folder.jpg", "folder.png"})


def _planned_sidecars(source: Path, destination: Path) -> list[dict[str, str]]:
    remaining_media = [
        path for path in source.parent.iterdir()
        if path.is_file() and path.resolve() != source and path.suffix.casefold() in MEDIA_SUFFIXES
    ]
    if remaining_media:
        return []
    moves: list[dict[str, str]] = []
    for path in source.parent.iterdir():
        if not path.is_file() or path.name.casefold() not in COVER_NAMES:
            continue
        target = destination.parent / path.name
        if target.exists():
            raise ValueError(f"quarantine sidecar destination already exists: {target}")
        moves.append({"source": str(path.resolve()), "destination": str(target.resolve())})
    return moves


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


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


def preflight(plan: dict[str, Any]) -> tuple[Path, list[dict[str, Any]]]:
    if plan.get("mode") != "dry-run" or plan.get("actions_applied") != 0:
        raise ValueError("only an unapplied dry-run plan can be executed")
    roots = plan.get("roots") or {}
    database = Path(str(roots.get("database") or "")).resolve()
    source_root = Path(str(roots.get("source") or "")).resolve()
    output_root = Path(str(roots.get("output") or "")).resolve()
    if not database.is_file() or not source_root.is_dir() or not output_root.is_dir():
        raise ValueError("a configured repair root is unavailable")
    expected_run = plan.get("process_run") or {}
    latest_run = _latest_run(database)
    if (
        expected_run.get("status") != "complete"
        or latest_run.get("status") != "complete"
        or int(expected_run.get("id") or -1) != int(latest_run["id"])
        or expected_run.get("finished_at") != latest_run.get("finished_at")
    ):
        raise ValueError("blocking-output plan is stale or its run is not complete")
    quarantine_root = Path(
        str((plan.get("safety") or {}).get("quarantine_root_if_approved") or "")
    ).resolve()
    if (
        not _inside(quarantine_root, output_root)
        or quarantine_root == output_root
        or quarantine_root.relative_to(output_root).parts[0] != "_quarantine"
    ):
        raise ValueError("quarantine root must be beneath output/_quarantine")

    checked: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_destinations: set[str] = set()
    for operation in plan.get("blocking_output_quarantine_operations") or []:
        if operation.get("action") != "quarantine_blocking_output":
            raise ValueError(f"unsupported repair action: {operation.get('action')!r}")
        output = Path(str(operation.get("output") or "")).resolve()
        destination = Path(str(operation.get("destination") or "")).resolve()
        relative = Path(str(operation.get("relative_path") or ""))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError(f"unsafe relative path: {relative}")
        if destination != (quarantine_root / relative).resolve():
            raise ValueError(f"destination does not match quarantine layout: {output}")
        if not _inside(output, output_root) or _inside(output, quarantine_root):
            raise ValueError(f"blocking output is outside the clean output tree: {output}")
        if str(output) in seen_sources or str(destination) in seen_destinations:
            raise ValueError("plan contains duplicate paths")
        seen_sources.add(str(output))
        seen_destinations.add(str(destination))

        already_quarantined = False
        if output.is_file() and destination.exists():
            raise ValueError(f"both blocking output and destination exist: {output}")
        if output.is_file():
            media_path = output
        elif destination.is_file():
            media_path = destination
            already_quarantined = True
        else:
            raise ValueError(f"blocking output is missing: {output}")
        selected = [str(value) for value in operation.get("selected_source_files") or []]
        if not selected:
            raise ValueError(f"operation has no selected source files: {output}")
        for relative_source in selected:
            source_path = (source_root / relative_source).resolve()
            if not _inside(source_path, source_root) or not source_path.is_file():
                raise ValueError(f"selected source file is missing or unsafe: {source_path}")
            source_probe = probe_media(source_path)
            if not source_probe.codec_name or not source_probe.duration_seconds:
                raise ValueError(f"selected source file is not probeable audio: {source_path}")
        compatible, reason = _existing_output_is_compatible(
            media_path, source_root, selected, dict(operation.get("metadata") or {})
        )
        if compatible:
            raise ValueError(f"blocking output became compatible and must be preserved: {output}")
        checked.append(
            {
                "book_id": operation["book_id"],
                "source": str(output),
                "media_path": str(media_path),
                "destination": str(destination),
                "already_quarantined": already_quarantined,
                "revalidated_conflict": reason,
                "sidecars": [] if already_quarantined else _planned_sidecars(output, destination),
            }
        )
    return quarantine_root, checked


def execute(plan: dict[str, Any], *, approve: bool) -> tuple[dict[str, Any], Path | None]:
    quarantine_root, operations = preflight(plan)
    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "applied" if approve else "preflight-only",
        "source_library_writes": False,
        "permanent_deletions": 0,
        "quarantine_root": str(quarantine_root),
        "operations_planned": len(operations),
        "operations_moved": 0,
        "operations_already_quarantined": sum(
            item["already_quarantined"] for item in operations
        ),
        "sidecars_moved": 0,
        "operations": operations,
    }
    if not approve:
        return result, None
    quarantine_root.mkdir(parents=True, exist_ok=True)
    for operation in operations:
        if operation["already_quarantined"]:
            continue
        source = Path(operation["source"])
        destination = Path(operation["destination"])
        if not source.is_file() or destination.exists():
            raise RuntimeError(f"filesystem changed after preflight: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        result["operations_moved"] += 1
        for sidecar_move in operation["sidecars"]:
            sidecar = Path(sidecar_move["source"])
            target = Path(sidecar_move["destination"])
            if not sidecar.is_file() or target.exists():
                raise RuntimeError(f"sidecar changed after preflight: {sidecar}")
            target.parent.mkdir(parents=True, exist_ok=True)
            sidecar.rename(target)
            result["sidecars_moved"] += 1
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    log_path = quarantine_root / f"repair-log-{stamp}.json"
    log_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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
        "operations_moved": result["operations_moved"],
        "operations_already_quarantined": result["operations_already_quarantined"],
        "log": str(log) if log else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
