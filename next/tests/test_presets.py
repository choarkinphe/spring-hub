"""Preset loading and preset-path-safety tests."""

from __future__ import annotations

import json
import unittest

from cutecat.config import EngineConfig
from cutecat.engine import HandBrakeEngine
from cutecat.presets import (
    PresetError,
    discover_preset_files,
    load_imported_presets,
    validate_preset_document,
)

from helpers import TempEnv


class PresetValidationTests(unittest.TestCase):
    def test_accepts_valid_document(self):
        doc = {"PresetList": [{"PresetName": "My Preset", "Category": "Custom"}]}
        presets = validate_preset_document(doc)
        self.assertEqual(presets[0].name, "My Preset")
        self.assertEqual(presets[0].source, "imported")

    def test_rejects_missing_preset_list(self):
        with self.assertRaises(PresetError):
            validate_preset_document({"Presets": []})

    def test_rejects_absolute_path_field(self):
        doc = {"PresetList": [{"PresetName": "X", "path": "/etc/passwd"}]}
        with self.assertRaises(PresetError):
            validate_preset_document(doc)

    def test_rejects_traversal_path_field(self):
        doc = {"PresetList": [{"PresetName": "X", "output_file": "../../escape.mp4"}]}
        with self.assertRaises(PresetError):
            validate_preset_document(doc)

    def test_allows_relative_path_field(self):
        doc = {"PresetList": [{"PresetName": "X", "path": "sub/ok.mp4"}]}
        presets = validate_preset_document(doc)
        self.assertEqual(len(presets), 1)

    def test_nested_absolute_path_rejected(self):
        doc = {"PresetList": [{"PresetName": "X", "Meta": {"file": "/etc/shadow"}}]}
        with self.assertRaises(PresetError):
            validate_preset_document(doc)

    def test_non_path_fields_ignored(self):
        doc = {"PresetList": [{"PresetName": "X", "Description": "/not/a/path/check"}]}
        presets = validate_preset_document(doc)
        self.assertEqual(len(presets), 1)


class PresetImportTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.engine = HandBrakeEngine(EngineConfig(handbrake_bin=self.env.mock, ffprobe_bin=""))
        self.config = self.env.config()

    def tearDown(self):
        self.env.cleanup()

    def _write(self, relative, payload):
        target = self.env.media / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def test_load_valid_preset_file(self):
        self._write("presets/custom.json", {"PresetList": [{"PresetName": "P1"}]})
        presets = load_imported_presets(self.config, self.engine, self.config.storage_roots[0], "presets/custom.json")
        self.assertEqual(presets[0].name, "P1")
        self.assertEqual(presets[0].file, "presets/custom.json")

    def test_reject_preset_outside_root(self):
        from cutecat.pathsafe import PathSafetyError

        with self.assertRaises(PathSafetyError):
            load_imported_presets(self.config, self.engine, self.config.storage_roots[0], "../outside.json")

    def test_reject_invalid_json(self):
        target = self.env.media / "bad.json"
        target.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PresetError):
            load_imported_presets(self.config, self.engine, self.config.storage_roots[0], "bad.json")

    def test_discover_lists_json(self):
        self._write("presets/a.json", {"PresetList": []})
        found = discover_preset_files(self.config.storage_roots)
        self.assertTrue(any(f["relative"].endswith("a.json") for f in found))


if __name__ == "__main__":
    unittest.main()
