#!/usr/bin/env python3
"""Opt-in real HandBrakeCLI contract checks (stdlib, CPU, isolated files).

Run from next/: PYTHONPATH=. python3 tools/verify_cli_mapping.py
Requires a real HandBrakeCLI. No mock substitution and no user media writes.
Checks resolved job settings as well as output scan; not a visual-quality test.
"""

import inspect
import json
import subprocess
import tempfile
from pathlib import Path

from cutecat.config import EngineConfig
from cutecat.engine import HandBrakeEngine, _build_probe_clip, _extract_json_documents
from cutecat.spec import TranscodeSpec, build_engine_args, prepare_job_preset


def make_source(path):
    # Reuse the raw fixture format, but give scans a positive whole-second
    # duration and standard 16-byte AVI index entries for seeking previews.
    code = inspect.getsource(_build_probe_clip)
    code = code.replace('fps, frames = 1, 1', 'fps, frames = 30, 180')
    code = code.replace('chunk(b"00db", struct.pack("<IIII", 0x10, offset, len(frame), 0))',
                        'struct.pack("<4sIII", b"00db", 0x10, offset, len(frame))')
    code = code.replace('chunk(b"01wb", struct.pack("<IIII", 0x10, offset, len(audio) - 8, 0))',
                        'struct.pack("<4sIII", b"01wb", 0x10, offset, len(audio) - 8)')
    namespace = dict(_build_probe_clip.__globals__)
    exec(code, namespace)
    namespace['_build_probe_clip'](str(path))


