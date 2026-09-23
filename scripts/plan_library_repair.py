#!/usr/bin/env python3
"""Create a no-change repair plan from a library alignment audit.

The generated plan never changes either audiobook tree. Even high-confidence
duplicates are only proposed for recoverable quarantine after a clean rebuild
and a second compatibility check.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from audit_library_alignment import person_matches, text_similarity, title_matches


def _duration_match(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, float, float]:
    left_duration = float(left.get("duration_seconds") or 0)
    right_duration = float(right.get("duration_seconds") or 0)
    difference = abs(left_duration - right_duration)
    ratio = difference / max(left_duration, right_duration, 1.0)
    return bool(
        left_duration
        and right_duration
        and difference <= max(5.0, max(left_duration, right_duration) * 0.005)
    ), difference, ratio


def _usable_output(item: dict[str, Any]) -> bool:
    probe = item.get("probe") or {}
    return bool(
        not item.get("issues")
        and not probe.get("error")
        and probe.get("codec") == "aac"
        and probe.get("duration_seconds")
    )


def _reference_supports_observed_identity(
    untracked: dict[str, Any], current: dict[str, Any]
) -> bool:
    """Use the same path/tag evidence the cleanup executor will revalidate."""
    author = str(untracked.get("observed_author") or "").strip()
    title = str(untracked.get("observed_title") or "").strip()
    if not author or not title:
        return False
    relative = Path(str(current.get("relative_path") or ""))
    path_author = relative.parts[0] if relative.parts else ""
    path_title = relative.parent.name
    probe = current.get("probe") or {}
    author_tags = probe.get("author_tags") or []
    title_tags = probe.get("title_tags") or []
    author_ok = person_matches(author, path_author) or any(
        person_matches(author, str(value)) for value in author_tags
    )
    title_ok = title_matches(title, path_title, threshold=0.82) or any(
        title_matches(title, str(value), threshold=0.82) for value in title_tags
    )
    return author_ok and title_ok


def _candidate_record(
    untracked: dict[str, Any], current: dict[str, Any], title_score: float
) -> dict[str, Any]:
    duration_ok, difference, ratio = _duration_match(
        untracked.get("probe") or {}, current.get("probe") or {}
    )
    return {
        "path": current["path"],
        "relative_path": current["relative_path"],
        "book_id": current["book_id"],
        "author": current.get("canonical_author"),
        "title": current.get("canonical_title"),
        "title_similarity": round(title_score, 3),
        "duration_compatible": duration_ok,
        "duration_difference_seconds": round(difference, 3),
        "duration_difference_ratio": round(ratio, 5),
        "current_output_is_clean": _usable_output(current),
    }


def classify_untracked(
    audit: dict[str, Any], quarantine_root: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current = list(audit["current_outputs"])
    by_path = {str(Path(item["path"]).resolve()): item for item in current}
    operations: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for item in audit["untracked_outputs"]:
        exact_paths = {
            str(Path(path).resolve()) for path in item.get("possible_current_outputs") or []
        }
        candidates: list[dict[str, Any]] = []
        for possible in current:
            same_author = person_matches(
                item.get("observed_author") or "", possible.get("canonical_author") or ""
            )
            if not same_author:
                continue
            observed_title = item.get("observed_title") or ""
            aliases = [
                possible.get("canonical_title") or "",
                possible.get("book_id") or "",
            ]
            score = max(text_similarity(observed_title, alias) for alias in aliases)
            exact_identity = str(Path(possible["path"]).resolve()) in exact_paths
            if score < 0.82 and not exact_identity:
                continue
            if not _reference_supports_observed_identity(item, possible):
                continue
            candidates.append(_candidate_record(item, possible, score))

        candidates.sort(
            key=lambda candidate: (
                not candidate["duration_compatible"],
                not candidate["current_output_is_clean"],
                -candidate["title_similarity"],
                candidate["duration_difference_ratio"],
            )
        )
        compatible = [
            candidate
            for candidate in candidates
            if candidate["duration_compatible"] and candidate["current_output_is_clean"]
        ]
        base = {
            "source": item["path"],
            "relative_path": item["relative_path"],
            "observed_author": item.get("observed_author"),
            "observed_title": item.get("observed_title"),
        }
        if len(compatible) == 1:
            reference = compatible[0]
            operations.append(
                {
                    **base,
                    "phase": "after_clean_rebuild",
                    "action": "quarantine_redundant_output",
                    "destination": str(quarantine_root / item["relative_path"]),
                    "reference_output": reference["path"],
                    "confidence": round(min(0.99, 0.75 + reference["title_similarity"] * 0.24), 3),
                    "evidence": {
                        "same_normalized_author": True,
                        "title_similarity": reference["title_similarity"],
                        "duration_difference_seconds": reference["duration_difference_seconds"],
                        "reference_is_readable_aac_without_audit_issues": True,
                    },
                    "preconditions": [
                        "fresh repaired scan and build has completed",
                        "reference output is still current and readable",
                        "duration and embedded identity are revalidated immediately before quarantine",
                    ],
                }
            )
        elif len(compatible) > 1:
            review.append(
                {
                    **base,
                    "category": "ambiguous_duplicate_candidates",
                    "reason": "more than one clean current output has matching identity and duration",
                    "candidates": compatible,
                    "recommended_action": "preserve until one source identity is confirmed",
                }
            )
        elif candidates:
            review.append(
                {
                    **base,
                    "category": "possible_partial_or_alternate_edition",
                    "reason": "author/title evidence overlaps but duration or current-output safety does not",
                    "candidates": candidates[:5],
                    "recommended_action": "preserve and compare after the clean rebuild",
                }
            )
        else:
            review.append(
                {
                    **base,
                    "category": "untracked_unique_output",
                    "reason": "no clean current output has strong author, title, and duration agreement",
                    "candidates": [],
                    "recommended_action": "preserve and reconcile to the repaired source scan",
                }
            )
    return operations, review


def metadata_rechecks(audit: dict[str, Any]) -> list[dict[str, Any]]:
    rechecks: list[dict[str, Any]] = []
    risky_titles = {
        "end credits", "opening credits", "a litrpg adventure", "audiobook full", "001", "20"
    }
    for item in audit["source_books"]:
        conflicts = [issue for issue in item["issues"] if "conflict" in issue]
        if item["state"] != "complete" or not conflicts:
            continue
        title = str(item.get("canonical_title") or "").casefold()
        severity = "high" if len(conflicts) > 1 or title in risky_titles else "review"
        rechecks.append(
            {
                "book_id": item["book_id"],
                "severity": severity,
                "current_author": item.get("canonical_author"),
                "current_title": item.get("canonical_title"),
                "metadata_source": item.get("metadata_source"),
                "conflicts": conflicts,
                "source_files": item.get("source_files") or [],
                "source_author_tags": item.get("source_author_tags") or [],
                "source_album_tags": item.get("source_album_tags") or [],
                "action": "discard_stale_match_and_reidentify_on_next_scan",
            }
        )
    return rechecks


def author_consolidations(audit: dict[str, Any]) -> list[dict[str, Any]]:
    output_root = Path(audit["roots"]["output"]).resolve()
    current_counts: Counter[str] = Counter()
    untracked_counts: Counter[str] = Counter()
    for item in audit["current_outputs"]:
        relative = Path(item["path"]).resolve().relative_to(output_root)
        if relative.parts:
            current_counts[relative.parts[0]] += 1
    for item in audit["untracked_outputs"]:
        relative = Path(item["path"]).resolve().relative_to(output_root)
        if relative.parts:
            untracked_counts[relative.parts[0]] += 1
    results: list[dict[str, Any]] = []
    for group in audit["author_folder_variants"]:
        folders = list(group["folders"])
        canonical = sorted(
            folders, key=lambda name: (-current_counts[name], -untracked_counts[name], name.casefold())
        )[0]
        results.append(
            {
                "identity": group["identity"],
                "canonical_folder": canonical,
                "variants": [
                    {
                        "folder": name,
                        "current_outputs": current_counts[name],
                        "untracked_outputs": untracked_counts[name],
                    }
                    for name in folders
                ],
                "action": "use canonical spelling for new outputs; quarantine redundant files first; move only verified unique books; prune only an empty variant folder",
            }
        )
    return results


def build_plan(audit: dict[str, Any], stamp: str) -> dict[str, Any]:
    output_root = Path(audit["roots"]["output"]).resolve()
    quarantine_root = output_root / "_quarantine" / f"library-repair-{stamp}"
    operations, review = classify_untracked(audit, quarantine_root)
    metadata = metadata_rechecks(audit)
    authors = author_consolidations(audit)
    review_counts = Counter(item["category"] for item in review)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "dry-run",
        "actions_applied": 0,
        "based_on_audit": audit.get("generated_at"),
        "process_run": audit.get("process_run"),
        "roots": audit["roots"],
        "safety": {
            "source_library_writes": False,
            "output_library_writes": False,
            "permanent_deletions_planned": 0,
            "quarantine_root_if_approved": str(quarantine_root),
        },
        "summary": {
            "source_audio_files_accounted": (
                audit["summary"]["source_unaccounted_files"] == 0
            ),
            "repaired_scanner_book_groups": audit["summary"].get("source_detected_books"),
            "repaired_scanner_states": audit["summary"].get("source_reconciliation_states", {}),
            "untracked_outputs_examined": len(audit["untracked_outputs"]),
            "post_rebuild_quarantine_candidates": len(operations),
            "outputs_preserved_for_review": len(review),
            "review_categories": dict(review_counts),
            "stale_completed_metadata_rechecks": len(metadata),
            "high_priority_metadata_rechecks": sum(item["severity"] == "high" for item in metadata),
            "old_output_collision_groups": len(audit["output_path_collisions"]),
            "author_folder_variant_groups": len(authors),
        },
        "execution_order": [
            "restart the application so the repaired detector and output-claim safeguards are loaded",
            "run a fresh discovery and metadata pass; do not clean the old output tree yet",
            "build or safely adopt outputs, quarantining any identity that meets an incompatible existing file",
            "generate a new alignment audit and revalidate every proposed duplicate against the new current run",
            "only after explicit approval, move revalidated redundant outputs to the quarantine root and consolidate empty author-folder variants",
        ],
        "post_rebuild_quarantine_operations": operations,
        "preserved_output_review": review,
        "metadata_reidentification": metadata,
        "old_output_claim_collisions": audit["output_path_collisions"],
        "author_folder_consolidation": authors,
    }


def _table(rows: list[list[object]], headers: list[str]) -> list[str]:
    if not rows:
        return ["None."]
    clean = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
    result = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    result.extend("| " + " | ".join(clean(value) for value in row) + " |" for row in rows)
    return result


def write_plan(report_dir: Path, plan: dict[str, Any], stamp: str) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"library-repair-dry-run-{stamp}.json"
    markdown_path = report_dir / f"library-repair-dry-run-{stamp}.md"
    json_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = plan["summary"]
    high_metadata = [
        item for item in plan["metadata_reidentification"] if item["severity"] == "high"
    ]
    lines = [
        "# Audiobook library repair dry run",
        "",
        f"Generated: {plan['generated_at']}",
        "",
        "**No audiobook was moved, renamed, overwritten, or deleted.** This plan requires a fresh repaired run before any cleanup can be approved.",
        "",
        "## Outcome",
        "",
        f"- The repaired scanner accounts for every source audio file: **{summary['source_audio_files_accounted']}**.",
        f"- Repaired scanner book groups: **{summary['repaired_scanner_book_groups']}**.",
        f"- Existing untracked outputs examined: **{summary['untracked_outputs_examined']}**.",
        f"- Potential redundant outputs after rebuild and revalidation: **{summary['post_rebuild_quarantine_candidates']}**.",
        f"- Outputs deliberately preserved for review: **{summary['outputs_preserved_for_review']}**.",
        f"- Completed metadata records queued for fresh identification: **{summary['stale_completed_metadata_rechecks']}** ({summary['high_priority_metadata_rechecks']} high priority).",
        f"- Old output-path collision groups the new claim guard prevents: **{summary['old_output_collision_groups']}**.",
        f"- Author-folder spelling groups to consolidate safely: **{summary['author_folder_variant_groups']}**.",
        "",
        "## Required order",
        "",
        *[f"{number}. {step.capitalize()}." for number, step in enumerate(plan["execution_order"], 1)],
        "",
        "## Preserved-output review categories",
        "",
        *_table(
            [[category, count] for category, count in sorted(summary["review_categories"].items())],
            ["Category", "Outputs"],
        ),
        "",
        "## High-priority metadata re-identification",
        "",
        *_table(
            [
                [item["current_author"], item["current_title"], ", ".join(item["conflicts"]), item["source_files"][0]]
                for item in high_metadata
            ],
            ["Current author", "Current title", "Conflict", "First source file"],
        ),
        "",
        "## Author folder consolidation",
        "",
        *_table(
            [
                [
                    item["identity"],
                    item["canonical_folder"],
                    "; ".join(
                        f"{variant['folder']} ({variant['current_outputs']} current, {variant['untracked_outputs']} untracked)"
                        for variant in item["variants"]
                    ),
                ]
                for item in plan["author_folder_consolidation"]
            ],
            ["Author identity", "Canonical folder", "Existing folders"],
        ),
        "",
        "## Full exact plan",
        "",
        f"The companion `{json_path.name}` contains every proposed quarantine source/destination, comparison target, confidence, precondition, collision claim, and preserved review item. Nothing in it has been applied.",
        "",
    ]
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-json", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    audit_path = args.audit_json.expanduser().resolve()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("mode") != "read-only":
        raise ValueError("repair planning requires a read-only alignment audit")
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    plan = build_plan(audit, stamp)
    json_path, markdown_path = write_plan(args.report_dir.expanduser().resolve(), plan, stamp)
    print(json.dumps({"summary": plan["summary"], "json": str(json_path), "markdown": str(markdown_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
