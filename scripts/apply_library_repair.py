#!/usr/bin/env python3
"""Revalidate and apply an approved audiobook-library quarantine plan.

This executor never touches the source library and never deletes media. It
performs a complete preflight before moving any redundant output, then moves
each approved file into the plan's timestamped quarantine directory. Reusing
the same plan is safe: an already moved and still-valid file is recorded as
already quarantined.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .audit_library_alignment import (
        person_matches,
        probe_summary,
        text_similarity,
        title_matches,
    )
except ImportError:  # Direct execution from the scripts directory.
    from audit_library_alignment import (  # type: ignore[no-redef]
        person_matches,
        probe_summary,
        text_similarity,
        title_matches,
    )


@dataclass(frozen=True)
class Preflight:
    source: str
    media_path: str
    destination: str
    reference_output: str
    already_quarantined: bool
    duration_difference_seconds: float
    duration_difference_ratio: float
    source_title_similarity: float
    reference_title_similarity: float
    sidecars: tuple[tuple[str, str], ...]


MEDIA_SUFFIXES = frozenset({".m4b", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"})
COVER_NAMES = frozenset({"cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "folder.jpg", "folder.png"})


def _planned_sidecars(source: Path, destination: Path) -> tuple[tuple[str, str], ...]:
    """Carry cover art when this move removes the directory's final audiobook."""
    remaining_media = [
        path for path in source.parent.iterdir()
        if path.is_file() and path.resolve() != source and path.suffix.casefold() in MEDIA_SUFFIXES
    ]
    if remaining_media:
        return ()
    moves: list[tuple[str, str]] = []
    for path in source.parent.iterdir():
        if not path.is_file() or path.name.casefold() not in COVER_NAMES:
            continue
        target = destination.parent / path.name
        if target.exists():
            raise ValueError(f"quarantine sidecar destination already exists: {target}")
        moves.append((str(path.resolve()), str(target.resolve())))
    return tuple(moves)


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _clean_relative(value: object) -> Path:
    relative = Path(str(value))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"unsafe relative path in repair plan: {value!r}")
    return relative


def _identity_support(
    *, path: Path, output_root: Path, author: str, title: str, probe: Any
) -> tuple[bool, float]:
    relative = path.relative_to(output_root)
    path_author = relative.parts[0] if relative.parts else ""
    path_title = relative.parent.name
    author_ok = person_matches(author, path_author) or any(
        person_matches(author, value) for value in probe.author_tags
    )
    title_scores = [text_similarity(title, path_title)]
    title_scores.extend(text_similarity(title, value) for value in probe.title_tags)
    title_score = max(title_scores, default=0.0)
    title_ok = title_matches(title, path_title, threshold=0.82) or any(
        title_matches(title, value, threshold=0.82) for value in probe.title_tags
    )
    return author_ok and title_ok, title_score


def _usable_aac(probe: Any) -> bool:
    return bool(
        not probe.error
        and probe.codec == "aac"
        and probe.duration_seconds
        and float(probe.duration_seconds) > 0
    )


def _latest_run(database: Path) -> dict[str, Any]:
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
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


def validate_plan(plan: dict[str, Any]) -> tuple[Path, Path, Path, list[dict[str, Any]]]:
    if plan.get("mode") != "dry-run" or plan.get("actions_applied") != 0:
        raise ValueError("only an unapplied dry-run repair plan can be executed")
    roots = plan.get("roots") or {}
    source_root = Path(str(roots.get("source") or "")).resolve()
    output_root = Path(str(roots.get("output") or "")).resolve()
    database = Path(str(roots.get("database") or "")).resolve()
    if not source_root.is_dir() or not output_root.is_dir() or not database.is_file():
        raise ValueError("the plan's source, output, or database root is unavailable")

    run = plan.get("process_run") or {}
    latest = _latest_run(database)
    if run.get("status") != "complete" or latest.get("status") != "complete":
        raise ValueError("repair cleanup requires a completed fresh processing run")
    if int(run.get("id") or -1) != int(latest["id"]):
        raise ValueError("the repair plan is stale because a newer processing run exists")
    if not run.get("finished_at") or run.get("finished_at") != latest.get("finished_at"):
        raise ValueError("the completed run no longer matches the audited run")

    quarantine_root = Path(
        str((plan.get("safety") or {}).get("quarantine_root_if_approved") or "")
    ).resolve()
    if not _inside(quarantine_root, output_root) or quarantine_root == output_root:
        raise ValueError("quarantine root must be a child of the output library")
    try:
        first_relative = quarantine_root.relative_to(output_root).parts[0]
    except (ValueError, IndexError) as error:
        raise ValueError("invalid quarantine root") from error
    if first_relative != "_quarantine":
        raise ValueError("quarantine root must be beneath output/_quarantine")

    operations = list(plan.get("post_rebuild_quarantine_operations") or [])
    sources = [str(Path(str(item.get("source") or "")).resolve()) for item in operations]
    destinations = [
        str(Path(str(item.get("destination") or "")).resolve()) for item in operations
    ]
    if len(set(sources)) != len(sources) or len(set(destinations)) != len(destinations):
        raise ValueError("repair plan contains duplicate source or destination paths")
    return source_root, output_root, quarantine_root, operations


