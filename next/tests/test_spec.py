"""Structured-parameter whitelist and CLI-mapping tests."""

from __future__ import annotations

import unittest

from cutecat.spec import SpecError, TranscodeSpec, build_engine_args


class SpecValidationTests(unittest.TestCase):
    def test_rejects_unknown_top_level_field(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"video": {}, "evil": "--output /etc/passwd"})

    def test_rejects_unknown_video_field(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"video": {"encoder": "x264", "hack": "1"}})

    def test_rejects_bad_encoder(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"video": {"encoder": "rm -rf /"}})

    def test_rejects_bad_quality_type(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"video": {"quality_type": "wat"}})

    def test_rejects_quality_out_of_range(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"video": {"quality": 999}})

    def test_rejects_bad_container(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"container": "exe"})

    def test_rejects_absolute_subtitle_file(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"subtitles": {"srt_file": "/etc/passwd"}})

    def test_rejects_platform_specific_auxiliary_paths(self):
        for path in ("C:\\subs.srt", "\\\\server\\share\\subs.srt", ".", "one.srt,two.srt"):
            with self.subTest(path=path), self.assertRaises(SpecError):
                TranscodeSpec.from_dict({"subtitles": {"srt_file": path}})

    def test_rejects_traversal_chapter_file(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"chapters": {"marker_file": "../../x"}})

    def test_rejects_bad_timecode(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"chapters": {"mode": "markers", "markers": [{"start": "nope"}]}})

    def test_rejects_bad_custom_filter(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"filters": {"deinterlace": "custom", "deinterlace_custom": "x; rm -rf /"}})

    def test_rejects_metadata_unknown_key(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"metadata": {"__proto__": "x"}})

    def test_vfr_cfr_mutually_exclusive(self):
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({"video": {"vfr": True, "cfr": True}})

    def test_rejects_invalid_rate_control_combinations(self):
        for video in ({"two_pass": True}, {"turbo": True}, {"quality_type": "abr"},
                      {"vfr": True, "peak_framerate": True}):
            with self.subTest(video=video), self.assertRaises(SpecError):
                TranscodeSpec.from_dict({"video": video})

    def test_rejects_unimplemented_metadata_and_markers(self):
        for payload in ({"metadata": {"title": "ignored before"}},
                        {"chapters": {"mode": "markers", "markers": [{"start": "00:00:00"}]}}):
            with self.subTest(payload=payload), self.assertRaises(SpecError):
                TranscodeSpec.from_dict(payload)

    def test_rejects_unimplemented_track_overrides(self):
        for payload in ({"audio": {"tracks": [{"language": "eng"}]}},
                        {"audio": {"tracks": [{"default_track": True}]}},
                        {"subtitles": {"tracks": [{"track": 1, "burn": True}]}},
                        {"dimensions": {"keep_aspect": False}}):
            with self.subTest(payload=payload), self.assertRaises(SpecError):
                TranscodeSpec.from_dict(payload)

    def test_accepts_minimal_spec(self):
        spec = TranscodeSpec.from_dict({})
        self.assertEqual(spec.video.encoder, "x264")


