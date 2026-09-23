from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .models import BookGroup, ScannedFile
from .probe import is_aac_container


DISC_DIRECTORY = re.compile(
    r"^(?:(?:cd|disc|disk|part)[ _.-]*\d+|\(?\d+\s*(?:of|/)\s*\d+\)?)$",
    re.I,
)
VARIANT_DIRECTORY = re.compile(r"^alt(?:\.|ernate|ernative)?\s+versions?$", re.I)
WHOLE_BOOK_MARKER = re.compile(
    r"(?:\b(?:complete|full(?:\s+audiobook)?|unabridged)\b|\d+complete\b)", re.I
)
EXPLICIT_PART = re.compile(r"(?<!\d)(\d{1,3})\s*(?:of|/)\s*(\d{1,3})(?!\d)", re.I)
UNDERSCORE_PART = re.compile(r"_(\d{1,3})_(\d{1,3})$")


@dataclass(frozen=True)
class Reconciliation:
    state: str
    chosen_files: tuple[str, ...]
    alternate_files: tuple[str, ...]
    confidence: float
    evidence: tuple[str, ...]
    conflicts: tuple[str, ...]
    problem_files: tuple[str, ...] = ()


def _duration(item: ScannedFile) -> float:
    return item.probe.duration_seconds or 0 if item.probe else 0


def _container(item: ScannedFile) -> bool:
    return item.path.suffix.casefold() in {".m4a", ".m4b", ".mp4"} or bool(
        item.probe and is_aac_container(item.probe))


def _close(left: float, right: float, tolerance: float = 0.01) -> bool:
    return left > 0 and right > 0 and abs(left - right) / max(left, right) <= tolerance


def _representation_root(item: ScannedFile) -> Path:
    parent = item.relative_path.parent
    if _disc_parent(item):
        parent = parent.parent
    if VARIANT_DIRECTORY.match(parent.name):
        parent = parent.parent
    return parent


def _disc_parent(item: ScannedFile) -> bool:
    """Numeric disc folders count as packaging for short tracks in one book group."""
    parent = item.relative_path.parent
    return bool(DISC_DIRECTORY.fullmatch(parent.name) or (
        re.fullmatch(r"\d{1,3}", parent.name)
        and len(item.relative_path.parts) >= 3
        and 0 < _duration(item) < 2 * 60 * 60
    ))


def _legacy_member(item: ScannedFile) -> bool:
    return bool(item.relative_path.stem.lower().startswith("tmp_") or any(
        re.search(r"(?:^|[-_ ])tmpfiles?$", part, re.I)
        for part in item.relative_path.parts[:-1]
    ))


def _corroborated_original_whole(valid: tuple[ScannedFile, ...]) -> ScannedFile | None:
    """Do not prefer a legacy aggregate of two original whole-book editions."""
    originals = tuple(item for item in valid if not _legacy_member(item))
    if len(originals) != 2 or len(originals) == len(valid):
        return None
    containers = [item for item in originals if _container(item)]
    mp3s = [item for item in originals if item.path.suffix.lower() == ".mp3"]
    if len(containers) != 1 or len(mp3s) != 1:
        return None
    container = containers[0]
    if (container.probe and len(container.probe.chapters) >= 2
            and _duration(container) >= 2 * 60 * 60
            and _close(_duration(container), _duration(mp3s[0]), tolerance=0.005)):
        return container
    return None


def _whole_mp3_candidate(item: ScannedFile) -> bool:
    return item.path.suffix.casefold() == ".mp3" and (
        bool(WHOLE_BOOK_MARKER.search(item.relative_path.stem))
        or any(VARIANT_DIRECTORY.match(part) for part in item.relative_path.parts[:-1])
        or _duration(item) >= 2 * 60 * 60
    )


def _explicit_whole_mp3(item: ScannedFile) -> bool:
    return item.path.suffix.casefold() == ".mp3" and (
        bool(WHOLE_BOOK_MARKER.search(item.relative_path.stem))
        or any(VARIANT_DIRECTORY.match(part) for part in item.relative_path.parts[:-1])
    )


