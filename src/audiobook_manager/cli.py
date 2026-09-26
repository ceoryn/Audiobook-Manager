from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Sequence

from .database import StateDatabase
from .grouping import group_files
from .lineage import analyze_scan_report
from .reporting import build_report
from .review import decision_status, filter_items, render_detail, render_queue, review_items
from .scanner import scan_library
from .engine import process_library
from .webserver import serve_review_app
from .configuration import require_outside_source


def _path_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audiobook-manager", description="Safely inspect audiobook libraries")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="read-only media discovery and probing")
    scan.add_argument("library", type=Path, help="library directory to inspect")
    scan.add_argument(
        "--database",
        type=Path,
        default=Path.cwd() / "audiobook-manager.sqlite3",
        help="SQLite cache path (default: ./audiobook-manager.sqlite3)",
    )
    scan.add_argument("--no-cache", action="store_true", help="do not read or write SQLite state")
    scan.add_argument(
        "--workers",
        type=int,
        default=4,
        help="maximum concurrent ffprobe processes (default: 4)",
    )
    scan.add_argument("--report", type=Path, help="write JSON report here instead of stdout")
    analyze = subparsers.add_parser("analyze", help="infer explainable media lineage from a scan report")
    analyze.add_argument("scan_report", type=Path, help="Phase 1 JSON scan report")
    analyze.add_argument("--report", type=Path, help="write Phase 2 JSON here instead of stdout")
    review = subparsers.add_parser("review", help="inspect the Phase 2 relationship queue")
    review.add_argument("analysis_report", type=Path, help="Phase 2 JSON analysis report")
    review.add_argument("--database", type=Path, default=Path.cwd() / "audiobook-manager.sqlite3")
    review.add_argument("--id", dest="relationship_id", help="show one relationship in detail")
    review.add_argument("--band", choices=("high", "medium", "low", "very_low"))
    review.add_argument("--decision", choices=("unreviewed", "confirm", "reject", "defer"))
    review.add_argument("--limit", type=int, default=20)
    decide = subparsers.add_parser("decide", help="persist a human review decision")
    decide.add_argument("analysis_report", type=Path, help="Phase 2 JSON analysis report")
    decide.add_argument("relationship_id", help="stable ID shown by the review command")
    decide.add_argument("decision", choices=("confirm", "reject", "defer"))
    decide.add_argument("--database", type=Path, default=Path.cwd() / "audiobook-manager.sqlite3")
    decide.add_argument("--note", help="optional explanation for the decision")
    web = subparsers.add_parser("web-api", help="serve the local browser review API")
    web.add_argument("--analysis-report", type=Path, default=Path.cwd() / "lineage-analysis.json")
    web.add_argument("--database", type=Path, default=Path.cwd() / "library-state.sqlite3")
    web.add_argument(
        "--config", type=Path, default=Path.cwd() / "audiobook-manager.json",
        help="local JSON configuration path (default: ./audiobook-manager.json)",
    )
    web.add_argument("--source-root", type=Path, help="initial source folder; the UI can change it")
    web.add_argument("--output-root", type=Path, help="initial output folder; the UI can change it")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8788)
    web.add_argument(
        "--prefer-latin-metadata", action="store_true",
        help="auto-publish only Latin-script display metadata when a safe match exists",
    )
    process = subparsers.add_parser("process", help="autonomously build a clean Audiobookshelf library")
    process.add_argument("source", type=Path)
    process.add_argument("destination", type=Path)
    process.add_argument("--database", type=Path, default=Path.cwd() / "library-state.sqlite3")
    process.add_argument("--workers", type=int, default=4)
    process.add_argument("--conversion-workers", type=int, default=4)
    process.add_argument("--metadata-threshold", type=int, default=80)
    process.add_argument("--prefer-latin-metadata", action="store_true")
    return parser


