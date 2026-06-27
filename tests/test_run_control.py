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

    def test_cancel_specific_run_route_targets_selected_non_terminal_run(self):
        selected = self.store.create_run(run_id="selected-run", session_id="selected-run")
        other = self.store.create_run(run_id="other-run", session_id="other-run")

        response = self.client.post("/runs/selected-run/cancel")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/runs")
        selected_run = self.store.get_run(selected["run_id"])
        other_run = self.store.get_run(other["run_id"])
        self.assertEqual(selected_run["overall_status"], "skipped")
        self.assertNotEqual(other_run["overall_status"], "skipped")
        events = self.events.find_events(run_id=selected["run_id"], event_name="run_cancelled")
        self.assertEqual(len(events), 1)

    def test_cancel_specific_terminal_run_is_safe_noop(self):
        run = self.store.create_run(run_id="terminal-run", session_id="terminal-run")
        self.store.mark_step_success(run["run_id"], "post_castopod_draft")
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE pipeline_runs SET overall_status = ? WHERE run_id = ?",
                ("success", run["run_id"]),
            )

        response = self.client.post("/runs/terminal-run/cancel")

        self.assertEqual(response.status_code, 303)
        terminal = self.store.get_run(run["run_id"])
        self.assertEqual(terminal["overall_status"], "success")
        events = self.events.find_events(run_id=run["run_id"], event_name="run_cancelled")
        self.assertEqual(events[-1]["status"], "skipped")

    def test_cancel_current_run_route_is_safe_without_open_run(self):
        response = self.client.post("/runs/current/cancel")

        self.assertEqual(response.status_code, 303)
        events = self.events.find_events(event_name="run_cancelled")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "skipped")
        self.assertIn("No open run", events[0]["message"])

    def test_runs_page_shows_delete_action_for_each_run(self):
        first = self.store.create_run(run_id="delete-first", session_id="delete-first")
        second = self.store.create_run(run_id="delete-second", session_id="delete-second")

        response = self.client.get("/runs")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'action="/runs/{first["run_id"]}/delete"', body)
        self.assertIn(f'action="/runs/{second["run_id"]}/delete"', body)
        self.assertIn("This cannot be undone.", body)

    def test_delete_run_removes_selected_run_steps_and_events_only(self):
        selected = self.store.create_run(run_id="delete-selected", session_id="delete-selected")
        other = self.store.create_run(run_id="delete-other", session_id="delete-other")

        response = self.client.post(f'/runs/{selected["run_id"]}/delete')

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/runs")
        self.assertIsNone(self.store.get_run(selected["run_id"]))
        self.assertIsNotNone(self.store.get_run(other["run_id"]))
        self.assertEqual(self.events.find_events(run_id=selected["run_id"]), [])
        with self.store.connect() as connection:
            step_count = connection.execute(
                "SELECT COUNT(*) FROM pipeline_steps WHERE run_id = ?",
                (selected["run_id"],),
            ).fetchone()[0]
        self.assertEqual(step_count, 0)

    def test_delete_unknown_run_is_safe(self):
        response = self.client.post("/runs/not-found/delete", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Pipeline run was not found.", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
