import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.audit_library_completeness import archive_inventory


class CompletenessAuditTests(unittest.TestCase):
    def test_archive_audio_is_counted_even_when_no_unpacked_files_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "book.zab"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("Book/01.mp3", b"audio one")
                zipped.writestr("Book/02.mp3", b"audio two")
                zipped.writestr("Book/cover.jpg", b"art")
            result = archive_inventory(archive, root, {})
            self.assertEqual(2, result["audio_members"])
            self.assertEqual(2, result["members_without_unpacked_candidate"])
            self.assertEqual(None, result["error"])

    def test_bad_archive_is_reported_without_aborting_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "broken.zab"
            archive.write_bytes(b"broken")
            self.assertTrue(archive_inventory(archive, root, {})["error"])
