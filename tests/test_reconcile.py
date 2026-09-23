from __future__ import annotations

import unittest
from pathlib import Path

from audiobook_manager.models import BookGroup, Chapter, ProbeResult, ScannedFile
from dataclasses import replace
from audiobook_manager.reconcile import reconcile_group


def media(name: str, duration: float, *, readable: bool = True) -> ScannedFile:
    probe = ProbeResult(duration, name.rsplit(".", 1)[-1], "aac", 96000, 44100, 2) if readable else None
    return ScannedFile(Path("/src") / name, Path(name), round(duration * 1000), 1,
                       probe, None if readable else "invalid media")


class ReconcileTests(unittest.TestCase):
    def test_sparse_explicit_part_set_is_not_published_as_a_book(self) -> None:
        group = BookGroup("Book", (
            media("Book/Book 01 of 04.mp3", 500),
            media("Book/Book 03 of 04.mp3", 500),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("quarantine", result.state)
        self.assertIn("2 missing", result.conflicts[0])

    def test_unreadable_explicit_part_prevents_partial_conversion(self) -> None:
        group = BookGroup("Book", (
            media("Book/Book_01_03.mp3", 500),
            media("Book/Book_02_03.mp3", 0, readable=False),
            media("Book/Book_03_03.mp3", 500),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("quarantine", result.state)
        self.assertIn("1 unreadable", result.conflicts[0])

    def test_complete_explicit_part_set_can_convert(self) -> None:
        group = BookGroup("Book", (
            media("Book/Book (1 Of 2).mp3", 500),
            media("Book/Book (2 Of 2).mp3", 500),
        ), ())
        self.assertEqual("combine_components", reconcile_group(group).state)

    def test_series_book_and_file_suffix_is_not_an_explicit_part_total(self) -> None:
        group = BookGroup("Book", (
            media("Book/Example_Series_Book_6_01.m4b", 30000),
        ), ())
        self.assertEqual("complete_m4b", reconcile_group(group).state)

    def test_numbered_original_tracks_preferred_to_temporary_aggregate(self) -> None:
        result = reconcile_group(BookGroup("Book", (
            media("Book/01 - chapter.mp3", 600), media("Book/02 - chapter.mp3", 700),
            media("Book-tmpfiles/tmp_Book.m4b", 1300)), ()))
        self.assertEqual("combine_components", result.state)
        self.assertEqual(("Book/01 - chapter.mp3", "Book/02 - chapter.mp3"), result.chosen_files)

    def test_combines_mp4_components(self) -> None:
        result = reconcile_group(BookGroup("Book", (
            media("Book/01.mp4", 600), media("Book/02.mp4", 700)), ()))
        self.assertEqual("combine_components", result.state)
        self.assertEqual(2, len(result.chosen_files))

    def test_numeric_disc_folders_are_not_equivalent_complete_books(self) -> None:
        group = BookGroup("Book", tuple(
            media(f"Author/Book/{disc:02d}/{track:02d}.mp3", 400 + track)
            for disc in range(1, 7) for track in range(1, 4)
        ), ())
        result = reconcile_group(group)
        self.assertEqual(18, len(result.chosen_files))
        self.assertEqual((), result.alternate_files)

    def test_original_chaptered_whole_wins_over_double_length_legacy_aggregate(self) -> None:
        original = media("Author/Book/Book.m4b", 18000)
        original = replace(original, probe=replace(original.probe, chapters=(
            Chapter(0, 0, 9000, "One"), Chapter(1, 9000, 18000, "Two"))))
        group = BookGroup("Book", (
            original, media("Author/Book/Book.mp3", 18000.1),
            media("Book-tmpfiles/1-finished.m4b", 18000),
            media("Book-tmpfiles/2-finished.m4b", 18000),
            media("Book-tmpfiles/tmp_Book.m4b", 36000),
        ), ())
        result = reconcile_group(group)
        self.assertEqual(("Author/Book/Book.m4b",), result.chosen_files)
        self.assertEqual(4, len(result.alternate_files))

    def test_prefers_complete_m4b_over_mp3_sources(self) -> None:
        group = BookGroup("Book", (media("01.mp3", 50), media("02.mp3", 50), media("Book.m4b", 100.2)), ())
        result = reconcile_group(group)
        self.assertEqual("complete_m4b", result.state)
        self.assertEqual(("Book.m4b",), result.chosen_files)
        self.assertEqual(2, len(result.alternate_files))

    def test_one_complete_target_matching_two_component_sets_is_not_ambiguous(self) -> None:
        group = BookGroup("Book", (
            media("01.mp3", 40), media("02.mp3", 60),
            media("01-finished.m4b", 40), media("02-finished.m4b", 60),
            media("Book.m4b", 100.2)), ())
        result = reconcile_group(group)
        self.assertEqual("complete_m4b", result.state)
        self.assertEqual(("Book.m4b",), result.chosen_files)
        self.assertEqual(0.98, result.confidence)
        self.assertNotIn("multiple complete-container candidates", result.conflicts)

    def test_quarantines_unexplained_competing_representations(self) -> None:
        group = BookGroup("Book", (media("01.mp3", 50), media("mystery.m4b", 900)), ())
        self.assertEqual("quarantine", reconcile_group(group).state)

    def test_combines_multipart_mp3_set(self) -> None:
        group = BookGroup("Book", (media("01.mp3", 50), media("02.mp3", 60)), ())
        self.assertEqual("combine_components", reconcile_group(group).state)

    def test_duplicate_component_trees_select_one_complete_representation(self) -> None:
        group = BookGroup("Book", (
            media("Copy A/01.mp3", 40), media("Copy A/02.mp3", 60),
            media("Copy B/01.mp3", 40), media("Copy B/02.mp3", 60),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("combine_components", result.state)
        self.assertEqual(2, len(result.chosen_files))
        self.assertEqual(2, len(result.alternate_files))
        self.assertEqual(
            {"Copy A/01.mp3", "Copy A/02.mp3", "Copy B/01.mp3", "Copy B/02.mp3"},
            {*result.chosen_files, *result.alternate_files},
        )
        self.assertIn("alternate complete representations", result.evidence[0])

    def test_similar_duration_containers_are_alternates_not_components(self) -> None:
        group = BookGroup("Book", (media("edition.m4b", 1000), media("alternate.m4a", 1005)), ())
        result = reconcile_group(group)
        self.assertEqual("complete_m4b", result.state)
        self.assertEqual(1, len(result.chosen_files))
        self.assertEqual(1, len(result.alternate_files))

    def test_similar_containers_retain_complete_mp3_and_broken_files(self) -> None:
        group = BookGroup("Book", (
            media("edition.m4b", 1000), media("alternate.m4a", 1000),
            media("complete.mp3", 1000), media("broken.mp3", 0, readable=False),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("complete_m4b", result.state)
        self.assertEqual(2, len(result.alternate_files))
        self.assertIn("complete.mp3", result.alternate_files)
        self.assertEqual(("broken.mp3",), result.problem_files)

    def test_explicit_complete_mp3_is_preferred_over_component_tracks(self) -> None:
        group = BookGroup("Book", (
            media("Book/01.mp3", 40), media("Book/02.mp3", 60),
            media("Book/Book complete.mp3", 100),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("convert_single", result.state)
        self.assertEqual(("Book/Book complete.mp3",), result.chosen_files)
        self.assertEqual(2, len(result.alternate_files))

    def test_audiobook_sized_mp3_matching_components_is_a_whole_file(self) -> None:
        group = BookGroup("Book", (
            media("Book/01.mp3", 4000), media("Book/02.mp3", 4100),
            media("Book.mp3", 8100),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("convert_single", result.state)
        self.assertEqual(("Book.mp3",), result.chosen_files)
        self.assertEqual(2, len(result.alternate_files))

    def test_complete_multidisc_tree_wins_over_a_matching_partial_target(self) -> None:
        group = BookGroup("Book", (
            media("Edition/Part 1/01.mp3", 2000),
            media("Edition/Part 1/02.mp3", 2100),
            media("Edition/Part 2/01.mp3", 2200),
            media("Edition/Part 2/02.mp3", 2300),
            media("Legacy Part 1/01.mp3", 2000),
            media("Legacy Part 1/02.mp3", 2100),
            media("Legacy Part 1/complete.m4b", 4100),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("combine_components", result.state)
        self.assertEqual(4, len(result.chosen_files))
        self.assertTrue(all(path.startswith("Edition/") for path in result.chosen_files))
        self.assertEqual(3, len(result.alternate_files))

    def test_incomplete_legacy_conversion_does_not_quarantine_complete_mp3_tree(self) -> None:
        group = BookGroup("Book", (
            media("Source/01.mp3", 3000), media("Source/02.mp3", 3100),
            media("Book-tmpfiles/01-finished.m4b", 3000),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("combine_components", result.state)
        self.assertEqual(("Source/01.mp3", "Source/02.mp3"), result.chosen_files)
        self.assertEqual(("Book-tmpfiles/01-finished.m4b",), result.alternate_files)

    def test_two_matching_complete_trees_outvote_a_shorter_alternate_edition(self) -> None:
        group = BookGroup("Book", (
            media("Containers/01.m4b", 3000), media("Containers/02.m4b", 2700),
            media("Source/01.mp3", 2800), media("Source/02.mp3", 2920),
            media("Short Edition/01.mp3", 2500), media("Short Edition/02.mp3", 2600),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("combine_components", result.state)
        self.assertEqual(("Containers/01.m4b", "Containers/02.m4b"), result.chosen_files)
        self.assertEqual(4, len(result.alternate_files))

    def test_alternate_version_mp3_is_preferred_when_it_matches_chapter_total(self) -> None:
        group = BookGroup("Book", (
            media("Book/01.mp3", 40), media("Book/02.mp3", 60),
            media("Book/Alternative Version/Book.mp3", 100),
        ), ())
        result = reconcile_group(group)
        self.assertEqual(("Book/Alternative Version/Book.mp3",), result.chosen_files)

    def test_duration_conflicting_alternate_mp3_is_not_appended_to_components(self) -> None:
        group = BookGroup("Book", (
            media("Book/01.mp3", 3000), media("Book/02.mp3", 3100),
            media("Book/Alternative Version/Book.mp3", 5000),
        ), ())
        result = reconcile_group(group)
        self.assertEqual("combine_components", result.state)
        self.assertEqual(("Book/01.mp3", "Book/02.mp3"), result.chosen_files)
        self.assertEqual(("Book/Alternative Version/Book.mp3",), result.alternate_files)
        self.assertIn("duration differs", result.conflicts[0])

    def test_quarantines_directory_filename_identity_conflict(self) -> None:
        group = BookGroup("The City", (media("77 Shadowstreet.mp3", 1000),),
                          ("directory and filename identities conflict",))
        result = reconcile_group(group)
        self.assertEqual("quarantine", result.state)
        self.assertIn("different books", result.conflicts[0])
