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

    def test_runtime_settings_templates_and_manual_job_controls(self):
        status, settings = _request(self.port, "POST", "/api/v1/settings", {"auto_start": False, "max_concurrent_jobs": 2})
        self.assertEqual(status, 200, settings)
        self.assertEqual(settings["values"]["max_concurrent_jobs"], 2)
        status, template = _request(self.port, "POST", "/api/v1/task-templates", {
            "name": "Daily", "spec": {"video": {"encoder": "x264"}}, "form_spec": {}, "preset_id": "custom"})
        self.assertEqual(status, 200, template)
        status, settings = _request(self.port, "POST", "/api/v1/settings", {"default_task_template_id": template["id"]})
        self.assertEqual(status, 200)
        status, name = _request(self.port, "POST", "/api/v1/output-name", {"source": "a/clip.avi", "encoder": "x264", "container": "mkv"})
        self.assertEqual(name["path"], "converted/clip.mkv")
        status, job = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"}, "output": {"root": "out", "path": "out.mp4"}, "spec": {}})
        self.assertEqual(job["status"], "waiting")
        for action, expected in (("pause", "paused"), ("start", "queued")):
            status, result = _request(self.port, "POST", f"/api/v1/jobs/{job['id']}/{action}", {})
            self.assertEqual(status, 200, result)
            self.assertEqual(result["status"], expected)
        status, result = _request(self.port, "DELETE", f"/api/v1/jobs/{job['id']}")
        self.assertEqual(status, 200, result)
        self.assertFalse(result["media_deleted"])
        status, result = _request(self.port, "DELETE", f"/api/v1/task-templates/{template['id']}")
        self.assertEqual(status, 200)
        self.assertIsNone(self.state.runtime.read()["default_task_template_id"])

    def test_runtime_rejects_invalid_configuration_and_templates(self):
        for payload in ({"max_concurrent_jobs": 9}, {"output_name_template": "../{source}.mp4"}, {"handbrake_bin": "evil"}):
            status, body = _request(self.port, "POST", "/api/v1/settings", payload)
            self.assertEqual(status, 400, body)
        status, body = _request(self.port, "POST", "/api/v1/task-templates", {"name": "bad", "spec": {"video": {"encoder": "bad"}}, "form_spec": {}})
        self.assertEqual(status, 400)
        self.assertEqual(self.store.list_jobs(), [])

    def test_system_status_readonly_no_engine_probe(self):
        from unittest.mock import patch
        with patch.object(self.engine, "probe", side_effect=AssertionError("must not encode")):
            status, data = _request(self.port, "GET", "/api/v1/system/status")
        self.assertEqual(status, 200)
        self.assertIn("cpu", data)
        self.assertIn("gpu", data)
        self.assertIn("sampled_at", data)
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.store._conn.execute("SELECT count(*) FROM job_logs").fetchone()[0], 0)

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

    def test_same_input_output_rejected_even_with_overwrite(self):
        from dataclasses import replace
        self.state.config = replace(self.config, engine=replace(self.config.engine, refuse_overwrite=False))
        status, data = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "media", "path": "movie.mp4"},
        })
        self.assertEqual(status, 400)
        self.assertIn("must be different", data["error"])

    def test_mount_marker_missing_rejects_job(self):
        from dataclasses import replace
        self.state.config = replace(self.config, storage_roots=(
            replace(self.config.storage_roots[0], mount_marker=".mounted"), self.config.storage_roots[1]))
        status, _ = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "x.mp4"},
        })
        self.assertEqual(status, 400)

    def test_auxiliary_symlink_escape_rejected(self):
        (self.env.media / "escape.srt").symlink_to(self.env.out / "secret.srt")
        (self.env.out / "secret.srt").write_text("secret")
        status, _ = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "x.mp4"},
            "spec": {"subtitles": {"srt_file": "escape.srt"}},
        })
        self.assertEqual(status, 400)

    def test_spec_validation_endpoint(self):
        status, data = _request(self.port, "POST", "/api/v1/spec/validate", {"video": {"encoder": "x265"}})
        self.assertEqual(status, 200)
        self.assertTrue(data["valid"])
        status, _ = _request(self.port, "POST", "/api/v1/spec/validate", {"metadata": {"title": "ignored"}})
        self.assertEqual(status, 400)

    def test_wrapped_validation_matches_preset_job_without_side_effects(self):
        raw = {"preset": "General/Fast 1080p30", "filters": {"rotate": "90", "hflip": False}}
        status, validated = _request(self.port, "POST", "/api/v1/spec/validate", {
            "spec": raw, "preset_id": "General/Fast 1080p30",
        })
        self.assertEqual(status, 200, validated)
        self.assertEqual(validated["args_scope"], "overrides")
        self.assertNotIn("-e", validated["args"])
        self.assertIn("encoder_readiness", validated["not_checked"])
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.store._conn.execute("SELECT count(*) FROM job_logs").fetchone()[0], 0)
        self.assertFalse((self.env.out / "validation").exists())
        status, job = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "validation/out.mp4"},
            "preset_id": "General/Fast 1080p30", "spec": raw,
        })
        self.assertEqual(status, 200, job)
        self.assertEqual(job["args"], validated["args"])

    def test_validation_resolves_imported_uuid_and_checks_reset(self):
        ids = []
        for encoder, filename in (("x264", "one.json"), ("x265", "two.json")):
            ids.append(self.store.upsert_preset(name="Same", source="imported", root="media", file=filename,
                doc={"PresetList": [{"PresetName": "Same", "VideoEncoder": encoder}]}))
        for identity in ids:
            status, body = _request(self.port, "POST", "/api/v1/spec/validate", {
                "preset_id": identity, "spec": {"preset": "Same"},
            })
            self.assertEqual(status, 200, body)
            self.assertEqual(body["preset_id"], identity)
            self.assertEqual(body["args"], ["--preset", "Same", "--title", "1"])
        for payload in ({"preset_id": "missing", "spec": {}},
                        {"preset_id": ids[0], "spec": {"preset": "Wrong"}},
                        {"preset_id": ids[0], "spec": {"video": {"preset": None}}},
                        {"spec": {}, "input": {"path": "forbidden"}}, {"spec": []}):
            status, body = _request(self.port, "POST", "/api/v1/spec/validate", payload)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.store.list_jobs(), [])

    def test_validation_scope_no_encoder_admission_or_file_existence(self):
        status, body = _request(self.port, "POST", "/api/v1/spec/validate", {
            "spec": {"video": {"encoder": "nvenc_h265"}, "subtitles": {"srt_file": "missing.srt"}},
            "preset_id": "custom",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["args_scope"], "custom")
        self.assertEqual(self.store.list_jobs(), [])

    def test_validation_custom_without_engine_and_preset_failure(self):
        from unittest.mock import patch
        from cutecat.engine import EngineError
        with patch.object(self.engine, "export_preset", side_effect=EngineError("engine unavailable")):
            status, body = _request(self.port, "POST", "/api/v1/spec/validate", {})
            self.assertEqual(status, 200, body)
            status, body = _request(self.port, "POST", "/api/v1/spec/validate", {"preset": "General/Fast 1080p30"})
            self.assertEqual(status, 503, body)

    def test_ui_constraints_do_not_offer_rejected_modes(self):
        status, body = _request(self.port, "GET", "/api/v1/capabilities")
        self.assertEqual(status, 200)
        opts = body["spec_options"]
        self.assertNotIn("cqp", opts["video_quality_types"])
        self.assertNotIn("add", opts["subtitle_behaviors"])
        self.assertNotIn("strict", opts["ui_constraints"]["anamorphic"])
        self.assertEqual(opts["ui_constraints"]["bitrate_quality_types"], ["abr", "vbr"])

    def test_cli_mapping_rejection_does_not_enqueue(self):
        for payload in ({"video": {"quality_type": "cqp"}},
                        {"dimensions": {"anamorphic": "strict"}},
                        {"filters": {"deinterlace": "custom", "deinterlace_custom": "mode=3:parity=-1"}},
                        {"subtitles": {"srt_burn": True}}):
            status, data = _request(self.port, "POST", "/api/v1/jobs", {
                "input": {"root": "media", "path": "movie.mp4"},
                "output": {"root": "out", "path": "x.mp4"}, "spec": payload,
            })
            self.assertEqual(status, 400, data)
            self.assertEqual(self.store.list_jobs(), [])

    def test_preset_mapping_error_returns_400_not_500(self):
        status, data = _request(self.port, "POST", "/api/v1/jobs", {
            "input": {"root": "media", "path": "movie.mp4"},
            "output": {"root": "out", "path": "x.mp4"},
            "spec": {"preset": "Fast 1080p30", "video": {"preset": None}},
        })
        self.assertEqual(status, 400, data)
        self.assertEqual(self.store.list_jobs(), [])

    def test_cli_validation_exposes_correct_units_and_flags(self):
        status, data = _request(self.port, "POST", "/api/v1/spec/validate", {
            "audio": {"tracks": [{"samplerate": "48000", "name": "Main"}]},
            "filters": {"rotate": "90", "hflip": True},
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["args"][data["args"].index("--arate") + 1], "48")
        self.assertIn("--rotate=angle=90:hflip=1", data["args"])
        self.assertIn("--aname", data["args"])

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
        self.assertIn('id="system-info"', body)
        home = body.split('<dialog id="system-drawer"')[0]
        self.assertIn('id="system-summary"', home)
        self.assertNotIn('id="resource-info"', home)
        self.assertNotIn('id="codec-status"', home)
        self.assertNotIn('id="storage-status"', home)
        self.assertIn('id="job-list"', body)
        self.assertIn('<dialog id="create-task-dialog"', body)
        self.assertIn('<dialog id="file-picker-dialog"', body)
        self.assertNotIn('data-workspace=', body)
        self.assertNotIn('data-page="queue" hidden', body)

    def test_page_assets_use_relative_paths_and_are_served_with_correct_types(self):
        import re

        base = f"http://127.0.0.1:{self.port}"
        with urllib.request.urlopen(base + "/", timeout=10) as res:
            page = res.read().decode()
        references = re.findall(r'(?:href|src)="(\./(?:styles\.css|app\.js))"', page)
        self.assertEqual(set(references), {"./styles.css", "./app.js"})
        for reference in references:
            name = reference.removeprefix("./")
            expected = (self.state.web_dir / name).read_bytes()
            self.assertTrue(expected)
            # File previews resolve beside index.html; HTTP serves those same files.
            page_path = self.state.web_dir / "index.html"
            self.assertEqual((page_path.parent / reference).read_bytes(), expected)
            for route in ("/" + name, "/assets/" + name):
                with self.subTest(route=route):
                    with urllib.request.urlopen(base + route, timeout=10) as res:
                        self.assertEqual(res.status, 200)
                        content_type = res.headers.get_content_type()
                        if name.endswith(".css"):
                            self.assertEqual(content_type, "text/css")
                        else:
                            self.assertIn(content_type, ("text/javascript", "application/javascript"))
                        self.assertEqual(res.read(), expected)

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
        for path in ("/api/v1/capabilities", "/api/v1/system/status"):
            status, _ = _request(self.port, "GET", path)
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
