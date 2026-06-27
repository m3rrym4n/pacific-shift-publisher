import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app import app
from azuracast_config import AzuraCastConfigStore
from broadcast_selection import (
    BroadcastSelectionError,
    get_ready_broadcasts,
    select_broadcast_for_pipeline,
)
from pipeline_state import PipelineStateStore


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError("request failed")
            error.response = self
            raise error

    def json(self):
        return self.payload


class BroadcastSelectionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        self.store = PipelineStateStore(self.db_path)
        self.config_store = AzuraCastConfigStore(self.db_path)
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_id": "1",
                "streamer_id": "7",
                "station_name": "Storm Surge",
                "station_shortcode": "storm_surge",
                "api_key": "managed-secret",
            }
        )
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        self.temp_dir.cleanup()

    def test_ready_broadcasts_filter_sort_and_calculate_display_fields(self):
        http_get = Mock(
            return_value=FakeResponse(
                [
                    self._broadcast(1, "2026-06-25T20:00:00Z", "2026-06-25T21:00:00Z"),
                    self._broadcast(2, "2026-06-25T22:00:00Z", "2026-06-25T23:30:00Z"),
                    self._broadcast(3, recording=None),
                ]
            )
        )

        broadcasts = get_ready_broadcasts(store=self.store, http_get=http_get)

        self.assertEqual([item["broadcast_id"] for item in broadcasts], [2, 1])
        self.assertEqual(broadcasts[0]["duration_minutes"], 90.0)
        self.assertEqual(broadcasts[0]["size_mb"], 10.0)
        self.assertEqual(broadcasts[0]["download_url"], "https://azuracast.example/2.mp3")
        self.assertEqual(
            http_get.call_args.args[0],
            "https://azuracast.example/api/station/1/streamer/7/broadcasts",
        )

    def test_ready_broadcasts_returns_empty_list(self):
        broadcasts = get_ready_broadcasts(
            store=self.store,
            http_get=Mock(return_value=FakeResponse([self._broadcast(1, recording=None)])),
        )

        self.assertEqual(broadcasts, [])

    def test_ready_broadcasts_rejects_disabled_integration(self):
        config, errors = self.config_store.save_config({"enabled": False})
        self.assertEqual(errors, [])

        with self.assertRaises(BroadcastSelectionError) as context:
            get_ready_broadcasts(store=self.store, config=config, http_get=Mock())

        self.assertEqual(context.exception.status_code, 409)

    def test_selection_creates_run_and_runs_tracklist_before_mp3(self):
        calls = []

        def tracklist_runner(run_id, store):
            calls.append("tracklist")
            return store.update_step_status(run_id, "acquire_tracklist", "success")

        def mp3_runner(run_id, store):
            calls.append("mp3")
            return store.update_step_status(run_id, "acquire_mp3", "success")

        def assemble_runner(run_id, store):
            calls.append("assemble")
            return store.update_step_status(run_id, "assemble_episode", "success")

        result = select_broadcast_for_pipeline(
            2,
            store=self.store,
            http_get=Mock(return_value=FakeResponse([self._broadcast(2)])),
            tracklist_runner=tracklist_runner,
            mp3_runner=mp3_runner,
            assemble_runner=assemble_runner,
        )

        run = result["run"]
        self.assertTrue(result["created"])
        self.assertEqual(calls, ["tracklist", "mp3", "assemble"])
        self.assertEqual(run["broadcast_id"], "2")
        self.assertEqual(run["started_at"], "2026-06-25T22:00:00+00:00")
        self.assertEqual(run["ended_at"], "2026-06-25T23:00:00+00:00")
        self.assertEqual(run["recording_reference"], "https://azuracast.example/2.mp3")

    def test_selection_updates_overlapping_incomplete_run(self):
        existing = self.store.mark_stream_start(
            session_id="webhook-session",
            station="Storm Surge",
            streamer="SeaCapn",
            started_at="2026-06-25T22:00:10Z",
        )
        existing = self.store.mark_stream_end(
            run_id=existing["run_id"],
            ended_at="2026-06-25T22:59:50Z",
        )
        self.store.mark_step_failed(existing["run_id"], "acquire_mp3", message="Not ready.")

        result = select_broadcast_for_pipeline(
            2,
            store=self.store,
            http_get=Mock(return_value=FakeResponse([self._broadcast(2)])),
            tracklist_runner=lambda run_id, store: store.update_step_status(
                run_id, "acquire_tracklist", "skipped"
            ),
            mp3_runner=Mock(),
        )

        self.assertFalse(result["created"])
        self.assertEqual(result["run"]["run_id"], existing["run_id"])
        self.assertEqual(len(self.store.get_recent_runs()), 1)
        self.assertEqual(result["run"]["broadcast_id"], "2")

    def test_broadcast_routes_and_runs_button(self):
        ready = [{"broadcast_id": 2, "started_at": "2026-06-25T22:00:00+00:00"}]
        with patch("app.get_ready_broadcasts", return_value=ready):
            response = self.client.get("/api/broadcasts")
        runs_response = self.client.get("/runs")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), ready)
        self.assertIn("Get Broadcasts", runs_response.get_data(as_text=True))
        self.assertIn("No ready broadcast recordings are available.", runs_response.get_data(as_text=True))

    def test_disabled_broadcast_route_returns_error(self):
        error = BroadcastSelectionError("AzuraCast integration is disabled.", 409)
        with patch("app.get_ready_broadcasts", side_effect=error):
            response = self.client.get("/api/broadcasts")

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])

    def test_select_route_targets_broadcast_and_returns_run(self):
        selected = {
            "run": {"run_id": "selected-run", "overall_status": "in_progress"},
            "created": True,
            "broadcast": {"broadcast_id": 2},
        }
        with patch("app.select_broadcast_for_pipeline", return_value=selected) as selector:
            response = self.client.post("/api/broadcasts/select", json={"broadcast_id": 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["run_id"], "selected-run")
        selector.assert_called_once()
        self.assertEqual(selector.call_args.args, (2,))
        self.assertEqual(selector.call_args.kwargs["store"].db_path, self.db_path)

    def _broadcast(
        self,
        broadcast_id,
        started_at="2026-06-25T22:00:00Z",
        ended_at="2026-06-25T23:00:00Z",
        recording="default",
    ):
        if recording == "default":
            recording = {
                "size": 10 * 1024 * 1024,
                "downloadUrl": f"https://azuracast.example/{broadcast_id}.mp3",
            }
        return {
            "id": broadcast_id,
            "timestampStart": started_at,
            "timestampEnd": ended_at,
            "recording": recording,
        }


if __name__ == "__main__":
    unittest.main()
