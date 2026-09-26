from __future__ import annotations

import re
from typing import Any

from .display import person_key, prefer_display
from .models import BookGroup

ASIN = re.compile(r"\bB0[A-Z0-9]{8}\b", re.I)
ARTIFACTS = re.compile(
    r"\b(?:tmpfiles?|temp|converted?|converting|finished|unabridged|"
    r"graphic\s*audio|audiobooks?|normal\s+audio|\d+k)\b",
    re.I,
)
BOOK_NUMBER = re.compile(r"\bbook\s+(\d+(?:\.\d+)?)\s*[-:._]?\s*", re.I)
LEADING_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)?\s*[-:._]?\s*")
LEADING_BOOK_NUMBER = re.compile(r"^\s*book\s+\d+(?:\.\d+)?\s*[-:._]?\s*", re.I)
LEADING_COLLECTION_CODE = re.compile(r"^\s*hp\s*[-:._]?\s*\d+\s*[-:._]?\s*", re.I)
LEADING_FILE_POSITION = re.compile(
    r"^\s*(?:book\s*)?[a-z]?(\d+(?:\.\d+)?)\s*[-:._]?\s*", re.I
)
SERIES_NUMBER_TITLE = re.compile(
    r"^(.+?)\s+(\d+(?:\.\d+)?)(?![\d.])\s*[-:._]\s*(.+)$"
)
TRACK_LABEL = re.compile(r"^\s*[-_. ]*\d+\s*/\s*\d+\s*$")
NAVIGATION_TITLE = re.compile(
    r"^(?:(?:opening|end)\s+credits?|prologue|epilogue|"
    r"(?:chapter|track|part|disc|disk|cd)\s*\d*|\d+)$",
    re.I,
)
COLLECTION_ITEM = re.compile(r"^(.+?)\s+-\s+(\d{1,3}(?:\.\d+)?)\s+-\s+(.+)$")
BRACKETED_SERIES = re.compile(r"\[([^]]*?)\s+book\s+(\d+(?:\.\d+)?)\s*]", re.I)
VARIANT_FOLDER = re.compile(r"^alt(?:\.|ernate|ernative)?\s+versions?$", re.I)
GENERIC_LIBRARY_WORDS = frozenset({"audio", "audiobook", "audiobooks", "book", "books",
                                   "collection", "complete", "library", "series"})
GENERIC_TITLE_SUFFIX_WORDS = frozenset({
    "a", "adaptation", "adventure", "an", "audio", "audiobook", "dramatized",
    "edition", "light", "litrpg", "novel", "series", "the", "unabridged",
})
PLACEHOLDER_TITLES = frozenset({"album", "no title", "unknown", "unknown title", "untitled"})
QUALITY_MARKER = re.compile(r"\[\s*\d+\s*(?:k|kbps|vbr)\s*]", re.I)
PRODUCTION_CREDIT = re.compile(r"^graphic\s*audio(?:,?\s*llc\.?)?$", re.I)
NON_AUTHOR_CREDIT = re.compile(
    r"^(?:artist|no\s+artist|unknown(?:\s+author)?|"
    r"(?:narrated|read|performed)\s+by\b)",
    re.I,
)
GRAPHIC_AUDIO_BRACKETED_AUTHOR = re.compile(r"^graphic\s*audio\s*\[([^]]+)]$", re.I)
GRAPHIC_AUDIO_BOOK_FOLDER = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*-\s*(.+?)\s*-\s*(\d+(?:\.\d+)?)\s*-\s*.+?\s*-\s*GA\s*$",
    re.I,
)


def _clean_title(value: str) -> str:
    value = ARTIFACTS.sub(" ", value)
    value = LEADING_COLLECTION_CODE.sub("", value)
    value = QUALITY_MARKER.sub(" ", value)
    value = re.sub(r"\([^)]*(?:fantasy|audio|bitrate|narrat)[^)]*\)", " ", value, flags=re.I)
    value = re.sub(r"\(\s*\)", " ", value)
    value = re.sub(r"[,;]?\s+part\s+\d+\s*$", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value.replace("_", " ").strip(" -_"))


def _clean_series(value: str) -> str:
    return _clean_title(value).strip(" -_")


def _clean_author_credit(value: str) -> str:
    value = re.sub(r"\s*\(authors?\)\s*$", "", value, flags=re.I)
    value = re.sub(r"^\s*(?:written|authored)\s+by\s+", "", value, flags=re.I)
    value = re.split(
        r"[,;]?\s*(?:narrated|read|performed)\s+by\b", value, maxsplit=1, flags=re.I
    )[0].strip()
    return re.sub(
        r"\s*(?:\((?:writing\s+)?as\s+[^)]+\)|(?:writing\s+)?as\s+.+)$",
        "",
        value,
        flags=re.I,
    ).strip()


def _source_authors(values: list[str]) -> list[str]:
    authors: list[str] = []
    for value in values:
        if match := GRAPHIC_AUDIO_BRACKETED_AUTHOR.fullmatch(value):
            value = match.group(1).strip()
        elif PRODUCTION_CREDIT.fullmatch(value) or NON_AUTHOR_CREDIT.search(value):
            continue
        if value and person_key(value) not in {person_key(author) for author in authors}:
            authors.append(value)
    return authors


