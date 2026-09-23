#!/usr/bin/env python3
"""Dry-run organized placement of verified Goliath M4Bs on Jupiter.

This command never writes audiobook media. It only considers the proposed_copy
subset of a prior ASIN-verified remote import plan.
"""

from __future__ import annotations

import argparse
import difflib
import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from audiobook_manager.display import person_key, prefer_display
from audiobook_manager.output import plan_output

from scripts.audit_remote_library import AUDIO, normalized


RESERVE_BYTES = 100 * 2**30


def media_paths(root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in AUDIO]


def maybe_same_source_book(metadata: dict[str, Any], source_books: list[dict[str, Any]]) -> list[str]:
    title = normalized(metadata["title"])
    author = person_key(metadata["authors"][0])
    matches = []
    for book in source_books:
        if person_key(book.get("canonical_author") or "") != author:
            continue
        other = normalized(book.get("canonical_title") or "")
        if title == other or (len(title) >= 12 and len(other) >= 12 and
                              difflib.SequenceMatcher(None, title, other).ratio() >= 0.88):
            matches.extend(book.get("source_files") or [])
    return matches[:5]


def jupiter_destination(root: Path, metadata: dict[str, Any], asin: str) -> Path:
    target = plan_output(root, metadata).audio
    return target.with_name(f"{target.stem} [{asin}]{target.suffix}")


def build_plan(remote_plan: dict[str, Any], jupiter_root: Path,
               source_paths: list[str], source_books: list[dict[str, Any]],
               *, free_bytes: int) -> dict[str, Any]:
    source_keys = {normalized(path) for path in source_paths}
    candidates = [item for item in remote_plan["operations"] if item["action"] == "proposed_copy"]
    author_spellings: dict[str, list[str]] = defaultdict(list)
    for item in candidates:
        author = str(item["metadata"]["authors"][0])
        author_spellings[person_key(author)].append(author)
    canonical_authors = {key: prefer_display(options[0], options, person_key)
                         for key, options in author_spellings.items()}
    operations = []
    for item in candidates:
        metadata = dict(item["metadata"])
        metadata["authors"] = list(metadata["authors"])
        metadata["authors"][0] = canonical_authors[person_key(metadata["authors"][0])]
        asin = item["asin"]
        target = jupiter_destination(jupiter_root, metadata, asin)
        record = {"action": "proposed_copy_to_jupiter", "remote_source": item["source_files"][0],
                  "source_fingerprint": item["source_fingerprints"][0],
                  "source_bytes": item["source_bytes"],
                  "duration_seconds": item["duration_seconds"], "asin": asin,
                  "metadata": metadata, "jupiter_destination": str(target),
                  "output_destination_from_prior_plan": item["destination"]}
        if not target.resolve().is_relative_to(jupiter_root.resolve()):
            record["action"] = "review_destination_escape"
            record["reason"] = "Resolved destination escapes the Jupiter audiobook root"
        elif target.exists() or normalized(target.relative_to(jupiter_root).as_posix()) in source_keys:
            record["action"] = "review_destination_exists"
            record["reason"] = "Never overwrite an existing Jupiter file"
        elif any(asin.casefold() in path.casefold() for path in source_paths):
            record["action"] = "review_existing_asin"
            record["reason"] = "This ASIN already appears somewhere on Jupiter"
        elif matches := maybe_same_source_book(metadata, source_books):
            record["action"] = "review_possible_jupiter_duplicate"
            record["reason"] = "A same-author title is already in the Jupiter source catalog"
            record["matched_source_files"] = matches
        elif target.parent.is_dir() and any(
                child.is_file() and child.suffix.casefold() in AUDIO for child in target.parent.iterdir()):
            record["action"] = "review_book_folder_has_audio"
            record["reason"] = "The target book folder already contains audio"
        operations.append(record)
    targets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in operations:
        if item["action"] == "proposed_copy_to_jupiter":
            targets[item["jupiter_destination"].casefold()].append(item)
    for group in targets.values():
        if len(group) > 1:
            for item in group:
                item["action"] = "review_target_collision"
                item["reason"] = "Multiple remote books propose the same Jupiter path"
    proposed = [item for item in operations if item["action"] == "proposed_copy_to_jupiter"]
    planned_bytes = sum(item["source_bytes"] for item in proposed)
    largest = max((item["source_bytes"] for item in proposed), default=0)
    if free_bytes - planned_bytes - largest < RESERVE_BYTES:
        for item in proposed:
            item["action"] = "defer_disk_budget"
            item["reason"] = "Batch would consume the 100 GiB reserve plus staging space"
    counts = Counter(item["action"] for item in operations)
    bytes_by_action: Counter[str] = Counter()
    for item in operations:
        bytes_by_action[item["action"]] += item["source_bytes"]
    return {"mode": "dry_run_no_media_writes", "generated_at": datetime.now().astimezone().isoformat(),
            "based_on": remote_plan["generated_at"], "remote_host": remote_plan["remote_host"],
            "remote_root": remote_plan["remote_root"], "jupiter_root": str(jupiter_root),
            "summary": {"actions": dict(counts), "bytes_by_action": dict(bytes_by_action),
                        "current_jupiter_audio_files": len(source_paths),
                        "free_bytes_at_plan": free_bytes, "reserve_bytes": RESERVE_BYTES,
                        "largest_staging_bytes": largest},
            "operations": operations,
            "execution_requirements": [
                "Obtain approval of this exact plan before copying media.",
                "Keep Goliath read-only; never overwrite or delete anything on Jupiter.",
                "Stage on the Jupiter filesystem outside Audiobooks, then verify SHA-256 and ffprobe identity/duration.",
                "Recheck remote size/mtime and Jupiter target absence before each copy.",
                "Publish only a fully validated M4B under its final organized path; retain a resumable provenance manifest.",
                "Do not copy any review records or the 200 remote temporary-file artifacts."]}


