"""Unprivileged, pinned FFmpeg install: no automatic network or system changes."""
import hashlib
import io
import json
import tarfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from helpers import TempEnv
from test_api import _request
from cutecat.engine import HandBrakeEngine
from cutecat.ffmpeg_install import FFmpegInstall, RELEASE, managed_paths, allowed_download
from cutecat.server import AppState, build_server
from cutecat.store import Store


def archive(path, members=None):
    with tarfile.open(path, "w:xz") as bundle:
        for name, data, kind in members or [
            ("build/bin/ffmpeg", b"#!/bin/sh\necho 'ffmpeg version test'\n", tarfile.REGTYPE),
            ("build/bin/ffprobe", b"#!/bin/sh\necho 'ffprobe version test'\n", tarfile.REGTYPE),
            ("build/LICENSE", b"GPL-3.0", tarfile.REGTYPE),
        ]:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.size = len(data)
            bundle.addfile(item, io.BytesIO(data))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.config = replace(self.env.config(), engine=replace(self.env.config().engine, ffmpeg_bin="missing-ffmpeg-test", ffprobe_bin="missing-ffprobe-test"))
        self.store = Store(self.config.database)
        self.state = AppState(self.config, self.store, HandBrakeEngine(self.config.engine))
        self.installer = self.state.ffmpeg_install

    def tearDown(self):
        if self.installer._thread:
            self.installer._thread.join(5)
        self.store.close()
        self.env.cleanup()

    def test_status_never_executes_downloads_or_creates_directories(self):
        with patch("subprocess.run", side_effect=AssertionError("no execution")), patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no network")):
            report = self.installer.status()
        self.assertTrue(report["can_install"])
        self.assertFalse(Path(report["directory"]).exists())
        self.assertEqual(self.store.list_jobs(), [])

    def test_system_install_is_used_without_overwrite(self):
        engine = self.state.backends.get("ffmpeg")
        with patch.object(engine, "binary_path", return_value="/usr/bin/ffmpeg"), patch.object(engine, "ffprobe_path", return_value="/usr/bin/ffprobe"), patch.object(self.installer, "_download") as download:
            result = self.installer.start()
        self.assertTrue(result["installed"])
        self.assertFalse(result["can_install"])
        download.assert_not_called()

    def test_install_publish_and_restart_selection(self):
        fixture = self.env.tmp / "fixture.tar.xz"
        archive(fixture)
        old = self.state.backends.get("ffmpeg")
        def download(target, item):
            target.write_bytes(fixture.read_bytes())
        with patch.object(self.installer, "_download", side_effect=download):
            self.installer.start()
            self.installer._thread.join(5)
        self.assertEqual(self.installer.status()["status"], "succeeded", self.installer.status())
        paths = managed_paths(self.config.database)
        self.assertTrue(paths)
        self.assertIn("tools/ffmpeg", paths["ffmpeg_bin"])
        current = self.state.backends.get("ffmpeg")
        self.assertIsNot(current, old)
        self.assertEqual(old.config.ffmpeg_bin, "missing-ffmpeg-test")
        fresh = AppState(self.config, self.store, self.state.engine)
        self.assertEqual(fresh.backends.get("ffmpeg").binary_path(), paths["ffmpeg_bin"])
        self.assertFalse(any(p.name.startswith(".install-") for p in Path(self.installer.status()["directory"]).iterdir()))

    def test_system_paths_recover_without_mutating_managed_instance(self):
        fixture = self.env.tmp / "fixture.tar.xz"
        archive(fixture)
        with patch.object(self.installer, "_download", side_effect=lambda target, item: target.write_bytes(fixture.read_bytes())):
            self.installer.start()
            self.installer._thread.join(5)
        managed = self.state.backends.get("ffmpeg")
        with patch("cutecat.backends.shutil.which", return_value="/usr/bin/existing"):
            system = self.state.backends.get("ffmpeg")
        self.assertIsNot(system, managed)
        self.assertEqual(system.config.ffmpeg_bin, self.config.engine.ffmpeg_bin)
        self.assertIn("tools/ffmpeg", managed.config.ffmpeg_bin)

    def test_failure_cleans_staging_and_can_retry(self):
        with patch.object(self.installer, "_download", side_effect=ValueError("checksum mismatch")):
            self.installer.start()
            self.installer._thread.join(5)
        self.assertEqual(self.installer.status()["status"], "failed")
        self.assertIsNone(managed_paths(self.config.database))
        self.assertTrue(self.installer.status()["can_install"])
        self.assertFalse(any(p.name.startswith(".install-") for p in Path(self.installer.status()["directory"]).iterdir()))

    def test_duplicate_start_and_unsupported(self):
        self.installer._state["status"] = "downloading"
        with self.assertRaises(RuntimeError):
            self.installer.start()
        self.installer._state["status"] = "idle"
        with patch("cutecat.ffmpeg_install.platform.system", return_value="Windows"):
            self.assertFalse(self.installer.status()["supported"])
            with self.assertRaises(ValueError):
                self.installer.start()

    def test_extract_rejects_traversal_links_duplicates_missing_and_bombs(self):
        for members in [
            [("../outside", b"a", tarfile.REGTYPE)],
            [("/outside", b"a", tarfile.REGTYPE)],
            [("build/bin/ffmpeg", b"", tarfile.SYMTYPE)],
            [("build/bin/ffmpeg", b"a", tarfile.REGTYPE)] * 2,
            [("build/only.txt", b"a", tarfile.REGTYPE)],
        ]:
            fixture = self.env.tmp / "unsafe.tar.xz"
            archive(fixture, members)
            directory = self.env.tmp / f"extract-{len(list(self.env.tmp.iterdir()))}"
            directory.mkdir()
            with self.assertRaises(ValueError):
                self.installer._extract(fixture, directory)
        fixture = self.env.tmp / "big.tar.xz"
        archive(fixture)
        with patch("cutecat.ffmpeg_install.MAX_EXTRACTED", 1), self.assertRaises(ValueError):
            self.installer._extract(fixture, self.env.tmp)

    def test_hash_and_redirect_boundary_before_execution(self):
        self.assertTrue(allowed_download("https://release-assets.githubusercontent.com/a"))
        for url in ("http://github.com/a", "https://example.com/a", "https://github.com.evil/a", "https://user:pass@github.com/a", "https://github.com:8080/a"):
            self.assertFalse(allowed_download(url))
        payload = b"archive"
        class Response(io.BytesIO):
            def geturl(self): return "https://github.com/a"
        for size, digest, valid in [(len(payload), hashlib.sha256(payload).hexdigest(), True), (len(payload), "0" * 64, False), (1, "0" * 64, False)]:
            target = self.env.tmp / f"download-{digest}-{size}"
            with patch("urllib.request.OpenerDirector.open", return_value=Response(payload)):
                if valid:
                    self.installer._download(target, ("bundle.tar.xz", size, digest))
                else:
                    with self.assertRaises(ValueError):
                        self.installer._download(target, ("bundle.tar.xz", size, digest))

    def test_download_resumes_a_truncated_response(self):
        payload = b"complete archive"
        class Response(io.BytesIO):
            def __init__(self, data, status=200, headers=None):
                super().__init__(data)
                self.status, self.headers = status, headers or {}
            def geturl(self): return "https://github.com/a"
        responses = [Response(payload[:4]), Response(payload[4:], 206, {"Content-Range": f"bytes 4-{len(payload)-1}/{len(payload)}"})]
        seen = []
        def open_request(request, **kwargs):
            seen.append(request.get_header("Range"))
            return responses.pop(0)
        with patch("urllib.request.OpenerDirector.open", side_effect=open_request):
            target = self.env.tmp / "resumed"
            self.installer._download(target, ("bundle.tar.xz", len(payload), hashlib.sha256(payload).hexdigest()))
        self.assertEqual(seen, [None, "bytes=4-"])
        self.assertEqual(target.read_bytes(), payload)

    def test_symlink_install_directory_rejected(self):
        root = Path(self.installer.status()["directory"])
        root.parent.mkdir()
        root.symlink_to(self.env.out, target_is_directory=True)
        self.installer.start()
        self.installer._thread.join(5)
        self.assertEqual(self.installer.status()["status"], "failed")
        self.assertEqual(list(self.env.out.iterdir()), [])

    def test_api_confirmation_auth_and_no_paths_accepted(self):
        server = build_server(self.state)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            port = server.server_address[1]
            route = "/api/v1/ffmpeg/install"
            self.assertEqual(_request(port, "GET", route)[0], 200)
            for body in ({}, {"confirm": 1}, {"confirm": True, "url": "http://evil"}, {"confirm": True, "path": "/tmp"}):
                # bool/int equality must not accept a numeric confirmation.
                self.assertEqual(_request(port, "POST", route, body)[0], 400, body)
            with patch.object(self.installer, "start", return_value={"status": "starting"}) as start:
                self.assertEqual(_request(port, "POST", route, {"confirm": True}, {"Origin": "https://evil.example"})[0], 403)
                start.assert_not_called()
                self.assertEqual(_request(port, "POST", route, {"confirm": True})[0], 200)
            self.state.config = replace(self.config, api_token="secret")
            self.assertEqual(_request(port, "POST", route, {"confirm": True})[0], 401)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
