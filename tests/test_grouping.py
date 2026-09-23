from __future__ import annotations

import unittest
from pathlib import Path

from audiobook_manager.grouping import group_files
from audiobook_manager.models import ScannedFile


def item(relative: str) -> ScannedFile:
    path = Path("/fixture") / relative
    return ScannedFile(path, Path(relative), 0, 0, None)


class GroupingTests(unittest.TestCase):
    def test_folds_disc_directories_into_book_parent(self) -> None:
        groups = group_files([item("Author/Book/CD1/01.mp3"), item("Author/Book/CD2/02.mp3")])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].key, "Author/Book")
        self.assertIn("CD/disc/part", groups[0].reasons[1])

    def test_folds_parenthesized_of_total_disc_directories(self) -> None:
        groups = group_files([
            item("Author/Book/(1of17)/01.mp3"),
            item("Author/Book/(17of17)/01.mp3"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].key, "Author/Book")

    def test_keeps_distinct_book_directories_separate(self) -> None:
        groups = group_files([item("Author/One/01.mp3"), item("Author/Two/01.mp3")])
        self.assertEqual([group.key for group in groups], ["Author/One", "Author/Two"])


if __name__ == "__main__":
    unittest.main()
