from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any

from .models import Chapter, ProbeResult


class ProbeError(RuntimeError):
    """Raised when ffprobe cannot inspect one media file."""


def is_aac_container(probe: ProbeResult) -> bool:
    """Recognize an AAC MP4 container even when the filename has no extension."""
    formats = set((probe.format_name or "").casefold().split(","))
    return probe.codec_name == "aac" and bool(formats & {"mov", "mp4", "m4a", "m4b"})


RIFF_SCAN_BYTES = 1024 * 1024


def _embedded_riff_offset(path: Path) -> int | None:
    """Return the offset of a WAV container hidden behind an ID3 prefix.

    Some audiobook tools write a large ID3 block in front of a RIFF/WAVE file
    while retaining an ``.mp3`` suffix. FFmpeg cannot auto-detect those files,
    but it can read them losslessly when given the verified RIFF offset.
    """
    if path.suffix.casefold() != ".mp3":
        return None
    try:
        with path.open("rb") as stream:
            prefix = stream.read(RIFF_SCAN_BYTES)
    except OSError:
        return None
    if not prefix.startswith(b"ID3"):
        return None
    offset = prefix.find(b"RIFF")
    if offset <= 0 or prefix[offset + 8 : offset + 12] != b"WAVE":
        return None
    return offset


def media_input_args(path: Path) -> list[str]:
    """Return FFmpeg input options required to read a known wrapped file."""
    offset = _embedded_riff_offset(path)
    return ["-skip_initial_bytes", str(offset), "-f", "wav"] if offset else []


def _run_probe(path: Path, timeout_seconds: float, input_args: list[str]) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        *input_args,
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "--",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out after {timeout_seconds:g} seconds") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        raise ProbeError(detail[-1] if detail else f"ffprobe exited {completed.returncode}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe returned invalid JSON") from exc


def _number(value: Any, conversion: type[float] | type[int]) -> float | int | None:
    try:
        result = conversion(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def probe_media(path: Path, *, timeout_seconds: float = 60.0) -> ProbeResult:
    try:
        payload = _run_probe(path, timeout_seconds, [])
    except ProbeError:
        input_args = media_input_args(path)
        if not input_args:
            raise
        payload = _run_probe(path, timeout_seconds, input_args)

    try:
        return _parse_probe(payload)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ProbeError(f"ffprobe returned malformed media information: {exc}") from exc


def _parse_probe(payload: dict[str, Any]) -> ProbeResult:
    audio = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"), {})
    media_format = payload.get("format", {})
    tags: dict[str, str] = {}
    for source in (media_format.get("tags", {}), audio.get("tags", {})):
        for key, value in source.items():
            tags[str(key).lower()] = str(value)

    chapters = tuple(
        Chapter(
            index=int(chapter.get("id", index)),
            start_seconds=float(chapter.get("start_time", 0)),
            end_seconds=float(chapter.get("end_time", 0)),
            title=(chapter.get("tags") or {}).get("title"),
        )
        for index, chapter in enumerate(payload.get("chapters", []))
    )
    duration = _number(media_format.get("duration", audio.get("duration")), float)
    if any(not math.isfinite(chapter.start_seconds)
           or not math.isfinite(chapter.end_seconds)
           or chapter.start_seconds < 0 or chapter.end_seconds < chapter.start_seconds
           for chapter in chapters):
        raise ValueError("invalid chapter timestamps")
    return ProbeResult(
        duration_seconds=float(duration) if duration is not None else None,
        format_name=media_format.get("format_name"),
        codec_name=audio.get("codec_name"),
        bitrate=_number(audio.get("bit_rate", media_format.get("bit_rate")), int),  # type: ignore[arg-type]
        sample_rate=_number(audio.get("sample_rate"), int),  # type: ignore[arg-type]
        channels=_number(audio.get("channels"), int),  # type: ignore[arg-type]
        tags=tags,
        chapters=chapters,
    )
