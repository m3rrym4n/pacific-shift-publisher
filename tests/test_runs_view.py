import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from pipeline_state import PipelineStateStore
from runs_view import build_recent_runs_view_model


class RecentRunsViewModelTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.store = PipelineStateStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_no_runs_view_model_has_empty_state(self):
        view_model = build_recent_runs_view_model(self.store)

        self.assertFalse(view_model["has_runs"])
        self.assertEqual(view_model["empty_message"], "No pipeline runs yet.")
        self.assertEqual(view_model["rows"], [])

    def test_recent_runs_are_newest_first(self):
        older = self.store.create_run(
            run_id="older-run",
            show_name="Older Show",
            station="Pacific Shift",
        )
        newer = self.store.create_run(
            run_id="newer-run",
            show_name="Newer Show",
            station="Pacific Shift",
        )
        self._set_updated_at(older["run_id"], "2026-06-24T10:00:00+00:00")
        self._set_updated_at(newer["run_id"], "2026-06-24T11:00:00+00:00")

        view_model = build_recent_runs_view_model(self.store)

        self.assertEqual([row["run_id"] for row in view_model["rows"]], ["newer-run", "older-run"])

    def test_failed_run_identifies_failed_step(self):
        run = self.store.create_run(run_id="failed-run", show_name="Failure Show")
        self.store.mark_step_failed(
            run["run_id"],
            "acquire_tracklist",
            message="Tracklist acquisition failed.",
        )

        row = build_recent_runs_view_model(self.store)["rows"][0]

        self.assertEqual(row["overall_status_text"], "Failed")
        self.assertEqual(row["overall_status_class"], "danger")
        self.assertEqual(row["failed_step"], "Acquire Tracklist")

    def test_in_progress_run_is_distinguishable(self):
        run = self.store.create_run(run_id="progress-run", show_name="Progress Show")
        self.store.update_step_status(
            run["run_id"],
            "acquire_mp3",
            "in_progress",
            message="Downloading MP3.",
        )

        row = build_recent_runs_view_model(self.store)["rows"][0]
        acquire_mp3 = self._step(row, "acquire_mp3")

        self.assertEqual(row["overall_status_text"], "Waiting")
        self.assertEqual(acquire_mp3["status_text"], "In Progress")
        self.assertEqual(acquire_mp3["status_class"], "warning")
        self.assertTrue(row["can_cancel"])

    def test_castopod_draft_reference_and_step_state_are_exposed(self):
        run = self.store.create_run(run_id="draft-run", show_name="Draft Show")
        self.store.mark_step_success(
            run["run_id"],
            "post_castopod_draft",
            message="Castopod draft created.",
        )
        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET castopod_episode_id = ?,
                    castopod_episode_url = ?,
                    overall_status = ?
                WHERE run_id = ?
                """,
                ("episode-42", "https://castopod.example/episodes/42", "success", run["run_id"]),
            )

        row = build_recent_runs_view_model(self.store)["rows"][0]

        self.assertEqual(row["castopod_episode_id"], "episode-42")
        self.assertEqual(row["castopod_episode_url"], "https://castopod.example/episodes/42")
        self.assertTrue(row["steps"])
        self.assertIn("Success", row["step_summary"])
        self.assertFalse(row["can_cancel"])

    def test_terminal_failed_and_skipped_runs_do_not_allow_cancel(self):
        failed = self.store.create_run(run_id="failed-terminal")
        skipped = self.store.create_run(run_id="skipped-terminal")
        self.store.mark_step_failed(failed["run_id"], "acquire_tracklist", message="Failed terminal.")
        self.store.cancel_run(skipped["run_id"])

        rows = {row["run_id"]: row for row in build_recent_runs_view_model(self.store)["rows"]}

        self.assertFalse(rows["failed-terminal"]["can_cancel"])
        self.assertFalse(rows["skipped-terminal"]["can_cancel"])
        self.assertFalse(rows["failed-terminal"]["can_retry"])
        self.assertFalse(rows["skipped-terminal"]["can_retry"])

    def test_successful_runs_do_not_allow_retry(self):
        run = self.store.mark_stream_start(
            session_id="success-run",
            started_at="2026-06-25T22:00:00+00:00",
        )
        run = self.store.mark_stream_end(
            run_id=run["run_id"],
            ended_at="2026-06-25T23:00:00+00:00",
        )
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE pipeline_runs SET overall_status = ? WHERE run_id = ?",
                ("success", run["run_id"]),
            )

        row = build_recent_runs_view_model(self.store)["rows"][0]

        self.assertFalse(row["can_retry"])

    def _set_updated_at(self, run_id, updated_at):
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE pipeline_runs SET updated_at = ? WHERE run_id = ?",
                (updated_at, run_id),
            )

    def _step(self, row, step_key):
        return next(step for step in row["steps"] if step["step_key"] == step_key)


class RecentRunsRouteTest(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    @patch("app.build_recent_runs_view_model")
    def test_runs_route_renders_empty_state(self, recent_runs):
        recent_runs.return_value = {
            "has_runs": False,
            "empty_message": "No pipeline runs yet.",
            "empty_detail": "Recent automation attempts will appear here after runs are created.",
            "rows": [],
        }

        response = self.client.get("/runs")

        self.assertEqual(response.status_code, 200)
        self.assertIn("No pipeline runs yet.", response.get_data(as_text=True))

    @patch("app.build_recent_runs_view_model")
    def test_runs_route_renders_recent_runs_table(self, recent_runs):
        recent_runs.return_value = {
            "has_runs": True,
            "rows": [
                {
                    "run_id": "run-123456",
                    "short_run_id": "run-1234",
                    "display_name": "Storm Surge / Pacific Shift",
                    "started_at": "2026-06-24T10:00:00+00:00",
                    "ended_at": "2026-06-24T11:00:00+00:00",
                    "overall_status_text": "Failed",
                    "overall_status_class": "danger",
                    "failed_step": "Acquire Tracklist",
                    "castopod_episode_id": "episode-42",
                    "castopod_episode_url": "https://castopod.example/episodes/42",
                    "updated_at": "2026-06-24T11:02:00+00:00",
                    "step_summary": "1 Failed, 5 Pending",
                    "can_cancel": True,
                    "can_retry": True,
                    "steps": [
                        {
                            "step_key": "acquire_tracklist",
                            "label": "Acquire Tracklist",
                            "status_text": "Failed",
                            "status_class": "danger",
                            "message": "Tracklist acquisition failed.",
                        }
                    ],
                }
            ],
        }

        response = self.client.get("/runs")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Run ID", body)
        self.assertIn("Storm Surge / Pacific Shift", body)
        self.assertIn("Acquire Tracklist", body)
        self.assertIn("episode-42", body)
        self.assertIn("Tracklist acquisition failed.", body)
        self.assertIn('action="/runs/run-123456/cancel"', body)
        self.assertIn("Cancel Run", body)
        self.assertIn('action="/runs/run-123456/retry"', body)
        self.assertIn("Retry", body)


if __name__ == "__main__":
    unittest.main()
