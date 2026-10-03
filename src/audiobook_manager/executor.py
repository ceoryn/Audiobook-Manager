from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .conversion import convert_to_m4b, copy_verified_m4b
from .database import StateDatabase
from .output import normalize_output_metadata, plan_output, write_cover
from .probe import is_aac_container, probe_media
from .providers import latin_display_metadata


class OutputClaimConflict(RuntimeError):
    """Raised when two detected identities resolve to one physical output."""


class ExistingOutputConflict(RuntimeError):
    """Raised when a pre-existing output does not match the current source job."""


def failure_detail(exc: BaseException, limit: int = 4000) -> str:
    detail = str(exc)
    if len(detail) <= limit:
        return detail
    half = (limit - 80) // 2
    return f"{detail[:half]}\n… {len(detail) - (half * 2)} diagnostic characters omitted …\n{detail[-half:]}"


def _identity(value: object) -> str:
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def _existing_output_is_compatible(
    output: Path, source: Path, files: list[str], metadata: dict[str, object]
) -> tuple[bool, str]:
    """Validate an idempotent output before adopting it for the current job."""
    try:
        result = probe_media(output)
        source_probes = [probe_media(source / relative) for relative in files]
    except (OSError, RuntimeError) as exc:
        return False, f"media validation failed: {exc}"
    expected_duration = sum(item.duration_seconds or 0 for item in source_probes)
    actual_duration = result.duration_seconds or 0
    if result.codec_name != "aac" or not actual_duration:
        return False, "existing output is not readable AAC audio"
    if expected_duration <= 0 or abs(actual_duration - expected_duration) > max(
        5.0, expected_duration * 0.005
    ):
        return False, (
            f"duration differs from current source job: expected {expected_duration:.2f}s, "
            f"found {actual_duration:.2f}s"
        )
    preserve_source_chapters = (
        len(files) == 1
        and is_aac_container(source_probes[0])
    )
    expected_chapters = (
        len(source_probes[0].chapters) if preserve_source_chapters else len(files)
    )
    if len(result.chapters) != expected_chapters:
        return False, (
            f"chapter count differs from current source job: expected {expected_chapters}, "
            f"found {len(result.chapters)}"
        )
    expected_title = _identity(metadata.get("title"))
    observed_titles = {_identity(result.tags.get(key, "")) for key in ("title", "album")}
    if not expected_title or expected_title not in observed_titles:
        return False, "embedded title does not match the current metadata"
    authors = [_identity(value) for value in (metadata.get("authors") or []) if _identity(value)]
    observed_authors = {
        _identity(result.tags.get(key, ""))
        for key in ("author", "artist", "album_artist", "albumartist")
    }
    if not authors or not any(
        expected == observed
        or f" {expected} " in f" {observed} "
        or f" {observed} " in f" {expected} "
        for expected in authors
        for observed in observed_authors
        if observed
    ):
        return False, "embedded author does not match the current metadata"
    return True, "duration and embedded identity match"


def _source_is_canonical(source: Path, destination: Path, plan_audio: Path,
                         files: list[str], metadata: dict[str, object]) -> bool:
    if len(files) != 1 or Path(files[0]).suffix.casefold() != ".m4b":
        return False
    expected = plan_audio.relative_to(destination.resolve())
    if Path(files[0]) != expected:
        return False
    probe = probe_media(source / files[0])
    title = _identity(metadata.get("title"))
    source_titles = {_identity(probe.tags.get(key, "")) for key in ("title", "album")}
    authors = metadata.get("authors") or []
    author = _identity(authors[0]) if isinstance(authors, list) and authors else ""
    source_authors = {_identity(probe.tags.get(key, "")) for key in ("author", "artist", "album_artist")}
    if not title or title not in source_titles or not author or author not in source_authors:
        return False
    series = metadata.get("series")
    if isinstance(series, dict):
        series = series.get("name")
    return not series or _identity(series) == _identity(probe.tags.get("grouping", ""))


def _prune_empty_output_directories(start: Path, output_root: Path) -> list[Path]:
    """Remove only empty conversion footprints, never files or the output root."""
    root = output_root.resolve()
    current = start.resolve()
    current.relative_to(root)
    removed: list[Path] = []
    while current != root:
        try:
            current.rmdir()
        except OSError:
            break
        removed.append(current)
        current = current.parent
    return removed


