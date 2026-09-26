from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from audiobook_manager.database import StateDatabase
from audiobook_manager.providers import identify_metadata, search_providers


class Good:
    name = "good"
    def search(self, query: str) -> list[dict[str, object]]:
        return [{"title": query}]


class Bad:
    name = "bad"
    def search(self, query: str) -> list[dict[str, object]]:
        raise OSError(f"offline during {query}")


class Transient:
    name = "transient"
    def __init__(self) -> None:
        self.calls = 0
    def search(self, query: str) -> list[dict[str, object]]:
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporary timeout")
        return [{"title": query}]


class Recording:
    name = "recording"
    def __init__(self) -> None:
        self.queries: list[str] = []
    def search(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        return [{"title": "Dune", "authors": ["Frank Herbert"]}]


class PoorCasing:
    name = "poor_casing"
    def search(self, query: str) -> list[dict[str, object]]:
        del query
        return [{"title": "fear nothing", "authors": ["dean koontz"]},
                {"title": "Fear Nothing", "authors": ["Dean Koontz", "Phil Parks"]}]


class WrongLocalAuthor:
    name = "wrong_local_author"
    def search(self, query: str) -> list[dict[str, object]]:
        del query
        return [{"title": "Hard Eight", "authors": ["Janet Evanovich"]}]


class SeriesFocused:
    name = "series_focused"
    def __init__(self) -> None:
        self.queries: list[str] = []
    def search(self, query: str) -> list[dict[str, object]]:
        self.queries.append(query)
        if query == "Bloodlines Legacy of the Force":
            return [{"title": "Bloodlines", "authors": ["Karen Traviss"]}]
        return []


class NativeThenEnglish:
    name = "native_then_english"
    def search(self, query: str) -> list[dict[str, object]]:
        if "author:" in query or "くまなの" in query:
            return [{"title": "Kuma Kuma Kuma Bear, Vol. 6", "authors": ["くまなの"]}]
        return [{"title": "Kuma Kuma Kuma Bear (Light Novel) Vol. 6",
                 "authors": ["Kumanano"]}]


class ProviderTests(unittest.TestCase):
    def test_latin_preference_searches_past_native_script_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, _ = identify_metadata(
                    {"title": "Kuma Kuma Kuma Bear, Vol. 6", "authors": ["くまなの"],
                     "series": "Kuma Kuma Kuma Bear Light Novel", "series_position": "6"},
                    [NativeThenEnglish()], database, prefer_latin=True,
                )
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(["Kumanano"], chosen.candidate["authors"])

    def test_latin_preference_holds_native_only_match_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = Recording()
            provider.search = lambda query: [{  # type: ignore[method-assign]
                "title": "くま クマ 熊 ベアー", "authors": ["くまなの"]
            }]
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, _ = identify_metadata(
                    {"title": "くま クマ 熊 ベアー", "authors": ["くまなの"]},
                    [provider], database, threshold=60, prefer_latin=True,
                )
        self.assertIsNone(chosen)

    def test_provider_failure_does_not_discard_other_results(self) -> None:
        results, failures = search_providers("Dune", [Bad(), Good()])
        self.assertEqual([{"title": "Dune"}], results)
        self.assertEqual(1, len(failures))

    def test_identification_uses_persistent_provider_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = Good()
            chosen, _, _ = identify_metadata({"title": "Dune", "authors": []}, [provider], database, threshold=60)
            self.assertIsNotNone(chosen)
            cached = database.cached_metadata("good", "Dune")
            self.assertEqual([{"title": "Dune"}], cached)

    def test_identification_retries_transient_provider_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = Transient()
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, failures = identify_metadata(
                    {"title": "Dune", "authors": []}, [provider], database, threshold=60)
            self.assertIsNotNone(chosen)
            self.assertEqual([], failures)
            self.assertEqual(2, provider.calls)

    def test_identification_tries_fielded_title_author_query_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = Recording()
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, _ = identify_metadata(
                    {"title": "Dune", "authors": ["Frank Herbert"]}, [provider], database)
            self.assertIsNotNone(chosen)
            self.assertEqual('title:"Dune" author:"Frank Herbert"', provider.queries[0])

    def test_chosen_identity_uses_better_equivalent_display_casing(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            chosen, _, _ = identify_metadata(
                {"title": "Fear Nothing", "authors": ["Dean Koontz"]},
                [PoorCasing()], database)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual("Fear Nothing", chosen.candidate["title"])
        self.assertEqual(["Dean Koontz"], chosen.candidate["authors"])

    def test_matched_metadata_keeps_strong_local_series_structure(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = Recording()
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, _ = identify_metadata(
                    {"title": "Dune", "authors": ["Frank Herbert"],
                     "series": "Dune", "series_position": "1"}, [provider], database)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual("Dune", chosen.candidate["series"])
        self.assertEqual("1", chosen.candidate["series_position"])

    def test_matched_metadata_preserves_graphic_audio_edition_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = Recording()
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, _ = identify_metadata(
                    {"title": "Dune", "authors": ["Frank Herbert"],
                     "edition": "GraphicAudio"}, [provider], database)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual("Dune (GraphicAudio)", chosen.candidate["title"])
        self.assertEqual("GraphicAudio", chosen.candidate["edition"])

    def test_unique_exact_title_does_not_override_supported_local_author(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = WrongLocalAuthor()
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, _ = identify_metadata(
                    {"title": "Hard Eight", "authors": ["Stephanie Plum"]}, [provider], database)
        self.assertIsNone(chosen)

    def test_latin_display_preference_preserves_supported_local_author(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = Recording()
            provider.search = lambda query: [{  # type: ignore[method-assign]
                "title": "Untapped", "authors": ["Unrelated Catalog Author"]
            }]
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, _ = identify_metadata(
                    {"title": "Untapped", "authors": ["Supported Source Author"]},
                    [provider], database, prefer_latin=True,
                )
        self.assertIsNone(chosen)

    def test_latin_display_preference_keeps_author_for_mixed_script_credits(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = Recording()
            provider.search = lambda query: [{  # type: ignore[method-assign]
                "title": "Example", "authors": ["Another Author"]
            }]
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, _ = identify_metadata(
                    {"title": "Example", "authors": ["Known Author", "くまなの"]},
                    [provider], database, prefer_latin=True,
                )
        self.assertIsNone(chosen)

    def test_series_query_repairs_narrator_stored_as_local_artist(self) -> None:
        with tempfile.TemporaryDirectory() as directory, StateDatabase(Path(directory) / "state.db") as database:
            provider = SeriesFocused()
            with patch("audiobook_manager.providers.time.sleep"):
                chosen, _, _ = identify_metadata(
                    {"title": "Bloodlines", "authors": ["Marc Thompson"],
                     "series": "Legacy of the Force", "series_position": "2",
                     "allow_author_repair": True},
                    [provider], database)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(["Karen Traviss"], chosen.candidate["authors"])
        self.assertIn("Bloodlines Legacy of the Force", provider.queries)
