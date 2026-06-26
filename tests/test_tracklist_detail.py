import os
import tempfile
import unittest
from pathlib import Path

from app import app
from pipeline_state import PipelineStateStore


class TracklistDetailRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.store = PipelineStateStore(self.db_path)

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        self.temp_dir.cleanup()

    def test_dashboard_renders_tracklist_link_with_safe_new_window_pattern(self):
        run = self._create_acquired_tracklist_run()

        response = self.client.get("/dashboard")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(f'href="/runs/{run["run_id"]}/tracklist"', body)
        self.assertIn('target="_blank"', body)
        self.assertIn('rel="noopener noreferrer"', body)
        self.assertIn("2 tracks", body)

    def test_tracklist_detail_route_shows_acquired_rows(self):
        run = self._create_acquired_tracklist_run()

        response = self.client.get(f"/runs/{run['run_id']}/tracklist")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Tracklist for run tracklis", body)
        self.assertIn(run["run_id"], body)
        self.assertIn("2026-06-25T14:53:20+00:00", body)
        self.assertIn("2026-06-25T14:59:30+00:00", body)
        self.assertIn("Tracks", body)
        self.assertIn("2", body)
        self.assertIn("Episode Time", body)
        self.assertIn("0:00:41", body)
        self.assertNotIn("2026-06-25T14:54:01+00:00", body)
        self.assertIn("0:00:41 Example Artist - Example Title", body)
        self.assertIn("--:--:-- Fallback Display", body)
        self.assertIn("Example Artist", body)
        self.assertIn("Example Title", body)
        self.assertIn("--:--:--", body)
        self.assertIn("Unknown artist", body)
        self.assertIn("Fallback Display", body)
        self.assertNotIn("super-secret", body)

    def test_tracklist_detail_route_returns_404_for_unknown_run(self):
        response = self.client.get("/runs/missing-run/tracklist")

        self.assertEqual(response.status_code, 404)
        self.assertIn("Pipeline run was not found.", response.get_data(as_text=True))

    def test_manual_upload_still_renders_required_fields(self):
        response = self.client.get("/manual-upload")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/upload"', body)
        self.assertIn('name="podcast_id"', body)
        self.assertIn('name="save_as_draft"', body)
        self.assertIn('name="title"', body)
        self.assertIn('name="description"', body)
        self.assertIn('name="audio_file"', body)

    def _create_acquired_tracklist_run(self):
        run = self.store.mark_stream_start(
            run_id="tracklist-detail-run",
            session_id="tracklist-detail-run",
            started_at="2026-06-25T14:53:20+00:00",
            station="Storm Surge",
            show_name="Storm Surge",
        )
        run = self.store.mark_stream_end(
            run_id=run["run_id"],
            ended_at="2026-06-25T14:59:30+00:00",
        )
        return self.store.update_step_status(
            run["run_id"],
            "acquire_tracklist",
            "success",
            message="Tracklist acquired with 2 tracks.",
            error_details={
                "track_count_filtered": 2,
                "tracks": [
                    {
                        "played_at": "2026-06-25T14:54:01+00:00",
                        "artist": "Example Artist",
                        "title": "Example Title",
                    },
                    {
                        "played_at": None,
                        "artist": None,
                        "title": None,
                        "display": "Fallback Display",
                        "secret": "super-secret",
                    },
                ],
                "tracklist": "Tracklist\n\n01. Example Artist - Example Title\n02. Fallback Display",
            },
        )


if __name__ == "__main__":
    unittest.main()
