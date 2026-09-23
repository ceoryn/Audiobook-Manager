from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from .grouping import DISC_DIRECTORY
from .models import BookGroup, ScannedFile

SPACE = re.compile(r"[^a-z0-9]+")
TRACK_PREFIX = re.compile(
    r"^(?:(?:cd|disc|disk|part|chapter|track)\s*)?"
    r"\d+(?:\.\d+)?[a-z]?(?:\s*[-_.]\s*|\s+)",
    re.I,
)
ARTIFACT = re.compile(
    r"(?:[-_ ]?(?:tmpfiles?|temp|converted?|finished|unabridged|normal\s+audio|audio\s*pops?|\d+k))+$",
    re.I,
)
PAREN_ARTIFACT_SUFFIX = re.compile(
    r"\s*[([]\s*(?:tmpfiles?|temp|converted?|finished|unabridged|normal\s+audio|audio\s*pops?|\d+k)\s*[)\]][,;]?\s*$",
    re.I,
)
DISC_SUFFIX = re.compile(
    r"\s*[([]?\s*(?:(?:cd|disc|disk|part|pt\.?)\s*\d+(?:\s*(?:of|/)\s*\d+)?|"
    r"\d+\s*(?:of|/)\s*\d+)\s*[)\]]?"
    r"(?:\s+(?:author\s+interview|bonus(?:\s+material)?))?\s*$",
    re.I,
)
NARRATOR_SUFFIX = re.compile(
    r"\s*[([]?\s*(?:read|narrated|performed)\s+by\s+[^)\]]+[)\]]?\s*$",
    re.I,
)
NUMBERED_SEPARATOR = re.compile(r"\s+-\s+(?=\d+\s*[.:-]?\s*)", re.I)
BOOK_SEGMENT = re.compile(
    r"\bbook\s+\d+(?:\.\d+)?(?![\d.])(?:\s*[-:._]\s*|\s+)(.+)$", re.I
)
COLLECTION_ITEM = re.compile(r"^(.+?)\s+-\s+(\d{1,3}(?:\.\d+)?)\s+-\s+(.+)$")
VARIANT_DIRECTORY = re.compile(r"^alt(?:\.|ernate|ernative)?\s+versions?$", re.I)
FLAT_BOOK_PREFIX = re.compile(
    r"^\s*(?:book\s*)?[a-z]?\d+(?:\.\d+)?\s*[-:._]?\s*", re.I
)
GENERIC_FLAT_TITLES = frozenset({"audio", "audiobook", "book", "complete", "full"})
PLACEHOLDER_TITLES = frozenset({"album", "no title", "unknown", "unknown title", "untitled"})
PACKAGING_LABEL = re.compile(
    r"^(?:(?:cd|disc|disk|part|track)\s*)?\d{1,3}$", re.I
)
GENERIC_CONTAINER_NAMES = GENERIC_FLAT_TITLES | frozenset({
    "final", "finished", "output", "part", "track",
})
IDENTITY_NOISE = frozenset({
    "and", "audio", "audiobook", "book", "complete", "full", "graphic", "graphicaudio",
    "normal", "of", "part", "the", "tmp",
})
YEAR_SUFFIX = re.compile(r"\s+[([]?(?:19|20)\d{2}[)\]]?\s*$")
PART_SUFFIX = re.compile(r"\s+(?:part|pt\.?)\s*\d+\s*$", re.I)
BY_AUTHOR_SUFFIX = re.compile(
    r"\s+by\s+[A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+)+\s*$"
)


def _clean(value: str) -> str:
    return SPACE.sub(" ", value.casefold()).strip()


def _raw_tag(item: ScannedFile, *names: str) -> str:
    if not item.probe:
        return ""
    for name in names:
        if value := item.probe.tags.get(name):
            if stripped := value.strip():
                return stripped
    return ""


def _tag(item: ScannedFile, *names: str) -> str:
    return _clean(_raw_tag(item, *names))


def _structural_root(path: Path) -> Path:
    parent = path.parent
    return parent.parent if DISC_DIRECTORY.match(parent.name) else parent


def _effective_structural_root(item: ScannedFile) -> Path:
    root = _structural_root(item.relative_path)
    if (
        PACKAGING_LABEL.fullmatch(root.name)
        and len(item.relative_path.parts) >= 3
        and item.probe
        and (item.probe.duration_seconds or 0) < 2 * 60 * 60
    ):
        root = root.parent
    if VARIANT_DIRECTORY.fullmatch(root.name) and root.parent != Path("."):
        root = root.parent
    return root


