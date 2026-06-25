import os
import tempfile
import unittest
from pathlib import Path

from app import app
from pipeline_logging import StructuredPipelineLogger


class LogsViewTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.logger = StructuredPipelineLogger(self.db_path)

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        self.temp_dir.cleanup()

    def test_logs_renders_empty_state(self):
        response = self.client.get("/logs")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Logs", body)
        self.assertIn("Recent structured Publisher pipeline events.", body)
        self.assertIn("No pipeline events yet.", body)

    def test_logs_renders_recent_events_newest_first(self):
        self.emit_event(
            timestamp="2026-06-24T22:00:00+00:00",
            run_id="run-old",
            session_id="session-old",
            step_key="stream_start",
            event_name="stream_start.succeeded",
            status="success",
            message="Stream started.",
        )
        self.emit_event(
            timestamp="2026-06-24T23:00:00+00:00",
            run_id="run-new",
            session_id="session-new",
            step_key="stream_end",
            event_name="stream_end.succeeded",
            status="success",
            message="Stream ended.",
        )

        response = self.client.get("/logs")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertLess(body.index("stream_end.succeeded"), body.index("stream_start.succeeded"))
        self.assertIn("2026-06-24T23:00:00+00:00", body)
        self.assertIn("run-new", body)
        self.assertIn("session-new", body)
        self.assertIn("stream_end", body)
        self.assertIn("success", body)
        self.assertIn("Stream ended.", body)

    def test_logs_displays_safe_details_for_webhook_and_tracklist_events(self):
        self.emit_event(
            step_key="stream_start",
            event_name="azuracast_webhook_diagnostics",
            status="success",
            message="AzuraCast webhook request diagnostics.",
            details={
                "station_name": "Storm Surge",
                "station_shortcode": "storm_surge",
                "streamer": "SeaCapn",
                "parser_decision": "recognized_non_live",
                "parser_reason": "Known non-live Now Playing payload.",
                "payload_kind": "now_playing",
                "song_history_count": 96,
            },
        )
        self.emit_event(
            run_id="tracklist-run",
            step_key="acquire_tracklist",
            event_name="acquire_tracklist.succeeded",
            status="success",
            message="Tracklist generated.",
            details={
                "history_url_used": "https://azuracast.example/api/nowplaying_static/storm_surge.json",
                "track_count_total": 42,
                "track_count_filtered": 5,
            },
        )

        response = self.client.get("/logs")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("azuracast_webhook_diagnostics", body)
        self.assertIn("Station Name", body)
        self.assertIn("Storm Surge", body)
        self.assertIn("Parser Decision", body)
        self.assertIn("recognized_non_live", body)
        self.assertIn("acquire_tracklist.succeeded", body)
        self.assertIn("History Url Used", body)
        self.assertIn("Track Count Filtered", body)
        self.assertIn("5", body)

    def test_logs_do_not_display_sensitive_or_raw_detail_fields(self):
        self.emit_event(
            step_key="stream_start",
            event_name="azuracast_webhook_diagnostics",
            status="failed",
            message="Authorization: Bearer secret-token",
            details={
                "station": "Storm Surge",
                "api_key": "super-secret-api-key",
                "authorization": "Bearer hidden-token",
                "cookie": "session=hidden",
                "raw_body": "{\"secret\":\"hidden\"}",
                "song_history": [{"song": {"text": "A - B"}}],
            },
        )

        response = self.client.get("/logs")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Storm Surge", body)
        self.assertIn("[redacted]", body)
        self.assertNotIn("secret-token", body)
        self.assertNotIn("super-secret-api-key", body)
        self.assertNotIn("hidden-token", body)
        self.assertNotIn("session=hidden", body)
        self.assertNotIn("raw_body", body)
        self.assertNotIn("song_history", body)

    def test_logs_filters_by_run_session_step_and_event_name(self):
        self.emit_event(
            run_id="run-a",
            session_id="session-a",
            step_key="stream_start",
            event_name="stream_start.succeeded",
            status="success",
            message="Message alpha",
        )
        self.emit_event(
            run_id="run-b",
            session_id="session-b",
            step_key="acquire_tracklist",
            event_name="acquire_tracklist.failed",
            status="failed",
            message="Message beta",
        )

        cases = [
            ("/logs?run_id=run-a", "Message alpha", "Message beta"),
            ("/logs?session_id=session-b", "Message beta", "Message alpha"),
            ("/logs?step_key=acquire_tracklist", "Message beta", "Message alpha"),
            ("/logs?event_name=stream_start.succeeded", "Message alpha", "Message beta"),
        ]
        for path, included, excluded in cases:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                self.assertIn(included, body)
                self.assertNotIn(excluded, body)

    def test_manual_upload_still_renders_required_fields(self):
        response = self.client.get("/manual-upload")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('action="/upload"', body)
        self.assertIn('name="podcast_id"', body)
        self.assertIn('name="save_as_draft"', body)
        self.assertIn('name="title"', body)
        self.assertIn('name="description"', body)
        self.assertIn('name="audio_file"', body)

    def emit_event(
        self,
        *,
        run_id="run-logs",
        session_id="session-logs",
        step_key,
        event_name,
        status,
        message,
        details=None,
        timestamp="2026-06-24T22:00:00+00:00",
    ):
        return self.logger.emit(
            run_id=run_id,
            session_id=session_id,
            step_key=step_key,
            event_name=event_name,
            status=status,
            message=message,
            details=details,
            timestamp=timestamp,
            level="ERROR" if status == "failed" else "INFO",
        )


if __name__ == "__main__":
    unittest.main()
