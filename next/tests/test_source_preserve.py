"""HEVC source-policy mapping, admission snapshots and no silent fallback."""
import copy
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch
from helpers import TempEnv
from test_api import _request
from cutecat.builtin_templates import builtin_templates
from cutecat.spec import TranscodeSpec, SpecError, build_engine_args
from cutecat.source_preserve import resolve_source
from cutecat.server import AppState, build_server
from cutecat.engine import HandBrakeEngine
from cutecat.store import Store
from cutecat.worker import Worker


def source():
    return {"titles": [{"Duration": 4.0, "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360,
         "pix_fmt": "yuv420p", "bit_rate": "1234567", "avg_frame_rate": "24000/1001", "sample_aspect_ratio": "1:1"},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2, "bit_rate": "128000"}]}]}


def policy():
    return TranscodeSpec.from_dict(builtin_templates()[-1]["spec"])


class SourceTests(unittest.TestCase):
    def test_mapping_preserves_without_fixed_dimensions_rate_or_audio_encoding(self):
        spec, args, snapshot = resolve_source(policy(), source())
        self.assertEqual(spec.video.encoder, "x265")
        self.assertEqual(spec.video.bitrate_kbps, 1235)
        self.assertEqual(args[args.index("-b:v") + 1], "1235k")
        self.assertEqual(snapshot["framerate"], "24000/1001")
        for flag in ("-vf", "-r", "-crf", "-ar:a:0", "-b:a:0", "-ac:a:0"):
            self.assertNotIn(flag, args)
        self.assertIn("copy", args)
        self.assertIn("hvc1", args)
        self.assertEqual(args[args.index("-map_metadata") + 1], "0")
        self.assertIn("passthrough", args)
        self.assertEqual(spec.dimensions.anamorphic, "auto")
        self.assertIsNone(spec.dimensions.width)

    def test_ten_bit_bps_tag_and_all_audio_subtitle_copy(self):
        scan = source()
        video = scan["titles"][0]["streams"][0]
        video.pop("bit_rate")
        video.update(pix_fmt="yuv420p10le", tags={"BPS": "5000000"}, sample_aspect_ratio="4:3")
        scan["titles"][0]["streams"] += [{"codec_type": "audio", "codec_name": "ac3"}, {"codec_type": "subtitle", "codec_name": "mov_text"}]
        spec, args, snapshot = resolve_source(policy(), scan)
        self.assertEqual(spec.video.encoder, "x265_10bit")
        self.assertEqual(spec.video.bitrate_kbps, 5000)
        self.assertEqual(snapshot["audio_tracks"], 2)
        self.assertEqual(snapshot["subtitle_tracks"], 1)
        self.assertIn("0:a:1", args)
        self.assertIn("0:s", args)
        self.assertNotIn("mov_text", args)

    def test_missing_bitrate_never_uses_container_total_or_rf(self):
        scan = source()
        scan["format"] = {"bit_rate": "9000000"}
        scan["titles"][0]["streams"][0].pop("bit_rate")
        with self.assertRaisesRegex(SpecError, "平均码率"):
            resolve_source(policy(), scan)

    def test_bad_metadata_and_incompatible_streams_fail_closed(self):
        for update in ({"pix_fmt":"yuv444p"}, {"bit_rate":"NaN"}, {"bit_rate":"99999999999"},
                       {"avg_frame_rate":"0/0"}, {"width":0}, {"color_transfer":"smpte2084"},
                       {"side_data_list":[{"rotation":90}]}, {"sample_aspect_ratio":"-1:1"}):
            scan = source();scan["titles"][0]["streams"][0].update(update)
            with self.assertRaises(SpecError):resolve_source(policy(), scan)
        for stream in ({"codec_type":"audio","codec_name":"opus"}, {"codec_type":"subtitle","codec_name":"subrip"},
                       {"codec_type":"attachment"}, {"codec_type":"video"}):
            scan = source();scan["titles"][0]["streams"].append(stream)
            with self.assertRaises(SpecError):resolve_source(policy(), scan)

    def test_policy_validation_and_fingerprint(self):
        raw = policy().to_dict()
        for fragment in ({"source_preserve":"yes"}, {"container":"mkv"}, {"dimensions":{"width":640}},
                         {"video":{"framerate":"30"}}, {"streaming":{"pixel_format":"yuv420p"}}, {"audio":{"tracks":[{"encoder":"aac"}]}}):
            changed = copy.deepcopy(raw)
            for key,value in fragment.items():
                changed[key] = {**changed.get(key,{}),**value} if isinstance(value,dict) else value
            with self.assertRaises(SpecError):TranscodeSpec.from_dict(changed)
        with self.assertRaises(SpecError):build_engine_args(policy())
        _, _, first = resolve_source(policy(), source())
        scan = source();scan["titles"][0]["streams"][0]["width"] = 800
        _, _, second = resolve_source(policy(), scan)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])


