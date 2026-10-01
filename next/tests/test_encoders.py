"""Encoder catalog uses real CLI enumeration, not a list of assumed installs."""
import subprocess
import unittest
from unittest.mock import patch

from cutecat.config import EngineConfig
from cutecat.engine import EngineCapabilities, HandBrakeEngine
from cutecat.encoders import AUDIO_CATALOG_ONLY, encoder_catalog, cli_encoder
from cutecat.spec import VIDEO_ENCODERS, VIDEO_HW_ENCODERS, AUDIO_ENCODERS

HELP = """   -e, --encoder <string> Select video encoder:
                               svt_av1
                               x264
                               nvenc_h264
                               VP9_10bit
       --encoder-preset <string>
                           prose (all encoders except theora)
   -E, --aencoder <string> Select audio encoder(s):
                               av_aac
                               copy:dts
                               flac16
                               pcm16
                               copy
                           Defaults:
                               av_mp4 av_aac
       --audio-copy-mask <string>
[12:00:00] qsv: not available on this system
[12:00:00] nvenc: version 11.1 is available
"""


class EncoderTests(unittest.TestCase):
    def caps(self, **kwargs):
        values = dict(available=True, binary="HandBrakeCLI", version="1.11.0",
                      version_string="HandBrake 1.11.0", help_available=True,
                      presets_available=True, encoders=["x264", "svt_av1", "nvenc_h264"],
                      audio_encoders=["av_aac", "flac16", "pcm16", "copy"],
                      video_encoders_known=True, audio_encoders_known=True)
        values.update(kwargs)
        return EngineCapabilities(**values)

    def catalog(self, caps):
        # Mirrors server._encoder_catalog: explain the never-submittable audio
        # values, but keep them out of the spec whitelist.
        return encoder_catalog(
            caps, VIDEO_ENCODERS, VIDEO_HW_ENCODERS,
            AUDIO_ENCODERS | set(AUDIO_CATALOG_ONLY),
        )

    def test_real_sections_exclude_prose_and_diagnostics(self):
        self.assertEqual(HandBrakeEngine._parse_encoders(HELP), ["svt_av1", "x264", "nvenc_h264", "VP9_10bit"])
        self.assertEqual(HandBrakeEngine._parse_encoder_section(HELP, "--aencoder"), ["av_aac", "copy:dts", "flac16", "pcm16", "copy"])

    def test_wrapped_parenthesized_sections(self):
        help_text = "-e, --encoder <string> Set video encoder (x264,\n x265, nvenc_h264)\n-E, --aencoder <string> Set audio encoder (aac, copy)"
        self.assertEqual(HandBrakeEngine._parse_encoders(help_text), ["x264", "x265", "nvenc_h264"])
        self.assertEqual(HandBrakeEngine._parse_encoder_section(help_text, "--aencoder"), ["aac", "copy"])

    def test_prose_only_is_unknown_not_an_enum(self):
        self.assertEqual(HandBrakeEngine._parse_encoders("qsv: not available\nprose mentions x264 nvenc_h264"), [])

    def test_hardware_diagnostics_do_not_prove_encode(self):
        self.assertEqual(HandBrakeEngine._parse_hardware_status(HELP), {"qsv": "unavailable", "nvenc": "detected"})
        gpu = next(e for e in self.catalog(self.caps(hardware_status={"nvenc": "detected"}))["video"] if e["id"] == "nvenc_h264")
        self.assertEqual(gpu["status"], "unverified")
        self.assertTrue(gpu["installed"])

    def test_unavailable_hardware_is_not_selectable(self):
        gpu = next(e for e in self.catalog(self.caps(hardware_status={"nvenc": "unavailable"}))["video"] if e["id"] == "nvenc_h264")
        self.assertFalse(gpu["selectable"])
        self.assertTrue(gpu["installed"])
        self.assertEqual(gpu["status"], "no_hardware")
        self.assertTrue(gpu["hardware_missing"])

    def test_missing_hardware_and_missing_build_are_distinct(self):
        # No GPU at all -> the encoder needs hardware the machine lacks.
        absent = next(e for e in self.catalog(self.caps())["video"] if e["id"] == "nvenc_h265")
        self.assertEqual(absent["status"], "no_hardware")
        self.assertTrue(absent["hardware_missing"])
        self.assertFalse(absent["installed"])
        # Hardware present but the build omits the encoder -> installable.
        installable = next(
            e for e in self.catalog(self.caps(hardware_status={"nvenc": "detected"}))["video"]
            if e["id"] == "nvenc_h265"
        )
        self.assertEqual(installable["status"], "not_installed")
        self.assertFalse(installable["hardware_missing"])
        self.assertIn("未包含此编码器", installable["reason"])

    def test_software_encoder_absent_from_build_is_not_installed(self):
        cards = self.catalog(self.caps(encoders=["x264"], hardware_status={"nvenc": "detected"}))
        self.assertEqual(next(e for e in cards["video"] if e["id"] == "av1")["status"], "not_installed")
        self.assertEqual(next(e for e in cards["audio"] if e["id"] == "opus")["status"], "not_installed")

    def test_instantiation_probe_beats_the_help_list(self):
        # HandBrakeCLI --help under-reports the encoder table, so the probe is the
        # authority: a "works" verdict must beat "absent from --help", and a
        # "blocked" one must read as missing hardware rather than missing install.
        probe = {
            "x264": "works", "svt_av1": "works", "nvenc_h264": "works",
            "nvenc_h265": "blocked", "qsv_h264": "absent", "vce_h264": "absent",
            "vt_h264": "absent",
        }
        cards = {
            c["id"]: c for c in self.catalog(
                self.caps(video_encoder_probe=probe, hardware_status={"nvenc": "detected"}),
            )["video"]
        }
        # Present and encodable, although --help never listed it.
        self.assertEqual(cards["nvenc_h265"]["status"], "no_hardware")
        self.assertTrue(cards["nvenc_h265"]["installed"])
        self.assertTrue(cards["nvenc_h265"]["hardware_missing"])
        self.assertIsNone(cards["nvenc_h265"]["acquisition"])
        # Really absent from this build -> install a different build.
        self.assertEqual(cards["qsv_h264"]["status"], "not_installed")
        self.assertFalse(cards["qsv_h264"]["installed"])
        self.assertFalse(cards["qsv_h264"]["hardware_missing"])
        self.assertTrue(cards["qsv_h264"]["acquisition"])
        # Absent, but no build provides it on this platform.
        self.assertEqual(cards["vt_h264"]["status"], "unsupported")
        self.assertIsNone(cards["vt_h264"]["acquisition"])

    def test_probe_verdict_is_looked_up_through_the_alias(self):
        # The spec value is "av1"; the CLI calls it "svt_av1". The probe tests the
        # real name, so the lookup must follow the alias or av1 reads as absent.
        probe = {"x264": "works", "svt_av1": "works"}
        cards = {c["id"]: c for c in self.catalog(self.caps(video_encoder_probe=probe))["video"]}
        self.assertEqual(cards["av1"]["status"], "available")
        self.assertTrue(cards["av1"]["selectable"])
        self.assertIsNone(cards["av1"]["acquisition"])
        # x265 was neither probed nor listed by --help, so the weaker fallback
        # answers — and must say that it is the weaker one.
        self.assertEqual(cards["x265"]["status"], "not_installed")
        self.assertIn("实例化检测未覆盖", cards["x265"]["reason"])

    def test_gpu_probe_success_is_still_unverified_not_available(self):
        probe = {"x264": "works", "nvenc_h264": "works"}
        card = next(
            e for e in self.catalog(self.caps(video_encoder_probe=probe, hardware_status={"nvenc": "detected"}))["video"]
            if e["id"] == "nvenc_h264"
        )
        self.assertEqual(card["status"], "unverified")
        self.assertTrue(card["selectable"])

    def test_acquisition_is_guidance_only_never_an_install_capability(self):
        # Cute Cat must never gain the ability to install anything: the catalog may
        # only carry text and a copyable host command, and only for encoders whose
        # absence a different engine build would actually fix.
        cards = self.catalog(self.caps(encoders=["x264"], hardware_status={"nvenc": "detected"}))
        seen = 0
        for card in cards["video"] + cards["audio"]:
            acquisition = card["acquisition"]
            if acquisition is None:
                self.assertNotEqual(card["status"], "not_installed", card["id"])
                continue
            seen += 1
            self.assertEqual(card["status"], "not_installed", card["id"])
            self.assertFalse(card["selectable"], card["id"])
            self.assertTrue(acquisition["headline"], card["id"])
            self.assertIn("本程序不会执行安装", acquisition["note"], card["id"])
            self.assertTrue(acquisition["options"], card["id"])
            for option in acquisition["options"]:
                self.assertIsInstance(option["command"], str, card["id"])
                self.assertNotIn("\n", option["command"], card["id"])
        self.assertTrue(seen)

    def test_audio_auto_is_not_reported_as_installable(self):
        # "auto" is a convenience value, not an encoder that could be installed.
        auto = next(e for e in self.catalog(self.caps())["audio"] if e["id"] == "auto")
        self.assertEqual(auto["status"], "unsupported")
        self.assertFalse(auto["selectable"])

    def test_passthrough_only_audio_is_never_called_uninstalled(self):
        # HandBrake ships no DTS/DTS-HD encoder, so no build could add one.
        cards = {c["id"]: c for c in self.catalog(self.caps())["audio"]}
        for value in ("dts", "dtshd"):
            self.assertEqual(cards[value]["status"], "passthrough_only")
            self.assertNotEqual(cards[value]["status"], "not_installed")
            self.assertEqual(cards[value]["device"], "passthrough")
            self.assertIn("直通", cards[value]["reason"])
        # A real copy-through entry is still selectable.
        self.assertTrue(cards["copy"]["selectable"])

    def test_catalog_only_audio_values_are_never_submittable(self):
        # Verified against HandBrakeCLI 1.11.0 with a real audio track:
        #   -E auto   -> "Invalid audio encoder (auto)"
        #   -E dtshd  -> "Invalid audio encoder (dtshd)"
        #   -E dts    -> accepted, but a passthrough request that produced AAC
        # So none of them may reach the engine, even though the UI explains them.
        for value in sorted(AUDIO_CATALOG_ONLY):
            self.assertNotIn(value, AUDIO_ENCODERS, value)
            card = next(c for c in self.catalog(self.caps())["audio"] if c["id"] == value)
            self.assertFalse(card["selectable"], value)
            self.assertTrue(card["reason"], value)
            self.assertIn(card["status"], ("passthrough_only", "unsupported"), value)

    def test_catalog_only_stays_unselectable_even_if_engine_lists_it(self):
        # The CLI accepting "dts" as a passthrough request must not make it an
        # encoder the user can pick.
        caps = self.caps(audio_encoders=["av_aac", "copy", "dts", "auto"])
        for value in ("dts", "auto"):
            card = next(c for c in self.catalog(caps)["audio"] if c["id"] == value)
            self.assertFalse(card["selectable"], value)

    def test_audio_fallback_is_alias_resolved(self):
        # --audio-fallback takes the same names as -E: the CLI rejects the short
        # forms (flac/lpcm) but accepts flac16/pcm16.
        engine = HandBrakeEngine(EngineConfig(handbrake_bin="HandBrakeCLI"))
        with patch.object(engine, "binary_path", return_value="/bin/hb"), patch.object(engine, "probe", return_value=self.caps()):
            command = engine.build_encode_command(
                input_path="input", output_path="output",
                args=["-e", "x264", "-E", "aac", "--audio-fallback", "flac"],
            )
        self.assertEqual(command[command.index("--audio-fallback") + 1], "flac16")

    def test_cards_never_claim_installable_without_a_build_slot(self):
        # Every unusable card must explain itself as either hardware or build.
        for cards in self.catalog(self.caps()).values():
            for card in cards:
                if card["selectable"]:
                    continue
                self.assertIn(card["status"], ("no_hardware", "not_installed", "unsupported", "passthrough_only", "missing", "unknown"), card["id"])
                self.assertTrue(card["reason"], card["id"])
                self.assertEqual(card["status"] == "no_hardware", card["hardware_missing"], card["id"])

    def test_missing_engine_and_failed_enumeration_are_distinct(self):
        missing = self.catalog(self.caps(available=False))["video"][0]
        unknown = self.catalog(self.caps(video_encoders_known=False))["video"][0]
        self.assertFalse(missing["installed"])
        self.assertIsNone(unknown["installed"])
        self.assertFalse(unknown["selectable"])

    def test_aliases_only_when_real_name_reported(self):
        self.assertEqual(cli_encoder("video", "av1", ["svt_av1"]), "svt_av1")
        self.assertEqual(cli_encoder("audio", "aac", ["av_aac"]), "av_aac")
        self.assertEqual(cli_encoder("audio", "aac", ["aac", "av_aac"]), "aac")
        self.assertIsNone(cli_encoder("audio", "dts", ["copy:dts"]))

    def test_every_card_has_description_and_processor(self):
        for kind, cards in self.catalog(self.caps()).items():
            for card in cards:
                self.assertTrue(card["description"], card["id"])
                self.assertIn(card["device"], ("cpu", "gpu", "passthrough"))
                self.assertIn(card["installed"], (True, False, None))

    def test_encode_command_adapts_only_explicit_aliases(self):
        engine = HandBrakeEngine(EngineConfig(handbrake_bin="HandBrakeCLI"))
        with patch.object(engine, "binary_path", return_value="/bin/hb"), patch.object(engine, "probe", return_value=self.caps()):
            command = engine.build_encode_command(input_path="input", output_path="output", args=["-e", "av1", "-E", "aac,flac,lpcm,copy"])
        self.assertEqual(command[-4:], ["-e", "svt_av1", "-E", "av_aac,flac16,pcm16,copy"])

    def test_svt_av1_is_not_videotoolbox_hardware(self):
        engine = HandBrakeEngine(EngineConfig(handbrake_bin="HandBrakeCLI"))
        with patch.object(engine, "binary_path", return_value="/bin/hb"), patch("cutecat.engine._run", return_value=subprocess.CompletedProcess([], 0, HELP, "")):
            caps = engine.probe()
        self.assertEqual(caps.hardware_encoders, ["nvenc_h264"])

    def test_failed_help_cannot_report_installed(self):
        engine = HandBrakeEngine(EngineConfig(handbrake_bin="HandBrakeCLI"))
        with patch.object(engine, "binary_path", return_value="/bin/hb"), patch("cutecat.engine._run", return_value=subprocess.CompletedProcess([], 1, HELP, "failed")):
            caps = engine.probe()
        self.assertFalse(caps.video_encoders_known)
        self.assertFalse(caps.audio_encoders_known)
        self.assertEqual(caps.encoders, [])


if __name__ == "__main__":
    unittest.main()
