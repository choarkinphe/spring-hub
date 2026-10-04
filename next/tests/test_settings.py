"""Expanded settings, portable templates, output allocation and safe cleanup."""
import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from cutecat.runtime import RuntimeSettings, SettingsConflict
from cutecat.outputs import allocate_output, OutputConflict
from cutecat.pathsafe import resolve_request, PathSafetyError
from cutecat.engine import HandBrakeEngine
from cutecat.worker import Worker
from cutecat.templates import TemplateBundles, validate_template
from cutecat.maintenance import Maintenance
from cutecat.store import Store
from helpers import TempEnv


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.config = self.env.config()
        self.store = Store(self.config.database)
        self.runtime = RuntimeSettings(self.config, self.store)
        self.engine = HandBrakeEngine(self.config.engine)
        self.roots = list(self.config.storage_roots)
        self.source = resolve_request(self.roots, "media", "movie.mp4", require_exists=True)

    def tearDown(self):
        self.store.close()
        self.env.cleanup()

    def job(self, name="result.mp4", policy="rename"):
        return self.store.create_allocated_job(
            lambda reserved: allocate_output(self.roots, self.source, "out", name, reserved, policy),
            input_root="media", input_path="movie.mp4", output_root="out", output_path=name,
            preset_id="custom", preset_name=None, container="mp4", spec={}, args=[],
            execution={"preset":None,"overrides":{},"settings":{"job_timeout_seconds":0,"output_collision_policy":policy,"requested_output":name}}, auto_start=False)

    def test_runtime_defaults_revision_and_old_values(self):
        self.store.set_setting("runtime_settings", json.dumps({"auto_start":False}))
        self.assertEqual(self.runtime.read()["output_collision_policy"], "reject")
        report = self.runtime.report()
        self.assertTrue(report["defaults"]["auto_start"])
        self.runtime.save({"expected_revision":report["revision"],"output_collision_policy":"rename"})
        with self.assertRaises(SettingsConflict):
            self.runtime.save({"expected_revision":report["revision"]})
        for bad in ({"output_collision_policy":"overwrite"},{"output_collision_policy":True},{"expected_revision":True}):
            with self.assertRaises(ValueError): self.runtime.save(bad)

    def test_parallel_creation_reserves_distinct_paths(self):
        jobs, errors = [], []
        def create():
            try: jobs.append(self.job())
            except Exception as exc: errors.append(exc)
        threads = [threading.Thread(target=create) for _ in range(5)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(len({j.output_path for j in jobs}), 5)
        self.assertIn("result (4).mp4", {j.output_path for j in jobs})
        with self.assertRaises(OutputConflict): self.job(policy="reject")

    def test_source_protection_and_dangling_link(self):
        with self.assertRaises(PathSafetyError):
            allocate_output(self.roots, self.source, "media", "movie.mp4", set(), "rename")
        (self.env.out / "result.mp4").symlink_to(self.env.out / "missing")
        self.assertEqual(self.job().output_path, "result (1).mp4")
        (self.env.out / "directory.mp4").mkdir()
        self.assertEqual(self.job("directory.mp4").output_path,"directory (1).mp4")

    def test_publication_race_retries_only_existing_destination(self):
        job=self.job(); self.store.control_job(job.id,"start")
        worker=Worker(self.config,self.store,self.engine,self.runtime)
        real_link=os.link
        calls=[]
        def race(src,dst):
            calls.append(str(dst))
            if len(calls)==1:
                from pathlib import Path
                Path(dst).write_bytes(b"RACER")
                raise FileExistsError("claimed externally")
            return real_link(src,dst)
        with patch("cutecat.worker.os.link",side_effect=race):
            worker._run_job(job.id)
        current=self.store.get_job(job.id)
        self.assertEqual(current.status,"succeeded",current.error)
        self.assertEqual(current.output_path,"result (1).mp4")
        self.assertEqual((self.env.out/"result.mp4").read_bytes(),b"RACER")
        self.assertEqual(len(calls),2)

    def test_completed_reservations_are_released_and_retry_safe(self):
        job = self.job()
        self.store.set_status(job.id,"failed")
        self.store.finish_execution(job.id)
        self.assertEqual(self.job().output_path,"result.mp4")

    def test_external_claim_does_not_overwrite_at_publication(self):
        job = self.job()
        self.store.control_job(job.id,"start")
        (self.env.out / "result.mp4").write_bytes(b"EXTERNAL")
        Worker(self.config,self.store,self.engine,self.runtime)._run_job(job.id)
        current = self.store.get_job(job.id)
        self.assertEqual(current.status,"succeeded",current.error)
        self.assertEqual(current.output_path,"result (1).mp4")
        self.assertTrue(current.as_dict()["output_renamed"])
        self.assertEqual((self.env.out / "result.mp4").read_bytes(),b"EXTERNAL")
        self.assertTrue((self.env.out / "result (1).mp4").is_file())

    def test_new_job_rejects_no_clobber_when_existing(self):
        (self.env.out / "result.mp4").write_bytes(b"EXTERNAL")
        with self.assertRaises(OutputConflict): self.job(policy="reject")

    def test_terminal_events_skip_initial_and_repeat_status(self):
        job = self.job()
        cursor = self.store.event_report()["cursor"]
        self.store.set_status(job.id,"failed")
        self.store.set_status(job.id,"failed")
        events = self.store.event_report(cursor)
        self.assertEqual(len(events["events"]),1)
        self.assertFalse(self.store.event_report()["events"])
        self.store.control_job(job.id,"start")
        self.store.set_status(job.id,"failed")
        self.assertEqual(len(self.store.event_report(cursor)["events"]),2)
        self.assertTrue(self.store.event_report(cursor,1)["has_more"])
        self.store.delete_job(job.id)
        self.assertTrue(self.store.event_report(cursor)["events"][0]["deleted"])
        self.assertNotIn("source",self.store.event_report(cursor)["events"][0])

    def test_cleanup_preview_only_terminal_and_no_media(self):
        old = (datetime.now(timezone.utc)-timedelta(days=40)).isoformat()
        job = self.job(); self.store.set_status(job.id,"succeeded")
        self.store.update_job(job.id,finished_at=old)
        self.store.add_log(job.id,"info","test")
        waiting = self.job("waiting.mp4")
        (self.env.out / "result.mp4").write_bytes(b"KEEP")
        maintenance = Maintenance(self.store)
        preview = maintenance.preview({"days":30,"statuses":["succeeded"]})
        self.assertEqual(preview["count"],1)
        late = self.job("late.mp4"); self.store.set_status(late.id,"succeeded"); self.store.update_job(late.id,finished_at=old)
        result = maintenance.cleanup({"token":preview["token"]})
        self.assertEqual(result["deleted"],1)
        self.assertIsNone(self.store.get_job(job.id))
        self.assertIsNotNone(self.store.get_job(waiting.id))
        self.assertIsNotNone(self.store.get_job(late.id))
        self.assertEqual((self.env.out / "result.mp4").read_bytes(),b"KEEP")
        with self.assertRaises(ValueError): maintenance.cleanup({"token":preview["token"]})
        with self.assertRaises(ValueError): maintenance.preview({"statuses":["running"]})

    def test_cleanup_skips_retried_record(self):
        job = self.job(); self.store.set_status(job.id,"failed")
        self.store.update_job(job.id,finished_at=(datetime.now(timezone.utc)-timedelta(days=40)).isoformat())
        maintenance=Maintenance(self.store)
        preview=maintenance.preview({"statuses":["failed"]})
        self.store.control_job(job.id,"start")
        self.assertEqual(maintenance.cleanup({"token":preview["token"]})["deleted"],0)

    def test_template_preset_dependency_is_portable(self):
        payload={"name":"preset template","spec":{"preset":"General/Fast 1080p30"},"preset_id":"General/Fast 1080p30","form_spec":{},"baseline":{},"version":1}
        template=self.store.save_template(payload)
        bundles=TemplateBundles(self.store,self.engine)
        bundle=bundles.export({"ids":[template["id"]]})
        document=bundle["templates"][0]["preset_document"]
        self.assertEqual(document["PresetList"][0]["PresetName"],"General/Fast 1080p30")
        imported=bundles.import_bundle(bundle)["templates"][0]
        self.assertNotEqual(imported["preset_id"],template["preset_id"])
        self.assertIsNotNone(self.store.get_preset(imported["preset_id"]))

    def test_cleanup_rechecks_candidate_timestamp_inside_transaction(self):
        job=self.job(); self.store.set_status(job.id,"failed")
        old=(datetime.now(timezone.utc)-timedelta(days=40)).isoformat()
        self.store.update_job(job.id,finished_at=old)
        candidates=self.store.cleanup_candidates({"failed"},datetime.now(timezone.utc).isoformat())
        self.store.update_job(job.id,finished_at=(datetime.now(timezone.utc)-timedelta(days=39)).isoformat())
        result=self.store.cleanup_jobs(candidates,{"failed"},datetime.now(timezone.utc).isoformat())
        self.assertEqual(result["deleted"],0)
        self.assertIsNotNone(self.store.get_job(job.id))

    def test_execution_timeout_comes_from_snapshot(self):
        job=self.job(); self.runtime.save({"job_timeout_seconds":321})
        self.store.control_job(job.id,"start")
        worker=Worker(self.config,self.store,self.engine,self.runtime)
        with patch.object(worker,"_encode_job",side_effect=RuntimeError("stop")) as encode:
            worker._run_job(job.id)
        self.assertEqual(encode.call_args.args[-1],0)

    def test_template_bundle_roundtrip_and_id_authority(self):
        payload={"name":"test","description":"","spec":{},"form_spec":{},"baseline":None,"version":1}
        template=self.store.save_template(payload)
        bundles=TemplateBundles(self.store,self.engine)
        bundle=bundles.export({"ids":[template["id"]]})
        self.assertNotIn(template["id"],json.dumps(bundle))
        preview=bundles.import_bundle(bundle,preview=True)
        self.assertEqual(preview["count"],1)
        self.assertEqual(len(self.store.list_templates()),1)
        imported=bundles.import_bundle(bundle)["templates"][0]
        self.assertNotEqual(imported["id"],template["id"])
        self.assertEqual(imported["name"],template["name"])
        bad=json.loads(json.dumps(bundle)); bad["templates"].append({"template":{**payload,"args":["evil"]},"preset_document":None})
        with self.assertRaises(ValueError): bundles.import_bundle(bad)
        self.assertEqual(len(self.store.list_templates()),2)
        for field in ("id","args","token"):
            with self.assertRaises(ValueError): validate_template({**payload,field:"evil"})


if __name__ == "__main__":
    unittest.main()