def _filename_book(stem: str) -> str:
    return _clean(TRACK_PREFIX.sub("", stem).strip(" -_."))


def _path_book(value: str) -> str:
    """Extract the leaf book title while retaining the full path as evidence."""
    previous = ""
    while previous != value:
        previous = value
        value = NARRATOR_SUFFIX.sub("", value)
        value = BY_AUTHOR_SUFFIX.sub("", value)
        value = DISC_SUFFIX.sub("", value)
        value = PAREN_ARTIFACT_SUFFIX.sub("", value)
        value = ARTIFACT.sub("", value).strip(" -_.")
    value = NUMBERED_SEPARATOR.split(value)[-1]
    if match := BOOK_SEGMENT.search(value):
        value = match.group(1)
    value = YEAR_SUFFIX.sub("", value)
    return _filename_book(value)


def _long_collection_item(item: ScannedFile) -> str:
    """Identify one-long-file-per-book collections mislabeled with one album tag."""
    if not item.probe or (item.probe.duration_seconds or 0) < 2 * 60 * 60:
        return ""
    match = COLLECTION_ITEM.match(item.relative_path.stem)
    if not match:
        return ""
    title = match.group(3).strip()
    if re.search(r"\b(?:chapter|disc|part|track)\b|\b\d+\s+of\s+\d+\b", title, flags=re.I):
        return ""
    return _clean(title)


def _flat_long_file_title(item: ScannedFile) -> str:
    """Split audiobook-sized files stored directly in one author/collection folder."""
    if (len(item.relative_path.parts) != 2 or not item.probe
            or (item.probe.duration_seconds or 0) < 2 * 60 * 60):
        return ""
    tagged_title = _path_book(_raw_tag(item, "title"))
    tagged_author = _tag(item, "author", "album_artist", "artist")
    top_level = _clean(item.relative_path.parts[0])
    tag_is_author = bool(
        tagged_title
        and (
            tagged_title == tagged_author
            or tagged_title == top_level
        )
    )
    if (
        tagged_title
        and not tag_is_author
        and tagged_title not in GENERIC_FLAT_TITLES | PLACEHOLDER_TITLES
    ):
        return tagged_title
    stem = FLAT_BOOK_PREFIX.sub("", item.relative_path.stem)
    filename_title = _path_book(stem)
    return "" if filename_title in GENERIC_FLAT_TITLES else filename_title


def _standalone_container_title(item: ScannedFile, siblings: int) -> str:
    """Find a structural title for one audiobook-sized M4A/M4B container."""
    if item.path.suffix.casefold() not in {".m4a", ".m4b"} or not item.probe:
        return ""
    duration = item.probe.duration_seconds or 0
    tagged_title = _path_book(_raw_tag(item, "title"))
    tagged_album = _path_book(_raw_tag(item, "album"))
    if (
        siblings > 1
        and duration >= 20 * 60
        and not _legacy_temp_member(item)
        and tagged_title
        and tagged_title not in GENERIC_CONTAINER_NAMES | PLACEHOLDER_TITLES
        and not PACKAGING_LABEL.fullmatch(tagged_title)
        and tagged_album
        and _identity_overlap(tagged_title, tagged_album) < 0.5
    ):
        return tagged_title
    filename_title = _path_book(item.relative_path.stem)
    filename_words, _ = _identity_tokens(filename_title)
    if (
        siblings > 1
        and duration >= 20 * 60
        and not _legacy_temp_member(item)
        and len(filename_words) >= 2
        and filename_title not in GENERIC_CONTAINER_NAMES | PLACEHOLDER_TITLES
        and not PACKAGING_LABEL.fullmatch(filename_title)
        and (not tagged_album or _identity_overlap(filename_title, tagged_album) < 0.5)
        and (
            not tagged_title
            or PACKAGING_LABEL.fullmatch(tagged_title)
            or _identity_overlap(filename_title, tagged_title) < 0.5
        )
    ):
        return filename_title
    if duration < 2 * 60 * 60:
        return ""
    if siblings == 1:
        parent = item.relative_path.parent.name
        if not VARIANT_DIRECTORY.fullmatch(parent):
            candidate = _path_book(parent)
            tagged_author = _tag(item, "author", "album_artist", "artist")
            if (
                candidate
                and candidate != tagged_author
                and candidate not in GENERIC_CONTAINER_NAMES
            ):
                return candidate
    stripped = FLAT_BOOK_PREFIX.sub("", item.relative_path.stem)
    if stripped != item.relative_path.stem:
        candidate = _path_book(stripped)
        if candidate and candidate not in GENERIC_CONTAINER_NAMES:
            return candidate
    return ""


