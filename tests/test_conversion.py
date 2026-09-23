from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from audiobook_manager.conversion import convert_to_m4b
from audiobook_manager.probe import is_aac_container, probe_media
from audiobook_manager.executor import _existing_output_is_compatible


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class MixedFormatConversionTests(unittest.TestCase):
    def test_id3_prefixed_wave_mp3_is_recovered_and_converted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            wave = root / "wrapped.wav"
            source = source_root / "wrapped.mp3"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-ar", "44100", "-ac", "2",
                    "-c:a", "libmp3lame", "-f", "wav", str(wave),
                ],
                check=True,
            )
            source.write_bytes(b"ID3" + (b"\0" * 125) + wave.read_bytes())
            destination = output_root / "Author" / "Book" / "Author - Book.m4b"
            result = convert_to_m4b(
                inputs=[source],
                library_root=source_root,
                output_root=output_root,
                destination=destination,
                metadata={"title": "Book", "authors": ["Author"]},
            )
            probe = probe_media(result)
            self.assertEqual("aac", probe.codec_name)
            self.assertAlmostEqual(1.0, probe.duration_seconds or 0, delta=0.1)

    def test_copying_existing_m4b_preserves_chapters_while_retagging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            output_root = root / "output"
            source_root.mkdir()
            source = source_root / "chaptered.m4b"
            chapters = root / "chapters.ffmeta"
            chapters.write_text(
                ";FFMETADATA1\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=One\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\ntitle=Two\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-f", "ffmetadata", "-i",
                    str(chapters), "-map", "0:a:0", "-map_metadata", "1",
                    "-map_chapters", "1", "-c:a", "aac", "-metadata",
                    "artist=くまなの", str(source),
                ],
                check=True,
            )
            destination = output_root / "Kumanano" / "Book" / "Kumanano - Book.m4b"
            result = convert_to_m4b(
                inputs=[source],
                library_root=source_root,
                output_root=output_root,
                destination=destination,
                metadata={"title": "Book", "authors": ["Kumanano"]},
                copy_audio=True,
            )
            probe = probe_media(result)
            self.assertEqual("aac", probe.codec_name)
            self.assertEqual(2, len(probe.chapters))
            self.assertEqual("Book", probe.tags.get("title"))
            self.assertEqual("Kumanano", probe.tags.get("artist"))
            extensionless = source.with_suffix("")
            source.rename(extensionless)
            self.assertTrue(is_aac_container(probe_media(extensionless)))
            compatible, detail = _existing_output_is_compatible(
                result, source_root, [extensionless.name],
                {"title": "Book", "authors": ["Kumanano"]},
            )
            self.assertTrue(compatible, detail)

    def test_mixed_aac_and_mp3_parts_are_concatenated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            first = source / "part-1.m4b"
            second = source / "part-2.mp3"
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.5", "-ar", "44100", "-ac", "1",
                    "-c:a", "aac", str(first),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=660:duration=0.5", "-ar", "48000", "-ac", "2",
                    "-c:a", "libmp3lame", str(second),
                ],
                check=True,
            )
            destination = output / "Author" / "Book" / "Author - Book.m4b"
            result = convert_to_m4b(
                inputs=[first, second],
                library_root=source,
                output_root=output,
                destination=destination,
                metadata={"title": "Book", "authors": ["Author"]},
            )
            probe = probe_media(result)
            self.assertEqual("aac", probe.codec_name)
            self.assertAlmostEqual(1.0, probe.duration_seconds or 0, delta=0.1)
            self.assertEqual(2, len(probe.chapters))


if __name__ == "__main__":
    unittest.main()
