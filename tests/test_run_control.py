import os
import tempfile
import unittest
from pathlib import Path

from app import app
from pipeline_logging import StructuredPipelineLogger
from pipeline_state import PipelineStateStore


class RunControlRouteTest(unittest.TestCase):
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

    def test_dashboard_shows_cancel_control_for_open_run(self):
        self.store.mark_stream_start(session_id="open-run")

        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('action="/runs/current/cancel"', body)
        self.assertIn("Cancel open run", body)

    def test_cancel_current_run_route_closes_open_run_and_logs_event(self):
        self.store.mark_stream_start(session_id="open-run")

        response = self.client.post("/runs/current/cancel")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/dashboard")
        run = self.store.get_run_by_session_id("open-run")
        self.assertEqual(run["overall_status"], "skipped")
        self.assertIsNotNone(run["ended_at"])
        event_names = [event["event_name"] for event in self.events.find_events(run_id=run["run_id"])]
        self.assertIn("run_cancelled", event_names)

    def test_cancel_current_run_route_is_safe_without_open_run(self):
        response = self.client.post("/runs/current/cancel")

        self.assertEqual(response.status_code, 303)
        events = self.events.find_events(event_name="run_cancelled")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "skipped")
        self.assertIn("No open run", events[0]["message"])


if __name__ == "__main__":
    unittest.main()
