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
    derive_podcast_id,
    find_published_episode,
    resolve_podcast_api_url,
    select_matching_enclosure,
    wait_for_podcast_readiness,
)
from pipeline_state import PipelineStateStore
from rss_source import RssSourceStore


PODCAST_ID = "1f1712f1-14a4-6b16-b7b6-8b09cdf2c9b3"


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
                "station_id": "1",
            }
        )
        self.rss_store.save_config(
            {
                "enabled": True,
                "source_name": "Storm Surge AzuraCast Podcast",
                "feed_url": f"https://azuracast.example/api/station/storm_surge/public/podcast/{PODCAST_ID}/episodes",
                "station_identifier": "storm_surge",
                "podcast_identifier": "storm-surge",
            }
        )

    def test_resolve_podcast_api_url_uses_station_id_and_derived_podcast_id(self):
        self.configure()

        url = resolve_podcast_api_url(self.config_store.get_config(), self.rss_store.get_config())

        self.assertEqual(
            url,
            f"https://azuracast.example/api/station/1/podcast/{PODCAST_ID}/episodes",
        )
        self.assertNotIn("/podcasts/storm_surge/episodes", url)
        self.assertNotIn("/podcasts/storm-surge/episodes", url)

    def test_derive_podcast_id_from_rss_feed_url(self):
        self.rss_store.save_config(
            {
                "enabled": True,
                "feed_url": f"https://azuracast.example/api/station/storm_surge/public/podcast/{PODCAST_ID}/episodes",
                "podcast_identifier": "storm-surge",
            }
        )

        self.assertEqual(derive_podcast_id(self.rss_store.get_config()), PODCAST_ID)

    def test_resolve_podcast_api_url_rejects_missing_podcast_id(self):
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_id": "1",
                "station_shortcode": "storm_surge",
            }
        )
        self.rss_store.save_config(
            {
                "enabled": True,
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
                "podcast_identifier": "storm-surge",
            }
        )

        with self.assertRaises(ValueError) as context:
            resolve_podcast_api_url(self.config_store.get_config(), self.rss_store.get_config())

        self.assertIn("podcast ID could not be derived", str(context.exception))

    def test_find_published_episode_accepts_flexible_shapes(self):
        episode = find_published_episode(
            {
                "episodes": [
                    {"id": "draft", "is_published": False, "has_media": True, "podcast_id": PODCAST_ID},
                    {"id": "published", "is_published": True, "has_media": True, "links": {"download": "https://example.test/file.mp3"}, "podcast_id": PODCAST_ID},
                ]
            },
            source_config=type("Source", (), {"podcast_identifier": PODCAST_ID, "feed_url": None})(),
        )

        self.assertEqual(episode["id"], "published")

    def test_readiness_fails_when_episode_is_not_published(self):
        self.configure()
        http_get = Mock(return_value=FakeResponse(payload={"episodes": [{"id": "draft", "is_published": False, "has_media": True, "podcast_id": PODCAST_ID}]}))

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "azuracast-secret"}):
            result = wait_for_podcast_readiness(
                self.run,
                config=self.config_store.get_config(),
                source_config=self.rss_store.get_config(),
                http_get=http_get,
                event_store=self.events,
                timeout_seconds=0,
                poll_interval_seconds=1,
                sleep_func=lambda _: None,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["details"]["readiness_decision"], "not_ready")
        self.assertFalse(result["details"]["candidate_episodes"][0]["is_published"])

    def test_readiness_fails_when_episode_has_no_media(self):
        self.configure()
        http_get = Mock(return_value=FakeResponse(payload={"episodes": [{"id": "no-media", "is_published": True, "has_media": False, "podcast_id": PODCAST_ID}]}))

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "azuracast-secret"}):
            result = wait_for_podcast_readiness(
                self.run,
                config=self.config_store.get_config(),
                source_config=self.rss_store.get_config(),
                http_get=http_get,
                event_store=self.events,
                timeout_seconds=0,
                poll_interval_seconds=1,
                sleep_func=lambda _: None,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["details"]["readiness_decision"], "not_ready")
        self.assertFalse(result["details"]["candidate_episodes"][0]["has_media"])

    def test_readiness_accepts_scoped_episode_without_podcast_id(self):
        self.configure()
        http_get = Mock(
            return_value=FakeResponse(
                payload={
                    "episodes": [
                        {
                            "id": "scoped-episode",
                            "title": "Scoped Episode",
                            "is_published": True,
                            "has_media": True,
                        }
                    ]
                }
            )
        )

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "azuracast-secret"}):
            result = wait_for_podcast_readiness(
                self.run,
                config=self.config_store.get_config(),
                source_config=self.rss_store.get_config(),
                http_get=http_get,
                event_store=self.events,
                timeout_seconds=0,
                poll_interval_seconds=1,
                sleep_func=lambda _: None,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["details"]["selected_episode_id"], "scoped-episode")
        self.assertEqual(result["details"]["readiness_decision"], "published_with_media")
        self.assertIsNone(result["details"]["podcast_episode_download_url"])

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

    def test_acquire_mp3_success_downloads_episode_url_and_creates_draft_without_rss_refresh(self):
        self.configure()
        http_get = Mock(
            side_effect=[
                FakeResponse(payload={"episodes": [{"id": "ep-1", "title": "Storm Surge Episode", "is_published": True, "has_media": True, "links": {"download": "https://azuracast.example/download.mp3"}, "podcast_id": PODCAST_ID}]}),
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
        self.assertNotIn("rss_source.refresh_succeeded", event_names)
        self.assertNotIn("rss_enclosure.match_succeeded", event_names)
        self.assertIn("acquire_mp3.download_succeeded", event_names)
        self.assertIn("acquire_mp3.validation_succeeded", event_names)
        self.assertIn("castopod_draft.create_succeeded", event_names)
        readiness_event = next(event for event in events if event["event_name"] == "azuracast_podcast_readiness_succeeded")
        self.assertEqual(readiness_event["details"]["derived_podcast_id"], PODCAST_ID)
        self.assertEqual(readiness_event["details"]["readiness_decision"], "published_with_media")
        self.assertEqual(readiness_event["details"]["episodes_returned"], 1)
        self.assertEqual(readiness_event["details"]["selected_episode_id"], "ep-1")
        self.assertEqual(
            readiness_event["details"]["podcast_episode_download_url"],
            "https://azuracast.example/download.mp3",
        )
        self.assertEqual(http_get.call_args_list[1].args[0], "https://azuracast.example/download.mp3")
        self.assertIsNone(self.rss_store.get_config().last_refresh_status)
        self.assertEqual(http_post.call_count, 1)
        self.assertNotIn("azuracast-secret", str(events))
        self.assertNotIn("publisher-secret", str(events))

    def test_acquire_mp3_does_not_refresh_rss_before_readiness(self):
        self.configure()
        http_get = Mock(return_value=FakeResponse(payload={"episodes": [{"id": "ep-1", "is_published": False, "has_media": True, "podcast_id": PODCAST_ID}]}))

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

    def test_acquire_mp3_does_not_require_rss_source_to_be_enabled(self):
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_shortcode": "storm_surge",
                "station_id": "1",
            }
        )
        self.rss_store.save_config(
            {
                "enabled": False,
                "feed_url": f"https://azuracast.example/api/station/storm_surge/public/podcast/{PODCAST_ID}/episodes",
            }
        )
        http_get = Mock(
            side_effect=[
                FakeResponse(payload={"episodes": [{"id": "ep-1", "is_published": True, "has_media": True, "links": {"download": "https://azuracast.example/download.mp3"}, "podcast_id": PODCAST_ID}]}),
                FakeResponse(content=b"mp3-data", headers={"content-type": "audio/mpeg"}),
            ]
        )
        http_post = Mock(return_value=FakeResponse(payload={"id": 321}, status_code=201))

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
            )

        step = self._step(updated, "acquire_mp3")
        self.assertEqual(step["status"], "success")
        self.assertIsNone(self.rss_store.get_config().last_refresh_status)

    def test_acquire_mp3_handles_api_error(self):
        self.configure()
        http_get = Mock(return_value=FakeResponse(payload={}, text="server exploded", status_code=500, headers={"content-type": "application/json"}))

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
        events = self.events.find_events(run_id=self.run["run_id"], step_key="acquire_mp3")
        failed = next(event for event in events if event["event_name"] == "azuracast_podcast_readiness_failed")
        self.assertEqual(failed["details"]["http_status_code"], 500)
        self.assertEqual(failed["details"]["candidate_endpoint"], f"https://azuracast.example/api/station/1/podcast/{PODCAST_ID}/episodes")
        self.assertIn("server exploded", failed["details"]["response_body_snippet"])

    def test_acquire_mp3_fails_when_ready_episode_has_no_download_url(self):
        self.configure()
        http_get = Mock(
            return_value=FakeResponse(payload={"episodes": [{"id": "ep-1", "is_published": True, "has_media": True, "podcast_id": PODCAST_ID}]})
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
        self.assertIn("No download URL in episode response", step["message"])
        self.assertEqual(http_get.call_count, 1)
        self.assertIsNone(self.rss_store.get_config().last_refresh_status)

    def test_acquire_mp3_handles_download_failure(self):
        self.configure()
        http_get = Mock(
            side_effect=[
                FakeResponse(payload={"episodes": [{"id": "ep-1", "is_published": True, "has_media": True, "links": {"download": "https://azuracast.example/download.mp3"}, "podcast_id": PODCAST_ID}]}),
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
                FakeResponse(payload={"episodes": [{"id": "ep-1", "is_published": True, "has_media": True, "links": {"download": "https://azuracast.example/download.mp3"}, "podcast_id": PODCAST_ID}]}),
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