class SourceApiTests(unittest.TestCase):
    def setUp(self):
        self.env=TempEnv();self.config=self.env.config();self.store=Store(self.config.database)
        self.state=AppState(self.config,self.store,HandBrakeEngine(self.config.engine))
        self.state.runtime.save({"auto_start":False})
        self.server=build_server(self.state);self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        self.template=builtin_templates()[-1]
        self.body={"engine":"ffmpeg","template_id":self.template["id"],"input":{"root":"media","path":"movie.mp4"},"output":{"root":"out","path":"result.mp4"}}

    def tearDown(self):
        self.server.shutdown();self.thread.join();self.server.server_close();self.store.close();self.env.cleanup()

    def request(self, route, body):
        return _request(self.port, "POST", "/api/v1/"+route, body)

    def test_prevalidation_pending_resolved_and_handbrake_rejection(self):
        raw = {"engine":"ffmpeg","template_id":self.template["id"]}
        code,data=self.request("spec/validate",raw)
        self.assertEqual(code,200,data);self.assertTrue(data["pending_source"]);self.assertEqual(data["args"],[])
        with patch("cutecat.ffmpeg_engine.FFmpegEngine.scan",return_value=source()):
            code,data=self.request("spec/validate",{**raw,"input":self.body["input"]})
        self.assertEqual(code,200,data);self.assertFalse(data["pending_source"])
        self.assertEqual(data["source_snapshot"]["target_bitrate_kbps"],1235)
        self.assertEqual(self.request("spec/validate",{**raw,"engine":"handbrake"})[0],400)
        self.assertEqual(self.store.list_jobs(),[])

    def test_snapshot_worker_change_and_copy_export(self):
        with patch("cutecat.ffmpeg_engine.FFmpegEngine.scan",return_value=source()),patch("cutecat.server.admit"):
            code,data=self.request("jobs",self.body)
        self.assertEqual(code,200,data)
        job=self.store.get_job(data["id"])
        self.assertEqual(job.spec["video"]["bitrate_kbps"],1235)
        self.assertTrue(job.execution["source_policy"]["source_preserve"])
        self.assertIn("passthrough",job.args)
        self.store.control_job(job.id,"start")
        claimed=self.store.claim_next_queued()
        changed=source();changed["titles"][0]["streams"][0]["width"]=800
        worker=Worker(self.config,self.store,self.state.engine,self.state.runtime)
        with patch("cutecat.ffmpeg_engine.FFmpegEngine.scan",return_value=changed):worker._run_job(claimed.id)
        self.assertEqual(self.store.get_job(job.id).status,"failed")
        self.assertIn("变化",self.store.get_job(job.id).error)
        code,bundle=self.request("task-templates/export",{"ids":[self.template["id"]]})
        self.assertEqual(code,200,bundle)
        self.assertTrue(bundle["templates"][0]["template"]["spec"]["source_preserve"])
        self.assertEqual(self.request("task-templates/import-preview",bundle)[0],200)

    def test_no_bitrate_does_not_create_job(self):
        bad=source();bad["titles"][0]["streams"][0].pop("bit_rate")
        with patch("cutecat.ffmpeg_engine.FFmpegEngine.scan",return_value=bad),patch("cutecat.server.admit"):
            code,data=self.request("jobs",self.body)
        self.assertEqual(code,400,data)
        self.assertEqual(self.store.list_jobs(),[])


if __name__ == "__main__":unittest.main()
