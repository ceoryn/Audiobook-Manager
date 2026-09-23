from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.apply_library_repair import execute_plan
from scripts.audit_library_alignment import ProbeSummary


class LibraryRepairExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source_root = root / "source"
        self.output_root = root / "output"
        self.source_root.mkdir()
        self.output_root.mkdir()
        self.database = root / "state.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """CREATE TABLE process_runs (
                       id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                       started_at TEXT, finished_at TEXT
                   )"""
            )
            connection.execute(
                "INSERT INTO process_runs VALUES (3, 'complete', 'start', 'finish')"
            )

        self.redundant = self.output_root / "Dean Koontz" / "Old" / "Ticktock.m4b"
        self.reference = self.output_root / "Dean Koontz" / "Ticktock" / "Ticktock.m4b"
        self.redundant.parent.mkdir(parents=True)
        self.reference.parent.mkdir(parents=True)
        self.redundant.write_bytes(b"redundant")
        self.cover = self.redundant.parent / "cover.jpg"
        self.cover.write_bytes(b"cover")
        self.reference.write_bytes(b"current")
        relative = self.redundant.relative_to(self.output_root)
        self.quarantine = self.output_root / "_quarantine" / "repair-1"
        self.destination = self.quarantine / relative
        self.plan = {
            "mode": "dry-run",
            "actions_applied": 0,
            "process_run": {
                "id": 3,
                "status": "complete",
                "started_at": "start",
                "finished_at": "finish",
            },
            "roots": {
                "database": str(self.database),
                "source": str(self.source_root),
                "output": str(self.output_root),
            },
            "safety": {"quarantine_root_if_approved": str(self.quarantine)},
            "post_rebuild_quarantine_operations": [
                {
                    "action": "quarantine_redundant_output",
                    "source": str(self.redundant),
                    "relative_path": str(relative),
                    "destination": str(self.destination),
                    "reference_output": str(self.reference),
                    "observed_author": "Dean Koontz",
                    "observed_title": "Ticktock",
                }
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _probe(path: Path) -> ProbeSummary:
        return ProbeSummary(
            path=str(path), error=None, duration_seconds=1000.0, codec="aac",
            author_tags=("Dean Koontz",), title_tags=("Ticktock",),
            series_tag=None, chapters=1,
        )

    @patch("scripts.apply_library_repair.probe_summary", side_effect=_probe)
    def test_preflight_changes_nothing(self, _probe_mock: object) -> None:
        result, log = execute_plan(self.plan, approve=False)
        self.assertEqual("preflight-only", result["mode"])
        self.assertEqual(0, result["operations_moved"])
        self.assertTrue(self.redundant.exists())
        self.assertTrue(self.cover.exists())
        self.assertFalse(self.destination.exists())
        self.assertIsNone(log)

    @patch("scripts.apply_library_repair.probe_summary", side_effect=_probe)
    def test_approved_plan_moves_to_recoverable_quarantine(self, _probe_mock: object) -> None:
        result, log = execute_plan(self.plan, approve=True)
        self.assertEqual(1, result["operations_moved"])
        self.assertFalse(self.redundant.exists())
        self.assertEqual(b"redundant", self.destination.read_bytes())
        self.assertEqual(1, result["sidecars_moved"])
        self.assertFalse(self.cover.exists())
        self.assertEqual(b"cover", (self.destination.parent / "cover.jpg").read_bytes())
        self.assertIsNotNone(log)
        self.assertTrue(log.is_file())

    def test_rejects_plan_when_newer_run_exists(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO process_runs VALUES (4, 'complete', 'new', 'new-finish')"
            )
        with self.assertRaisesRegex(ValueError, "stale"):
            execute_plan(self.plan, approve=False)


if __name__ == "__main__":
    unittest.main()
