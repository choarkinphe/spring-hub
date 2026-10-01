"""HTTP API integration tests (real ThreadingHTTPServer, mock engine)."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from cutecat.engine import HandBrakeEngine
from cutecat.server import AppState, build_server
from cutecat.store import Store

from helpers import TempEnv


def _request(port, method, path, body=None, headers=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "null")
        finally:
            exc.close()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.config = self.env.config()
        self.store = Store(self.config.database)
        self.engine = HandBrakeEngine(self.config.engine)
        self.state = AppState(self.config, self.store, self.engine)
        self.server = build_server(self.state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.env.cleanup()

    def test_health_live(self):
        status, data = _request(self.port, "GET", "/health/live")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_capabilities(self):
        status, data = _request(self.port, "GET", "/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(data["engine"]["available"])
        self.assertIn("features", data)
        self.assertIn("spec_options", data)

    def test_encoders_endpoint(self):
        status, data = _request(self.port, "GET", "/api/v1/encoders")
        self.assertEqual(status, 200)
        self.assertTrue(data["engine"]["video_encoders_known"])
        self.assertEqual(len(data["encoder_catalog"]["video"]), 20)
        self.assertTrue(all(card["description"] for card in data["encoder_catalog"]["audio"]))

    def test_storage_roots(self):
        status, data = _request(self.port, "GET", "/api/v1/storage-roots")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["roots"]), 2)

    def test_list_entries(self):
        status, data = _request(self.port, "GET", "/api/v1/storage-roots/media/entries?path=")
        self.assertEqual(status, 200)
        self.assertTrue(any(e["name"] == "movie.mp4" for e in data["entries"]))

    def test_entries_rejects_traversal(self):
        status, data = _request(self.port, "GET", "/api/v1/storage-roots/media/entries?path=../")
        self.assertEqual(status, 400)

    def test_entries_rejects_unknown_root(self):
        status, _ = _request(self.port, "GET", "/api/v1/storage-roots/nope/entries?path=")
        self.assertEqual(status, 400)

    def test_probe(self):
        status, data = _request(self.port, "POST", "/api/v1/probe",
                                {"root": "media", "path": "movie.mp4"})
        self.assertEqual(status, 200)
        self.assertEqual(data["scan"]["title_count"], 2)

    def test_create_job_and_fetch(self):
        status, job = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "converted/out.mp4"},
            "preset_id": "custom",
            "spec": {"video": {"encoder": "x265", "quality": 20}},
        })
        self.assertEqual(status, 200)
        self.assertEqual(job["status"], "queued")
        status, fetched = _request(self.port, "GET", f"/api/v1/jobs/{job['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["id"], job["id"])
        self.assertIn("x265", fetched["args"])

    def test_create_job_rejects_bad_spec(self):
        status, _ = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "converted/x.mp4"},
            "spec": {"video": {"encoder": "bogus"}},
        })
        self.assertEqual(status, 400)

    def test_create_job_rejects_missing_input(self):
        status, _ = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "missing.mp4"},
            "output": {"root": "out", "path": "converted/x.mp4"},
            "spec": {},
        })
        self.assertEqual(status, 400)

    def test_create_job_rejects_absolute_output(self):
        status, _ = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "/etc/passwd"},
            "spec": {},
        })
        self.assertEqual(status, 400)

    def test_list_jobs_and_counts(self):
        _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "converted/out.mp4"},
            "spec": {},
        })
        status, data = _request(self.port, "GET", "/api/v1/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["jobs"]), 1)
        self.assertEqual(data["counts"].get("queued"), 1)

    def test_cancel_job(self):
        _, job = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "converted/out.mp4"},
            "spec": {},
        })
        status, _ = _request(self.port, "POST", f"/api/v1/jobs/{job['id']}/cancel", {})
        self.assertEqual(status, 200)
        _, fetched = _request(self.port, "GET", f"/api/v1/jobs/{job['id']}")
        self.assertEqual(fetched["status"], "canceled")

    def test_logs_endpoint(self):
        _, job = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "converted/out.mp4"},
            "spec": {},
        })
        status, data = _request(self.port, "GET", f"/api/v1/jobs/{job['id']}/logs")
        self.assertEqual(status, 200)
        self.assertTrue(data["logs"])

    def test_presets_endpoint(self):
        status, data = _request(self.port, "GET", "/api/v1/presets")
        self.assertEqual(status, 200)
        self.assertIn("official", data)
        self.assertTrue(data["official"])

    def test_import_preset(self):
        preset = {"PresetList": [{"PresetName": "Imported One"}]}
        (self.env.media / "presets").mkdir(parents=True, exist_ok=True)
        (self.env.media / "presets" / "p.json").write_text(json.dumps(preset), encoding="utf-8")
        status, data = _request(self.port, "POST", "/api/v1/presets/import",
                                {"root": "media", "path": "presets/p.json"})
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["imported"][0]["name"], "Imported One")

    def test_index_served(self):
        # The workbench page is HTML, not JSON.
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=10) as res:
            body = res.read().decode()
        self.assertEqual(res.status, 200)
        self.assertIn("HandBrake", body)
        self.assertIn("data-tab=\"summary\"", body)

    def test_unknown_api_route_404(self):
        status, _ = _request(self.port, "GET", "/api/v1/nope")
        self.assertEqual(status, 404)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        base = self.env.config()
        from dataclasses import replace

        self.config = replace(base, api_token="s3cret")
        self.store = Store(self.config.database)
        self.engine = HandBrakeEngine(self.config.engine)
        self.server = build_server(AppState(self.config, self.store, self.engine))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.env.cleanup()

    def test_requires_token(self):
        status, _ = _request(self.port, "GET", "/api/v1/capabilities")
        self.assertEqual(status, 401)

    def test_accepts_bearer(self):
        status, _ = _request(self.port, "GET", "/api/v1/capabilities",
                             headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(status, 200)

    def test_accepts_x_api_token(self):
        status, _ = _request(self.port, "GET", "/api/v1/capabilities",
                             headers={"X-API-Token": "s3cret"})
        self.assertEqual(status, 200)

    def test_rejects_wrong_token(self):
        status, _ = _request(self.port, "GET", "/api/v1/capabilities",
                             headers={"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
