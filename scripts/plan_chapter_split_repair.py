#!/usr/bin/env python3
"""Plan chapter-preserving remuxes and retirement of verified split outputs.

This planner is deliberately read-only.  It finds current M4Bs that lost
chapters when an AAC M4A/M4B source was remuxed, and legacy part/disc outputs
whose unique durations add up to one clean current whole-book output.  The
resulting JSON records exact file size/mtime preconditions for a later,
separately approved executor.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .audit_library_alignment import person_matches, significant_tokens
except ImportError:  # Direct execution from the scripts directory.
    from audit_library_alignment import person_matches, significant_tokens  # type: ignore[no-redef]


PART_PATTERNS = (
    re.compile(r"\s*\[\s*\d{1,3}(?:\s*[-–]\s*\d{1,3})?\s*\]\s*$", re.I),
    re.compile(r"\s*\(\s*(?:disk|disc)\s*\d+\s*\)\s*(?:author interview)?\s*$", re.I),
    re.compile(r"\s*[,\-]?\s*(?:part|pt)\.?\s*\d+\s*$", re.I),
    re.compile(r"\s*\(\s*\d+\s+of\s+\d+\s*\)\s*$", re.I),
    re.compile(r"\s+\d+\s+of\s+\d+\s*$", re.I),
)
EDITION_NOISE = frozenset({
    "adaptation", "audio", "audiobook", "dramatized", "graphic", "graphicaudio"
})


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def part_base(value: object) -> str | None:
    text = str(value).strip()
    for pattern in PART_PATTERNS:
        stripped = pattern.sub("", text).strip(" ,-–")
        if stripped != text:
            return normalized(stripped)
    return None


def part_number(value: object) -> int | None:
    text = str(value).strip()
    patterns = (
        r"\[\s*(\d{1,3})(?:\s*[-–]\s*\d{1,3})?\s*\]\s*$",
        r"\(\s*(?:disk|disc)\s*(\d+)\s*\)\s*(?:author interview)?\s*$",
        r"(?:part|pt)\.?\s*(\d+)\s*$",
        r"\(?\s*(\d+)\s+of\s+\d+\s*\)?\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return int(match.group(1))
    return None


def duration_compatible(left: float, right: float) -> bool:
    return bool(
        left and right and abs(left - right) <= max(5.0, max(left, right) * 0.005)
    )


def file_record(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or not resolved.is_relative_to(root):
        raise ValueError(f"missing or unsafe repair file: {path}")
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "relative_path": str(resolved.relative_to(root)),
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
    }


def companion_records(path: Path, root: Path) -> list[dict[str, Any]]:
    """Record non-media siblings that would otherwise become abandoned."""
    media = {".m4b", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma"}
    other_media = [
        item for item in path.parent.iterdir()
        if item.is_file() and item.resolve() != path.resolve() and item.suffix.casefold() in media
    ]
    if other_media:
        return []
    return [
        file_record(item, root)
        for item in sorted(path.parent.iterdir(), key=lambda value: value.name.casefold())
        if item.is_file() and item.resolve() != path.resolve()
    ]


def _load_database(
    database: Path, run_id: int
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        latest = connection.execute(
            "SELECT id,status,started_at,finished_at FROM process_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest is None or int(latest["id"]) != run_id or latest["status"] != "complete":
            raise ValueError("the audit is stale or the latest processing run is not complete")
        books: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT * FROM detected_books WHERE run_id=? AND state!='superseded'", (run_id,)
        ):
            item = dict(row)
            item["files"] = json.loads(item.pop("files_json"))
            item["metadata"] = json.loads(item["metadata_json"]) if item["metadata_json"] else None
            books[item["book_id"]] = item
        cached: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT path,probe_json,error FROM media_files"):
            cached[str(Path(row["path"]).resolve())] = {
                "probe": json.loads(row["probe_json"]) if row["probe_json"] else None,
                "error": row["error"],
            }
        return books, cached
    finally:
        connection.close()


def chapter_repairs(
    *,
    audit: dict[str, Any],
    books: dict[str, dict[str, Any]],
    cached: dict[str, dict[str, Any]],
    source_root: Path,
    output_root: Path,
    archive_root: Path,
) -> list[dict[str, Any]]:
    current = {item["book_id"]: item for item in audit["current_outputs"]}
    repairs: list[dict[str, Any]] = []
    for book_id, book in books.items():
        if book["state"] != "complete" or book_id not in current:
            continue
        source_files = book["files"]
        if len(source_files) != 1 or Path(source_files[0]).suffix.casefold() not in {".m4a", ".m4b"}:
            continue
        source = (source_root / source_files[0]).resolve()
        cached_item = cached.get(str(source)) or {}
        source_probe = cached_item.get("probe") or {}
        output_item = current[book_id]
        output_probe = output_item.get("probe") or {}
        source_chapters = len(source_probe.get("chapters") or [])
        output_chapters = int(output_probe.get("chapters") or 0)
        source_duration = float(source_probe.get("duration_seconds") or 0)
        output_duration = float(output_probe.get("duration_seconds") or 0)
        if not (
            not cached_item.get("error")
            and source_probe.get("codec_name") == "aac"
            and output_probe.get("codec") == "aac"
            and source_chapters > output_chapters
            and duration_compatible(source_duration, output_duration)
        ):
            continue
        output = Path(output_item["path"]).resolve()
        output_snapshot = file_record(output, output_root)
        repairs.append({
            "action": "lossless_remux_preserving_source_chapters",
            "book_id": book_id,
            "author": output_item.get("canonical_author"),
            "title": output_item.get("canonical_title"),
            "source": file_record(source, source_root),
            "current_output": output_snapshot,
            "archive_destination": str(
                (archive_root / "replaced-chapter-flattened" / output_snapshot["relative_path"]).resolve()
            ),
            "source_codec": "aac",
            "source_duration_seconds": source_duration,
            "current_duration_seconds": output_duration,
            "source_chapters": source_chapters,
            "current_chapters": output_chapters,
            "metadata": book.get("metadata") or {},
            "validation_required": [
                "source and current-output size/mtime still match this plan",
                "staged output contains AAC and exactly the source chapter count",
                "staged duration matches source within 0.5 percent",
                "staged compressed audio-stream MD5 equals source",
                "old generated output is archived before staged output is published",
            ],
        })
    return sorted(repairs, key=lambda item: str(item["current_output"]["relative_path"]).casefold())


def _identity_score(left: object, right: object) -> float:
    left_tokens = significant_tokens(left) - EDITION_NOISE
    right_tokens = significant_tokens(right) - EDITION_NOISE
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def _duration_buckets(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    buckets: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda value: float(value["probe"].get("duration_seconds") or 0)):
        duration = float(item["probe"].get("duration_seconds") or 0)
        title = item.get("observed_title") or Path(item["relative_path"]).parent.name
        number = part_number(title)
        for bucket in buckets:
            representative = float(bucket[0]["probe"].get("duration_seconds") or 0)
            representative_title = (
                bucket[0].get("observed_title")
                or Path(bucket[0]["relative_path"]).parent.name
            )
            representative_number = part_number(representative_title)
            if (
                number is not None
                and number == representative_number
                and abs(duration - representative) <= max(1.0, representative * 0.0001)
            ):
                bucket.append(item)
                break
        else:
            buckets.append([item])
    return buckets


def split_output_sets(
    *, audit: dict[str, Any], output_root: Path, archive_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    untracked = list(audit["untracked_outputs"])
    marked: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    unmarked: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in untracked:
        author_key = normalized(item.get("observed_author") or "")
        title = item.get("observed_title") or Path(item["relative_path"]).parent.name
        base = part_base(title)
        if base:
            marked[(author_key, base)].append(item)
        else:
            unmarked[(author_key, normalized(title))].append(item)

    safe: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for (author_key, base), initial in sorted(marked.items()):
        if len(initial) < 2:
            continue
        items = [*initial, *unmarked.get((author_key, base), [])]
        buckets = _duration_buckets(items)
        unique_duration = sum(
            float(bucket[0]["probe"].get("duration_seconds") or 0) for bucket in buckets
        )
        candidates: list[dict[str, Any]] = []
        for current in audit["current_outputs"]:
            if not person_matches(author_key, current.get("canonical_author") or ""):
                continue
            identity_score = _identity_score(base, current.get("canonical_title") or "")
            current_duration = float(current["probe"].get("duration_seconds") or 0)
            if identity_score < 0.75 or not duration_compatible(unique_duration, current_duration):
                continue
            if current.get("issues") or current["probe"].get("codec") != "aac":
                continue
            if int(current["probe"].get("chapters") or 0) < len(buckets):
                continue
            candidates.append({
                "book_id": current["book_id"],
                "path": current["path"],
                "relative_path": current["relative_path"],
                "title": current.get("canonical_title"),
                "duration_seconds": current_duration,
                "chapters": int(current["probe"].get("chapters") or 0),
                "identity_score": round(identity_score, 3),
                "duration_difference_seconds": round(abs(unique_duration - current_duration), 3),
                "snapshot": file_record(Path(current["path"]), output_root),
            })
        record = {
            "author": initial[0].get("observed_author"),
            "part_identity": base,
            "legacy_files": [
                {
                    **file_record(Path(item["path"]), output_root),
                    "duration_seconds": float(item["probe"].get("duration_seconds") or 0),
                    "chapters": int(item["probe"].get("chapters") or 0),
                    "companion_files": companion_records(Path(item["path"]), output_root),
                }
                for item in items
            ],
            "unique_part_count": len(buckets),
            "duplicate_representations": len(items) - len(buckets),
            "unique_part_duration_seconds": round(unique_duration, 3),
            "candidates": candidates,
        }
        if len(candidates) == 1:
            reference = candidates[0]
            record.update({
                "action": "archive_legacy_split_outputs",
                "reference_whole_output": reference,
                "archive_destinations": [
                    str((archive_root / "redundant-split-outputs" / item["relative_path"]).resolve())
                    for item in record["legacy_files"]
                ],
                "validation_required": [
                    "all legacy files and the whole-book reference still match this plan",
                    "each duration-duplicate bucket is retained for reversible recovery",
                    "unique part durations still add up to the whole-book duration within 0.5 percent",
                    "whole-book chapter count is at least the unique part count",
                ],
            })
            safe.append(record)
        else:
            record["reason"] = (
                "no unique clean whole-book output has matching author, title, aggregate duration, and chapters"
            )
            review.append(record)
    return safe, review


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    audit_path = args.audit.expanduser().resolve()
    database = args.database.expanduser().resolve()
    archive_root = args.archive_root.expanduser().resolve()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("mode") != "read-only":
        raise ValueError("repair planning requires a read-only alignment audit")
    source_root = Path(audit["roots"]["source"]).resolve()
    output_root = Path(audit["roots"]["output"]).resolve()
    if database != Path(audit["roots"]["database"]).resolve():
        raise ValueError("database does not match the selected audit")
    if archive_root.is_relative_to(source_root):
        raise ValueError("repair archive must not be placed inside the source library")
    run_id = int(audit["process_run"]["id"])
    books, cached = _load_database(database, run_id)
    chapters = chapter_repairs(
        audit=audit,
        books=books,
        cached=cached,
        source_root=source_root,
        output_root=output_root,
        archive_root=archive_root,
    )
    split_sets, split_review = split_output_sets(
        audit=audit, output_root=output_root, archive_root=archive_root
    )
    split_files = [item for group in split_sets for item in group["legacy_files"]]
    chapter_bytes = sum(int(item["current_output"]["size"]) for item in chapters)
    split_bytes = sum(int(item["size"]) for item in split_files)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "dry-run",
        "actions_applied": 0,
        "based_on_audit": str(audit_path),
        "process_run": audit["process_run"],
        "roots": {
            "database": str(database),
            "source": str(source_root),
            "output": str(output_root),
            "archive": str(archive_root),
        },
        "safety": {
            "source_library_writes": False,
            "output_library_writes_during_planning": False,
            "permanent_deletions": 0,
            "execution_requires_separate_explicit_approval": True,
            "all_replaced_or_redundant_generated_media_is_archived": True,
        },
        "summary": {
            "current_outputs_with_recoverable_source_chapters": len(chapters),
            "chapters_restored_if_approved": sum(
                int(item["source_chapters"]) - int(item["current_chapters"]) for item in chapters
            ),
            "chapter_flattened_outputs_to_archive": len(chapters),
            "chapter_flattened_bytes_to_archive": chapter_bytes,
            "chapter_flattened_gib_to_archive": round(chapter_bytes / 2**30, 2),
            "verified_split_sets": len(split_sets),
            "verified_split_files_to_archive": len(split_files),
            "verified_split_bytes_to_archive": split_bytes,
            "verified_split_gib_to_archive": round(split_bytes / 2**30, 2),
            "split_sets_preserved_for_review": len(split_review),
        },
        "chapter_repairs": chapters,
        "verified_split_sets": split_sets,
        "split_sets_preserved_for_review": split_review,
    }


def markdown(plan: dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = [
        "# Chapter and split-output repair — dry run",
        "",
        f"Generated: {plan['generated_at']}",
        "",
        "No audiobook was changed. The source library was only read.",
        "",
        "## Proposed repair",
        "",
        f"- Chapter-flattened current M4Bs to losslessly remux: **{summary['current_outputs_with_recoverable_source_chapters']}**",
        f"- Embedded chapters restored: **{summary['chapters_restored_if_approved']}**",
        f"- Old generated M4Bs archived: **{summary['chapter_flattened_gib_to_archive']} GiB**",
        f"- Verified legacy split sets already represented by a whole M4B: **{summary['verified_split_sets']}**",
        f"- Legacy split M4Bs archived: **{summary['verified_split_files_to_archive']}** ({summary['verified_split_gib_to_archive']} GiB)",
        f"- Incomplete or ambiguous split sets preserved: **{summary['split_sets_preserved_for_review']}**",
        "- Source writes: **0**",
        "- Permanent deletions: **0**",
        "",
        "The proposed archive is outside the source library so the original audiobooks remain untouched:",
        "",
        f"`{plan['roots']['archive']}`",
        "",
        "## Verified split sets",
        "",
        "| Author | Split identity | Legacy files | Unique parts | Whole-book chapters | Difference | Whole output |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for group in plan["verified_split_sets"]:
        reference = group["reference_whole_output"]
        lines.append(
            "| " + " | ".join([
                str(group.get("author") or "").replace("|", "\\|"),
                str(group["part_identity"]).replace("|", "\\|"),
                str(len(group["legacy_files"])),
                str(group["unique_part_count"]),
                str(reference["chapters"]),
                f"{reference['duration_difference_seconds']:.3f}s",
                f"`{reference['relative_path']}`",
            ]) + " |"
        )
    if not plan["verified_split_sets"]:
        lines.append("| None | | | | | | |")
    lines.extend([
        "",
        "## Chapter repairs by author",
        "",
        "| Author | Books | Old size (GiB) |",
        "|---|---:|---:|",
    ])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in plan["chapter_repairs"]:
        grouped[str(item.get("author") or "Unknown")].append(item)
    for author, items in sorted(grouped.items(), key=lambda value: (-len(value[1]), value[0].casefold())):
        size = sum(int(item["current_output"]["size"]) for item in items) / 2**30
        lines.append(f"| {author.replace('|', '\\|')} | {len(items)} | {size:.2f} |")
    lines.extend([
        "",
        "## Important execution constraint",
        "",
        "The output disk does not have enough comfortable headroom to retain all replaced files there. The executor should stage one book at a time and move the old generated M4B to the proposed external archive before publishing its validated replacement.",
        "",
        "Every file, duration, chapter count, and size/mtime precondition is recorded in the JSON companion.",
        "",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = build_plan(args)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    report_dir = args.report_dir.expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"chapter-split-repair-dry-run-{stamp}.json"
    markdown_path = report_dir / f"chapter-split-repair-dry-run-{stamp}.md"
    json_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown(plan), encoding="utf-8")
    print(json.dumps({"summary": plan["summary"], "json": str(json_path), "markdown": str(markdown_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
