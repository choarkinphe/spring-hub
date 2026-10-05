"""Real CPU playback-template contract checks; does not alter service data.

PYTHONPATH=next python3 next/tools/verify_playback.py --ffmpeg /path/ffmpeg --ffprobe /path/ffprobe
"""
import argparse
import json
import struct
import subprocess
import tempfile
from pathlib import Path
from cutecat.builtin_templates import builtin_templates
from cutecat.config import EngineConfig
from cutecat.engine import HandBrakeEngine
from cutecat.ffmpeg_engine import FFmpegEngine
from cutecat.backends import build_args
from cutecat.spec import TranscodeSpec


def boxes(path):
    result=[]
    with path.open('rb') as handle:
        while header := handle.read(8):
            size, kind = struct.unpack('>I4s',header)
            if size == 1:
                size = struct.unpack('>Q',handle.read(8))[0]
                extra=8
            else:
                extra=0
            result.append(kind.decode())
            if size == 0:
                break
            handle.seek(size-8-extra,1)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ffmpeg',required=True)
    parser.add_argument('--ffprobe',required=True)
    parser.add_argument('--handbrake',default='HandBrakeCLI')
    args=parser.parse_args()
    config=EngineConfig(handbrake_bin=args.handbrake,ffmpeg_bin=args.ffmpeg,ffprobe_bin=args.ffprobe)
    engines={'handbrake':HandBrakeEngine(config),'ffmpeg':FFmpegEngine(config)}
    with tempfile.TemporaryDirectory(prefix='springhub-playback-') as directory:
        source=Path(directory)/'source.mkv'
        subprocess.run([args.ffmpeg,'-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=320x240:rate=24',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','3','-c:v','ffv1','-c:a','pcm_s16le',str(source)],check=True)
        for name, engine in engines.items():
            for template in builtin_templates():
                spec=TranscodeSpec.from_dict(template['spec'])
                output=Path(directory)/(name+'-'+template['slug']+'.mp4')
                engine.run_encode(input_path=str(source),output_path=str(output),args=build_args(name,spec),timeout=90)
                scan=engine.scan(str(output))
                assert scan.get('titles'), (name,template['slug'],'no video')
                metadata=json.loads(subprocess.check_output([args.ffprobe,'-v','error','-show_format','-show_streams','-of','json',str(output)],text=True))
                video=next(s for s in metadata['streams'] if s['codec_type']=='video')
                assert video['codec_name']==('hevc' if template['slug']=='hevc' else 'h264'), video
                assert video['pix_fmt']=='yuv420p',video
                assert (video['width'],video['height'])==(320,240),video
                assert float(metadata['format']['duration'])>0
                audio=[s for s in metadata['streams'] if s['codec_type']=='audio']
                assert bool(audio)==bool(spec.audio.tracks),audio
                if audio:
                    assert audio[0]['codec_name']=='aac' and audio[0]['sample_rate']=='48000',audio
                atoms=boxes(output)
                assert atoms.index('moov')<atoms.index('mdat'),atoms
                print(f'{name} {template["slug"]}: {video["codec_name"]} {video["width"]}x{video["height"]} {video["pix_fmt"]}, faststart OK',flush=True)
        # Non-square pixels, portrait orientation and high-rate downscaling.
        for label, size, sar, expected in [('portrait','720x1280','1',(360,640)),
                                            ('anamorphic','720x576','16/15',(768,576))]:
            fixture=Path(directory)/(label+'.mkv')
            subprocess.run([args.ffmpeg,'-nostdin','-v','error','-f','lavfi','-i',f'testsrc2=size={size}:rate=60',
                '-t','1','-vf','setsar='+sar,'-c:v','ffv1',str(fixture)],check=True)
            raw=builtin_templates()[1]['spec']
            raw['audio']['tracks']=[]
            if label=='portrait': raw['dimensions'].update(width=640,height=640)
            output=Path(directory)/(label+'-out.mp4')
            engines['ffmpeg'].run_encode(input_path=str(fixture),output_path=str(output),args=build_args('ffmpeg',TranscodeSpec.from_dict(raw)),timeout=90)
            meta=json.loads(subprocess.check_output([args.ffprobe,'-v','error','-show_streams','-of','json',str(output)],text=True))
            v=meta['streams'][0]
            assert (v['width'],v['height'])==expected,v
            assert v['sample_aspect_ratio']=='1:1',v
            n,d=map(int,v['avg_frame_rate'].split('/')); assert n/d<=30,v
            print(f'ffmpeg {label}: display aspect, size and <=30 fps OK',flush=True)
        # Verify actual second pass, not just argv generation.
        raw=builtin_templates()[0]['spec']
        raw['video'].update(quality_type='abr',bitrate_kbps=600,two_pass=True,turbo=False)
        output=Path(directory)/'two-pass.mp4'
        engines['ffmpeg'].run_encode(input_path=str(source),output_path=str(output),args=build_args('ffmpeg',TranscodeSpec.from_dict(raw)),timeout=90)
        assert list(Path(directory).glob('passlog*'))
        assert engines['ffmpeg'].scan(str(output))['titles']
        print('ffmpeg two-pass: both passes and playable output OK',flush=True)


if __name__=='__main__':
    main()
