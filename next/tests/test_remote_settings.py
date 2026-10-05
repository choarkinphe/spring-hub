"""Web-managed rffmpeg configuration, snapshots and explicit checks."""
import json
import subprocess
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from helpers import TempEnv
from test_api import _request
from cutecat.engine import HandBrakeEngine, EngineError
from cutecat.server import AppState, build_server
from cutecat.store import Store
from cutecat.runtime import RuntimeSettings, SettingsConflict
from cutecat.worker import Worker
from cutecat.remote_settings import RemoteCheck, validate_remote


class RemoteSettingsTests(unittest.TestCase):
    def setUp(self):
        self.env=TempEnv(); self.config=self.env.config(); self.store=Store(self.config.database)
        self.runtime=RuntimeSettings(self.config,self.store)

    def tearDown(self):
        self.store.close();self.env.cleanup()

    def values(self, **overrides):
        return {'rffmpeg_bin':'/opt/rffmpeg/ffmpeg','rffprobe_bin':'rffprobe','remote_probe_dir':str(self.env.out),**overrides}

    def test_persistence_defaults_disable_and_no_invocations(self):
        with patch('subprocess.run',side_effect=AssertionError('save must not execute')):
            self.runtime.save(self.values())
            another=RuntimeSettings(self.config,self.store)
            self.assertEqual(another.read()['rffmpeg_bin'],'/opt/rffmpeg/ffmpeg')
            self.assertEqual(another.report()['defaults']['rffmpeg_bin'],'')
            another.save(dict.fromkeys(self.values(),''))
        self.assertEqual(self.runtime.read()['remote_probe_dir'],'')

    def test_format_boundary_and_conflict(self):
        for data in (self.values(rffmpeg_bin='rffmpeg --flag'),self.values(rffprobe_bin='relative/bin'),
                     self.values(rffmpeg_bin='evil\n'),self.values(remote_probe_dir='/tmp'),
                     self.values(remote_probe_dir=str(self.env.out/'..'/'outside')),self.values(rffprobe_bin='')):
            with self.assertRaises(ValueError): self.runtime.save(data)
        outside=self.env.tmp/'outside';outside.mkdir()
        (self.env.out/'escape').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):self.runtime.save(self.values(remote_probe_dir=str(self.env.out/'escape')))
        self.runtime.save(self.values(remote_probe_dir=str(self.env.out/'not-mounted-yet')))
        with self.assertRaises(SettingsConflict): self.runtime.save({'expected_revision':0})

    def test_legacy_partial_toml_does_not_break_unrelated_save(self):
        config=replace(self.config,engine=replace(self.config.engine,rffmpeg_bin='legacy'))
        runtime=RuntimeSettings(config,self.store)
        runtime.save({'auto_start':False})
        self.assertFalse(runtime.read()['auto_start'])
        with self.assertRaises(ValueError):runtime.save({'rffmpeg_bin':'new'})

    def test_check_version_contract_no_encode_or_files(self):
        config=replace(self.config.engine,**self.values(rffmpeg_bin='ffmpeg',rffprobe_bin='ffprobe'))
        before=set(self.env.out.iterdir())
        def invoke(cmd,**kw):
            self.assertEqual(cmd[1:],['-version']);self.assertEqual(kw['timeout'],5)
            return subprocess.CompletedProcess(cmd,0,cmd[0]+' version 8.0 test\n', '')
        with patch('cutecat.remote_settings.shutil.which',side_effect=lambda x:x),patch('cutecat.ffmpeg_engine.subprocess.run',side_effect=invoke):
            result=RemoteCheck().run(config,self.config.storage_roots)
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual(set(self.env.out.iterdir()),before)
        self.assertEqual(self.store.list_jobs(),[])

    def test_check_missing_mount_skips_remote_and_timeout_sanitized(self):
        config=replace(self.config.engine,**self.values(rffmpeg_bin='ffmpeg',rffprobe_bin='ffprobe'))
        roots=(replace(self.config.storage_roots[1],mount_marker='.missing'),)
        with patch('cutecat.remote_settings.shutil.which',return_value='ffmpeg'),patch('subprocess.run') as call:
            self.assertEqual(RemoteCheck().run(config,roots)['status'],'failed')
            call.assert_not_called()
        with patch('cutecat.remote_settings.shutil.which',side_effect=lambda x:x),patch('subprocess.run',side_effect=subprocess.TimeoutExpired('PRIVATE_HOST_PASSWORD',5)):
            result=RemoteCheck().run(config,self.config.storage_roots)
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('PRIVATE_HOST_PASSWORD',json.dumps(result))

    def test_check_concurrency_and_disabled(self):
        check=RemoteCheck();check._lock.acquire()
        try:
            with self.assertRaises(RuntimeError):check.run(self.config.engine,self.config.storage_roots)
        finally:check._lock.release()
        self.assertEqual(check.run(self.config.engine,self.config.storage_roots)['status'],'disabled')

    def test_api_worker_use_runtime_and_keep_old_engine_instance(self):
        state=AppState(self.config,self.store,HandBrakeEngine(self.config.engine))
        worker=Worker(self.config,self.store,state.engine,state.runtime)
        state.runtime.save(self.values())
        old=worker.backends.get('rffmpeg')
        snapshot=state.backends.remote_settings()
        state.runtime.save(self.values(rffmpeg_bin='/opt/new/ffmpeg'))
        current=worker.backends.get('rffmpeg')
        frozen=worker.backends.get('rffmpeg',remote_config=snapshot)
        self.assertEqual(old.config.rffmpeg_bin,'/opt/rffmpeg/ffmpeg')
        self.assertEqual(current.config.rffmpeg_bin,'/opt/new/ffmpeg')
        self.assertEqual(frozen.config.rffmpeg_bin,old.config.rffmpeg_bin)
        self.assertIsNot(current,old)


