from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import BookGroup, ProbeResult, ScannedFile

NUMBER_TOKEN = re.compile(r"\d+")
WORD_TOKEN = re.compile(r"[a-z0-9]+")
TEMP_SUFFIX = re.compile(r"(?:[-_ ]tmpfiles|[-_ ]temp|[-_ ]conversion)$", re.IGNORECASE)
NAME_STOPWORDS = frozenset({"a", "an", "and", "audio", "audiobook", "book", "by", "of", "the"})
SEQUENCE_NAME = re.compile(
    r"^(?:cd|disc|disk|part|track|chapter|ch)?[ _.-]*\d+(?:[ _.-]*(?:of)[ _.-]*\d+)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Evidence:
    kind: str
    points: float
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "points": round(self.points, 2), "explanation": self.explanation}


@dataclass(frozen=True)
class LineageRelationship:
    relationship: str
    source_files: tuple[str, ...]
    target_file: str | None
    confidence: float
    confidence_band: str
    evidence: tuple[Evidence, ...]
    conflicts: tuple[str, ...]
    cleanup_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "relationship": self.relationship,
            "source_files": list(self.source_files),
            "target_file": self.target_file,
            "confidence": round(self.confidence, 2),
            "confidence_band": self.confidence_band,
            "evidence": [item.to_dict() for item in self.evidence],
            "conflicts": list(self.conflicts),
            "cleanup_eligible": False,
        }


@dataclass(frozen=True)
class GroupAnalysis:
    group_key: str
    relationships: tuple[LineageRelationship, ...]
    review_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_key": self.group_key,
            "relationships": [item.to_dict() for item in self.relationships],
            "requires_review": bool(self.review_reasons),
            "review_reasons": list(self.review_reasons),
        }


def confidence_band(score: float) -> str:
    if score >= 95:
        return "high"
    if score >= 80:
        return "medium"
    if score >= 50:
        return "low"
    return "very_low"


def _duration(item: ScannedFile) -> float | None:
    return item.probe.duration_seconds if item.probe else None


def _duration_evidence(target: float, component_total: float) -> tuple[Evidence, str | None]:
    difference = abs(target - component_total)
    denominator = max(target, component_total, 1.0)
    ratio = difference / denominator
    if difference <= 1.0:
        points = 50.0
    elif difference <= 5.0:
        points = 47.0
    elif difference <= 30.0:
        points = 42.0
    elif ratio <= 0.005:
        points = 36.0
    elif ratio <= 0.01:
        points = 28.0
    elif ratio <= 0.03:
        points = 16.0
    else:
        points = 0.0
    conflict = None if ratio <= 0.03 else f"duration difference is {difference:.2f}s ({ratio:.1%})"
    return (
        Evidence(
            "duration_total",
            points,
            f"target duration differs from component total by {difference:.2f}s ({ratio:.3%})",
        ),
        conflict,
    )


def _tag_values(items: Iterable[ScannedFile], key: str) -> set[str]:
    return {
        value.strip().casefold()
        for item in items
        if item.probe and (value := item.probe.tags.get(key)) and value.strip()
    }


def _metadata_evidence(target: ScannedFile, components: tuple[ScannedFile, ...]) -> tuple[Evidence, ...]:
    evidence: list[Evidence] = []
    for key, label in (("album", "album"), ("artist", "artist"), ("album_artist", "album artist")):
        target_values = _tag_values((target,), key)
        component_values = _tag_values(components, key)
        if target_values and component_values and target_values & component_values:
            evidence.append(Evidence(f"metadata_{key}", 4.0, f"{label} metadata agrees"))
    return tuple(evidence)


def _is_sequence_like(path: Path) -> bool:
    stem = path.stem.strip()
    return bool(SEQUENCE_NAME.match(stem) or re.match(r"^\d+[ _.-]", stem))


def _ordered(items: Iterable[ScannedFile]) -> tuple[ScannedFile, ...]:
    def key(item: ScannedFile) -> tuple[Any, ...]:
        parts = NUMBER_TOKEN.split(str(item.relative_path).casefold())
        numbers = tuple(int(value) for value in NUMBER_TOKEN.findall(str(item.relative_path)))
        return (*parts, numbers, str(item.relative_path).casefold())

    return tuple(sorted(items, key=key))


def _name_tokens(value: str) -> set[str]:
    cleaned = TEMP_SUFFIX.sub("", value)
    return {
        token
        for token in WORD_TOKEN.findall(cleaned.casefold())
        if token not in NAME_STOPWORDS and not token.isdigit()
    }