def _component_sets(
    target: ScannedFile, valid: tuple[ScannedFile, ...]
) -> list[tuple[ScannedFile, ...]]:
    roots: dict[Path, list[ScannedFile]] = {}
    for item in valid:
        if item is target:
            continue
        roots.setdefault(_representation_root(item), []).append(item)
    sets = [tuple(items) for items in roots.values() if len(items) >= 2]
    same_root = tuple(
        item for item in valid
        if item is not target and _representation_root(item) == _representation_root(target)
    )
    if len(same_root) >= 2:
        sets.append(same_root)
    mp3s = tuple(item for item in valid if item is not target and item.path.suffix.casefold() == ".mp3")
    m4bs = tuple(
        item for item in valid
        if item is not target and _container(item)
    )
    if len(mp3s) >= 2:
        sets.append(mp3s)
    if len(m4bs) >= 2:
        sets.append(m4bs)
    unique: dict[tuple[str, ...], tuple[ScannedFile, ...]] = {}
    for items in sets:
        key = tuple(sorted(str(item.relative_path) for item in items))
        unique[key] = items
    return list(unique.values())


def _equivalent_component_representations(
    valid: tuple[ScannedFile, ...],
) -> tuple[tuple[ScannedFile, ...], tuple[ScannedFile, ...]] | None:
    """Choose one of multiple complete component trees with equal runtimes."""
    roots: dict[Path, list[ScannedFile]] = {}
    for item in valid:
        roots.setdefault(_representation_root(item), []).append(item)
    representations = [tuple(items) for items in roots.values() if len(items) >= 2]
    if len(representations) < 2 or sum(map(len, representations)) != len(valid):
        return None
    totals = [sum(map(_duration, items)) for items in representations]
    if not all(_close(totals[0], total) for total in totals[1:]):
        return None
    chosen = max(
        representations,
        key=lambda items: (sum(item.size for item in items), len(items)),
    )
    alternates = tuple(item for items in representations if items is not chosen for item in items)
    return chosen, alternates


def _multidisc_representation(
    valid: tuple[ScannedFile, ...],
) -> tuple[tuple[ScannedFile, ...], float] | None:
    """Find an explicitly segmented representation spanning at least two discs/parts."""
    roots: dict[Path, list[ScannedFile]] = {}
    segments: dict[Path, set[str]] = {}
    for item in valid:
        parent = item.relative_path.parent
        if not _disc_parent(item):
            continue
        root = parent.parent
        roots.setdefault(root, []).append(item)
        segments.setdefault(root, set()).add(parent.name.casefold())
    candidates = [
        (tuple(items), sum(map(_duration, items)))
        for root, items in roots.items()
        if len(segments[root]) >= 2
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda pair: (pair[1], sum(item.size for item in pair[0])), reverse=True
    )
    if len(candidates) > 1 and not _close(candidates[0][1], candidates[1][1]):
        return None
    return candidates[0]


def _corroborated_representation(
    valid: tuple[ScannedFile, ...],
) -> tuple[tuple[ScannedFile, ...], tuple[ScannedFile, ...]] | None:
    """Choose a highest-coverage representation corroborated by another tree."""
    roots: dict[Path, list[ScannedFile]] = {}
    for item in valid:
        roots.setdefault(_representation_root(item), []).append(item)
    representations = [
        (tuple(items), sum(map(_duration, items))) for items in roots.values()
    ]
    if len(representations) < 2:
        return None
    maximum = max(total for _, total in representations)
    clusters: list[list[tuple[tuple[ScannedFile, ...], float]]] = []
    for representation in representations:
        cluster = [
            other for other in representations
            if _close(representation[1], other[1])
        ]
        if len(cluster) >= 2 and max(total for _, total in cluster) >= maximum * 0.98:
            clusters.append(cluster)
    if not clusters:
        return None
    cluster = max(clusters, key=lambda items: max(total for _, total in items))

    def quality(pair: tuple[tuple[ScannedFile, ...], float]) -> tuple[int, int, float]:
        items, total = pair
        containers = sum(_container(item) for item in items)
        return (containers == len(items), sum(item.size for item in items), total)

    chosen, _ = max(cluster, key=quality)
    alternates = tuple(item for item in valid if item not in chosen)
    return chosen, alternates


