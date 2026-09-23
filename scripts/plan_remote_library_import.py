#!/usr/bin/env python3
"""Plan, but never execute, a cautious import from a reconciled SSH library.

Only single-file M4Bs with a corroborated Audnexus ASIN can become proposed
copies. The report is a review artifact, not authorization to write media.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from audiobook_manager.output import plan_output
from audiobook_manager.providers import AudnexusProvider, latin_display_metadata

from scripts.audit_remote_library import active_outputs, classify, normalized


ASIN = re.compile(r"\[([A-Z0-9]{10})\]", re.I)
RESERVE_BYTES = 40 * 2**30


def compact(value: object) -> str:
    return "".join(re.findall(r"[^\W_]+", str(value).casefold()))


def asin_from_path(path: str) -> str | None:
    match = ASIN.search(path)
    return match.group(1).upper() if match else None


def source_title_matches(title: str, path: str) -> bool:
    """Check the ASIN-bearing source name, not the often-generic album tag."""
    parts = Path(path).parts
    if len(parts) < 3 or parts[0] != "Books":
        return False
    folder = re.sub(r"\s*\[[A-Z0-9]{10}\]$", "", parts[1], flags=re.I)
    filename = re.sub(r"\s*\[[A-Z0-9]{10}\]$", "", Path(parts[-1]).stem, flags=re.I)
    needle = normalized(title)
    return bool(needle) and (needle == normalized(folder) or needle in normalized(filename))


def author_matches(authors: list[str], source_author: str) -> bool:
    source = compact(source_author)
    return any(len(compact(author)) >= 5 and compact(author) in source for author in authors)


def runtime_matches(expected_minutes: Any, actual_seconds: float) -> bool:
    try:
        expected = float(expected_minutes) * 60
    except (TypeError, ValueError):
        return False
    return expected > 0 and actual_seconds > 0 and abs(actual_seconds - expected) <= max(900, expected * 0.08)


def similar_existing_title(title: str, author: str,
                           outputs: list[dict[str, Any]]) -> list[str]:
    """Flag near-identical same-author titles; this is a review hint, not identity proof."""
    title_key = normalized(title)
    author_key = normalized(author)
    if len(title_key) < 12 or not author_key:
        return []
    matches = []
    for item in outputs:
        if item["author"] != author_key:
            continue
        if any(difflib.SequenceMatcher(None, title_key, other).ratio() >= 0.86
               for other in item["titles"] if len(other) >= 12):
            matches.append(item["path"])
    return matches[:5]


def series_folder_alias(destination: Path, output_root: Path) -> list[str]:
    series = destination.parent.parent
    if series.parent == output_root or series.exists() or not series.parent.is_dir():
        return []
    return [str(sibling) for sibling in series.parent.iterdir()
            if sibling.is_dir() and normalized(sibling.name) == normalized(series.name)]


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("metadata cache must be an object")
    return data


def save_cache(path: Path, data: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def lookup(asin: str, cache: dict[str, dict[str, Any]], cache_path: Path,
           *, online: bool) -> tuple[dict[str, Any] | None, str | None]:
    cached = cache.get(asin)
    if cached is not None:
        return cached.get("metadata"), cached.get("error")
    if not online:
        return None, "ASIN has not been checked online"
    time.sleep(0.4)
    try:
        results = AudnexusProvider(asin).search("")
        metadata = results[0] if results else None
        error = None if metadata else "Audnexus returned no result"
    except HTTPError as exc:
        metadata, error = None, f"Audnexus HTTP {exc.code}"
        if exc.code not in {400, 404}:
            return metadata, error
    except (OSError, URLError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
        return None, f"Audnexus unavailable: {exc}"
    cache[asin] = {"metadata": metadata, "error": error,
                   "checked_at": datetime.now().astimezone().isoformat()}
    save_cache(cache_path, cache)
    return metadata, error


def propose(item: dict[str, Any], output_root: Path, outputs: list[dict[str, Any]],
            cache: dict[str, dict[str, Any]], cache_path: Path, *, online: bool) -> dict[str, Any]:
    record = {"source_files": item["source_files"],
              "source_fingerprints": item.get("source_fingerprints", []),
              "source_bytes": item["source_bytes"],
              "source_title": item["title"], "source_author": item["author"],
              "duration_seconds": item["duration_seconds"],
              "previous_classification": item["classification"],
              "matched_outputs": item["matched_outputs"]}
    if item["classification"] != "missing_candidate":
        record["action"] = "no_copy_existing_or_review"
        record["reason"] = "Previous reconciliation found an existing-title or edition/alias issue"
        return record
    files = item["source_files"]
    if len(files) != 1 or not files[0].lower().endswith(".m4b"):
        record["action"] = "review_multipart_or_format"
        record["reason"] = "Requires track-order, completeness and/or conversion review"
        return record
    if (len(record["source_fingerprints"]) != 1 or
            record["source_fingerprints"][0].get("path") != files[0] or
            record["source_fingerprints"][0].get("size") != item["source_bytes"] or
            not isinstance(record["source_fingerprints"][0].get("mtime_ns"), int)):
        record["action"] = "review_missing_fingerprint"
        record["reason"] = "Remote source size/mtime fingerprint is required before execution"
        return record
    asin = asin_from_path(files[0])
    if not asin:
        record["action"] = "review_no_asin"
        record["reason"] = "No ASIN to corroborate this single-file book online"
        return record
    record["asin"] = asin
    metadata, error = lookup(asin, cache, cache_path, online=online)
    if not metadata:
        record["action"] = "review_metadata_unavailable"
        record["reason"] = error or "No metadata"
        return record
    record["metadata"] = {key: metadata.get(key) for key in
                          ("provider", "provider_id", "asin", "title", "authors",
                           "narrators", "series", "publisher", "runtime_minutes")}
    checks = {
        "asin": str(metadata.get("asin") or "").upper() == asin,
        "title": source_title_matches(str(metadata.get("title") or ""), files[0]),
        "author": author_matches(metadata.get("authors") or [], item["author"]),
        "runtime": runtime_matches(metadata.get("runtime_minutes"), item["duration_seconds"]),
        "latin_display": latin_display_metadata(metadata),
    }
    record["identity_checks"] = checks
    if not all(checks.values()):
        record["action"] = "review_identity_mismatch"
        record["reason"] = "ASIN metadata disagrees with source path, author, runtime or English display"
        return record
    classification, matches = classify(str(metadata["title"]), str(metadata["authors"][0]), outputs)
    if classification != "missing_candidate":
        record["action"] = "no_copy_existing_or_review"
        record["reason"] = "Verified metadata matches or may alias an existing output"
        record["matched_outputs"] = matches
        return record
    similar = similar_existing_title(str(metadata["title"]), str(metadata["authors"][0]), outputs)
    if similar:
        record["action"] = "no_copy_existing_or_review"
        record["reason"] = "Verified metadata closely resembles an existing same-author output"
        record["matched_outputs"] = similar
        return record
    destination = plan_output(output_root, metadata).audio
    record["destination"] = str(destination)
    if not destination.resolve().is_relative_to(output_root.resolve()):
        record["action"] = "review_destination_escape"
        record["reason"] = "Resolved destination escapes the output root"
        return record
    aliases = series_folder_alias(destination, output_root)
    if aliases:
        record["action"] = "review_series_folder_alias"
        record["reason"] = "Existing series folder differs only by punctuation/casing; avoid splitting the series"
        record["matched_series_folders"] = aliases
        return record
    if destination.exists():
        record["action"] = "review_destination_exists"
        record["reason"] = "Never overwrite an existing output"
        return record
    record["action"] = "proposed_copy"
    record["reason"] = "ASIN, source title, author and duration agree; no output-title match"
    return record


def build_plan(reconciliation: dict[str, Any], output_root: Path,
               cache: dict[str, dict[str, Any]], cache_path: Path,
               *, online: bool, free_bytes: int) -> dict[str, Any]:
    outputs = active_outputs(output_root)
    operations = [propose(item, output_root, outputs, cache, cache_path, online=online)
                  for item in reconciliation["books"]]
    by_destination: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in operations:
        if item["action"] == "proposed_copy":
            by_destination[item["destination"].casefold()].append(item)
    for group in by_destination.values():
        if len(group) > 1:
            for item in group:
                item["action"] = "review_destination_collision"
                item["reason"] = "Multiple remote books propose the same output path"
    # The largest individual staged copy needs room in addition to the final files.
    proposed = [item for item in operations if item["action"] == "proposed_copy"]
    proposed_bytes = sum(item["source_bytes"] for item in proposed)
    largest = max((item["source_bytes"] for item in proposed), default=0)
    if free_bytes - proposed_bytes - largest < RESERVE_BYTES:
        for item in proposed:
            item["action"] = "defer_disk_budget"
            item["reason"] = "Whole proposed batch does not preserve a 40 GiB reserve plus staging"
    counts = Counter(item["action"] for item in operations)
    bytes_by_action: Counter[str] = Counter()
    for item in operations:
        bytes_by_action[item["action"]] += item["source_bytes"]
    return {"mode": "dry_run_no_media_writes", "generated_at": datetime.now().astimezone().isoformat(),
            "based_on": reconciliation.get("generated_at"),
            "remote_host": reconciliation["remote_host"],
            "remote_root": reconciliation["remote_root"],
            "output_root": str(output_root),
            "metadata_source": "Audnexus ASIN lookup (cached)",
            "summary": {"actions": dict(counts), "bytes_by_action": dict(bytes_by_action),
                        "free_bytes_at_plan": free_bytes, "reserve_bytes": RESERVE_BYTES,
                        "largest_staging_bytes": largest},
            "operations": operations,
            "execution_requirements": [
                "Show this plan and obtain explicit approval before copying any book.",
                "Treat remote and Jupiter source as read-only; never delete or overwrite them.",
                "Before each copy, recheck source size/mtime, destination absence and free space.",
                "Stage in output filesystem, verify checksum, audio stream, duration, chapters and identity, then move atomically.",
                "Retag only the staged copy using verified metadata; retain an import provenance manifest for resumability.",
                "Do not automatically copy review, represented, collision or deferred records."]}


def markdown(plan: dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = ["# Goliath import plan — dry run", "", plan["generated_at"], "",
             "No audiobook media has been copied, modified or removed.", "",
             "## Storage", "",
             f"- Free now: {summary['free_bytes_at_plan'] / 2**30:.1f} GiB",
             f"- Required reserve: {summary['reserve_bytes'] / 2**30:.1f} GiB",
             f"- Largest staging copy: {summary['largest_staging_bytes'] / 2**30:.1f} GiB", "",
             "## Actions", ""]
    for action, count in sorted(summary["actions"].items()):
        gib = summary["bytes_by_action"][action] / 2**30
        lines.append(f"- {action}: {count} groups, {gib:.2f} GiB")
    lines += ["", "## Proposed copies", "",
              "| Author | Title | ASIN | GiB | Destination |",
              "|---|---|---|---:|---|"]
    for item in plan["operations"]:
        if item["action"] != "proposed_copy":
            continue
        metadata = item["metadata"]
        safe = lambda value: str(value).replace("|", "\\|")
        lines.append(f"| {safe(', '.join(metadata['authors']))} | {safe(metadata['title'])} | "
                     f"{item['asin']} | {item['source_bytes']/2**30:.2f} | "
                     f"{safe(item['destination'])} |")
    lines += ["", "## Review and no-copy records", "",
              "| Action | Source author | Source title | Reason |", "|---|---|---|---|"]
    for item in plan["operations"]:
        if item["action"] == "proposed_copy":
            continue
        safe = lambda value: str(value).replace("|", "\\|")
        lines.append(f"| {item['action']} | {safe(item['source_author'])} | "
                     f"{safe(item['source_title'])} | {safe(item['reason'])} |")
    lines += ["", "## Required before execution", "",
              *[f"- {text}" for text in plan["execution_requirements"]], ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconciliation", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("reports/remote-audnexus-cache.json"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    parser.add_argument("--lookup-asins", action="store_true", help="Perform optional rate-limited Audnexus requests")
    args = parser.parse_args()
    reconciliation = json.loads(args.reconciliation.read_text())
    output = args.output_root.resolve()
    if not output.is_dir():
        parser.error("output root must exist")
    cache = load_cache(args.cache)
    plan = build_plan(reconciliation, output, cache, args.cache,
                      online=args.lookup_asins, free_bytes=shutil.disk_usage(output).free)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    base = args.report_dir / f"remote-library-import-plan-{datetime.now():%Y-%m-%d-%H%M%S}"
    base.with_suffix(".json").write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    base.with_suffix(".md").write_text(markdown(plan))
    print(json.dumps({"json": str(base.with_suffix('.json')),
                      "markdown": str(base.with_suffix('.md')),
                      "summary": plan["summary"]}, indent=2))


if __name__ == "__main__":
    main()
