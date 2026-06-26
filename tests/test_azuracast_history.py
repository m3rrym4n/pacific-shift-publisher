import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from azuracast_config import AzuraCastConfig, AzuraCastConfigStore
from azuracast_history import (
    generate_tracklist_for_run,
    load_azuracast_history,
    prepare_description_with_tracklist,
    resolve_nowplaying_history_url,
)
from pipeline_state import PipelineStateStore


class FakeResponse:
    def __init__(self, payload=None, status_code=200, json_error=False):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError("failed")
            error.response = self
            raise error

    def json(self):
        if self.json_error:
            raise ValueError("bad json")
        return self.payload


class AzuraCastHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        self.state_store = PipelineStateStore(self.db_path)
        self.config_store = AzuraCastConfigStore(self.db_path)

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        self.temp_dir.cleanup()

    def test_endpoint_override_is_honored(self):
        config = AzuraCastConfig(
            enabled=True,
            base_url="https://azuracast.example",
            station_shortcode="storm_surge",
            nowplaying_url="https://cdn.example/history.json",
        )

        self.assertEqual(resolve_nowplaying_history_url(config), "https://cdn.example/history.json")

    def test_endpoint_derived_from_configured_base_url_and_shortcode(self):
        config = AzuraCastConfig(
            enabled=True,
            base_url="https://azuracast.example/",
            station_shortcode="storm_surge",
        )

        self.assertEqual(
            resolve_nowplaying_history_url(config),
            "https://azuracast.example/api/nowplaying_static/storm_surge.json",
        )

    def test_endpoint_derived_from_station_id_when_shortcode_missing(self):
        config = AzuraCastConfig(
            enabled=True,
            base_url="https://azuracast.example",
            station_id="1",
        )

        self.assertEqual(
            resolve_nowplaying_history_url(config),
            "https://azuracast.example/api/nowplaying/1",
        )

    def test_missing_or_disabled_config_returns_safe_error(self):
        result = load_azuracast_history(config=AzuraCastConfig(enabled=False))

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "AzuraCast integration is disabled.")

    def test_history_client_uses_config_helper_and_fetches_json(self):
        http_get = Mock(return_value=FakeResponse(payload={"song_history": []}))
        with patch(
            "azuracast_history.get_azuracast_config",
            return_value=AzuraCastConfig(
                enabled=True,
                base_url="https://azuracast.example",
                station_shortcode="storm_surge",
            ),
        ) as config_loader:
            result = load_azuracast_history(http_get=http_get)

        self.assertTrue(result.ok)
        config_loader.assert_called_once()
        http_get.assert_called_once_with(
            "https://azuracast.example/api/nowplaying_static/storm_surge.json",
            headers={},
            timeout=15,
        )

    def test_optional_api_key_is_sent_but_not_in_error(self):
        with patch.dict(os.environ, {"AZURACAST_API_KEY": "super-secret-token"}):
            http_get = Mock(return_value=FakeResponse(status_code=500, payload={}))
            result = load_azuracast_history(
                config=AzuraCastConfig(
                    enabled=True,
                    base_url="https://azuracast.example",
                    station_shortcode="storm_surge",
                ),
                http_get=http_get,
            )

        self.assertFalse(result.ok)
        self.assertEqual(http_get.call_args.kwargs["headers"], {"Authorization": "Bearer super-secret-token"})
        self.assertNotIn("super-secret-token", result.error)

    def test_http_failure_and_invalid_json_are_safe(self):
        config = AzuraCastConfig(
            enabled=True,
            base_url="https://azuracast.example",
            station_shortcode="storm_surge",
        )
        http_failure = load_azuracast_history(
            config=config,
            http_get=Mock(return_value=FakeResponse(status_code=503, payload={})),
        )
        bad_json = load_azuracast_history(
            config=config,
            http_get=Mock(return_value=FakeResponse(json_error=True)),
        )

        self.assertFalse(http_failure.ok)
        self.assertEqual(http_failure.status_code, 503)
        self.assertFalse(bad_json.ok)
        self.assertIn("not valid JSON", bad_json.error)

    def test_generate_tracklist_for_completed_run(self):
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_shortcode": "storm_surge",
            }
        )
        run = self.state_store.mark_stream_start(
            session_id="session-72",
            started_at="2026-06-20T06:00:00+00:00",
            station="Storm Surge",
        )
        run = self.state_store.mark_stream_end(
            run_id=run["run_id"],
            ended_at="2026-06-20T06:05:00+00:00",
        )
        http_get = Mock(return_value=FakeResponse(payload=self.history_payload()))

        result = generate_tracklist_for_run(run["run_id"], store=self.state_store, http_get=http_get)

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["tracklist"],
            "Tracklist\n\n0:00:00 Start - Boundary\n0:03:00 Middle - Track\n0:05:00 End - Boundary",
        )
        self.assertEqual(len(result["tracks"]), 3)
        self.assertEqual(result["track_count_total"], 5)
        self.assertEqual(result["track_count_filtered"], 3)

    def test_generate_tracklist_includes_overlapping_opener_and_startup_grace(self):
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_shortcode": "storm_surge",
            }
        )
        run = self.state_store.mark_stream_start(
            session_id="session-opener",
            started_at="2026-06-20T06:00:00+00:00",
            station="Storm Surge",
        )
        run = self.state_store.mark_stream_end(
            run_id=run["run_id"],
            ended_at="2026-06-20T06:05:00+00:00",
        )
        http_get = Mock(
            return_value=FakeResponse(
                payload={
                    "song_history": [
                        {"played_at": 1781935180, "duration": 90, "song": {"artist": "Known", "title": "Opener"}},
                        {"played_at": 1781935223, "song": {"artist": "First", "title": "Inside Grace"}},
                        {"played_at": 1781935283, "song": {"artist": "Second", "title": "Normal Offset"}},
                    ]
                }
            )
        )

        result = generate_tracklist_for_run(run["run_id"], store=self.state_store, http_get=http_get)

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["tracklist"],
            "Tracklist\n\n0:00:00 Known - Opener\n0:00:23 First - Inside Grace\n0:01:23 Second - Normal Offset",
        )
        self.assertEqual([track["display"] for track in result["tracks"]], [
            "Known - Opener",
            "First - Inside Grace",
            "Second - Normal Offset",
        ])

    def test_generate_tracklist_clamps_first_public_track_inside_startup_grace(self):
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_shortcode": "storm_surge",
            }
        )
        run = self.state_store.mark_stream_start(
            session_id="session-grace",
            started_at="2026-06-20T06:00:00+00:00",
        )
        run = self.state_store.mark_stream_end(run_id=run["run_id"], ended_at="2026-06-20T06:05:00+00:00")
        http_get = Mock(
            return_value=FakeResponse(
                payload={
                    "song_history": [
                        {"played_at": 1781935223, "song": {"artist": "First", "title": "Inside Grace"}},
                        {"played_at": 1781935283, "song": {"artist": "Second", "title": "Normal Offset"}},
                    ]
                }
            )
        )

        result = generate_tracklist_for_run(run["run_id"], store=self.state_store, http_get=http_get)

        self.assertEqual(
            result["tracklist"],
            "Tracklist\n\n0:00:00 First - Inside Grace\n0:01:23 Second - Normal Offset",
        )

    def test_generate_tracklist_requires_completed_window(self):
        run = self.state_store.mark_stream_start(session_id="open-session")

        result = generate_tracklist_for_run(
            run["run_id"],
            store=self.state_store,
            config=AzuraCastConfig(enabled=True, base_url="https://azuracast.example", station_shortcode="storm_surge"),
            http_get=Mock(return_value=FakeResponse(payload=self.history_payload())),
        )

        self.assertFalse(result["ok"])
        self.assertIn("completed session window", result["error"])
        self.assertIn("No AzuraCast track history", result["tracklist"])

    def test_prepare_description_appends_tracklist(self):
        self.assertEqual(
            prepare_description_with_tracklist("Existing", "Tracklist\n\n01. A - B"),
            "Existing\n\nTracklist\n\n01. A - B",
        )

    def history_payload(self):
        return {
            "song_history": [
                {"played_at": 1781935560, "song": {"artist": "Too New", "title": "Outside"}},
                {"played_at": 1781935500, "song": {"artist": "End", "title": "Boundary"}},
                {"played_at": 1781935380, "song": {"text": "Middle - Track"}},
                {"played_at": 1781935200, "song": {"artist": "Start", "title": "Boundary"}},
                {"played_at": 1781935100, "song": {"artist": "Too Old", "title": "Outside"}},
            ]
        }


if __name__ == "__main__":
    unittest.main()
