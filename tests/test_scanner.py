from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from audiobook_manager.models import ProbeResult
from audiobook_manager.probe import ProbeError
from audiobook_manager.scanner import discover_media, scan_library


def successful_probe(_path: Path) -> ProbeResult:
    return ProbeResult(10.0, "mp3", "mp3", 64000, 44100, 2, {"title": "Fixture"})


class ScannerTests(unittest.TestCase):
    def test_discovers_mp4_and_extensionless_audio_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "book").write_bytes(b"\x00\x00\x00\x20ftypM4A ")
            (root / "track.mp4").touch()
            (root / "notes").write_bytes(b"not media")
            (root / "readme.txt").write_bytes(b"ID3")
            self.assertEqual(["book", "track.mp4"], [p.name for p in discover_media(root)])

    def test_discovers_supported_files_recursively_and_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Book" / "CD2").mkdir(parents=True)
            (root / "Book" / "CD1").mkdir()
            (root / "Book" / "CD2" / "02.M4B").touch()
            (root / "Book" / "CD1" / "01.mp3").touch()
            (root / "ignore.txt").touch()
            found = [path.relative_to(root).as_posix() for path in discover_media(root)]
            self.assertEqual(found, ["Book/CD1/01.mp3", "Book/CD2/02.M4B"])

    def test_one_probe_failure_does_not_abort_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "good.mp3").touch()
            (root / "bad.m4b").touch()

            def probe(path: Path) -> ProbeResult:
                if path.name == "bad.m4b":
                    raise ProbeError("corrupt fixture")
                return successful_probe(path)

            results = scan_library(root, probe=probe)
            self.assertEqual(len(results), 2)
            self.assertEqual(sum(item.error is not None for item in results), 1)
            self.assertEqual(sum(item.probe is not None for item in results), 1)

    def test_missing_library_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a directory"):
            scan_library(Path("/definitely/not/a/library"), probe=successful_probe)

    def test_rejects_invalid_worker_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "at least 1"):
                scan_library(Path(directory), probe=successful_probe, workers=0)


if __name__ == "__main__":
    unittest.main()
