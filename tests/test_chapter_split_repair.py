from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from audiobook_manager.probe import probe_media
from scripts.apply_chapter_split_repair import apply_plan
from scripts.remove_empty_output_directories import (
    empty_output_directories,
    remove_empty_directories,
)
from scripts.plan_chapter_split_repair import (
    chapter_repairs,
    file_record,
    part_base,
    split_output_sets,
)


class ChapterSplitPlannerTests(unittest.TestCase):
    def test_empty_directory_audit_excludes_internal_repair_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Example Writer" / "Empty Book").mkdir(parents=True)
            (root / "Example Writer" / "Full Book").mkdir(parents=True)
            (root / "Example Writer" / "Full Book" / "book.m4b").write_bytes(b"audio")
            (root / "_quarantine" / "Empty").mkdir(parents=True)
            rows = empty_output_directories(root)
            self.assertEqual(
                ["Example Writer/Empty Book"],
                [row["relative_path"] for row in rows],
            )

    def test_empty_directory_cleanup_prunes_parents_but_never_internal_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty = root / "Example Writer" / "Series" / "Empty Book"
            empty.mkdir(parents=True)
            full = root / "Example Writer" / "Full Book"
            full.mkdir(parents=True)
            (full / "book.m4b").write_bytes(b"audio")
            internal = root / "_quarantine" / "Empty"
            internal.mkdir(parents=True)
            result = remove_empty_directories(output_root=root, audited_paths=[empty])
            self.assertEqual(2, result["directories_removed"])
            self.assertFalse(empty.exists())
            self.assertFalse((root / "Example Writer" / "Series").exists())
            self.assertTrue(full.is_dir())
            self.assertTrue(internal.is_dir())
            self.assertEqual([], result["remaining_empty_directories"])

    def test_part_base_handles_brackets_discs_and_of_total(self) -> None:
        self.assertEqual("the way of kings", part_base("The Way of Kings [05]"))
        self.assertEqual("high five", part_base("High Five (Disk 9) Author Interview"))
        self.assertEqual("the hero of ages", part_base("The Hero of Ages (2 of 3)"))

    def test_detects_chapter_loss_and_complete_split_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, output_root, archive_root = root / "source", root / "output", root / "archive"
            source_root.mkdir()
            output_root.mkdir()
            source = source_root / "Book.m4b"
            source.write_bytes(b"source")
            whole = output_root / "Author" / "Synthetic Story" / "Author - Synthetic Story.m4b"
            whole.parent.mkdir(parents=True)
            whole.write_bytes(b"whole")
            untracked = []
            for number in range(1, 4):
                part = output_root / "Author" / f"Synthetic Story (Disk {number})" / f"part{number}.m4b"
                part.parent.mkdir(parents=True)
                part.write_bytes(bytes([number]))
                cover = part.parent / "cover.jpg"
                cover.write_bytes(b"cover")
                untracked.append({
                    "path": str(part),
                    "relative_path": str(part.relative_to(output_root)),
                    "observed_author": "Author",
                    "observed_title": f"Synthetic Story (Disk {number})",
                    "probe": {"duration_seconds": 100.0, "chapters": 1, "codec": "aac"},
                })
            audit = {
                "current_outputs": [{
                    "book_id": "book", "path": str(whole),
                    "relative_path": str(whole.relative_to(output_root)),
                    "canonical_author": "Author", "canonical_title": "Synthetic Story",
                    "probe": {"duration_seconds": 300.0, "chapters": 3, "codec": "aac"},
                    "issues": [],
                }],
                "untracked_outputs": untracked,
            }
            books = {"book": {"state": "complete", "files": ["Book.m4b"], "metadata": {}}}
            cached = {str(source.resolve()): {"error": None, "probe": {
                "codec_name": "aac", "duration_seconds": 300.0, "chapters": [{}, {}, {}],
            }}}
            audit["current_outputs"][0]["probe"]["chapters"] = 1
            chapters = chapter_repairs(
                audit=audit, books=books, cached=cached, source_root=source_root,
                output_root=output_root, archive_root=archive_root,
            )
            self.assertEqual(1, len(chapters))
            self.assertEqual(3, chapters[0]["source_chapters"])
            audit["current_outputs"][0]["probe"]["chapters"] = 3
            groups, review = split_output_sets(
                audit=audit, output_root=output_root, archive_root=archive_root
            )
            self.assertEqual([], review)
            self.assertEqual(1, len(groups))
            self.assertEqual(3, groups[0]["unique_part_count"])
            self.assertEqual(1, len(groups[0]["legacy_files"][0]["companion_files"]))


