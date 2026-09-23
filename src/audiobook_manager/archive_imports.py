"""Revalidate completed archive imports during normal library discovery."""
from __future__ import annotations

import hashlib
import json
import zlib
from pathlib import Path

from .database import StateDatabase
from .executor import _identity
from .probe import probe_media


def archive_book_id(relative_path: Path) -> str:
    digest = hashlib.sha256(relative_path.as_posix().encode("utf-8")).hexdigest()[:24]
    return f"archive:{digest}"


def validate_imported_output(
    output: Path, *, metadata: dict[str, object], duration_seconds: float,
    chapter_count: int,
) -> tuple[bool, str]:
    try:
        probe = probe_media(output)
    except (OSError, RuntimeError) as error:
        return False, f"output cannot be probed: {error}"
    if probe.codec_name != "aac" or not probe.duration_seconds:
        return False, "output is not readable AAC audio"
    if abs(probe.duration_seconds - duration_seconds) > max(5.0, duration_seconds * 0.005):
        return False, "output duration differs from verified archive members"
    if len(probe.chapters) != chapter_count:
        return False, "output chapter count differs from verified archive members"
    title = _identity(metadata.get("title", ""))
    author = _identity((metadata.get("authors") or [""])[0])
    titles = {_identity(probe.tags.get(key, "")) for key in ("title", "album")}
    authors = {_identity(probe.tags.get(key, "")) for key in ("artist", "album_artist", "author")}
    if not title or title not in titles or not author or author not in authors:
        return False, "output embedded title or author differs from verified archive tags"
    return True, "AAC duration, chapters, and embedded identity match"


def sync_archive_imports(
    database: StateDatabase, *, run_id: int, source: Path, destination: Path,
) -> set[str]:
    """Make previously imported books visible; never trust stale provenance."""
    source, destination = source.resolve(), destination.resolve()
    active: set[str] = set()
    for record in database.archive_imports(source, destination):
        relative = Path(str(record["archive_relative_path"]))
        book_id = archive_book_id(relative)
        active.add(book_id)
        fingerprint = hashlib.sha256(json.dumps(
            [relative.as_posix(), record["size"], record["modified_ns"], record["members"]],
            sort_keys=True,
        ).encode()).hexdigest()
        evidence = [{"kind": "archive_import", "explanation":
                     "archive members were CRC-checked and converted in an approved import"}]
        database.store_detected_book(
            run_id=run_id, book_id=book_id, state="identified",
            source_fingerprint=fingerprint, classification="verified_archive_import",
            files=[relative.as_posix()], evidence=evidence,
        )
        try:
            archive = (source / relative).resolve()
            output = Path(str(record["output_path"])).resolve()
            if relative.is_absolute() or not archive.is_relative_to(source):
                raise ValueError("archive provenance path escapes source root")
            if not output.is_relative_to(destination):
                raise ValueError("registered output path escapes output root")
            stat = archive.stat()
            if stat.st_size != record["size"] or stat.st_mtime_ns != record["modified_ns"]:
                raise ValueError("source archive changed since import; manual review required")
            if not output.is_file():
                raise ValueError("registered output is missing")
            metadata = dict(record["metadata"])
            valid, detail = validate_imported_output(
                output, metadata=metadata,
                duration_seconds=float(record["duration_seconds"]),
                chapter_count=int(record["chapter_count"]),
            )
            if not valid:
                raise ValueError(detail)
            cover = next((item for item in record["members"]
                          if item.get("role") == "cover"), None)
            if cover:
                cover_path = output.parent / "cover.jpg"
                if not cover_path.is_file() or cover_path.stat().st_size != cover["size"]:
                    raise ValueError("registered archive cover is missing or changed")
                checksum = 0
                with cover_path.open("rb") as reader:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        checksum = zlib.crc32(chunk, checksum)
                if checksum & 0xFFFFFFFF != cover["crc32"]:
                    raise ValueError("registered archive cover checksum changed")
            owner = database.reserve_output_claim(run_id, book_id, output)
            if owner is not None:
                raise ValueError(f"output is claimed by another book: {owner}")
            metadata["_verified_output"] = True
            database.store_book_identification(
                run_id=run_id, book_id=book_id, state="metadata_matched",
                metadata=metadata, candidates=[], evidence=evidence,
                confidence=1,
            )
            database.adopt_verified_output(
                run_id=run_id, book_id=book_id, metadata=metadata, output_path=output,
            )
            database.log_event(run_id, "archive_import_verified", book_id=book_id,
                               detail=detail)
        except (OSError, RuntimeError, ValueError, TypeError, IndexError) as error:
            database.update_book_state(run_id, book_id, "quarantined", failure=str(error))
            database.log_event(run_id, "archive_import_needs_review", level="warning",
                               book_id=book_id, detail=str(error))
    return active
