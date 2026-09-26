from __future__ import annotations

import unittest
from pathlib import Path

from audiobook_manager.hints import extract_hint
from audiobook_manager.models import BookGroup, ProbeResult, ScannedFile


class HintTests(unittest.TestCase):
    def test_author_role_label_is_not_part_of_author_name(self) -> None:
        probe = ProbeResult(3600, "m4b", "aac", 96000, 44100, 2,
                            {"title": "Example Book", "artist": "Example Writer (Author)"})
        relative = Path("Example Writer/Example Book/book.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("example", (item,), ()))
        self.assertEqual(["Example Writer"], hint["authors"])

    def test_placeholder_credit_falls_back_to_real_author_folder(self) -> None:
        probe = ProbeResult(
            60, "mp3", "mp3", 64000, 44100, 2,
            {"album": "Shattered", "artist": "No Artist"},
        )
        relative = Path("Dean Koontz/1973 Shattered/01.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("no artist — shattered", (item,), ()))
        self.assertEqual(["Dean Koontz"], hint["authors"])

    def test_untitled_series_book_uses_series_and_position(self) -> None:
        probe = ProbeResult(
            3600, "m4b", "aac", 64000, 44100, 2,
            {"title": "Untitled", "artist": "Shirtaloon",
             "series": "He Who Fights with Monsters"},
        )
        relative = Path("He Who Fights with Monsters/Book 04/book.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("shirtaloon — book 04", (item,), ()))
        self.assertEqual("He Who Fights with Monsters 04", hint["title"])

    def test_extracts_embedded_identity_asin_and_runtime(self) -> None:
        probe = ProbeResult(3600, "m4b", "aac", 96000, 44100, 2,
                            {"album": "Project Hail Mary", "artist": "Andy Weir",
                             "narrator": "Ray Porter", "asin": "B08G9PRS1K"})
        item = ScannedFile(Path("/src/book.m4b"), Path("book.m4b"), 1, 1, probe)
        hint = extract_hint(BookGroup("fallback", (item,), ()))
        self.assertEqual("Project Hail Mary", hint["title"])
        self.assertEqual("B08G9PRS1K", hint["asin"])
        self.assertEqual(60, hint["runtime_minutes"])

    def test_removes_conversion_artifacts_from_fallback_title(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2, {})
        item = ScannedFile(Path("/src/Mentats of Dune tmpfiles.mp3"),
                           Path("Mentats of Dune tmpfiles.mp3"), 1, 1, probe)
        hint = extract_hint(BookGroup("Mentats of Dune tmpfiles", (item,), ()))
        self.assertEqual("Mentats of Dune", hint["title"])

    def test_extracts_series_and_position_from_directory_structure(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "The High King of Montival", "artist": "S. M. Stirling"})
        relative = Path("Collection - S. M. Stirling/Emberverse Series/Book 7 - The High King of Montival/01.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("s m stirling — the high king of montival", (item,), ()))
        self.assertEqual("Emberverse Series", hint["series"])
        self.assertEqual("7", hint["series_position"])

    def test_prefers_single_file_title_and_artist_over_series_album_artist(self) -> None:
        probe = ProbeResult(60, "m4b", "aac", 64000, 44100, 2,
                            {"album": "10 The Hard Way", "title": "The Hard Way",
                             "album_artist": "Jack Reacher", "artist": "Lee Child",
                             "grouping": "Jack Reacher"})
        item = ScannedFile(Path("/src/book.m4b"), Path("Jack Reacher/10 The Hard Way/book.m4b"), 1, 1, probe)
        hint = extract_hint(BookGroup("jack reacher — 10 the hard way", (item,), ()))
        self.assertEqual("The Hard Way", hint["title"])
        self.assertEqual(["Lee Child"], hint["authors"])
        self.assertEqual("Jack Reacher", hint["series"])

    def test_uses_equivalent_parts_tags_when_chosen_m4b_has_none(self) -> None:
        blank = ProbeResult(120, "m4b", "aac", 64000, 44100, 2, {})
        tagged = ProbeResult(60, "m4b", "aac", 64000, 44100, 2,
                             {"album": "A Game of Thrones", "artist": "George R. R. Martin",
                              "album_artist": "Roy Dotrice", "title": "Prologue"})
        finished = ScannedFile(Path("/src/finished.m4b"), Path("tmp/finished.m4b"), 1, 1, blank)
        part = ScannedFile(Path("/src/part.m4b"), Path("tmp/part.m4b"), 1, 1, tagged)
        hint = extract_hint(BookGroup("a game of thrones", (finished, part), ()), ("tmp/finished.m4b",))
        self.assertEqual("A Game of Thrones", hint["title"])
        self.assertEqual(["George R. R. Martin"], hint["authors"])
        self.assertEqual(["Roy Dotrice"], hint["narrators"])

    def test_rejects_track_fraction_as_title_and_infers_filename_author(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2, {"title": " - 08/16"})
        relative = Path("Cosmere/6.5 - Secret History - Normal Audio/Secret History - Brandon Sanderson.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("5 secret history normal audio", (item,), ()))
        self.assertEqual("secret history", hint["title"].casefold())
        self.assertEqual(["Brandon Sanderson"], hint["authors"])

    def test_splits_series_number_from_embedded_title(self) -> None:
        probe = ProbeResult(60, "m4b", "aac", 64000, 44100, 2,
                            {"album": "Dune 02 - Dune Messiah", "title": "Dune 02 - Dune Messiah",
                             "artist": "Frank Herbert"})
        item = ScannedFile(Path("/src/book.m4b"), Path("Dune/book.m4b"), 1, 1, probe)
        hint = extract_hint(BookGroup("frank herbert — dune 02 dune messiah", (item,), ()))
        self.assertEqual("Dune Messiah", hint["title"])
        self.assertEqual("Dune", hint["series"])
        self.assertEqual("02", hint["series_position"])

    def test_source_folder_repairs_equivalent_lowercase_author_tag(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "fear nothing", "artist": "dean koontz"})
        relative = Path("Dean Koontz/Moonlight Bay/Fear Nothing/01.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("dean koontz — fear nothing", (item,), ()))
        self.assertEqual(["Dean Koontz"], hint["authors"])

    def test_infers_author_from_author_title_filename(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2, {})
        relative = Path("Dean Koontz/1979 The Key to Midnight/Dean Koontz - The Key to Midnight.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("the key to midnight", (item,), ()))
        self.assertEqual(["Dean Koontz"], hint["authors"])

    def test_infers_name_like_top_level_author_folder(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2, {})
        relative = Path("D. J. Molles/The Remaining - Aftermath/01 - aftermath.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("the remaining aftermath", (item,), ()))
        self.assertEqual(["D. J. Molles"], hint["authors"])

    def test_does_not_treat_collection_folder_as_author(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2, {})
        relative = Path("Harry Potter Complete Audiobook Collection/Book/01.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("harry potter", (item,), ()))
        self.assertEqual([], hint["authors"])

    def test_extracts_book_and_series_from_split_long_collection_file(self) -> None:
        probe = ProbeResult(36000, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "Malazan Book of the Fallen", "artist": "Steven Erikson"})
        relative = Path("Steven Erikson/Series/Malazan Book of the Fallen - 01 - Gardens of the Moon.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("steven erikson — gardens of the moon", (item,),
                                      ("long-form collection files split into individual books",)))
        self.assertEqual("Gardens of the Moon", hint["title"])
        self.assertEqual("Malazan Book of the Fallen", hint["series"])
        self.assertEqual("01", hint["series_position"])

    def test_infers_author_from_series_author_collection_folder(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2, {})
        relative = Path("The Cosmere Series - Brandon Sanderson/Book/01.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("book", (item,), ()))
        self.assertEqual(["Brandon Sanderson"], hint["authors"])

    def test_does_not_infer_tmp_series_prefix_or_title_as_author(self) -> None:
        probe = ProbeResult(60, "m4b", "aac", 64000, 44100, 2, {})
        relative = Path("Emberverse Series - Book 3 - A Meeting at Corvallis-tmpfiles/"
                        "tmp_Emberverse Series - Book 3 - A Meeting at Corvallis.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("a meeting at corvallis", (item,), ()))
        self.assertEqual([], hint["authors"])

    def test_extracts_series_from_same_book_directory_name(self) -> None:
        probe = ProbeResult(60, "m4b", "aac", 64000, 44100, 2, {})
        relative = Path("Emberverse Series - Book 3 - A Meeting at Corvallis-tmpfiles/book.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("a meeting at corvallis", (item,), ()))
        self.assertEqual("Emberverse Series", hint["series"])
        self.assertEqual("3", hint["series_position"])

    def test_skips_alt_versions_folder_when_extracting_series(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2, {})
        relative = Path("Dean Koontz/Frankenstein Series/Alt. Versions/Book 1 - Prodigal Son/book.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("prodigal son", (item,), ()))
        self.assertEqual("Frankenstein Series", hint["series"])
        self.assertEqual("1", hint["series_position"])

    def test_repeated_track_title_and_bracketed_folder_repair_generic_album(self) -> None:
        probe = ProbeResult(60, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "Legacy of The Force - Book 2", "title": "Bloodlines",
                             "artist": "Marc Thompson", "album_artist": "Marc Thompson"})
        files = tuple(ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
                      for relative in (
                          Path("Star Wars AudioBooks/100-Bloodlines[Legacy Of The Force Book 2]/01.mp3"),
                          Path("Star Wars AudioBooks/100-Bloodlines[Legacy Of The Force Book 2]/02.mp3")))
        hint = extract_hint(BookGroup("marc thompson — legacy of the force book 2", files, ()))
        self.assertEqual("Bloodlines", hint["title"])
        self.assertEqual("Legacy Of The Force", hint["series"])
        self.assertEqual("2", hint["series_position"])

    def test_removes_empty_parentheses_left_by_unabridged_marker(self) -> None:
        probe = ProbeResult(60, "m4b", "aac", 64000, 44100, 2,
                            {"album": "The Given Sacrifice (Unabridged)",
                             "artist": "S. M. Stirling"})
        relative = Path("Emberverse Series/Book 10 - The Given Sacrifice/book.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("the given sacrifice", (item,), ()))
        self.assertEqual("The Given Sacrifice", hint["title"])

    def test_single_complete_file_prefers_album_over_end_credits_title(self) -> None:
        probe = ProbeResult(3600, "m4b", "aac", 64000, 44100, 2,
                            {"album": "Implode", "title": "End Credits",
                             "artist": "Dakota Krout"})
        relative = Path("Dakota Krout/Implode/book.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("dakota krout — implode", (item,), ()))
        self.assertEqual("Implode", hint["title"])

    def test_packaging_word_is_removed_from_title(self) -> None:
        probe = ProbeResult(3600, "m4b", "aac", 64000, 44100, 2,
                            {"album": "Invent Audiobook", "artist": "Dakota Krout"})
        relative = Path("Dakota Krout - Invent Audiobook/book.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("dakota krout — invent audiobook", (item,), ()))
        self.assertEqual("Invent", hint["title"])

    def test_duration_matched_group_identity_beats_a_garbled_whole_file_tag(self) -> None:
        probe = ProbeResult(3600, "mp3", "mp3", 64000, 44100, 2,
                            {"title": "OddInterludeUnabridged_mp332_timberattler61"})
        relative = Path("Dean Koontz/Odd Interlude/whole.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup(
            "dean koontz — odd interlude", (item,),
            ("duration-matched alternate representation joined to its source book",),
        ))
        self.assertEqual("odd interlude", hint["title"].casefold())

    def test_part_number_is_removed_from_a_grouped_book_title(self) -> None:
        probe = ProbeResult(3600, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "Ticktock (Unabridged), Part 1",
                             "artist": "Dean Koontz"})
        relative = Path("Dean Koontz/Ticktock/01.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("dean koontz — ticktock", (item,), ()))
        self.assertEqual("Ticktock", hint["title"])

    def test_graphic_audio_parts_keep_edition_but_not_production_author(self) -> None:
        probe = ProbeResult(3600, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "Warbreaker [01]", "artist": "GraphicAudio"})
        relative = Path(
            "The Cosmere Series - Brandon Sanderson/11 - Warbreaker - GA/Part 1/book.mp3"
        )
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup(
            "graphicaudio — warbreaker graphic audio", (item,),
            ("GraphicAudio edition parts grouped",),
        ))
        self.assertEqual("warbreaker", hint["title"].casefold())
        self.assertEqual(["Brandon Sanderson"], hint["authors"])
        self.assertEqual("GraphicAudio", hint["edition"])

    def test_graphic_audio_folder_supplies_series_and_position(self) -> None:
        probe = ProbeResult(3600, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "The Way of Kings [01]",
                             "artist": "GraphicAudio [Brandon Sanderson]"})
        relative = Path(
            "The Cosmere Series - Brandon Sanderson/"
            "12 - Stormlight Archive - 1 - The Way of Kings - GA/Part 1/book.mp3"
        )
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup(
            "graphicaudio — the way of kings graphic audio", (item,), ()
        ))
        self.assertEqual("Stormlight Archive", hint["series"])
        self.assertEqual("1", hint["series_position"])

    def test_harry_potter_collection_code_and_written_by_credit_are_removed(self) -> None:
        probe = ProbeResult(3600, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "HP-6 Harry Potter And The Half-Blood Prince",
                             "artist": "Written By J. K. Rowling, Narrated By Jim Dale"})
        relative = Path("Harry Potter Complete Audiobook Collection/book.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("harry potter 6", (item,), ()))
        self.assertEqual("Harry Potter And The Half-Blood Prince", hint["title"])
        self.assertEqual(["J. K. Rowling"], hint["authors"])

    def test_bitrate_marker_is_removed_without_losing_edition_label(self) -> None:
        probe = ProbeResult(3600, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "Legacy [Prequel] [64vbr]", "artist": "Greg Bear"})
        relative = Path("Greg Bear/Legacy/book.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("greg bear legacy", (item,), ()))
        self.assertEqual("Legacy [Prequel]", hint["title"])

    def test_decimal_book_number_at_end_is_not_reduced_to_fraction(self) -> None:
        probe = ProbeResult(3600, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "A Warm Heart in Winter, Book 18.5",
                             "artist": "J. R. Ward", "title": "Part 1"})
        relative = Path("J.R.Ward/B18.5 A Warm Heart in Winter/part01.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("a warm heart in winter", (item,), ()))
        self.assertEqual("A Warm Heart in Winter, Book 18.5", hint["title"])

    def test_placeholder_album_does_not_replace_structural_title(self) -> None:
        probe = ProbeResult(1800, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "Unknown", "artist": "Robert Jordan, Brandon Sanderson",
                             "title": "A Memory of Light, Chapter 01"})
        relative = Path("Robert Jordan/Book 14 - A Memory of Light/01.mp3")
        first = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        second_probe = ProbeResult(1800, "mp3", "mp3", 64000, 44100, 2,
                                   {**probe.tags, "title": "A Memory of Light, Chapter 02"})
        second_relative = Path("Robert Jordan/Book 14 - A Memory of Light/02.mp3")
        second = ScannedFile(Path("/src") / second_relative, second_relative, 1, 1,
                             second_probe)
        hint = extract_hint(BookGroup("robert jordan — a memory of light",
                                      (first, second), ()))
        self.assertEqual("a memory of light", hint["title"].casefold())

    def test_split_collection_container_uses_structural_title_and_album_as_series(self) -> None:
        probe = ProbeResult(45000, "m4b", "aac", 96000, 44100, 2,
                            {"album": "Legend of Drizzt: Neverwinter Saga",
                             "artist": "R. A. Salvatore"})
        relative = Path("R.A Salvatore/Charon's Claw/Legend of Drizzt - Book 22.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        group = BookGroup(
            "r a salvatore — charon s claw", (item,),
            ("audiobook-sized containers split from a shared collection album",),
        )
        hint = extract_hint(group, (str(relative),))
        self.assertEqual("charon s claw", hint["title"].casefold())
        self.assertEqual("Legend of Drizzt: Neverwinter Saga", hint["series"])
        self.assertEqual("22", hint["series_position"])

    def test_split_container_keeps_specific_embedded_book_title(self) -> None:
        probe = ProbeResult(
            3600, "m4b", "aac", 96000, 44100, 2,
            {"album": "He Who Fights with Monsters, Book 02",
             "title": "He Who Fights with Monsters, Book 02",
             "artist": "Shirtaloon", "series": "He Who Fights with Monsters"},
        )
        relative = Path(
            "He Who Fights with Monsters/Book 02/He Who Fights with Monsters, Book 02.m4b"
        )
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup(
            "shirtaloon — book 02", (item,),
            ("audiobook-sized containers split from a shared collection album",),
        ))
        self.assertEqual("he who fights with monsters, book 02", hint["title"].casefold())
        self.assertEqual("He Who Fights with Monsters", hint["series"])

    def test_split_container_keeps_album_supported_by_filename(self) -> None:
        probe = ProbeResult(
            3600, "m4a", "aac", 96000, 44100, 2,
            {"album": "2015 - Aurora", "artist": "Kim Stanley Robinson"},
        )
        relative = Path("Kim Stanley Robinson/2015 - Aurora.m4a")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup(
            "kim stanley robinson — kim stanley robinson", (item,),
            ("audiobook-sized containers split from a shared collection album",),
        ))
        self.assertEqual("Aurora", hint["title"])

    def test_author_named_parent_is_not_mistaken_for_series(self) -> None:
        probe = ProbeResult(3600, "m4b", "aac", 64000, 44100, 2,
                            {"album": "The Healer, Book 1", "artist": "Roman Romanovich"})
        relative = Path("Roman Romanovich/The Healer Book 1/book.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("roman romanovich — the healer book 1", (item,), ()))
        self.assertIsNone(hint["series"])
        self.assertFalse(hint["allow_author_repair"])

    def test_narrator_suffix_is_removed_before_online_author_matching(self) -> None:
        probe = ProbeResult(3600, "mp3", "mp3", 64000, 44100, 2,
                            {"album": "Brother Odd",
                             "artist": "Dean Koontz, read by David Aaron Baker"})
        relative = Path("Dean Koontz/Brother Odd/Brother Odd complete.mp3")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("brother odd", (item,), ()))
        self.assertEqual(["Dean Koontz"], hint["authors"])

    def test_generic_volume_suffix_does_not_replace_the_book_title(self) -> None:
        probe = ProbeResult(3600, "m4b", "aac", 64000, 44100, 2,
                            {"album": "Kuma Kuma Kuma Bear, Vol. 3: Light Novel",
                             "artist": "Kumanano"})
        relative = Path("Kuma Kuma Kuma Bear/Book 3/book.m4b")
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("kumanano — kuma kuma kuma bear vol 3", (item,), ()))
        self.assertEqual("Kuma Kuma Kuma Bear, Vol. 3: Light Novel", hint["title"])
        self.assertEqual("Kuma Kuma Kuma Bear", hint["series"])
        self.assertEqual("3", hint["series_position"])

    def test_tmp_folder_prefix_separates_author_title_and_series(self) -> None:
        probe = ProbeResult(3600, "m4b", "aac", 64000, 44100, 2,
                            {"album": "Implode", "artist": "Dakota Krout"})
        relative = Path(
            "Dakota Krout - Implode The Completionist Chronicles, Book 8-tmpfiles/book.m4b"
        )
        item = ScannedFile(Path("/src") / relative, relative, 1, 1, probe)
        hint = extract_hint(BookGroup("dakota krout — implode", (item,), ()))
        self.assertEqual("The Completionist Chronicles", hint["series"])
        self.assertEqual("8", hint["series_position"])