def _generic_title_suffix(value: str) -> bool:
    words = {
        word.casefold()
        for word in re.findall(r"[^\W_]+", value, flags=re.UNICODE)
    }
    return bool(words) and words <= GENERIC_TITLE_SUFFIX_WORDS


def _container_embedded_title(albums: list[str], series_names: list[str],
                              selected: list[Any]) -> str | None:
    """Keep a book-specific album when a collection container was split.

    The detector may split several long containers that share a collection,
    but some files also carry a precise per-book album. Prefer that identity
    when it differs from an explicit series tag or is supported by the file
    name. A genuinely shared collection album remains structural context.
    """
    if len(selected) != 1 or not albums:
        return None
    album = albums[0]
    if series_names and all(person_key(album) != person_key(series) for series in series_names):
        return album
    album_words = set(person_key(album).split())
    stem_words = set(person_key(selected[0].relative_path.stem).split())
    if album_words and album_words <= stem_words:
        return album
    return None


def _fallback_series_title(series: str | None, position: str | None) -> str | None:
    if not series or not position:
        return None
    cleaned = _clean_title(series)
    return f"{cleaned} {position}" if cleaned else None


def _series_from_prefix(prefix: str, title: str) -> str:
    candidate = _clean_series(prefix)
    if " - " in candidate:
        candidate = candidate.rsplit(" - ", 1)[-1].strip()
    if title and candidate.casefold().startswith(title.casefold()):
        remainder = candidate[len(title):].strip(" ,-:._")
        if remainder:
            candidate = remainder
    return candidate


def _name_like_author(candidate: str) -> bool:
    tokens = re.findall(r"[^\W\d_]+", candidate, flags=re.UNICODE)
    lowered = {token.casefold() for token in tokens}
    return (2 <= len(tokens) <= 5 and not lowered & GENERIC_LIBRARY_WORDS
            and not candidate.casefold().startswith("the ")
            and any(character.isupper() for character in candidate))


def _author_from_paths(selected: list[Any]) -> str | None:
    if not selected:
        return None
    roots = {item.relative_path.parts[0] for item in selected if len(item.relative_path.parts) > 1}
    if len(roots) != 1:
        return None
    root = next(iter(roots)).strip()
    if match := re.search(r"\bby\s+(.+)$", root, flags=re.I):
        return match.group(1).strip()

    segments = [segment.strip() for segment in root.split(" - ") if segment.strip()]
    # Author-title collection names consistently put the author in one of the
    # first two fields. Looking farther risks treating the book title as a name.
    candidates = segments[:2] if len(segments) > 1 else [root]
    return next((candidate for candidate in candidates if _name_like_author(candidate)), None)


def _path_supports_author(author: str, path_parts: list[str]) -> bool:
    contributors = [
        part.strip()
        for part in re.split(r"\s+(?:and|&)\s+|[,;]", author, flags=re.I)
        if part.strip()
    ]
    path_keys = [person_key(part) for part in path_parts]
    return any(
        key and any(key == path_key or f" {key} " in f" {path_key} " for path_key in path_keys)
        for key in map(person_key, contributors or [author])
    )