class ChapterSplitExecutorTests(unittest.TestCase):
    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_lossless_repair_archives_old_output_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, output_root, archive_root = root / "source", root / "output", root / "archive"
            source_root.mkdir()
            output_root.mkdir()
            source = source_root / "Book.m4b"
            metadata = root / "chapters.ffmeta"
            metadata.write_text(
                ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=200\n"
                "title=One\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=200\nEND=400\ntitle=Two\n",
                encoding="utf-8",
            )
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.4", "-f", "ffmetadata", "-i", str(metadata),
                "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1", "-c:a", "aac",
                str(source),
            ], check=True)
            output = output_root / "Author" / "Book" / "Author - Book.m4b"
            output.parent.mkdir(parents=True)
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:a:0",
                "-map_chapters", "-1", "-c", "copy", str(output),
            ], check=True)
            source_hash = self._sha(source)
            database = root / "state.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                "CREATE TABLE process_runs(id INTEGER PRIMARY KEY,status TEXT,started_at TEXT,finished_at TEXT);"
                "CREATE TABLE detected_books(run_id INTEGER,book_id TEXT,state TEXT,metadata_json TEXT,confidence REAL,updated_at TEXT);"
                "CREATE TABLE process_events(id INTEGER PRIMARY KEY,run_id INTEGER,level TEXT,event TEXT,book_id TEXT,detail TEXT);"
                "INSERT INTO process_runs VALUES(1,'complete','start','finish');"
            )
            connection.execute(
                "INSERT INTO detected_books VALUES(1,'book','complete',?,0,'now')",
                (json.dumps({"title": "Book", "authors": ["Author"]}),),
            )
            connection.commit()
            connection.close()
            source_probe = probe_media(source)
            output_probe = probe_media(output)
            archive = archive_root / "replaced-chapter-flattened" / output.relative_to(output_root)
            plan = {
                "mode": "dry-run", "actions_applied": 0,
                "roots": {"database": str(database), "source": str(source_root),
                          "output": str(output_root), "archive": str(archive_root)},
                "process_run": {"id": 1, "status": "complete", "finished_at": "finish"},
                "summary": {"chapter_flattened_bytes_to_archive": output.stat().st_size,
                            "verified_split_bytes_to_archive": 0},
                "chapter_repairs": [{
                    "book_id": "book", "source": file_record(source, source_root),
                    "current_output": file_record(output, output_root),
                    "archive_destination": str(archive),
                    "source_chapters": len(source_probe.chapters),
                    "current_chapters": len(output_probe.chapters),
                    "source_duration_seconds": source_probe.duration_seconds,
                    "metadata": {"title": "Book", "authors": ["Author"]},
                }],
                "verified_split_sets": [],
            }
            result, log = apply_plan(plan)
            self.assertEqual(1, result["chapter_outputs_repaired"])
            self.assertTrue(log.is_file())
            self.assertTrue(archive.is_file())
            self.assertEqual(2, len(probe_media(output).chapters))
            self.assertEqual(source_hash, self._sha(source))
            connection = sqlite3.connect(database)
            saved = json.loads(connection.execute(
                "SELECT metadata_json FROM detected_books WHERE book_id='book'"
            ).fetchone()[0])
            connection.close()
            self.assertTrue(saved["_verified_output"])


if __name__ == "__main__":
    unittest.main()