def _identity_overlap(left: str, right: str) -> float:
    left_words, right_words = set(left.split()), set(right.split())
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / min(len(left_words), len(right_words))


def _coded_album_matches_structure(album: str, structural_title: str) -> bool:
    """Recognize compact series codes prepended to an otherwise exact title."""
    if not album or not structural_title or not album.endswith(structural_title):
        return False
    prefix = album[:-len(structural_title)].strip(" -:._")
    return bool(re.fullmatch(r"[a-z]{1,6}\s*\d{1,3}(?:\.\d+)?", prefix))


def _strip_top_level_author_credit(title: str, item: ScannedFile) -> str:
    if len(item.relative_path.parts) < 2:
        return title
    author = _clean(item.relative_path.parts[0])
    if not author:
        return title
    for pattern in (rf"^{re.escape(author)}\s+", rf"\s+{re.escape(author)}$"):
        cleaned = re.sub(pattern, "", title).strip()
        if cleaned != title and cleaned:
            return cleaned
    return title


def _graphic_audio_structural_title(
    item: ScannedFile, root: Path, album: str
) -> str:
    path_marked = bool(re.search(r"(?:^|[-_ ])ga\s*$", root.name, flags=re.I))
    credit_marked = bool(re.fullmatch(
        r"graphic\s*audio(?:,?\s*llc\.?)?",
        _raw_tag(item, "album_artist", "artist"),
        flags=re.I,
    ))
    if not path_marked and not credit_marked:
        return ""
    structural = (
        re.sub(r"\s+ga$", "", _path_book(root.name)).strip()
        if path_marked else album
    )
    structural_words, _ = _identity_tokens(structural)
    album_words, _ = _identity_tokens(album)
    if structural_words and (not album_words or structural_words <= album_words):
        return f"{structural} graphic audio"
    return ""


def _legacy_part_structural_title(
    item: ScannedFile, candidate: str, structural_title: str
) -> str:
    if not _legacy_temp_member(item) or not PART_SUFFIX.search(candidate):
        return ""
    base = _clean(PART_SUFFIX.sub("", candidate))
    base_words, _ = _identity_tokens(base)
    structural_words, _ = _identity_tokens(structural_title)
    if base_words and structural_words and (
        base_words <= structural_words or structural_words <= base_words
    ):
        return base
    return ""


def _legacy_temp_member(item: ScannedFile) -> bool:
    return (
        item.relative_path.stem.casefold().startswith(("tmp_", "tmp-", "tmp "))
        or any(re.search(r"(?:^|[-_ ])tmpfiles?$", part, flags=re.I)
               for part in item.relative_path.parts[:-1])
    )


def _identity_tokens(title: str) -> tuple[set[str], set[str]]:
    # The denominator in "3 of 5" describes packaging, not book identity.
    collapsed = re.sub(r"\b(\d+)\s+of\s+\d+\b", r"\1", title)
    tokens = re.findall(r"[a-z]+|\d+(?:\.\d+)?", collapsed.casefold())
    words = {token for token in tokens if not token[0].isdigit() and token not in IDENTITY_NOISE}
    numbers = {
        str(float(token)).removesuffix(".0")
        for token in tokens if token[0].isdigit()
    }
    return words, numbers


def _titles_can_be_legacy_equivalents(
    left: str, right: str, *, allow_single_word: bool = False
) -> bool:
    left_words, left_numbers = _identity_tokens(left)
    right_words, right_numbers = _identity_tokens(right)
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return False
    if not left_words or not right_words:
        return False
    overlap = left_words & right_words
    minimum = 1 if allow_single_word else 2
    return len(overlap) >= minimum and (
        left_words <= right_words or right_words <= left_words
        or len(overlap) / min(len(left_words), len(right_words)) >= 0.9
    )


