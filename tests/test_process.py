from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from audiobook_manager.database import StateDatabase
from audiobook_manager.engine import _local_metadata, _metadata_is_usable, process_library
from audiobook_manager.executor import (
    ExistingOutputConflict,
    OutputClaimConflict,
    _prune_empty_output_directories,
    execute_book,
    failure_detail,
    reuse_verified_output,
)
from audiobook_manager.output import plan_output
from audiobook_manager.probe import probe_media


class ParallelProcessTests(unittest.TestCase):
    def test_approved_recovery_selection_survives_rediscovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "output"
            source.mkdir()
            destination.mkdir()
            output = destination / "Recovered.m4b"
            output.write_bytes(b"generated fixture; media validator is mocked")
            parts = ["disc1.mp3", "disc2.mp3"]
            metadata = {"title": "Recovered", "authors": ["Example Author"],
                        "_verified_output": True, "_approved_source_selection": parts}
            with StateDatabase(root / "state.db") as database:
                old = database.start_process_run(source, destination)
                database.store_detected_book(run_id=old, book_id="book", state="identified",
                    source_fingerprint="unchanged", classification="combine_components",
                    files=parts, alternate_files=["broken-alternate.mp3"], evidence=[])
                database.store_book_identification(run_id=old, book_id="book", state="complete",
                    metadata=metadata, candidates=[], evidence=[], confidence=1)
                database.update_book_state(old, "book", "complete", output_path=str(output))
                database.update_process_run(old, "complete")
                new = database.start_process_run(source, destination)
                database.store_detected_book(run_id=new, book_id="book", state="identified",
                    source_fingerprint="unchanged", classification="convert_single",
                    files=["broken-alternate.mp3"], alternate_files=parts, evidence=[])
                kwargs = dict(database=database, run_id=new, book_id="book", source=source,
                              destination=destination, files=["broken-alternate.mp3"])
                with patch("audiobook_manager.executor._existing_output_is_compatible",
                           return_value=(True, "validated")) as validate:
                    self.assertIsNone(reuse_verified_output(**kwargs, source_fingerprint="changed",
                                      known_files=["broken-alternate.mp3", *parts]))
                    self.assertIsNone(reuse_verified_output(**kwargs, source_fingerprint="unchanged",
                                      known_files=["broken-alternate.mp3"]))
                    self.assertIsNone(reuse_verified_output(**kwargs, source_fingerprint="unchanged"))
                    validate.assert_not_called()
                    actual = reuse_verified_output(**kwargs, source_fingerprint="unchanged",
                                                   known_files=["broken-alternate.mp3", *parts])
                    validate.assert_called_once_with(output, source, parts, metadata)
                self.assertEqual(str(output), actual)
                current = database.process_books(new)[0]
                self.assertEqual(parts, current["files"])
                self.assertEqual(["broken-alternate.mp3"], current["alternate_files"])
                self.assertEqual("complete", current["state"])

    def test_approved_selection_cannot_escape_source_or_bypass_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "output"
            source.mkdir()
            destination.mkdir()
            output = destination / "Recovered.m4b"
            output.write_bytes(b"generated fixture")
            for selection in (["../outside.mp3"], ["/outside.mp3"], ["part.mp3", "part.mp3"],
                              [], "part.mp3", ["part.mp3"]):
                with self.subTest(selection=selection), StateDatabase(root / "state.db") as database:
                    old = database.start_process_run(source, destination)
                    metadata = {"title": "Recovered", "authors": ["Example Author"],
                                "_verified_output": True, "_approved_source_selection": selection}
                    database.store_detected_book(run_id=old, book_id="old", state="identified",
                        source_fingerprint="unchanged", classification="convert_single", files=["part.mp3"], evidence=[])
                    database.store_book_identification(run_id=old, book_id="old", state="complete",
                        metadata=metadata, candidates=[], evidence=[], confidence=1)
                    database.update_book_state(old, "old", "complete", output_path=str(output))
                    database.update_process_run(old, "complete")
                    new = database.start_process_run(source, destination)
                    database.store_detected_book(run_id=new, book_id="new", state="identified",
                        source_fingerprint="unchanged", classification="convert_single", files=["part.mp3"], evidence=[])
                    with patch("audiobook_manager.executor._existing_output_is_compatible",
                               return_value=(False, "invalid output")):
                        result = reuse_verified_output(database, run_id=new, book_id="new",
                            source_fingerprint="unchanged", source=source, destination=destination,
                            files=["part.mp3"], known_files=["part.mp3", "../outside.mp3", "/outside.mp3"])
                    self.assertIsNone(result)
                    self.assertEqual("identified", database.process_books(new)[0]["state"])

    def test_local_metadata_keeps_unmatched_book_available_for_review(self) -> None:
        metadata = _local_metadata({"title": "Unlisted Book", "authors": ["Local Author"],
                                    "series": "Local Series", "series_position": "2"})
        self.assertEqual("local", metadata["_metadata_source"])
        self.assertTrue(metadata["_needs_metadata_review"])
        self.assertEqual(["Local Author"], metadata["authors"])

    def test_local_metadata_preserves_graphic_audio_edition_identity(self) -> None:
        metadata = _local_metadata({
            "title": "Warbreaker", "authors": ["Brandon Sanderson"],
            "edition": "GraphicAudio",
        })
        self.assertEqual("Warbreaker (GraphicAudio)", metadata["title"])
        self.assertEqual("GraphicAudio", metadata["edition"])

    def test_local_metadata_removes_production_and_narrator_credits(self) -> None:
        metadata = _local_metadata({
            "title": "Book", "authors": ["GraphicAudio [Brandon Sanderson]"],
            "narrators": ["Narrator Name"],
        })
        self.assertEqual(["Brandon Sanderson"], metadata["authors"])
        metadata = _local_metadata({
            "title": "Book", "authors": ["Written By J. K. Rowling, Narrated By Jim Dale;"],
            "narrators": ["Jim Dale"],
        })
        self.assertEqual(["J. K. Rowling"], metadata["authors"])

    def test_local_metadata_splits_contributors_and_strips_pen_name_note(self) -> None:
        metadata = _local_metadata({
            "title": "Book", "authors": ["Dean Koontz (writing as Owen West)"]})
        self.assertEqual(["Dean Koontz"], metadata["authors"])
        metadata = _local_metadata({
            "title": "Book", "authors": ["Margaret Weis and Tracy Hickman"]})
        self.assertEqual(["Margaret Weis", "Tracy Hickman"], metadata["authors"])

    def test_local_metadata_repairs_inverted_person_name(self) -> None:
        metadata = _local_metadata({"title": "Book", "authors": ["Brooks, Terry"]})
        self.assertEqual(["Terry Brooks"], metadata["authors"])

    def test_large_ffmpeg_failure_is_bounded_but_keeps_both_ends(self) -> None:
        detail = failure_detail(RuntimeError("start" + ("x" * 10000) + "end"))
        self.assertLessEqual(len(detail), 4100)
        self.assertTrue(detail.startswith("start"))
        self.assertTrue(detail.endswith("end"))

    def test_unknown_author_is_not_automatically_usable(self) -> None:
        self.assertFalse(_metadata_is_usable({"title": "Book", "authors": ["Unknown Author"]}))
        self.assertTrue(_metadata_is_usable({"title": "Book", "authors": ["Known Author"]}))

    def test_author_name_is_not_accepted_as_a_book_title(self) -> None:
        self.assertFalse(_metadata_is_usable({"title": "J. R. Ward", "authors": ["J.R. Ward"]}))

    def test_series_or_collection_credit_is_not_treated_as_local_author(self) -> None:
        self.assertFalse(_metadata_is_usable(
            {"title": "Skybowl", "authors": ["Dragon Star"], "series": "Dragon Star"}))
        self.assertFalse(_metadata_is_usable(
            {"title": "Mentats of Dune", "authors": ["Great Schools of Dune"]}))
        self.assertFalse(_metadata_is_usable(
            {"title": "Eon", "authors": ["Greg Bear-The Way[1-3]"]}))

    def test_prunes_only_empty_conversion_footprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); leaf = root / "Author" / "Book"; leaf.mkdir(parents=True)
            removed = _prune_empty_output_directories(leaf, root)
            self.assertEqual([leaf, leaf.parent], removed)
            self.assertTrue(root.exists())

    def test_conversion_queue_runs_multiple_books_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; destination = root / "clean"
            source.mkdir(); destination.mkdir(); database_path = root / "state.db"
            with StateDatabase(database_path) as database:
                run_id = database.start_process_run(source, destination)
                for number in range(4):
                    book_id = f"book-{number}"
                    database.store_detected_book(run_id=run_id, book_id=book_id, state="identified",
                        source_fingerprint=book_id, classification="complete_m4b",
                        files=[f"{book_id}.m4b"], evidence=[])
                    database.store_book_identification(run_id=run_id, book_id=book_id,
                        state="metadata_matched", metadata={"title": book_id, "authors": ["Author"]},
                        candidates=[], evidence=[], confidence=0.9)
            lock = threading.Lock(); active = 0; maximum = 0
            def fake_execute(database: StateDatabase, fake_run_id: int, book_id: str) -> str:
                nonlocal active, maximum
                with lock:
                    active += 1; maximum = max(maximum, active)
                time.sleep(0.05)
                database.update_book_state(fake_run_id, book_id, "complete", output_path=f"/{book_id}.m4b")
                with lock: active -= 1
                return f"/{book_id}.m4b"
            with patch("audiobook_manager.engine.discover_run", return_value=run_id), \
                 patch("audiobook_manager.engine.execute_book", side_effect=fake_execute):
                summary = process_library(source, destination, database_path, conversion_workers=4)
            self.assertGreaterEqual(maximum, 2)
            self.assertEqual(4, summary["books_successfully_processed"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ProcessTests(unittest.TestCase):
    def test_explicitly_verified_output_is_reused_without_rebuilding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; destination = root / "clean"
            source.mkdir(); destination.mkdir(); database_path = root / "state.db"
            source_audio = source / "input.m4b"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.25", "-metadata", "title=Stable Book",
                "-metadata", "album=Stable Book", "-metadata", "artist=Stable Author",
                "-c:a", "aac", str(source_audio)], check=True)
            metadata = {"title": "Stable Book", "authors": ["Stable Author"],
                        "_verified_output": True}
            output = plan_output(destination, metadata).audio
            output.parent.mkdir(parents=True)
            shutil.copy2(source_audio, output)
            with StateDatabase(database_path) as database:
                first_run = database.start_process_run(source, destination)
                database.store_detected_book(run_id=first_run, book_id="old", state="identified",
                    source_fingerprint="unchanged", classification="complete_m4b",
                    files=[source_audio.name], evidence=[])
                database.store_book_identification(run_id=first_run, book_id="old",
                    state="metadata_matched", metadata=metadata, candidates=[], evidence=[], confidence=1)
                database.update_book_state(first_run, "old", "complete", output_path=str(output))
                database.update_process_run(first_run, "complete")
                second_run = database.start_process_run(source, destination)
                database.store_detected_book(run_id=second_run, book_id="new", state="identified",
                    source_fingerprint="unchanged", classification="complete_m4b",
                    files=[source_audio.name], evidence=[])
                reused = reuse_verified_output(database, run_id=second_run, book_id="new",
                    source_fingerprint="unchanged", source=source, destination=destination,
                    files=[source_audio.name], require_latin=True)
                current = database.process_books(second_run)[0]
            self.assertEqual(str(output), reused)
            self.assertEqual("complete", current["state"])
            self.assertEqual(metadata, current["metadata"])

    def test_existing_wrong_output_is_preserved_and_book_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; destination = root / "clean"
            source.mkdir(); destination.mkdir(); database_path = root / "state.db"
            source_audio = source / "input.m4b"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.25", "-metadata", "title=Test Book",
                "-metadata", "album=Test Book", "-metadata", "artist=Test Author",
                "-c:a", "aac", str(source_audio)], check=True)
            metadata = {"title": "Test Book", "authors": ["Test Author"]}
            existing = plan_output(destination, metadata).audio
            existing.parent.mkdir(parents=True)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=660:duration=0.25", "-metadata", "title=Different Book",
                "-metadata", "album=Different Book", "-metadata", "artist=Test Author",
                "-c:a", "aac", str(existing)], check=True)
            original_hash = hashlib.sha256(existing.read_bytes()).hexdigest()
            with StateDatabase(database_path) as database:
                run_id = database.start_process_run(source, destination)
                database.store_detected_book(run_id=run_id, book_id="test", state="identified",
                    source_fingerprint="test", classification="complete_m4b",
                    files=[source_audio.name], evidence=[])
                database.store_book_identification(run_id=run_id, book_id="test",
                    state="metadata_matched", metadata=metadata, candidates=[], evidence=[],
                    confidence=0.9)
                with self.assertRaises(ExistingOutputConflict):
                    execute_book(database, run_id, "test")
                book = database.process_books(run_id)[0]
            self.assertEqual("quarantined", book["state"])
            self.assertIn("embedded title", str(book["failure"]))
            self.assertEqual(original_hash, hashlib.sha256(existing.read_bytes()).hexdigest())
            self.assertTrue(source_audio.exists())

    def test_existing_output_that_lost_source_chapters_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; destination = root / "clean"
            source.mkdir(); destination.mkdir(); database_path = root / "state.db"
            chapter_file = root / "chapters.ffmeta"
            chapter_file.write_text(
                ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=125\n"
                "title=One\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=125\nEND=250\ntitle=Two\n",
                encoding="utf-8",
            )
            source_audio = source / "input.m4b"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.25", "-f", "ffmetadata", "-i",
                str(chapter_file), "-map_metadata", "1", "-map_chapters", "1",
                "-metadata", "title=Test Book", "-metadata", "album=Test Book",
                "-metadata", "artist=Test Author", "-c:a", "aac", str(source_audio),
            ], check=True)
            metadata = {"title": "Test Book", "authors": ["Test Author"]}
            existing = plan_output(destination, metadata).audio
            existing.parent.mkdir(parents=True)
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.25", "-metadata", "title=Test Book",
                "-metadata", "album=Test Book", "-metadata", "artist=Test Author",
                "-c:a", "aac", str(existing),
            ], check=True)
            with StateDatabase(database_path) as database:
                run_id = database.start_process_run(source, destination)
                database.store_detected_book(
                    run_id=run_id, book_id="test", state="identified",
                    source_fingerprint="test", classification="complete_m4b",
                    files=[source_audio.name], evidence=[],
                )
                database.store_book_identification(
                    run_id=run_id, book_id="test", state="metadata_matched",
                    metadata=metadata, candidates=[], evidence=[], confidence=1,
                )
                with self.assertRaises(ExistingOutputConflict):
                    execute_book(database, run_id, "test")
                repaired = database.process_books(run_id)[0]
            self.assertEqual("quarantined", repaired["state"])
            self.assertIn("chapter count", repaired["failure"])

    def test_two_books_cannot_claim_the_same_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; destination = root / "clean"
            source.mkdir(); destination.mkdir(); database_path = root / "state.db"
            metadata = {"title": "Shared Title", "authors": ["Test Author"]}
            with StateDatabase(database_path) as database:
                run_id = database.start_process_run(source, destination)
                database.store_detected_book(run_id=run_id, book_id="second", state="identified",
                    source_fingerprint="second", classification="complete_m4b",
                    files=["missing.m4b"], evidence=[])
                database.store_book_identification(run_id=run_id, book_id="second",
                    state="metadata_matched", metadata=metadata, candidates=[], evidence=[],
                    confidence=0.9)
                output = plan_output(destination, metadata).audio
                self.assertIsNone(database.reserve_output_claim(run_id, "first", output))
                with self.assertRaises(OutputClaimConflict):
                    execute_book(database, run_id, "second")
                book = database.process_books(run_id)[0]
            self.assertEqual("quarantined", book["state"])
            self.assertIn("already claimed", str(book["failure"]))
            self.assertFalse(output.exists())

    def test_clean_canonical_m4b_is_copied_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; destination = root / "clean"
            relative = Path("Test Author/Test Book/Test Author - Test Book.m4b")
            original = source / relative; original.parent.mkdir(parents=True)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25",
                "-metadata", "title=Test Book", "-metadata", "album=Test Book",
                "-metadata", "artist=Test Author", "-c:a", "aac", str(original)], check=True)
            database_path = root / "state.db"
            with StateDatabase(database_path) as database:
                run_id = database.start_process_run(source, destination)
                database.store_detected_book(run_id=run_id, book_id="test", state="identified",
                    source_fingerprint="test", classification="complete_m4b", files=[str(relative)], evidence=[])
                database.store_book_identification(run_id=run_id, book_id="test", state="metadata_matched",
                    metadata={"title": "Test Book", "authors": ["Test Author"]}, candidates=[],
                    evidence=[], confidence=0.9)
                output = Path(execute_book(database, run_id, "test"))
                events = database.process_log(run_id)
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),
                             hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertIn("clean_source_copied", {event["event"] for event in events})

    def test_hands_off_multipart_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; destination = root / "clean"
            source.mkdir(); destination.mkdir(); database_path = root / "state.db"
            for number, frequency in ((1, 440), (2, 660)):
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency={frequency}:duration=0.25", "-metadata", "album=Synthetic Book",
                    "-metadata", "artist=Test Author", "-c:a", "libmp3lame", str(source / f"{number:02}.mp3")], check=True)
            with StateDatabase(database_path) as database:
                database.cache_metadata("open_library", "Synthetic Book Test Author", [{
                    "provider": "Open Library", "provider_id": "OLTEST", "title": "Synthetic Book",
                    "authors": ["Test Author"], "series": "Fixture Series", "series_position": 1,
                    "cover_url": "https://example.com/not-an-approved-cover.jpg"}])
            summary = process_library(source, destination, database_path)
            output = destination / "Test Author/Fixture Series/01 - Synthetic Book/Test Author - Synthetic Book.m4b"
            self.assertEqual(1, summary["books_successfully_processed"])
            self.assertEqual(2, len(probe_media(output).chapters))
            self.assertTrue((source / "01.mp3").exists())
            with StateDatabase(database_path) as database:
                self.assertIn("cover_skipped", {event["event"] for event in database.process_log(1)})
