"""Path-safety unit tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from cutecat.config import StorageRoot
from cutecat.pathsafe import (
    PathSafetyError,
    check_preset_path_field,
    normalise_relative,
    resolve_in_root,
)


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cc-path-"))
        self.root = StorageRoot(id="r", label="R", path=str(self.tmp))
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "file.mp4").write_text("x")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_accepts_simple_relative(self):
        resolved = resolve_in_root(self.root, "sub/file.mp4", require_exists=True)
        self.assertTrue(resolved.is_file)
        self.assertEqual(resolved.relative, "sub/file.mp4")

    def test_accepts_empty_as_root(self):
        resolved = resolve_in_root(self.root, "")
        self.assertEqual(resolved.absolute, Path(os.path.realpath(self.tmp)))

    def test_rejects_parent_traversal(self):
        with self.assertRaises(PathSafetyError):
            resolve_in_root(self.root, "../etc/passwd")

    def test_rejects_absolute(self):
        with self.assertRaises(PathSafetyError):
            resolve_in_root(self.root, "/etc/passwd")

    def test_rejects_windows_drive(self):
        with self.assertRaises(PathSafetyError):
            resolve_in_root(self.root, "C:/Windows/system32")

    def test_rejects_backslash_traversal(self):
        with self.assertRaises(PathSafetyError):
            resolve_in_root(self.root, "..\\..\\secret")

    def test_rejects_nul_byte(self):
        with self.assertRaises(PathSafetyError):
            resolve_in_root(self.root, "sub/\x00evil")

    def test_rejects_symlink_escape(self):
        outside = Path(tempfile.mkdtemp(prefix="cc-outside-"))
        try:
            (outside / "secret.txt").write_text("top secret")
            link = self.tmp / "escape"
            os.symlink(outside, link)
            with self.assertRaises(PathSafetyError):
                resolve_in_root(self.root, "escape/secret.txt", require_exists=True)
        finally:
            import shutil

            shutil.rmtree(outside, ignore_errors=True)

    def test_normalise_relative_collapses(self):
        self.assertEqual(normalise_relative("./a//b/"), "a/b")

    def test_require_exists_raises(self):
        with self.assertRaises(PathSafetyError):
            resolve_in_root(self.root, "nope.mp4", require_exists=True)

    def test_preset_path_field_flags_absolute(self):
        self.assertIsNotNone(check_preset_path_field("path", "/etc/passwd"))
        self.assertIsNotNone(check_preset_path_field("output", "../x"))
        self.assertIsNone(check_preset_path_field("path", "sub/file"))
        self.assertIsNone(check_preset_path_field("encoder", "/anything"))


if __name__ == "__main__":
    unittest.main()
