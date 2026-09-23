from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Chapter:
    index: int
    start_seconds: float
    end_seconds: float
    title: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    duration_seconds: float | None
    format_name: str | None
    codec_name: str | None
    bitrate: int | None
    sample_rate: int | None
    channels: int | None
    tags: dict[str, str] = field(default_factory=dict)
    chapters: tuple[Chapter, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProbeResult:
        chapters = tuple(Chapter(**chapter) for chapter in value.get("chapters", []))
        return cls(
            duration_seconds=value.get("duration_seconds"),
            format_name=value.get("format_name"),
            codec_name=value.get("codec_name"),
            bitrate=value.get("bitrate"),
            sample_rate=value.get("sample_rate"),
            channels=value.get("channels"),
            tags=dict(value.get("tags", {})),
            chapters=chapters,
        )


@dataclass(frozen=True)
class ScannedFile:
    path: Path
    relative_path: Path
    size: int
    modified_ns: int
    probe: ProbeResult | None
    error: str | None = None
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "relative_path": str(self.relative_path),
            "size": self.size,
            "modified_ns": self.modified_ns,
            "cached": self.cached,
            "error": self.error,
            "probe": self.probe.to_dict() if self.probe else None,
        }

@dataclass(frozen=True)
class BookGroup:
    key: str
    files: tuple[ScannedFile, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "reasons": list(self.reasons),
            "files": [str(item.relative_path) for item in self.files],
        }
