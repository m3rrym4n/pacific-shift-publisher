import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app import app
from azuracast_config import AzuraCastConfigStore
from pipeline_logging import StructuredPipelineLogger
from pipeline_mp3 import acquire_mp3_for_run, find_matching_broadcast, resolve_broadcasts_url
from pipeline_state import PipelineStateStore


class FakeResponse:
    def __init__(self, *, payload=None, content=b"", status_code=200, headers=None, json_error=False):
        self.payload = payload
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError("request failed")
            error.response = self
            raise error

    def json(self):
        if self.json_error:
            raise ValueError("bad json")
        return self.payload

    def iter_content(self, chunk_size=1):
        yield self.content


class PipelineMp3Test(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        self.original_api_key = os.environ.get("AZURACAST_API_KEY")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        os.environ.pop("AZURACAST_API_KEY", None)
        self.store = PipelineStateStore(self.db_path)
        self.config_store = AzuraCastConfigStore(self.db_path)
        self.events = StructuredPipelineLogger(self.db_path)
        self.run = self.store.mark_stream_start(
            session_id="session-30",
            started_at="2026-06-25T22:00:00+00:00",
            station="Storm Surge",
            show_name="Storm Surge",
        )
        self.run = self.store.mark_stream_end(
            run_id=self.run["run_id"],
            ended_at="2026-06-25T23:00:00+00:00",
        )
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_id": "1",
                "streamer_id": "7",
                "api_key": "managed-secret",
            }
        )

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

    def test_broadcast_url_uses_station_and_streamer_ids(self):
        url = resolve_broadcasts_url(self.config_store.get_config())

        self.assertEqual(url, "https://azuracast.example/api/station/1/streamer/7/broadcasts")
        self.assertNotIn("podcast", url)

    def test_match_requires_start_and_end_within_sixty_seconds(self):
        http_get = Mock(
            return_value=FakeResponse(
                payload=[
                    self._broadcast("too-early", "2026-06-25T21:58:59Z", "2026-06-25T23:00:00Z"),
                    self._broadcast("match", "2026-06-25T22:00:45Z", "2026-06-25T22:59:10Z"),
                ]
            )
        )

        match = find_matching_broadcast(
            self.run,
            http_get=http_get,
            config=self.config_store.get_config(),
            api_key="managed-secret",
        )

        self.assertEqual(match["id"], "match")
        self.assertEqual(http_get.call_args.kwargs["headers"], {"X-API-Key": "managed-secret"})
        self.assertNotIn("Authorization", http_get.call_args.kwargs["headers"])

    def test_recording_null_persists_broadcast_and_waits_without_polling(self):
        http_get = Mock(return_value=FakeResponse(payload=[self._broadcast("broadcast-1")]))

        updated = acquire_mp3_for_run(
            self.run["run_id"], self.store, http_get=http_get, event_store=self.events
        )

        self.assertEqual(self._step(updated, "acquire_mp3")["status"], "waiting_transcode")
        self.assertEqual(updated["recording_reference"], "broadcast-1")
        self.assertEqual(http_get.call_count, 1)
        event_names = [event["event_name"] for event in self.events.find_events(run_id=updated["run_id"])]
        self.assertIn("azuracast_broadcast_match_succeeded", event_names)
        self.assertIn("azuracast_transcode_waiting", event_names)

    def test_existing_broadcast_reference_filters_broadcast_list(self):
        self.store.set_recording_reference(self.run["run_id"], "1")
        http_get = Mock(
            return_value=FakeResponse(
                payload=[self._broadcast("other"), self._broadcast(1)]
            )
        )

        updated = acquire_mp3_for_run(
            self.run["run_id"], self.store, http_get=http_get, event_store=self.events
        )

        self.assertEqual(self._step(updated, "acquire_mp3")["status"], "waiting_transcode")
        self.assertEqual(
            http_get.call_args.args[0],
            "https://azuracast.example/api/station/1/streamer/7/broadcasts",
        )
        self.assertNotIn("/broadcast/1", http_get.call_args.args[0])

    def test_missing_stored_broadcast_fails_without_single_broadcast_request(self):
        self.store.set_recording_reference(self.run["run_id"], "missing")
        http_get = Mock(
            return_value=FakeResponse(payload=[self._broadcast("other")])
        )

        updated = acquire_mp3_for_run(
            self.run["run_id"],
            self.store,
            http_get=http_get,
            event_store=self.events,
        )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "failed")
        self.assertIn("Stored AzuraCast broadcast was not found", step["message"])
        self.assertEqual(
            http_get.call_args.args[0],
            "https://azuracast.example/api/station/1/streamer/7/broadcasts",
        )

    def test_ready_recording_downloads_and_validates_without_castopod(self):
        broadcast = self._broadcast(
            "broadcast-1",
            recording={
                "path": "recordings/show.mp3",
                "size": 8,
                "downloadUrl": "https://azuracast.example/download/broadcast-1.mp3",
            },
        )
        http_get = Mock(
            side_effect=[
                FakeResponse(payload=[broadcast]),
                FakeResponse(content=b"mp3-data", headers={"content-type": "audio/mpeg"}),
            ]
        )

        with patch("castopod_client.requests.post") as castopod_post:
            updated = acquire_mp3_for_run(
                self.run["run_id"],
                self.store,
                http_get=http_get,
                event_store=self.events,
            )

        self.assertEqual(self._step(updated, "acquire_mp3")["status"], "success")
        self.assertIsNone(updated["castopod_episode_id"])
        self.assertIsNone(updated["castopod_episode_url"])
        castopod_post.assert_not_called()
        self.assertEqual(http_get.call_args_list[1].args[0], broadcast["recording"]["downloadUrl"])
        self.assertEqual(http_get.call_args_list[1].kwargs["headers"], {"X-API-Key": "managed-secret"})
        self.assertNotIn("managed-secret", str(self.events.find_events(run_id=updated["run_id"])))

    def test_confirmed_broadcast_download_url_bypasses_broadcast_matching(self):
        self.store.assign_broadcast(
            self.run["run_id"],
            broadcast_id="confirmed-1",
            started_at=self.run["started_at"],
            ended_at=self.run["ended_at"],
            recording_reference="https://azuracast.example/confirmed.mp3",
        )
        http_get = Mock(
            return_value=FakeResponse(content=b"mp3-data", headers={"content-type": "audio/mpeg"})
        )

        updated = acquire_mp3_for_run(
            self.run["run_id"], self.store, http_get=http_get, event_store=self.events
        )

        self.assertEqual(self._step(updated, "acquire_mp3")["status"], "success")
        self.assertEqual(http_get.call_count, 1)
        self.assertEqual(http_get.call_args.args[0], "https://azuracast.example/confirmed.mp3")

    def test_no_matching_broadcast_fails_clearly(self):
        http_get = Mock(
            return_value=FakeResponse(
                payload=[self._broadcast("old", "2026-06-25T20:00:00Z", "2026-06-25T21:00:00Z")]
            )
        )

        updated = acquire_mp3_for_run(
            self.run["run_id"], self.store, http_get=http_get, event_store=self.events
        )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "failed")
        self.assertIn("No AzuraCast streamer broadcast matched", step["message"])

    def test_missing_api_key_fails_without_request(self):
        self.config_store.clear_api_key()
        http_get = Mock()

        updated = acquire_mp3_for_run(
            self.run["run_id"], self.store, http_get=http_get, event_store=self.events
        )

        self.assertEqual(self._step(updated, "acquire_mp3")["status"], "failed")
        self.assertIn("API key is not configured", self._step(updated, "acquire_mp3")["message"])
        http_get.assert_not_called()

    def test_api_and_download_failures_are_safe(self):
        api_failure = acquire_mp3_for_run(
            self.run["run_id"],
            self.store,
            http_get=Mock(return_value=FakeResponse(status_code=500)),
            event_store=self.events,
        )
        self.assertIn("broadcast API request failed", self._step(api_failure, "acquire_mp3")["message"])

        second = self.store.mark_stream_start(
            session_id="download-failure",
            started_at="2026-06-25T22:00:00Z",
            station="Storm Surge",
        )
        second = self.store.mark_stream_end(run_id=second["run_id"], ended_at="2026-06-25T23:00:00Z")
        broadcast = self._broadcast(
            "broadcast-2",
            recording={"path": "show.mp3", "size": 1, "downloadUrl": "https://azuracast.example/show.mp3"},
        )
        download_failure = acquire_mp3_for_run(
            second["run_id"],
            self.store,
            http_get=Mock(side_effect=[FakeResponse(payload=[broadcast]), FakeResponse(status_code=404)]),
            event_store=self.events,
        )
        self.assertIn("RSS enclosure download failed", self._step(download_failure, "acquire_mp3")["message"])

    def test_manual_upload_still_renders_required_fields(self):
        app.config["TESTING"] = True
        response = app.test_client().get("/manual-upload")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/upload"', body)
        for name in ("podcast_id", "save_as_draft", "title", "description", "audio_file"):
            self.assertIn(f'name="{name}"', body)

    def _broadcast(
        self,
        broadcast_id,
        started_at="2026-06-25T22:00:00Z",
        ended_at="2026-06-25T23:00:00Z",
        recording=None,
    ):
        return {
            "id": broadcast_id,
            "timestampStart": started_at,
            "timestampEnd": ended_at,
            "recording": recording,
        }

    def _step(self, run, step_key):
        return next(step for step in run["steps"] if step["step_key"] == step_key)


if __name__ == "__main__":
    unittest.main()
