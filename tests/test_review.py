from __future__ import annotations

import unittest

from audiobook_manager.review import decision_status, filter_items, render_detail, render_queue, review_items


def analysis() -> dict[str, object]:
    return {
        "groups": [
            {
                "group_key": "Author/Book",
                "review_reasons": ["relationship requires confirmation"],
                "relationships": [
                    {
                        "relationship": "combined_from_mp3_components",
                        "source_files": ["Author/Book/01.mp3", "Author/Book/02.mp3"],
                        "target_file": "Author/Book/Book.m4b",
                        "confidence": 88.0,
                        "confidence_band": "medium",
                        "evidence": [{"kind": "duration", "points": 50.0, "explanation": "durations align"}],
                        "conflicts": ["chapters unavailable"],
                        "cleanup_eligible": False,
                    }
                ],
            }
        ]
    }


class ReviewTests(unittest.TestCase):
    def test_relationship_id_is_stable(self) -> None:
        first = review_items(analysis())[0]
        second = review_items(analysis())[0]
        self.assertEqual(first.relationship_id, second.relationship_id)
        self.assertEqual(len(first.relationship_id), 16)

    def test_changed_evidence_marks_decision_stale_without_changing_identity(self) -> None:
        original = review_items(analysis())[0]
        changed = analysis()
        changed["groups"][0]["relationships"][0]["confidence"] = 90.0  # type: ignore[index]
        updated = review_items(changed)[0]
        self.assertEqual(original.relationship_id, updated.relationship_id)
        decisions = {
            original.relationship_id: {
                "decision": "confirm",
                "evidence_fingerprint": original.evidence_fingerprint,
            }
        }
        self.assertEqual(decision_status(updated, decisions), ("confirm", True))

    def test_filter_and_render_queue(self) -> None:
        item = review_items(analysis())[0]
        filtered = filter_items([item], band="medium", decision="unreviewed", decisions={})
        rendered = render_queue(filtered, {}, limit=20)
        self.assertIn(item.relationship_id, rendered)
        self.assertIn("unreviewed", rendered)

    def test_render_detail_includes_evidence_conflicts_and_safety(self) -> None:
        item = review_items(analysis())[0]
        rendered = render_detail(item, {})
        self.assertIn("durations align", rendered)
        self.assertIn("chapters unavailable", rendered)
        self.assertIn("cannot modify or clean up", rendered)


if __name__ == "__main__":
    unittest.main()
