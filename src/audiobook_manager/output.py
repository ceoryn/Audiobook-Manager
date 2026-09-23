from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .display import display_key, person_key, prefer_display

UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")
SPACE = re.compile(r"\s+")


def safe_component(value: str, fallback: str = "Unknown") -> str:
    cleaned = SPACE.sub(" ", UNSAFE.sub(" ", value)).strip(" .")
    return cleaned[:160] or fallback


def _position(value: object) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        number = float(text)
        return f"{int(number):02d}" if number.is_integer() else f"{number:04.1f}".rstrip("0")
    except ValueError:
        return safe_component(text)


@dataclass(frozen=True)
class OutputPlan:
    directory: Path
    audio: Path
    cover: Path


def _existing_directory(parent: Path, proposed: str, *, person: bool = False) -> str:
    identity = person_key if person else display_key
    try:
        existing = [path.name for path in parent.iterdir()
                    if path.is_dir() and identity(path.name) == identity(proposed)]
    except OSError:
        existing = []
    return prefer_display(proposed, existing, identity)


def normalize_output_metadata(destination: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Reuse the established author spelling for case-equivalent output folders."""
    normalized = dict(metadata)
    authors = list(metadata.get("authors") or [metadata.get("author") or "Unknown Author"])
    if authors:
        authors[0] = _existing_directory(destination.resolve(), str(authors[0]), person=True)
    normalized["authors"] = authors
    return normalized


def approved_cover_url(url: str) -> str:
    """Normalize and validate cover hosts supplied by configured metadata providers."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme == "http" and (host == "books.google.com" or host.endswith(".googleusercontent.com")):
        parsed = parsed._replace(scheme="https")
    if parsed.scheme != "https" or not (
        host == "covers.openlibrary.org" or host == "books.google.com" or host.endswith(".googleusercontent.com")
    ):
        raise ValueError("cover URL is not from an approved HTTPS metadata provider")
    return urlunsplit(parsed)


def plan_output(destination: Path, metadata: dict[str, Any]) -> OutputPlan:
    authors = metadata.get("authors") or [metadata.get("author") or "Unknown Author"]
    author = safe_component(str(authors[0]), "Unknown Author")
    author = _existing_directory(destination.resolve(), author, person=True)
    title = safe_component(str(metadata.get("title") or "Unknown Title"), "Unknown Title")
    series_value = metadata.get("series")
    if isinstance(series_value, dict):
        series_name = series_value.get("name")
        position = _position(series_value.get("position"))
    else:
        series_name = series_value
        position = _position(metadata.get("series_position") or metadata.get("volume"))
    book_folder = f"{position} - {title}" if position else title
    directory = destination.resolve() / author
    if series_name:
        directory /= safe_component(str(series_name))
    directory /= safe_component(book_folder)
    return OutputPlan(directory, directory / f"{author} - {title}.m4b", directory / "cover.jpg")


def write_cover(url: str, destination: Path) -> None:
    url = approved_cover_url(url)
    with urlopen(Request(url, headers={"User-Agent": "AudiobookManager/0.1"}), timeout=15) as response:  # noqa: S310
        content = response.read(10_000_001)
    if not content or len(content) > 10_000_000:
        raise ValueError("cover image is empty or larger than 10 MB")
    if not (content.startswith(b"\xff\xd8\xff") or content.startswith(b"\x89PNG\r\n\x1a\n")):
        raise ValueError("cover response is not a JPEG or PNG image")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    temporary = destination.with_suffix(destination.suffix + ".partial")
    temporary.write_bytes(content)
    temporary.replace(destination)
