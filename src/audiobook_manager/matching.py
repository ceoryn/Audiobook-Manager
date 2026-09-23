from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

NOISE = frozenset({
    "a", "an", "and", "adaptation", "adventure", "audio", "audiobook", "book",
    "by", "complete", "credits", "disc", "disk", "dramatized", "edition", "end",
    "full", "light", "litrpg", "novel", "of", "opening", "series", "the",
    "tmpfiles", "unabridged", "vol", "volume",
})


def _words(value: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
        if word not in NOISE
    }


def _similarity(left: str, right: str) -> float:
    a, b = " ".join(sorted(_words(left))), " ".join(sorted(_words(right)))
    if not a or not b:
        return 0.0
    overlap = len(_words(left) & _words(right)) / max(1, len(_words(left) | _words(right)))
    return 0.65 * overlap + 0.35 * SequenceMatcher(None, a, b).ratio()


def _title_variants(value: str) -> list[str]:
    options = [value]
    options.extend(re.split(r"\s*[:|]\s*|\s+[-–—]\s+", value))
    without_parenthetical = re.sub(r"\s*\([^)]*\)\s*$", "", value)
    if without_parenthetical != value:
        options.append(without_parenthetical)
    return [option for option in options if option.strip()]


def _title_similarity(left: str, right: str) -> float:
    """Compare catalog titles while tolerating provider-added series subtitles."""
    left_variants = _title_variants(left)
    right_variants = _title_variants(right)
    if not left_variants or not right_variants:
        return 0.0
    return max(_similarity(a, b) for a in left_variants for b in right_variants)


def _exact_title(left: str, right: str) -> bool:
    normalize = lambda value: " ".join(
        word for word in re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
        if word not in NOISE
    )
    return bool(normalize(left)) and normalize(left) == normalize(right)


def _exact_title_variant(left: str, right: str) -> bool:
    return any(_exact_title(a, b) for a in _title_variants(left) for b in _title_variants(right))


def _identity_key(candidate: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    title = str(candidate.get("title", ""))
    title = re.sub(r"\s*\([^)]*\)\s*$", "", title)
    authors = " ".join(map(str, candidate.get("authors") or []))
    return tuple(sorted(_words(title))), tuple(sorted(_words(authors)))


def _collapse_equivalent_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Treat provider duplicate works with the same title/author identity as one result."""
    unique: dict[tuple[tuple[str, ...], tuple[str, ...]], dict[str, Any]] = {}
    for candidate in candidates:
        key = _identity_key(candidate)
        current = unique.get(key)
        if current is None or sum(bool(candidate.get(field)) for field in ("cover_url", "description", "isbn", "publisher")) > sum(
            bool(current.get(field)) for field in ("cover_url", "description", "isbn", "publisher")
        ):
            unique[key] = candidate
    return list(unique.values())


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: dict[str, Any]
    score: int
    reasons: tuple[str, ...]


def score_candidate(hint: dict[str, Any], candidate: dict[str, Any]) -> ScoredCandidate:
    reasons: list[str] = []
    title = _title_similarity(str(hint.get("title", "")), str(candidate.get("title", "")))
    score = round(title * 65)
    reasons.append(f"title similarity {title:.0%}")
    hint_authors = " ".join(hint.get("authors") or [])
    candidate_authors = " ".join(candidate.get("authors") or [])
    if hint_authors and candidate_authors:
        author = _similarity(hint_authors, candidate_authors)
        score += round(author * 25)
        reasons.append(f"author similarity {author:.0%}")
    elif not hint_authors and candidate_authors and title >= 0.95 and _exact_title_variant(
            str(hint.get("title", "")), str(candidate.get("title", ""))):
        score += 20
        reasons.append("unique exact title compensates for missing local author")
    if hint.get("isbn") and hint.get("isbn") == candidate.get("isbn"):
        score += 20
        reasons.append("ISBN exact match")
    series = str(hint.get("series", ""))
    if series and series.casefold() in str(candidate.get("title", "")).casefold():
        score += 5
        reasons.append("series appears in title")
    hint_runtime = hint.get("runtime_minutes")
    candidate_runtime = candidate.get("runtime_minutes")
    if isinstance(hint_runtime, (int, float)) and isinstance(candidate_runtime, (int, float)) and hint_runtime > 0:
        difference = abs(float(hint_runtime) - float(candidate_runtime)) / float(hint_runtime)
        if difference <= 0.01:
            score += 10
            reasons.append(f"runtime difference {difference:.1%}")
        elif difference > 0.05:
            score -= 20
            reasons.append(f"runtime conflict {difference:.1%}")
    hint_narrators = " ".join(hint.get("narrators") or [])
    candidate_narrators = " ".join(candidate.get("narrators") or [])
    if hint_narrators and candidate_narrators:
        narrator = _similarity(hint_narrators, candidate_narrators)
        score += round(narrator * 10)
        reasons.append(f"narrator similarity {narrator:.0%}")
    return ScoredCandidate(candidate, max(0, min(score, 100)), tuple(reasons))


def choose_automatic(hint: dict[str, Any], candidates: list[dict[str, Any]], *, threshold: int = 80,
                     ambiguity_margin: int = 5) -> tuple[ScoredCandidate | None, list[ScoredCandidate]]:
    ranked = sorted((score_candidate(hint, item) for item in _collapse_equivalent_candidates(candidates)),
                    key=lambda item: item.score, reverse=True)
    if not ranked or ranked[0].score < threshold:
        return None, ranked
    if len(ranked) > 1 and ranked[0].score - ranked[1].score < ambiguity_margin:
        return None, ranked
    return ranked[0], ranked
