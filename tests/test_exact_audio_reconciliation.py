from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.apply_exact_audio_reconciliation import _stream_hash, execute


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
class ExactAudioReconciliationTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[dict[str, object], Path, Path, Path]:
        output = root / "output"
        current = output / "Author" / "Canonical" / "Author - Canonical.m4b"
        untracked = output / "Author" / "Old Name" / "Author - Old Name.m4b"
        current.parent.mkdir(parents=True)
        untracked.parent.mkdir(parents=True)
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.25", "-c:a", "aac", str(current),
            ],
            check=True,
        )
        shutil.copy2(current, untracked)
        database = root / "state.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE process_runs (id INTEGER, status TEXT, finished_at TEXT)"
        )
        connection.execute(
            "INSERT INTO process_runs VALUES (6, 'complete', '2026-09-14 00:17:49')"
        )
        connection.commit()
        connection.close()
        quarantine = output / "_quarantine" / "exact-audio-test"
        digest = _stream_hash(current)
        plan: dict[str, object] = {
            "mode": "dry-run",
            "actions_applied": 0,
            "roots": {"output": str(output), "database": str(database)},
            "process_run": {
                "id": 6,
                "status": "complete",
                "finished_at": "2026-09-14 00:17:49",
            },
            "safety": {"quarantine_root_if_approved": str(quarantine)},
            "quarantine_operations": [{
                "action": "quarantine_exact_audio_duplicate",
                "source": str(untracked),
                "relative_path": str(untracked.relative_to(output)),
                "destination": str(quarantine / untracked.relative_to(output)),
                "reference_output": str(current),
                "reference_book_id": "author — canonical",
                "audio_stream_sha256": digest,
            }],
        }
        return plan, current, untracked, quarantine

    def test_approved_exact_match_moves_only_untracked_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, current, untracked, quarantine = self._fixture(Path(directory))
            result, log = execute(plan, approve=True, workers=2)
            self.assertEqual(1, result["operations_moved"])
            self.assertTrue(current.is_file())
            self.assertFalse(untracked.exists())
            self.assertTrue((quarantine / untracked.relative_to(current.parents[2])).is_file())
            self.assertTrue(log and log.is_file())

    def test_changed_digest_is_rejected_before_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, current, untracked, _ = self._fixture(Path(directory))
            plan["quarantine_operations"][0]["audio_stream_sha256"] = "0" * 64  # type: ignore[index]
            with self.assertRaisesRegex(ValueError, "identity changed"):
                execute(plan, approve=True, workers=2)
            self.assertTrue(current.is_file())
            self.assertTrue(untracked.is_file())


if __name__ == "__main__":
    unittest.main()
