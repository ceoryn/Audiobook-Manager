from __future__ import annotations

import unittest
from pathlib import Path

from audiobook_manager.detection import detect_books
from audiobook_manager.models import ProbeResult, ScannedFile


def media(path: str, album: str = "", author: str = "", duration: float = 60,
          title: str = "") -> ScannedFile:
    tags = {key: value for key, value in {
        "album": album, "artist": author, "title": title}.items() if value}
    return ScannedFile(Path("/source") / path, Path(path), 1, 1,
                       ProbeResult(duration, "mp3", "mp3", 64000, 44100, 2, tags))


class DetectionTests(unittest.TestCase):
    def test_author_mislabeled_as_title_uses_long_file_name(self) -> None:
        groups = detect_books([
            media(
                "Kim Stanley Robinson/2015 - Aurora.m4a",
                "Kim Stanley Robinson", "Kim Stanley Robinson", 36000,
                title="Kim Stanley Robinson",
            )
        ])
        self.assertEqual("kim stanley robinson — aurora", groups[0].key)

    def test_splits_multiple_tagged_books_in_author_folder(self) -> None:
        groups = detect_books([media("Author/01.mp3", "Book One", "Author"),
                               media("Author/02.mp3", "Book Two", "Author")])
        self.assertEqual(2, len(groups))

    def test_folds_cd_directories_for_untagged_book(self) -> None:
        groups = detect_books([media("Author/Book/CD1/01.mp3"), media("Author/Book/CD2/01.mp3")])
        self.assertEqual(1, len(groups))
        self.assertEqual(2, len(groups[0].files))

    def test_tags_join_complete_m4b_and_source_parts(self) -> None:
        groups = detect_books([media("mess/01.mp3", "Dune", "Frank Herbert"),
                               media("else/final.m4b", "Dune", "Frank Herbert")])
        self.assertEqual(1, len(groups))

    def test_one_word_title_joins_numbered_copy_for_same_author(self) -> None:
        groups = detect_books([
            media("Collection A/01. Dune.m4b", "Dune", "Frank Herbert", 72000),
            media("Collection A/02. Messiah.m4b", "Messiah", "Frank Herbert", 36000),
            media(
                "Collection B/Dune 01 - Dune.m4b",
                "Dune 01 - Dune", "Frank Herbert", 72000,
            ),
            media(
                "Collection B/Dune 02 - Messiah.m4b",
                "Dune 02 - Messiah", "Frank Herbert", 36000,
            ),
        ])
        self.assertEqual(2, len(groups))
        dune = next(group for group in groups if group.key.endswith("dune"))
        self.assertEqual(2, len(dune.files))

    def test_same_title_by_different_authors_stays_separate(self) -> None:
        groups = detect_books([
            media("Dean Koontz/Relentless/01.mp3", "Relentless", "Dean Koontz", 3600),
            media("Dean Koontz/Relentless/02.mp3", "Relentless", "Dean Koontz", 3700),
            media(
                "R.A Salvatore/Relentless/01.mp3", "Relentless",
                "R. A. Salvatore, Victor Bevine", 4100,
            ),
            media(
                "R.A Salvatore/Relentless/02.mp3", "Relentless",
                "R. A. Salvatore, Victor Bevine", 4200,
            ),
        ])
        self.assertEqual(2, len(groups))
        self.assertEqual({2}, {len(group.files) for group in groups})
        self.assertEqual(
            {"dean koontz — relentless", "r a salvatore victor bevine — relentless"},
            {group.key for group in groups},
        )

    def test_inverted_author_spelling_does_not_split_representations(self) -> None:
        groups = detect_books([
            media(
                "David Eddings/The Hidden City/01.mp3",
                "The Hidden City", "Eddings, David", 3600,
            ),
            media(
                "The Hidden City-tmpfiles/01-finished.m4b",
                "The Hidden City", "Eddings, David", 3600,
            ),
        ])
        self.assertEqual(1, len(groups))

    def test_joins_tmp_conversion_with_numbered_source_directory(self) -> None:
        groups = detect_books([
            media("Dune Collection/07 - Great Schools/02. Mentats of Dune/01.mp3"),
            media("07 - Great Schools - 02. Mentats of Dune-tmpfiles/final.m4b"),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("mentats of dune", groups[0].key)

    def test_tagged_parts_join_untagged_equivalent_representation(self) -> None:
        groups = detect_books([
            media("Collection/Series/Book 7 - The High King of Montival/01.mp3",
                  "The High King of Montival", "S. M. Stirling"),
            media("Book 7 - The High King of Montival-tmpfiles/final.m4b"),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual(2, len(groups[0].files))

    def test_unabridged_album_suffix_does_not_split_tmpfile_representations(self) -> None:
        groups = detect_books([
            media("Collection/Series/Book 10 - The Given Sacrifice/01.mp3"),
            media("Collection/Series/Book 10 - The Given Sacrifice/02.mp3",
                  "The Given Sacrifice (Unabridged)", "S. M. Stirling"),
            media("Book 10 - The Given Sacrifice-tmpfiles/01-finished.m4b"),
            media("Book 10 - The Given Sacrifice-tmpfiles/02-finished.m4b"),
            media("Book 10 - The Given Sacrifice-tmpfiles/tmp-book.m4b"),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual(5, len(groups[0].files))

    def test_compact_series_code_does_not_split_complete_representation(self) -> None:
        groups = detect_books([
            media("Book 1 - Dies the Fire-tmpfiles/01-finished.m4b",
                  "EV01 Dies the Fire", "S. M. Stirling"),
            media("Book 1 - Dies the Fire-tmpfiles/02-finished.m4b",
                  "EV01 Dies the Fire", "S. M. Stirling"),
            media("Book 1 - Dies the Fire-tmpfiles/tmp-book.m4b",
                  "Dies the Fire", "S. M. Stirling"),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("s m stirling — dies the fire", groups[0].key)
        self.assertEqual(3, len(groups[0].files))

    def test_numeric_album_tags_are_packaging_not_book_identity(self) -> None:
        groups = detect_books([
            media(
                "Mistborn - Shadows of Self-tmpfiles/01-finished.m4b",
                "01", "Brandon Sanderson", 1800,
            ),
            media(
                "Mistborn - Shadows of Self-tmpfiles/02-finished.m4b",
                "02", "Brandon Sanderson", 1900,
            ),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual(
            "brandon sanderson — mistborn shadows of self", groups[0].key
        )
        self.assertEqual(2, len(groups[0].files))

    def test_generic_album_tag_uses_enclosing_book_title(self) -> None:
        groups = detect_books([
            media("Author/Actual Book/01.mp3", "Audiobook", "Author", 1800),
            media("Author/Actual Book/02.mp3", "Audiobook", "Author", 1900),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("author — actual book", groups[0].key)

    def test_normal_audio_suffix_is_packaging_not_title(self) -> None:
        groups = detect_books([
            media(
                "Collection/Book - Normal Audio/book.mp3",
                "Book", "Known Author", 1800,
            ),
            media(
                "Known Author/Book/book.m4b",
                "Book", "Known Author", 1800,
            ),
        ])
        self.assertEqual(1, len(groups))

    def test_album_equal_to_author_uses_enclosing_book_title(self) -> None:
        groups = detect_books([
            media("Known Author/Actual Book/01.mp3", "Known Author", "Known Author", 1800),
            media("Known Author/Actual Book/02.mp3", "Known Author", "Known Author", 1900),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("known author — actual book", groups[0].key)

    def test_plain_numeric_disc_folders_fold_into_book(self) -> None:
        groups = detect_books([
            media("Stephanie Plum/13A - Plum Lucky/01/01.mp3", "01", "Artist", 600),
            media("Stephanie Plum/13A - Plum Lucky/02/01.mp3", "02", "Artist", 600),
            media("Stephanie Plum/13A - Plum Lucky/03/01.mp3", "03", "Artist", 600),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("artist — plum lucky", groups[0].key)
        self.assertEqual(3, len(groups[0].files))

    def test_legacy_tmp_complete_file_joins_duration_matched_components(self) -> None:
        groups = detect_books([
            media("Greg Bear/Anvil of Stars/01.mp3", "Anvil of Stars", "Greg Bear", 3600),
            media("Greg Bear/Anvil of Stars/02.mp3", "Anvil of Stars", "Greg Bear", 3600),
            media("Greg Bear-Forge of God-2-Anvil of Stars-tmpfiles/"
                  "tmp_Greg Bear-Forge of God-2-Anvil of Stars.m4b", duration=7200),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("greg bear — anvil of stars", groups[0].key)
        self.assertEqual(3, len(groups[0].files))
        self.assertIn("duration-matched alternate representation", groups[0].reasons[0])

    def test_legacy_merge_does_not_collapse_different_series_numbers(self) -> None:
        groups = detect_books([
            media("Series-tmpfiles/02.m4b", "Example Series Book 2", "Author", 7200),
            media("Series-tmpfiles/03.m4b", "Example Series Book 3", "Author", 7200),
        ])
        self.assertEqual(2, len(groups))

    def test_legacy_finished_parts_join_their_complete_target(self) -> None:
        groups = detect_books([
            media("D.j. Molles - The Remaining - Aftermath-tmpfiles/1-finished.m4b",
                  title="The Remaining - Aftermath Part 1", duration=7200),
            media("D.j. Molles - The Remaining - Aftermath-tmpfiles/2-finished.m4b",
                  title="The Remaining - Aftermath Part 2", duration=7300),
            media("D.j. Molles - The Remaining - Aftermath-tmpfiles/"
                  "tmp_D.j. Molles - The Remaining - Aftermath.m4b", duration=14500),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("the remaining aftermath", groups[0].key)

    def test_legacy_finished_parts_do_not_split_on_chapter_title_tags(self) -> None:
        groups = detect_books([
            media(
                "Author - The Book-tmpfiles/01-finished.m4b",
                "The Book", "Author", 1800, title="Chapter 1",
            ),
            media(
                "Author - The Book-tmpfiles/02-finished.m4b",
                "The Book", "Author", 1900, title="Chapter 2",
            ),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("author — the book", groups[0].key)

    def test_graphic_audio_parts_share_an_edition_identity(self) -> None:
        groups = detect_books([
            media("Cosmere/12 - Stormlight Archive - 1 - The Way of Kings - GA/"
                  "Part 1/01.mp3", "The Stormlight Archive 1 - The Way of Kings 01",
                  "GraphicAudio", 4000),
            media("Cosmere/12 - Stormlight Archive - 1 - The Way of Kings - GA/"
                  "Part 2/01.mp3", "The Stormlight Archive 1 - The Way of Kings 02",
                  "GraphicAudio", 4100),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual(
            "graphicaudio — the way of kings graphic audio", groups[0].key
        )

    def test_graphic_audio_production_credit_does_not_split_author_copy(self) -> None:
        groups = detect_books([
            media(
                "Cosmere/Book - GA/Part 1/01.mp3",
                "Example", "GraphicAudio [Brandon Sanderson]", 4000,
            ),
            media(
                "Example-tmpfiles/01-finished.m4b",
                "Example", "Brandon Sanderson", 3900,
            ),
        ])
        self.assertEqual(1, len(groups))

    def test_publication_year_suffix_does_not_split_whole_file_from_parts(self) -> None:
        groups = detect_books([
            media("Dean Koontz/The Dead Town/01.mp3", "The Dead Town", "Dean Koontz", 3600),
            media("Dean Koontz/The Dead Town/02.mp3", "The Dead Town", "Dean Koontz", 3600),
            media("Dean Koontz/The Dead Town 2011.mp3", author="Dean Koontz", duration=7200),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("dean koontz — the dead town", groups[0].key)

    def test_parenthesized_publication_year_is_also_packaging(self) -> None:
        groups = detect_books([
            media("Dean Koontz/Book 5 - The Dead Town/01.mp3",
                  "The Dead Town", "Dean Koontz", 3600),
            media("Dean Koontz/Alt. Versions/Book 5 - The Dead Town (2011)/"
                  "The Dead Town.mp3", author="Dean Koontz", duration=3600),
        ])
        self.assertEqual(1, len(groups))

    def test_top_level_author_suffix_does_not_split_an_untagged_copy(self) -> None:
        groups = detect_books([
            media("Brandon Sanderson/Words of Radiance/book.m4b",
                  "Words of Radiance", "Brandon Sanderson", 7200),
            media("Brandon Sanderson/Words of Radiance - Brandon Sanderson/01.mp3",
                  duration=3600),
            media("Brandon Sanderson/Words of Radiance - Brandon Sanderson/02.mp3",
                  duration=3600),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("brandon sanderson — words of radiance", groups[0].key)

    def test_graphic_audio_credit_marks_parts_outside_ga_named_folder(self) -> None:
        groups = detect_books([
            media("Author/Series/The Way of Kings (1 of 5)/01.mp3",
                  "The Way of Kings (1 of 5)", "Graphic Audio", 4000),
            media("Author/Series/The Way of Kings (2 of 5)/01.mp3",
                  "The Way of Kings (2 of 5)", "Graphic Audio", 4100),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual(
            "graphic audio — the way of kings graphic audio", groups[0].key
        )

    def test_splits_long_series_files_that_share_collection_album(self) -> None:
        groups = detect_books([
            media("Author/Series/Series - 01 - First Novel.mp3", "Series", "Author", 36000),
            media("Author/Series/Series - 02 - Second Novel.mp3", "Series", "Author", 38000),
        ])
        self.assertEqual(2, len(groups))
        self.assertEqual({"author — first novel", "author — second novel"}, {group.key for group in groups})

    def test_does_not_split_short_chapter_files_with_numbered_names(self) -> None:
        groups = detect_books([
            media("Author/Book/Book - 01 - Opening.mp3", "Book", "Author", 1800),
            media("Author/Book/Book - 02 - Middle.mp3", "Book", "Author", 1800),
        ])
        self.assertEqual(1, len(groups))

    def test_generic_alt_version_directories_do_not_merge_different_books(self) -> None:
        groups = detect_books([
            media("Author/First Book/Alt. Version/First Book.mp3"),
            media("Author/Second Book/Alternate Version/Second Book.mp3"),
        ])
        self.assertEqual({"first book", "second book"}, {group.key for group in groups})

    def test_marks_conflicting_single_file_directory_and_filename(self) -> None:
        groups = detect_books([media("Dean Koontz/2014 The City/77 Shadowstreet.mp3")])
        self.assertEqual(1, len(groups))
        self.assertIn("directory and filename identities conflict", groups[0].reasons)

    def test_matching_embedded_title_resolves_compact_filename(self) -> None:
        groups = detect_books([
            media("R.A Salvatore/Hero/HeroLegendofDrizztHomecomingBook3.mp3",
                  author="R. A. Salvatore", title="Hero: Legend of Drizzt", duration=9000),
        ])
        self.assertEqual(1, len(groups))
        self.assertNotIn("directory and filename identities conflict", groups[0].reasons)

    def test_compact_filename_can_support_a_clear_structural_title(self) -> None:
        groups = detect_books([
            media("Dean Koontz/Odd Thomas/5 Odd Interlude/"
                  "OddInterludeUnabridged_mp332.mp3", duration=9000),
        ])
        self.assertEqual(1, len(groups))
        self.assertNotIn("directory and filename identities conflict", groups[0].reasons)

    def test_broken_legacy_fragment_stays_with_readable_directory_siblings(self) -> None:
        readable = media("Book-tmpfiles/01-finished.m4b", "Book", "Author", 60)
        broken = ScannedFile(
            Path("/source/Book-tmpfiles/02-converting.m4b"),
            Path("Book-tmpfiles/02-converting.m4b"), 1, 1, None, "invalid media",
        )
        groups = detect_books([readable, broken])
        self.assertEqual(1, len(groups))
        self.assertEqual(2, len(groups[0].files))

    def test_by_author_folder_suffix_is_packaging_not_title(self) -> None:
        groups = detect_books([
            media("Julian May/The Many-Colored Land/01.mp3",
                  "The Many-Colored Land", "Julian May", 3600),
            media("The Many-Colored Land by Julian May/part.mp3", duration=1800),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("julian may — the many colored land", groups[0].key)

    def test_disc_suffixes_do_not_split_one_book(self) -> None:
        groups = detect_books([
            media("Book/Disc 1/01.mp3", "High Five (Disc 1)", "Janet Evanovich"),
            media("Book/Disc 2/01.mp3", "High Five (Disk 2)", "Janet Evanovich"),
            media("Book/Disc 9/01.mp3", "High Five (Disk 9) Author Interview", "Janet Evanovich"),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("janet evanovich — high five", groups[0].key)

    def test_pt_suffix_and_repeated_author_credit_are_packaging(self) -> None:
        groups = detect_books([
            media(
                "Dean Koontz/2014 The City/The City pt 1.mp3",
                "Dean Koontz - The City pt 1", "Dean Koontz", 3600,
            ),
            media(
                "Dean Koontz/2014 The City/The City pt.2.mp3",
                "Dean Koontz - The City pt.2", "Dean Koontz", 3700,
            ),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("dean koontz — the city", groups[0].key)

    def test_misfiled_title_joins_a_corroborating_named_copy_not_siblings(self) -> None:
        groups = detect_books([
            media(
                "Dean Koontz/2014 The City/The City pt 1.mp3",
                "Dean Koontz - The City pt 1", "Dean Koontz", 3600,
            ),
            media(
                "Dean Koontz/2014 The City/The City pt 2.mp3",
                "Dean Koontz - The City pt 2", "Dean Koontz", 3700,
            ),
            media("Dean Koontz/2014 The City/77 Shadowstreet.mp3", duration=7200),
            media("Dean Koontz/2011 77 Shadowstreet/77 Shadowstreet.mp3", duration=7200),
        ])
        self.assertEqual(2, len(groups))
        by_title = {group.key: group for group in groups}
        self.assertEqual(2, len(by_title["dean koontz — the city"].files))
        self.assertEqual(2, len(by_title["77 shadowstreet"].files))

    def test_parenthesized_of_total_disc_folders_fold_into_one_book(self) -> None:
        groups = detect_books([
            media("Dean Koontz/One Door Away From Heaven/(1of17)/001.mp3",
                  "One Door Away From Heaven 1of17", "Dean Koontz"),
            media("Dean Koontz/One Door Away From Heaven/(2of17)/002.mp3",
                  "One Door Away From Heaven 2of17", "Dean Koontz"),
        ])
        self.assertEqual(1, len(groups))
        self.assertEqual("dean koontz — one door away from heaven", groups[0].key)
        self.assertEqual(2, len(groups[0].files))

    def test_narrator_suffix_does_not_split_alternate_representation(self) -> None:
        groups = detect_books([
            media("Book/01.mp3", "Brother Odd", "Dean Koontz"),
            media("Alternate/book.mp3", "Brother Odd read by David Aaron Baker", "Dean Koontz"),
        ])
        self.assertEqual(1, len(groups))

    def test_splits_untagged_audiobook_sized_files_in_author_folder(self) -> None:
        groups = detect_books([
            media("J.R.Ward/B02 Lover Eternal.mp3", duration=51052),
            media("J.R.Ward/B04 Lover Revealed.mp3", duration=58742),
        ])
        self.assertEqual({"lover eternal", "lover revealed"}, {group.key for group in groups})

    def test_uses_title_tag_for_flat_long_file_when_album_is_missing(self) -> None:
        groups = detect_books([
            media("J.R.Ward/B20 - file.mp3", author="J.R. Ward", duration=56509,
                  title="Lover Unveiled (Black Dagger Brotherhood #19) (Unabridged)"),
        ])
        self.assertEqual(
            "j r ward — lover unveiled black dagger brotherhood 19", groups[0].key
        )

    def test_album_ending_in_book_number_keeps_its_full_identity(self) -> None:
        groups = detect_books([
            media("Series/Book 05/book.m4b", "He Who Fights with Monsters, Book 05",
                  "Shirtaloon", 72000),
            media("Other/Book 15/01.mp3", "The New Jedi Order - Book 15",
                  "Shane Dix", 4200),
            media("Other/Book 18.5/01.mp3", "A Warm Heart in Winter, Book 18.5",
                  "J. R. Ward", 4200),
        ])
        self.assertEqual(
            {"shirtaloon — he who fights with monsters book 05",
             "shane dix — the new jedi order book 15",
             "j r ward — a warm heart in winter book 18 5"},
            {group.key for group in groups},
        )

    def test_placeholder_album_uses_structural_book_title(self) -> None:
        groups = detect_books([
            media("Author/Book 14 - A Memory of Light/01.mp3", "Unknown", "Author"),
            media("Author/Book 14 - A Memory of Light/02.mp3", "Unknown", "Author"),
        ])
        self.assertEqual("author — a memory of light", groups[0].key)

    def test_series_album_does_not_merge_separate_long_containers(self) -> None:
        groups = detect_books([
            media("Author/Gauntlgrym/book.m4b", "Neverwinter Saga", "Author", 45000),
            media("Author/Charon's Claw/book.m4b", "Neverwinter Saga", "Author", 47000),
        ])
        self.assertEqual(
            {"author — gauntlgrym", "author — charon s claw"},
            {group.key for group in groups},
        )
        self.assertTrue(all(
            "split from a shared collection album" in group.reasons[0]
            for group in groups
        ))

    def test_series_album_does_not_hide_descriptive_container_title(self) -> None:
        groups = detect_books([
            media(
                "Brandon Sanderson/Skyward/Skyward - Skyward, Book 1.m4b",
                "Skyward", "Brandon Sanderson", 55000,
                title="Skyward - Skyward, Book 1",
            ),
            media(
                "Brandon Sanderson/Skyward/Defending Elysium - Skyward, Book 0.m4b",
                "Skyward Series", "Brandon Sanderson", 6300,
                title="Defending Elysium",
            ),
        ])
        self.assertEqual(
            {"brandon sanderson — skyward", "brandon sanderson — defending elysium"},
            {group.key for group in groups},
        )

    def test_collection_container_uses_descriptive_filename_when_tags_are_noise(self) -> None:
        groups = detect_books([
            media(
                "Author/Collection/The Emperor's Soul - A Novella.m4b",
                duration=14000, title="- 03/16",
            ),
            media(
                "Author/Collection/Another Story.m4b",
                duration=13000, title="- 04/16",
            ),
        ])
        self.assertEqual(
            {"the emperor s soul a novella", "another story"},
            {group.key for group in groups},
        )

    def test_numbered_long_containers_in_collection_are_individual_books(self) -> None:
        groups = detect_books([
            media("Dune/Prelude/01. House Atreides.m4b", "The New Dune Chronicles",
                  "Brian Herbert", 70000),
            media("Dune/Prelude/02. House Harkonnen.m4b", "The New Dune Chronicles",
                  "Brian Herbert", 71000),
        ])
        self.assertEqual(
            {"brian herbert — house atreides", "brian herbert — house harkonnen"},
            {group.key for group in groups},
        )