def _duration_signatures(members: list[ScannedFile]) -> tuple[float, ...]:
    roots: dict[Path, float] = defaultdict(float)
    containers: list[float] = []
    for item in members:
        duration = item.probe.duration_seconds if item.probe else 0
        if duration and duration > 0:
            roots[_structural_root(item.relative_path)] += duration
            if item.path.suffix.casefold() in {".m4a", ".m4b"} and duration >= 20 * 60:
                containers.append(duration)
    return tuple(
        [value for value in roots.values() if value >= 20 * 60]
        + containers
    )


def _durations_prove_same_legacy_output(
    left: list[ScannedFile], right: list[ScannedFile]
) -> bool:
    return any(
        abs(a - b) <= max(5.0, max(a, b) * 0.005)
        for a in _duration_signatures(left)
        for b in _duration_signatures(right)
    )


def _merge_legacy_equivalent_buckets(
    buckets: dict[str, list[ScannedFile]],
) -> tuple[dict[str, list[ScannedFile]], set[str]]:
    """Join old tmp conversion trees only when title and runtime both prove identity."""
    titles = list(buckets)
    parents = list(range(len(titles)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = root(left), root(right)
        if a != b:
            parents[b] = a

    legacy = [any(_legacy_temp_member(item) for item in buckets[title]) for title in titles]
    whole = [
        len(buckets[title]) == 1
        and bool(buckets[title][0].probe)
        and (buckets[title][0].probe.duration_seconds or 0) >= 20 * 60
        for title in titles
    ]
    tmp_targets = [
        len(buckets[title]) == 1
        and buckets[title][0].relative_path.stem.casefold().startswith("tmp_")
        and buckets[title][0].path.suffix.casefold() in {".m4a", ".m4b"}
        for title in titles
    ]

    def author_identities(members: list[ScannedFile]) -> set[frozenset[str]]:
        by_top: dict[str, list[ScannedFile]] = defaultdict(list)
        for item in members:
            top = item.relative_path.parts[0] if item.relative_path.parts else ""
            by_top[top].append(item)
        return {
            frozenset(identity.split())
            for partition in by_top.values()
            if (identity := _partition_author_identity(partition))
        }

    bucket_authors = [author_identities(buckets[title]) for title in titles]
    for left in range(len(titles)):
        for right in range(left + 1, len(titles)):
            if not (legacy[left] or legacy[right] or whole[left] or whole[right]):
                continue
            if not _titles_can_be_legacy_equivalents(
                titles[left], titles[right],
                allow_single_word=(
                    tmp_targets[left]
                    or tmp_targets[right]
                    or bool(bucket_authors[left] & bucket_authors[right])
                ),
            ):
                continue
            if _durations_prove_same_legacy_output(buckets[titles[left]], buckets[titles[right]]):
                union(left, right)

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(titles)):
        components[root(index)].append(index)
    merged: dict[str, list[ScannedFile]] = {}
    merged_titles: set[str] = set()
    for indexes in components.values():
        canonical = min(
            (titles[index] for index in indexes),
            key=lambda title: (len(_identity_tokens(title)[0]), len(title), title),
        )
        merged[canonical] = [
            item for index in indexes for item in buckets[titles[index]]
        ]
        if len(indexes) > 1:
            merged_titles.add(canonical)
    return merged, merged_titles


def _attach_unreadable_siblings(
    buckets: dict[str, list[ScannedFile]],
) -> dict[str, list[ScannedFile]]:
    """Keep broken conversion fragments with readable siblings from the same directory."""
    readable_by_parent: dict[Path, set[str]] = defaultdict(set)
    for title, members in buckets.items():
        for item in members:
            if item.probe and (item.probe.duration_seconds or 0) > 0:
                readable_by_parent[item.relative_path.parent].add(title)
    result = {title: list(members) for title, members in buckets.items()}
    for title, members in list(result.items()):
        if any(item.probe and (item.probe.duration_seconds or 0) > 0 for item in members):
            continue
        candidates = {
            candidate
            for item in members
            for candidate in readable_by_parent.get(item.relative_path.parent, set())
            if candidate != title
        }
        if len(candidates) == 1:
            result[next(iter(candidates))].extend(members)
            del result[title]
    return result


def _partition_author_identity(members: list[ScannedFile]) -> str:
    """Return stable primary-author evidence for one top-level source tree."""
    top_level = _clean(members[0].relative_path.parts[0])
    tagged = {
        " ".join(
            word
            for word in _tag(item, "author", "album_artist", "artist").split()
            if word not in {"audio", "graphic", "graphicaudio", "llc"}
        )
        for item in members
        if _tag(item, "author", "album_artist", "artist")
    }
    tagged.discard("")
    # A folder such as ``R.A Salvatore`` is stronger than a combined Artist tag
    # such as ``R. A. Salvatore, Victor Bevine`` when it is contained in it.
    top_words = set(top_level.split())
    if len(top_words) >= 2 and any(top_words <= set(value.split()) for value in tagged):
        return top_level
    return min(tagged, key=lambda value: (len(value.split()), len(value), value)) if tagged else ""


def _split_conflicting_author_bucket(
    members: list[ScannedFile],
) -> list[list[ScannedFile]]:
    """Keep same-title books by different authors as separate identities.

    Exact title tags alone are insufficient: unrelated books frequently share a
    short title. Top-level trees are only separated when every tree has author
    evidence and those identities conflict. Matching-duration trees remain
    connected because they are strong alternate-representation evidence.
    """
    by_top: dict[str, list[ScannedFile]] = defaultdict(list)
    for item in members:
        top = item.relative_path.parts[0] if item.relative_path.parts else ""
        by_top[top].append(item)
    partitions = list(by_top.values())
    if len(partitions) < 2:
        return [members]
    authors = [_partition_author_identity(partition) for partition in partitions]
    if not all(authors) or len(set(authors)) < 2:
        return [members]

    parents = list(range(len(partitions)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = root(left), root(right)
        if a != b:
            parents[b] = a

    for left in range(len(partitions)):
        for right in range(left + 1, len(partitions)):
            if (
                authors[left] == authors[right]
                or set(authors[left].split()) == set(authors[right].split())
                or _durations_prove_same_legacy_output(
                partitions[left], partitions[right]
                )
            ):
                union(left, right)
    grouped: dict[int, list[ScannedFile]] = defaultdict(list)
    for index, partition in enumerate(partitions):
        grouped[root(index)].extend(partition)
    return list(grouped.values())


def _single_file_identity_conflict(item: ScannedFile, structural_title: str) -> bool:
    if item.probe and _tag(item, "album"):
        return False
    tagged_title = _path_book(_raw_tag(item, "title"))
    if tagged_title and _identity_overlap(structural_title, tagged_title) >= 0.5:
        return False
    filename_title = _filename_book(item.relative_path.stem)
    if not filename_title or filename_title.isdigit() or filename_title in {"audio", "audiobook", "track"}:
        return False
    left, right = set(structural_title.split()), set(filename_title.split())
    if not left or not right:
        return False
    compact_left = re.sub(r"[^a-z0-9]+", "", structural_title)
    compact_right = re.sub(r"[^a-z0-9]+", "", filename_title)
    if len(compact_left) >= 8 and compact_left in compact_right:
        return False
    overlap = len(left & right) / min(len(left), len(right))
    return overlap < 0.5


def detect_books(files: list[ScannedFile]) -> list[BookGroup]:
    """Detect probable books with tags first and paths only as fallback evidence."""
    buckets: dict[str, list[ScannedFile]] = defaultdict(list)
    identity_conflict_paths: set[Path] = set()
    sibling_counts = Counter(item.relative_path.parent for item in files)
    structurally_supported_roots: set[Path] = set()
    structural_support_counts: Counter[Path] = Counter()
    structural_support_durations: dict[Path, float] = defaultdict(float)
    for item in files:
        support_root = _effective_structural_root(item)
        structural = _strip_top_level_author_credit(_path_book(support_root.name), item)
        supporting_album = _strip_top_level_author_credit(
            _path_book(_raw_tag(item, "album")), item
        )
        if (
            structural
            and supporting_album
            and _identity_overlap(structural, supporting_album) >= 0.8
        ):
            structurally_supported_roots.add(support_root)
            structural_support_counts[support_root] += 1
            structural_support_durations[support_root] += (
                item.probe.duration_seconds if item.probe else 0
            ) or 0
    for item in files:
        # Album tags often differ only by packaging suffixes such as
        # "(Unabridged)". Canonicalize them the same way as directory names so
        # tagged chapters and untagged temporary/complete representations stay
        # in one book identity.
        album = _path_book(_raw_tag(item, "album"))
        if album in PLACEHOLDER_TITLES:
            album = ""
        author = _tag(item, "album_artist", "artist", "author")
        root = _effective_structural_root(item)
        album = _strip_top_level_author_credit(album, item)
        collection_item = _long_collection_item(item)
        flat_long_title = _flat_long_file_title(item)
        standalone_container = _standalone_container_title(
            item, sibling_counts[item.relative_path.parent]
        )
        structural_title = _strip_top_level_author_credit(
            _path_book(root.name), item
        )
        if (
            PACKAGING_LABEL.fullmatch(album)
            and structural_title
            and not PACKAGING_LABEL.fullmatch(structural_title)
        ):
            album = ""
        graphic_audio_title = _graphic_audio_structural_title(item, root, album)
        legacy_part_title = _legacy_part_structural_title(
            item, flat_long_title or album, structural_title
        )
        generic_album = bool(album and album in {
            _clean(author), _clean(root.name), *GENERIC_FLAT_TITLES
        })
        if generic_album and structural_title and structural_title not in (
            GENERIC_CONTAINER_NAMES | PLACEHOLDER_TITLES
        ):
            album = ""
        if collection_item:
            title = collection_item
        elif graphic_audio_title:
            title = graphic_audio_title
        elif legacy_part_title:
            title = legacy_part_title
        elif standalone_container and (
            not album or _identity_overlap(standalone_container, album) < 0.5
        ):
            title = standalone_container
        elif flat_long_title and (not album or generic_album):
            title = flat_long_title
        elif _coded_album_matches_structure(album, structural_title):
            title = structural_title
        elif album:
            title = album
        else:
            title = structural_title or _filename_book(item.relative_path.stem)
        if (
            not album
            and title == structural_title
            and not _legacy_temp_member(item)
            and root in structurally_supported_roots
            and structural_support_counts[root] >= 2
            and bool(item.probe and item.probe.duration_seconds)
            and (item.probe.duration_seconds or 0) >= structural_support_durations[root] * 0.8
            and _single_file_identity_conflict(item, structural_title)
        ):
            filename_title = _filename_book(item.relative_path.stem)
            raw_filename_title = _clean(item.relative_path.stem)
            if match := re.match(r"^(\d+)\s+(.+)$", raw_filename_title):
                if not match.group(1).startswith("0") and int(match.group(1)) >= 10:
                    filename_title = raw_filename_title
            if filename_title and filename_title not in GENERIC_CONTAINER_NAMES:
                title = filename_title
                identity_conflict_paths.add(item.relative_path)
        buckets[title].append(item)

    buckets = _attach_unreadable_siblings(dict(buckets))
    buckets, legacy_merges = _merge_legacy_equivalent_buckets(buckets)
    separated = [
        (title, partition)
        for title, members in buckets.items()
        for partition in _split_conflicting_author_bucket(members)
    ]
    groups: list[BookGroup] = []
    for title, members in sorted(
        separated,
        key=lambda pair: (pair[0], str(pair[1][0].relative_path).casefold()),
    ):
        ordered = tuple(sorted(members, key=lambda item: str(item.relative_path).casefold()))
        authors = sorted({_tag(item, "author", "artist", "album_artist") for item in members} - {""})
        author = authors[0] if len(authors) == 1 else ""
        label = " — ".join(value for value in (author, title) if value) or "unknown"
        tagged = sum(bool(_tag(item, "album")) for item in members)
        split_collection = any(_long_collection_item(item) == title for item in members)
        split_container_collection = any(
            _standalone_container_title(item, sibling_counts[item.relative_path.parent]) == title
            and bool(_path_book(_raw_tag(item, "album")))
            and _path_book(_raw_tag(item, "album")) != title
            for item in members
        )
        identity_conflict = len(members) == 1 and (
            members[0].relative_path in identity_conflict_paths
            or _single_file_identity_conflict(members[0], title)
        )
        reason = ("duration-matched alternate representation joined to its source book"
                  if title in legacy_merges else
                  "directory and filename identities conflict"
                  if identity_conflict else
                  "long-form collection files split into individual books"
                  if split_collection else
                  "audiobook-sized containers split from a shared collection album"
                  if split_container_collection else
                  "tag and normalized path identity reconciled equivalent representations"
                  if tagged and tagged != len(members) else
                  "embedded album identity grouped these files" if tagged else
                  "normalized directory/disc structure grouped untagged files")
        groups.append(BookGroup(label, ordered, (reason, f"detector identity: {label}")))
    return groups
