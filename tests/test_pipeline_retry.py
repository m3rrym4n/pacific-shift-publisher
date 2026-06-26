import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import app
from azuracast_config import AzuraCastConfigStore
from pipeline_logging import StructuredPipelineLogger
from pipeline_retry import can_retry_run, retry_pipeline_run
from pipeline_state import PipelineStateStore
from rss_source import RssSourceStore


class PipelineRetryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        self.original_api_key = os.environ.get("AZURACAST_API_KEY")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        self.store = PipelineStateStore(self.db_path)
        self.events = StructuredPipelineLogger(self.db_path)

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        if self.original_api_key is None:
            os.environ.pop("AZURACAST_API_KEY", None)
        else:
            os.environ["AZURACAST_API_KEY"] = self.original_api_key
        self.temp_dir.cleanup()

    def test_can_retry_failed_completed_session_but_not_success_or_open_run(self):
        failed = self._failed_retryable_run("failed-retry")
        success = self._failed_retryable_run("success-run")
        success = self.store.update_step_status(success["run_id"], "acquire_mp3", "success")
        success = self.store.update_step_status(success["run_id"], "acquire_tracklist", "success")
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE pipeline_runs SET overall_status = ? WHERE run_id = ?",
                ("success", success["run_id"]),
            )
        open_run = self.store.mark_stream_start(session_id="open-run")

        self.assertTrue(can_retry_run(failed))
        self.assertFalse(can_retry_run(self.store.get_run(success["run_id"])))
        self.assertFalse(can_retry_run(open_run))

    def test_retry_success_uses_existing_run_and_does_not_rerun_successful_tracklist(self):
        run = self._failed_retryable_run("retry-success")
        tracklist_runner = Mock()

        result = retry_pipeline_run(
            run["run_id"],
            self.store,
            event_store=self.events,
            mp3_runner=self._mp3_success,
            tracklist_runner=tracklist_runner,
        )

        updated = result["run"]
        self.assertTrue(result["ok"])
        self.assertEqual(updated["run_id"], run["run_id"])
        self.assertEqual(self._step(updated, "acquire_mp3")["status"], "success")
        self.assertEqual(self._step(updated, "acquire_tracklist")["status"], "success")
        self.assertEqual(len(self.store.get_recent_runs()), 1)
        tracklist_runner.assert_not_called()
        event_names = [event["event_name"] for event in self.events.find_events(run_id=run["run_id"])]
        self.assertIn("run_retry.started", event_names)
        self.assertIn("run_retry.succeeded", event_names)

    def test_retry_failure_leaves_run_available_for_another_retry(self):
        run = self._failed_retryable_run("retry-failure")

        result = retry_pipeline_run(
            run["run_id"],
            self.store,
            event_store=self.events,
            mp3_runner=self._mp3_download_failure,
            tracklist_runner=Mock(),
        )

        updated = result["run"]
        self.assertFalse(result["ok"])
        self.assertEqual(updated["overall_status"], "failed")
        self.assertTrue(can_retry_run(updated))
        self.assertIn("MP3 download failed", self._step(updated, "acquire_mp3")["message"])

    def test_retry_missing_azuracast_api_key_uses_mp3_acquisition_path(self):
        run = self._failed_retryable_run("missing-api-key")
        AzuraCastConfigStore(self.db_path).save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_shortcode": "storm_surge",
            }
        )
        RssSourceStore(self.db_path).save_config(
            {
                "enabled": True,
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
            }
        )
        os.environ.pop("AZURACAST_API_KEY", None)

        result = retry_pipeline_run(run["run_id"], self.store, event_store=self.events)

        self.assertFalse(result["ok"])
        acquire_mp3 = self._step(result["run"], "acquire_mp3")
        self.assertEqual(acquire_mp3["status"], "failed")
        self.assertIn("AZURACAST_API_KEY is not configured", acquire_mp3["message"])

    def test_retry_no_matching_enclosure_records_clear_failure(self):
        run = self._failed_retryable_run("no-enclosure")

        result = retry_pipeline_run(
            run["run_id"],
            self.store,
            event_store=self.events,
            mp3_runner=self._mp3_no_matching_enclosure,
            tracklist_runner=Mock(),
        )

        self.assertFalse(result["ok"])
        acquire_mp3 = self._step(result["run"], "acquire_mp3")
        self.assertEqual(acquire_mp3["status"], "failed")
        self.assertIn("No matching RSS enclosure", acquire_mp3["message"])

    def test_retry_skips_mp3_when_castopod_draft_already_exists(self):
        run = self._failed_retryable_run("draft-exists")
        with self.store.connect() as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET castopod_episode_id = ?,
                    castopod_episode_url = ?
                WHERE run_id = ?
                """,
                ("episode-123", "https://castopod.example/episodes/123", run["run_id"]),
            )
        mp3_runner = Mock()

        result = retry_pipeline_run(
            run["run_id"],
            self.store,
            event_store=self.events,
            mp3_runner=mp3_runner,
            tracklist_runner=Mock(),
        )

        mp3_runner.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(self._step(result["run"], "acquire_mp3")["status"], "success")
        events = self.events.find_events(run_id=run["run_id"], event_name="run_retry.acquire_mp3_skipped")
        self.assertEqual(events[-1]["status"], "skipped")

    def _failed_retryable_run(self, run_id):
        run = self.store.mark_stream_start(
            session_id=run_id,
            started_at="2026-06-25T22:00:00+00:00",
            station="Storm Surge",
            show_name="Storm Surge",
        )
        run = self.store.mark_stream_end(
            run_id=run["run_id"],
            ended_at="2026-06-25T23:00:00+00:00",
        )
        run = self.store.mark_step_failed(
            run["run_id"],
            "acquire_mp3",
            message="MP3 acquisition failed: previous failure.",
        )
        return self.store.update_step_status(
            run["run_id"],
            "acquire_tracklist",
            "success",
            message="Tracklist acquired with 11 tracks.",
            error_details={"track_count_filtered": 11},
        )

    def _mp3_success(self, run_id, store, event_store=None):
        run = store.update_step_status(
            run_id,
            "acquire_mp3",
            "success",
            message="AzuraCast podcast audio acquired and Castopod draft created.",
            error_details={"audio_size_bytes": 12345},
        )
        with store.connect() as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET castopod_episode_id = ?,
                    castopod_episode_url = ?
                WHERE run_id = ?
                """,
                ("episode-321", "https://castopod.example/episodes/321", run_id),
            )
        return store.get_run(run_id)

    def _mp3_download_failure(self, run_id, store, event_store=None):
        return store.mark_step_failed(
            run_id,
            "acquire_mp3",
            message="MP3 acquisition failed: MP3 download failed.",
        )

    def _mp3_no_matching_enclosure(self, run_id, store, event_store=None):
        return store.mark_step_failed(
            run_id,
            "acquire_mp3",
            message="MP3 acquisition failed: No matching RSS enclosure was found for the completed session.",
        )

    def _step(self, run, step_key):
        return next(step for step in run["steps"] if step["step_key"] == step_key)


class PipelineRetryRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        self.temp_dir.cleanup()

    @patch("app.retry_pipeline_run")
    def test_retry_route_targets_existing_run(self, retry_pipeline_run_mock):
        retry_pipeline_run_mock.return_value = {
            "ok": True,
            "message": "Run retry completed.",
            "run": {"run_id": "route-run"},
        }

        response = self.client.post("/runs/route-run/retry")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/runs")
        retry_pipeline_run_mock.assert_called_once()
        self.assertEqual(retry_pipeline_run_mock.call_args.args[0], "route-run")

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


if __name__ == "__main__":
    unittest.main()