def markdown(plan: dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = ["# Goliath → Jupiter import plan — dry run", "", plan["generated_at"], "",
             "No audiobook media has been copied, modified or removed.", "",
             f"Jupiter free: {summary['free_bytes_at_plan']/2**30:.1f} GiB; "
             f"reserved: {summary['reserve_bytes']/2**30:.1f} GiB.", "",
             "## Actions", ""]
    for action, count in sorted(summary["actions"].items()):
        lines.append(f"- {action}: {count} books, {summary['bytes_by_action'][action]/2**30:.2f} GiB")
    lines += ["", "## Proposed copies", "",
              "| Author | Title | ASIN | GiB | Jupiter destination |",
              "|---|---|---|---:|---|"]
    for item in plan["operations"]:
        if item["action"] != "proposed_copy_to_jupiter":
            continue
        safe = lambda value: str(value).replace("|", "\\|")
        meta = item["metadata"]
        lines.append(f"| {safe(meta['authors'][0])} | {safe(meta['title'])} | {item['asin']} | "
                     f"{item['source_bytes']/2**30:.2f} | {safe(item['jupiter_destination'])} |")
    held = [item for item in plan["operations"] if item["action"] != "proposed_copy_to_jupiter"]
    if held:
        lines += ["", "## Held for review", "",
                  "| Author | Title | Action | Reason |", "|---|---|---|---|"]
        for item in held:
            safe = lambda value: str(value).replace("|", "\\|")
            meta = item["metadata"]
            lines.append(f"| {safe(meta['authors'][0])} | {safe(meta['title'])} | "
                         f"{item['action']} | {safe(item['reason'])} |")
    lines += ["", "## Execution requirements", "",
              *[f"- {text}" for text in plan["execution_requirements"]], ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-plan", type=Path, required=True)
    parser.add_argument("--jupiter-root", type=Path, required=True)
    parser.add_argument("--source-audit", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    args = parser.parse_args()
    remote_plan = json.loads(args.remote_plan.read_text())
    source_audit = json.loads(args.source_audit.read_text())
    jupiter = args.jupiter_root.resolve()
    if not jupiter.is_dir():
        parser.error("Jupiter root must exist")
    paths = media_paths(jupiter)
    if len(paths) != source_audit["summary"]["source_media_records"]:
        parser.error("source audit is stale; Jupiter file count changed")
    plan = build_plan(remote_plan, jupiter, paths, source_audit["source_books"],
                      free_bytes=shutil.disk_usage(jupiter).free)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    base = args.report_dir / f"goliath-to-jupiter-plan-{datetime.now():%Y-%m-%d-%H%M%S}"
    base.with_suffix(".json").write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    base.with_suffix(".md").write_text(markdown(plan))
    print(json.dumps({"json": str(base.with_suffix('.json')),
                      "markdown": str(base.with_suffix('.md')),
                      "summary": plan["summary"]}, indent=2))


if __name__ == "__main__":
    main()
