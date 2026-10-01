"""SQLite store tests: queue ordering, status transitions, logs, presets."""

from __future__ import annotations

import unittest

from cutecat.store import Store

from helpers import TempEnv


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.store = Store(self.env.config().database)

    def tearDown(self):
        self.store.close()
        self.env.cleanup()

    def _job(self, path="movie.mp4", output="out.mp4"):
        return self.store.create_job(
            input_root="media", input_path=path, output_root="out",
            output_path=output, preset_id="custom", preset_name=None,
            container="mp4", spec={"version": 1}, args=["-e", "x264"],
        )

    def test_create_and_get(self):
        job = self._job()
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.spec, {"version": 1})
        self.assertEqual(job.args, ["-e", "x264"])
        self.assertIsNotNone(self.store.get_job(job.id))

    def test_claim_fifo(self):
        first = self._job(output="a.mp4")
        self._job(output="b.mp4")
        claimed = self.store.claim_next_queued()
        self.assertEqual(claimed.id, first.id)
        self.assertEqual(claimed.status, "probing")

    def test_claim_empty_returns_none(self):
        self.assertIsNone(self.store.claim_next_queued())

    def test_status_transition_sets_finished(self):
        job = self._job()
        self.store.set_status(job.id, "succeeded")
        reloaded = self.store.get_job(job.id)
        self.assertEqual(reloaded.status, "succeeded")
        self.assertIsNotNone(reloaded.finished_at)
        self.assertEqual(reloaded.progress, 1.0)

    def test_cancel_queued(self):
        job = self._job()
        self.assertTrue(self.store.request_cancel(job.id))
        self.assertEqual(self.store.get_job(job.id).status, "canceled")

    def test_cancel_terminal_returns_false(self):
        job = self._job()
        self.store.set_status(job.id, "succeeded")
        self.assertFalse(self.store.request_cancel(job.id))

    def test_cancel_running_sets_flag(self):
        job = self._job()
        self.store.claim_next_queued()
        self.store.set_status(job.id, "running")
        self.assertTrue(self.store.request_cancel(job.id))
        self.assertTrue(self.store.cancel_requested(job.id))

    def test_update_progress(self):
        job = self._job()
        self.store.update_job(job.id, progress=0.5, speed="42.5", eta_seconds=10)
        reloaded = self.store.get_job(job.id)
        self.assertAlmostEqual(reloaded.progress, 0.5)
        self.assertEqual(reloaded.speed, "42.5")

    def test_logs_roundtrip(self):
        job = self._job()
        self.store.add_log(job.id, "info", "hello")
        self.store.add_log(job.id, "error", "boom")
        logs = self.store.get_logs(job.id)
        self.assertEqual([l["message"] for l in logs], ["hello", "boom"])

    def test_recover_interrupted(self):
        job = self._job()
        self.store.claim_next_queued()
        self.store.set_status(job.id, "running")
        count = self.store.recover_interrupted()
        self.assertEqual(count, 1)
        self.assertEqual(self.store.get_job(job.id).status, "interrupted")

    def test_preset_upsert_and_list(self):
        self.store.upsert_preset(name="P", source="imported", file="a.json", doc={"x": 1})
        self.store.upsert_preset(name="P", source="imported", file="a.json", doc={"x": 2})
        presets = self.store.list_presets(source="imported")
        self.assertEqual(len(presets), 1)
        self.assertEqual(self.store.get_preset(presets[0]["id"])["doc"], {"x": 2})

    def test_counts(self):
        self._job(output="a.mp4")
        self._job(output="b.mp4")
        self.assertEqual(self.store.counts().get("queued"), 2)

    def test_persistence_across_reopen(self):
        job = self._job()
        path = self.env.config().database
        self.store.close()
        reopened = Store(path)
        try:
            self.assertIsNotNone(reopened.get_job(job.id))
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
