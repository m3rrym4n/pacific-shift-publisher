import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from app import app
from pipeline_logging import StructuredPipelineLogger
from pipeline_run_snapshot import (
    SnapshotImportError,
    export_run_snapshot,
    import_run_snapshot,
)
from pipeline_state import PipelineStateStore


class PipelineRunSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.store = PipelineStateStore(self.db_path)
        self.events = StructuredPipelineLogger(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_export_includes_run_steps_events_and_tracklist_details(self):
        run = self._create_snapshot_run()

        snapshot = export_run_snapshot(run["run_id"], self.store, self.events)

        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["run"]["run_id"], run["run_id"])
        self.assertEqual(len(snapshot["run"]["steps"]), 6)
        event_names = [event["event_name"] for event in snapshot["events"]]
        self.assertIn("acquire_tracklist.succeeded", event_names)
        tracklist_event = next(event for event in snapshot["events"] if event["event_name"] == "acquire_tracklist.succeeded")
        self.assertEqual(tracklist_event["details"]["track_count_filtered"], 1)
        self.assertEqual(tracklist_event["details"]["tracks"][0]["title"], "Snapshot Title")

    def test_export_redacts_sensitive_values(self):
        run = self._create_snapshot_run()
        self.events.emit(
            run_id=run["run_id"],
            session_id=run["session_id"],
            step_key="acquire_mp3",
            event_name="acquire_mp3.debug",
            status="failed",
            message="Authorization: Bearer super-secret-token",
            details={
                "api_key": "abc123",
                "authorization": "Bearer abc123",
                "safe_value": "visible",
            },
        )

        snapshot = export_run_snapshot(run["run_id"], self.store, self.events)
        rendered = json.dumps(snapshot)

        self.assertIn("visible", rendered)
        self.assertNotIn("abc123", rendered)
        self.assertNotIn("super-secret-token", rendered)
        self.assertIn("[redacted]", rendered)

    def test_import_restores_run_steps_and_events(self):
        run = self._create_snapshot_run()
        snapshot = export_run_snapshot(run["run_id"], self.store, self.events)
        target_dir = tempfile.TemporaryDirectory()
        self.addCleanup(target_dir.cleanup)
        target_store = PipelineStateStore(str(Path(target_dir.name) / "target.sqlite"))
        target_events = StructuredPipelineLogger(target_store.db_path)

        imported = import_run_snapshot(snapshot, target_store)

        self.assertEqual(imported["run_id"], run["run_id"])
        self.assertEqual(imported["station"], "Storm Surge")
        self.assertEqual(self._step(imported, "acquire_tracklist")["status"], "success")
        events = target_events.find_events(run_id=run["run_id"], step_key="acquire_tracklist")
        self.assertTrue(events)
        self.assertEqual(events[-1]["details"]["track_count_filtered"], 1)

    def test_import_rejects_duplicate_run_id(self):
        run = self._create_snapshot_run()
        snapshot = export_run_snapshot(run["run_id"], self.store, self.events)

        with self.assertRaises(SnapshotImportError):
            import_run_snapshot(snapshot, self.store)

    def test_import_rejects_invalid_snapshot(self):
        with self.assertRaises(SnapshotImportError):
            import_run_snapshot({"schema_version": 1}, self.store)

    def _create_snapshot_run(self):
        run = self.store.create_run(
            run_id="snapshot-run",
            session_id="snapshot-session",
            station="Storm Surge",
            show_name="Storm Surge",
            streamer="SeaCapn",
        )
        run = self.store.mark_stream_start(
            session_id="snapshot-session",
            started_at="2026-06-25T22:00:00+00:00",
            station="Storm Surge",
            show_name="Storm Surge",
            streamer="SeaCapn",
        )
        run = self.store.mark_stream_end(
            run_id=run["run_id"],
            ended_at="2026-06-25T23:00:00+00:00",
        )
        return self.store.update_step_status(
            run["run_id"],
            "acquire_tracklist",
            "success",
            message="Tracklist acquired.",
            error_details={
                "track_count_filtered": 1,
                "tracklist": "Tracklist\n\n0:00:00 Snapshot Artist - Snapshot Title",
                "tracks": [
                    {
                        "played_at": "2026-06-25T22:00:00+00:00",
                        "artist": "Snapshot Artist",
                        "title": "Snapshot Title",
                    }
                ],
            },
        )

    def _step(self, run, step_key):
        return next(step for step in run["steps"] if step["step_key"] == step_key)


class PipelineRunSnapshotRouteTest(unittest.TestCase):
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

    def test_runs_page_shows_import_and_export_actions(self):
        self.store.create_run(run_id="route-run", session_id="route-run")

        response = self.client.get("/runs")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/runs/import"', body)
        self.assertIn("Import Run", body)
        self.assertIn('href="/runs/route-run/export"', body)
        self.assertIn("Export", body)

    def test_export_route_downloads_json_snapshot(self):
        run = self.store.create_run(run_id="download-run", session_id="download-run")

        response = self.client.get(f"/runs/{run['run_id']}/export")

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        payload = response.get_json()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["run"]["run_id"], run["run_id"])

    def test_import_route_accepts_exported_snapshot(self):
        source_dir = tempfile.TemporaryDirectory()
        self.addCleanup(source_dir.cleanup)
        source_store = PipelineStateStore(str(Path(source_dir.name) / "source.sqlite"))
        source_run = source_store.create_run(run_id="imported-run", session_id="imported-run")
        snapshot = export_run_snapshot(source_run["run_id"], source_store)

        response = self.client.post(
            "/runs/import",
            data={"snapshot_file": (io.BytesIO(json.dumps(snapshot).encode("utf-8")), "snapshot.json")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 303)
        imported = self.store.get_run("imported-run")
        self.assertIsNotNone(imported)

    def test_import_route_rejects_invalid_json_without_crashing(self):
        response = self.client.post(
            "/runs/import",
            data={"snapshot_file": (io.BytesIO(b"not-json"), "bad.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("valid JSON snapshot", response.get_data(as_text=True))

    def test_import_route_rejects_duplicate_run(self):
        run = self.store.create_run(run_id="duplicate-run", session_id="duplicate-run")
        snapshot = export_run_snapshot(run["run_id"], self.store)

        response = self.client.post(
            "/runs/import",
            data={"snapshot_file": (io.BytesIO(json.dumps(snapshot).encode("utf-8")), "snapshot.json")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("already exists", response.get_data(as_text=True))

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
