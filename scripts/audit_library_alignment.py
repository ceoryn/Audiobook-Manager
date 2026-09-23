#!/usr/bin/env python3
"""Read-only audit of source identities and the generated audiobook library.

The audit deliberately performs no writes beneath either library root. It reads
the current process database, checks that every detected source file still
exists, probes generated M4Bs, and emits JSON plus a human-readable Markdown
report beneath a caller-selected report directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from audiobook_manager.detection import detect_books
from audiobook_manager.display import person_key
from audiobook_manager.models import ProbeResult, ScannedFile
from audiobook_manager.probe import ProbeError, probe_media
from audiobook_manager.reconcile import reconcile_group


AUDIO_SUFFIXES = {".mp3", ".m4a", ".m4b"}
AUTHOR_TAGS = ("album_artist", "albumartist", "artist", "author")
TITLE_TAGS = ("album", "title")
NOISE_WORDS = {
    "a",
    "an",
    "and",
    "audio",
    "audiobook",
    "book",
    "by",
    "complete",
    "full",
    "the",
    "unabridged",
}


@dataclass(frozen=True)
class ProbeSummary:
    path: str
    error: str | None
    duration_seconds: float | None
    codec: str | None
    author_tags: tuple[str, ...]
    title_tags: tuple[str, ...]
    series_tag: str | None
    chapters: int


def normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def significant_tokens(value: object) -> set[str]:
    return {
        token
        for token in normalized_text(value).split()
        if token not in NOISE_WORDS and (len(token) > 1 or token.isdigit())
    }


def semantic_title_tokens(value: object) -> set[str]:
    return {token for token in significant_tokens(value) if not token.isdigit()}


def text_similarity(left: object, right: object) -> float:
    a, b = normalized_text(left), normalized_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = SequenceMatcher(None, a, b).ratio()
    a_tokens, b_tokens = significant_tokens(a), significant_tokens(b)
    if not a_tokens or not b_tokens:
        return sequence
    containment = len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens))
    jaccard = len(a_tokens & b_tokens) / len(a_tokens | b_tokens)
    return max(sequence, containment * 0.94, jaccard)


def title_matches(expected: object, observed: object, *, threshold: float = 0.68) -> bool:
    if text_similarity(expected, observed) >= threshold:
        return True
    expected_tokens = semantic_title_tokens(expected)
    observed_tokens = semantic_title_tokens(observed)
    if min(len(expected_tokens), len(observed_tokens)) < 2:
        return False
    return expected_tokens <= observed_tokens or observed_tokens <= expected_tokens


def person_matches(expected: object, observed: object) -> bool:
    wanted = person_key(expected)
    present = person_key(observed)
    if not wanted or not present:
        return False
    if wanted == present or f" {wanted} " in f" {present} ":
        return True
    components = re.split(r"\s*(?:,|;|&|/|\band\b)\s*", str(observed), flags=re.IGNORECASE)
    return any(text_similarity(expected, component) >= 0.82 for component in components if component)


def placeholder_author(value: object) -> bool:
    return normalized_text(value) in {"", "artist", "no artist", "unknown", "unknown author"}


def placeholder_title(value: object) -> bool:
    return normalized_text(value) in {"", "album", "no title", "unknown", "unknown title", "untitled"}


def compact_strings(values: Iterable[object]) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for value in values:
        text = str(value).strip()
        key = normalized_text(text)
        if text and key and key not in seen:
            seen[key] = text
    return tuple(seen.values())


def metadata_author(metadata: dict[str, Any] | None) -> str:
    if not metadata:
        return ""
    authors = metadata.get("authors") or []
    if authors:
        return str(authors[0]).strip()
    return str(metadata.get("author") or "").strip()


def metadata_series(metadata: dict[str, Any] | None) -> str:
    if not metadata:
        return ""
    series = metadata.get("series")
    if isinstance(series, dict):
        return str(series.get("name") or "").strip()
    return str(series or "").strip()


def metadata_title(metadata: dict[str, Any] | None) -> str:
    return str((metadata or {}).get("title") or "").strip()


def metadata_provenance(metadata: dict[str, Any] | None) -> str:
    if not metadata:
        return "none"
    return str(
        metadata.get("_metadata_source")
        or metadata.get("provider")
        or metadata.get("source")
        or "present"
    )


def probe_summary(path: Path) -> ProbeSummary:
    try:
        result = probe_media(path, timeout_seconds=90)
    except (ProbeError, OSError) as error:
        return ProbeSummary(str(path), str(error), None, None, (), (), None, 0)
    tags = result.tags
    authors = compact_strings(tags[key] for key in AUTHOR_TAGS if tags.get(key))
    titles = compact_strings(tags[key] for key in TITLE_TAGS if tags.get(key))
    return ProbeSummary(
        path=str(path),
        error=None,
        duration_seconds=result.duration_seconds,
        codec=result.codec_name,
        author_tags=authors,
        title_tags=titles,
        series_tag=(tags.get("grouping") or tags.get("series") or "").strip() or None,
        chapters=len(result.chapters),
    )


def load_database(database: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        run_row = connection.execute(
            "SELECT * FROM process_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if run_row is None:
            raise RuntimeError("the process database contains no runs")
        run = dict(run_row)
        rows = connection.execute(
            """SELECT book_id, state, classification, files_json, metadata_json,
                      confidence, output_path, failure, quarantine_path
                 FROM detected_books
                WHERE run_id=? AND state!='superseded'
                ORDER BY book_id""",
            (run["id"],),
        ).fetchall()
        books: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["files"] = json.loads(item.pop("files_json"))
            raw_metadata = item.pop("metadata_json")
            item["metadata"] = json.loads(raw_metadata) if raw_metadata else None
            books.append(item)
        probes: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT path, size, modified_ns, probe_json, error FROM media_files"
        ):
            probes[str(row["path"])] = {
                "size": row["size"],
                "modified_ns": row["modified_ns"],
                "probe": json.loads(row["probe_json"]) if row["probe_json"] else None,
                "error": row["error"],
            }
        return run, books, probes
    finally:
        connection.close()


def output_media_files(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() == ".m4b"
            and not any(part.startswith("_") for part in path.relative_to(root).parts)
        ),
        key=lambda path: str(path).casefold(),
    )


def source_checks(
    *, source_root: Path, books: list[dict[str, Any]], cached: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    missing_files: list[dict[str, Any]] = []
    for book in books:
        metadata = book["metadata"]
        author, title = metadata_author(metadata), metadata_title(metadata)
        author_values: list[str] = []
        album_values: list[str] = []
        title_values: list[str] = []
        cache_errors: list[dict[str, str]] = []
        absent: list[str] = []
        paths = [str(value) for value in book["files"]]
        for relative in paths:
            absolute = source_root / relative
            if not absolute.is_file():
                absent.append(relative)
                missing_files.append({"book_id": book["book_id"], "path": relative})
            cache_item = cached.get(str(absolute.resolve()))
            if not cache_item:
                cache_errors.append({"path": relative, "error": "not present in scan cache"})
                continue
            if cache_item["error"]:
                cache_errors.append({"path": relative, "error": str(cache_item["error"])})
                continue
            probe = cache_item["probe"] or {}
            tags = probe.get("tags") or {}
            author_values.extend(tags[key] for key in AUTHOR_TAGS if tags.get(key))
            if tags.get("album"):
                album_values.append(tags["album"])
            if tags.get("title"):
                title_values.append(tags["title"])

        author_tags = compact_strings(author_values)
        albums = compact_strings(album_values)
        source_titles = compact_strings(title_values)
        searchable_path = " ".join(paths)

        author_status = "not_applicable"
        author_best = 0.0
        if author and not placeholder_author(author):
            author_status = "unverified"
            if any(person_matches(author, value) for value in author_tags):
                author_status, author_best = "aligned", 1.0
            elif person_matches(author, searchable_path):
                author_status, author_best = "aligned", 1.0
            else:
                author_best = max(
                    [text_similarity(author, value) for value in author_tags] +
                    [text_similarity(author, searchable_path)],
                    default=0.0,
                )
                if author_best >= 0.78:
                    author_status = "aligned"
                elif author_tags:
                    author_status = "conflict"

        title_status = "not_applicable"
        title_best = 0.0
        if title and not placeholder_title(title):
            title_status = "unverified"
            candidates = [*albums, searchable_path]
            title_best = max((text_similarity(title, value) for value in candidates), default=0.0)
            if any(title_matches(title, value) for value in candidates):
                title_status = "aligned"
            elif albums:
                title_status = "conflict"

        issues: list[str] = []
        if not metadata:
            issues.append("missing_canonical_metadata")
        else:
            if placeholder_author(author):
                issues.append("missing_canonical_author")
            if placeholder_title(title):
                issues.append("missing_canonical_title")
        if absent:
            issues.append("missing_source_file")
        if cache_errors:
            issues.append("source_probe_problem")
        if author_status == "conflict":
            issues.append("source_author_conflict")
        elif author_status == "unverified":
            issues.append("source_author_unverified")
        if title_status == "conflict":
            issues.append("source_title_conflict")
        elif title_status == "unverified":
            issues.append("source_title_unverified")

        results.append(
            {
                "book_id": book["book_id"],
                "state": book["state"],
                "classification": book["classification"],
                "canonical_author": author or None,
                "canonical_title": title or None,
                "metadata_source": metadata_provenance(metadata),
                "needs_metadata_review": bool((metadata or {}).get("_needs_metadata_review")),
                "source_files": paths,
                "source_author_tags": list(author_tags),
                "source_album_tags": list(albums),
                "source_title_samples": list(source_titles[:10]),
                "author_alignment": author_status,
                "author_similarity": round(author_best, 3),
                "title_alignment": title_status,
                "title_similarity": round(title_best, 3),
                "cache_errors": cache_errors,
                "issues": issues,
            }
        )
    return results, missing_files


def source_detector_coverage(
    *, source_root: Path, books: list[dict[str, Any]], cached: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    physical = sorted(
        (
            path
            for path in source_root.rglob("*")
            if path.is_file() and path.suffix.casefold() in AUDIO_SUFFIXES
        ),
        key=lambda path: str(path).casefold(),
    )
    scanned: list[ScannedFile] = []
    uncached: list[str] = []
    for path in physical:
        absolute = str(path.resolve())
        item = cached.get(absolute)
        if item is None:
            uncached.append(str(path.relative_to(source_root)))
            continue
        probe = ProbeResult.from_dict(item["probe"]) if item["probe"] else None
        scanned.append(
            ScannedFile(
                path=path,
                relative_path=path.relative_to(source_root),
                size=int(item["size"]),
                modified_ns=int(item["modified_ns"]),
                probe=probe,
                error=item["error"],
                cached=True,
            )
        )

    selected: set[str] = set()
    alternate_records: list[dict[str, Any]] = []
    problem_records: list[dict[str, Any]] = []
    unaccounted: list[dict[str, Any]] = []
    reconciliation_states: Counter[str] = Counter()
    detected_groups = 0
    for group in detect_books(scanned):
        detected_groups += 1
        result = reconcile_group(group)
        reconciliation_states[result.state] += 1
        selected_for_group = set(result.chosen_files)
        alternates_for_group = set(result.alternate_files) - selected_for_group
        problems_for_group = set(result.problem_files)
        selected.update(selected_for_group)
        for relative in sorted(alternates_for_group, key=str.casefold):
            alternate_records.append(
                {
                    "path": relative,
                    "group_key": group.key,
                    "selected_files": sorted(selected_for_group, key=str.casefold),
                    "reason": "; ".join(result.evidence),
                }
            )
        for relative in sorted(problems_for_group, key=str.casefold):
            media = next(
                item for item in group.files if str(item.relative_path) == relative
            )
            problem_records.append(
                {
                    "path": relative,
                    "group_key": group.key,
                    "probe_error": media.error,
                    "reason": "unreadable or invalid source representation",
                }
            )
        accounted_for_group = selected_for_group | alternates_for_group | problems_for_group
        for media in group.files:
            relative = str(media.relative_path)
            if relative in accounted_for_group:
                continue
            unaccounted.append(
                {
                    "path": relative,
                    "group_key": group.key,
                    "reconciliation_state": result.state,
                    "readable": bool(media.probe and media.probe.duration_seconds),
                    "duration_seconds": media.probe.duration_seconds if media.probe else None,
                    "probe_error": media.error,
                    "selected_files": sorted(selected_for_group, key=str.casefold),
                    "recorded_alternates": sorted(alternates_for_group, key=str.casefold),
                    "reason": (
                        "readable representation omitted by reconciliation"
                        if media.probe and media.probe.duration_seconds
                        else "unreadable representation omitted by reconciliation"
                    ),
                }
            )

    active_references = {
        str(relative)
        for book in books
        for relative in book["files"]
    }
    physical_relative = {str(path.relative_to(source_root)) for path in physical}
    cached_beneath_root = {
        str(Path(path).relative_to(source_root))
        for path in cached
        if Path(path).is_relative_to(source_root)
    }
    return {
        "physical_media_files": len(physical),
        "detected_book_groups": detected_groups,
        "reconciliation_states": dict(reconciliation_states),
        "active_file_references": len(active_references),
        "detector_selected_files": len(selected),
        "recorded_alternate_representations": len(alternate_records),
        "recorded_problem_files": len(problem_records),
        "unaccounted_files": len(unaccounted),
        "unaccounted_readable_files": sum(item["readable"] for item in unaccounted),
        "uncached_physical_files": uncached,
        "cached_files_missing_on_disk": sorted(cached_beneath_root - physical_relative, key=str.casefold),
        "detector_selected_not_active": sorted(selected - active_references, key=str.casefold),
        "active_not_detector_selected": sorted(active_references - selected, key=str.casefold),
        "alternate_representations": alternate_records,
        "problem_files": problem_records,
        "unaccounted": unaccounted,
    }


def clean_output_checks(
    *, output_root: Path, books: list[dict[str, Any]], workers: int
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    output_files = output_media_files(output_root)
    summaries: dict[str, ProbeSummary] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="output-ffprobe") as executor:
        futures = {executor.submit(probe_summary, path): path for path in output_files}
        for future in as_completed(futures):
            result = future.result()
            summaries[result.path] = result

    complete = [book for book in books if book["state"] == "complete" and book["output_path"]]
    claims_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for book in complete:
        claims_by_path[str(Path(book["output_path"]).resolve())].append(book)
    tracked_by_path = {path: claims[0] for path, claims in claims_by_path.items()}
    path_collisions = [
        {
            "path": path,
            "claims": [
                {
                    "book_id": book["book_id"],
                    "canonical_author": metadata_author(book["metadata"]),
                    "canonical_title": metadata_title(book["metadata"]),
                    "source_files": book["files"],
                }
                for book in claims
            ],
        }
        for path, claims in claims_by_path.items()
        if len(claims) > 1
    ]
    path_collisions.sort(key=lambda item: item["path"].casefold())
    clean_paths = {str(path.resolve()) for path in output_files}
    missing_outputs = [
        {
            "book_id": book["book_id"],
            "canonical_author": metadata_author(book["metadata"]),
            "canonical_title": metadata_title(book["metadata"]),
            "expected_output": book["output_path"],
        }
        for book in complete
        if str(Path(book["output_path"]).resolve()) not in clean_paths
    ]

    current_checks: list[dict[str, Any]] = []
    current_identity_paths: dict[tuple[str, str], list[str]] = defaultdict(list)
    for path_string, book in tracked_by_path.items():
        if path_string not in clean_paths:
            continue
        path = Path(path_string)
        relative = path.relative_to(output_root)
        summary = summaries[path_string]
        metadata = book["metadata"]
        author, title = metadata_author(metadata), metadata_title(metadata)
        series = metadata_series(metadata)
        author_folder = relative.parts[0] if relative.parts else ""
        book_folder = relative.parent.name
        path_author_ok = bool(author and person_key(author_folder) == person_key(author))
        path_title_score = text_similarity(title, book_folder)
        path_title_ok = bool(title and title_matches(title, book_folder))
        tag_author_ok = any(person_matches(author, value) for value in summary.author_tags)
        tag_title_score = max(
            (text_similarity(title, value) for value in summary.title_tags), default=0.0
        )
        tag_title_ok = any(title_matches(title, value, threshold=0.82) for value in summary.title_tags)
        series_ok: bool | None = None
        if series:
            series_ok = any(normalized_text(part) == normalized_text(series) for part in relative.parts[:-2])
            if summary.series_tag:
                series_ok = series_ok and text_similarity(series, summary.series_tag) >= 0.82
        issues: list[str] = []
        if summary.error:
            issues.append("unreadable_output")
        if summary.codec != "aac":
            issues.append("unexpected_output_codec")
        if len(claims_by_path[path_string]) > 1:
            issues.append("multiple_current_book_claims")
        if not path_author_ok:
            issues.append("output_author_folder_mismatch")
        if not path_title_ok:
            issues.append("output_title_folder_mismatch")
        if not summary.author_tags:
            issues.append("missing_output_author_tag")
        elif not tag_author_ok:
            issues.append("output_author_tag_mismatch")
        if not summary.title_tags:
            issues.append("missing_output_title_tag")
        elif not tag_title_ok:
            issues.append("output_title_tag_mismatch")
        if series_ok is False:
            issues.append("output_series_mismatch")
        current_identity_paths[(person_key(author), normalized_text(title))].append(path_string)
        current_checks.append(
            {
                "book_id": book["book_id"],
                "claiming_book_ids": [claim["book_id"] for claim in claims_by_path[path_string]],
                "canonical_author": author,
                "canonical_title": title,
                "canonical_series": series or None,
                "path": path_string,
                "relative_path": str(relative),
                "probe": asdict(summary),
                "path_author_aligned": path_author_ok,
                "path_title_aligned": path_title_ok,
                "path_title_similarity": round(path_title_score, 3),
                "tag_author_aligned": tag_author_ok,
                "tag_title_aligned": tag_title_ok,
                "tag_title_similarity": round(tag_title_score, 3),
                "series_aligned": series_ok,
                "issues": issues,
            }
        )

    untracked: list[dict[str, Any]] = []
    all_identity_paths: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for path in output_files:
        path_string = str(path.resolve())
        relative = path.relative_to(output_root)
        summary = summaries[path_string]
        fallback_author = relative.parts[0] if relative.parts else ""
        fallback_title = relative.parent.name
        observed_author = fallback_author
        observed_title = summary.title_tags[0] if summary.title_tags else fallback_title
        identity = (person_key(observed_author), normalized_text(observed_title))
        ownership = "current" if path_string in tracked_by_path else "untracked"
        all_identity_paths[identity].append({"path": path_string, "ownership": ownership})
        if ownership == "current":
            continue
        possible = current_identity_paths.get(identity, [])
        untracked.append(
            {
                "path": path_string,
                "relative_path": str(relative),
                "observed_author": observed_author or None,
                "observed_title": observed_title or None,
                "probe": asdict(summary),
                "classification": "duplicate_of_current" if possible else "untracked_unknown",
                "possible_current_outputs": possible,
            }
        )

    duplicate_groups = [
        {
            "identity": {"author_key": identity[0], "title_key": identity[1]},
            "files": items,
        }
        for identity, items in all_identity_paths.items()
        if identity[0] and identity[1] and len(items) > 1
    ]
    duplicate_groups.sort(
        key=lambda group: (
            -len(group["files"]),
            group["identity"]["author_key"],
            group["identity"]["title_key"],
        )
    )
    return current_checks, untracked, missing_outputs, duplicate_groups, path_collisions


def author_folder_variants(output_root: Path) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for path in output_root.iterdir():
        if path.is_dir() and not path.name.startswith("_"):
            grouped[person_key(path.name)].append(path.name)
    return [
        {"identity": key, "folders": sorted(names, key=str.casefold)}
        for key, names in grouped.items()
        if key and len(names) > 1
    ]


def markdown_table(rows: list[list[object]], headers: list[str]) -> list[str]:
    if not rows:
        return ["None."]
    clean = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(clean(value) for value in row) + " |" for row in rows)
    return lines


def write_reports(
    *, report_dir: Path, payload: dict[str, Any], stamp: str
) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"library-alignment-audit-{stamp}.json"
    markdown_path = report_dir / f"library-alignment-audit-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = payload["summary"]
    source_issues = [item for item in payload["source_books"] if item["issues"]]
    current_issues = [item for item in payload["current_outputs"] if item["issues"]]
    lines = [
        "# Audiobook library alignment audit",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "This was a read-only audit. No source or output audiobook was changed.",
        "",
        "## Summary",
        "",
        f"- Active detected books: **{summary['active_books']}**",
        f"- Source media records: **{summary['source_media_records']}**",
        f"- Books detected by the repaired scanner: **{summary['source_detected_books']}**",
        f"- Source files assigned to active books: **{summary['source_files_assigned']}**",
        f"- Source alternate representations recorded by the detector: **{summary['source_alternate_representations']}**",
        f"- Source problem files recorded by the detector: **{summary['source_problem_files']}**",
        f"- Source files unaccounted for by reconciliation: **{summary['source_unaccounted_files']}** ({summary['source_unaccounted_readable_files']} readable)",
        f"- Source probe failures: **{summary['source_probe_failures']}**",
        f"- Completed identities conflicting with source evidence: **{summary['completed_identity_conflicts']}**",
        f"- Missing source files: **{summary['missing_source_files']}**",
        f"- Clean output M4Bs: **{summary['clean_output_m4bs']}**",
        f"- Readable AAC clean outputs: **{summary['readable_aac_clean_outputs']}**",
        f"- Completed database records: **{summary['completed_book_records']}**",
        f"- Unique current output files: **{summary['unique_current_output_files']}**",
        f"- Current output-path collision groups: **{summary['output_path_collision_groups']}** ({summary['extra_output_claims']} extra claims)",
        f"- Missing current outputs: **{summary['missing_current_outputs']}**",
        f"- Current outputs with alignment issues: **{summary['current_outputs_with_issues']}**",
        f"- Untracked output M4Bs: **{summary['untracked_outputs']}**",
        f"- Untracked outputs duplicating a current identity: **{summary['untracked_duplicates_of_current']}**",
        f"- Duplicate author/title identity groups: **{summary['duplicate_identity_groups']}**",
        f"- Author-folder spelling collisions: **{summary['author_folder_variant_groups']}**",
        "",
        "## Current run states",
        "",
        *markdown_table(
            [[key, value] for key, value in sorted(summary["active_states"].items())],
            ["State", "Books"],
        ),
        "",
        "## Metadata provenance",
        "",
        *markdown_table(
            [[key, value] for key, value in sorted(summary["metadata_sources"].items())],
            ["Metadata source", "Active books"],
        ),
        "",
        f"Of the completed records, **{summary['completed_online_metadata']}** use online-provider metadata and **{summary['completed_local_metadata']}** still use locally inferred metadata.",
        "",
        "## Completed identities conflicting with source evidence",
        "",
        "These are review findings, not automatic verdicts: narrator-only tags and author aliases can create legitimate disagreements. Rows with both author and title conflicts, or placeholder-like titles, are the highest-risk cases.",
        "",
        *markdown_table(
            [
                [item["canonical_author"], item["canonical_title"], ", ".join(issue for issue in item["issues"] if "conflict" in issue), item["source_files"][0]]
                for item in payload["source_books"]
                if item["state"] == "complete" and any("conflict" in issue for issue in item["issues"])
            ],
            ["Canonical author", "Canonical title", "Conflict", "First source path"],
        ),
        "",
        "## Source-file detector coverage",
        "",
        "Files recorded as alternate representations were deliberately excluded in favor of another source representation. Unaccounted files were omitted without being retained as a selected file or a recorded alternate and require correction in the reconciliation logic.",
        "",
        *markdown_table(
            [
                ["yes" if item["readable"] else "no", item["group_key"], item["reason"], item["path"]]
                for item in payload["source_detector_coverage"]["unaccounted"]
            ],
            ["Readable", "Detected group", "Finding", "Source path"],
        ),
        "",
        "## Current outputs with alignment issues",
        "",
        *markdown_table(
            [
                [item["canonical_author"], item["canonical_title"], ", ".join(item["issues"]), item["relative_path"]]
                for item in current_issues
            ],
            ["Author", "Title", "Issue", "Path"],
        ),
        "",
        "## Current output-path collisions",
        "",
        "Each group below contains multiple detected-book records claiming the same physical M4B. This inflates the completed counter and can hide omitted discs or alternate editions.",
        "",
        *markdown_table(
            [
                [item["path"], len(item["claims"]), "; ".join(claim["book_id"] for claim in item["claims"])]
                for item in payload["output_path_collisions"]
            ],
            ["Output path", "Claims", "Detected identities"],
        ),
        "",
        "## Missing current outputs",
        "",
        *markdown_table(
            [
                [item["canonical_author"], item["canonical_title"], item["expected_output"]]
                for item in payload["missing_current_outputs"]
            ],
            ["Author", "Title", "Expected path"],
        ),
        "",
        "## Author-folder spelling collisions",
        "",
        *markdown_table(
            [[item["identity"], "; ".join(item["folders"])] for item in payload["author_folder_variants"]],
            ["Normalized identity", "Folders"],
        ),
        "",
        "## Untracked outputs",
        "",
        "These files exist in the clean output tree but are not owned by the current completed run. They are not automatically safe to delete; each needs reconciliation.",
        "",
        *markdown_table(
            [
                [item["classification"], item["observed_author"] or "", item["observed_title"] or "", item["relative_path"]]
                for item in payload["untracked_outputs"]
            ],
            ["Classification", "Observed author", "Observed title", "Path"],
        ),
        "",
        "## Duplicate author/title identities",
        "",
    ]
    if payload["duplicate_identity_groups"]:
        for group in payload["duplicate_identity_groups"]:
            identity = group["identity"]
            lines.extend(
                [
                    f"### {identity['author_key']} — {identity['title_key']}",
                    "",
                    *[f"- `{item['ownership']}` — `{item['path']}`" for item in group["files"]],
                    "",
                ]
            )
    else:
        lines.extend(["None.", ""])
    lines.extend(
        [
            "## Source identities requiring attention",
            "",
            "`unverified` means the cached source path/tags do not contain enough evidence; it is not automatically a wrong match. `conflict` means present embedded evidence disagrees with the current canonical identity.",
            "",
            *markdown_table(
                [
                    [item["state"], item["canonical_author"] or "", item["canonical_title"] or item["book_id"], ", ".join(item["issues"])]
                    for item in source_issues
                ],
                ["State", "Author", "Title / identity", "Issue"],
            ),
            "",
            "## Full-detail companion",
            "",
            f"All checked records, observed tags, similarities, and exact paths are in `{json_path.name}`.",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    parser.add_argument("--workers", type=int, default=8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("workers must be at least 1")
    database = args.database.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"database does not exist: {database}")
    if not source_root.is_dir():
        raise ValueError(f"source root does not exist: {source_root}")
    if not output_root.is_dir():
        raise ValueError(f"output root does not exist: {output_root}")

    run, books, cached = load_database(database)
    source_results, missing_source = source_checks(
        source_root=source_root, books=books, cached=cached
    )
    detector_coverage = source_detector_coverage(
        source_root=source_root, books=books, cached=cached
    )
    current, untracked, missing_outputs, duplicates, path_collisions = clean_output_checks(
        output_root=output_root, books=books, workers=args.workers
    )
    variants = author_folder_variants(output_root)
    states = Counter(str(book["state"]) for book in books)
    metadata_sources = Counter(metadata_provenance(book["metadata"]) for book in books)
    completed_sources = Counter(
        metadata_provenance(book["metadata"])
        for book in books
        if book["state"] == "complete"
    )
    current_issues = [item for item in current if item["issues"]]
    all_output_checks = [*current, *untracked]
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    payload: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "read-only",
        "roots": {
            "database": str(database),
            "source": str(source_root),
            "output": str(output_root),
        },
        "process_run": {
            "id": run["id"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        },
        "summary": {
            "active_books": len(books),
            "active_states": dict(states),
            "metadata_sources": dict(metadata_sources),
            "completed_online_metadata": sum(
                count for source, count in completed_sources.items() if source not in {"local", "none"}
            ),
            "completed_local_metadata": completed_sources["local"],
            "completed_identity_conflicts": sum(
                item["state"] == "complete" and any("conflict" in issue for issue in item["issues"])
                for item in source_results
            ),
            "source_media_records": len(cached),
            "source_detected_books": detector_coverage["detected_book_groups"],
            "source_reconciliation_states": detector_coverage["reconciliation_states"],
            "source_files_assigned": detector_coverage["active_file_references"],
            "source_alternate_representations": detector_coverage["recorded_alternate_representations"],
            "source_problem_files": detector_coverage["recorded_problem_files"],
            "source_unaccounted_files": detector_coverage["unaccounted_files"],
            "source_unaccounted_readable_files": detector_coverage["unaccounted_readable_files"],
            "source_probe_failures": sum(bool(item["error"]) for item in cached.values()),
            "missing_source_files": len(missing_source),
            "source_books_with_issues": sum(bool(item["issues"]) for item in source_results),
            "source_author_conflicts": sum("source_author_conflict" in item["issues"] for item in source_results),
            "source_title_conflicts": sum("source_title_conflict" in item["issues"] for item in source_results),
            "clean_output_m4bs": len(output_media_files(output_root)),
            "readable_aac_clean_outputs": sum(
                not item["probe"]["error"]
                and item["probe"]["codec"] == "aac"
                and bool(item["probe"]["duration_seconds"])
                for item in all_output_checks
            ),
            "completed_book_records": sum(book["state"] == "complete" for book in books),
            "unique_current_output_files": len(current),
            "output_path_collision_groups": len(path_collisions),
            "extra_output_claims": sum(len(item["claims"]) - 1 for item in path_collisions),
            "missing_current_outputs": len(missing_outputs),
            "current_outputs_with_issues": len(current_issues),
            "untracked_outputs": len(untracked),
            "untracked_duplicates_of_current": sum(item["classification"] == "duplicate_of_current" for item in untracked),
            "duplicate_identity_groups": len(duplicates),
            "author_folder_variant_groups": len(variants),
        },
        "missing_source_files": missing_source,
        "source_detector_coverage": detector_coverage,
        "source_books": source_results,
        "current_outputs": current,
        "missing_current_outputs": missing_outputs,
        "output_path_collisions": path_collisions,
        "untracked_outputs": untracked,
        "duplicate_identity_groups": duplicates,
        "author_folder_variants": variants,
    }
    json_path, markdown_path = write_reports(
        report_dir=args.report_dir.expanduser().resolve(), payload=payload, stamp=stamp
    )
    print(json.dumps({"summary": payload["summary"], "json": str(json_path), "markdown": str(markdown_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
