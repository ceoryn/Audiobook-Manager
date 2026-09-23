from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from audiobook_manager.probe import probe_media
from audiobook_manager.database import StateDatabase
from scripts.repair_output_hygiene import apply_plan, build_plan, reconcile_database


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class OutputHygieneRepairTests(unittest.TestCase):
    def test_retags_chaptered_book_then_quarantines_duplicates_and_cover_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            output_root = root / "output"
            series_source = source_root / "Series Source"
            book_source = series_source / "Series - Book 1"
            book_source.mkdir(parents=True)
            output_root.mkdir()
            metadata = root / "chapters.ffmeta"
            metadata.write_text(
                ";FFMETADATA1\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=500\ntitle=One\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\ntitle=Two\n",
                encoding="utf-8",
            )
            source = book_source / "Series - Book 1.m4b"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-f", "lavfi", "-i", "testsrc=size=320x320:duration=1",
                "-f", "ffmetadata", "-i", str(metadata),
                "-map", "0:a:0", "-map", "1:v:0", "-map_metadata", "2", "-map_chapters", "2",
                "-c:a", "aac", "-c:v", "mjpeg", "-frames:v", "1",
                "-disposition:v:0", "attached_pic", "-metadata", "artist=著者", str(source),
            ], check=True)

            old = output_root / "著者" / "Series" / "01 - Old"
            old.mkdir(parents=True)
            shutil.copy2(source, old / "Old.m4b")
            (old / "cover.jpg").write_bytes(b"old-cover")
            cover_only = output_root / "Author" / "Book without audio"
            cover_only.mkdir(parents=True)
            (cover_only / "cover.jpg").write_bytes(b"orphan-cover")

            database = root / "state.sqlite3"
            with StateDatabase(database) as state:
                run_id = state.start_process_run(source_root, output_root)
                state.store_detected_book(
                    run_id=run_id, book_id="fixture", state="identified",
                    source_fingerprint="fixture-fingerprint", classification="complete_m4b",
                    files=[str(source.relative_to(source_root))], evidence=[],
                )
                state.store_book_identification(
                    run_id=run_id, book_id="fixture", state="metadata_matched",
                    metadata={"title": "Old", "authors": ["著者"]}, candidates=[],
                    evidence=[], confidence=0.9,
                )
                state.update_book_state(
                    run_id, "fixture", "complete", output_path=str(old / "Old.m4b")
                )
                state.update_process_run(run_id, "complete", {"books_successfully_processed": 1})

            plan = build_plan(Namespace(
                source_root=source_root,
                output_root=output_root,
                database=database,
                source_series_directory="Series Source",
                canonical_author="Author",
                canonical_series="Series (Light Novel)",
                series_marker="Series",
                first_volume=1,
                last_volume=1,
            ))
            self.assertEqual(1, plan["summary"]["canonical_books_to_create"])
            self.assertEqual(1, plan["summary"]["obsolete_series_directories_to_quarantine"])
            self.assertEqual(1, plan["summary"]["cover_only_directories_to_quarantine"])

            result, log = apply_plan(plan)
            canonical = Path(result["canonical_outputs"][0])
            probe = probe_media(canonical)
            self.assertEqual(2, len(probe.chapters))
            self.assertEqual("Author", probe.tags.get("artist"))
            self.assertTrue((canonical.parent / "cover.jpg").is_file())
            self.assertFalse(old.exists())
            self.assertFalse(cover_only.exists())
            self.assertEqual(2, result["directories_quarantined"])
            self.assertTrue(log.is_file())
            backup = root / "before-reconcile.sqlite3"
            report = root / "reconcile.json"
            reconciliation = reconcile_database(
                plan, backup_path=backup, report_path=report
            )
            self.assertEqual(1, reconciliation["records_updated"])
            self.assertTrue(backup.is_file())
            self.assertTrue(report.is_file())
            with StateDatabase(database) as state:
                repaired = state.process_books(run_id)[0]
            self.assertEqual("complete", repaired["state"])
            self.assertEqual(str(canonical), repaired["output_path"])
            self.assertEqual(["Author"], repaired["metadata"]["authors"])
            self.assertTrue(repaired["metadata"]["_verified_output"])


if __name__ == "__main__":
    unittest.main()