def _family_match(left: str, right: str) -> float:
    left_tokens = _name_tokens(Path(left).name)
    right_tokens = _name_tokens(Path(right).name)
    if min(len(left_tokens), len(right_tokens)) < 2:
        return 0.0
    overlap = left_tokens & right_tokens
    containment = len(overlap) / min(len(left_tokens), len(right_tokens))
    jaccard = len(overlap) / len(left_tokens | right_tokens)
    return 0.7 * containment + 0.3 * jaccard


def build_family_groups(groups: list[BookGroup]) -> list[BookGroup]:
    """Attach strong name-matched source groups to legacy temporary conversion groups.

    The merge is analysis-only. Original paths and scan groups remain unchanged.
    """
    expanded: list[BookGroup] = []
    for group in groups:
        if not TEMP_SUFFIX.search(group.key):
            expanded.append(group)
            continue
        candidates = [
            (candidate, _family_match(group.key, candidate.key))
            for candidate in groups
            if candidate is not group and not TEMP_SUFFIX.search(candidate.key)
        ]
        strong = [(candidate, score) for candidate, score in candidates if score >= 0.84]
        if not strong:
            expanded.append(group)
            continue
        best_score = max(score for _, score in strong)
        matches = [candidate for candidate, score in strong if score >= best_score - 0.03]
        members_by_path = {str(item.relative_path): item for item in group.files}
        for candidate in matches:
            members_by_path.update((str(item.relative_path), item) for item in candidate.files)
        members = tuple(sorted(members_by_path.values(), key=lambda item: str(item.relative_path).casefold()))
        reasons = (*group.reasons, f"analysis-only name match joined {len(matches)} probable source group(s)")
        expanded.append(BookGroup(f"family:{TEMP_SUFFIX.sub('', group.key)}", members, reasons))
    return expanded


def _component_relationship(
    components: tuple[ScannedFile, ...], target: ScannedFile, relationship: str
) -> LineageRelationship | None:
    target_duration = _duration(target)
    component_durations = [_duration(item) for item in components]
    if target_duration is None or len(components) < 2 or any(value is None for value in component_durations):
        return None
    total = sum(value for value in component_durations if value is not None)
    duration_evidence, conflict = _duration_evidence(target_duration, total)
    evidence = [duration_evidence, Evidence("directory_context", 5.0, "files share a conservative scan group")]
    conflicts: list[str] = [conflict] if conflict else []

    chapter_count = len(target.probe.chapters) if target.probe else 0
    if chapter_count == len(components):
        evidence.append(
            Evidence("chapter_count", 25.0, f"target has {chapter_count} chapters for {len(components)} components")
        )
    elif chapter_count:
        conflicts.append(f"target has {chapter_count} chapters but there are {len(components)} components")
    else:
        conflicts.append("target has no embedded chapters to align with components")

    if not _is_sequence_like(target.relative_path):
        evidence.append(Evidence("filename_role", 5.0, "target filename is not a simple sequence member"))
    if sum(_is_sequence_like(item.relative_path) for item in components) / len(components) >= 0.75:
        evidence.append(Evidence("component_sequence", 7.0, "most component filenames form a sequence"))
    legacy_markers = sum(
        any(marker in item.relative_path.stem.casefold() for marker in ("finished", "converting"))
        for item in components
    )
    if TEMP_SUFFIX.search(str(target.relative_path.parent)) and legacy_markers / len(components) >= 0.75:
        evidence.append(
            Evidence(
                "legacy_conversion_layout",
                17.0,
                "target is in a tmpfiles directory and most components carry conversion-stage markers",
            )
        )
    evidence.extend(_metadata_evidence(target, components))

    score = max(0.0, min(100.0, sum(item.points for item in evidence) - 4.0 * len(conflicts)))
    if score < 50:
        return None
    return LineageRelationship(
        relationship,
        tuple(str(item.relative_path) for item in components),
        str(target.relative_path),
        score,
        confidence_band(score),
        tuple(evidence),
        tuple(conflicts),
    )


