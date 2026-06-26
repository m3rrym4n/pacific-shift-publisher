import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app import app
from azuracast_config import AzuraCastConfig, AzuraCastConfigStore
from pipeline_logging import StructuredPipelineLogger
from pipeline_mp3 import (
    acquire_mp3_for_run,
    find_published_episode,
    resolve_podcast_api_url,
    select_matching_enclosure,
)
from pipeline_state import PipelineStateStore
from rss_source import RssSourceStore
from tests.test_rss_source import RSS_FIXTURE


class FakeResponse:
    def __init__(
        self,
        *,
        payload=None,
        text=None,
        content=b"",
        status_code=200,
        headers=None,
        json_error=False,
    ):
        self.payload = payload
        self.text = text if text is not None else ""
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
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        self.state_store = PipelineStateStore(self.db_path)
        self.config_store = AzuraCastConfigStore(self.db_path)
        self.rss_store = RssSourceStore(self.db_path)
        self.events = StructuredPipelineLogger(self.db_path)
        self.run = self.state_store.mark_stream_start(
            session_id="session-30",
            started_at="2026-06-25T22:00:00+00:00",
            station="Storm Surge",
            show_name="Storm Surge",
        )
        self.run = self.state_store.mark_stream_end(
            run_id=self.run["run_id"],
            ended_at="2026-06-25T23:00:00+00:00",
        )

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        self.temp_dir.cleanup()

    def configure(self):
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_shortcode": "storm_surge",
            }
        )
        self.rss_store.save_config(
            {
                "enabled": True,
                "source_name": "Storm Surge AzuraCast Podcast",
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
                "station_identifier": "storm_surge",
                "podcast_identifier": "storm-surge",
            }
        )

    def test_resolve_podcast_api_url_uses_configured_station_and_podcast(self):
        self.configure()

        url = resolve_podcast_api_url(self.config_store.get_config(), self.rss_store.get_config())

        self.assertEqual(
            url,
            "https://azuracast.example/api/station/storm_surge/podcasts/storm-surge/episodes",
        )

    def test_find_published_episode_accepts_flexible_shapes(self):
        episode = find_published_episode(
            {
                "episodes": [
                    {"id": "draft", "status": "draft", "podcast_slug": "storm-surge"},
                    {"id": "published", "status": "published", "podcast_slug": "storm-surge"},
                ]
            },
            source_config=type("Source", (), {"podcast_identifier": "storm-surge"})(),
        )

        self.assertEqual(episode["id"], "published")

    def test_matching_enclosure_prefers_newest_item_after_session_start(self):
        item = select_matching_enclosure(
            [
                {
                    "title": "Old",
                    "pub_date": "Thu, 25 Jun 2026 21:00:00 GMT",
                    "enclosure_url": "https://azuracast.example/old.mp3",
                },
                {
                    "title": "New",
                    "pub_date": "Thu, 25 Jun 2026 23:45:00 GMT",
                    "enclosure_url": "https://azuracast.example/new.mp3",
                },
            ],
            self.run,
        )

        self.assertEqual(item["title"], "New")

    def test_matching_enclosure_fails_without_time_qualified_item(self):
        item = select_matching_enclosure(
            [
                {
                    "title": "Old",
                    "pub_date": "Thu, 25 Jun 2026 21:00:00 GMT",
                    "enclosure_url": "https://azuracast.example/old.mp3",
                }
            ],
            self.run,
        )

        self.assertIsNone(item)

    def test_acquire_mp3_success_refreshes_rss_downloads_audio_and_creates_draft(self):
        self.configure()
        http_get = Mock(
            side_effect=[
                FakeResponse(payload={"episodes": [{"id": "ep-1", "status": "published", "podcast_slug": "storm-surge"}]}),
                FakeResponse(text=RSS_FIXTURE),
                FakeResponse(content=b"mp3-data", headers={"content-type": "audio/mpeg"}),
            ]
        )
        castopod_response = FakeResponse(payload={"id": 321, "url": "https://castopod.example/episodes/321"}, status_code=201)
        http_post = Mock(return_value=castopod_response)

        with patch.dict(
            os.environ,
            {
                "AZURACAST_API_KEY": "azuracast-secret",
                "CASTOPOD_URL": "https://castopod.example",
                "API_USER": "publisher",
                "API_PASS": "publisher-secret",
                "PODCAST_ID": "1",
            },
        ):
            updated = acquire_mp3_for_run(
                self.run["run_id"],
                self.state_store,
                rss_store=self.rss_store,
                http_get=http_get,
                http_post=http_post,
                event_store=self.events,
                readiness_timeout_seconds=1,
                poll_interval_seconds=1,
            )

        step = self._step(updated, "acquire_mp3")
        events = self.events.find_events(run_id=self.run["run_id"], step_key="acquire_mp3")
        event_names = [event["event_name"] for event in events]

        self.assertEqual(step["status"], "success")
        self.assertEqual(updated["castopod_episode_id"], "321")
        self.assertIn("azuracast_podcast_readiness_succeeded", event_names)
        self.assertIn("rss_source.refresh_succeeded", event_names)
        self.assertIn("rss_enclosure.match_succeeded", event_names)
        self.assertIn("acquire_mp3.download_succeeded", event_names)
        self.assertIn("acquire_mp3.validation_succeeded", event_names)
        self.assertIn("castopod_draft.create_succeeded", event_names)
        self.assertEqual(http_post.call_count, 1)
        self.assertNotIn("azuracast-secret", str(events))
        self.assertNotIn("publisher-secret", str(events))

    def test_acquire_mp3_does_not_refresh_rss_before_readiness(self):
        self.configure()
        http_get = Mock(return_value=FakeResponse(payload={"episodes": [{"id": "ep-1", "status": "draft"}]}))

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "azuracast-secret"}):
            updated = acquire_mp3_for_run(
                self.run["run_id"],
                self.state_store,
                rss_store=self.rss_store,
                http_get=http_get,
                http_post=Mock(),
                event_store=self.events,
                sleep_func=lambda _: None,
                readiness_timeout_seconds=0,
                poll_interval_seconds=1,
            )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "failed")
        self.assertEqual(http_get.call_count, 1)
        self.assertEqual(self.rss_store.get_config().last_refresh_status, None)
        self.assertIn("readiness timed out", step["message"])

    def test_acquire_mp3_fails_when_api_key_missing(self):
        self.configure()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AZURACAST_API_KEY", None)
            updated = acquire_mp3_for_run(
                self.run["run_id"],
                self.state_store,
                rss_store=self.rss_store,
                http_get=Mock(),
                http_post=Mock(),
                event_store=self.events,
            )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "failed")
        self.assertIn("AZURACAST_API_KEY is not configured", step["message"])

    def test_acquire_mp3_skips_when_rss_source_disabled(self):
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_shortcode": "storm_surge",
            }
        )
        self.rss_store.save_config(
            {
                "enabled": False,
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
            }
        )
        http_get = Mock(
            return_value=FakeResponse(payload={"episodes": [{"id": "ep-1", "status": "published"}]})
        )

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "azuracast-secret"}):
            updated = acquire_mp3_for_run(
                self.run["run_id"],
                self.state_store,
                rss_store=self.rss_store,
                http_get=http_get,
                http_post=Mock(),
                event_store=self.events,
            )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "failed")
        self.assertIn("source is disabled", step["message"])

    def test_acquire_mp3_handles_api_error(self):
        self.configure()
        http_get = Mock(return_value=FakeResponse(payload={}, status_code=500))

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "azuracast-secret"}):
            updated = acquire_mp3_for_run(
                self.run["run_id"],
                self.state_store,
                rss_store=self.rss_store,
                http_get=http_get,
                http_post=Mock(),
                event_store=self.events,
            )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "failed")
        self.assertIn("podcast API request failed", step["message"])

    def test_acquire_mp3_fails_when_no_matching_enclosure(self):
        self.configure()
        old_feed = RSS_FIXTURE.replace(
            "Thu, 25 Jun 2026 23:45:00 GMT",
            "Thu, 25 Jun 2026 21:45:00 GMT",
        )
        http_get = Mock(
            side_effect=[
                FakeResponse(payload={"episodes": [{"id": "ep-1", "status": "published", "podcast_slug": "storm-surge"}]}),
                FakeResponse(text=old_feed),
            ]
        )

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "azuracast-secret"}):
            updated = acquire_mp3_for_run(
                self.run["run_id"],
                self.state_store,
                rss_store=self.rss_store,
                http_get=http_get,
                http_post=Mock(),
                event_store=self.events,
            )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "failed")
        self.assertIn("No matching RSS enclosure", step["message"])

    def test_acquire_mp3_handles_download_failure(self):
        self.configure()
        http_get = Mock(
            side_effect=[
                FakeResponse(payload={"episodes": [{"id": "ep-1", "status": "published", "podcast_slug": "storm-surge"}]}),
                FakeResponse(text=RSS_FIXTURE),
                FakeResponse(status_code=404),
            ]
        )

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "azuracast-secret"}):
            updated = acquire_mp3_for_run(
                self.run["run_id"],
                self.state_store,
                rss_store=self.rss_store,
                http_get=http_get,
                http_post=Mock(),
                event_store=self.events,
            )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "failed")
        self.assertIn("download failed", step["message"])

    def test_acquire_mp3_handles_validation_failure(self):
        self.configure()
        http_get = Mock(
            side_effect=[
                FakeResponse(payload={"episodes": [{"id": "ep-1", "status": "published", "podcast_slug": "storm-surge"}]}),
                FakeResponse(text=RSS_FIXTURE),
                FakeResponse(content=b"", headers={"content-type": "audio/mpeg"}),
            ]
        )

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "azuracast-secret"}):
            updated = acquire_mp3_for_run(
                self.run["run_id"],
                self.state_store,
                rss_store=self.rss_store,
                http_get=http_get,
                http_post=Mock(),
                event_store=self.events,
            )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "failed")
        self.assertIn("audio asset is empty", step["message"])

    def test_manual_upload_still_renders_required_fields(self):
        app.config["TESTING"] = True
        response = app.test_client().get("/manual-upload")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="podcast_id"', body)
        self.assertIn('name="save_as_draft"', body)
        self.assertIn('name="title"', body)
        self.assertIn('name="description"', body)
        self.assertIn('name="audio_file"', body)
        self.assertIn('action="/upload"', body)

    def _step(self, run, step_key):
        return next(step for step in run["steps"] if step["step_key"] == step_key)


if __name__ == "__main__":
    unittest.main()
