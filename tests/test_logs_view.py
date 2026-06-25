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
        self.assertIn("Detail mode: Safe", body)
        self.assertIn("Auto-refresh", body)
        self.assertIn("Manual refresh", body)
        self.assertIn("Copy visible logs", body)
        self.assertIn("Download Logs", body)
        self.assertIn("No pipeline events yet.", body)
        self.assertNotIn("Available Logs", body)
        self.assertNotIn("Pipeline Events source", body)

    def test_logs_renders_one_tail_viewer_oldest_to_newest(self):
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
        self.assertIn("Live Pipeline Log", body)
        self.assertIn("Oldest events at top; newest events at bottom.", body)
        self.assertNotIn("<table", body)
        self.assertLess(body.index("stream_start.succeeded"), body.index("stream_end.succeeded"))
        self.assertIn("2026-06-24T23:00:00+00:00", body)
        self.assertIn("run-new", body)
        self.assertIn("session-new", body)
        self.assertIn("stream_end", body)
        self.assertIn("success", body)
        self.assertIn("Stream ended.", body)
        self.assertIn("[stream_start]", body)
        self.assertIn("[stream_start.succeeded]", body)
        self.assertIn("[success]", body)
        self.assertIn("run=run-old", body)
        self.assertIn("session=session-old", body)

    def test_logs_displays_log_lines_with_inline_safe_details(self):
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
        self.assertIn("[stream_start] [azuracast_webhook_diagnostics] [success]", body)
        self.assertIn("station=Storm Surge", body)
        self.assertIn("station_shortcode=storm_surge", body)
        self.assertIn("streamer=SeaCapn", body)
        self.assertIn("parser=recognized_non_live", body)
        self.assertIn("payload=now_playing", body)
        self.assertIn("history_count=96", body)
        self.assertIn("acquire_tracklist.succeeded", body)
        self.assertIn("history_url=https://azuracast.example/api/nowplaying_static/storm_surge.json", body)
        self.assertIn("tracks_filtered=5", body)

    def test_logs_safe_mode_hides_non_whitelisted_detail_fields(self):
        self.emit_event(
            step_key="stream_start",
            event_name="azuracast_webhook_diagnostics",
            status="success",
            message="Diagnostics.",
            details={
                "station": "Storm Surge",
                "unlisted_debug_context": "visible outside safe mode",
            },
        )

        default_response = self.client.get("/logs")
        explicit_response = self.client.get("/logs?detail_mode=safe")

        for response in (default_response, explicit_response):
            with self.subTest(path=response.request.path):
                body = response.get_data(as_text=True)
                self.assertIn("Detail mode: Safe", body)
                self.assertIn("Storm Surge", body)
                self.assertNotIn("visible outside safe mode", body)

    def test_logs_verbose_mode_shows_full_sanitized_json_details(self):
        self.emit_event(
            step_key="stream_start",
            event_name="azuracast_webhook_diagnostics",
            status="success",
            message="Diagnostics.",
            details={
                "station": "Storm Surge",
                "unlisted_debug_context": "visible in verbose mode",
                "authorization": "Bearer hidden-token",
                "api_key": "super-secret-api-key",
            },
        )

        response = self.client.get("/logs?detail_mode=verbose")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Detail mode: Verbose", body)
        self.assertIn("Verbose JSON", body)
        self.assertIn("unlisted_debug_context", body)
        self.assertIn("visible in verbose mode", body)
        self.assertIn("authorization", body)
        self.assertIn("api_key", body)
        self.assertIn("[redacted]", body)
        self.assertNotIn("hidden-token", body)
        self.assertNotIn("super-secret-api-key", body)

    def test_logs_raw_mode_is_selectable_without_environment_variable(self):
        self.emit_event(
            step_key="stream_start",
            event_name="azuracast_webhook_diagnostics",
            status="success",
            message="Diagnostics.",
            details={
                "station": "Storm Surge",
                "raw_body": "{\"hello\":\"world\",\"api_key\":\"raw-secret\"}",
                "unlisted_debug_context": "visible in raw mode",
                "authorization": "Bearer hidden-token",
                "cookie": "session=hidden",
                "password": "super-secret-password",
                "bearer": "hidden-bearer-token",
            },
        )

        response = self.client.get("/logs?detail_mode=raw")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Raw Debug mode is enabled. Use only for local troubleshooting.", body)
        self.assertIn("Detail mode: Raw Debug", body)
        self.assertIn("Raw Debug JSON", body)
        self.assertNotIn("Raw Debug mode is disabled", body)
        self.assertIn("raw_body", body)
        self.assertIn("hello", body)
        self.assertIn("world", body)
        self.assertIn("visible in raw mode", body)
        self.assertIn("authorization", body)
        self.assertIn("cookie", body)
        self.assertIn("password", body)
        self.assertIn("bearer", body)
        self.assertIn("[redacted]", body)
        self.assertNotIn("hidden-token", body)
        self.assertNotIn("session=hidden", body)
        self.assertNotIn("super-secret-password", body)
        self.assertNotIn("hidden-bearer-token", body)
        self.assertNotIn("raw-secret", body)

    def test_logs_do_not_display_sensitive_or_raw_detail_fields_in_safe_mode(self):
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

    def test_logs_copy_and_auto_refresh_controls_are_present(self):
        response = self.client.get("/logs")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('id="auto-refresh-toggle"', body)
        self.assertIn('id="manual-refresh-button"', body)
        self.assertIn('id="copy-visible-logs-button"', body)
        self.assertIn("window.setInterval", body)
        self.assertIn("navigator.clipboard.writeText", body)

    def test_logs_download_returns_text_attachment_with_safe_default(self):
        self.emit_event(
            step_key="stream_start",
            event_name="azuracast_webhook_diagnostics",
            status="success",
            message="Diagnostics.",
            details={
                "station": "Storm Surge",
                "unlisted_debug_context": "hidden in safe download",
                "api_key": "super-secret-api-key",
            },
        )

        response = self.client.get("/logs/download")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["Content-Type"])
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        self.assertIn("pacific-shift-publisher-logs-", response.headers["Content-Disposition"])
        self.assertIn("Pacific Shift Publisher Logs", body)
        self.assertIn("Detail mode: safe", body)
        self.assertIn("[stream_start] [azuracast_webhook_diagnostics] [success]", body)
        self.assertIn("station=Storm Surge", body)
        self.assertNotIn("hidden in safe download", body)
        self.assertNotIn("super-secret-api-key", body)

    def test_logs_download_verbose_includes_sanitized_details(self):
        self.emit_event(
            step_key="acquire_tracklist",
            event_name="acquire_tracklist.succeeded",
            status="success",
            message="Tracklist acquired.",
            details={
                "track_count_filtered": 4,
                "unlisted_debug_context": "visible in verbose download",
                "authorization": "Bearer hidden-token",
            },
        )

        response = self.client.get("/logs/download?detail_mode=verbose")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Detail mode: verbose", body)
        self.assertIn("visible in verbose download", body)
        self.assertIn("\"authorization\": \"[redacted]\"", body)
        self.assertNotIn("hidden-token", body)

    def test_logs_download_raw_respects_redaction_and_step_filter(self):
        self.emit_event(
            step_key="stream_start",
            event_name="stream_start.succeeded",
            status="success",
            message="Stream started.",
        )
        self.emit_event(
            step_key="acquire_tracklist",
            event_name="acquire_tracklist.failed",
            status="failed",
            message="Tracklist failed.",
            details={
                "raw_body": "{\"api_key\":\"raw-secret\",\"visible\":\"yes\"}",
                "token": "secret-token",
            },
        )

        response = self.client.get("/logs/download?step_key=acquire_tracklist&detail_mode=raw")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Detail mode: raw", body)
        self.assertIn("step_key filter: acquire_tracklist", body)
        self.assertIn("acquire_tracklist.failed", body)
        self.assertNotIn("stream_start.succeeded", body)
        self.assertIn("visible", body)
        self.assertNotIn("raw-secret", body)
        self.assertNotIn("secret-token", body)
        self.assertIn("[redacted]", body)

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
