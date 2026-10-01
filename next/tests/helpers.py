"""Shared test helpers (unittest style, runnable standalone)."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cutecat.config import AppConfig, EngineConfig, StorageRoot

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_CLI = REPO_ROOT / "tools" / "mock_handbrakecli.py"


def make_mock_engine(tmp: Path) -> str:
    """Return an executable path to the mock HandBrakeCLI."""

    target = tmp / "HandBrakeCLI"
    target.write_text(f"#!/usr/bin/env python3\nimport runpy, sys\n"
                      f"sys.argv[0] = {str(MOCK_CLI)!r}\n"
                      f"runpy.run_path({str(MOCK_CLI)!r}, run_name='__main__')\n",
                      encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(target)


class TempEnv:
    """Create an isolated media/output/engine environment on disk."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cutecat-test-"))
        self.media = self.tmp / "media"
        self.out = self.tmp / "out"
        self.media.mkdir(parents=True, exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.media / "movie.mp4").write_bytes(b"FAKE-INPUT\n")
        self.mock = make_mock_engine(self.tmp)

    def config(self, **engine_overrides) -> AppConfig:
        engine = EngineConfig(handbrake_bin=self.mock, ffprobe_bin="", **engine_overrides)
        return AppConfig(
            listen="127.0.0.1:0",
            database=str(self.tmp / "cutecat.db"),
            storage_roots=(
                StorageRoot(id="media", label="Media", path=str(self.media), read_only=False),
                StorageRoot(id="out", label="Out", path=str(self.out), read_only=False),
            ),
            engine=engine,
        )

    def cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)


__all__ = ["TempEnv", "make_mock_engine", "MOCK_CLI", "REPO_ROOT"]