def _scan(args: argparse.Namespace) -> int:
    library = args.library.expanduser().resolve()
    if not library.is_dir():
        print(f"error: library path is not a directory: {library}", file=sys.stderr)
        return 2
    if args.report and _path_within(args.report, library):
        print("error: refusing to write a report inside the scanned library", file=sys.stderr)
        return 2
    if not args.no_cache and _path_within(args.database, library):
        print("error: refusing to write the state database inside the scanned library", file=sys.stderr)
        return 2

    database_context = nullcontext(None) if args.no_cache else StateDatabase(args.database)
    try:
        with database_context as database:
            files = scan_library(library, database=database, workers=args.workers)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = build_report(library, files, group_files(files))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        args.report.expanduser().resolve().write_text(rendered, encoding="utf-8")
        print(
            f"Scanned {report['summary']['media_files']} files in {report['summary']['groups']} groups; "
            f"{report['summary']['probe_errors']} probe errors. Report: {args.report}"
        )
    else:
        print(rendered, end="")
    return 0


def _analyze(args: argparse.Namespace) -> int:
    try:
        scan_report = json.loads(args.scan_report.expanduser().resolve().read_text(encoding="utf-8"))
        library = Path(scan_report["library_root"])
        if args.report and _path_within(args.report, library):
            print("error: refusing to write an analysis report inside the scanned library", file=sys.stderr)
            return 2
        analysis = analyze_scan_report(scan_report)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"error: could not analyze scan report: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(analysis, indent=2, sort_keys=True) + "\n"
    if args.report:
        destination = args.report.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
        summary = analysis["summary"]
        print(
            f"Analyzed {summary['groups_analyzed']} groups; found {summary['relationships']} relationships; "
            f"{summary['groups_requiring_review']} groups require review. Report: {destination}"
        )
    else:
        print(rendered, end="")
    return 0


def _load_analysis(path: Path) -> dict[str, object]:
    return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))


def _review(args: argparse.Namespace) -> int:
    if args.limit < 1:
        print("error: --limit must be at least 1", file=sys.stderr)
        return 2
    try:
        analysis = _load_analysis(args.analysis_report)
        require_outside_source(
            args.database, Path(str(analysis["library_root"])), purpose="state database",
        )
        items = review_items(analysis)
        with StateDatabase(args.database) as database:
            decisions = database.decisions()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"error: could not load review queue: {exc}", file=sys.stderr)
        return 1
    if args.relationship_id:
        item = next((candidate for candidate in items if candidate.relationship_id == args.relationship_id), None)
        if item is None:
            print(f"error: relationship ID not found: {args.relationship_id}", file=sys.stderr)
            return 2
        print(render_detail(item, decisions), end="")
        return 0
    filtered = filter_items(items, band=args.band, decision=args.decision, decisions=decisions)
    print(render_queue(filtered, decisions, limit=args.limit), end="")
    return 0


def _decide(args: argparse.Namespace) -> int:
    try:
        analysis = _load_analysis(args.analysis_report)
        require_outside_source(
            args.database, Path(str(analysis["library_root"])), purpose="state database",
        )
        items = review_items(analysis)
        item = next((candidate for candidate in items if candidate.relationship_id == args.relationship_id), None)
        if item is None:
            print(f"error: relationship ID not found: {args.relationship_id}", file=sys.stderr)
            return 2
        with StateDatabase(args.database) as database:
            database.store_decision(
                relationship_id=item.relationship_id,
                decision=args.decision,
                evidence_fingerprint=item.evidence_fingerprint,
                group_key=item.group_key,
                note=args.note,
            )
            status, stale = decision_status(item, database.decisions())
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"error: could not store decision: {exc}", file=sys.stderr)
        return 1
    print(f"Saved {status} for {item.relationship_id}; evidence current: {not stale}")
    print("No audiobook files were modified. Decisions do not authorize cleanup.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scan":
        return _scan(args)
    if args.command == "analyze":
        return _analyze(args)
    if args.command == "review":
        return _review(args)
    if args.command == "decide":
        return _decide(args)
    if args.command == "web-api":
        try:
            serve_review_app(analysis_path=args.analysis_report, database_path=args.database,
                             config_path=args.config, source_root=args.source_root,
                             output_root=args.output_root,
                             prefer_latin_metadata=args.prefer_latin_metadata,
                             host=args.host, port=args.port)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"error: could not start review API: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "process":
        try:
            summary = process_library(args.source, args.destination, args.database,
                                      workers=args.workers, metadata_threshold=args.metadata_threshold,
                                      conversion_workers=args.conversion_workers,
                                      prefer_latin_metadata=args.prefer_latin_metadata)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"error: processing could not start: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    return 2
