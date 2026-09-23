from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from collections.abc import Callable

from .database import StateDatabase
from .detection import detect_books
from .reconcile import reconcile_group
from .hints import extract_hint
from .providers import default_providers, identify_metadata, latin_display_metadata
from .executor import execute_book, failure_detail, reuse_verified_output
from .scanner import scan_library
from .display import person_key
from .archive_imports import sync_archive_imports


ROLE_OR_ORGANIZATION = re.compile(
    r"graphic\s*audio|narrat(?:ed|or)|read\s+by|publisher|press|audio\s*(?:llc)?|no\s+artist|^artist$",
    re.I,
)


def _normalized_local_authors(hint: dict[str, object]) -> list[str]:
    narrator_keys = {person_key(value) for value in (hint.get("narrators") or [])}
    normalized: list[str] = []
    for raw in (hint.get("authors") or []):
        value = html.unescape(str(raw)).strip().strip(";")
        bracket = re.search(r"graphic\s*audio\s*\[([^]]+)]", value, flags=re.I)
        if bracket:
            value = bracket.group(1).strip()
        elif re.fullmatch(r"graphic\s*audio(?:,?\s*llc\.?)?", value, flags=re.I):
            continue
        value = re.sub(r"^written\s+by\s+", "", value, flags=re.I)
        value = re.split(r"[,;]?\s*(?:narrated|read|performed)\s+by\b", value,
                         maxsplit=1, flags=re.I)[0]
        value = re.sub(r"\s*\((?:(?:writing\s+)?as\s+[^)]+|audio)\)\s*$", "", value,
                       flags=re.I)
        value = value.split("/", 1)[0].strip()
        comma_parts = [part.strip() for part in value.split(",") if part.strip()]
        if len(comma_parts) == 2 and all(len(part.split()) == 1 for part in comma_parts):
            value = f"{comma_parts[1]} {comma_parts[0]}"
            parts = [value]
        else:
            parts = [part.strip() for part in re.split(r"\s+(?:and|&)\s+|[,;]", value)
                     if part.strip()]
        for part in parts:
            key = person_key(part)
            if (not key or key in narrator_keys or ROLE_OR_ORGANIZATION.search(part)
                    or key in {person_key(existing) for existing in normalized}):
                continue
            normalized.append(part)
    return normalized


def _local_metadata(hint: dict[str, object]) -> dict[str, object]:
    title = str(hint.get("title") or "Untitled").strip() or "Untitled"
    edition = str(hint.get("edition") or "").strip()
    if edition and edition.casefold() not in title.casefold():
        title = f"{title} ({edition})"
    authors = _normalized_local_authors(hint)
    metadata: dict[str, object] = {"title": title, "authors": authors or ["Unknown Author"],
                                  "_metadata_source": "local", "_needs_metadata_review": True}
    for key in ("narrators", "series", "series_position", "edition", "asin", "isbn"):
        if hint.get(key): metadata[key] = hint[key]
    return metadata


def _metadata_is_usable(metadata: dict[str, object], *, require_latin: bool = False) -> bool:
    title = str(metadata.get("title") or "").strip().casefold()
    authors = [str(value).strip() for value in (metadata.get("authors") or [])]
    author_keys = [person_key(author) for author in authors]
    series = metadata.get("series")
    if isinstance(series, dict):
        series = series.get("name")
    primary = authors[0] if authors else ""
    title_matches_author = bool(primary and person_key(title) == person_key(primary))
    suspicious_primary = (ROLE_OR_ORGANIZATION.search(primary) or primary.casefold().startswith("tmp ")
                          or any(character.isdigit() for character in primary)
                          or any(character in primary for character in "[]")
                          or " of " in primary.casefold()
                          or (series and person_key(primary) == person_key(series)))
    return ((not require_latin or latin_display_metadata(metadata))
            and title not in {"", "untitled", "unknown title"} and not title_matches_author
            and bool(author_keys)
            and not suspicious_primary and all(
                author not in {"", "unknown", "unknown author", "no artist"}
                for author in author_keys))