def preflight_operation(
    operation: dict[str, Any], *, output_root: Path, quarantine_root: Path
) -> Preflight:
    if operation.get("action") != "quarantine_redundant_output":
        raise ValueError(f"unsupported repair action: {operation.get('action')!r}")
    source = Path(str(operation.get("source") or "")).resolve()
    reference = Path(str(operation.get("reference_output") or "")).resolve()
    destination = Path(str(operation.get("destination") or "")).resolve()
    relative = _clean_relative(operation.get("relative_path"))
    expected_destination = (quarantine_root / relative).resolve()
    if destination != expected_destination:
        raise ValueError(f"destination does not match quarantine layout: {source}")
    for label, path in (("source", source), ("reference", reference)):
        if not _inside(path, output_root) or _inside(path, quarantine_root):
            raise ValueError(f"{label} is outside the clean output tree: {path}")
    if not _inside(destination, quarantine_root):
        raise ValueError(f"destination escapes quarantine root: {destination}")
    if source == reference:
        raise ValueError(f"source and reference are the same file: {source}")
    if not reference.is_file():
        raise ValueError(f"reference output is missing: {reference}")

    already_quarantined = False
    if source.is_file() and destination.exists():
        raise ValueError(f"both source and quarantine destination exist: {source}")
    if source.is_file():
        media_path = source
    elif destination.is_file():
        media_path = destination
        already_quarantined = True
    else:
        raise ValueError(f"planned redundant output is missing: {source}")

    source_probe = probe_summary(media_path)
    reference_probe = probe_summary(reference)
    if not _usable_aac(source_probe):
        raise ValueError(f"planned redundant output is not readable AAC: {media_path}")
    if not _usable_aac(reference_probe):
        raise ValueError(f"current reference output is not readable AAC: {reference}")
    left = float(source_probe.duration_seconds)
    right = float(reference_probe.duration_seconds)
    difference = abs(left - right)
    ratio = difference / max(left, right, 1.0)
    if difference > max(5.0, max(left, right) * 0.005):
        raise ValueError(
            f"duration changed or no longer matches ({difference:.3f}s): {source}"
        )

    author = str(operation.get("observed_author") or "").strip()
    title = str(operation.get("observed_title") or "").strip()
    if not author or not title:
        raise ValueError(f"repair operation lacks an observed identity: {source}")
    source_ok, source_title_score = _identity_support(
        path=source,
        output_root=output_root,
        author=author,
        title=title,
        probe=source_probe,
    )
    reference_ok, reference_title_score = _identity_support(
        path=reference,
        output_root=output_root,
        author=author,
        title=title,
        probe=reference_probe,
    )
    if not source_ok or not reference_ok:
        raise ValueError(f"embedded/path identity no longer agrees: {source}")
    return Preflight(
        source=str(source),
        media_path=str(media_path),
        destination=str(destination),
        reference_output=str(reference),
        already_quarantined=already_quarantined,
        duration_difference_seconds=round(difference, 3),
        duration_difference_ratio=round(ratio, 6),
        source_title_similarity=round(source_title_score, 3),
        reference_title_similarity=round(reference_title_score, 3),
        sidecars=() if already_quarantined else _planned_sidecars(source, destination),
    )


def execute_plan(
    plan: dict[str, Any], *, approve: bool
) -> tuple[dict[str, Any], Path | None]:
    source_root, output_root, quarantine_root, operations = validate_plan(plan)
    preflight = [
        preflight_operation(item, output_root=output_root, quarantine_root=quarantine_root)
        for item in operations
    ]
    source_paths = {item.source for item in preflight if not item.already_quarantined}
    references = {item.reference_output for item in preflight}
    overlap = source_paths & references
    if overlap:
        raise ValueError(f"a planned source is also a required reference: {sorted(overlap)[0]}")

    result: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "applied" if approve else "preflight-only",
        "source_library_writes": False,
        "permanent_deletions": 0,
        "source_root": str(source_root),
        "output_root": str(output_root),
        "quarantine_root": str(quarantine_root),
        "operations_planned": len(preflight),
        "operations_moved": 0,
        "operations_already_quarantined": sum(item.already_quarantined for item in preflight),
        "sidecars_moved": 0,
        "operations": [asdict(item) for item in preflight],
    }
    if not approve:
        return result, None

    quarantine_root.mkdir(parents=True, exist_ok=True)
    for item in preflight:
        if item.already_quarantined:
            continue
        source = Path(item.source)
        destination = Path(item.destination)
        if not source.is_file() or destination.exists():
            raise RuntimeError(f"filesystem changed after preflight: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        result["operations_moved"] += 1
        for sidecar_source, sidecar_destination in item.sidecars:
            sidecar = Path(sidecar_source)
            target = Path(sidecar_destination)
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
    parser.add_argument(
        "--approve",
        action="store_true",
        help="apply the fully revalidated moves; omission performs preflight only",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_path = args.plan_json.expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result, log_path = execute_plan(plan, approve=args.approve)
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "operations_planned": result["operations_planned"],
                "operations_moved": result["operations_moved"],
                "operations_already_quarantined": result["operations_already_quarantined"],
                "log": str(log_path) if log_path else None,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