def _parallel_conversion(mp3s: tuple[ScannedFile, ...], m4bs: tuple[ScannedFile, ...]) -> LineageRelationship | None:
    if len(mp3s) < 2 or len(mp3s) != len(m4bs):
        return None
    pairs = list(zip(_ordered(mp3s), _ordered(m4bs), strict=True))
    if any(_duration(left) is None or _duration(right) is None for left, right in pairs):
        return None
    differences = [abs((_duration(left) or 0) - (_duration(right) or 0)) for left, right in pairs]
    aligned = sum(difference <= 2.0 for difference in differences)
    ratio = aligned / len(pairs)
    score = 55.0 * ratio + 25.0
    evidence = [
        Evidence("one_to_one_count", 25.0, f"{len(mp3s)} MP3 files and {len(m4bs)} M4B files"),
        Evidence("duration_sequence", 55.0 * ratio, f"{aligned}/{len(pairs)} ordered durations align within 2 seconds"),
    ]
    conflicts: list[str] = []
    if ratio < 0.8:
        conflicts.append("fewer than 80% of component durations align")
    if score < 50:
        return None
    return LineageRelationship(
        "parallel_intermediate_conversion",
        tuple(str(item.relative_path) for item in _ordered(mp3s)),
        None,
        score,
        confidence_band(score),
        tuple(evidence),
        tuple(conflicts),
    )


def analyze_group(group: BookGroup) -> GroupAnalysis:
    valid = tuple(item for item in group.files if item.probe and item.probe.duration_seconds)
    mp3s = tuple(item for item in valid if item.path.suffix.lower() == ".mp3")
    m4bs = tuple(item for item in valid if item.path.suffix.lower() in {".m4a", ".m4b"})
    relationships: list[LineageRelationship] = []

    intermediate_m4bs = tuple(item for item in m4bs if _is_sequence_like(item.relative_path))
    parallel = _parallel_conversion(mp3s, intermediate_m4bs)
    if parallel:
        relationships.append(parallel)

    for target in m4bs:
        other_m4bs = tuple(item for item in m4bs if item is not target)
        for components, relationship in (
            (mp3s, "combined_from_mp3_components"),
            (other_m4bs, "combined_from_m4b_components"),
        ):
            candidate = _component_relationship(components, target, relationship)
            if candidate:
                relationships.append(candidate)

    # Retain only the strongest claim for each target/type. This avoids a noisy report in large folders.
    unique: dict[tuple[str, str | None], LineageRelationship] = {}
    for item in relationships:
        key = (item.relationship, item.target_file)
        if key not in unique or item.confidence > unique[key].confidence:
            unique[key] = item
    ordered = tuple(sorted(unique.values(), key=lambda item: (-item.confidence, item.relationship, item.target_file or "")))

    review: list[str] = []
    if any(item.error for item in group.files):
        review.append("one or more files could not be probed")
    if len(valid) > 1 and not ordered:
        review.append("multiple valid media files have no supported lineage conclusion")
    if any(item.confidence < 95 or item.conflicts for item in ordered):
        review.append("one or more relationships are uncertain or contain conflicting evidence")
    if len({item.target_file for item in ordered if item.target_file and item.confidence >= 80}) > 1:
        review.append("multiple plausible final targets require comparison")
    return GroupAnalysis(group.key, ordered, tuple(dict.fromkeys(review)))


def scanned_file_from_dict(root: Path, value: dict[str, Any]) -> ScannedFile:
    relative = Path(value["relative_path"])
    probe_value = value.get("probe")
    return ScannedFile(
        path=root / relative,
        relative_path=relative,
        size=int(value.get("size", 0)),
        modified_ns=int(value.get("modified_ns", 0)),
        probe=ProbeResult.from_dict(probe_value) if probe_value else None,
        error=value.get("error"),
        cached=bool(value.get("cached", False)),
    )


def groups_from_scan_report(report: dict[str, Any]) -> list[BookGroup]:
    root = Path(report["library_root"])
    files = {value["relative_path"]: scanned_file_from_dict(root, value) for value in report["files"]}
    groups: list[BookGroup] = []
    for value in report["groups"]:
        members = tuple(files[path] for path in value["files"] if path in files)
        groups.append(BookGroup(value["key"], members, tuple(value.get("reasons", ()))))
    return groups


def analyze_scan_report(report: dict[str, Any]) -> dict[str, Any]:
    analyses = [analyze_group(group) for group in build_family_groups(groups_from_scan_report(report))]
    relationships = [relation for group in analyses for relation in group.relationships]
    bands = {band: sum(item.confidence_band == band for item in relationships) for band in ("high", "medium", "low", "very_low")}
    return {
        "schema_version": 1,
        "source_scan_schema_version": report.get("schema_version"),
        "library_root": report["library_root"],
        "safety": {
            "read_only_analysis": True,
            "cleanup_recommendations_generated": False,
            "cleanup_eligible_relationships": 0,
        },
        "summary": {
            "groups_analyzed": len(analyses),
            "relationships": len(relationships),
            "confidence_bands": bands,
            "groups_requiring_review": sum(bool(group.review_reasons) for group in analyses),
        },
        "groups": [group.to_dict() for group in analyses if group.relationships or group.review_reasons],
    }
