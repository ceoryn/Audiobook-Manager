from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from audiobook_manager.configuration import browse_directories
from audiobook_manager.controller import ProcessController
from audiobook_manager.database import StateDatabase
from audiobook_manager.review import review_items
from audiobook_manager.webserver import ReviewRequestHandler, build_review_payload


class WebServerPayloadTests(unittest.TestCase):
    def test_folder_listing_contains_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "library").mkdir()
            (root / "book.m4b").write_bytes(b"fixture")
            listing = browse_directories(root)
        self.assertEqual(["library"], [item["name"] for item in listing["directories"]])

    def test_payload_exposes_relationships_and_decision_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "analysis.json"
            database = root / "state.sqlite3"
            report.write_text(
                json.dumps(
                    {
                        "library_root": "/library",
                        "summary": {"relationships": 1, "groups_analyzed": 1},
                        "safety": {"read_only_analysis": True},
                        "groups": [
                            {
                                "group_key": "Book",
                                "review_reasons": ["review"],
                                "relationships": [
                                    {
                                        "relationship": "combined_from_mp3_components",
                                        "source_files": ["Book/01.mp3", "Book/02.mp3"],
                                        "target_file": "Book/Book.m4b",
                                        "confidence": 80,
                                        "confidence_band": "medium",
                                        "evidence": [],
                                        "conflicts": [],
                                        "cleanup_eligible": False,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            payload = build_review_payload(report, database, root / "outputs")
            self.assertEqual(payload["decisionCounts"]["unreviewed"], 1)
            self.assertEqual(payload["items"][0]["status"], "unreviewed")
            self.assertFalse(payload["items"][0]["cleanup_eligible"])
            self.assertEqual(payload["outputRoot"], str((root / "outputs").resolve()))


class WebServerConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "state.sqlite3"
        self.config = self.root / "config.json"
        controller = ProcessController(self.database)
        handler = type(
            "TestReviewHandler",
            (ReviewRequestHandler,),
            {
                "analysis_path": self.root / "analysis.json",
                "database_path": self.database,
                "scan_path": self.root / "scan.json",
                "config_path": self.config,
                "controller": controller,
            },
        )
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except PermissionError:
            self.skipTest("local sockets are disabled in this test environment")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_folder_endpoint_and_configuration_round_trip(self) -> None:
        with urllib.request.urlopen(
            f"{self.base_url}/api/folders?path={self.root}", timeout=2,
        ) as response:
            folders = json.load(response)
        self.assertIn("source", [item["name"] for item in folders["directories"]])

        request = urllib.request.Request(
            f"{self.base_url}/api/config",
            data=json.dumps({
                "source": str(self.source),
                "destination": str(self.root / "output"),
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            saved = json.load(response)
        self.assertTrue(saved["configured"])
        self.assertTrue(self.config.exists())
        self.assertEqual(str(self.source.resolve()), saved["source"])

    def test_configuration_rejects_nested_output(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/config",
            data=json.dumps({
                "source": str(self.source),
                "destination": str(self.source / "output"),
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(400, raised.exception.code)
        raised.exception.close()
        self.assertFalse(self.config.exists())

    def test_untrusted_origin_and_host_cannot_change_configuration(self) -> None:
        for headers in ({"Origin": "https://untrusted.example"},
                        {"Host": f"untrusted.example:{self.server.server_port}"}):
            with self.subTest(headers=headers):
                request = urllib.request.Request(
                    f"{self.base_url}/api/config",
                    data=json.dumps({"source": str(self.source),
                                     "destination": str(self.root / "output")}).encode(),
                    headers={"Content-Type": "application/json", **headers},
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(403, raised.exception.code)
                raised.exception.close()
                self.assertFalse(self.config.exists())

    def test_dashboard_preflight_and_requests_succeed(self) -> None:
        for origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
            with self.subTest(origin=origin):
                preflight = urllib.request.Request(
                    f"{self.base_url}/api/config", method="OPTIONS",
                    headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
                )
                with urllib.request.urlopen(preflight, timeout=2) as response:
                    self.assertEqual(204, response.status)
                    self.assertEqual(origin, response.headers["Access-Control-Allow-Origin"])
                    self.assertEqual(b"", response.read())
                health = urllib.request.Request(f"{self.base_url}/api/health", headers={"Origin": origin})
                with urllib.request.urlopen(health, timeout=2) as response:
                    self.assertEqual("ok", json.load(response)["status"])

    def test_conversion_rejects_stale_confirmation(self) -> None:
        from tests.test_review import analysis

        report = analysis()
        report["library_root"] = str(self.source)
        item = review_items(report)[0]
        with StateDatabase(self.database) as database:
            database.store_decision(relationship_id=item.relationship_id, decision="confirm",
                                    evidence_fingerprint=item.evidence_fingerprint,
                                    group_key=item.group_key, note=None)
            database.approve_metadata(item.relationship_id, {
                "title": "Fixture", "authors": ["Writer"],
                "provider": "fixture", "provider_id": "fixture-book",
            })
        report["groups"][0]["relationships"][0]["confidence"] = 1
        (self.root / "analysis.json").write_text(json.dumps(report))
        self.server.RequestHandlerClass.controller.configure(self.source, self.root / "output")
        request = urllib.request.Request(
            f"{self.base_url}/api/convert",
            data=json.dumps({"relationshipId": item.relationship_id}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with patch("audiobook_manager.webserver.convert_to_m4b") as convert:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(400, raised.exception.code)
            self.assertIn("current lineage", raised.exception.read().decode())
            raised.exception.close()
            convert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
