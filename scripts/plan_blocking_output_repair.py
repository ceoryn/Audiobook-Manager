#!/usr/bin/env python3
"""Plan recoverable quarantine for stale outputs blocking a completed run."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.database import StateDatabase
from audiobook_manager.executor import _existing_output_is_compatible
from audiobook_manager.output import normalize_output_metadata, plan_output
from audiobook_manager.probe import probe_media


def build_plan(database_path: Path) -> dict[str, Any]:
    with StateDatabase(database_path) as database:
        status = database.latest_process_status()
        if not status or status.get("status") != "complete":
            raise ValueError("blocking-output repair requires a completed latest run")
        run_id = int(status["id"])
        run = database.process_run(run_id)
        if run is None:
            raise ValueError("completed process run is missing")
        source_root = Path(str(run["source_root"])).resolve()
        output_root = Path(str(run["destination_root"])).resolve()
        candidates = [
            book for book in database.process_books(run_id)
            if book["state"] == "quarantined"
            and book["classification"] != "quarantine"
            and book.get("metadata")
            and not str(book.get("failure") or "").startswith(
                "output path is already claimed"
            )
        ]

    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    quarantine_root = output_root / "_quarantine" / f"blocking-outputs-run-{run_id}-{stamp}"
    operations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for book in candidates:
        metadata = normalize_output_metadata(output_root, dict(book["metadata"]))
        output = plan_output(output_root, metadata).audio.resolve()
        files = [str(value) for value in book["files"]]
        base = {
            "book_id": book["book_id"],
            "classification": book["classification"],
            "failure": book.get("failure"),
            "metadata": metadata,
            "selected_source_files": files,
            "output": str(output),
        }
        if not output.is_file():
            skipped.append({**base, "reason": "blocking output is no longer present"})
            continue
        compatible, current_reason = _existing_output_is_compatible(
            output, source_root, files, metadata
        )
        if compatible:
            skipped.append({**base, "reason": "output is now compatible; preserve it"})
            continue
        try:
            observed = probe_media(output).to_dict()
        except (OSError, RuntimeError) as error:
            observed = {"error": str(error)}
        relative = output.relative_to(output_root)
        operations.append(
            {
                **base,
                "action": "quarantine_blocking_output",
                "relative_path": str(relative),
                "destination": str(quarantine_root / relative),
                "revalidated_conflict": current_reason,
                "observed_output_probe": observed,
                "preconditions": [
                    "latest completed run still matches this plan",
                    "every selected source file still exists and remains probeable",
                    "the existing output is rechecked and still incompatible immediately before moving",
                    "the destination does not exist",
                ],
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
            "quarantine_root_if_approved": str(quarantine_root),
        },
        "summary": {
            "quarantined_books_examined": len(candidates),
            "blocking_outputs_to_quarantine": len(operations),
            "outputs_preserved_or_missing": len(skipped),
        },
        "blocking_output_quarantine_operations": operations,
        "skipped": skipped,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.database.expanduser().resolve()
    plan = build_plan(database)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    report_dir = args.report_dir.expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"blocking-output-repair-dry-run-{stamp}.json"
    markdown_path = report_dir / f"blocking-output-repair-dry-run-{stamp}.md"
    json_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = plan["summary"]
    lines = [
        "# Blocking output repair dry run",
        "",
        f"Generated: {plan['generated_at']}",
        "",
        "No file was changed. Approved execution moves stale blocking outputs to recoverable quarantine; it never writes to the source library and never deletes media.",
        "",
        f"- Quarantined books examined: **{summary['quarantined_books_examined']}**",
        f"- Blocking outputs eligible for quarantine: **{summary['blocking_outputs_to_quarantine']}**",
        f"- Preserved or missing outputs: **{summary['outputs_preserved_or_missing']}**",
        "",
        "## Planned moves",
        "",
        "| Book | Existing output | Revalidated conflict |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| {item['book_id']} | {item['relative_path']} | {item['revalidated_conflict']} |"
        for item in plan["blocking_output_quarantine_operations"]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"summary": summary, "json": str(json_path), "markdown": str(markdown_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
