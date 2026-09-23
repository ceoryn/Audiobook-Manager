#!/usr/bin/env python3
"""Build a no-change plan for explicitly evidenced problem-book repairs.

The correction document supplies book identities and metadata; this planner
derives all source selections, output paths, media probes, blocker handling,
and safety preconditions from the current database and filesystem.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.database import StateDatabase
from audiobook_manager.executor import _existing_output_is_compatible
from audiobook_manager.output import normalize_output_metadata, plan_output
from audiobook_manager.probe import ProbeError, media_input_args, probe_media


def _book_map(database: StateDatabase, run_id: int) -> dict[str, dict[str, Any]]:
    return {
        str(book["book_id"]): book
        for book in database.process_books(run_id)
        if book["state"] != "superseded"
    }


def _selection(book: dict[str, Any], correction: dict[str, Any]) -> list[str]:
    requested = correction.get("selection", "selected")
    if requested == "selected":
        files = list(map(str, book["files"]))
    elif requested == "alternates":
        files = list(map(str, book["alternate_files"]))
    elif isinstance(requested, list):
        files = list(map(str, requested))
        known = {
            *map(str, book["files"]),
            *map(str, book["alternate_files"]),
            *map(str, book["problem_files"]),
        }
        if not set(files) <= known:
            raise ValueError(f"selection contains an unrecorded source file: {book['book_id']}")
    else:
        raise ValueError(f"invalid selection for {book['book_id']}: {requested!r}")
    if not files:
        raise ValueError(f"empty source selection for {book['book_id']}")
    return files


def _source_probe(source_root: Path, relative: str) -> dict[str, Any]:
    path = (source_root / relative).resolve()
    if not path.is_relative_to(source_root) or not path.is_file():
        raise ValueError(f"missing or unsafe source file: {relative}")
    try:
        probe = probe_media(path, timeout_seconds=120)
    except (OSError, ProbeError) as error:
        return {"path": relative, "error": str(error)}
    return {
        "path": relative,
        "error": None,
        "duration_seconds": probe.duration_seconds,
        "codec": probe.codec_name,
        "special_input_args": media_input_args(path),
    }


def build_plan(database_path: Path, corrections_path: Path,
               metadata_overrides_path: Path | None = None) -> dict[str, Any]:
    corrections = json.loads(corrections_path.read_text(encoding="utf-8"))
    overrides = (json.loads(metadata_overrides_path.read_text(encoding="utf-8"))
                 if metadata_overrides_path else {})
    if not isinstance(overrides, dict):
        raise ValueError("metadata overrides must be a book-id to metadata mapping")
    planned_ids = {str(item["book_id"]) for item in corrections.get("repairs") or []}
    if not set(overrides) <= planned_ids:
        raise ValueError("metadata override names a book outside the correction plan")
    with StateDatabase(database_path) as database:
        status = database.latest_process_status()
        if not status or status.get("status") != "complete":
            raise ValueError("targeted repair requires a completed latest run")
        run_id = int(status["id"])
        run = database.process_run(run_id)
        if run is None:
            raise ValueError("latest process run is missing")
        books = _book_map(database, run_id)

    source_root = Path(str(run["source_root"])).resolve()
    output_root = Path(str(run["destination_root"])).resolve()
    if int(corrections.get("expected_run_id", run_id)) != run_id:
        raise ValueError("correction document was prepared for a different process run")
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    quarantine_root = output_root / "_quarantine" / f"targeted-repair-run-{run_id}-{stamp}"
    operations: list[dict[str, Any]] = []
    for correction in corrections.get("repairs") or []:
        book_id = str(correction["book_id"])
        book = books.get(book_id)
        if book is None:
            raise ValueError(f"active book is missing: {book_id}")
        expected_states = set(map(str, correction.get("expected_states") or []))
        if expected_states and str(book["state"]) not in expected_states:
            raise ValueError(
                f"book state changed for {book_id}: {book['state']} not in {sorted(expected_states)}"
            )
        files = _selection(book, correction)
        probes = [_source_probe(source_root, relative) for relative in files]
        readable = [item for item in probes if not item["error"] and item["duration_seconds"]]
        if len(readable) != len(probes):
            raise ValueError(f"selected repair source is not fully readable: {book_id}")
        metadata = dict(correction["metadata"])
        if book_id in overrides:
            changes = overrides[book_id]
            if not isinstance(changes, dict) or set(changes) != {"title"}:
                raise ValueError("curated metadata override must contain only a title")
            title = str(changes["title"]).strip()
            if not title or len(title) > 160:
                raise ValueError("curated title must be nonempty and at most 160 characters")
            metadata["title"] = title
        metadata["_metadata_source"] = "verified_repair_plan"
        metadata["_needs_metadata_review"] = False
        normalized = normalize_output_metadata(output_root, metadata)
        output = plan_output(output_root, normalized).audio.resolve()
        if not output.is_relative_to(output_root):
            raise ValueError(f"planned output escapes library: {output}")
        existing = output.is_file()
        compatible = False
        compatibility = "output does not exist"
        if existing:
            compatible, compatibility = _existing_output_is_compatible(
                output, source_root, files, normalized
            )
        relative_output = output.relative_to(output_root)
        prior_output = Path(str(book["output_path"])).resolve() if book.get("output_path") else None
        if prior_output and not prior_output.is_relative_to(output_root):
            raise ValueError(f"prior output escapes library: {prior_output}")
        prior_quarantine = (
            {"source": str(prior_output),
             "destination": str(quarantine_root / prior_output.relative_to(output_root))}
            if prior_output and prior_output != output and prior_output.is_file() else None
        )
        operations.append(
            {
                "book_id": book_id,
                "prior_state": book["state"],
                "prior_classification": book["classification"],
                "action": "adopt_existing" if compatible else "rebuild",
                "classification": (
                    "complete_m4b"
                    if len(files) == 1 and Path(files[0]).suffix.casefold() in {".m4a", ".m4b"}
                    else "convert_single" if len(files) == 1 else "combine_components"
                ),
                "selected_source_files": files,
                "source_probes": probes,
                "expected_duration_seconds": round(
                    sum(float(item["duration_seconds"]) for item in readable), 3
                ),
                "metadata": normalized,
                "output": str(output),
                "relative_output": str(relative_output),
                "existing_output": existing,
                "existing_output_compatible": compatible,
                "existing_output_check": compatibility,
                "blocking_output_quarantine": (
                    str(quarantine_root / relative_output) if existing and not compatible else None
                ),
                "prior_output_quarantine": prior_quarantine,
                "evidence": list(map(str, correction.get("evidence") or [])),
            }
        )

    merges: list[dict[str, Any]] = []
    for correction in corrections.get("merge_records") or []:
        source_id = str(correction["book_id"])
        target_id = str(correction["into"])
        source = books.get(source_id)
        target = books.get(target_id)
        if source is None or target is None:
            raise ValueError(f"merge record is missing: {source_id} -> {target_id}")
        files = list(dict.fromkeys([
            *map(str, source["files"]),
            *map(str, source["alternate_files"]),
            *map(str, source["problem_files"]),
        ]))
        merges.append(
            {
                "book_id": source_id,
                "into": target_id,
                "prior_state": source["state"],
                "files_attached_as_alternates": files,
                "evidence": list(map(str, correction.get("evidence") or [])),
            }
        )

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "dry-run",
        "actions_applied": 0,
        "process_run": {
            "id": run_id,
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        },
        "roots": {
            "database": str(database_path.resolve()),
            "source": str(source_root),
            "output": str(output_root),
        },
        "safety": {
            "source_library_writes": False,
            "permanent_deletions_planned": 0,
            "output_writes_require_approval": True,
            "quarantine_root_if_approved": str(quarantine_root),
        },
        "summary": {
            "books_to_repair": len(operations),
            "existing_outputs_to_adopt": sum(item["action"] == "adopt_existing" for item in operations),
            "outputs_to_build": sum(item["action"] == "rebuild" for item in operations),
            "blocking_outputs_to_quarantine": sum(bool(item["blocking_output_quarantine"]) +
                                                 bool(item["prior_output_quarantine"])
                                                 for item in operations),
            "curated_title_overrides": len(overrides),
            "duplicate_database_records_to_merge": len(merges),
            "source_files_selected": sum(len(item["selected_source_files"]) for item in operations),
            "unrecoverable_books_preserved_for_review": len(corrections.get("preserved_unrecoverable") or []),
        },
        "repair_operations": operations,
        "record_merges": merges,
        "preserved_unrecoverable": corrections.get("preserved_unrecoverable") or [],
    }


def _write_markdown(path: Path, plan: dict[str, Any]) -> None:
    summary = plan["summary"]
    lines = [
        "# Targeted problem-book repair dry run",
        "",
        f"Generated: {plan['generated_at']}",
        "",
        "**No source or output audiobook was changed. Explicit approval is required to execute this plan.**",
        "",
        f"- Books repaired or adopted: **{summary['books_to_repair']}**",
        f"- Existing verified outputs adopted: **{summary['existing_outputs_to_adopt']}**",
        f"- Outputs rebuilt from source: **{summary['outputs_to_build']}**",
        f"- Incompatible blockers moved to recoverable quarantine first: **{summary['blocking_outputs_to_quarantine']}**",
        f"- Reviewed title overrides: **{summary['curated_title_overrides']}**",
        f"- Duplicate database records merged: **{summary['duplicate_database_records_to_merge']}**",
        f"- Source-damaged books retained for review: **{summary['unrecoverable_books_preserved_for_review']}**",
        "",
        "## Planned repairs",
        "",
        "| Book | Prior state | Action | Source files | Output |",
        "|---|---|---|---:|---|",
    ]
    lines.extend(
        f"| {item['book_id']} | {item['prior_state']} | {item['action']} | "
        f"{len(item['selected_source_files'])} | {item['relative_output']} |"
        for item in plan["repair_operations"]
    )
    prior_moves = [item for item in plan["repair_operations"] if item["prior_output_quarantine"]]
    if prior_moves:
        lines += ["", "## Superseded output paths to archive", ""]
        for item in prior_moves:
            prior = item["prior_output_quarantine"]
            lines.append(f"- {item['book_id']}: `{prior['source']}` → `{prior['destination']}`")
    lines += ["", "## Preserved source-damaged books", ""]
    if plan["preserved_unrecoverable"]:
        lines += ["| Book | Reason |", "|---|---|"]
        lines.extend(
            f"| {item['book_id']} | {item['reason']} |"
            for item in plan["preserved_unrecoverable"]
        )
    else:
        lines.append("None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--corrections", type=Path, required=True)
    parser.add_argument("--metadata-overrides", type=Path,
                        help="reviewed book-id to title overrides; data, not program rules")
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.database.expanduser().resolve()
    corrections = args.corrections.expanduser().resolve()
    plan = build_plan(database, corrections,
                      args.metadata_overrides.expanduser().resolve()
                      if args.metadata_overrides else None)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    report_dir = args.report_dir.expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"targeted-repair-dry-run-{stamp}.json"
    markdown_path = report_dir / f"targeted-repair-dry-run-{stamp}.md"
    json_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_markdown(markdown_path, plan)
    print(json.dumps({"summary": plan["summary"], "json": str(json_path), "markdown": str(markdown_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
