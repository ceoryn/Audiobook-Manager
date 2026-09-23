from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.plan_remote_library_import import (
    author_matches,
    build_plan,
    runtime_matches,
    series_folder_alias,
    similar_existing_title,
    source_title_matches,
)


class RemoteImportPlanTests(unittest.TestCase):
    def test_identity_checks_are_conservative(self) -> None:
        self.assertTrue(source_title_matches(
            "Columbus Day", "Books/Columbus Day [B01N48VJFJ]/"
            "Columbus Day꞉ Expeditionary Force, Book 1 [B01N48VJFJ].m4b"))
        self.assertFalse(source_title_matches(
            "Different Book", "Books/Columbus Day [B01N48VJFJ]/"
            "Columbus Day [B01N48VJFJ].m4b"))
        self.assertTrue(author_matches(["B. V. Larson"], "B.V. Larson"))
        self.assertFalse(author_matches(["D. J. Molles"], "B. V. Larson"))
        self.assertTrue(runtime_matches(600, 600 * 60 + 300))
        self.assertFalse(runtime_matches(600, 450 * 60))
        self.assertEqual(["Timothy Zahn/Last Command.m4b"], similar_existing_title(
            "Star Wars: The Thrawn Trilogy, Book 3: The Last Command", "Timothy Zahn",
            [{"author": "timothy zahn", "titles": ["star wars the thrawn trilogy the last command"],
              "path": "Timothy Zahn/Last Command.m4b"}]))

    def test_plan_never_copies_conflict_or_unverified_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            path = "Books/Example [B000000001]/Example [B000000001].m4b"
            record = {"title": "Example", "author": "Example Writer", "source_files": [path],
                      "source_fingerprints": [{"path": path, "size": 1000, "mtime_ns": 1}],
                      "source_bytes": 1000, "duration_seconds": 3600,
                      "classification": "missing_candidate", "matched_outputs": []}
            metadata = {"asin": "B000000001", "title": "Example",
                        "authors": ["Example Writer"], "runtime_minutes": 60,
                        "series": None}
            reconciliation = {"generated_at": "fixture", "remote_host": "fixture",
                              "remote_root": "/fixture", "books": [record, dict(record)]}
            cache = {"B000000001": {"metadata": metadata, "error": None}}
            plan = build_plan(reconciliation, output, cache, root / "cache.json",
                              online=False, free_bytes=100 * 2**30)
            self.assertEqual(2, plan["summary"]["actions"]["review_destination_collision"])
            self.assertNotIn("proposed_copy", plan["summary"]["actions"])
            reconciliation["books"] = [{**record, "source_files": [path, "part2.m4b"]}]
            plan = build_plan(reconciliation, output, cache, root / "cache.json",
                              online=False, free_bytes=100 * 2**30)
            self.assertEqual("review_multipart_or_format", plan["operations"][0]["action"])

    def test_existing_output_blocks_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            book = output / "Example Writer" / "Example"
            book.mkdir(parents=True)
            (book / "Example Writer - Example.m4b").touch()
            record = {"title": "Unclear Album", "author": "Example Writer",
                      "source_files": ["Books/Example [B000000001]/Example [B000000001].m4b"],
                      "source_fingerprints": [{"path": "Books/Example [B000000001]/Example [B000000001].m4b",
                                               "size": 1000, "mtime_ns": 1}],
                      "source_bytes": 1000, "duration_seconds": 3600,
                      "classification": "missing_candidate", "matched_outputs": []}
            reconciliation = {"generated_at": "fixture", "remote_host": "fixture",
                              "remote_root": "/fixture", "books": [record]}
            cache = {"B000000001": {"metadata": {"asin": "B000000001", "title": "Example",
                     "authors": ["Example Writer"], "runtime_minutes": 60}, "error": None}}
            plan = build_plan(reconciliation, output, cache, root / "cache.json",
                              online=False, free_bytes=100 * 2**30)
            self.assertEqual("no_copy_existing_or_review", plan["operations"][0]["action"])

    def test_series_folder_alias_blocks_split(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Example Writer" / "Example Series,").mkdir(parents=True)
            target = root / "Example Writer" / "Example Series" / "01 - Example" / "Example.m4b"
            self.assertEqual([str(root / "Example Writer" / "Example Series,")],
                             series_folder_alias(target, root))


if __name__ == "__main__":
    unittest.main()
