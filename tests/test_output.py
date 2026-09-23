from __future__ import annotations

import unittest
from pathlib import Path

from audiobook_manager.output import approved_cover_url, normalize_output_metadata, plan_output


class OutputTests(unittest.TestCase):
    def test_audiobookshelf_series_layout(self) -> None:
        plan = plan_output(Path("/clean"), {"title": "Dune: Messiah", "authors": ["Frank Herbert"],
                                                   "series": "Dune", "series_position": 2})
        self.assertEqual(Path("/clean/Frank Herbert/Dune/02 - Dune Messiah/Frank Herbert - Dune Messiah.m4b"), plan.audio)
        self.assertEqual("cover.jpg", plan.cover.name)

    def test_non_series_layout_and_unsafe_characters(self) -> None:
        plan = plan_output(Path("/clean"), {"title": "A/B?", "authors": ["A: Writer"]})
        self.assertEqual(Path("/clean/A Writer/A B/A Writer - A B.m4b"), plan.audio)

    def test_google_books_cover_is_upgraded_to_approved_https(self) -> None:
        self.assertEqual(
            "https://books.google.com/books/content?id=test",
            approved_cover_url("http://books.google.com/books/content?id=test"),
        )

    def test_unrecognized_cover_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            approved_cover_url("https://example.com/cover.jpg")

    def test_reuses_case_equivalent_existing_author_folder(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "Dean Koontz").mkdir()
            metadata = normalize_output_metadata(
                destination, {"title": "Fear Nothing", "authors": ["dean koontz"]})
            plan = plan_output(destination, metadata)
        self.assertEqual(["Dean Koontz"], metadata["authors"])
        self.assertEqual("Dean Koontz", plan.audio.parts[-3])

    def test_reuses_author_folder_across_initial_punctuation(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "J. K. Rowling").mkdir()
            metadata = normalize_output_metadata(
                destination, {"title": "Book", "authors": ["J.K. Rowling"]})
        self.assertEqual(["J. K. Rowling"], metadata["authors"])
