import tempfile
import unittest
from pathlib import Path

from dashboard import build_dashboard_view_model
from pipeline_constants import PIPELINE_STEP_KEYS
from pipeline_logging import StructuredPipelineLogger
from pipeline_state import PipelineStateStore


class DashboardViewModelTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.store = PipelineStateStore(self.db_path)
        self.events = StructuredPipelineLogger(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_no_run_view_model_has_empty_state_and_ordered_cards(self):
        view_model = build_dashboard_view_model(self.store, self.events)

        self.assertFalse(view_model["has_run"])
        self.assertEqual(view_model["empty_message"], "No pipeline runs yet.")
        self.assertEqual([card["step_key"] for card in view_model["cards"]], list(PIPELINE_STEP_KEYS))
        self.assertTrue(all(card["status_text"] == "Pending" for card in view_model["cards"]))
        self.assertTrue(all(card["status_class"] for card in view_model["cards"]))

    def test_latest_run_view_model_includes_run_summary_and_cards(self):
        run = self.store.create_run(
            station="Pacific Shift Radio",
            show_name="Storm Surge",
            streamer="Merry Man",
            session_id="dashboard-session",
        )
        self.store.mark_stream_start(session_id="dashboard-session")
        self.store.update_step_status(
            run["run_id"],
            "acquire_mp3",
            "in_progress",
            message="Downloading MP3.",
        )

        view_model = build_dashboard_view_model(self.store, self.events)

        self.assertTrue(view_model["has_run"])
        self.assertEqual(view_model["run"]["show_name"], "Storm Surge")
        self.assertEqual(view_model["run"]["station"], "Pacific Shift Radio")
        self.assertEqual(view_model["run"]["streamer"], "Merry Man")
        self.assertEqual([card["step_key"] for card in view_model["cards"]], list(PIPELINE_STEP_KEYS))
        acquire_mp3 = self._card(view_model, "acquire_mp3")
        self.assertEqual(acquire_mp3["status_text"], "In Progress")
        self.assertEqual(acquire_mp3["status_class"], "warning")
        self.assertEqual(acquire_mp3["message"], "Downloading MP3.")

    def test_failed_step_is_distinguishable(self):
        run = self.store.create_run(session_id="failed-session")
        self.store.mark_step_failed(
            run["run_id"],
            "acquire_tracklist",
            message="Tracklist fetch failed.",
        )

        view_model = build_dashboard_view_model(self.store, self.events)
        failed_card = self._card(view_model, "acquire_tracklist")
        pending_card = self._card(view_model, "assemble_episode")

        self.assertEqual(failed_card["status_text"], "Failed")
        self.assertEqual(failed_card["status_class"], "danger")
        self.assertNotEqual(failed_card["status_class"], pending_card["status_class"])

    def test_castopod_draft_reference_appears_when_present(self):
        run = self.store.create_run(session_id="draft-session")
        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET castopod_episode_id = ?,
                    castopod_episode_url = ?
                WHERE run_id = ?
                """,
                ("42", "https://castopod.example/episodes/42", run["run_id"]),
            )

        view_model = build_dashboard_view_model(self.store, self.events)

        self.assertTrue(view_model["draft"]["available"])
        self.assertEqual(view_model["draft"]["episode_id"], "42")
        self.assertEqual(view_model["draft"]["episode_url"], "https://castopod.example/episodes/42")

    def _card(self, view_model, step_key):
        return next(card for card in view_model["cards"] if card["step_key"] == step_key)


if __name__ == "__main__":
    unittest.main()
