import json
import logging
import tempfile
import unittest
from pathlib import Path

from pipeline_constants import PIPELINE_STATUSES, PIPELINE_STEP_KEYS
from pipeline_logging import StructuredPipelineLogger, sanitize_log_value
from pipeline_state import PipelineStateStore


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class StructuredPipelineLoggerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.handler = ListHandler()
        self.logger = logging.getLogger(f"test.pipeline.{id(self)}")
        self.logger.handlers = []
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)
        self.events = StructuredPipelineLogger(self.db_path, logger=self.logger)
        self.store = PipelineStateStore(self.db_path)

    def tearDown(self):
        self.logger.handlers = []
        self.temp_dir.cleanup()

    def test_emit_structured_event_with_required_fields(self):
        event = self.events.emit(
            run_id="run-001",
            session_id="session-001",
            step_key="stream_start",
            event_name="stream_start.succeeded",
            status="success",
            message="Stream start captured.",
            details={"source": "test"},
        )

        self.assertEqual(
            set(event.keys()),
            {
                "timestamp",
                "level",
                "run_id",
                "session_id",
                "step_key",
                "event_name",
                "status",
                "message",
                "details",
            },
        )
        self.assertEqual(event["run_id"], "run-001")
        self.assertEqual(event["session_id"], "session-001")
        self.assertEqual(event["step_key"], "stream_start")
        self.assertEqual(event["status"], "success")
        self.assertEqual(json.loads(self.handler.records[0].getMessage())["run_id"], "run-001")

    def test_rejects_invalid_step_or_status(self):
        with self.assertRaises(ValueError):
            self.events.emit(
                run_id="run-001",
                step_key="bad_step",
                event_name="bad",
                status="success",
                message="bad",
            )

        with self.assertRaises(ValueError):
            self.events.emit(
                run_id="run-001",
                step_key="stream_start",
                event_name="bad",
                status="bad_status",
                message="bad",
            )

    def test_sanitizer_redacts_tokens_and_authorization_headers(self):
        sanitized = sanitize_log_value(
            {
                "message": "Authorization: Bearer abc123",
                "nested": ["token=secret-token", "password=hunter2"],
            }
        )

        serialized = json.dumps(sanitized)
        self.assertIn("[redacted]", serialized)
        self.assertNotIn("abc123", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("hunter2", serialized)

    def test_events_can_be_filtered_by_run_session_and_step(self):
        self.events.emit(
            run_id="run-a",
            session_id="session-a",
            step_key="stream_start",
            event_name="stream_start.succeeded",
            status="success",
            message="ok",
        )
        self.events.emit(
            run_id="run-b",
            session_id="session-b",
            step_key="acquire_mp3",
            event_name="acquire_mp3.failed",
            status="failed",
            message="failed",
        )

        self.assertEqual(len(self.events.find_events(run_id="run-a")), 1)
        self.assertEqual(len(self.events.find_events(session_id="session-b")), 1)
        self.assertEqual(len(self.events.find_events(step_key="acquire_mp3")), 1)

    def test_pipeline_state_creation_and_step_updates_emit_events(self):
        run = self.store.create_run(session_id="session-log")
        self.store.update_step_status(run["run_id"], "acquire_mp3", "in_progress")
        self.store.mark_step_success(run["run_id"], "acquire_mp3", message="MP3 acquired.")
        self.store.mark_step_failed(
            run["run_id"],
            "acquire_tracklist",
            message="Tracklist failed Authorization: Bearer hidden",
            error_details="api_key=hidden-key",
        )

        events = self.events.find_events(run_id=run["run_id"])
        event_names = [event["event_name"] for event in events]

        self.assertIn("pipeline_run.created", event_names)
        self.assertIn("acquire_mp3.started", event_names)
        self.assertIn("acquire_mp3.succeeded", event_names)
        self.assertIn("acquire_tracklist.failed", event_names)
        failure = events[-1]
        self.assertEqual(failure["level"], "ERROR")
        self.assertIn("[redacted]", failure["message"])
        self.assertIn("[redacted]", failure["details"]["error_details"])

    def test_success_and_failure_patterns_supported_for_all_steps(self):
        run = self.store.create_run(session_id="all-steps")
        for step_key in PIPELINE_STEP_KEYS:
            self.store.mark_step_success(run["run_id"], step_key, message=f"{step_key} ok")
            self.store.mark_step_failed(run["run_id"], step_key, message=f"{step_key} failed")

        events = self.events.find_events(run_id=run["run_id"])
        for step_key in PIPELINE_STEP_KEYS:
            self.assertIn(f"{step_key}.succeeded", [event["event_name"] for event in events])
            self.assertIn(f"{step_key}.failed", [event["event_name"] for event in events])

    def test_required_statuses_are_supported(self):
        self.assertEqual(
            set(PIPELINE_STATUSES),
            {"pending", "waiting", "waiting_transcode", "in_progress", "success", "failed", "skipped"},
        )


if __name__ == "__main__":
    unittest.main()