def reuse_verified_output(
    database: StateDatabase,
    *,
    run_id: int,
    book_id: str,
    source_fingerprint: str,
    source: Path,
    destination: Path,
    files: list[str],
    require_latin: bool = False,
    known_files: list[str] | None = None,
) -> str | None:
    """Adopt a validated, explicitly verified output for unchanged source files."""
    output_root = destination.resolve()
    for prior in database.verified_outputs_for_source(
        source_fingerprint, before_run_id=run_id
    ):
        metadata = prior["metadata"]
        if not isinstance(metadata, dict) or (require_latin and not latin_display_metadata(metadata)):
            continue
        output = Path(str(prior["output_path"])).resolve()
        if not output.is_file() or not output.is_relative_to(output_root):
            continue
        approved = metadata.get("_approved_source_selection")
        selected: list[str] | None = None
        if approved is not None:
            if (not isinstance(approved, list) or not approved
                    or not all(isinstance(path, str) for path in approved)
                    or len(set(approved)) != len(approved) or known_files is None
                    or not set(approved) <= set(known_files)
                    or any(Path(path).is_absolute() or ".." in Path(path).parts
                           or not (source / path).resolve().is_relative_to(source.resolve())
                           for path in approved)):
                continue
            selected = approved
        compatible, _detail = _existing_output_is_compatible(output, source, selected or files, metadata)
        if not compatible:
            continue
        owner = database.reserve_output_claim(run_id, book_id, output)
        if owner is not None:
            continue
        try:
            database.adopt_verified_output(
                run_id=run_id, book_id=book_id, metadata=metadata, output_path=output,
                selected_files=selected,
            )
        except Exception:
            database.release_output_claim(run_id, book_id, output)
            raise
        return str(output)
    return None


def execute_book(database: StateDatabase, run_id: int, book_id: str) -> str:
    run = database.process_run(run_id)
    book = next((item for item in database.process_books(run_id) if item["book_id"] == book_id), None)
    if run is None or book is None:
        raise ValueError("process run or book does not exist")
    if book["state"] not in {"metadata_matched", "local_metadata"} or not book["metadata"]:
        raise ValueError("book is not automatically eligible")
    source, destination = Path(str(run["source_root"])), Path(str(run["destination_root"]))
    metadata = normalize_output_metadata(destination, book["metadata"])
    plan = plan_output(destination, metadata)
    claim_acquired = False
    try:
        owner = database.reserve_output_claim(run_id, book_id, plan.audio)
        if owner is not None:
            detail = f"output path is already claimed by detected identity: {owner}"
            database.update_book_state(run_id, book_id, "quarantined", failure=detail)
            raise OutputClaimConflict(detail)
        claim_acquired = True
        if plan.audio.exists():
            compatible, detail = _existing_output_is_compatible(
                plan.audio, source, list(map(str, book["files"])), metadata
            )
            if not compatible:
                raise ExistingOutputConflict(detail)
            database.update_book_state(run_id, book_id, "complete", output_path=str(plan.audio))
            return str(plan.audio)
        if (book["state"] == "metadata_matched" and
                _source_is_canonical(source, destination, plan.audio, book["files"], metadata)):
            output = copy_verified_m4b(source=source / str(book["files"][0]), library_root=source,
                                       output_root=destination, destination=plan.audio)
            database.log_event(run_id, "clean_source_copied", book_id=book_id,
                               detail="byte-identical SHA-256 verified M4B copy")
        else:
            inputs = [source / str(path) for path in book["files"]]
            copy_audio = (
                len(inputs) == 1
                and is_aac_container(probe_media(inputs[0]))
            )
            output = convert_to_m4b(inputs=inputs,
                library_root=source, output_root=destination, metadata=metadata,
                destination=plan.audio, copy_audio=copy_audio)
        cover_url = str(metadata.get("cover_url") or "")
        if cover_url:
            try:
                write_cover(cover_url, plan.cover)
            except (OSError, TimeoutError, ValueError) as exc:
                database.log_event(run_id, "cover_skipped", level="warning", book_id=book_id,
                                   detail=f"optional cover unavailable: {failure_detail(exc, 500)}")
        database.update_book_state(run_id, book_id, "complete", output_path=str(output))
        return str(output)
    except Exception as exc:
        if isinstance(exc, FileExistsError) and plan.audio.exists():
            compatible, _ = _existing_output_is_compatible(
                plan.audio, source, list(map(str, book["files"])), metadata
            )
            if compatible:
                database.update_book_state(run_id, book_id, "complete", output_path=str(plan.audio))
                return str(plan.audio)
        if claim_acquired:
            database.release_output_claim(run_id, book_id, plan.audio)
        removed = _prune_empty_output_directories(plan.directory, destination)
        state = "quarantined" if isinstance(
            exc, (OutputClaimConflict, ExistingOutputConflict)
        ) else "failed"
        database.update_book_state(run_id, book_id, state, failure=failure_detail(exc))
        if removed:
            database.log_event(run_id, "empty_output_pruned", book_id=book_id,
                               detail=f"removed {len(removed)} empty conversion directories")
        raise