def main():
    engine = HandBrakeEngine(EngineConfig(handbrake_bin='HandBrakeCLI', ffprobe_bin=''))
    assert engine.binary_path(), 'A real HandBrakeCLI is required'
    caps = engine.probe()
    assert caps.version == '1.11.0', f'This contract check targets 1.11.0, got {caps.version}'
    assert caps.video_encoder_probe.get('x264') == 'works'
    cases = [
        ('geometry', {'dimensions': {'width': 160, 'height': 120, 'crop_mode': 'none', 'anamorphic': 'none', 'modulus': 2}}, {}),
        ('loose', {'dimensions': {'anamorphic': 'loose', 'crop_mode': 'none'}}, {}),
        ('rotate90-flip', {'filters': {'rotate': '90', 'hflip': True}}, {'angle': '90', 'hflip': '1'}),
        ('rotate180', {'filters': {'rotate': '180'}}, {'angle': '180'}),
        ('rotate270', {'filters': {'rotate': '270'}}, {'angle': '270'}),
        ('yadif-fast', {'filters': {'deinterlace': 'fast'}}, {'mode': '1'}),
        ('yadif-default', {'filters': {'deinterlace': 'default'}}, {'mode': 3}),
        ('yadif-custom', {'filters': {'deinterlace': 'custom', 'deinterlace_custom': 'mode=3:parity=0'}}, {'mode': '3'}),
        ('bob', {'filters': {'deinterlace': 'bob'}}, {'mode': 7}),
        ('hqdn3d', {'filters': {'denoise': 'hqdn3d'}}, {'y-spatial': 3}),
        ('nlmeans', {'filters': {'denoise': 'nlmeans'}}, {'y-strength': 6}),
        ('hqdn3d-custom', {'filters': {'denoise': 'custom', 'denoise_custom': 'y-spatial=2:cb-spatial=2:cr-spatial=2:y-temporal=2:cb-temporal=2:cr-temporal=2'}}, {'y-spatial': '2'}),
        ('detelecine', {'filters': {'detelecine': 'default'}}, {'skip-left': 1}),
        ('detelecine-custom', {'filters': {'detelecine': 'custom', 'detelecine_custom': 'skip-left=1:skip-right=1:skip-top=4:skip-bottom=4:plane=0'}}, {'skip-left': '1'}),
        ('deblock', {'filters': {'deblock': 5}}, {'thresh': '5'}),
        ('chroma', {'filters': {'chroma_smooth': True}}, {'cb-strength': 1.2}),
        ('lapsharp', {'filters': {'lapsharp': True}}, {'y-kernel': 'isolap'}),
        ('unsharp', {'filters': {'unsharp': True}}, {'y-size': 7}),
        ('grayscale', {'filters': {'grayscale': True}}, {}),
        ('colors', {'filters': {'color_matrix': 'bt709', 'color_range': 'full', 'color_primaries': 'bt709', 'color_transfer': 'bt709'}}, {'primaries': 'bt709', 'transfer': 'bt709'}),
        ('lossless264', {'video': {'quality_type': 'lossless'}}, {}),
        ('lossless265', {'video': {'encoder': 'x265', 'quality_type': 'lossless'}}, {}),
        ('audio', {'audio': {'tracks': [{'source': '1', 'encoder': 'aac', 'samplerate': '44100', 'bitrate': '96', 'mixdown': 'mono', 'gain': -5, 'name': 'Main'}]}}, {}),
        ('audio-multiple', {'audio': {'tracks': [{'source': '1', 'samplerate': '44100', 'name': 'First'}, {'source': '1', 'samplerate': '48000', 'name': 'Second', 'gain': -3}]}}, {}),
        ('color2020', {'filters': {'color_matrix': 'bt2020', 'color_primaries': 'bt2020', 'color_transfer': 'smpte2084'}}, {'primaries': 'bt2020', 'transfer': 'smpte2084'}),
        ('color601', {'filters': {'color_matrix': 'bt601', 'color_range': 'auto'}}, {}),
        ('crop', {'dimensions': {'crop_mode': 'custom', 'crop_top': 4, 'crop_bottom': 4, 'crop_left': 2, 'crop_right': 2, 'anamorphic': 'none'}}, {'crop-top': 4, 'crop-left': 2}),
        ('srt-default', {'subtitles': {'srt_file': 'sub.srt', 'srt_default': True, 'srt_language': 'eng', 'srt_offset_ms': -500}}, {}),
        ('srt-burn', {'subtitles': {'srt_file': 'sub.srt', 'srt_burn': True}}, {}),
    ]
    with tempfile.TemporaryDirectory(prefix='cute-cat-real-cli-') as directory:
        root = Path(directory)
        source = root / 'source.avi'
        make_source(source)
        (root / 'sub.srt').write_text('1\n00:00:00,500 --> 00:00:03,000\n真实字幕 UTF-8\n', encoding='utf-8')
        results = []
        for name, payload, settings in cases:
            raw = {'container': 'mkv', 'video': {'encoder': 'x264', 'preset': 'ultrafast'},
                   'dimensions': {'crop_mode': 'none'}, **payload}
            if 'video' in payload:
                raw['video'] = {'encoder': 'x264', 'preset': 'ultrafast', **payload['video']}
            spec = TranscodeSpec.from_dict(raw)
            args = build_engine_args(spec)
            if '--srt-file' in args:
                args[args.index('--srt-file') + 1] = str(root / 'sub.srt')
            output = root / (name + '.mkv')
            command = engine.build_encode_command(input_path=str(source), output_path=str(output), args=args)
            proc = subprocess.run([*command, '--previews', '1:0', '--json'], capture_output=True, text=True, timeout=180)
            assert proc.returncode == 0, f'{name}: {proc.stderr[-5000:]}'
            assert output.is_file() and output.stat().st_size > 0, (name, proc.stderr[-6000:])
            documents = _extract_json_documents(proc.stderr) + _extract_json_documents(proc.stdout)
            job = next(doc for doc in documents if isinstance(doc, dict) and 'Video' in doc and 'Filters' in doc and 'Destination' in doc)
            filters = job['Filters']['FilterList']
            for key, value in settings.items():
                assert any(f['Settings'].get(key) == value or str(f['Settings'].get(key)) == str(value) for f in filters), (name, key, filters)
            scan = engine.scan(str(output))
            title = scan['titles'][0]
            geometry = title['Geometry']
            assert title['Duration']['Seconds'] > 0, title['Duration']
            if name == 'grayscale':
                assert any(f['ID'] == 26 for f in filters), filters
            if name == 'bob':
                assert title['FrameRate']['Num'] / title['FrameRate']['Den'] == 60, title['FrameRate']
            if name == 'geometry':
                assert (geometry['Width'], geometry['Height']) == (160, 120), geometry
            if name == 'rotate90-flip':
                assert (geometry['Width'], geometry['Height']) == (240, 320), geometry
            if name == 'audio':
                audio = title['AudioList'][0]
                assert audio['SampleRate'] == 44100, audio
                assert job['Audio']['AudioList'][0]['Name'] == 'Main'
                assert job['Audio']['AudioList'][0]['Gain'] == -5
            if name == 'audio-multiple':
                assert [a['SampleRate'] for a in title['AudioList']] == [44100, 48000], title['AudioList']
                assert [a['Name'] for a in job['Audio']['AudioList']] == ['First', 'Second']
            if name.startswith('lossless'):
                assert job['Video']['Quality'] == 0, job['Video']
                if name == 'lossless265':
                    assert 'lossless=1' in job['Video']['Options']
                    assert 'Rate Control                        : Lossless' in proc.stderr
            if name == 'colors':
                assert job['Video']['ColorMatrixOverride'] == 1
                assert job['Video']['ColorRange'] == 2
            if name.startswith('srt-'):
                subtitles = job['Subtitle']['SubtitleList']
                assert len(subtitles) == 1, job['Subtitle']
                assert subtitles[0]['Burn'] == (name == 'srt-burn'), subtitles
                assert subtitles[0]['Default'] == (name == 'srt-default'), subtitles
                assert bool(title['SubtitleList']) == (name == 'srt-default'), title['SubtitleList']
            result = {'case': name, 'bytes': output.stat().st_size, 'width': geometry['Width'], 'height': geometry['Height'],
                      'filter_ids': [f['ID'] for f in filters], 'video_codec': title['VideoCodec']}
            results.append(result)
            print(json.dumps(result), flush=True)
        # Exercise imported preset resets with deliberately non-default values.
        document = engine.export_preset('Fast 1080p30')
        entry = document['PresetList'][0]
        entry.update(VideoTune='film', VideoProfile='high', VideoLevel='4.0', VideoFramerate='15',
                     PictureRotate='angle=270:hflip=1', PictureDenoiseFilter='nlmeans',
                     PictureDenoisePreset='medium', PictureDeinterlaceFilter='yadif',
                     PictureDeinterlacePreset='default', PictureCombDetectPreset='default',
                     VideoMultiPass=True, VideoTurboMultiPass=True)
        raw = {'preset': '__cute_cat_job__', 'video': {'tune': None, 'profile': 'auto', 'level': 'auto',
               'framerate': 'auto', 'two_pass': False, 'turbo': False},
               'filters': {'rotate': '90', 'hflip': False, 'denoise': 'off', 'deinterlace': 'off'}}
        snapshot = prepare_job_preset(document, raw)
        snapshot_path = root / 'preset.json'
        snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
        args = ['--preset-import-file', str(snapshot_path), *build_engine_args(TranscodeSpec.from_dict(raw), overrides=raw, preset=snapshot)]
        output = root / 'preset-reset.mkv'
        command = engine.build_encode_command(input_path=str(source), output_path=str(output), args=args)
        proc = subprocess.run([*command, '--previews', '1:0', '--json'], capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0 and output.is_file(), proc.stderr[-5000:]
        job = next(d for d in _extract_json_documents(proc.stderr) + _extract_json_documents(proc.stdout)
                   if isinstance(d, dict) and 'Video' in d and 'Destination' in d)
        assert job['Video']['Tune'] == '' and job['Video']['Profile'] == 'auto' and job['Video']['Level'] == 'auto', job['Video']
        assert not job['Video']['MultiPass'] and not job['Video']['Turbo'], job['Video']
        assert not any(f['ID'] in (3, 6, 7, 13, 14) for f in job['Filters']['FilterList']), job['Filters']
        title = engine.scan(str(output))['titles'][0]
        assert title['FrameRate']['Num'] / title['FrameRate']['Den'] == 30, title['FrameRate']
        assert title['Geometry']['Width'] == 240 and title['Geometry']['Height'] == 320
        print('PASS: imported preset resets preserve source 30 fps and combined rotation; inherited filters disabled', flush=True)
        # Build two soft source subtitles, then select the second one as list index 1.
        for number in (1, 2):
            (root / f'source{number}.srt').write_text(f'1\n00:00:00,500 --> 00:00:03,000\nSource {number}\n', encoding='utf-8')
        source_sub = root / 'source-subtitles.mkv'
        proc = subprocess.run(['HandBrakeCLI', '-i', str(source), '-o', str(source_sub), '-f', 'av_mkv',
                '-e', 'x264', '--encoder-preset', 'ultrafast', '--subtitle', 'none', '--srt-file',
                ','.join(str(root / f'source{n}.srt') for n in (1, 2)), '--srt-codeset', 'UTF-8,UTF-8',
                '--srt-lang', 'eng,fra'], capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0 and len(engine.scan(str(source_sub))['titles'][0]['SubtitleList']) == 2
        for burn in (False, True):
            raw = {'container': 'mkv', 'video': {'preset': 'ultrafast'},
                   'subtitles': {'tracks': [{'track': 2}], 'burn_track': 1 if burn else None, 'default_track': 1}}
            output = root / f'source-selected-{burn}.mkv'
            cmd = engine.build_encode_command(input_path=str(source_sub), output_path=str(output), args=build_engine_args(TranscodeSpec.from_dict(raw)))
            proc = subprocess.run([*cmd, '--json'], capture_output=True, text=True, timeout=180)
            assert proc.returncode == 0 and output.is_file(), proc.stderr[-5000:]
            job = next(d for d in _extract_json_documents(proc.stderr) + _extract_json_documents(proc.stdout)
                       if isinstance(d, dict) and 'Video' in d and 'Destination' in d)
            selected = job['Subtitle']['SubtitleList']
            assert len(selected) == 1 and selected[0]['Track'] == 1 and selected[0]['Burn'] == burn, selected
            title = engine.scan(str(output))['titles'][0]
            assert len(title['SubtitleList']) == (0 if burn else 1), title['SubtitleList']
        print('PASS: source track 2 selected, burn/default use selected-list index 1')
        print(f'PASS: {len(results) + 4} real encodes, job configuration and output scan verified')


if __name__ == '__main__':
    main()
