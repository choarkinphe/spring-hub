"""Regression tests for the HandBrakeCLI 1.11 parameter contract."""

import unittest

from cutecat.spec import SpecError, TranscodeSpec, build_engine_args, prepare_job_preset


class CliMappingTests(unittest.TestCase):
    def args(self, raw, preset=None):
        return build_engine_args(TranscodeSpec.from_dict(raw), overrides=raw if preset else None, preset=preset)

    def test_anamorphic_switches(self):
        for mode, flag in [('auto', '--auto-anamorphic'), ('none', '--non-anamorphic'), ('loose', '--loose-anamorphic')]:
            self.assertIn(flag, self.args({'dimensions': {'anamorphic': mode}}))
        for mode in ('strict', 'custom'):
            with self.assertRaises(SpecError):
                self.args({'dimensions': {'anamorphic': mode}})

    def test_crop_mode_is_explicit(self):
        for mode in ('none', 'auto', 'custom'):
            raw = {'dimensions': {'crop_mode': mode}}
            if mode == 'custom':
                raw['dimensions']['crop_top'] = 4
            args = self.args(raw)
            self.assertEqual(args[args.index('--crop-mode') + 1], mode)
        with self.assertRaises(SpecError):
            self.args({'dimensions': {'crop_top': 4}})

    def test_rotate_and_flip_are_one_optional_argument(self):
        for angle in ('off', '90', '180', '270'):
            for flip in (False, True):
                args = self.args({'filters': {'rotate': angle, 'hflip': flip}})
                self.assertEqual([a for a in args if a.startswith('--rotate=')],
                                 [f'--rotate=angle={0 if angle == "off" else angle}:hflip={int(flip)}'])
                self.assertNotIn('--hflip', args)

    def test_partial_rotation_preserves_other_preset_component(self):
        preset = {'PresetList': [{'PictureRotate': 'angle=270:hflip=1'}]}
        self.assertIn('--rotate=angle=0:hflip=1', self.args({'filters': {'rotate': 'off'}}, preset))
        self.assertIn('--rotate=angle=270:hflip=0', self.args({'filters': {'hflip': False}}, preset))
        self.assertIn('--rotate=angle=90:hflip=0', self.args({'filters': {'rotate': '90', 'hflip': False}}, preset))
        self.assertFalse(any(a.startswith('--rotate') for a in self.args({}, preset)))

    def test_filter_algorithms_and_legacy_aliases(self):
        for mode, setting in [('fast', 'skip-spatial'), ('slow', 'mode=3'), ('slower', 'mode=3'), ('bob', 'bob')]:
            args = self.args({'filters': {'deinterlace': mode}})
            self.assertIn('--deinterlace=' + setting, args)
            self.assertIn('--no-decomb', args)
        for mode in ('nlmeans', 'hqdn3d'):
            self.assertIn('--' + mode + '=medium', self.args({'filters': {'denoise': mode}}))
        self.assertIn('--detelecine=default', self.args({'filters': {'detelecine': 'default'}}))
        self.assertIn('--deblock=strength=strong:thresh=4', self.args({'filters': {'deblock': 4}}))

    def test_custom_filters_require_algorithm_specific_keys(self):
        valid = {'deinterlace': 'mode=3:parity=0', 'denoise': 'y-spatial=3:cb-temporal=2',
                 'detelecine': 'skip-left=1:plane=0'}
        for key, value in valid.items():
            self.args({'filters': {key: 'custom', key + '_custom': value}})
        for filters in ({'denoise': 'custom'}, {'denoise': 'custom', 'denoise_custom': 'nlmeans'},
                        {'deinterlace': 'custom', 'deinterlace_custom': 'bogus=1'},
                        {'deinterlace': 'custom', 'deinterlace_custom': 'mode=1:mode=3'},
                        {'denoise_custom': 'y-spatial=3'}, {'lapsharp': True, 'unsharp': True}):
            with self.assertRaises(SpecError):
                self.args({'filters': filters})

    def test_color_signalling_and_conversion_are_distinct(self):
        args = self.args({'filters': {'color_matrix': 'bt709', 'color_range': 'full',
                                     'color_primaries': 'bt2020', 'color_transfer': 'smpte2084'}})
        self.assertEqual(args[args.index('--color-matrix') + 1], '709')
        self.assertEqual(args[args.index('--colorspace') + 1], 'primaries=bt2020:transfer=smpte2084')
        self.assertNotIn('--color-primaries', args)
        self.assertNotIn('--color-transfer', args)
        for key, value in [('color_matrix', 'fcc'), ('color_primaries', 'bt601'), ('color_transfer', 'bt2100')]:
            with self.assertRaises(SpecError):
                self.args({'filters': {key: value}})

    def test_lossless_encoder_specific_and_no_fake_cqp(self):
        for encoder in ('x264', 'x265'):
            args = self.args({'video': {'encoder': encoder, 'quality_type': 'lossless'}})
            self.assertEqual(args[args.index('-q') + 1], '0')
            self.assertNotIn('--lossless', args)
            self.assertEqual('--encopts' in args, encoder == 'x265')
        preset = {'PresetList': [{'VideoEncoder': 'x265'}]}
        self.assertIn('lossless=1', self.args({'video': {'quality_type': 'lossless'}}, preset))
        for video in ({'quality_type': 'cqp'}, {'encoder': 'av1', 'quality_type': 'lossless'}, {'quality': None}):
            with self.assertRaises(SpecError):
                self.args({'video': video})

    def test_samplerate_hz_to_khz_and_names(self):
        args = self.args({'audio': {'tracks': [{'samplerate': '44100', 'name': 'Main'}, {'samplerate': '48000', 'gain': -5}]}})
        self.assertEqual(args[args.index('--arate') + 1], '44.1,48')
        self.assertEqual(args[args.index('--aname') + 1], 'Main,')
        self.assertEqual(args[args.index('--gain') + 1], '0,-5')
        self.assertNotIn('--ar', args)
        self.assertNotIn('--audio-track-names', args)
        for track in ({'name': 'one,two'}, {'encoder': 'copy', 'gain': 2}, {'encoder': 'none', 'samplerate': '48000'}):
            with self.assertRaises(SpecError):
                self.args({'audio': {'tracks': [track]}})

    def test_srt_independent_burn_default_language_offset(self):
        args = self.args({'subtitles': {'srt_file': 'sub.srt', 'srt_burn': True,
                                       'srt_default': True, 'srt_language': 'eng', 'srt_offset_ms': -500}})
        self.assertIn('--srt-burn=1', args)
        self.assertIn('--srt-default=1', args)
        self.assertEqual(args[args.index('--srt-codeset') + 1], 'UTF-8')
        self.assertEqual(args[args.index('--srt-offset') + 1], '-500')
        self.assertEqual(args[args.index('--srt-lang') + 1], 'eng')
        self.assertIn('--subtitle-burned=none', args)

    def test_source_subtitle_indexes_selected_list(self):
        args = self.args({'subtitles': {'tracks': [{'track': 3}, {'track': 5}], 'burn_track': 2, 'default_track': 1}})
        self.assertIn('--subtitle-burned=2', args)
        self.assertIn('--subtitle-default=1', args)
        for subs in ({'tracks': [{'track': 5}], 'burn_track': 5}, {'default_track': 1},
                     {'forced_only': True}, {'srt_burn': True}, {'srt_language': 'en'},
                     {'srt_file': 'x.srt', 'srt_codeset': 'UTF-8,latin1'},
                     {'srt_file': 'x.srt', 'behavior': 'burn', 'srt_burn': True}):
            with self.assertRaises(SpecError):
                self.args({'subtitles': subs})

    def test_snapshot_resets_are_private_and_cli_resets_explicit(self):
        original = {'PresetList': [{'VideoFramerate': '15', 'VideoFramerateMode': 'cfr', 'AudioCopyMask': ['copy:aac']}]}
        raw = {'video': {'framerate': 'auto', 'cfr': False, 'tune': None, 'profile': 'auto', 'level': 'auto'},
               'audio': {'copy_mask': None}}
        snapshot = prepare_job_preset(original, raw)
        self.assertEqual(original['PresetList'][0]['VideoFramerate'], '15')
        self.assertEqual(snapshot['PresetList'][0]['VideoFramerate'], 'auto')
        self.assertEqual(snapshot['PresetList'][0]['VideoFramerateMode'], 'vfr')
        self.assertEqual(snapshot['PresetList'][0]['AudioCopyMask'], [])
        args = self.args(raw, snapshot)
        self.assertNotIn('-r', args)
        self.assertEqual(args[args.index('--encoder-tune') + 1], '')
        self.assertEqual(args[args.index('--encoder-profile') + 1], 'auto')
        self.assertEqual(args[args.index('--encoder-level') + 1], 'auto')
        for raw in ({'video': {'preset': None}}, {'dimensions': {'width': None}},
                    {'filters': {'color_matrix': 'auto'}}):
            with self.assertRaises(SpecError):
                self.args(raw, snapshot)

    def test_zero_deblock_is_not_false_and_chapters_auto_explicit(self):
        preset = {'PresetList': [{'VideoEncoder': 'x264'}]}
        args = self.args({'filters': {'deblock': 0}, 'chapters': {'mode': 'auto'}}, preset)
        self.assertIn('--deblock=strength=strong:thresh=0', args)
        self.assertNotIn('--no-deblock', args)
        self.assertIn('--markers', args)
        args = self.args({'filters': {'color_range': 'auto'}}, preset)
        self.assertEqual(args[args.index('--color-range') + 1], 'auto')

    def test_sparse_flags_are_not_lost_by_optional_argument_arity(self):
        preset = {'PresetList': [{'VideoEncoder': 'x264'}]}
        args = self.args({'filters': {'denoise': 'nlmeans', 'chroma_smooth': True, 'grayscale': True},
                          'audio': {'tracks': [{'samplerate': '48000', 'gain': -5}]}}, preset)
        for flag in ('--nlmeans=medium', '--chroma-smooth=medium', '--grayscale', '--no-hqdn3d', '--arate', '--gain'):
            self.assertIn(flag, args)
        self.assertNotIn('--encoder-preset', args)


if __name__ == '__main__':
    unittest.main()
