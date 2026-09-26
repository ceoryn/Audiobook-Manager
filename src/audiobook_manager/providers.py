from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .metadata import search_open_library
from .database import StateDatabase
from .display import prefer_display
from .matching import ScoredCandidate, choose_automatic


def latin_display_metadata(metadata: dict[str, Any]) -> bool:
    """Return whether user-facing identity fields use only Latin letter scripts."""
    series = metadata.get("series")
    values = [metadata.get("title"), *(metadata.get("authors") or [])]
    values.append(series.get("name") if isinstance(series, dict) else series)
    for value in values:
        for character in str(value or ""):
            if character.isalpha() and "LATIN" not in unicodedata.name(character, ""):
                return False
    return True


class MetadataProvider(Protocol):
    name: str
    def search(self, query: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class OpenLibraryProvider:
    name: str = "open_library"
    def search(self, query: str) -> list[dict[str, Any]]:
        return [item.to_dict() for item in search_open_library(query)]


@dataclass(frozen=True)
class GoogleBooksProvider:
    api_key: str | None = None
    name: str = "google_books"
    def search(self, query: str) -> list[dict[str, Any]]:
        key = self.api_key or os.environ.get("GOOGLE_BOOKS_API_KEY")
        if not key:
            return []
        url = "https://www.googleapis.com/books/v1/volumes?" + urlencode({"q": query, "maxResults": 8, "key": key})
        with urlopen(Request(url, headers={"User-Agent": "AudiobookManager/0.1"}), timeout=12) as response:  # noqa: S310
            payload = json.load(response)
        results = []
        for item in payload.get("items", []):
            info = item.get("volumeInfo", {})
            identifiers = info.get("industryIdentifiers") or []
            results.append({"provider": "Google Books", "provider_id": item.get("id"),
                "title": info.get("title", ""), "authors": info.get("authors") or [],
                "publisher": info.get("publisher"), "publish_year": str(info.get("publishedDate", ""))[:4] or None,
                "description": info.get("description"), "cover_url": (info.get("imageLinks") or {}).get("thumbnail"),
                "isbn": identifiers[0].get("identifier") if identifiers else None})
        return results


@dataclass(frozen=True)
class AudnexusProvider:
    asin: str
    region: str = "us"
    name: str = "audnexus"
    def search(self, query: str) -> list[dict[str, Any]]:
        del query
        url = f"https://api.audnex.us/books/{quote(self.asin)}?region={quote(self.region)}"
        with urlopen(Request(url, headers={"User-Agent": "AudiobookManager/0.1"}), timeout=12) as response:  # noqa: S310
            item = json.load(response)
        return [{"provider": "Audnexus", "provider_id": item.get("asin", self.asin),
            "asin": item.get("asin"), "title": item.get("title", ""),
            "authors": [x.get("name") for x in item.get("authors", []) if x.get("name")],
            "narrators": [x.get("name") for x in item.get("narrators", []) if x.get("name")],
            "series": item.get("seriesPrimary"), "description": item.get("description"),
            "publisher": item.get("publisherName"), "runtime_minutes": item.get("runtimeLengthMin"),
            "cover_url": item.get("image") or item.get("imageUrl")}]


def search_providers(query: str, providers: list[MetadataProvider]) -> tuple[list[dict[str, Any]], list[str]]:
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for provider in providers:
        try:
            results.extend(provider.search(query))
        except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
            failures.append(f"{provider.name}: {exc}")
    return results, failures


def _normalize_chosen_display(chosen: ScoredCandidate, hint: dict[str, Any],
                              candidates: list[dict[str, Any]]) -> ScoredCandidate:
    """Improve casing from equivalent evidence without changing matched identities."""
    metadata = dict(chosen.candidate)
    titles = [hint.get("title"), *(item.get("title") for item in candidates)]
    if metadata.get("title"):
        metadata["title"] = prefer_display(str(metadata["title"]), titles)
    author_options = [*(hint.get("authors") or []),
                      *(author for item in candidates for author in (item.get("authors") or []))]
    if metadata.get("authors"):
        metadata["authors"] = [prefer_display(str(author), author_options)
                               for author in metadata["authors"]]
    if not metadata.get("series") and hint.get("series"):
        metadata["series"] = hint["series"]
    if not (metadata.get("series_position") or metadata.get("volume")) and hint.get("series_position"):
        metadata["series_position"] = hint["series_position"]
    if isinstance(metadata.get("series"), str):
        series_options = [hint.get("series"), *(item.get("series") for item in candidates
                                                if isinstance(item.get("series"), str))]
        metadata["series"] = prefer_display(str(metadata["series"]), series_options)
    edition = str(hint.get("edition") or "").strip()
    if edition and edition.casefold() not in str(metadata.get("title") or "").casefold():
        metadata["title"] = f"{metadata['title']} ({edition})"
        metadata["edition"] = edition
    return ScoredCandidate(metadata, chosen.score, chosen.reasons)


def identify_metadata(hint: dict[str, Any], providers: list[MetadataProvider], database: StateDatabase,
                      *, threshold: int = 80,
                      prefer_latin: bool = False) -> tuple[ScoredCandidate | None, list[ScoredCandidate], list[str]]:
    title = str(hint.get("title", "")).strip()
    simplified = re.sub(r"^.*?\bbook\s+\d+(?:\.\d+)?\s*[-:._]?\s*", "", title, flags=re.I).strip()
    authors = list(map(str, hint.get("authors") or []))
    series = str(hint.get("series") or "").strip()
    def quoted(value: str) -> str:
        return value.replace('"', " ").strip()
    fielded = (f'title:"{quoted(simplified)}" author:"{quoted(authors[0])}"'
               if simplified and authors else "")
    queries = [fielded, " ".join([simplified, *authors]).strip(),
               " ".join([simplified, series]).strip() if series else "", simplified,
               " ".join([title, *authors]).strip(), title]
    queries = list(dict.fromkeys(query for query in queries if query))
    candidates: list[dict[str, Any]] = []
    failures: list[str] = []
    for query in queries:
        for provider in providers:
            cached = database.cached_metadata(provider.name, query)
            if cached is not None:
                candidates.extend(cached)
                continue
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    if attempt: time.sleep(2 ** (attempt - 1))
                    else: time.sleep(0.35)
                    found = provider.search(query)
                    database.cache_metadata(provider.name, query, found)
                    candidates.extend(found)
                    last_error = None
                    break
                except HTTPError as exc:
                    if exc.code in {400, 404}:
                        database.cache_metadata(provider.name, query, [])
                        last_error = None
                        break
                    last_error = exc
                except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
                    last_error = exc
            if last_error:
                failures.append(f"{provider.name} after 3 attempts: {last_error}")
        provisional, _ = choose_automatic(hint, candidates, threshold=threshold)
        if provisional and (not prefer_latin or latin_display_metadata(provisional.candidate)):
            break
        if authors and hint.get("allow_author_repair"):
            # A focused title/series search can repair an embedded Artist tag
            # that actually names the narrator. Stop before a broad title-only
            # query introduces unrelated books with the same title.
            title_only, _ = choose_automatic({**hint, "authors": []}, candidates,
                                             threshold=threshold)
            if title_only:
                break
    chosen, ranked = choose_automatic(hint, candidates, threshold=threshold)
    if not chosen and hint.get("authors") and hint.get("allow_author_repair"):
        # Embedded author fields are often series, narrator, or production
        # credits. A unique exact-title result may repair that bad local field.
        title_only_hint = {**hint, "authors": []}
        title_only_chosen, title_only_ranked = choose_automatic(
            title_only_hint, candidates, threshold=threshold)
        if title_only_chosen:
            chosen, ranked = title_only_chosen, title_only_ranked
    if prefer_latin:
        latin_candidates = [item for item in candidates if latin_display_metadata(item)]
        # Display preferences must not erase a supported Latin author. Only
        # native-script author names require the title-based transliteration
        # fallback; an explicitly repairable local credit uses the same rule
        # as the normal identification path above.
        translate_author = bool(authors) and all(
            not latin_display_metadata({"authors": [author]}) for author in authors
        )
        latin_hint = ({**hint, "authors": []}
                      if translate_author or hint.get("allow_author_repair") else hint)
        latin_chosen, _ = choose_automatic(
            latin_hint, latin_candidates, threshold=threshold
        )
        if latin_chosen:
            chosen = latin_chosen
        elif chosen and not latin_display_metadata(chosen.candidate):
            chosen = None
    if chosen:
        chosen = _normalize_chosen_display(chosen, hint, candidates)
    return chosen, ranked, failures


def default_providers(hint: dict[str, Any]) -> list[MetadataProvider]:
    providers: list[MetadataProvider] = []
    if hint.get("asin"):
        providers.append(AudnexusProvider(str(hint["asin"])))
    providers.extend((OpenLibraryProvider(), GoogleBooksProvider()))
    return providers
