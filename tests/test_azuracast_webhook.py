import os
import tempfile
import unittest
from pathlib import Path

from app import app
from pipeline_logging import StructuredPipelineLogger
from pipeline_state import PipelineStateStore


class AzuraCastWebhookTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.store = PipelineStateStore(self.db_path)
        self.events = StructuredPipelineLogger(self.db_path)

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        self.temp_dir.cleanup()

    def test_streamer_start_records_session_start(self):
        response = self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event": "streamer_start",
                "station": "Storm Surge",
                "streamer": "SeaCapn",
                "timestamp": "2026-06-24T22:00:00Z",
                "session_id": "storm-surge-20260624",
            },
        )

        self.assertEqual(response.status_code, 200)
        run = self.store.get_run_by_session_id("storm-surge-20260624")
        self.assertIsNotNone(run)
        self.assertEqual(run["station"], "Storm Surge")
        self.assertEqual(run["streamer"], "SeaCapn")
        self.assertEqual(run["started_at"], "2026-06-24T22:00:00+00:00")
        self.assertEqual(run["overall_status"], "in_progress")
        self.assertEqual(self._step(run, "stream_start")["status"], "success")

    def test_streamer_stop_correlates_to_started_session(self):
        self._post_start()

        response = self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event": "streamer_stop",
                "station": "Storm Surge",
                "streamer": "SeaCapn",
                "timestamp": "2026-06-24T23:00:00Z",
                "session_id": "storm-surge-20260624",
            },
        )

        self.assertEqual(response.status_code, 200)
        run = self.store.get_run_by_session_id("storm-surge-20260624")
        self.assertEqual(run["started_at"], "2026-06-24T22:00:00+00:00")
        self.assertEqual(run["ended_at"], "2026-06-24T23:00:00+00:00")
        self.assertEqual(run["current_step"], "stream_end")
        self.assertEqual(self._step(run, "stream_end")["status"], "success")

    def test_start_and_stop_without_session_correlate_by_active_station_streamer(self):
        start = self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event_type": "streamer_started",
                "station_name": "Storm Surge",
                "streamer_name": "SeaCapn",
                "timestamp": "2026-06-24T22:00:00Z",
            },
        )
        stop = self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event_type": "streamer_stopped",
                "station_name": "Storm Surge",
                "streamer_name": "SeaCapn",
                "timestamp": "2026-06-24T23:00:00Z",
            },
        )

        self.assertEqual(start.status_code, 200)
        self.assertEqual(stop.status_code, 200)
        self.assertEqual(start.get_json()["run_id"], stop.get_json()["run_id"])
        run = self.store.get_run(stop.get_json()["run_id"])
        self.assertEqual(run["ended_at"], "2026-06-24T23:00:00+00:00")

    def test_duplicate_start_does_not_create_unbounded_runs(self):
        first = self._post_start()
        second = self._post_start()

        runs = self.store.get_recent_runs()

        self.assertEqual(first.get_json()["run_id"], second.get_json()["run_id"])
        self.assertEqual(len(runs), 1)
        event_names = [event["event_name"] for event in self.events.find_events()]
        self.assertIn("azuracast_webhook_duplicate", event_names)

    def test_duplicate_stop_preserves_completed_window(self):
        self._post_start()
        self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event": "streamer_stop",
                "station": "Storm Surge",
                "streamer": "SeaCapn",
                "timestamp": "2026-06-24T23:00:00Z",
                "session_id": "storm-surge-20260624",
            },
        )
        self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event": "streamer_stop",
                "station": "Storm Surge",
                "streamer": "SeaCapn",
                "timestamp": "2026-06-25T01:00:00Z",
                "session_id": "storm-surge-20260624",
            },
        )

        run = self.store.get_run_by_session_id("storm-surge-20260624")

        self.assertEqual(run["ended_at"], "2026-06-24T23:00:00+00:00")
        event_names = [event["event_name"] for event in self.events.find_events(run_id=run["run_id"])]
        self.assertIn("azuracast_webhook_duplicate", event_names)

    def test_stop_without_matching_start_is_handled_safely(self):
        response = self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event": "streamer_stop",
                "station": "Storm Surge",
                "streamer": "SeaCapn",
                "timestamp": "2026-06-24T23:00:00Z",
                "session_id": "orphan-session",
            },
        )

        self.assertEqual(response.status_code, 200)
        run = self.store.get_run_by_session_id("orphan-session")
        self.assertEqual(run["ended_at"], "2026-06-24T23:00:00+00:00")
        self.assertIsNone(run["started_at"])
        event_names = [event["event_name"] for event in self.events.find_events(run_id=run["run_id"])]
        self.assertIn("azuracast_webhook_out_of_order", event_names)

    def test_invalid_json_and_unknown_event_are_safe(self):
        invalid = self.client.post(
            "/api/webhooks/azuracast",
            data="not-json",
            content_type="application/json",
        )
        unknown = self.client.post(
            "/api/webhooks/azuracast",
            json={"event": "song_changed", "station": "Storm Surge"},
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(self.store.get_recent_runs(), [])

    def test_missing_timestamp_falls_back_to_receipt_time(self):
        response = self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event": "streamer_start",
                "station": "Storm Surge",
                "streamer": "SeaCapn",
                "session_id": "fallback-time",
            },
        )

        self.assertEqual(response.status_code, 200)
        run = self.store.get_run_by_session_id("fallback-time")
        self.assertIsNotNone(run["started_at"])

    def test_structured_events_are_emitted_without_raw_secret_payload(self):
        response = self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event": "streamer_start",
                "station": "token=super-secret",
                "streamer": "SeaCapn",
                "timestamp": "2026-06-24T22:00:00Z",
                "session_id": "secret-session",
                "authorization": "Bearer hidden",
            },
        )

        self.assertEqual(response.status_code, 200)
        events = self.events.find_events(session_id="secret-session")
        serialized = str(events)
        self.assertIn("azuracast_webhook_received", serialized)
        self.assertIn("azuracast_stream_start_recorded", serialized)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("Bearer hidden", serialized)

    def _post_start(self):
        return self.client.post(
            "/api/webhooks/azuracast",
            json={
                "event": "streamer_start",
                "station": "Storm Surge",
                "streamer": "SeaCapn",
                "timestamp": "2026-06-24T22:00:00Z",
                "session_id": "storm-surge-20260624",
            },
        )

    def _step(self, run, step_key):
        return next(step for step in run["steps"] if step["step_key"] == step_key)


if __name__ == "__main__":
    unittest.main()