def _source_with_partial_legacy_conversion(
    valid: tuple[ScannedFile, ...],
) -> tuple[tuple[ScannedFile, ...], tuple[ScannedFile, ...]] | None:
    """Recover a complete MP3 tree when a legacy M4B conversion stopped partway."""
    mp3_roots: dict[Path, list[ScannedFile]] = {}
    containers: list[ScannedFile] = []
    for item in valid:
        if item.path.suffix.casefold() == ".mp3":
            mp3_roots.setdefault(_representation_root(item), []).append(item)
        elif _container(item):
            containers.append(item)
    if len(mp3_roots) != 1 or not containers:
        return None
    source = tuple(next(iter(mp3_roots.values())))
    source_duration = sum(map(_duration, source))
    converted_duration = sum(map(_duration, containers))
    if (
        len(source) < 2
        or source_duration <= 0
        or converted_duration >= source_duration * 0.98
        or not all(any(re.search(r"(?:^|[-_ ])tmpfiles?$", part, flags=re.I)
                        for part in item.relative_path.parts[:-1])
                   for item in containers)
    ):
        return None
    return source, tuple(containers)


def _explicit_sequence_conflict(items: tuple[ScannedFile, ...]) -> str | None:
    """Reject a sparse explicitly numbered edition before it becomes a partial M4B."""
    numbered: list[tuple[ScannedFile, int, int]] = []
    for item in items:
        stem = item.relative_path.stem
        explicit = EXPLICIT_PART.search(stem)
        underscore = (UNDERSCORE_PART.search(stem)
                      if len(items) >= 2 and item.path.suffix.casefold() == ".mp3"
                      else None)
        match = explicit or underscore
        if not match:
            return None  # An unnumbered whole-book alternative may exist.
        number, total = map(int, match.groups())
        if not explicit and number > total:
            # ``Book_6_01`` often means series book 6, file 1; the trailing
            # pair is not an "x of y" part declaration.
            return None
        if not (1 <= number <= total <= 500):
            return "invalid explicit part numbering in source files"
        numbered.append((item, number, total))
    if not numbered:
        return None
    if (len({number for _, number, _ in numbered}) == 1
            and len({total for _, _, total in numbered}) > 1):
        # ``Book_1_02``, ``Book_1_03`` can mean series book 1, files 2/3.
        return None
    totals = {total for _, _, total in numbered}
    if len(totals) != 1:
        return "conflicting explicit part totals in source files"
    total = next(iter(totals))
    present = {number for _, number, _ in numbered}
    readable = {number for item, number, _ in numbered if item.probe and _duration(item) > 0}
    if len(present) != len(numbered):
        return "duplicate explicit part numbers need review"
    if readable != set(range(1, total + 1)):
        missing = len(set(range(1, total + 1)) - present)
        unreadable = len(present - readable)
        return (f"explicit {total}-part edition is incomplete: {len(present)} present, "
                f"{missing} missing, {unreadable} unreadable")
    return None