class RemoteApiTests(unittest.TestCase):
    def setUp(self):
        self.env=TempEnv();self.config=self.env.config();self.store=Store(self.config.database)
        self.state=AppState(self.config,self.store,HandBrakeEngine(self.config.engine))
        self.server=build_server(self.state);self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        self.values={'rffmpeg_bin':'ffmpeg','rffprobe_bin':'ffprobe','remote_probe_dir':str(self.env.out)}

    def tearDown(self):
        self.server.shutdown();self.thread.join();self.server.server_close();self.store.close();self.env.cleanup()

    def req(self, method, route, body=None):
        return _request(self.port,method,'/api/v1/'+route,body)

    def test_save_get_check_and_public_config(self):
        with patch('subprocess.run',side_effect=AssertionError('no automatic execution')):
            code,data=self.req('POST','settings',self.values)
            self.assertEqual(code,200,data)
            self.assertEqual(self.req('GET','settings')[1]['values']['rffmpeg_bin'],'ffmpeg')
            config=self.req('GET','config')[1]
            self.assertEqual(config['effective_rffmpeg'],self.values)
            self.assertEqual(config['engine']['rffmpeg_bin'],'')
        self.assertEqual(self.req('POST','rffmpeg/check',{'argv':[]})[0],400)
        self.state.config=replace(self.config,api_token='secret')
        self.assertEqual(self.req('POST','rffmpeg/check',{})[0],401)

    def test_creation_freezes_remote_and_rejects_concurrent_change(self):
        self.state.runtime.save(self.values)
        body={'engine':'rffmpeg','input':{'root':'media','path':'movie.mp4'},'output':{'root':'out','path':'frozen.mp4'},'spec':{'dimensions':{'crop_mode':'none'}}}
        with patch('cutecat.server.admit'):
            code,data=self.req('POST','jobs',body)
        self.assertEqual(code,200,data)
        self.assertEqual(self.store.get_job(data['id']).execution['remote_config'],self.values)
        def change(*args,**kwargs):self.state.runtime.save({**self.values,'rffmpeg_bin':'newffmpeg'})
        body['output']['path']='race.mp4'
        with patch('cutecat.server.admit',side_effect=change):
            self.assertEqual(self.req('POST','jobs',body)[0],409)
        self.assertEqual(len(self.store.list_jobs()),1)

    def test_check_configuration_race_is_not_reported_passed(self):
        self.state.runtime.save(self.values)
        def change(*args):
            self.state.runtime.save({**self.values,'rffmpeg_bin':'other'})
            return {'status':'passed'}
        with patch.object(self.state.remote_check,'run',side_effect=change):
            self.assertEqual(self.req('POST','rffmpeg/check',{})[0],409)


if __name__=='__main__':unittest.main()
