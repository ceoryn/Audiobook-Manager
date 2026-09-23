from __future__ import annotations

import unittest
from pathlib import Path

from audiobook_manager.lineage import analyze_group, analyze_scan_report, build_family_groups, confidence_band
from audiobook_manager.models import BookGroup, Chapter, ProbeResult, ScannedFile


def media(
    relative: str,
    duration: float,
    *,
    chapters: int = 0,
    tags: dict[str, str] | None = None,
) -> ScannedFile:
    chapter_items = tuple(
        Chapter(index, index * duration / chapters, (index + 1) * duration / chapters, f"Chapter {index + 1}")
        for index in range(chapters)
    ) if chapters else ()
    probe = ProbeResult(
        duration,
        "mp3" if relative.endswith(".mp3") else "mov,mp4,m4a,3gp,3g2,mj2",
        "mp3" if relative.endswith(".mp3") else "aac",
        64000,
        44100,
        2,
        tags or {},
        chapter_items,
    )
    path = Path("/library") / relative
    return ScannedFile(path, Path(relative), 100, 1, probe)


class LineageTests(unittest.TestCase):
    def test_detects_combined_m4b_from_mp3_components(self) -> None:
        files = (
            media("Book/01.mp3", 100, tags={"album": "Book"}),
            media("Book/02.mp3", 120, tags={"album": "Book"}),
            media("Book/Book.m4b", 220.4, chapters=2, tags={"album": "Book"}),
        )
        analysis = analyze_group(BookGroup("Book", files, ("fixture",)))
        relation = next(item for item in analysis.relationships if item.relationship == "combined_from_mp3_components")
        self.assertGreaterEqual(relation.confidence, 95)
        self.assertEqual(relation.target_file, "Book/Book.m4b")
        self.assertFalse(relation.cleanup_eligible)

    def test_detects_parallel_intermediate_conversions(self) -> None:
        files = (
            media("Book/01.mp3", 100),
            media("Book/02.mp3", 120),
            media("Book/01.m4b", 100.3),
            media("Book/02.m4b", 119.8),
        )
        analysis = analyze_group(BookGroup("Book", files, ("fixture",)))
        relation = next(item for item in analysis.relationships if item.relationship == "parallel_intermediate_conversion")
        self.assertEqual(relation.confidence_band, "medium")
        self.assertFalse(relation.cleanup_eligible)

    def test_legacy_tmpfiles_layout_reaches_reviewable_medium_confidence(self) -> None:
        files = (
            media("Author-Book-tmpfiles/1-finished.m4b", 100),
            media("Author-Book-tmpfiles/2-finished.m4b", 120),
            media("Author-Book-tmpfiles/tmp_Author-Book.m4b", 220.2),
        )
        analysis = analyze_group(BookGroup("Author-Book-tmpfiles", files, ("fixture",)))
        relation = next(item for item in analysis.relationships if item.relationship == "combined_from_m4b_components")
        self.assertEqual(relation.confidence_band, "medium")
        self.assertTrue(analysis.review_reasons)
        self.assertFalse(relation.cleanup_eligible)

    def test_duration_mismatch_does_not_create_combined_claim(self) -> None:
        files = (
            media("Book/01.mp3", 100),
            media("Book/02.mp3", 120),
            media("Book/Book.m4b", 900, chapters=2),
        )
        analysis = analyze_group(BookGroup("Book", files, ("fixture",)))
        self.assertFalse(any(item.relationship == "combined_from_mp3_components" for item in analysis.relationships))
        self.assertTrue(analysis.review_reasons)

    def test_unrelated_multiple_files_require_review(self) -> None:
        files = (media("Mixed/Alpha.m4b", 100), media("Mixed/Beta.m4b", 900))
        analysis = analyze_group(BookGroup("Mixed", files, ("fixture",)))
        self.assertEqual(analysis.relationships, ())
        self.assertIn("no supported lineage conclusion", analysis.review_reasons[0])

    def test_error_requires_review(self) -> None:
        broken = ScannedFile(Path("/library/Book/bad.mp3"), Path("Book/bad.mp3"), 0, 0, None, "bad")
        analysis = analyze_group(BookGroup("Book", (broken,), ("fixture",)))
        self.assertIn("could not be probed", analysis.review_reasons[0])

    def test_report_explicitly_disables_cleanup_eligibility(self) -> None:
        source = media("Book/01.mp3", 100)
        target = media("Book/Book.m4b", 100, chapters=1)
        scan = {
            "schema_version": 1,
            "library_root": "/library",
            "files": [source.to_dict(), target.to_dict()],
            "groups": [{"key": "Book", "files": ["Book/01.mp3", "Book/Book.m4b"], "reasons": []}],
        }
        report = analyze_scan_report(scan)
        self.assertTrue(report["safety"]["read_only_analysis"])
        self.assertEqual(report["safety"]["cleanup_eligible_relationships"], 0)

    def test_confidence_thresholds(self) -> None:
        self.assertEqual(confidence_band(95), "high")
        self.assertEqual(confidence_band(80), "medium")
        self.assertEqual(confidence_band(50), "low")
        self.assertEqual(confidence_band(49.9), "very_low")

    def test_temp_conversion_group_joins_strongly_named_source_group(self) -> None:
        source = BookGroup(
            "Author/Brandon Sanderson-Warbreaker",
            (media("Author/Brandon Sanderson-Warbreaker/01.mp3", 100),),
            ("source",),
        )
        temporary = BookGroup(
            "Brandon Sanderson - Brandon Sanderson-Warbreaker-tmpfiles",
            (media("Brandon Sanderson - Brandon Sanderson-Warbreaker-tmpfiles/01-finished.m4b", 100),),
            ("temporary",),
        )
        groups = build_family_groups([source, temporary])
        family = next(group for group in groups if group.key.startswith("family:"))
        self.assertEqual(len(family.files), 2)
        self.assertIn("analysis-only name match", family.reasons[-1])

    def test_temp_group_does_not_join_on_one_generic_token(self) -> None:
        source = BookGroup("Author/Another Dune", (media("Author/Another Dune/01.mp3", 100),), ())
        temporary = BookGroup("Mentats of Dune-tmpfiles", (media("Mentats of Dune-tmpfiles/01.m4b", 100),), ())
        groups = build_family_groups([source, temporary])
        family = [group for group in groups if group.key.startswith("family:")]
        self.assertEqual(family, [])


if __name__ == "__main__":
    unittest.main()
