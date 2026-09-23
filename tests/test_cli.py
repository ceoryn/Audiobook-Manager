from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from audiobook_manager.cli import main


class CliTests(unittest.TestCase):
    def test_no_cache_scan_emits_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["scan", directory, "--no-cache"])
            self.assertEqual(status, 0)
            self.assertIn('"media_files": 0', output.getvalue())

    def test_refuses_database_inside_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                status = main(["scan", directory, "--database", str(root / "state.sqlite3")])
            self.assertEqual(status, 2)
            self.assertIn("inside the scanned library", errors.getvalue())

    def test_refuses_report_inside_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                status = main(["scan", directory, "--no-cache", "--report", str(Path(directory) / "scan.json")])
            self.assertEqual(status, 2)
            self.assertIn("inside the scanned library", errors.getvalue())

    def test_analyze_scan_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scan_path = root / "scan.json"
            output_path = root / "output" / "lineage.json"
            scan_path.write_text(
                json.dumps({"schema_version": 1, "library_root": str(root / "library"), "files": [], "groups": []}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["analyze", str(scan_path), "--report", str(output_path)])
            self.assertEqual(status, 0)
            self.assertTrue(output_path.is_file())
            self.assertIn("Analyzed 0 groups", output.getvalue())


if __name__ == "__main__":
    unittest.main()
