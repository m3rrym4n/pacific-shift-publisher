import tempfile
import unittest
from pathlib import Path

from assemble_episode import assemble_episode_for_run
from dashboard import build_dashboard_view_model
from pipeline_logging import StructuredPipelineLogger
from pipeline_retry import can_retry_run
from pipeline_state import PipelineStateStore


TRACKLIST = "Tracklist\n\n0:00:00 Artist One - First Track\n0:03:24 Artist Two - Second Track"


class AssembleEpisodeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.store = PipelineStateStore(self.db_path)
        self.events = StructuredPipelineLogger(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_assemble_episode_succeeds_and_persists_payload(self):
        run = self._ready_run()

        assembled = assemble_episode_for_run(run["run_id"], self.store)

        payload = assembled["assembled_episode_payload"]
        self.assertEqual(self._step(assembled)["status"], "success")
        self.assertEqual(payload["title"], "Storm Surge 20260627")
        self.assertEqual(payload["audio_url"], "https://azuracast.example/show.mp3")
        self.assertIn(TRACKLIST, payload["description"])
        self.assertIn("Tracklist", payload["description"])

    def test_missing_recording_reference_fails_clearly(self):
        run = self._ready_run(recording_reference=None)

        assembled = assemble_episode_for_run(run["run_id"], self.store)

        self.assertEqual(self._step(assembled)["status"], "failed")
        self.assertIn("recording_reference", self._step(assembled)["message"])
        self.assertIsNone(assembled["assembled_episode_payload"])
        self.assertTrue(can_retry_run(assembled))

    def test_missing_tracklist_fails_clearly(self):
        run = self._ready_run(tracklist=None)

        assembled = assemble_episode_for_run(run["run_id"], self.store)

        self.assertEqual(self._step(assembled)["status"], "failed")
        self.assertIn("tracklist content", self._step(assembled)["message"])

    def test_missing_session_window_identifies_exact_fields(self):
        run = self._ready_run(started_at=None, ended_at=None)

        assembled = assemble_episode_for_run(run["run_id"], self.store)

        self.assertEqual(self._step(assembled)["status"], "failed")
        self.assertIn("started_at", self._step(assembled)["message"])
        self.assertIn("ended_at", self._step(assembled)["message"])

    def test_dashboard_renders_assemble_episode_success(self):
        run = self._ready_run()
        assemble_episode_for_run(run["run_id"], self.store)

        view_model = build_dashboard_view_model(self.store, self.events)
        card = next(card for card in view_model["cards"] if card["step_key"] == "assemble_episode")

        self.assertEqual(card["label"], "Assemble Podcast Episode")
        self.assertEqual(card["status_text"], "Success")
        self.assertEqual(card["status_class"], "success")

    def _ready_run(
        self,
        *,
        recording_reference="https://azuracast.example/show.mp3",
        tracklist=TRACKLIST,
        started_at="2026-06-27T22:00:00+00:00",
        ended_at="2026-06-27T23:00:00+00:00",
    ):
        run = self.store.create_run(
            station="Storm Surge",
            show_name="Storm Surge",
            session_id=f"assemble-{len(self.store.get_recent_runs())}",
            recording_reference=recording_reference,
        )
        if started_at:
            run = self.store.mark_stream_start(
                session_id=run["session_id"],
                started_at=started_at,
                station="Storm Surge",
                show_name="Storm Surge",
            )
        if ended_at:
            run = self.store.mark_stream_end(run_id=run["run_id"], ended_at=ended_at)
        run = self.store.update_step_status(
            run["run_id"],
            "acquire_mp3",
            "success",
            message="MP3 ready.",
        )
        if tracklist:
            run = self.store.update_step_status(
                run["run_id"],
                "acquire_tracklist",
                "success",
                message="Tracklist acquired.",
                error_details={"track_count_filtered": 2, "tracklist": tracklist},
            )
        return run

    def _step(self, run):
        return next(step for step in run["steps"] if step["step_key"] == "assemble_episode")


if __name__ == "__main__":
    unittest.main()
