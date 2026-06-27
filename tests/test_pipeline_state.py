import tempfile
import unittest
from pathlib import Path

from pipeline_state import PIPELINE_STATUSES, PIPELINE_STEP_KEYS, PipelineStateStore


class PipelineStateStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.store = PipelineStateStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_run_initializes_required_steps(self):
        run = self.store.create_run(
            station="Pacific Shift",
            show_name="Storm Surge",
            streamer="DJ Test",
            session_id="session-001",
            recording_reference="recording-001.mp3",
        )

        self.assertEqual(run["session_id"], "session-001")
        self.assertEqual(run["overall_status"], "waiting")
        self.assertEqual(run["current_step"], "stream_start")
        self.assertEqual([step["step_key"] for step in run["steps"]], list(PIPELINE_STEP_KEYS))
        self.assertTrue(all(step["status"] == "pending" for step in run["steps"]))

    def test_run_id_is_stable_and_lookup_by_session_id_works(self):
        run = self.store.create_run(session_id="stable-session")
        fetched = self.store.get_run_by_session_id("stable-session")

        self.assertEqual(fetched["run_id"], run["run_id"])
        self.assertEqual(fetched["session_id"], "stable-session")

    def test_mark_stream_start_creates_run_and_marks_first_step_success(self):
        run = self.store.mark_stream_start(
            session_id="stream-session",
            station="Pacific Shift",
            show_name="Storm Surge",
        )

        self.assertEqual(run["overall_status"], "in_progress")
        self.assertEqual(run["current_step"], "stream_start")
        self.assertIsNotNone(run["started_at"])
        self.assertEqual(run["steps"][0]["status"], "success")
        self.assertEqual(run["steps"][0]["step_key"], "stream_start")

    def test_mark_stream_end_updates_same_run(self):
        started = self.store.mark_stream_start(session_id="same-session")
        ended = self.store.mark_stream_end(session_id="same-session")

        self.assertEqual(ended["run_id"], started["run_id"])
        self.assertIsNotNone(ended["ended_at"])
        stream_end_step = self._step(ended, "stream_end")
        self.assertEqual(stream_end_step["status"], "success")

    def test_update_step_to_in_progress_and_success(self):
        run = self.store.create_run()
        updated = self.store.update_step_status(
            run["run_id"],
            "acquire_mp3",
            "in_progress",
            message="Downloading recording.",
        )

        acquire_mp3 = self._step(updated, "acquire_mp3")
        self.assertEqual(acquire_mp3["status"], "in_progress")
        self.assertEqual(acquire_mp3["message"], "Downloading recording.")
        self.assertEqual(updated["current_step"], "acquire_mp3")

        succeeded = self.store.mark_step_success(
            run["run_id"],
            "acquire_mp3",
            message="Recording acquired.",
        )
        acquire_mp3 = self._step(succeeded, "acquire_mp3")
        self.assertEqual(acquire_mp3["status"], "success")
        self.assertEqual(acquire_mp3["message"], "Recording acquired.")

    def test_update_step_to_failed_preserves_sanitized_error_context(self):
        run = self.store.create_run()
        failed = self.store.mark_step_failed(
            run["run_id"],
            "acquire_tracklist",
            message="Tracklist acquisition failed token=abc123",
            error_details="authorization Bearer super-secret-token",
        )

        step = self._step(failed, "acquire_tracklist")
        self.assertEqual(failed["overall_status"], "failed")
        self.assertEqual(failed["current_step"], "acquire_tracklist")
        self.assertIn("[redacted]", failed["error_summary"])
        self.assertIn("[redacted]", step["error_details"])
        self.assertNotIn("abc123", failed["error_summary"])
        self.assertNotIn("super-secret-token", step["error_details"])

    def test_structured_step_details_are_logged_and_compacted_for_step_state(self):
        run = self.store.create_run()
        updated = self.store.update_step_status(
            run["run_id"],
            "acquire_tracklist",
            "success",
            message="Tracklist acquired.",
            error_details={
                "history_url_used": "https://azuracast.example/history.json",
                "track_count_filtered": 4,
            },
        )

        step = self._step(updated, "acquire_tracklist")
        self.assertIn("track_count_filtered", step["error_details"])

    def test_cancel_current_run_closes_open_run_and_skips_remaining_steps(self):
        started = self.store.mark_stream_start(session_id="cancel-session")

        result = self.store.cancel_current_run()
        cancelled = result["run"]

        self.assertTrue(result["cancelled"])
        self.assertEqual(cancelled["run_id"], started["run_id"])
        self.assertEqual(cancelled["overall_status"], "skipped")
        self.assertIsNotNone(cancelled["ended_at"])
        self.assertIsNone(self.store.find_active_run())
        self.assertEqual(self._step(cancelled, "stream_start")["status"], "success")
        self.assertEqual(self._step(cancelled, "stream_end")["status"], "skipped")

    def test_cancel_current_run_is_safe_without_open_run(self):
        result = self.store.cancel_current_run()

        self.assertFalse(result["cancelled"])
        self.assertIsNone(result["run"])

    def test_get_latest_run_returns_most_recently_updated_run(self):
        first = self.store.create_run(session_id="first")
        second = self.store.create_run(session_id="second")
        self.store.mark_step_success(first["run_id"], "acquire_mp3")

        latest = self.store.get_latest_run()

        self.assertEqual(latest["run_id"], first["run_id"])
        self.assertNotEqual(latest["run_id"], second["run_id"])

    def test_serialized_shape_contains_ui_ready_run_and_step_fields(self):
        run = self.store.create_run()

        self.assertEqual(
            set(run.keys()),
            {
                "run_id",
                "station",
                "show_name",
                "streamer",
                "started_at",
                "ended_at",
                "overall_status",
                "current_step",
                "session_id",
                "broadcast_id",
                "recording_reference",
                "tracklist_status",
                "castopod_episode_id",
                "castopod_episode_url",
                "error_summary",
                "created_at",
                "updated_at",
                "steps",
            },
        )
        self.assertEqual(
            set(run["steps"][0].keys()),
            {
                "step_key",
                "status",
                "started_at",
                "ended_at",
                "duration_ms",
                "message",
                "error_details",
                "retry_count",
            },
        )

    def test_rejects_unknown_step_or_status(self):
        run = self.store.create_run()

        with self.assertRaises(ValueError):
            self.store.update_step_status(run["run_id"], "unknown_step", "pending")

        with self.assertRaises(ValueError):
            self.store.update_step_status(run["run_id"], "stream_start", "unknown_status")

    def test_required_statuses_are_represented(self):
        self.assertEqual(
            set(PIPELINE_STATUSES),
            {"pending", "waiting", "waiting_transcode", "in_progress", "success", "failed", "skipped"},
        )

    def test_waiting_transcode_run_can_be_found_with_recording_reference(self):
        run = self.store.create_run(session_id="transcode")
        self.store.set_recording_reference(run["run_id"], "broadcast-123")
        self.store.update_step_status(run["run_id"], "acquire_mp3", "waiting_transcode")

        matches = self.store.get_runs_by_step_status("acquire_mp3", "waiting_transcode")

        self.assertEqual([item["run_id"] for item in matches], [run["run_id"]])
        self.assertEqual(matches[0]["recording_reference"], "broadcast-123")

    def _step(self, run, step_key):
        return next(step for step in run["steps"] if step["step_key"] == step_key)


if __name__ == "__main__":
    unittest.main()
