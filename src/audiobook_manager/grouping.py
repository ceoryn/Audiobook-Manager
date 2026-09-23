from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from .models import BookGroup, ScannedFile

DISC_DIRECTORY = re.compile(
    r"^(?:(?:cd|disc|disk|part)[ _.-]*\d+|\(?\d+\s*(?:of|/)\s*\d+\)?)$",
    re.IGNORECASE,
)


def _group_directory(relative_path: Path) -> Path:
    parent = relative_path.parent
    if parent.name and DISC_DIRECTORY.match(parent.name):
        return parent.parent
    return parent


def group_files(files: list[ScannedFile]) -> list[BookGroup]:
    buckets: dict[Path, list[ScannedFile]] = defaultdict(list)
    for item in files:
        buckets[_group_directory(item.relative_path)].append(item)

    groups: list[BookGroup] = []
    for directory in sorted(buckets, key=lambda value: str(value).casefold()):
        members = tuple(sorted(buckets[directory], key=lambda item: str(item.relative_path).casefold()))
        reasons = ["files share their nearest non-disc directory"]
        if any(DISC_DIRECTORY.match(item.relative_path.parent.name) for item in members):
            reasons.append("CD/disc/part subdirectories were folded into their parent")
        groups.append(BookGroup(str(directory) if str(directory) != "." else "<library-root>", members, tuple(reasons)))
    return groups
