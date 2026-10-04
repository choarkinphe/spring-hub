"""Engine scan/progress parsing and capability tests (using the mock CLI)."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from cutecat.config import EngineConfig
from cutecat.engine import (
    MUX_VIDEO_TRACK_RE,
    EngineError,
    HandBrakeEngine,
    parse_progress_line,
    parse_scan_output,
)

from helpers import TempEnv


class ScanParsingTests(unittest.TestCase):
    def test_parses_single_document(self):
        doc = {"Version": "1.7.3", "TitleList": [{"Index": 1}, {"Index": 2}]}
        parsed = parse_scan_output(json.dumps(doc))
        self.assertEqual(parsed["title_count"], 2)
        self.assertEqual(parsed["engine_version"], "1.7.3")

    def test_parses_newline_delimited(self):
        lines = "\n".join([
            json.dumps({"TitleList": [{"Index": 1}]}),
            json.dumps({"MainFeature": 1}),
        ])
        parsed = parse_scan_output(lines)
        self.assertEqual(parsed["title_count"], 1)
        self.assertEqual(parsed["main_feature"], 1)

    def test_raises_on_garbage(self):
        with self.assertRaises(EngineError):
            parse_scan_output("not json at all")

    def test_tolerates_prefixed_log_lines(self):
        text = 'Opening file...\n{"TitleList":[{"Index":1}]}\nDone.\n'
        parsed = parse_scan_output(text)
        self.assertEqual(parsed["title_count"], 1)


class ProgressParsingTests(unittest.TestCase):
    def test_parses_progress(self):
        event = parse_progress_line('{"State":"WORKING","Progress":12.5,"Rate":40.0,"ETA":9}')
        self.assertEqual(event["state"], "WORKING")
        self.assertAlmostEqual(event["progress"], 12.5)
        self.assertEqual(event["eta_seconds"], 9)

    def test_parses_hms_eta(self):
        event = parse_progress_line('{"State":"WORKING","Progress":1,"Hours":0,"Minutes":2,"Seconds":3}')
        self.assertEqual(event["eta_seconds"], 123)

    def test_ignores_non_json(self):
        self.assertIsNone(parse_progress_line("scanning title 1 of 2"))
        self.assertIsNone(parse_progress_line('{"Unrelated":true}'))


# Representative HandBrake 1.11 output: diagnostics, folders, names and
# descriptions share stderr; stdout contains only a newline.
PRESET_TREE = """[14:57:40] hb_init: starting libhb thread
General/
    Very Fast 1080p30
        Small H.264 video (up to 1080p30) and AAC stereo audio, in
        an MP4 container.
    Fast 1080p30
        H.264 video (up to 1080p30) and AAC stereo audio, in an MP4
        container.
Hardware/
    H.265 NVENC 1080p
        Nvidia NVENC hardware accelerated H.265 video (up to 1080p)
        and AAC stereo audio, in an MP4 container.
