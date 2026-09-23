#!/usr/bin/env python3
"""Verify a completed repair, audit empty output folders, and restart the UI."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


def empty_output_directories(output_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for directory in sorted(output_root.rglob("*"), key=lambda path: str(path).casefold()):
        if not directory.is_dir():
            continue
        relative = directory.relative_to(output_root)
        if not relative.parts or relative.parts[0].startswith("_"):
            continue
        if any(directory.iterdir()):
            continue
        rows.append({
            "path": str(directory.resolve()),
            "relative_path": str(relative),
            "author_folder": relative.parts[0],
            "depth": len(relative.parts),
        })
    return rows


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=check)


def _repair_log(archive_root: Path) -> tuple[Path, dict[str, Any]]:
    logs = sorted(archive_root.glob("chapter-split-repair-log-*.json"), key=lambda path: path.stat().st_mtime_ns)
    if not logs:
        raise RuntimeError("the repair service ended without a completion log")
    path = logs[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def write_empty_reports(
    *, output_root: Path, report_dir: Path, repair_log: Path
) -> tuple[Path, Path, dict[str, Any]]:
    rows = empty_output_directories(output_root)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "read-only",
        "output_root": str(output_root),
        "based_on_repair_log": str(repair_log),
        "summary": {
            "empty_leaf_directories": len(rows),
            "authors_affected": len({row["author_folder"] for row in rows}),
            "david_eddings_empty_directories": sum(
                row["author_folder"].casefold() == "david eddings" for row in rows
            ),
            "files_changed": 0,
            "directories_removed": 0,
        },
        "empty_directories": rows,
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"empty-output-directories-{stamp}.json"
    markdown_path = report_dir / f"empty-output-directories-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        "# Empty output-directory audit",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "This was a read-only audit. No directory or file was removed.",
        "",
        f"- Empty leaf directories: **{payload['summary']['empty_leaf_directories']}**",
        f"- Author folders affected: **{payload['summary']['authors_affected']}**",
        f"- David Eddings empty directories: **{payload['summary']['david_eddings_empty_directories']}**",
        "",
        "| Author folder | Empty relative path |",
        "|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['author_folder'].replace('|', '\\|')} | `{row['relative_path']}` |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path, payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    archive_root = args.archive_root.expanduser().resolve()
    database = args.database.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    report_dir = args.report_dir.expanduser().resolve()
    exit_code = 0
    try:
        try:
            log_path, repair = _repair_log(archive_root)
        except RuntimeError:
            plans = sorted(
                report_dir.glob("chapter-split-repair-dry-run-*.json"),
                key=lambda path: path.stat().st_mtime_ns,
            )
            if not plans:
                raise RuntimeError("no chapter/split dry-run plan is available for recovery")
            _run([
                "/usr/bin/env", "PYTHONPATH=src", "/usr/bin/python3",
                "scripts/apply_chapter_split_repair.py", "--plan", str(plans[-1]),
                "--approved", "--workers", "2",
            ])
            log_path, repair = _repair_log(archive_root)
        repaired = int(repair.get("chapter_outputs_repaired") or 0) + int(
            repair.get("chapter_outputs_already_repaired") or 0
        )
        archived = int(repair.get("split_outputs_archived") or 0) + int(
            repair.get("split_outputs_already_archived") or 0
        )
        if repaired != 237 or archived != 43:
            raise RuntimeError(
                f"repair completion counts are incomplete: chapters={repaired}, split_files={archived}"
            )
        _run([
            "/usr/bin/env", "PYTHONPATH=src", "/usr/bin/python3",
            "scripts/audit_library_alignment.py", "--database", str(database),
            "--source-root", str(source_root), "--output-root", str(output_root),
            "--report-dir", str(report_dir), "--workers", "8",
        ])
        json_path, markdown_path, payload = write_empty_reports(
            output_root=output_root, report_dir=report_dir, repair_log=log_path
        )
        print(json.dumps({
            "repair_log": str(log_path),
            "empty_directory_summary": payload["summary"],
            "empty_directory_json": str(json_path),
            "empty_directory_markdown": str(markdown_path),
        }, indent=2), flush=True)
    except Exception as error:
        print(f"post-repair verification failed: {error}", flush=True)
        exit_code = 1
    finally:
        started = _run([
            "systemctl", "--user", "start",
            "audiobook-manager-api.service", "audiobook-manager-dashboard.service",
        ], check=False)
        if started.returncode:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
