from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from audiobook_manager.archive_imports import sync_archive_imports
from audiobook_manager.database import StateDatabase
from audiobook_manager.engine import discover_run
from audiobook_manager.output import plan_output
from audiobook_manager.probe import probe_media
from scripts.apply_missing_source_import import import_archive, import_loose
from scripts.plan_missing_source_import import archive_book


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class MissingSourceImportTests(unittest.TestCase):
    def test_archive_import_is_registered_and_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            archive = source / "book.zab"
            cover_source = root / "cover.jpg"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                "color=c=red:s=32x32:d=1", "-frames:v", "1", "-update", "1",
                str(cover_source),
            ], check=True)
            members = []
            with zipfile.ZipFile(archive, "w") as zipped:
                for number in (1, 2):
                    track = root / f"track-{number}.mp3"
                    subprocess.run([
                        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={number * 220}:duration=0.7", "-c:a", "libmp3lame",
                        "-metadata", "artist=Example Author", "-metadata",
                        "album=Example Series 1 - Example Book", "-metadata",
                        f"track={number}", "-metadata", f"title=Track {number}",
                        "-metadata", "comment=Read by Example Narrator", "-metadata",
                        "publisher=Example Publisher", str(track),
                    ], check=True)
                    name = f"Example Book/Track {number:02d}.mp3"
                    zipped.write(track, name)
                    info = zipped.getinfo(name)
                    members.append({"name": name, "encrypted": False,
                                    "crc32": info.CRC, "size": info.file_size})
                zipped.write(cover_source, "Example Book/cover.jpg")
            stat = archive.stat()
            operation = archive_book({"path": str(archive), "members": members,
                                      "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}, output)
            self.assertEqual("Example Narrator", operation["metadata"]["narrator"])
            self.assertEqual("Example Publisher", operation["metadata"]["publisher"])
            self.assertIsNotNone(operation["cover"])
            with StateDatabase(root / "state.sqlite3") as database:
                run_id = database.start_process_run(source, output)
                imported = import_archive(operation, source_root=source, output_root=output,
                                          database=database)
                self.assertTrue(imported.is_file())
                self.assertTrue((imported.parent / "cover.jpg").is_file())
                self.assertEqual(2, len(probe_media(imported).chapters))
                sync_archive_imports(database, run_id=run_id, source=source, destination=output)
                self.assertEqual("complete", database.process_books(run_id)[0]["state"])
                database.begin_rediscovery(run_id)
                sync_archive_imports(database, run_id=run_id, source=source, destination=output)
                self.assertEqual("complete", database.process_books(run_id)[0]["state"])
                imported_cover = imported.parent / "cover.jpg"
                original_cover = imported_cover.read_bytes()
                imported_cover.write_bytes(b"\xff\xd8\xffdamaged")
                sync_archive_imports(database, run_id=run_id, source=source, destination=output)
                self.assertEqual("quarantined", database.process_books(run_id)[0]["state"])
                imported_cover.write_bytes(original_cover)
                sync_archive_imports(database, run_id=run_id, source=source, destination=output)
                self.assertEqual("complete", database.process_books(run_id)[0]["state"])
                os.utime(archive, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
                sync_archive_imports(database, run_id=run_id, source=source, destination=output)
                book = database.process_books(run_id)[0]
                self.assertEqual("quarantined", book["state"])
                self.assertIn("source archive changed", book["failure"])
                self.assertTrue(imported.is_file())
                database.update_process_run(run_id, "complete")
            next_run = discover_run(source, output, root / "state.sqlite3", identify=False)
            self.assertNotEqual(run_id, next_run)
            with StateDatabase(root / "state.sqlite3") as database:
                self.assertEqual("quarantined", database.process_books(next_run)[0]["state"])

    def test_extensionless_aac_import_uses_normal_detector_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            book = source / "Example Book"
            book.mkdir(parents=True)
            output.mkdir()
            chapters = root / "chapters.ffmeta"
            chapters.write_text(
                ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=One\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\ntitle=Two\n"
            )
            media = book / "Example Book"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=1", "-f", "ffmetadata", "-i",
                str(chapters), "-map", "0:a:0", "-map_chapters", "1",
                "-c:a", "aac", "-metadata", "title=Example Book", "-metadata",
                "artist=Example Author", "-f", "mp4", str(media),
            ], check=True)
            metadata = {"title": "Example Book", "authors": ["Example Author"]}
            probe = probe_media(media)
            stat = media.stat()
            operation = {"source": str(media), "output": str(plan_output(output, metadata).audio),
                         "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                         "metadata": metadata, "expected_duration_seconds": probe.duration_seconds,
                         "expected_chapters": len(probe.chapters)}
            with StateDatabase(root / "state.sqlite3") as database:
                run_id = database.start_process_run(source, output)
                imported = import_loose(operation, source_root=source, output_root=output,
                                        database=database, run_id=run_id)
                self.assertTrue(imported.is_file())
                self.assertEqual(2, len(probe_media(imported).chapters))
                records = database.process_books(run_id)
                self.assertEqual(1, len(records))
                self.assertEqual("complete", records[0]["state"])
                self.assertEqual(["Example Book/Example Book"], records[0]["files"])


if __name__ == "__main__":
    unittest.main()
