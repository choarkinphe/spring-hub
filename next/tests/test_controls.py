"""Task control, runtime configuration and naming safety regressions."""
import json
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from cutecat.store import Store
from cutecat.runtime import RuntimeSettings, output_name
from cutecat.engine import HandBrakeEngine
from cutecat.worker import Worker
from helpers import TempEnv


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.config = self.env.config()
        self.store = Store(self.config.database)

    def tearDown(self):
        self.store.close()
        self.env.cleanup()

    def job(self, name="x.mp4", auto_start=True):
        return self.store.create_job(input_root="media", input_path="movie.mp4", output_root="out", output_path=name,
            preset_id="custom", preset_name=None, container="mp4", spec={}, args=[], auto_start=auto_start)

    def wait(self, job, status, timeout=12):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            current = self.store.get_job(job.id)
            if current.status == status:
                return current
            time.sleep(.03)
        self.fail(f"expected {status}, got {current.status}: {current.error}")

    def test_waiting_pause_start_delete(self):
        job = self.job(auto_start=False)
        self.assertEqual(job.status, "waiting")
        self.assertTrue(self.store.control_job(job.id, "pause"))
        self.assertIsNone(self.store.claim_next_queued())
        self.assertTrue(self.store.control_job(job.id, "start"))
        self.assertEqual(self.store.claim_next_queued().id, job.id)
        self.assertFalse(self.store.delete_job(job.id))
        self.store.set_status(job.id, "failed")
        self.assertFalse(self.store.control_job(job.id, "start"))
        self.store.finish_execution(job.id)
        self.assertTrue(self.store.control_job(job.id, "start"))
        self.assertTrue(self.store.delete_job(job.id))
        self.assertIsNone(self.store.get_job(job.id))
        self.assertTrue((self.env.media / "movie.mp4").exists())

    def test_paused_active_cancel_and_restart_recovery(self):
        job = self.job()
        self.store.claim_next_queued()
        self.store.control_job(job.id, "pause")
        self.store.acknowledge_pause(job.id, True)
        self.assertEqual(self.store.get_job(job.id).paused_from, "probing")
        self.assertFalse(self.store.delete_job(job.id))
        self.assertTrue(self.store.request_cancel(job.id))
        self.assertTrue(self.store.cancel_requested(job.id))
        self.assertEqual(self.store.recover_interrupted(), 1)
        self.assertEqual(self.store.get_job(job.id).status, "interrupted")

    def test_finalizing_pause_blocks_publication(self):
        job = self.job()
        self.store.claim_next_queued()
        self.store.set_status(job.id, "finalizing")
        self.store.control_job(job.id, "pause")
        published = []
        self.assertFalse(self.store.complete_job(job.id, lambda: published.append(True)))
        self.assertFalse(published)

    def test_runtime_persistence_and_capacity_decrease(self):
        runtime = RuntimeSettings(self.config, self.store)
        runtime.save({"max_concurrent_jobs": 2, "auto_start": False})
        self.job("a.mp4"); self.job("b.mp4"); self.job("c.mp4")
        self.assertIsNotNone(runtime.claim())
        self.assertIsNotNone(runtime.claim())
        runtime.save({"max_concurrent_jobs": 1})
        self.assertIsNone(runtime.claim())
        runtime.release()
        self.assertIsNone(runtime.claim())
        runtime.release()
        self.assertIsNotNone(runtime.claim())
        self.assertFalse(RuntimeSettings(self.config, self.store).read()["auto_start"])
        for bad in ({"max_concurrent_jobs": 0}, {"max_concurrent_jobs": True}, {"auto_start":"yes"}, {"secret":"x"}):
            with self.assertRaises(ValueError): runtime.save(bad)

    def test_template_identity_and_default_deletion(self):
        template = self.store.save_template({"name":"Same", "spec":{}, "form_spec":{}})
        another = self.store.save_template({"name":"Same", "spec":{}, "form_spec":{}})
        self.assertNotEqual(template["id"], another["id"])
        runtime = RuntimeSettings(self.config, self.store)
        runtime.save({"default_task_template_id": template["id"]})
        self.store.delete_template(template["id"])
        self.assertIsNone(runtime.read()["default_task_template_id"])

    def test_naming_is_root_relative_and_sanitizes_components(self):
        self.assertEqual(output_name("converted/{source}-{encoder}.{ext}", "folder/movie.avi", "x264", None, "mkv"), "converted/movie-x264.mkv")
        self.assertNotIn("/outside", output_name("{preset}.{ext}", "x", "x264", "../../outside", "mp4"))
        for bad in ("../{source}.mp4", "/abs.mp4", "C:/x.mp4", "{secret}.mp4", "{source!r}.mp4"):
            with self.assertRaises((ValueError, __import__("cutecat.pathsafe", fromlist=["PathSafetyError"]).PathSafetyError)):
                output_name(bad, "movie.avi", "x264", None, "mp4")

    def test_process_pauses_resumes_and_preserves_output(self):
        config = replace(self.config, engine=replace(self.config.engine, extra_args=("--mock-slow",)))
        engine = HandBrakeEngine(config.engine)
        worker = Worker(config, self.store, engine)
        job = self.job()
        worker.start()
        try:
            self.wait(job, "running")
            self.assertTrue(self.store.control_job(job.id, "pause"))
            current = self.wait(job, "paused")
            progress = current.progress
            time.sleep(.7)
            self.assertEqual(self.store.get_job(job.id).progress, progress)
            self.assertEqual(worker.runtime.active, 1)
            self.assertTrue(self.store.control_job(job.id, "start"))
            self.wait(job, "succeeded", 15)
            self.assertTrue((self.env.out / "x.mp4").exists())
        finally:
            worker.stop()

    def test_pause_time_does_not_count_toward_timeout(self):
        config = replace(self.config, engine=replace(self.config.engine, extra_args=("--mock-slow",), job_timeout_seconds=6))
        worker = Worker(config, self.store, HandBrakeEngine(config.engine))
        job = self.job()
        worker.start()
        try:
            self.wait(job, "running")
            self.store.control_job(job.id, "pause")
            self.wait(job, "paused")
            time.sleep(6.2)
            self.assertEqual(self.store.get_job(job.id).status, "paused")
            self.store.control_job(job.id, "start")
            self.wait(job, "succeeded", 15)
        finally:
            worker.stop()

    def test_stop_terminates_paused_process(self):
        config = replace(self.config, engine=replace(self.config.engine, extra_args=("--mock-slow",)))
        worker = Worker(config, self.store, HandBrakeEngine(config.engine))
        job = self.job()
        worker.start()
        self.wait(job, "running")
        self.store.control_job(job.id, "pause")
        self.wait(job, "paused")
        worker.stop()
        self.assertEqual(self.store.get_job(job.id).status, "canceled")
        self.assertEqual(worker.runtime.active, 0)

    def test_paused_process_cancel_and_stop_exit(self):
        config = replace(self.config, engine=replace(self.config.engine, extra_args=("--mock-slow",)))
        worker = Worker(config, self.store, HandBrakeEngine(config.engine))
        job = self.job()
        worker.start()
        try:
            self.wait(job, "running")
            self.store.control_job(job.id, "pause")
            self.wait(job, "paused")
            self.store.request_cancel(job.id)
            self.wait(job, "canceled")
            self.assertFalse((self.env.out / "x.mp4").exists())
        finally:
            worker.stop()


if __name__ == "__main__":
    unittest.main()
