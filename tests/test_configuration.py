from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from audiobook_manager.configuration import (
    AppConfiguration,
    browse_directories,
    load_configuration,
    save_configuration,
    validate_library_paths,
)
from audiobook_manager.controller import ProcessController


class ConfigurationTests(unittest.TestCase):
    def test_missing_configuration_starts_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            configuration = load_configuration(Path(directory) / "missing.json")
        self.assertEqual(AppConfiguration(), configuration)
        self.assertFalse(configuration.complete)

    def test_legacy_library_root_is_loaded_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            config = root / "config.json"
            config.write_text(json.dumps({
                "library_root": str(source),
                "output_root": str(output),
            }), encoding="utf-8")
            loaded = load_configuration(config)
        self.assertEqual(source.resolve(), loaded.source)
        self.assertEqual(output.resolve(), loaded.destination)

    def test_configuration_is_saved_with_portable_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            config_path = root / "config.json"
            saved = save_configuration(config_path, source, output)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertTrue(saved.complete)
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual(str(source.resolve()), payload["source_root"])
        self.assertEqual(str(output.resolve()), payload["output_root"])
        self.assertNotIn("library_root", payload)

    def test_source_and_output_cannot_be_nested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "separate, non-nested"):
                validate_library_paths(source, source / "output")

    def test_folder_browser_lists_visible_directories_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Audiobooks").mkdir()
            (root / ".private").mkdir()
            (root / "not-a-folder.txt").write_text("fixture", encoding="utf-8")
            listing = browse_directories(root)
        self.assertEqual(str(root.resolve()), listing["path"])
        self.assertEqual(["Audiobooks"], [item["name"] for item in listing["directories"]])

    def test_controller_can_be_configured_after_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            controller = ProcessController(root / "state.sqlite3")
            self.assertFalse(controller.status()["configured"])
            controller.configure(source, root / "output")
            status = controller.status()
        self.assertTrue(status["configured"])
        self.assertEqual(str(source.resolve()), status["source"])


if __name__ == "__main__":
    unittest.main()
