"""Worker integration tests: full queue -> scan -> encode -> status flow.

These run against the **mock** HandBrakeCLI. They prove the wiring (queueing,
scan parsing, progress updates, logging, cancellation, overwrite refusal) but
not real-engine behaviour.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
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

    def test_empty_output_is_not_published(self):
        job = self._job()
        worker = Worker(self.config, self.store, self.engine)
        def encode(**kwargs):
            Path(kwargs["output_path"]).write_bytes(b"")
        with patch.object(self.engine, "run_encode", side_effect=encode):
            worker._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "failed")
        self.assertFalse((self.env.out / "converted/out.mp4").exists())
        self.assertFalse(list(self.env.out.glob("converted/.cute-cat-*")))

    def test_invalid_output_scan_preserves_existing_file(self):
        target = self.env.out / "converted/out.mp4"
        target.parent.mkdir()
        target.write_bytes(b"existing")
        config = replace(self.config, engine=replace(self.config.engine, refuse_overwrite=False))
        job = self._job()
        with patch.object(self.engine, "scan", return_value={"titles": []}):
            Worker(config, self.store, self.engine)._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "failed")
        self.assertEqual(target.read_bytes(), b"existing")

    def test_output_appearing_during_encode_is_not_overwritten(self):
        target = self.env.out / "converted/out.mp4"
        job = self._job()
        def encode(**kwargs):
            Path(kwargs["output_path"]).write_bytes(b"new")
            target.write_bytes(b"external")
        with patch.object(self.engine, "run_encode", side_effect=encode):
            Worker(self.config, self.store, self.engine)._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "failed")
        self.assertEqual(target.read_bytes(), b"external")

    def test_same_output_jobs_are_serialized(self):
        jobs = [self._job(), self._job()]
        worker = Worker(self.config, self.store, self.engine)
        threads = [threading.Thread(target=worker._run_job, args=(job.id,)) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(self.store.get_job(job.id).status for job in jobs), ["failed", "succeeded"])

    def test_cancel_during_finalization_does_not_publish(self):
        job = self._job()
        original_scan = self.engine.scan
        def scan(path):
            result = original_scan(path)
            if ".cute-cat-" in path:
                self.store.request_cancel(job.id)
            return result
        with patch.object(self.engine, "scan", side_effect=scan):
            Worker(self.config, self.store, self.engine)._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "canceled")
        self.assertFalse((self.env.out / "converted/out.mp4").exists())

    def test_auxiliary_paths_are_absolute_and_rooted(self):
        (self.env.media / "sub.srt").write_text("subtitle")
        (self.env.media / "chapters.csv").write_text("1,Intro\n")
        job = self._job(spec={"subtitles": {"srt_file": "sub.srt"},
                              "chapters": {"mode": "markers", "marker_file": "chapters.csv"}})
        Worker(self.config, self.store, self.engine)._run_job(job.id)
        args = self.store.get_job(job.id).args
        self.assertEqual(args[args.index("--srt-file") + 1], str(self.env.media / "sub.srt"))
        self.assertIn("--markers=" + str(self.env.media / "chapters.csv"), args)
        self.assertEqual(self.store.get_job(job.id).status, "succeeded")

    def test_input_alias_cannot_be_overwritten(self):
        config = replace(self.config, engine=replace(self.config.engine, refuse_overwrite=False))
        job = self.store.create_job(input_root="media", input_path="movie.mp4", output_root="media",
                                    output_path="movie.mp4", preset_id="custom", preset_name=None,
                                    container="mp4", spec={}, args=[])
        Worker(config, self.store, self.engine)._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "failed")
        self.assertEqual((self.env.media / "movie.mp4").read_bytes(), b"FAKE-INPUT\n")

    def test_hardlink_input_alias_is_rejected(self):
        target = self.env.out / "alias.mp4"
        target.hardlink_to(self.env.media / "movie.mp4")
        config = replace(self.config, engine=replace(self.config.engine, refuse_overwrite=False))
        job = self._job(output="alias.mp4")
        Worker(config, self.store, self.engine)._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "failed")
        self.assertEqual(target.read_bytes(), b"FAKE-INPUT\n")

    def test_validated_output_replaces_old_file_only_when_allowed(self):
        target = self.env.out / "converted/out.mp4"
        target.parent.mkdir()
        target.write_bytes(b"existing")
        config = replace(self.config, engine=replace(self.config.engine, refuse_overwrite=False))
        job = self._job()
        Worker(config, self.store, self.engine)._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "succeeded")
        self.assertNotEqual(target.read_bytes(), b"existing")
        self.assertFalse(list(target.parent.glob(".cute-cat-*")))

    def test_missing_mount_marker_prevents_publication(self):
        marker = self.env.out / ".mounted"
        marker.touch()
        roots = tuple(replace(root, mount_marker=".mounted") if root.id == "out" else root
                      for root in self.config.storage_roots)
        config = replace(self.config, storage_roots=roots)
        job = self._job()
        def encode(**kwargs):
            Path(kwargs["output_path"]).write_bytes(b"new")
            marker.unlink()
        with patch.object(self.engine, "run_encode", side_effect=encode):
            Worker(config, self.store, self.engine)._run_job(job.id)
        self.assertEqual(self.store.get_job(job.id).status, "failed")
        self.assertFalse((self.env.out / "converted/out.mp4").exists())

    def test_stop_joins_and_cancels_active_encode(self):
        config = replace(self.config, engine=replace(self.config.engine, extra_args=("--mock-slow",)))
        worker = Worker(config, self.store, HandBrakeEngine(config.engine))
        job = self._job()
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and self.store.get_job(job.id).status != "running":
                time.sleep(0.05)
            self.assertEqual(self.store.get_job(job.id).status, "running")
        finally:
            worker.stop()
        self.assertTrue(all(not thread.is_alive() for thread in worker._threads))
        self.assertEqual(self.store.get_job(job.id).status, "canceled")
        self.assertFalse((self.env.out / "converted/out.mp4").exists())

    def test_timeout_is_failure_not_user_cancel(self):
        config = replace(self.config, engine=replace(self.config.engine,
                          extra_args=("--mock-slow",), job_timeout_seconds=1))
        engine = HandBrakeEngine(config.engine)
        job = self._job()
        Worker(config, self.store, engine)._run_job(job.id)
        saved = self.store.get_job(job.id)
        self.assertEqual(saved.status, "failed")
        self.assertIn("timed out", saved.error)

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
