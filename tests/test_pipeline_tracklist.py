import tempfile
import unittest
from pathlib import Path

from pipeline_logging import StructuredPipelineLogger
from pipeline_state import PipelineStateStore
from pipeline_tracklist import acquire_tracklist_for_run


class PipelineTracklistTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.store = PipelineStateStore(self.db_path)
        self.events = StructuredPipelineLogger(self.db_path)
        self.run = self.store.mark_stream_start(
            session_id="tracklist-session",
            started_at="2026-06-24T22:00:00+00:00",
        )
        self.run = self.store.mark_stream_end(
            run_id=self.run["run_id"],
            ended_at="2026-06-24T23:00:00+00:00",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_acquire_tracklist_success_updates_step_and_logs_counts(self):
        def generator(run_id, store=None):
            return {
                "ok": True,
                "endpoint_url": "https://azuracast.example/api/nowplaying_static/storm.json",
                "track_count_total": 12,
                "track_count_filtered": 5,
                "tracks": [{"display": "A - B"}] * 5,
                "tracklist": "Tracklist\n\n01. A - B",
            }

        updated = acquire_tracklist_for_run(self.run["run_id"], self.store, generator=generator)

        step = self._step(updated, "acquire_tracklist")
        self.assertEqual(step["status"], "success")
        self.assertEqual(updated["tracklist_status"], "success")
        self.assertIn("5 tracks", step["message"])
        events = self.events.find_events(run_id=self.run["run_id"], step_key="acquire_tracklist")
        self.assertEqual(events[-1]["event_name"], "acquire_tracklist.succeeded")
        self.assertEqual(events[-1]["details"]["history_url_used"], "https://azuracast.example/api/nowplaying_static/storm.json")
        self.assertEqual(events[-1]["details"]["track_count_total"], 12)
        self.assertEqual(events[-1]["details"]["track_count_filtered"], 5)
        self.assertEqual(len(events[-1]["details"]["tracks"]), 5)
        self.assertEqual(events[-1]["details"]["tracks"][0]["display"], "A - B")

    def test_acquire_tracklist_skips_when_no_tracks_match(self):
        def generator(run_id, store=None):
            return {
                "ok": True,
                "endpoint_url": "https://azuracast.example/history.json",
                "track_count_total": 12,
                "track_count_filtered": 0,
                "tracks": [],
                "tracklist": "Tracklist\n\nNo AzuraCast track history was found for this session window.",
            }

        updated = acquire_tracklist_for_run(self.run["run_id"], self.store, generator=generator)

        step = self._step(updated, "acquire_tracklist")
        self.assertEqual(step["status"], "skipped")
        events = self.events.find_events(run_id=self.run["run_id"], step_key="acquire_tracklist")
        self.assertEqual(events[-1]["event_name"], "acquire_tracklist.skipped")
        self.assertEqual(events[-1]["details"]["track_count_filtered"], 0)
        self.assertIn("skip_reason", events[-1]["details"])

    def test_acquire_tracklist_failure_records_safe_reason(self):
        def generator(run_id, store=None):
            return {
                "ok": False,
                "endpoint_url": "https://azuracast.example/history.json",
                "error": "AzuraCast history request failed: Timeout token=hidden",
                "tracks": [],
            }

        updated = acquire_tracklist_for_run(self.run["run_id"], self.store, generator=generator)

        step = self._step(updated, "acquire_tracklist")
        self.assertEqual(step["status"], "failed")
        self.assertEqual(updated["overall_status"], "failed")
        events = self.events.find_events(run_id=self.run["run_id"], step_key="acquire_tracklist")
        self.assertEqual(events[-1]["event_name"], "acquire_tracklist.failed")
        serialized = str(events[-1])
        self.assertIn("failure_reason", serialized)
        self.assertNotIn("hidden", serialized)
        self.assertIn("[redacted]", serialized)

    def test_acquire_tracklist_skips_when_config_is_not_ready(self):
        def generator(run_id, store=None):
            return {
                "ok": False,
                "error": "AzuraCast integration is disabled.",
                "tracks": [],
            }

        updated = acquire_tracklist_for_run(self.run["run_id"], self.store, generator=generator)

        step = self._step(updated, "acquire_tracklist")
        self.assertEqual(step["status"], "skipped")
        events = self.events.find_events(run_id=self.run["run_id"], step_key="acquire_tracklist")
        self.assertEqual(events[-1]["event_name"], "acquire_tracklist.skipped")
        self.assertIn("skip_reason", events[-1]["details"])

    def _step(self, run, step_key):
        return next(step for step in run["steps"] if step["step_key"] == step_key)


if __name__ == "__main__":
    unittest.main()
