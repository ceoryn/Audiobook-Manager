from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterator
from pathlib import Path

from .database import StateDatabase
from .models import ProbeResult, ScannedFile
from .probe import ProbeError, probe_media

SUPPORTED_EXTENSIONS = frozenset({".mp3", ".m4a", ".m4b", ".mp4"})
ProbeFunction = Callable[[Path], ProbeResult]


def is_media_candidate(path: Path) -> bool:
    if path.suffix.lower() in SUPPORTED_EXTENSIONS:
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return False
    # Extensionless downloads still have a recognizable MP4 or ID3 header.
    return header[4:8] == b"ftyp" or header.startswith(b"ID3")


def discover_media(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*"), key=lambda value: str(value).casefold()):
        if path.is_file() and is_media_candidate(path):
            yield path


def scan_library(
    root: Path,
    *,
    database: StateDatabase | None = None,
    probe: ProbeFunction = probe_media,
    workers: int = 4,
) -> list[ScannedFile]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"library path is not a directory: {root}")

    if workers < 1:
        raise ValueError("workers must be at least 1")

    results: list[ScannedFile] = []
    pending: list[tuple[Path, int, int]] = []
    for path in discover_media(root):
        try:
            stat = path.stat()
        except OSError as exc:
            results.append(
                ScannedFile(path, path.relative_to(root), 0, 0, None, f"stat failed: {exc}")
            )
            continue

        cached = database.cached_probe(path, size=stat.st_size, modified_ns=stat.st_mtime_ns) if database else None
        if cached is not None:
            media_probe, error = cached
            results.append(
                ScannedFile(path, path.relative_to(root), stat.st_size, stat.st_mtime_ns, media_probe, error, True)
            )
            continue

        pending.append((path, stat.st_size, stat.st_mtime_ns))

    def inspect(details: tuple[Path, int, int]) -> ScannedFile:
        path, size, modified_ns = details
        try:
            media_probe = probe(path)
            return ScannedFile(path, path.relative_to(root), size, modified_ns, media_probe)
        except (ProbeError, OSError) as exc:
            return ScannedFile(path, path.relative_to(root), size, modified_ns, None, str(exc))

    # executor.map preserves discovery order while limiting simultaneous ffprobe processes.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ffprobe") as executor:
        inspected = executor.map(inspect, pending)
        for item in inspected:
            results.append(item)
            if database:
                database.store(item)

    results.sort(key=lambda item: str(item.relative_path).casefold())
    return results
