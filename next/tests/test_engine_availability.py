"""Engine selector prerequisites must not execute programs or contact remote hosts."""
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch
from helpers import TempEnv
from test_api import _request
from cutecat.engine import HandBrakeEngine
from cutecat.server import AppState, build_server
from cutecat.store import Store


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.env=TempEnv();self.config=self.env.config();self.store=Store(self.config.database)
        self.state=AppState(self.config,self.store,HandBrakeEngine(self.config.engine))
        self.server=build_server(self.state);self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.thread.join();self.server.server_close();self.store.close();self.env.cleanup()

    def report(self):
        code,data=_request(self.port,'GET','/api/v1/engines/availability')
        self.assertEqual(code,200,data)
        return {e['id']:e for e in data['engines']}

    def test_missing_and_unconfigured_no_execution(self):
        with patch('subprocess.run',side_effect=AssertionError('must not execute')):
            report=self.report()
        self.assertTrue(report['handbrake']['selectable'])
        self.assertFalse(report['ffmpeg']['selectable'])
        self.assertEqual(report['rffmpeg']['status'],'unconfigured')
        self.assertEqual(self.store.list_jobs(),[])

    def test_ffprobe_dependency(self):
        engine=self.state.backends.get('ffmpeg')
        with patch.object(engine,'binary_path',return_value='ffmpeg'),patch.object(engine,'ffprobe_path',return_value=None):
            report=self.report()
        self.assertFalse(report['ffmpeg']['selectable'])
        self.assertIn('ffprobe',report['ffmpeg']['reason'])

    def test_remote_shared_directory_and_auth(self):
        self.state.runtime.save({'rffmpeg_bin':self.env.mock,'rffprobe_bin':self.env.mock,'remote_probe_dir':str(self.env.out)})
        with patch('subprocess.run',side_effect=AssertionError('must not execute')):
            self.assertTrue(self.report()['rffmpeg']['selectable'])
            self.env.out.rmdir()
            self.assertFalse(self.report()['rffmpeg']['selectable'])
        self.state.config=replace(self.config,api_token='token')
        self.assertEqual(_request(self.port,'GET','/api/v1/engines/availability')[0],401)


if __name__=='__main__':unittest.main()
