from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.apply_new_import_relocation import apply


class NewImportRelocationTests(unittest.TestCase):
    def test_moves_only_ledger_verified_copy_and_removes_empty_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "Audiobooks"
            source = root / "B.V. Larson" / "Star Force" / "08 - Storm Assault" / "B.V. Larson - Storm Assault [B00FJ90DKO].m4b"
            target = root / "B. V. Larson" / "Star Force" / "08 - Storm Assault" / "B. V. Larson - Storm Assault [B00FJ90DKO].m4b"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"generated fixture")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            ledger_path = base / "ledger.json"
            ledger_path.write_text(json.dumps({str(source): {
                "asin": "B00FJ90DKO", "size": source.stat().st_size, "sha256": digest,
                "remote_source": "Books/fixture.m4b"}}))
            plan = {"mode": "dry-run", "jupiter_root": str(root), "source": str(source),
                    "destination": str(target), "asin": "B00FJ90DKO",
                    "size": source.stat().st_size, "sha256": digest}
            with patch("pathlib.Path.is_mount", return_value=True):
                result = apply(plan, ledger_path)
            self.assertEqual(digest, result["sha256"])
            self.assertFalse(source.exists())
            self.assertTrue(target.is_file())
            self.assertFalse(root.joinpath("B.V. Larson").exists())
            ledger = json.loads(ledger_path.read_text())
            self.assertNotIn(str(source), ledger)
            self.assertEqual(str(source), ledger[str(target)]["relocated_from"])

    def test_destination_collision_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "Audiobooks"
            source = root / "B.V. Larson" / "source [B00FJ90DKO].m4b"
            target = root / "B. V. Larson" / "target [B00FJ90DKO].m4b"
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            target.write_bytes(b"existing")
            digest = hashlib.sha256(b"source").hexdigest()
            ledger_path = base / "ledger.json"
            ledger_path.write_text(json.dumps({str(source): {
                "asin": "B00FJ90DKO", "size": 6, "sha256": digest}}))
            plan = {"mode": "dry-run", "jupiter_root": str(root), "source": str(source),
                    "destination": str(target), "asin": "B00FJ90DKO", "size": 6, "sha256": digest}
            with patch("pathlib.Path.is_mount", return_value=True):
                with self.assertRaises(FileExistsError):
                    apply(plan, ledger_path)
            self.assertEqual(b"source", source.read_bytes())
            self.assertEqual(b"existing", target.read_bytes())


if __name__ == "__main__":
    unittest.main()
