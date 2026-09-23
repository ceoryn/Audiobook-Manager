from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from audiobook_manager.database import SCHEMA_VERSION, StateDatabase
from audiobook_manager.models import ProbeResult, ScannedFile


class DatabaseTests(unittest.TestCase):
    def test_initializes_schema_and_round_trips_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "state" / "library.sqlite3"
            media = root / "book.mp3"
            media.touch()
            stat = media.stat()
            result = ProbeResult(12.5, "mp3", "mp3", 64000, 44100, 2, {"title": "Book"})
            item = ScannedFile(media, Path("book.mp3"), stat.st_size, stat.st_mtime_ns, result)
            with StateDatabase(database_path) as database:
                database.store(item)
                cached = database.cached_probe(media, size=stat.st_size, modified_ns=stat.st_mtime_ns)
            self.assertIsNotNone(cached)
            self.assertEqual(cached[0], result)  # type: ignore[index]
            with closing(sqlite3.connect(database_path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, SCHEMA_VERSION)

    def test_changed_fingerprint_is_not_a_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            media = Path(directory) / "book.mp3"
            media.touch()
            stat = media.stat()
            item = ScannedFile(media, Path("book.mp3"), 0, stat.st_mtime_ns, None, "bad")
            with StateDatabase(path) as database:
                database.store(item)
                self.assertIsNone(database.cached_probe(media, size=1, modified_ns=stat.st_mtime_ns))

    def test_newer_schema_fails_with_explanation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                with StateDatabase(path):
                    pass

    def test_migrates_version_one_and_persists_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version-one.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "CREATE TABLE media_files(path TEXT PRIMARY KEY, size INTEGER NOT NULL, modified_ns INTEGER NOT NULL, probe_json TEXT, error TEXT, scanned_at TEXT NOT NULL)"
                )
                connection.execute("PRAGMA user_version = 1")
            with StateDatabase(path) as database:
                database.store_decision(
                    relationship_id="abc",
                    decision="confirm",
                    evidence_fingerprint="fingerprint",
                    group_key="Book",
                    note="reviewed",
                )
                decisions = database.decisions()
            self.assertEqual(decisions["abc"]["decision"], "confirm")
            self.assertEqual(decisions["abc"]["note"], "reviewed")

    def test_migrates_version_nine_without_losing_existing_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version-nine.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE existing_marker(value TEXT)")
                connection.execute("INSERT INTO existing_marker VALUES ('preserved')")
                connection.execute("PRAGMA user_version = 9")
                connection.commit()
            with StateDatabase(path) as database:
                self.assertEqual([], database.archive_imports(
                    Path(directory) / "source", Path(directory) / "output"
                ))
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual("preserved", connection.execute(
                    "SELECT value FROM existing_marker"
                ).fetchone()[0])
                self.assertEqual(10, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_rejects_invalid_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with StateDatabase(Path(directory) / "state.sqlite3") as database:
                with self.assertRaisesRegex(ValueError, "invalid decision"):
                    database.store_decision(
                        relationship_id="abc",
                        decision="delete",
                        evidence_fingerprint="fingerprint",
                        group_key="Book",
                    )

    def test_process_manifest_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with StateDatabase(path) as database:
                run_id = database.start_process_run(Path(directory) / "source", Path(directory) / "clean")
                database.store_detected_book(
                    run_id=run_id, book_id="book-1", state="identified",
                    source_fingerprint="abc", classification="multipart_conversion",
                    files=["01.mp3", "02.mp3"],
                    alternate_files=["complete.m4b"],
                    problem_files=["broken.mp3"],
                    evidence=[{"kind": "duration", "explanation": "parts are sequential"}],
                )
            with StateDatabase(path) as database:
                books = database.process_books(run_id)
            self.assertEqual(["01.mp3", "02.mp3"], books[0]["files"])
            self.assertEqual(["complete.m4b"], books[0]["alternate_files"])
            self.assertEqual(["broken.mp3"], books[0]["problem_files"])
            self.assertEqual("identified", books[0]["state"])

    def test_output_claim_has_one_owner_and_is_releasable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(
            Path(directory) / "state.sqlite3"
        ) as database:
            run_id = database.start_process_run(
                Path(directory) / "source", Path(directory) / "clean"
            )
            output = Path(directory) / "clean" / "Author" / "Book.m4b"
            self.assertIsNone(database.reserve_output_claim(run_id, "first", output))
            self.assertIsNone(database.reserve_output_claim(run_id, "first", output))
            self.assertEqual("first", database.reserve_output_claim(run_id, "second", output))
            database.release_output_claim(run_id, "first", output)
            self.assertIsNone(database.reserve_output_claim(run_id, "second", output))

    def test_only_explicitly_verified_prior_outputs_are_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(
            Path(directory) / "state.sqlite3"
        ) as database:
            source = Path(directory) / "source"
            output = Path(directory) / "clean" / "Author" / "Book.m4b"
            first_run = database.start_process_run(source, output.parent.parent)
            for book_id, fingerprint, verified in (
                ("automatic", "automatic-fingerprint", False),
                ("verified", "verified-fingerprint", True),
            ):
                database.store_detected_book(
                    run_id=first_run, book_id=book_id, state="identified",
                    source_fingerprint=fingerprint, classification="complete_m4b",
                    files=[f"{book_id}.m4b"], evidence=[],
                )
                metadata = {"title": "Book", "authors": ["Author"]}
                if verified:
                    metadata["_verified_output"] = True
                database.store_book_identification(
                    run_id=first_run, book_id=book_id, state="metadata_matched",
                    metadata=metadata, candidates=[], evidence=[], confidence=1,
                )
                database.update_book_state(
                    first_run, book_id, "complete", output_path=str(output)
                )
            second_run = database.start_process_run(source, output.parent.parent)
            self.assertEqual([], database.verified_outputs_for_source(
                "automatic-fingerprint", before_run_id=second_run
            ))
            reusable = database.verified_outputs_for_source(
                "verified-fingerprint", before_run_id=second_run
            )
            self.assertEqual("verified", reusable[0]["book_id"])
            self.assertTrue(reusable[0]["metadata"]["_verified_output"])

    def test_obsolete_detector_identity_is_superseded_but_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.sqlite3") as database:
            run_id = database.start_process_run(Path(directory) / "source", Path(directory) / "clean")
            for book_id in ("old tmpfiles identity", "canonical title"):
                database.store_detected_book(
                    run_id=run_id, book_id=book_id, state="quarantined",
                    source_fingerprint=book_id, classification="complete_m4b",
                    files=[f"{book_id}.m4b"], evidence=[],
                )
            self.assertEqual(1, database.supersede_missing_books(run_id, {"canonical title"}))
            books = {book["book_id"]: book for book in database.process_books(run_id)}
            self.assertEqual("superseded", books["old tmpfiles identity"]["state"])
            self.assertEqual({"quarantined": 1}, database.latest_process_status()["counts"])  # type: ignore[index]

    def test_rediscovery_hides_prior_outcomes_from_active_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.sqlite3") as database:
            run_id = database.start_process_run(Path(directory) / "source", Path(directory) / "clean")
            database.store_detected_book(run_id=run_id, book_id="old", state="quarantined",
                source_fingerprint="old", classification="complete_m4b", files=["old.m4b"], evidence=[])
            self.assertEqual(1, database.begin_rediscovery(run_id))
            self.assertEqual({}, database.latest_process_status()["counts"])  # type: ignore[index]

    def test_worker_connection_opens_while_wal_writer_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with StateDatabase(path) as database:
                run_id = database.start_process_run(
                    Path(directory) / "source", Path(directory) / "clean"
                )

            with closing(sqlite3.connect(path)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "INSERT INTO process_events(run_id, level, event) VALUES (?, 'info', 'held')",
                    (run_id,),
                )
                with StateDatabase(path) as worker:
                    self.assertEqual(run_id, worker.process_run(run_id)["id"])  # type: ignore[index]

    def test_transaction_retries_transient_database_locks(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(
            Path(directory) / "state.sqlite3"
        ) as database:
            attempts = 0

            def temporarily_locked(connection: sqlite3.Connection) -> int:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise sqlite3.OperationalError("database is locked")
                return int(connection.execute("SELECT 42").fetchone()[0])

            self.assertEqual(42, database._write(temporarily_locked))
            self.assertEqual(3, attempts)

    def test_transaction_does_not_hide_non_locking_operational_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(
            Path(directory) / "state.sqlite3"
        ) as database:
            with self.assertRaisesRegex(sqlite3.OperationalError, "no such table"):
                database._write(
                    lambda connection: connection.execute("INSERT INTO missing_table VALUES (1)")
                )


if __name__ == "__main__":
    unittest.main()