def extract_hint(group: BookGroup, chosen_files: tuple[str, ...] = ()) -> dict[str, Any]:
    selected = [item for item in group.files if not chosen_files or str(item.relative_path) in chosen_files]
    selected_tags = [item.probe.tags for item in selected if item.probe]
    all_tags = [item.probe.tags for item in group.files if item.probe]
    tags = selected_tags if any(tag.get("album") or tag.get("artist") or tag.get("author") for tag in selected_tags) else all_tags
    def values(*keys: str) -> list[str]:
        found: list[str] = []
        for tag in tags:
            for key in keys:
                value = tag.get(key, "").strip()
                if value and value.casefold() not in {x.casefold() for x in found}:
                    found.append(value)
        return found
    text = " ".join([group.key, *(str(item.relative_path) for item in selected),
                     *(value for tag in tags for value in tag.values())])
    asins = ASIN.findall(text.upper())
    albums = [value for value in values("album")
              if value.casefold() not in PLACEHOLDER_TITLES]
    selected_track_titles = [tag["title"].strip() for tag in selected_tags if tag.get("title", "").strip()]
    useful_track_titles = [value for value in selected_track_titles
                           if not TRACK_LABEL.match(value) and not NAVIGATION_TITLE.match(value)
                           and value.casefold() != "title"]
    useful_track_title = useful_track_titles[0] if useful_track_titles else None
    repeated_track_title = (useful_track_title if useful_track_title and
                            len({value.casefold() for value in useful_track_titles}) == 1 else None)
    collection_split = any("long-form collection files split" in reason for reason in group.reasons)
    container_collection_split = any(
        "audiobook-sized containers split from a shared collection album" in reason
        for reason in group.reasons
    )
    embedded_container_title = _container_embedded_title(
        albums, values("series"), selected
    ) if container_collection_split else None
    collection_match = COLLECTION_ITEM.match(selected[0].relative_path.stem) if collection_split and len(selected) == 1 else None
    graphic_audio_edition = "graphic audio" in group.key.casefold()
    duration_matched_identity = any("duration-matched alternate representation" in reason
                                    for reason in group.reasons)
    raw_title = (group.key.split(" — ")[-1]
                 if graphic_audio_edition or duration_matched_identity else
                 collection_match.group(3) if collection_match else
                 embedded_container_title if embedded_container_title else
                 group.key.split(" — ")[-1] if container_collection_split else repeated_track_title
                 if len(selected) > 1 and repeated_track_title else albums[0] if albums else
                 useful_track_title if len(selected) == 1 and useful_track_title else
                 group.key.split(" — ")[-1])
    title = _clean_title(LEADING_BOOK_NUMBER.sub("", LEADING_NUMBER.sub("", raw_title)))
    path_parts = [part for item in selected for part in item.relative_path.parts[:-1]]
    numbered = next(((part, BOOK_NUMBER.search(part)) for part in reversed(path_parts)
                     if BOOK_NUMBER.search(part)), None)
    bracketed = next(((part, BRACKETED_SERIES.search(part)) for part in reversed(path_parts)
                      if BRACKETED_SERIES.search(part)), None)
    series = (values("series", "grouping") or [None])[0]
    series_position: str | None = None
    if collection_match:
        series = series or (albums[0] if albums else collection_match.group(1))
        series_position = collection_match.group(2)
    if container_collection_split and albums:
        series = series or albums[0]
        if selected:
            stem = selected[0].relative_path.stem
            file_number = BOOK_NUMBER.search(stem) or LEADING_FILE_POSITION.match(stem)
            if file_number:
                series_position = file_number.group(1)
    if match := SERIES_NUMBER_TITLE.match(raw_title):
        prefix, series_position, leaf_title = match.groups()
        if not _generic_title_suffix(leaf_title):
            title = _clean_title(leaf_title)
        if not series:
            series = re.sub(r"\bvol(?:ume)?\.?\s*$", "", _clean_title(prefix), flags=re.I).rstrip(" ,-")
    if bracketed:
        _, match = bracketed
        assert match is not None
        series = series or _clean_series(match.group(1))
        series_position = series_position or match.group(2)
    if numbered:
        part, match = numbered
        assert match is not None
        series_position = match.group(1)
        index = path_parts.index(part)
        if not series and index > 0:
            previous = path_parts[index - 1]
            if VARIANT_FOLDER.fullmatch(previous) and index > 1:
                previous = path_parts[index - 2]
            series = _clean_series(previous)
        elif not series:
            prefix = part[:match.start()].strip(" -_.")
            if prefix:
                series = _series_from_prefix(prefix, title)
    if graphic_audio_edition:
        ga_folder = next(
            (match for part in reversed(path_parts)
             if (match := GRAPHIC_AUDIO_BOOK_FOLDER.fullmatch(part))),
            None,
        )
        if ga_folder:
            series = series or _clean_series(ga_folder.group(1))
            series_position = series_position or ga_folder.group(2)
    if title.casefold() in PLACEHOLDER_TITLES:
        title = _fallback_series_title(
            str(series) if series else None, series_position
        ) or title
    authors = _source_authors(values("author", "artist"))
    if not authors:
        authors = _source_authors(values("album_artist"))
    if not authors and selected:
        stem_parts = selected[0].relative_path.stem.split(" - ", 1)
        if len(stem_parts) == 2:
            left, right = map(_clean_title, stem_parts)
            if left.casefold() == title.casefold() and _name_like_author(right):
                authors = [right]
            elif title.casefold() in right.casefold() and _name_like_author(left):
                authors = [left]
    if not authors and (path_author := _author_from_paths(selected)):
        authors = [path_author]
    authors = [
        cleaned
        for author in authors
        if (cleaned := _clean_author_credit(prefer_display(author, path_parts)))
    ]
    narrators = [value for value in values("narrator", "composer")
                 if not PRODUCTION_CREDIT.fullmatch(value)]
    album_artists = [value for value in values("album_artist")
                     if not PRODUCTION_CREDIT.fullmatch(value)]
    if not narrators and album_artists and album_artists[0].casefold() not in {x.casefold() for x in authors}:
        narrators = album_artists
    if series and any(person_key(series) == person_key(author) for author in authors):
        series = None
        series_position = None
    author_path_supported = any(_path_supports_author(author, path_parts) for author in authors)
    allow_author_repair = bool(authors and series and not author_path_supported)
    return {"title": title, "authors": authors,
            "narrators": narrators, "series": series,
            "series_position": series_position,
            "allow_author_repair": allow_author_repair,
            "edition": "GraphicAudio" if graphic_audio_edition else None,
            "asin": asins[0] if asins else None,
            "isbn": (values("isbn", "isbn13", "isbn10") or [None])[0],
            "runtime_minutes": sum((item.probe.duration_seconds or 0) for item in selected if item.probe) / 60}