def reconcile_group(group: BookGroup) -> Reconciliation:
    valid = tuple(item for item in group.files if item.probe and _duration(item) > 0)
    broken = tuple(item for item in group.files if item not in valid)
    if "directory and filename identities conflict" in group.reasons:
        return Reconciliation("quarantine", (), tuple(str(x.relative_path) for x in group.files), 0.2,
                              (), ("directory and filename identify different books",))
    if not valid:
        return Reconciliation(
            "quarantine", (), (), 0, (), ("no readable audio representation",),
            tuple(str(item.relative_path) for item in broken),
        )
    m4bs = tuple(item for item in valid if _container(item))
    mp3s = tuple(item for item in valid if item.path.suffix.casefold() == ".mp3")
    evidence: list[str] = []
    conflicts = [f"{len(broken)} unreadable file(s)"] if broken else []
    multidisc = _multidisc_representation(valid)

    original = _corroborated_original_whole(valid)
    if original is not None:
        return Reconciliation(
            "complete_m4b", (str(original.relative_path),),
            tuple(str(item.relative_path) for item in valid if item is not original),
            0.95,
            ("original chaptered container agrees with original whole MP3; legacy aggregates retained as alternates",),
            tuple(conflicts), tuple(str(item.relative_path) for item in broken),
        )

    # A single container matching the component total is the strongest complete-book signal.
    candidates: list[tuple[ScannedFile, str]] = []
    possible_targets = (*m4bs, *(item for item in mp3s if _whole_mp3_candidate(item)))
    for target in possible_targets:
        matches = [
            components
            for components in _component_sets(target, valid)
            if _close(_duration(target), sum(map(_duration, components)))
        ]
        if matches:
            kinds = ", ".join(sorted({item.path.suffix.casefold() for item in matches[0]}))
            candidates.append(
                (target, f"whole-file duration matches a {len(matches[0])}-component set ({kinds})")
            )
    if candidates:
        candidates.sort(key=lambda pair: (_duration(pair[0]), pair[0].size), reverse=True)
        if multidisc and multidisc[1] > _duration(candidates[0][0]) * 1.1:
            chosen_set, _ = multidisc
            evidence += [
                "explicit disc/part tree spans the complete segmented edition",
                "shorter matching targets retained as partial legacy representations",
            ]
            alternates = tuple(str(item.relative_path) for item in valid if item not in chosen_set)
            return Reconciliation(
                "combine_components",
                tuple(str(item.relative_path) for item in chosen_set),
                alternates,
                0.92,
                tuple(evidence),
                tuple(conflicts),
                tuple(str(item.relative_path) for item in broken),
            )
        chosen, reason = candidates[0]
        if _legacy_member(chosen):
            originals = tuple(item for item in valid
                              if not _legacy_member(item) and item.path.suffix.lower() == ".mp3")
            numbers = [re.match(r"^(\d+)[ ._-]", item.relative_path.name)
                       for item in originals]
            if (len(originals) >= 2 and all(numbers)
                    and len({_representation_root(item) for item in originals}) == 1
                    and sorted(int(match.group(1)) for match in numbers if match)
                    == list(range(1, len(originals) + 1))
                    and _close(sum(map(_duration, originals)), _duration(chosen), 0.005)):
                ordered = tuple(sorted(originals, key=lambda item: int(
                    re.match(r"^(\d+)", item.relative_path.name).group(1))))
                return Reconciliation(
                    "combine_components", tuple(str(item.relative_path) for item in ordered),
                    tuple(str(item.relative_path) for item in valid if item not in originals),
                    0.95,
                    ("complete numbered original track sequence agrees with legacy aggregate duration; original ordering and chapter boundaries preserved",),
                    tuple(conflicts), tuple(str(item.relative_path) for item in broken),
                )
        evidence += [reason, "complete container preferred over source/intermediate components"]
        alternates = tuple(str(x.relative_path) for x in valid if x is not chosen)
        confidence = 0.98 if len(candidates) == 1 else 0.9
        if len(candidates) > 1:
            conflicts.append("multiple complete-container candidates")
        state = "complete_m4b" if _container(chosen) else "convert_single"
        return Reconciliation(state, (str(chosen.relative_path),), alternates,
                              confidence, tuple(evidence), tuple(conflicts),
                              tuple(str(item.relative_path) for item in broken))

    sequence_conflict = _explicit_sequence_conflict(group.files)
    if sequence_conflict:
        return Reconciliation(
            "quarantine", (), tuple(str(item.relative_path) for item in valid),
            0.1, (), (sequence_conflict,),
            tuple(str(item.relative_path) for item in broken),
        )

    if multidisc:
        chosen_set, _ = multidisc
        evidence.append("explicit disc/part tree spans the complete segmented edition")
        alternates = tuple(str(item.relative_path) for item in valid if item not in chosen_set)
        return Reconciliation(
            "combine_components",
            tuple(str(item.relative_path) for item in chosen_set),
            alternates,
            0.9,
            tuple(evidence),
            tuple(conflicts),
            tuple(str(item.relative_path) for item in broken),
        )

    partial_legacy = _source_with_partial_legacy_conversion(valid)
    if partial_legacy:
        chosen_set, alternate_set = partial_legacy
        evidence.append("complete MP3 tree retained over an incomplete legacy conversion")
        return Reconciliation(
            "combine_components",
            tuple(str(item.relative_path) for item in chosen_set),
            tuple(str(item.relative_path) for item in alternate_set),
            0.9,
            tuple(evidence),
            tuple(conflicts),
            tuple(str(item.relative_path) for item in broken),
        )

    corroborated = _corroborated_representation(valid)
    if corroborated:
        chosen_set, alternate_set = corroborated
        evidence.append(
            "two complete trees corroborate the selected runtime as alternate complete representations"
        )
        state = (
            "complete_m4b"
            if len(chosen_set) == 1
            and _container(chosen_set[0])
            else "combine_components"
        )
        return Reconciliation(
            state,
            tuple(str(item.relative_path) for item in chosen_set),
            tuple(str(item.relative_path) for item in alternate_set),
            0.93,
            tuple(evidence),
            tuple(conflicts),
            tuple(str(item.relative_path) for item in broken),
        )

    equivalent_sets = _equivalent_component_representations(valid)
    if equivalent_sets:
        chosen_set, alternate_set = equivalent_sets
        evidence.append(
            "equal-duration component trees are alternate complete representations"
        )
        return Reconciliation(
            "combine_components",
            tuple(str(item.relative_path) for item in chosen_set),
            tuple(str(item.relative_path) for item in alternate_set),
            0.94,
            tuple(evidence),
            tuple(conflicts),
            tuple(str(item.relative_path) for item in broken),
        )

    # Similar-duration M4A/M4B files are alternate complete representations, not chapters.
    if len(m4bs) > 1 and all(_close(_duration(m4bs[0]), _duration(item)) for item in m4bs[1:]):
        chosen = max(m4bs, key=lambda item: (_duration(item), item.size))
        # A same-duration MP3 can be a complete source representation too. Keep
        # every other readable file visible as an alternate instead of silently
        # dropping non-container members from the audit trail.
        alternates = tuple(str(item.relative_path) for item in valid if item is not chosen)
        evidence.append("similar-duration M4A/M4B files are equivalent complete representations")
        return Reconciliation("complete_m4b", (str(chosen.relative_path),), alternates,
                              0.92, tuple(evidence), tuple(conflicts),
                              tuple(str(item.relative_path) for item in broken))

    if len(valid) == 1:
        chosen = valid[0]
        state = "complete_m4b" if chosen in m4bs else "convert_single"
        evidence.append("only readable representation")
        return Reconciliation(state, (str(chosen.relative_path),), (), 0.9,
                              tuple(evidence), tuple(conflicts),
                              tuple(str(item.relative_path) for item in broken))
    if mp3s and not m4bs:
        explicit_wholes = tuple(item for item in mp3s if _explicit_whole_mp3(item))
        components = tuple(item for item in mp3s if item not in explicit_wholes)
        if (explicit_wholes and components
                and all(_duration(item) < 2 * 60 * 60 for item in components)):
            component_duration = sum(map(_duration, components))
            if not any(_close(_duration(target), component_duration) for target in explicit_wholes):
                evidence.append(
                    "longer ordered components retained over a duration-conflicting alternate MP3"
                )
                conflicts.append("alternate complete MP3 duration differs from component tree")
                return Reconciliation(
                    "combine_components",
                    tuple(str(item.relative_path) for item in components),
                    tuple(str(item.relative_path) for item in explicit_wholes),
                    0.76,
                    tuple(evidence),
                    tuple(conflicts),
                    tuple(str(item.relative_path) for item in broken),
                )
        evidence.append("ordered MP3 component set with no competing M4B")
        return Reconciliation("combine_components", tuple(str(x.relative_path) for x in mp3s), (),
                              0.86, tuple(evidence), tuple(conflicts),
                              tuple(str(item.relative_path) for item in broken))
    if m4bs and not mp3s:
        evidence.append("multiple M4B containers without a provable complete target")
        return Reconciliation("combine_components", tuple(str(x.relative_path) for x in m4bs), (),
                              0.8, tuple(evidence), tuple(conflicts),
                              tuple(str(item.relative_path) for item in broken))
    return Reconciliation("quarantine", (), tuple(str(x.relative_path) for x in valid), 0.35,
                          tuple(evidence), tuple(conflicts + ["competing representations do not reconcile by duration"]),
                          tuple(str(item.relative_path) for item in broken))
