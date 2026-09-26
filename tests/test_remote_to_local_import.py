from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from audiobook_manager.probe import probe_media
from scripts.apply_remote_to_local_import import (
    already_imported,
    copy_and_verify,
    publish,
    run,
    validated_paths,
)
from scripts.plan_remote_to_local_import import build_plan


class RemoteToLocalPlanTests(unittest.TestCase):
    def test_organized_path_and_existing_asin_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = "Books/Example [B000000001]/Example [B000000001].m4b"
            remote_plan = {"generated_at": "fixture", "remote_host": "fixture",
                           "remote_root": "/remote", "operations": [{
                               "action": "proposed_copy", "source_files": [relative],
                               "source_fingerprints": [{"path": relative, "size": 1000, "mtime_ns": 1}],
                               "source_bytes": 1000, "duration_seconds": 3600,
                               "asin": "B000000001", "metadata": {"title": "Example",
                               "authors": ["Example Writer"],
                               "series": {"name": "Example Series", "position": "1"}},
                               "destination": "/output/Example.m4b"}]}
            plan = build_plan(remote_plan, root, [], [], free_bytes=200 * 2**30)
            book = plan["operations"][0]
            self.assertEqual("proposed_copy_to_destination", book["action"])
            self.assertEqual(root / "Example Writer" / "Example Series" / "01 - Example" /
                             "Example Writer - Example [B000000001].m4b",
                             Path(book["destination_path"]))
            held = build_plan(remote_plan, root, ["elsewhere/Book [B000000001].m4b"], [],
                              free_bytes=200 * 2**30)
            self.assertEqual("review_existing_asin", held["operations"][0]["action"])

    def test_author_initial_spacing_uses_one_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operations = []
            for asin, author, title in (("B000000001", "A.B. Example", "First"),
                                        ("B000000002", "A. B. Example", "Second")):
                source = f"Books/{title} [{asin}]/{title} [{asin}].m4b"
                operations.append({"action": "proposed_copy", "source_files": [source],
                                   "source_fingerprints": [{"path": source, "size": 100, "mtime_ns": 1}],
                                   "source_bytes": 100, "duration_seconds": 3600,
                                   "asin": asin, "metadata": {"title": title, "authors": [author]},
                                   "destination": f"/output/{title}.m4b"})
            plan = build_plan({"generated_at": "fixture", "remote_host": "fixture",
                               "remote_root": "/remote", "operations": operations},
                              root, [], [], free_bytes=200 * 2**30)
            authors = {Path(item["destination_path"]).relative_to(root).parts[0]
                       for item in plan["operations"]}
            self.assertEqual({"A. B. Example"}, authors)

    def test_rejects_unsafe_source_and_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {"destination_root": str(root)}
            item = {"asin": "B000000001", "remote_source": "../elsewhere.m4b",
                    "destination_path": str(root / "book.m4b"),
                    "source_fingerprint": {"path": "../elsewhere.m4b", "size": 5, "mtime_ns": 1},
                    "source_bytes": 5}
            with self.assertRaises(ValueError):
                validated_paths(plan, item, root)
            item["remote_source"] = "book.m4b"
            item["source_fingerprint"]["path"] = "book.m4b"
            item["destination_path"] = str(root.parent / "outside.m4b")
            with self.assertRaises(ValueError):
                validated_paths(plan, item, root)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RemoteToLocalCopyTests(unittest.TestCase):
    def test_stream_checksum_probe_and_atomic_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "generated.m4b"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=2", "-c:a", "aac", str(source)], check=True)
            stat = source.stat()
            duration = probe_media(source).duration_seconds
            item = {"source_bytes": stat.st_size, "duration_seconds": duration,
                    "source_fingerprint": {"path": "generated.m4b", "size": stat.st_size,
                                           "mtime_ns": stat.st_mtime_ns}}
            plan = {"remote_root": "/remote", "remote_host": "fixture"}
            stage = root / "stage.m4b"
            code = (
                "import hashlib,json,sys; from pathlib import Path; "
                "data=Path(sys.argv[1]).read_bytes(); sys.stdout.buffer.write(data); "
                "print(json.dumps({'sha256':hashlib.sha256(data).hexdigest(),"
                "'size':len(data),'mtime_ns':int(sys.argv[2])}),file=sys.stderr)"
            )
            with patch("scripts.apply_remote_to_local_import.remote_command",
                       return_value=[sys.executable, "-c", code, str(source), str(stat.st_mtime_ns)]):
                digest = copy_and_verify(plan, item, stage, "generated.m4b",
                                         root / "socket", root / "hosts")
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            target = root / "published.m4b"
            publish(stage, target)
            self.assertFalse(stage.exists())
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertTrue(already_imported(target, item, {str(target): {
                "source_fingerprint": item["source_fingerprint"], "sha256": digest}}))
            second_stage = root / "second.m4b"
            second_stage.write_bytes(b"different")
            with self.assertRaises(FileExistsError):
                publish(second_stage, target)
            self.assertEqual(source.read_bytes(), target.read_bytes())

    def test_run_publishes_and_resumes_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "Audiobooks"
            root.mkdir()
            socket = base / "socket"
            hosts = base / "hosts"
            socket.touch()
            hosts.touch()
            stage_root = base / ".staging"
            ledger = base / "ledger.json"
            target = root / "Example" / "Example [B000000001].m4b"
            fingerprint = {"path": "Books/Example.m4b", "size": 6, "mtime_ns": 1}
            item = {"action": "proposed_copy_to_destination", "remote_source": fingerprint["path"],
                    "source_fingerprint": fingerprint, "source_bytes": 6,
                    "destination_path": str(target), "asin": "B000000001",
                    "metadata": {"title": "Example"}}
            plan = {"schema_version": 2, "mode": "dry_run_no_media_writes",
                    "destination_device": root.stat().st_dev,
                    "destination_root": str(root), "summary": {"reserve_bytes": 0},
                    "operations": [item]}
            digest = hashlib.sha256(b"sample").hexdigest()

            def fake_copy(_plan, _item, stage, _relative, _socket, _hosts):
                stage.write_bytes(b"sample")
                return digest

            with patch("scripts.apply_remote_to_local_import.shutil.disk_usage",
                       return_value=SimpleNamespace(free=200 * 2**30)), \
                 patch("scripts.apply_remote_to_local_import.copy_and_verify",
                       side_effect=fake_copy) as mocked:
                first = run(plan, root, stage_root, ledger, socket, hosts)
                second = run(plan, root, stage_root, ledger, socket, hosts)
            self.assertEqual({"copied": 1}, first)
            self.assertEqual({"already_imported": 1}, second)
            self.assertEqual(1, mocked.call_count)
            self.assertEqual(b"sample", target.read_bytes())
            self.assertEqual(digest, json.loads(ledger.read_text())[str(target)]["sha256"])


if __name__ == "__main__":
    unittest.main()
