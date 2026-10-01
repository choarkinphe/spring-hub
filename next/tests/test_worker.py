"""Worker integration tests: full queue -> scan -> encode -> status flow.

These run against the **mock** HandBrakeCLI. They prove the wiring (queueing,
scan parsing, progress updates, logging, cancellation, overwrite refusal) but
not real-engine behaviour.
"""

from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace

from cutecat.engine import HandBrakeEngine
from cutecat.store import Store
from cutecat.worker import Worker

from helpers import TempEnv


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.config = self.env.config()
        self.store = Store(self.config.database)
        self.engine = HandBrakeEngine(self.config.engine)

    def tearDown(self):
        self.store.close()
        self.env.cleanup()

    def _job(self, *, output="converted/out.mp4", spec=None):
        return self.store.create_job(
            input_root="media", input_path="movie.mp4", output_root="out",
            output_path=output, preset_id="custom", preset_name=None,
            container="mp4", spec=spec or {"version": 1}, args=["-e", "x264"],
        )

    def test_successful_encode(self):
        job = self._job()
        worker = Worker(self.config, self.store, self.engine)
        worker._run_job(job.id)
        reloaded = self.store.get_job(job.id)
        self.assertEqual(reloaded.status, "succeeded")
        self.assertEqual(reloaded.progress, 1.0)
        self.assertEqual(reloaded.title_count, 2)  # from mock scan
        self.assertTrue((self.env.out / "converted" / "out.mp4").exists())
        logs = self.store.get_logs(job.id)
        self.assertTrue(any("encode finished" in l["message"] for l in logs))

    def test_failed_encode_cleans_partial(self):
        # Force failure via a mock flag injected as a fixed extra arg.
        config = self.config
        config = replace(config, engine=replace(config.engine, extra_args=("--mock-fail",)))
        engine = HandBrakeEngine(config.engine)
        job = self._job(output="converted/fail.mp4")
        worker = Worker(config, self.store, engine)
        worker._run_job(job.id)
        reloaded = self.store.get_job(job.id)
        self.assertEqual(reloaded.status, "failed")
        self.assertIsNotNone(reloaded.error)
        self.assertFalse((self.env.out / "converted" / "fail.mp4").exists())

    def test_refuse_overwrite(self):
        (self.env.out / "converted").mkdir(parents=True, exist_ok=True)
        (self.env.out / "converted" / "out.mp4").write_bytes(b"existing")
        job = self._job()
        worker = Worker(self.config, self.store, self.engine)
        worker._run_job(job.id)
        reloaded = self.store.get_job(job.id)
        self.assertEqual(reloaded.status, "failed")
        self.assertIn("already exists", reloaded.error)

    def test_path_rejected_after_submit(self):
        # A job whose input vanished should fail cleanly, not crash.
        job = self._job()
        (self.env.media / "movie.mp4").unlink()
        worker = Worker(self.config, self.store, self.engine)
        worker._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "failed")

    def test_cancellation(self):
        config = replace(self.config, engine=replace(self.config.engine, extra_args=("--mock-slow",)))
        engine = HandBrakeEngine(config.engine)
        job = self._job(output="converted/slow.mp4")
        worker = Worker(config, self.store, engine)

        thread = threading.Thread(target=worker._run_job, args=(job.id,))
        thread.start()
        # Wait until the worker flips the job to running, then cancel.
        deadline = time.time() + 5
        while time.time() < deadline:
            current = self.store.get_job(job.id)
            if current and current.status == "running":
                break
            time.sleep(0.05)
        self.store.request_cancel(job.id)
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        reloaded = self.store.get_job(job.id)
        self.assertEqual(reloaded.status, "canceled")
        self.assertFalse((self.env.out / "converted" / "slow.mp4").exists())

    def test_loop_processes_queued_job(self):
        job = self._job()
        worker = Worker(self.config, self.store, self.engine)
        worker.start()
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if self.store.get_job(job.id).status in ("succeeded", "failed"):
                    break
                time.sleep(0.1)
        finally:
            worker.stop()
        self.assertEqual(self.store.get_job(job.id).status, "succeeded")


if __name__ == "__main__":
    unittest.main()
