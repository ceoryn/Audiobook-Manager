from __future__ import annotations

import unittest

from audiobook_manager.matching import choose_automatic


class MatchingTests(unittest.TestCase):
    def test_approves_clear_match_over_threshold(self) -> None:
        winner, ranked = choose_automatic(
            {"title": "The Courts of Chaos", "authors": ["Roger Zelazny"]},
            [{"title": "The Courts of Chaos", "authors": ["Roger Zelazny"]},
             {"title": "Nine Princes in Amber", "authors": ["Roger Zelazny"]}],
        )
        self.assertIsNotNone(winner)
        self.assertEqual(90, ranked[0].score)

    def test_ambiguous_high_matches_stay_for_review(self) -> None:
        winner, _ = choose_automatic(
            {"title": "Dune", "authors": ["Frank Herbert"]},
            [{"title": "Dune", "authors": ["Frank Herbert"]},
             {"title": "Dune", "authors": ["Frank P Herbert"]}],
            ambiguity_margin=10,
        )
        self.assertIsNone(winner)

    def test_runtime_conflict_penalizes_wrong_edition(self) -> None:
        winner, ranked = choose_automatic(
            {"title": "Dune", "authors": ["Frank Herbert"], "runtime_minutes": 1200},
            [{"title": "Dune", "authors": ["Frank Herbert"], "runtime_minutes": 300}],
        )
        self.assertIsNone(winner)
        self.assertLess(ranked[0].score, 80)

    def test_provider_series_subtitle_does_not_penalize_exact_title(self) -> None:
        winner, ranked = choose_automatic(
            {"title": "The Hard Way", "authors": ["Lee Child"], "series": "Jack Reacher"},
            [{"title": "The Hard Way: Jack Reacher, Book 10", "authors": ["Lee Child"]}],
        )
        self.assertIsNotNone(winner)
        self.assertEqual(95, ranked[0].score)

    def test_provider_ascii_dash_series_prefix_does_not_hide_exact_title(self) -> None:
        winner, ranked = choose_automatic(
            {"title": "Bloodlines", "authors": [], "series": "Legacy of the Force"},
            [{"title": "Star Wars - Legacy of the Force - Bloodlines",
              "authors": ["Karen Traviss"]}],
        )
        self.assertIsNotNone(winner)
        self.assertEqual(90, ranked[0].score)

    def test_duplicate_provider_works_do_not_create_false_ambiguity(self) -> None:
        winner, ranked = choose_automatic(
            {"title": "Calamity", "authors": ["Brandon Sanderson"]},
            [{"title": "Calamity", "authors": ["Brandon Sanderson"], "provider_id": "one"},
             {"title": "Calamity", "authors": ["Brandon Sanderson"], "provider_id": "duplicate"}],
        )
        self.assertIsNotNone(winner)
        self.assertEqual(1, len(ranked))

    def test_unique_exact_title_can_supply_missing_local_author(self) -> None:
        winner, ranked = choose_automatic(
            {"title": "The Courts of Chaos", "authors": []},
            [{"title": "The Courts of Chaos", "authors": ["Roger Zelazny"]}],
        )
        self.assertIsNotNone(winner)
        self.assertEqual(85, ranked[0].score)

    def test_same_exact_title_by_different_authors_stays_ambiguous(self) -> None:
        winner, _ = choose_automatic(
            {"title": "The City", "authors": []},
            [{"title": "The City", "authors": ["Author One"]},
             {"title": "The City", "authors": ["Author Two"]}],
        )
        self.assertIsNone(winner)

    def test_generic_subtitle_does_not_match_unrelated_unicode_title(self) -> None:
        winner, ranked = choose_automatic(
            {"title": "Kuma Kuma Kuma Bear, Vol. 3: Light Novel", "authors": []},
            [{"title": "薬屋のひとりごと (light novel)", "authors": ["日向夏"]}],
        )
        self.assertIsNone(winner)
        self.assertLess(ranked[0].score, 80)

    def test_unicode_titles_match_themselves(self) -> None:
        winner, _ = choose_automatic(
            {"title": "薬屋のひとりごと", "authors": ["日向夏"]},
            [{"title": "薬屋のひとりごと", "authors": ["日向夏"]}],
        )
        self.assertIsNotNone(winner)

    def test_catalog_result_without_a_title_cannot_abort_matching(self) -> None:
        winner, ranked = choose_automatic(
            {"title": "Dune", "authors": ["Frank Herbert"]},
            [{"title": "", "authors": ["Frank Herbert"]}],
        )
        self.assertIsNone(winner)
        self.assertEqual(1, len(ranked))
        self.assertLess(ranked[0].score, 80)


if __name__ == "__main__":
    unittest.main()
