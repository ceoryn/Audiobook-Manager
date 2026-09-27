from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.audit_remote_library import active_outputs, build, classify, title_aliases


class RemoteLibraryAuditTests(unittest.TestCase):
    def test_named_collection_prefix_has_no_special_treatment(self) -> None:
        for title in ("Star Wars: Example Journey", "Example Saga: Example Journey"):
            with self.subTest(title=title):
                self.assertNotIn("example journey", title_aliases(title))
                self.assertIn("example journey", title_aliases(title, series=title.split(":")[0]))

    def test_series_evidence_preserves_full_title_and_word_boundaries(self) -> None:
        self.assertEqual({"example saga example journey", "example journey"},
                         title_aliases("Example Saga: Example Journey", series="Example Saga"))
        self.assertEqual({"example sagacious visitors"},
                         title_aliases("Example Sagacious Visitors", series="Example Saga"))
        self.assertNotIn("example journey", title_aliases(
            "Example Saga: Example Journey", series="Unrelated Saga"))

    def test_output_series_folder_supplies_collection_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book = root / "Example Writer" / "Example Saga" / "Example Journey"
            book.mkdir(parents=True)
            (book / "Example Writer - Example Saga - Example Journey.m4b").touch()
            outputs = active_outputs(root)
        self.assertEqual("example saga", outputs[0]["series"])
        self.assertEqual("represented_title", classify(
            "Example Saga: Example Journey", "Example Writer", outputs, series="Example Saga")[0])

    def test_unconfirmed_prefix_does_not_hide_a_missing_book(self) -> None:
        outputs = [{"path": "Example Writer/Example Journey/book.m4b",
                    "author": "example writer", "titles": ["example journey"]}]
        self.assertEqual("missing_candidate", classify(
            "Example Saga: Example Journey", "Example Writer", outputs)[0])
        self.assertEqual("represented_title", classify(
            "Example Saga: Example Journey", "Example Writer", outputs, series="Example Saga")[0])

    def test_conflicting_series_or_author_remains_in_review(self) -> None:
        outputs = [{"path": "Example Writer/Other Saga/Example Journey/book.m4b",
                    "author": "example writer", "titles": ["example journey"], "series": "other saga"}]
        self.assertEqual("review_author_or_edition", classify(
            "Example Saga: Example Journey", "Example Writer", outputs, series="Example Saga")[0])
        self.assertEqual("review_author_or_edition", classify(
            "Example Journey", "Different Writer", outputs)[0])

    def test_conflicting_source_series_tags_are_logged_and_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "output"
            source.mkdir()
            book = output / "Example Writer" / "Example Journey"
            book.mkdir(parents=True)
            (book / "Example Writer - Example Journey.m4b").touch()
            inventory, probes = [], []
            for number, series in enumerate(("Example Saga", "Other Saga"), 1):
                path = f"Example Writer/Example Journey/{number:02}.mp3"
                inventory.append(json.dumps({"path": path, "size": 1000, "mtime_ns": 1}))
                probes.append(json.dumps({"path": path, "duration_seconds": 3600,
                    "tags": {"album": "Example Journey", "artist": "Example Writer", "series": series}}))
            args = argparse.Namespace(source_root=str(source), output_root=str(output),
                                      host="fixture", remote_root="/generated-fixture")
            with patch("scripts.audit_remote_library.remote_command",
                       side_effect=["\n".join(inventory), "\n".join(probes)]):
                report = build(args)
        self.assertEqual(1, len(report["books"]))
        self.assertEqual("review_identity", report["books"][0]["classification"])
        self.assertEqual(["Example Saga", "Other Saga"], report["books"][0]["series_evidence"])


if __name__ == "__main__":
    unittest.main()
