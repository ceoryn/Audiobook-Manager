from __future__ import annotations

import unittest

from audiobook_manager.catalog import classify_catalog


class CatalogTests(unittest.TestCase):
    def test_classifies_every_scan_group(self) -> None:
        scan = {"groups": [{"key": "done", "files": ["done.m4b"]}, {"key": "parts", "files": ["01.mp3", "02.mp3"]}],
                "files": [{"relative_path": "done.m4b", "error": None}, {"relative_path": "01.mp3", "error": None}, {"relative_path": "02.mp3", "error": None}]}
        result = classify_catalog(scan)
        self.assertEqual(2, len(result))
        self.assertEqual("existing_m4b", result[0]["classification"])
        self.assertEqual("multipart_conversion", result[1]["classification"])
