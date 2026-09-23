import tempfile
import unittest
from pathlib import Path

from scripts.audit_output_folders import apply, inventory


class FolderAuditTests(unittest.TestCase):
    def test_preserves_audio_hidden_files_and_changed_artwork(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "output"
            for name in ("empty", "art", "changed", "book", "hidden", "_quarantine/empty"):
                (root / name).mkdir(parents=True)
            for name in ("art", "changed", "book"):
                (root / name / "cover.jpg").write_bytes(b"cover")
            (root / "book/book.m4b").write_bytes(b"audio")
            (root / "hidden/.keep").touch()
            plan = inventory(root)
            plan["stamp"] = "fixture"
            (root / "changed/book.m4b").write_bytes(b"new audio")
            result = apply(plan, Path(d) / "log.json")
            self.assertEqual(["art"], result["archived"])
            self.assertTrue((root / "_quarantine/folder-hygiene-fixture/art/cover.jpg").exists())
            self.assertTrue((root / "changed/book.m4b").exists())
            self.assertTrue((root / "book/book.m4b").exists())
            self.assertTrue((root / "hidden/.keep").exists())
            self.assertTrue((root / "_quarantine/empty").exists())
            self.assertFalse((root / "empty").exists())