def discover_run(source: Path, destination: Path, database_path: Path, *, workers: int = 4,
                 identify: bool = False, metadata_threshold: int = 80,
                 prefer_latin_metadata: bool = False,
                 checkpoint: Callable[[], None] | None = None) -> int:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("source and destination must be separate, non-nested directories")
    with StateDatabase(database_path) as database:
        run_id = database.resumable_process_run(source, destination) or database.start_process_run(source, destination)
        database.update_process_run(run_id, "discovering")
        database.log_event(run_id, "scan_started", detail=str(source))
        files = scan_library(source, database=database, workers=workers)
        database.log_event(run_id, "scan_complete", detail=f"{len(files)} media files")
        reset = database.begin_rediscovery(run_id)
        if reset:
            database.log_event(run_id, "rediscovery_reset", detail=f"{reset} prior outcomes hidden pending reevaluation")
        active_ids: set[str] = set()
        for group in detect_books(files):
            if checkpoint: checkpoint()
            active_ids.add(group.key)
            result = reconcile_group(group)
            members = list(group.files)
            fingerprint = hashlib.sha256("\n".join(f"{item.relative_path}|{item.size}|{item.modified_ns}" for item in members).encode()).hexdigest()
            state = "quarantined" if result.state == "quarantine" else "identified"
            evidence = ([{"kind": "representation", "explanation": reason} for reason in result.evidence] +
                        [{"kind": "conflict", "explanation": reason} for reason in result.conflicts])
            database.store_detected_book(run_id=run_id, book_id=group.key, state=state,
                source_fingerprint=fingerprint, classification=result.state,
                files=list(result.chosen_files or result.alternate_files or result.problem_files),
                alternate_files=list(result.alternate_files),
                problem_files=list(result.problem_files), evidence=evidence)
            database.log_event(run_id, "book_detected", book_id=group.key, detail=result.state)
            if identify and state != "quarantined":
                reused = reuse_verified_output(
                    database,
                    run_id=run_id,
                    book_id=group.key,
                    source_fingerprint=fingerprint,
                    source=source,
                    destination=destination,
                    files=list(result.chosen_files or result.alternate_files or result.problem_files),
                    require_latin=prefer_latin_metadata,
                )
                if reused:
                    database.log_event(
                        run_id,
                        "verified_output_reused",
                        book_id=group.key,
                        detail=reused,
                    )
                    continue
                hint = extract_hint(group, result.chosen_files)
                chosen, ranked, failures = identify_metadata(hint, default_providers(hint), database,
                                                              threshold=metadata_threshold,
                                                              prefer_latin=prefer_latin_metadata)
                score = (ranked[0].score / 100) if ranked else 0
                metadata_evidence = evidence + [{"kind": "metadata_score", "explanation": reason}
                                                for reason in (ranked[0].reasons if ranked else ())]
                metadata = chosen.candidate if chosen else _local_metadata(hint)
                state = "metadata_matched" if chosen else "local_metadata"
                database.store_book_identification(run_id=run_id, book_id=group.key,
                    state=state, metadata=metadata,
                    candidates=[{"score": item.score, "metadata": item.candidate,
                                 "reasons": list(item.reasons)} for item in ranked],
                    evidence=metadata_evidence, confidence=score,
                    failure=None if chosen else ("online metadata unavailable; using reviewable local metadata"))
                database.log_event(run_id, "metadata_matched" if chosen else "local_metadata_selected",
                    level="info", book_id=group.key,
                    detail=(f"score={score:.2f}" if chosen else
                            ("provider unavailable; local evidence retained" if failures else "online match below threshold; local evidence retained")))
        active_ids.update(sync_archive_imports(
            database, run_id=run_id, source=source, destination=destination,
        ))
        superseded = database.supersede_missing_books(run_id, active_ids)
        if superseded:
            database.log_event(run_id, "detector_identities_superseded",
                               detail=f"{superseded} obsolete identities retained as audit history")
    return run_id


def process_library(source: Path, destination: Path, database_path: Path, *, workers: int = 4,
                    metadata_threshold: int = 80, conversion_workers: int = 4,
                    prefer_latin_metadata: bool = False,
                    checkpoint: Callable[[], None] | None = None) -> dict[str, object]:
    if conversion_workers < 1:
        raise ValueError("conversion workers must be at least 1")
    run_id = discover_run(source, destination, database_path, workers=workers,
                          identify=True, metadata_threshold=metadata_threshold,
                          prefer_latin_metadata=prefer_latin_metadata, checkpoint=checkpoint)
    destination = destination.resolve()
    with StateDatabase(database_path) as database:
        database.update_process_run(run_id, "processing")
        eligible = [str(book["book_id"]) for book in database.process_books(run_id)
                    if book["state"] == "metadata_matched" or
                    (book["state"] == "local_metadata" and book["metadata"] and
                     _metadata_is_usable(book["metadata"], require_latin=prefer_latin_metadata))]
        database.log_event(run_id, "conversion_queue_started",
                           detail=f"{len(eligible)} books; {conversion_workers} parallel workers")

    def convert_one(book_id: str) -> None:
        with StateDatabase(database_path) as worker_database:
            try:
                worker_database.log_event(run_id, "book_conversion_started", book_id=book_id)
                execute_book(worker_database, run_id, book_id)
                worker_database.log_event(run_id, "book_complete", book_id=book_id)
            except (OSError, RuntimeError, ValueError) as exc:
                worker_database.log_event(run_id, "book_failed", level="error", book_id=book_id,
                                          detail=failure_detail(exc))

    with ThreadPoolExecutor(max_workers=conversion_workers, thread_name_prefix="audiobook-convert") as executor:
        pending: set[Future[None]] = set()
        remaining = iter(eligible)
        exhausted = False
        while pending or not exhausted:
            while len(pending) < conversion_workers and not exhausted:
                if checkpoint: checkpoint()
                try:
                    book_id = next(remaining)
                except StopIteration:
                    exhausted = True
                    break
                pending.add(executor.submit(convert_one, book_id))
            if pending:
                done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()

    with StateDatabase(database_path) as database:
        books = [book for book in database.process_books(run_id) if book["state"] != "superseded"]
        quarantine = destination / "_quarantine"
        questionable = [book for book in books if book["state"] in {"quarantined", "failed"}]
        if questionable:
            quarantine.mkdir(parents=True, exist_ok=True)
            (quarantine / "manifest.json").write_text(
                json.dumps(questionable, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        reviewable = [{"book_id": book["book_id"], "output_path": book["output_path"],
                       "metadata": book["metadata"]} for book in books
                      if book["metadata"] and book["metadata"].get("_needs_metadata_review")]
        if reviewable:
            review = destination / "_metadata_review"
            review.mkdir(parents=True, exist_ok=True)
            (review / "manifest.json").write_text(
                json.dumps(reviewable, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        counts = Counter(str(book["state"]) for book in books)
        summary: dict[str, object] = {"run_id": run_id, "books_discovered": len(books),
            "books_successfully_processed": counts["complete"], "books_quarantined": counts["quarantined"],
            "failures": counts["failed"], "metadata_unresolved": counts["identified"],
            "metadata_retry": counts["metadata_retry"],
            "books_needing_metadata_review": len(reviewable),
            "destination": str(destination)}
        database.update_process_run(run_id, "complete", summary)
        return summary
