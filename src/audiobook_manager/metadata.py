from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class MetadataCandidate:
    provider: str
    provider_id: str
    title: str
    authors: tuple[str, ...]
    publish_year: int | None = None
    publisher: str | None = None
    isbn: str | None = None
    cover_url: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["authors"] = list(self.authors)
        return value


def search_open_library(query: str, *, limit: int = 8, timeout_seconds: float = 12) -> list[MetadataCandidate]:
    query = query.strip()
    if not query:
        raise ValueError("metadata query cannot be empty")
    params = urlencode({"q": query, "limit": min(max(limit, 1), 20), "fields": "key,title,author_name,first_publish_year,publisher,isbn,cover_i"})
    request = Request(
        f"https://openlibrary.org/search.json?{params}",
        headers={"Accept": "application/json", "User-Agent": "AudiobookManager/0.1 (personal library metadata review)"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        payload = json.load(response)
    candidates: list[MetadataCandidate] = []
    for doc in payload.get("docs", []):
        title = str(doc.get("title", "")).strip()
        key = str(doc.get("key", "")).strip()
        if not title or not key:
            continue
        publishers = doc.get("publisher") or []
        isbns = doc.get("isbn") or []
        cover_id = doc.get("cover_i")
        candidates.append(MetadataCandidate(
            provider="Open Library", provider_id=key, title=title,
            authors=tuple(str(x) for x in (doc.get("author_name") or [])[:5]),
            publish_year=doc.get("first_publish_year") if isinstance(doc.get("first_publish_year"), int) else None,
            publisher=str(publishers[0]) if publishers else None,
            isbn=str(isbns[0]) if isbns else None,
            cover_url=f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None,
        ))
    return candidates
