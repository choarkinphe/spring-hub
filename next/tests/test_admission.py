"""Preset snapshots and server/worker admission regressions (interface mock)."""
import json
import sqlite3
import subprocess
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cutecat.admission import AdmissionError, validate_encoders
from cutecat.engine import HandBrakeEngine
from cutecat.presets import PresetError, resolve_job_preset, validate_preset_document
from cutecat.server import AppState, build_server
from cutecat.spec import TranscodeSpec, build_engine_args
from cutecat.store import Store, _SCHEMA
from cutecat.worker import Worker
from helpers import TempEnv
from test_api import _request


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.config = self.env.config()
        self.engine = HandBrakeEngine(self.config.engine)
        self.store = Store(self.config.database)
        self.caps = self.engine.probe()

    def tearDown(self):
        self.store.close()
        self.env.cleanup()

    def test_works_and_aliases(self):
        for video in ('x264', 'x265', 'av1'):
            spec = TranscodeSpec.from_dict({'video': {'encoder': video}, 'audio': {'tracks': [{'encoder': 'flac'}]}})
            validate_encoders(spec, self.caps)

    def test_rejects_blocked_absent_unknown_and_missing(self):
        spec = TranscodeSpec.from_dict({})
        for verdict, status in [('blocked', 409), ('absent', 409), ('unknown', 503), (None, 503)]:
            caps = replace(self.caps, video_encoder_probe={'x264': verdict} if verdict else None)
            with self.assertRaises(AdmissionError) as raised:
                validate_encoders(spec, caps)
            self.assertEqual(raised.exception.status, status)
        with self.assertRaises(AdmissionError) as raised:
            validate_encoders(spec, replace(self.caps, available=False))
        self.assertEqual(raised.exception.status, 503)

    def test_audio_unknown_and_missing(self):
        spec = TranscodeSpec.from_dict({'audio': {'tracks': [{'encoder': 'opus'}]}})
        for caps, status in [(replace(self.caps, audio_encoders_known=False), 503),
                             (replace(self.caps, audio_encoders=['av_aac']), 409)]:
            with self.assertRaises(AdmissionError) as raised:
                validate_encoders(spec, caps)
            self.assertEqual(raised.exception.status, status)

    def test_preset_encoder_not_dataclass_defaults_is_checked(self):
        doc = {'PresetList': [{'VideoEncoder': 'nvenc_h265', 'AudioList': []}]}
        spec = TranscodeSpec.from_dict({})
        with self.assertRaises(AdmissionError):
            validate_encoders(spec, self.caps, preset=doc, overrides={})
        validate_encoders(spec, self.caps, preset=doc, overrides={'video': {'encoder': 'x264'}})

    def test_preset_audio_and_explicit_empty_tracks(self):
        doc = {'PresetList': [{'VideoEncoder': 'x264', 'AudioList': [{'AudioEncoder': 'bogus'}]}]}
        spec = TranscodeSpec.from_dict({'audio': {'tracks': []}})
        with self.assertRaises(AdmissionError):
            validate_encoders(spec, self.caps, preset=doc, overrides={})
        validate_encoders(spec, self.caps, preset=doc, overrides={'audio': {'tracks': []}})
        self.assertIn('none', build_engine_args(spec, overrides={'audio': {'tracks': []}}))

    def test_sparse_preset_does_not_emit_defaults(self):
        spec = TranscodeSpec.from_dict({'preset': 'P'})
        self.assertEqual(build_engine_args(spec, overrides={'preset': 'P'}), ['--preset', 'P', '--title', '1'])

    def test_negative_gain_and_explicit_disabling(self):
        raw = {'preset': 'P', 'audio': {'tracks': [{'gain': -5}]}, 'video': {'two_pass': False, 'turbo': False},
               'filters': {'denoise': 'off', 'grayscale': False}}
        args = build_engine_args(TranscodeSpec.from_dict(raw), overrides=raw)
        self.assertEqual(args[args.index('--gain') + 1], '-5')
        for flag in ['--no-multi-pass', '--no-turbo', '--no-hqdn3d', '--no-nlmeans', '--no-grayscale']:
            self.assertIn(flag, args)

    def test_nested_presets_preserve_version(self):
        doc = {'VersionMajor': 72, 'PresetList': [{'Folder': True, 'PresetName': 'Folder',
                                                'ChildrenArray': [{'PresetName': 'Leaf'}]}]}
        preset = validate_preset_document(doc)[0]
        self.assertEqual(preset.category, 'Folder')
        self.assertEqual(preset.raw['VersionMajor'], 72)
        self.assertEqual(preset.raw['PresetList'][0]['PresetName'], 'Leaf')

    def test_disabled_duplicate_and_bad_names(self):
        for entries in [[{'PresetName': 'P', 'PresetDisabled': True}],
                        [{'PresetName': 'P'}, {'PresetName': 'P'}], [{'PresetName': 'P\n'}]]:
            with self.assertRaises(PresetError):
                validate_preset_document({'PresetList': entries})

    def test_import_identity_stable_and_root_scoped(self):
        first = self.store.upsert_preset(name='P', source='imported', root='media', file='p.json', doc={'a': 1})
        again = self.store.upsert_preset(name='P', source='imported', root='media', file='p.json', doc={'a': 2})
        second = self.store.upsert_preset(name='P', source='imported', root='out', file='p.json')
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.get_preset(first)['doc'], {'a': 2})

    def test_name_only_unknown_and_mismatched_id_rejected(self):
        pid = self.store.upsert_preset(name='Local', source='imported', root='media', file='p.json',
                                     doc={'PresetList': [{'PresetName': 'Local'}]})
        for identity, name in [(None, 'Local'), (pid, 'Wrong'), ('unknown-uuid', None), ('custom', 'Local')]:
            with self.assertRaises(PresetError):
                resolve_job_preset(self.store, self.engine, identity, name)

    def test_imported_same_name_as_official_uses_own_document(self):
        pid = self.store.upsert_preset(name='General/Fast 1080p30', source='imported', root='media', file='p.json',
                                     doc={'PresetList': [{'PresetName': 'General/Fast 1080p30', 'VideoEncoder': 'x265'}]})
        _, _, imported = resolve_job_preset(self.store, self.engine, pid, 'General/Fast 1080p30')
        _, _, official = resolve_job_preset(self.store, self.engine, 'General/Fast 1080p30', None)
        self.assertEqual(imported['PresetList'][0]['VideoEncoder'], 'x265')
        self.assertEqual(official['PresetList'][0]['VideoEncoder'], 'x264')

    def test_snapshot_survives_store_reopen_and_reimport(self):
        raw = {'preset': 'P'}
        doc = self.engine.export_preset('P', {'PresetList': [{'PresetName': 'P', 'VideoEncoder': 'x265'}]})
        job = self.store.create_job(input_root='media', input_path='movie.mp4', output_root='out', output_path='out.mp4',
                                   preset_id='imported-id', preset_name='P', container='auto',
                                   spec=TranscodeSpec.from_dict(raw).to_dict(), args=[], execution={'preset': doc, 'overrides': raw})
        self.store.close()
        self.store = Store(self.config.database)
        worker = Worker(self.config, self.store, self.engine)
        with patch.object(self.engine, 'run_encode', wraps=self.engine.run_encode) as run:
            worker._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, 'succeeded')
        args = run.call_args.kwargs['args']
        self.assertIn('--preset-import-file', args)
        self.assertIn('__cute_cat_job__', args)
        self.assertNotIn('-e', args)
        self.assertFalse(Path(args[args.index('--preset-import-file') + 1]).exists())

    def test_worker_refresh_rejects_changed_readiness_before_encode(self):
        job = self.store.create_job(input_root='media', input_path='movie.mp4', output_root='out', output_path='out.mp4',
                                   preset_id='custom', preset_name=None, container='auto', spec={}, args=[])
        with patch.object(self.engine, 'probe', return_value=replace(self.caps, video_encoder_probe={'x264': 'blocked'})) as probe, \
             patch.object(self.engine, 'run_encode') as run:
            Worker(self.config, self.store, self.engine)._run_job(job.id)
        probe.assert_called_once_with(refresh=True)
        run.assert_not_called()
        self.assertEqual(self.store.get_job(job.id).status, 'failed')
        self.assertFalse((self.env.out / 'out.mp4').exists())

    def test_probe_timeout_is_unknown_not_absent(self):
        with patch('cutecat.engine._run', side_effect=subprocess.TimeoutExpired('engine', 1)):
            self.assertEqual(self.engine._instantiate_encoder(self.env.mock, 'input', str(self.env.tmp), 'x264'), 'unknown')


class PresetApiTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.config = self.env.config()
        self.store = Store(self.config.database)
        self.engine = HandBrakeEngine(self.config.engine)
        self.state = AppState(self.config, self.store, self.engine)
        self.server = build_server(self.state)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.env.cleanup()

    def submit(self, spec=None, preset_id=None):
        body = {'input': {'root': 'media', 'path': 'movie.mp4'}, 'output': {'root': 'out', 'path': 'result.mp4'}, 'spec': spec or {}}
        if preset_id is not None:
            body['preset_id'] = preset_id
        return _request(self.port, 'POST', '/api/v1/jobs', body)

    def test_blocked_absent_unknown_do_not_insert_job(self):
        caps = self.engine.probe()
        for verdict, expected in [('blocked', 409), ('absent', 409), ('unknown', 503)]:
            with patch.object(self.engine, 'probe', return_value=replace(caps, video_encoder_probe={'x264': verdict})):
                status, result = self.submit()
            self.assertEqual(status, expected, result)
            self.assertEqual(self.store.list_jobs(), [])

    def test_official_snapshot_and_sparse_args(self):
        status, job = self.submit({'preset': 'General/Fast 1080p30'})
        self.assertEqual(status, 200, job)
        self.assertNotIn('-e', job['args'])
        self.assertNotIn('--subtitle', job['args'])
        self.assertEqual(self.store.get_job(job['id']).execution['preset']['PresetList'][0]['VideoEncoder'], 'x264')

    def test_imported_uuid_deleted_source_and_immutable_snapshot(self):
        path = self.env.media / 'preset [1] with spaces.json'
        path.write_text(json.dumps({'PresetList': [{'PresetName': 'Local', 'VideoEncoder': 'x265'}]}))
        status, result = _request(self.port, 'POST', '/api/v1/presets/import', {'root': 'media', 'path': path.name})
        self.assertEqual(status, 200, result)
        pid = result['imported'][0]['id']
        path.unlink()
        status, job = self.submit({'preset': 'Local'}, pid)
        self.assertEqual(status, 200, job)
        self.store.upsert_preset(name='Local', source='imported', root='media', file=path.name,
                                 doc={'PresetList': [{'PresetName': 'Local', 'VideoEncoder': 'nvenc_h265'}]})
        self.assertEqual(self.store.get_job(job['id']).execution['preset']['PresetList'][0]['VideoEncoder'], 'x265')
        Worker(self.config, self.store, self.engine)._run_job(job['id'])
        self.assertEqual(self.store.get_job(job['id']).status, 'succeeded')

    def test_bad_identity_is_400_and_not_inserted(self):
        for pid in ['missing', 42, 'custom']:
            status, result = self.submit({'preset': 'Missing'}, pid)
            self.assertEqual(status, 400, result)
        self.assertEqual(self.store.list_jobs(), [])


class MigrationTests(unittest.TestCase):
    def test_v1_migration_preserves_preset_id_and_job(self):
        env = TempEnv()
        try:
            conn = sqlite3.connect(env.config().database)
            conn.executescript(_SCHEMA)
            conn.execute("INSERT INTO presets(id,name,source,root,file,doc_json,created_at) VALUES('old','P','imported','media','p.json','{}','now')")
            conn.execute("INSERT INTO jobs(id,input_root,input_path,output_root,output_path,preset_id,status,spec_json,created_at) VALUES('job','media','movie.mp4','out','x.mp4','custom','queued','{}','now')")
            conn.commit()
            conn.close()
            store = Store(env.config().database)
            self.assertEqual(store.get_preset('old')['name'], 'P')
            self.assertIsNone(store.get_job('job').execution)
            self.assertEqual(store.get_job('job').status, 'queued')
            store.close()
        finally:
            env.cleanup()


if __name__ == '__main__':
    unittest.main()
