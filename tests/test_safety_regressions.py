from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audiobook_manager.configuration import save_configuration
from audiobook_manager.controller import ProcessController
from audiobook_manager.conversion import convert_to_m4b, copy_verified_m4b
from audiobook_manager.engine import discover_run
from audiobook_manager.probe import probe_media
from audiobook_manager.scanner import scan_library
from scripts.remove_empty_output_directories import main as cleanup_main
from scripts.remove_empty_output_directories import remove_empty_directories
from scripts.remote_import_plan import validate_plan
from scripts.verify_remote_to_local_import import verify


class SourceProtectionTests(unittest.TestCase):
    def test_processing_refuses_database_through_source_symlink_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            alias = root / "alias"
            alias.symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "state database must be outside"):
                discover_run(source, root / "output", alias / "state.sqlite3")
            self.assertEqual([], list(source.iterdir()))

    def test_config_failure_preserves_controller_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            controller = ProcessController(root / "state.sqlite3")
            with self.assertRaisesRegex(ValueError, "configuration must be outside"):
                controller.configure(source, root / "output", config_path=source / "config.json")
            self.assertIsNone(controller.source)
            self.assertIsNone(controller.destination)
            self.assertEqual([], list(source.iterdir()))
            with patch("audiobook_manager.controller.save_configuration", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    controller.configure(source, root / "output", config_path=root / "config.json")
            self.assertIsNone(controller.source)

    def test_config_and_controller_reject_source_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with self.assertRaises(ValueError):
                save_configuration(source / "config.json", source, root / "output")
            with self.assertRaises(ValueError):
                ProcessController(source / "state.sqlite3", source, root / "output")
            self.assertEqual([], list(source.iterdir()))

    def test_direct_conversion_and_copy_cannot_write_under_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            book = source / "book.m4b"
            book.write_bytes(b"original fixture")
            with self.assertRaisesRegex(ValueError, "outside the source"):
                convert_to_m4b(inputs=[book], library_root=source, output_root=source / "new",
                               metadata={"title": "Example"})
            with self.assertRaisesRegex(ValueError, "outside the source"):
                copy_verified_m4b(source=book, library_root=source, output_root=source,
                                  destination=source / "copy.m4b")
            # Ancestor output roots are used by archive staging, but must never
            # allow a final destination back inside the extracted input tree.
            with self.assertRaisesRegex(ValueError, "outside the source"):
                convert_to_m4b(inputs=[book], library_root=source, output_root=source.parent,
                               destination=source / "copy.m4b", metadata={"title": "Example"})
            self.assertEqual([book], list(source.iterdir()))
            self.assertEqual(b"original fixture", book.read_bytes())


class CleanupSafetyTests(unittest.TestCase):
    def test_cleanup_cli_is_read_only_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            empty = output / "Author" / "Empty"
            empty.mkdir(parents=True)
            audit = root / "audit.json"
            audit.write_text(json.dumps({"mode": "read-only", "output_root": str(output),
                                        "empty_directories": [{"path": str(empty)}]}))
            with patch("sys.argv", ["cleanup", "--audit", str(audit)]), \
                    contextlib.redirect_stdout(io.StringIO()) as response:
                self.assertEqual(0, cleanup_main())
            self.assertEqual("dry-run", json.loads(response.getvalue())["mode"])
            self.assertTrue(empty.is_dir())

    def test_only_audited_leaves_and_their_empty_parents_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "Author" / "Empty"
            unrelated = root / "Another Author" / "New Empty"
            empty.mkdir(parents=True)
            unrelated.mkdir(parents=True)
            result = remove_empty_directories(output_root=root, audited_paths=[empty])
            self.assertEqual(2, result["directories_removed"])
            self.assertTrue(unrelated.is_dir())

    def test_preflight_rejects_symlinks_and_internal_paths_before_any_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "Good"
            target = root / "Target"
            internal = root / "Author" / "_quarantine"
            for path in (good, target, internal):
                path.mkdir(parents=True)
            alias = root / "Alias"
            alias.symlink_to(target, target_is_directory=True)
            for unsafe in (alias, internal):
                with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                    remove_empty_directories(output_root=root, audited_paths=[good, unsafe])
                self.assertTrue(good.is_dir())
                self.assertTrue(target.is_dir())


class ProbeSafetyTests(unittest.TestCase):
    def test_malformed_chapter_data_is_isolated_to_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.m4b").touch()
            (root / "good.m4b").touch()

            def payload(path: Path, *_args: object) -> dict:
                chapters = [{"id": "invalid", "start_time": "0", "end_time": "10"}]
                return {"streams": [{"codec_type": "audio", "codec_name": "aac"}],
                        "format": {"duration": "10"},
                        "chapters": chapters if path.name == "bad.m4b" else []}

            with patch("audiobook_manager.probe._run_probe", side_effect=payload):
                files = scan_library(root)
            self.assertIsNotNone(files[0].error)
            self.assertEqual(10, files[1].probe.duration_seconds)

    def test_nonfinite_duration_cannot_pass_as_valid_audio(self) -> None:
        for duration in ("NaN", "inf", "-inf"):
            with self.subTest(duration=duration), patch(
                "audiobook_manager.probe._run_probe", return_value={"format": {"duration": duration}},
            ):
                self.assertIsNone(probe_media(Path("fixture.m4b")).duration_seconds)


class RemotePlanSafetyTests(unittest.TestCase):
    def test_old_plan_cannot_silently_verify_zero_books(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "regenerate"):
                verify({"mode": "dry_run_no_media_writes", "operations": []}, {}, Path(directory))

    def test_device_change_and_unknown_actions_require_new_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = {"schema_version": 2, "mode": "dry_run_no_media_writes",
                    "destination_root": str(root), "destination_device": root.stat().st_dev,
                    "summary": {"reserve_bytes": 100}, "operations": []}
            validate_plan(plan, root)
            with self.assertRaisesRegex(ValueError, "filesystem changed"):
                validate_plan({**plan, "destination_device": -1}, root)
            with self.assertRaisesRegex(ValueError, "unknown import action"):
                validate_plan({**plan, "operations": [{"action": "misspelled_copy"}]}, root)


if __name__ == "__main__":
    unittest.main()
