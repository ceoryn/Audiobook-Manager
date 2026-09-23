from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from audiobook_manager.database import StateDatabase
from scripts.apply_targeted_repair import execute, validate_plan
from scripts.plan_targeted_repair import build_plan


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class TargetedRepairTests(unittest.TestCase):
    def test_curated_title_archives_superseded_output_and_cover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            source_file = source / "Author" / "Book" / "01.mp3"
            source_file.parent.mkdir(parents=True)
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=1", "-c:a", "libmp3lame", str(source_file),
            ], check=True)
            old = output / "Author" / "BOOK (PUBLISHER)" / "Author - BOOK (PUBLISHER).m4b"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"incomplete generated output")
            cover = old.parent / "cover.jpg"
            cover.write_bytes(b"cover")
            database_path = root / "state.sqlite3"
            book_id = "author — book"
            metadata = {"title": "BOOK (PUBLISHER)", "authors": ["Author"]}
            with StateDatabase(database_path) as database:
                run_id = database.start_process_run(source, output)
                database.store_detected_book(
                    run_id=run_id, book_id=book_id, state="identified",
                    source_fingerprint="fixture", classification="convert_single",
                    files=["Author/Book/01.mp3"], evidence=[],
                )
                database.store_book_identification(
                    run_id=run_id, book_id=book_id, state="metadata_matched",
                    metadata=metadata, candidates=[], evidence=[], confidence=1,
                )
                database.update_book_state(run_id, book_id, "complete", output_path=str(old))
                database.update_process_run(run_id, "complete")
            corrections = root / "corrections.json"
            corrections.write_text(json.dumps({"expected_run_id": run_id, "repairs": [{
                "book_id": book_id, "expected_states": ["complete"],
                "metadata": metadata, "selection": ["Author/Book/01.mp3"],
                "evidence": ["generated fixture has a verified complete source"],
            }]}))
            overrides = root / "overrides.json"
            overrides.write_text(json.dumps({book_id: {"title": "Book"}}))
            plan = build_plan(database_path, corrections, overrides)
            operation = plan["repair_operations"][0]
            self.assertEqual(1, plan["summary"]["curated_title_overrides"])
            self.assertEqual(1, plan["summary"]["blocking_outputs_to_quarantine"])
            self.assertEqual(str(old), operation["prior_output_quarantine"]["source"])
            self.assertEqual("Book", operation["metadata"]["title"])
            validate_plan(plan)
            result, log = execute(plan, approve=True)
            self.assertEqual(1, result["operations_complete"])
            self.assertEqual(0, result["operations_failed"])
            self.assertIsNotNone(log)
            self.assertFalse(old.exists())
            self.assertFalse(old.parent.exists())
            self.assertTrue(Path(operation["output"]).is_file())
            archived = Path(operation["prior_output_quarantine"]["destination"])
            self.assertTrue(archived.is_file())
            self.assertTrue((archived.parent / "cover.jpg").is_file())
            with StateDatabase(database_path) as database:
                book = database.process_books(run_id)[0]
                self.assertEqual("complete", book["state"])
                self.assertEqual(str(Path(operation["output"])), book["output_path"])


if __name__ == "__main__":
    unittest.main()