HandBrake has exited.
"""


class PresetParsingTests(unittest.TestCase):
    def test_tree_excludes_logs_folders_and_descriptions(self):
        presets = HandBrakeEngine._parse_presets(PRESET_TREE)
        self.assertEqual([p["name"] for p in presets], [
            "General/Very Fast 1080p30", "General/Fast 1080p30",
            "Hardware/H.265 NVENC 1080p",
        ])
        self.assertEqual(presets[0]["category"], "General")
        self.assertEqual(presets[0]["description"],
                         "Small H.264 video (up to 1080p30) and AAC stereo audio, in an MP4 container.")

    def test_nested_folders_and_presets_without_descriptions(self):
        text = "Custom/\n    Nested/\n        First\n        Second\nOther/\n    Third\n"
        self.assertEqual([p["name"] for p in HandBrakeEngine._parse_presets(text)],
                         ["Custom/Nested/First", "Custom/Nested/Second", "Other/Third"])

    def test_flat_names_remain_supported(self):
        presets = HandBrakeEngine._parse_presets("General/Fast 1080p30\nWeb/Creator 1080p60\n")
        self.assertEqual(len(presets), 2)
        self.assertEqual(presets[0]["name"], "General/Fast 1080p30")

    def test_diagnostic_only_output_is_empty(self):
        self.assertEqual(HandBrakeEngine._parse_presets(
            "[14:57:40] qsv: not available on this system\nHandBrake has exited.\n"), [])


class EngineCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.engine = HandBrakeEngine(EngineConfig(handbrake_bin=self.env.mock, ffprobe_bin=""))

    def tearDown(self):
        self.env.cleanup()

    def test_missing_binary_reported_not_crash(self):
        engine = HandBrakeEngine(EngineConfig(handbrake_bin="/nonexistent/HandBrakeCLI"))
        caps = engine.probe()
        self.assertFalse(caps.available)
        self.assertTrue(caps.notes)

    def test_probe_reports_version_and_hardware(self):
        caps = self.engine.probe()
        self.assertTrue(caps.available)
        self.assertEqual(caps.version, "1.7.3")
        self.assertIn("x264", caps.encoders)
        # Hardware names come from the CLI, not from FFmpeg.
        self.assertIn("nvenc_h264", caps.hardware_encoders)

    def test_scan_via_mock(self):
        scan = self.engine.scan(str(self.env.media / "movie.mp4"))
        self.assertEqual(scan["title_count"], 2)

    def test_scan_missing_file_raises(self):
        with self.assertRaises(EngineError):
            self.engine.scan(str(self.env.media / "nope.mp4"))

    def test_list_presets(self):
        presets = self.engine.list_presets()
        self.assertTrue(any("1080p30" in p["name"] for p in presets))

    def test_real_tree_on_stderr(self):
        proc = subprocess.CompletedProcess([], 0, stdout="\n", stderr=PRESET_TREE)
        with patch("cutecat.engine._run", return_value=proc):
            self.assertEqual(len(self.engine.list_presets()), 3)
            self.assertTrue(self.engine.probe().presets_available)

    def test_real_tree_on_stdout_and_duplicate_streams(self):
        for stderr in ("", PRESET_TREE):
            with self.subTest(stderr=bool(stderr)):
                proc = subprocess.CompletedProcess([], 0, stdout=PRESET_TREE, stderr=stderr)
                with patch("cutecat.engine._run", return_value=proc):
                    self.assertEqual(len(self.engine.list_presets()), 3)

    def test_logs_do_not_report_presets_available(self):
        proc = subprocess.CompletedProcess([], 0, stdout="\n", stderr="[12:00:00] hb_init\n")
        with patch("cutecat.engine._run", return_value=proc):
            self.assertEqual(self.engine.list_presets(), [])
            self.assertFalse(self.engine.probe().presets_available)

    def test_failed_preset_command_is_not_available(self):
        proc = subprocess.CompletedProcess([], 1, stdout=PRESET_TREE, stderr="failed")
        with patch("cutecat.engine._run", return_value=proc):
            with self.assertRaisesRegex(EngineError, "failed"):
                self.engine.list_presets()
            caps = self.engine.probe()
            self.assertFalse(caps.presets_available)
            self.assertTrue(any("--preset-list failed" in note for note in caps.notes))

    def test_preset_timeout_is_engine_error(self):
        with patch("cutecat.engine._run", side_effect=subprocess.TimeoutExpired("HandBrakeCLI", 30)):
            with self.assertRaises(EngineError):
                self.engine.list_presets()


class JobEncoderParsingTests(unittest.TestCase):
    """The probe's presence oracle reads only the video track's encoder line."""

    LOG = (
        "[12:00:00] job configuration:\n"
        "[12:00:00]  * source\n"
        "[12:00:00]    + /tmp/in.avi\n"
        "[12:00:00]  * video track\n"
        "[12:00:00]    + encoder: H.265 (NVEnc)\n"
        "[12:00:00]  * audio track 1\n"
        "[12:00:00]    + encoder: AAC (libavcodec)\n"
        "[12:00:00] mux: track 0, 1 frames, 1 bytes, 1.00 kbps, /tmp/out.mkv\n"
    )

    def test_reads_the_video_track_encoder(self):
        self.assertEqual(HandBrakeEngine._parse_job_video_encoder(self.LOG), "H.265 (NVEnc)")

    def test_audio_only_block_is_not_a_presence_proof(self):
        # The audio track always names its own encoder; mistaking it for the
        # video one would mark every encoder as present.
        audio_only = self.LOG.replace("  * video track\n[12:00:00]    + encoder: H.265 (NVEnc)\n", "")
        self.assertIsNone(HandBrakeEngine._parse_job_video_encoder(audio_only))

    def test_log_without_a_job_configuration_is_absent(self):
        self.assertIsNone(HandBrakeEngine._parse_job_video_encoder("[12:00:00] ERROR: Unknown video codec (bogus)\n"))

    def test_mux_line_is_the_success_signal(self):
        self.assertTrue(MUX_VIDEO_TRACK_RE.search(self.LOG))
        self.assertIsNone(MUX_VIDEO_TRACK_RE.search(self.LOG.replace("mux: track 0", "mux: track 1")))

    def test_timestamp_prefix_is_optional(self):
        plain = "* video track\n  + encoder: H.264 (libx264)\n"
        self.assertEqual(HandBrakeEngine._parse_job_video_encoder(plain), "H.264 (libx264)")


class InstantiationProbeTests(unittest.TestCase):
    """End-to-end probe behaviour against the mock build."""

    def setUp(self):
        self.env = TempEnv()
        self.engine = HandBrakeEngine(EngineConfig(handbrake_bin=self.env.mock, ffprobe_bin=""))

    def tearDown(self):
        self.env.cleanup()

    def test_probe_finds_encoders_the_help_text_omits(self):
        caps = self.engine.probe()
        probe = caps.video_encoder_probe
        self.assertIsNotNone(probe, caps.probe_notes)
        # Present and encodable although --help never mentioned them.
        self.assertEqual(probe["nvenc_h265"], "blocked")
        self.assertEqual(probe["qsv_h264"], "blocked")
        self.assertEqual(probe["x264"], "works")
        # Not in the build at all.
        self.assertEqual(probe["vce_h264"], "absent")
        self.assertEqual(probe["vt_h264"], "absent")

    def test_probe_uses_the_cli_name_for_aliased_encoders(self):
        # The spec says "av1", the CLI says "svt_av1"; probing the spec's own
        # spelling would report a present encoder as absent.
        probe = self.engine.probe().video_encoder_probe
        self.assertEqual(probe["svt_av1"], "works")
        self.assertNotIn("av1", probe)

    def test_untrustworthy_probe_is_discarded_rather_than_guessed(self):
        # If the control encoder cannot be confirmed, every "absent" verdict is
        # suspect, so the whole result must be dropped instead of becoming
        # "not installed" in the catalog.
        with patch.object(self.engine, "_instantiate_encoder", return_value="absent"):
            caps = self.engine.probe()
        self.assertIsNone(caps.video_encoder_probe)
        self.assertTrue(any("结果不可信" in note for note in caps.probe_notes))

    def test_probe_is_cached_and_refresh_bypasses_it(self):
        with patch.object(self.engine, "_instantiate_encoder", wraps=self.engine._instantiate_encoder) as spy:
            first = self.engine.probe()
            calls = spy.call_count
            self.assertGreater(calls, 0)
            self.assertIs(self.engine.probe(), first)          # cache hit: same object
            self.assertEqual(spy.call_count, calls)            # ...and no new encodes
            self.engine.probe(refresh=True)
            self.assertGreater(spy.call_count, calls)          # refresh really re-probes

    def test_decoder_diagnostics_distinguish_compile_and_runtime(self):
        result = HandBrakeEngine._parse_decoder_backends(
            "[12:00:00] nvdec: is not compiled into this build\n"
            "qsv: not available on this system\nvideotoolbox: is available\n")
        self.assertEqual(result["nvdec"]["status"], "not_compiled")
        self.assertEqual(result["qsv"]["status"], "unavailable")
        self.assertEqual(result["videotoolbox"]["status"], "reported")
        self.assertEqual(HandBrakeEngine._parse_decoder_backends("--enable-hw-decoding nvdec"), {})

    def test_missing_binary_never_probes(self):
        engine = HandBrakeEngine(EngineConfig(handbrake_bin="/nonexistent/HandBrakeCLI"))
        with patch.object(engine, "_instantiate_encoder") as spy:
            caps = engine.probe()
        self.assertIsNone(caps.video_encoder_probe)
        self.assertEqual(spy.call_count, 0)


if __name__ == "__main__":
    unittest.main()