class ArgMappingTests(unittest.TestCase):
    def args(self, payload):
        return build_engine_args(TranscodeSpec.from_dict(payload))

    def test_encoder_and_quality(self):
        args = self.args({"video": {"encoder": "x265", "quality_type": "rf", "quality": 20}})
        self.assertIn("-e", args)
        self.assertEqual(args[args.index("-e") + 1], "x265")
        self.assertEqual(args[args.index("-q") + 1], "20")

    def test_preset_first(self):
        args = self.args({"preset": "Fast 1080p30"})
        self.assertEqual(args[0], "--preset")
        self.assertEqual(args[1], "Fast 1080p30")

    def test_two_pass_and_turbo(self):
        args = self.args({"video": {"quality_type": "abr", "bitrate_kbps": 1500, "two_pass": True, "turbo": True}})
        self.assertIn("--multi-pass", args)
        self.assertIn("--turbo", args)

    def test_dimensions(self):
        args = self.args({"dimensions": {"width": 1280, "height": 720, "crop_mode": "custom", "crop_top": 2, "crop_bottom": 4, "crop_left": 0, "crop_right": 0}})
        self.assertEqual(args[args.index("-w") + 1], "1280")
        self.assertEqual(args[args.index("-l") + 1], "720")
        self.assertIn("2:4:0:0", args)

    def test_filters(self):
        args = self.args({"filters": {"deinterlace": "slow", "denoise": "nlmeans", "rotate": "90", "grayscale": True}})
        self.assertIn("--deinterlace=mode=3", args)
        self.assertIn("--nlmeans=medium", args)
        self.assertIn("--rotate=angle=90:hflip=0", args)
        self.assertIn("--grayscale", args)

    def test_audio_tracks_joined(self):
        args = self.args({"audio": {"tracks": [
            {"track": 1, "encoder": "aac", "mixdown": "stereo", "bitrate": "160"},
            {"track": 2, "encoder": "ac3", "mixdown": "5point1", "bitrate": "448"},
        ]}})
        self.assertEqual(args[args.index("-E") + 1], "aac,ac3")
        self.assertEqual(args[args.index("-6") + 1], "stereo,5point1")
        self.assertEqual(args[args.index("-B") + 1], "160,448")

    def test_audio_encoders_exclude_cli_rejected_values(self):
        # Verified against HandBrakeCLI 1.11.0 with a real audio track present:
        #   -E auto  -> Invalid audio encoder (auto)
        #   -E dtshd -> Invalid audio encoder (dtshd)
        #   -E dts   -> accepted, but a passthrough request that yielded AAC
        for value in ("auto", "dts", "dtshd"):
            with self.assertRaises(SpecError, msg=value):
                TranscodeSpec.from_dict({"audio": {"tracks": [{"encoder": value}]}})
            with self.assertRaises(SpecError, msg=value):
                TranscodeSpec.from_dict({"audio": {"fallback_encoder": value}})

    def test_audio_encoders_still_accept_the_valid_set(self):
        for value in ("none", "copy", "aac", "ac3", "eac3", "truehd",
                      "flac", "mp3", "opus", "vorbis", "lpcm"):
            spec = TranscodeSpec.from_dict({"audio": {"tracks": [{"encoder": value}]}})
            self.assertEqual(spec.audio.tracks[0].encoder, value)

    def test_copy_mask_takes_codec_names_not_digits(self):
        # The CLI accepts "aac,ac3" and rejects "1,2,3"; the validator must agree.
        spec = TranscodeSpec.from_dict({"audio": {"copy_mask": "aac,ac3"}})
        self.assertEqual(spec.audio.copy_mask, "aac,ac3")
        for bad in ("1,2,3", "bogus", "av_aac", "aac,,ac3", "aac,", ",aac"):
            with self.assertRaises(SpecError, msg=bad):
                TranscodeSpec.from_dict({"audio": {"copy_mask": bad}})
        # An empty value is simply "not set", not an error.
        self.assertIsNone(TranscodeSpec.from_dict({"audio": {"copy_mask": ""}}).audio.copy_mask)

    def test_copy_mask_emitted_to_cli(self):
        args = self.args({"audio": {"copy_mask": "aac,ac3"}})
        self.assertEqual(args[args.index("--audio-copy-mask") + 1], "aac,ac3")

    def test_subtitles(self):
        args = self.args({"subtitles": {"behavior": "burn", "burn_track": 1, "srt_file": "subs/x.srt"}})
        self.assertEqual(args[args.index("--subtitle") + 1], "1")
        self.assertIn("--subtitle-burned=1", args)
        self.assertEqual(args[args.index("--srt-file") + 1], "subs/x.srt")

    def test_chapters_none(self):
        args = self.args({"chapters": {"mode": "none"}})
        self.assertIn("--no-markers", args)

    def test_chapter_csv_mapping(self):
        args = self.args({"chapters": {"mode": "markers", "marker_file": "chapters.csv"}})
        self.assertIn("--markers=chapters.csv", args)

    def test_audio_source_is_used(self):
        args = self.args({"audio": {"tracks": [{"source": "2"}]}})
        self.assertEqual(args[args.index("--audio") + 1], "2")

    def test_subtitle_tracks_and_foreign_scan(self):
        args = self.args({"subtitles": {"tracks": [{"track": 2}, {"track": 3}]}})
        self.assertEqual(args[args.index("--subtitle") + 1], "2,3")
        self.assertIn("scan", self.args({"subtitles": {"behavior": "foreign"}}))

    def test_container(self):
        args = self.args({"container": "mkv"})
        self.assertEqual(args[args.index("--format") + 1], "mkv")

    def test_no_arbitrary_flag_injection(self):
        # A crafted preset name must not be able to smuggle a second flag; the
        # name is passed as a single argv element and never re-split.
        args = self.args({"preset": "--output /etc/passwd"})
        self.assertEqual(args[0], "--preset")
        self.assertEqual(args[1], "--output /etc/passwd")
        # Exactly one value follows --preset, so it cannot inject a real -o.
        self.assertNotIn("-o", args)


if __name__ == "__main__":
    unittest.main()
