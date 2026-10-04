"""SpringHub public branding with legacy data and configuration compatibility."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from cutecat.config import load_config, environment
from cutecat.cli import _build_arg_parser
from cutecat import __version__


class BrandTests(unittest.TestCase):
    def test_public_and_legacy_entrypoints(self):
        for module in ("springhub", "cutecat"):
            proc = subprocess.run([sys.executable, "-m", module, "--version"], capture_output=True, text=True, check=True)
            self.assertEqual(proc.stdout.strip(), "springhub " + __version__)
        self.assertEqual(_build_arg_parser().prog, "springhub")

    def test_new_environment_prefix_precedes_legacy(self):
        with patch.dict(os.environ, {"SPRINGHUB_LISTEN":"127.0.0.1:18087","CUTE_CAT_LISTEN":"127.0.0.1:1","SPRINGHUB_ENGINE":"new-cli","CUTE_CAT_ENGINE":"old-cli","SPRINGHUB_DATABASE":"/tmp/same.db","SPRINGHUB_API_TOKEN":"new-token"}, clear=True):
            config = load_config()
            self.assertEqual(config.listen,"127.0.0.1:18087")
            self.assertEqual(config.engine.handbrake_bin,"new-cli")
            self.assertEqual(config.database,"/tmp/same.db")
            self.assertEqual(config.api_token,"new-token")
            self.assertNotIn("new-token",str(config.as_public_dict()))

    def test_legacy_environment_and_data_names_survive(self):
        with patch.dict(os.environ,{"CUTE_CAT_LISTEN":"127.0.0.1:18087","CUTE_CAT_ENGINE":"old-cli","CUTE_CAT_API_TOKEN":"old-token"},clear=True):
            config=load_config()
            self.assertEqual(config.engine.handbrake_bin,"old-cli")
            self.assertEqual(config.api_token,"old-token")
            self.assertEqual(config.database,"/data/cute-cat.db")

    def test_explicit_empty_new_value_does_not_inherit_legacy(self):
        with patch.dict(os.environ,{"SPRINGHUB_API_TOKEN":"","CUTE_CAT_API_TOKEN":"secret","SPRINGHUB_ENGINE":"","CUTE_CAT_ENGINE":"legacy"},clear=True):
            self.assertEqual(load_config().api_token,"")
            self.assertEqual(load_config().engine.handbrake_bin,"HandBrakeCLI")
            self.assertEqual(environment("ENGINE","default"),"default")

    def test_public_assets_and_preserved_protocol(self):
        root=Path(__file__).resolve().parents[1]
        html=(root/'web/index.html').read_text()
        css=(root/'web/styles.css').read_text()
        settings=(root/'web/settings.js').read_text()
        self.assertIn('SpringHub · HandBrake',html)
        self.assertIn('aria-label="SpringHub"',html)
        self.assertNotIn('🐱',html)
        self.assertIn('--bg: #000000',css)
        self.assertIn('--accent: #ff9900',css)
        self.assertNotIn('#4f9cf9',css)
        self.assertIn('cute-cat.preferences.v1',settings)
        self.assertIn('springhub-task-templates.json',settings)
        self.assertIn('cute-cat-task-templates',(root/'cutecat/templates.py').read_text())


if __name__ == "__main__":
    unittest.main()
