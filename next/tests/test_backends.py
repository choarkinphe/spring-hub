"""Playback templates, FFmpeg mapping, API/CLI and remote safety regression."""
import copy
import io
import json
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from helpers import TempEnv
from test_api import _request
from cutecat.spec import TranscodeSpec, SpecError, build_engine_args
from cutecat.builtin_templates import builtin_templates, find_template
from cutecat.ffmpeg_spec import build_ffmpeg_args
from cutecat.ffmpeg_engine import FFmpegEngine
from cutecat.engine import HandBrakeEngine, EngineCapabilities, EngineError
from cutecat.server import AppState, build_server
from cutecat.store import Store
from cutecat.worker import Worker
from cutecat.templates import TemplateBundles, validate_template, FIELDS
from cutecat.client import main as client_main
from cutecat.backends import build_args
from cutecat.process import run_process


class MappingTests(unittest.TestCase):
    def test_every_builtin_has_stable_safe_spec_for_all_engines(self):
        first = builtin_templates()
        self.assertEqual(len(first), 9)
        self.assertEqual([t['id'] for t in first], [t['id'] for t in builtin_templates()])
        for template in first:
            spec = TranscodeSpec.from_dict(template['spec'])
            hb, ff = build_engine_args(spec), build_ffmpeg_args(spec)
            self.assertIn('--optimize', hb)
            self.assertIn('-X', hb)
            self.assertNotIn('-w', hb)
            self.assertIn('+faststart', ff)
            self.assertIn('yuv420p', ff)
            self.assertEqual(spec.dimensions.crop_mode, 'none')
            self.assertEqual(spec.video.framerate, '30')
            self.assertEqual(spec.to_dict(), template['spec'])
        first[0]['spec']['video']['quality'] = 0
        self.assertEqual(builtin_templates()[0]['spec']['video']['quality'], 22)

    def test_streaming_validation_rejects_arbitrary_and_unpaired_values(self):
        for stream in ({'argv':[]}, {'faststart':'true'}, {'maxrate_kbps':100}, {'maxrate_kbps':1.5,'buffer_kbps':2}, {'keyframe_interval':0}):
            with self.assertRaises(SpecError):
                TranscodeSpec.from_dict({'streaming':stream})
        with self.assertRaises(SpecError):
            build_engine_args(TranscodeSpec.from_dict({'streaming':{'faststart':True}}))

    def test_ffmpeg_rejects_non_equivalent_features_and_raw_flags(self):
        base = builtin_templates()[0]['spec']
        for fragment in ({'preset':'Fast'}, {'title':2}, {'filters':{'lapsharp':True}},
                         {'dimensions':{'crop_mode':'auto'}}, {'subtitles':{'srt_file':'x.srt'}},
                         {'chapters':{'mode':'markers'}}, {'video':{'turbo':True,'two_pass':True,'quality_type':'abr','bitrate_kbps':1000}}):
            raw = copy.deepcopy(base)
            for key, val in fragment.items():
                raw[key] = {**raw.get(key, {}), **val} if isinstance(val, dict) else val
            with self.assertRaises(SpecError):
                build_ffmpeg_args(TranscodeSpec.from_dict(raw))
        with self.assertRaises(SpecError):
            TranscodeSpec.from_dict({'args':['-y']})

    def test_audio_indexes_and_two_pass_marker(self):
        raw = copy.deepcopy(builtin_templates()[0]['spec'])
        raw['video'].update(quality_type='abr',bitrate_kbps=1000,two_pass=True)
        raw['audio']['tracks'] = [{'source':'2','encoder':'aac','samplerate':'44100','bitrate':'128'}]
        args = build_args('ffmpeg', TranscodeSpec.from_dict(raw))
        self.assertIn('0:a:1', args)
        self.assertIn('44100', args)
        self.assertIn('__two_pass__', args)


class BackendApiTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.config = self.env.config()
        self.store = Store(self.config.database)
        self.engine = HandBrakeEngine(self.config.engine)
        self.state = AppState(self.config, self.store, self.engine)
        self.server = build_server(self.state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close()
        self.store.close(); self.env.cleanup()

    def req(self, method, path, body=None, headers=None):
        return _request(self.port, method, '/api/v1/' + path, body, headers)

    def test_builtin_readonly_default_copy_and_export(self):
        code, data = self.req('GET','task-templates')
        self.assertEqual(code,200)
        template = data['templates'][0]
        self.assertTrue(template['builtin'])
        self.assertEqual(self.store.list_templates(), [])
        self.assertEqual(self.req('DELETE','task-templates/'+template['id'])[0],409)
        self.assertEqual(self.req('POST','task-templates/'+template['id'],{})[0],409)
        self.assertEqual(self.req('POST','settings',{'default_task_template_id':template['id']})[0],200)
        payload = {k:v for k,v in template.items() if k in FIELDS}
        code, created = self.req('POST','task-templates',payload)
        self.assertEqual(code,200,created)
        self.assertNotEqual(created['id'],template['id'])
        code, bundle = self.req('POST','task-templates/export',{'ids':[template['id']]})
        self.assertEqual(code,200,bundle)
        self.assertEqual(self.req('POST','task-templates/import-preview',bundle)[0],200)
        self.assertEqual(self.req('POST','task-templates/import',bundle)[0],200)

    def test_validation_template_and_engine_selection_no_job_side_effect(self):
        identity = builtin_templates()[0]['id']
        for name in ('handbrake','ffmpeg','rffmpeg'):
            code, data = self.req('POST','spec/validate',{'engine':name,'template_id':identity})
            self.assertEqual(code,200,data)
            self.assertEqual(data['engine'],name)
            self.assertEqual(data['template_id'],identity)
        for payload in ({'engine':'evil'}, {'template_id':'unknown'}, {'engine':'ffmpeg','preset_id':'Fast','spec':{}}, {'engine':'ffmpeg','argv':[]}):
            self.assertEqual(self.req('POST','spec/validate',payload)[0],400)
        self.assertEqual(self.store.list_jobs(),[])
        code, cleared = self.req('POST','spec/validate',{'template_id':identity,'spec_mode':'replace',
            'spec':{'container':'mp4','dimensions':{'crop_mode':'none'}}})
        self.assertEqual(code,200,cleared)
        self.assertNotIn('streaming',cleared['spec'])
        self.assertNotIn('--optimize',cleared['args'])

    def test_template_job_is_frozen_and_legacy_default_unchanged(self):
        template = builtin_templates()[1]
        body = {'template_id':template['id'],'input':{'root':'media','path':'movie.mp4'},'output':{'root':'out','path':'new.mp4'}}
        code, job = self.req('POST','jobs',body)
        self.assertEqual(code,200,job)
        self.assertEqual(job['engine'],'handbrake')
        self.assertEqual(job['spec']['streaming']['faststart'],True)
        saved = self.store.get_job(job['id'])
        self.assertEqual(saved.execution['template_id'],template['id'])
        self.assertIn('--optimize',saved.args)
        body.update(engine='ffmpeg',output={'root':'out','path':'ff.mp4'})
        caps = EngineCapabilities(True,'ffmpeg','8','ffmpeg 8',True,False,
                                  audio_encoders=['aac'],audio_encoders_known=True,video_encoder_probe={'x264':'works'})
        with patch.object(self.state.backends.get('ffmpeg'),'probe',return_value=caps):
            code, ff = self.req('POST','jobs',body)
        self.assertEqual(code,200,ff)
        self.assertEqual(ff['engine'],'ffmpeg')
        self.assertIn('libx264',ff['args'])
        self.assertEqual(self.store.get_job(job['id']).spec,saved.spec)

    def test_engine_endpoint_and_auth(self):
        code, data = self.req('GET','engines')
        self.assertEqual(code,200)
        self.assertEqual([e['id'] for e in data['engines']],['handbrake','ffmpeg','rffmpeg'])
        self.assertFalse(data['engines'][2]['running_pause'])
        self.state.config = replace(self.config,api_token='secret')
        self.assertEqual(self.req('GET','engines')[0],401)
        self.assertEqual(self.req('GET','engines',headers={'Authorization':'Bearer secret'})[0],200)

    def test_cli_queries_and_template_validation_use_same_http(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = client_main(['validate','--server',f'http://127.0.0.1:{self.port}','--engine','ffmpeg','--template',builtin_templates()[0]['id']])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(out.getvalue())['engine'],'ffmpeg')
        self.assertEqual(self.store.list_jobs(),[])
        with redirect_stderr(io.StringIO()), patch('cutecat.client.request') as call:
            self.assertEqual(client_main(['submit','--wait','--wait-timeout','0']),2)
            call.assert_not_called()


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv(); self.config = self.env.config(); self.store = Store(self.config.database)

    def tearDown(self):
        self.store.close(); self.env.cleanup()

    def test_ffmpeg_two_pass_really_calls_twice_and_isolates_passlog(self):
        engine = FFmpegEngine(replace(self.config.engine,ffmpeg_bin=sys.executable))
        calls=[]
        with patch.object(engine,'scan',return_value={'titles':[{'Duration':6}]}), patch('cutecat.ffmpeg_engine.run_process',side_effect=lambda cmd,**kw: calls.append(cmd) or .1):
            engine.run_encode(input_path='in.avi',output_path=str(self.env.out/'stage'/'out.mp4'),args=['-c:v','libx264','__two_pass__'])
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0][calls[0].index('-pass')+1],'1')
        self.assertEqual(calls[1][calls[1].index('-pass')+1],'2')
        self.assertIn(str(self.env.out/'stage'/'passlog'),calls[1])
        self.assertNotIn('__two_pass__',calls[0])
        self.assertEqual(calls[1][-1],str(self.env.out/'stage'/'out.mp4'))

    def test_remote_active_pause_rejected_and_failed_staging_retained(self):
        worker = Worker(self.config,self.store,HandBrakeEngine(self.config.engine))
        job = self.store.create_job(input_root='media',input_path='movie.mp4',output_root='out',output_path='x.mp4',preset_id='custom',preset_name=None,container='mp4',spec={},args=[],execution={'engine':'rffmpeg'})
        self.store.set_status(job.id,'running')
        self.assertNotIn('pause',self.store.get_job(job.id).actions())
        self.assertFalse(self.store.control_job(job.id,'pause'))
        with self.assertRaises(EngineError):
            with worker._staging(job,self.env.out,remote=True) as path:
                Path(path,'out.mp4').write_bytes(b'partial')
                raise EngineError('__canceled__')
        self.assertTrue(Path(path,'out.mp4').exists())
        self.assertFalse((self.env.out/'x.mp4').exists())
        self.assertIn('remote exit not confirmed',self.store.get_logs(job.id)[-1]['message'])

    def test_remote_probe_dir_must_be_in_available_writable_root(self):
        engine = FFmpegEngine(replace(self.config.engine,remote_probe_dir=str(self.env.out)),remote=True,roots=self.config.storage_roots)
        self.assertEqual(engine._probe_directory(),str(self.env.out))
        engine.config = replace(engine.config,remote_probe_dir='/tmp')
        with self.assertRaises(EngineError): engine._probe_directory()
        engine.config = replace(engine.config,remote_probe_dir=str(self.env.out))
        engine.roots = (replace(self.config.storage_roots[1],mount_marker='.mounted'),)
        with self.assertRaises(EngineError): engine._probe_directory()

    def test_remote_probe_scratch_preserved_without_renaming_on_failure(self):
        engine = FFmpegEngine(self.config.engine,remote=True,roots=self.config.storage_roots)
        verdicts={}
        with engine._probe_scratch(str(self.env.out),verdicts) as path:
            verdicts['x264']='blocked'
        self.assertTrue(Path(path).is_dir())
        with engine._probe_scratch(str(self.env.out),{'x264':'works'}) as good:
            pass
        self.assertFalse(Path(good).exists())

    def test_process_cancel_and_timeout(self):
        cmd=[sys.executable,'-c','import time; time.sleep(20)']
        with self.assertRaisesRegex(EngineError,'__canceled__'):
            run_process(cmd,should_cancel=lambda:True)
        with self.assertRaisesRegex(EngineError,'timed out'):
            run_process(cmd,timeout=.1)

    def test_pause_preserves_process_and_excludes_paused_time(self):
        import time
        paused, done = threading.Event(), threading.Event()
        errors=[]
        def execute():
            try:
                run_process([sys.executable,'-c','import time; time.sleep(.5)'],timeout=1,
                    should_pause=lambda:not done.is_set(),on_pause=lambda value:paused.set() if value else None)
            except Exception as exc:
                errors.append(exc)
        thread=threading.Thread(target=execute)
        thread.start()
        self.assertTrue(paused.wait(2))
        time.sleep(1.2)
        self.assertTrue(thread.is_alive())
        done.set();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])

    def test_timeout_is_unknown_not_missing_or_blocked(self):
        from cutecat.ffmpeg_engine import InvocationUnknown
        engine = FFmpegEngine(self.config.engine)
        with patch('cutecat.ffmpeg_engine.subprocess.run',side_effect=subprocess.TimeoutExpired('ffmpeg',1)):
            with self.assertRaises(InvocationUnknown):
                engine._run(['ffmpeg','-version'])

    def test_inventory_parsing(self):
        self.assertEqual(FFmpegEngine._inventory(' V....D libx264 H264\n A..... aac AAC\n'),{'libx264':'V','aac':'A'})


if __name__ == '__main__':
    unittest.main()
