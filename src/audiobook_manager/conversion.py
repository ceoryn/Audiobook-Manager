from __future__ import annotations

import os
import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .probe import ProbeError, media_input_args, probe_media
from .output import approved_cover_url
from .configuration import require_outside_source


def _conversion_roots(library_root: Path, output_root: Path) -> tuple[Path, Path]:
    library_root = library_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not library_root.is_dir():
        raise ValueError("source library does not exist")
    require_outside_source(output_root, library_root, purpose="conversion output")
    # Archive imports use disposable extracted inputs under the output tree.
    # Their final destination must still be outside that input directory.
    return library_root, output_root


def _safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", " ", value)
    return re.sub(r"\s+", " ", value).strip(" .")[:180] or "Untitled"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified_m4b(*, source: Path, library_root: Path, output_root: Path,
                      destination: Path) -> Path:
    """Create a byte-identical, validated copy without modifying the source."""
    library_root, output_root = _conversion_roots(library_root, output_root)
    source, destination = source.resolve(), destination.resolve()
    require_outside_source(destination, library_root, purpose="copy destination")
    try:
        source.relative_to(library_root)
        destination.relative_to(output_root)
    except ValueError as exc:
        raise ValueError("clean copy must remain within configured source and output roots") from exc
    if source.suffix.casefold() != ".m4b" or not source.is_file():
        raise ValueError("clean-copy fast path requires one existing M4B")
    if destination.exists():
        raise FileExistsError(f"output already exists: {destination}")
    output_root.mkdir(parents=True, exist_ok=True)
    original = probe_media(source)
    if original.codec_name != "aac" or not original.duration_seconds:
        raise ValueError("clean-copy source must contain readable AAC audio")
    with tempfile.TemporaryDirectory(prefix="audiobook-manager-copy-", dir=output_root) as temp_dir:
        staged = Path(temp_dir) / "output.m4b"
        shutil.copy2(source, staged)
        copied = probe_media(staged)
        if copied.codec_name != original.codec_name or copied.duration_seconds != original.duration_seconds:
            raise RuntimeError("clean-copy media verification failed")
        if len(copied.chapters) != len(original.chapters) or _sha256(staged) != _sha256(source):
            raise RuntimeError("clean-copy byte or chapter verification failed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(staged, destination)
        except FileExistsError:
            raise FileExistsError(f"output already exists: {destination}") from None
    return destination


def convert_to_m4b(*, inputs: list[Path], library_root: Path, output_root: Path,
                   metadata: dict[str, Any], overwrite: bool = False,
                   destination: Path | None = None, copy_audio: bool = False) -> Path:
    library_root, output_root = _conversion_roots(library_root, output_root)
    if not inputs:
        raise ValueError("conversion needs at least one input")
    resolved = [path.resolve() for path in inputs]
    for path in resolved:
        try:
            path.relative_to(library_root)
        except ValueError as exc:
            raise ValueError(f"input is outside the source library: {path}") from exc
        if not path.is_file():
            raise ValueError(f"input does not exist: {path}")
    title = str(metadata.get("title") or "Untitled").strip()
    author = ", ".join(metadata.get("authors") or [])
    destination = destination.resolve() if destination else output_root / f"{_safe_name(f'{author} - {title}' if author else title)}.m4b"
    require_outside_source(destination, library_root, purpose="conversion destination")
    try:
        destination.relative_to(output_root)
    except ValueError as exc:
        raise ValueError("destination must remain inside output root") from exc
    if destination.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {destination}")
    output_root.mkdir(parents=True, exist_ok=True)
    probes = [probe_media(path) for path in resolved]
    with tempfile.TemporaryDirectory(prefix="audiobook-manager-", dir=output_root) as temp_dir:
        temp = Path(temp_dir)
        conversion_inputs = list(resolved)
        effective_probes = list(probes)
        special_inputs = [media_input_args(path) for path in resolved]
        # Some legacy files contain MP3 frames in a RIFF/WAVE payload behind a
        # large ID3 prefix.  Their container timestamps and declared duration
        # can be badly wrong even though most audio samples still decode.  A
        # per-part PCM normalization gives the recovered samples a fresh,
        # monotonic timeline before concatenation; processing all malformed
        # parts in one FFmpeg graph can otherwise silently discard hours.
        for index, (path, input_args) in enumerate(
            zip(resolved, special_inputs, strict=True), 1
        ):
            if not input_args:
                continue
            normalized = temp / f"recovered-{index:04d}.wav"
            recovered = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", *input_args,
                    "-i", str(path), "-map", "0:a:0", "-c:a", "pcm_s16le",
                    str(normalized),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if recovered.returncode:
                raise RuntimeError(
                    recovered.stderr.strip()
                    or f"failed to normalize legacy audio input: {path}"
                )
            normalized_probe = probe_media(normalized)
            if not normalized_probe.duration_seconds:
                raise RuntimeError(f"legacy audio input has no recoverable samples: {path}")
            conversion_inputs[index - 1] = normalized
            effective_probes[index - 1] = normalized_probe
        expected = sum((probe.duration_seconds or 0) for probe in effective_probes)
        concat = temp / "inputs.ffconcat"
        def escaped(path: Path) -> str:
            return str(path).replace("'", "'\\''")
        concat.write_text("ffconcat version 1.0\n" + "".join(f"file '{escaped(path)}'\n" for path in conversion_inputs), encoding="utf-8")
        chapter_lines = [";FFMETADATA1"]
        cursor_ms = 0
        for index, (path, source_probe, effective_probe) in enumerate(
            zip(resolved, probes, effective_probes, strict=True), 1
        ):
            duration_ms = round((effective_probe.duration_seconds or 0) * 1000)
            chapter_title = source_probe.tags.get("title") or path.stem or f"Part {index}"
            chapter_title = chapter_title.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", " ")
            chapter_lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={cursor_ms}", f"END={cursor_ms + duration_ms}", f"title={chapter_title}"]
            cursor_ms += duration_ms
        chapters = temp / "chapters.ffmeta"
        chapters.write_text("\n".join(chapter_lines) + "\n", encoding="utf-8")
        staged = temp / "output.m4b"
        signatures = {
            (probe.codec_name, probe.sample_rate, probe.channels)
            for probe in effective_probes
        }
        preserve_container = copy_audio and len(resolved) == 1
        conversion_input_args = [media_input_args(path) for path in conversion_inputs]
        filter_concat = len(resolved) > 1 and len(signatures) > 1
        if filter_concat:
            command = ["ffmpeg", "-nostdin", "-v", "error"]
            for path, input_args in zip(
                conversion_inputs, conversion_input_args, strict=True
            ):
                command += [*input_args, "-i", str(path)]
            metadata_index = len(resolved)
            command += ["-f", "ffmetadata", "-i", str(chapters)]
        elif preserve_container:
            # A pre-existing M4B may already contain dozens of real chapters and
            # attached artwork. Read it directly so a metadata-only remux keeps
            # that container structure rather than replacing it with one chapter.
            command = [
                "ffmpeg", "-nostdin", "-v", "error",
                *conversion_input_args[0], "-i", str(conversion_inputs[0]),
            ]
            metadata_index = 0
        else:
            if len(resolved) == 1 and conversion_input_args[0]:
                command = [
                    "ffmpeg", "-nostdin", "-v", "error", *conversion_input_args[0],
                    "-i", str(conversion_inputs[0]), "-f", "ffmetadata", "-i", str(chapters),
                ]
            else:
                command = [
                    "ffmpeg", "-nostdin", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-f", "ffmetadata", "-i", str(chapters),
                ]
            metadata_index = 1
        cover_url = "" if preserve_container else str(metadata.get("cover_url") or "").strip()
        cover: Path | None = None
        if cover_url:
            try:
                cover_url = approved_cover_url(cover_url)
                request = Request(cover_url, headers={"User-Agent": "AudiobookManager/0.1"})
                with urlopen(request, timeout=15) as response:  # noqa: S310
                    content = response.read(10_000_001)
                if not content or len(content) > 10_000_000:
                    raise ValueError("cover image is empty or larger than 10 MB")
                cover = temp / "cover.jpg"
                cover.write_bytes(content)
                command += ["-i", str(cover)]
            except (OSError, TimeoutError, ValueError):
                # Cover art is optional. A provider or network problem must not
                # prevent creation of an otherwise valid audiobook.
                cover = None
        if filter_concat:
            layout = "mono" if all(probe.channels == 1 for probe in probes) else "stereo"
            filters = [
                f"[{index}:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
                f"aformat=sample_fmts=fltp:channel_layouts={layout}[a{index}]"
                for index in range(len(resolved))
            ]
            joined = "".join(f"[a{index}]" for index in range(len(resolved)))
            filters.append(f"{joined}concat=n={len(resolved)}:v=0:a=1[aout]")
            command += ["-filter_complex", ";".join(filters), "-map", "[aout]"]
        elif preserve_container:
            command += ["-map", "0:a:0", "-map", "0:v?"]
        else:
            command += ["-map", "0:a:0"]
        if preserve_container:
            command += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
        else:
            command += [
                "-map_metadata", str(metadata_index),
                "-map_chapters", str(metadata_index),
            ]
            command += ["-c:a", "aac", "-b:a", "96k"]
        command += ["-movflags", "+faststart"]
        if cover:
            command += [
                "-map", f"{metadata_index + 1}:v:0", "-c:v", "copy",
                "-disposition:v:0", "attached_pic",
            ]
        command += [
                   "-metadata", f"title={title}", "-metadata", f"artist={author}",
                   "-metadata", f"album_artist={author}", "-metadata", f"album={title}",
                   "-metadata", "media_type=2"]
        if metadata.get("narrator"):
            command += ["-metadata", f"composer={metadata['narrator']}"]
        if metadata.get("series"):
            series = metadata["series"]
            series_name = series.get("name") if isinstance(series, dict) else series
            command += ["-metadata", f"grouping={series_name}"]
        if metadata.get("series_position") or metadata.get("volume"):
            command += ["-metadata", f"track={metadata.get('series_position') or metadata.get('volume')}"]
        if metadata.get("description"):
            command += ["-metadata", f"comment={metadata['description']}"]
        if metadata.get("asin"):
            command += ["-metadata", f"ASIN={metadata['asin']}"]
        if metadata.get("publisher"):
            command += ["-metadata", f"publisher={metadata['publisher']}"]
        if metadata.get("publish_year"):
            command += ["-metadata", f"date={metadata['publish_year']}"]
        # M4B is an MP4-family container.  Selecting mp4 explicitly also lets
        # metadata-only remuxes preserve valid attached JPEG cover streams that
        # FFmpeg's narrower legacy ``ipod`` muxer rejects.
        command += ["-f", "mp4", str(staged)]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or f"ffmpeg exited {completed.returncode}")
        try:
            result = probe_media(staged)
        except ProbeError as exc:
            raise RuntimeError(f"output verification failed: {exc}") from exc
        actual = result.duration_seconds or 0
        if expected <= 0 or abs(actual - expected) > max(5.0, expected * 0.005):
            raise RuntimeError(f"output duration verification failed: expected {expected:.2f}s, got {actual:.2f}s")
        if result.codec_name != "aac":
            raise RuntimeError(f"output codec verification failed: {result.codec_name}")
        if len(resolved) > 1 and len(result.chapters) != len(resolved):
            raise RuntimeError(f"output chapter verification failed: expected {len(resolved)}, got {len(result.chapters)}")
        if preserve_container and len(result.chapters) != len(probes[0].chapters):
            raise RuntimeError(
                "output chapter verification failed: "
                f"expected {len(probes[0].chapters)}, got {len(result.chapters)}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(staged, destination)
        except FileExistsError:
            raise FileExistsError(f"output already exists: {destination}") from None
        staged.unlink()
    return destination
