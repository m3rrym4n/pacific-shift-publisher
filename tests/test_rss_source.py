import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app import app
from pipeline_logging import StructuredPipelineLogger
from rss_source import RssSourceStore, parse_rss_feed, refresh_rss_source


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Storm Surge Podcast</title>
    <item>
      <title>Storm Surge 2026-06-25</title>
      <guid isPermaLink="false">episode-20260625</guid>
      <pubDate>Thu, 25 Jun 2026 23:45:00 GMT</pubDate>
      <enclosure url="https://azuracast.example/media/storm-surge-20260625.mp3"
                 type="audio/mpeg"
                 length="12345678" />
    </item>
    <item>
      <title>Storm Surge 2026-06-18</title>
      <guid isPermaLink="false">episode-20260618</guid>
      <pubDate>Thu, 18 Jun 2026 23:45:00 GMT</pubDate>
      <enclosure url="https://azuracast.example/media/storm-surge-20260618.mp3"
                 type="audio/mpeg"
                 length="23456789" />
    </item>
  </channel>
</rss>
"""


class FakeResponse:
    def __init__(self, text=RSS_FIXTURE, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError("request failed")
            error.response = self
            raise error


class RssSourceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.store = RssSourceStore(self.db_path)

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        self.temp_dir.cleanup()

    def test_source_configuration_save_and_load(self):
        config, errors = self.store.save_config(
            {
                "enabled": True,
                "source_name": "Storm Surge AzuraCast Podcast",
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
                "station_identifier": "storm_surge",
                "podcast_identifier": "storm-surge",
            }
        )
        loaded = self.store.get_config()

        self.assertEqual(errors, [])
        self.assertTrue(config.enabled)
        self.assertEqual(loaded.source_name, "Storm Surge AzuraCast Podcast")
        self.assertEqual(loaded.feed_url, "https://azuracast.example/public/storm_surge/podcast")
        self.assertEqual(loaded.station_identifier, "storm_surge")
        self.assertEqual(loaded.podcast_identifier, "storm-surge")

    def test_settings_source_route_renders_and_saves(self):
        response = self.client.post(
            "/settings/source",
            data={
                "enabled": "1",
                "source_name": "Storm Surge AzuraCast Podcast",
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
                "station_identifier": "storm_surge",
                "podcast_identifier": "storm-surge",
            },
        )
        reload_response = self.client.get("/settings/source")
        body = reload_response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(reload_response.status_code, 200)
        self.assertIn("AzuraCast Podcast RSS Source", body)
        self.assertIn("Storm Surge AzuraCast Podcast", body)
        self.assertIn("Refresh RSS Feed", body)

    def test_invalid_feed_url_is_rejected(self):
        response = self.client.post(
            "/settings/source",
            data={"enabled": "1", "feed_url": "not-a-url"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("RSS feed URL must start with http:// or https://.", response.get_data(as_text=True))

    def test_parse_rss_feed_extracts_items_and_enclosures(self):
        items = parse_rss_feed(RSS_FIXTURE)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Storm Surge 2026-06-25")
        self.assertEqual(items[0].pub_date, "Thu, 25 Jun 2026 23:45:00 GMT")
        self.assertEqual(items[0].guid, "episode-20260625")
        self.assertEqual(items[0].enclosure_url, "https://azuracast.example/media/storm-surge-20260625.mp3")
        self.assertEqual(items[0].enclosure_type, "audio/mpeg")
        self.assertEqual(items[0].enclosure_length, "12345678")

    def test_parse_failure_does_not_crash(self):
        self.store.save_config(
            {
                "enabled": True,
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
            }
        )

        result = refresh_rss_source(
            store=self.store,
            http_get=Mock(return_value=FakeResponse("<rss><broken")),
            event_store=StructuredPipelineLogger(self.db_path),
        )
        config = self.store.get_config()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(config.last_refresh_status, "failed")
        self.assertIn("feed XML could not be parsed", config.last_error_message)

    def test_refresh_success_updates_status_items_and_logs(self):
        self.store.save_config(
            {
                "enabled": True,
                "source_name": "Storm Surge AzuraCast Podcast",
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
                "station_identifier": "storm_surge",
                "podcast_identifier": "storm-surge",
            }
        )
        logger = StructuredPipelineLogger(self.db_path)

        result = refresh_rss_source(
            store=self.store,
            http_get=Mock(return_value=FakeResponse()),
            event_store=logger,
        )
        config = self.store.get_config()
        items = self.store.list_items()
        events = logger.find_events(event_name="rss_source.refresh_succeeded")

        self.assertTrue(result["ok"])
        self.assertEqual(config.last_refresh_status, "success")
        self.assertEqual(config.latest_item_title, "Storm Surge 2026-06-25")
        self.assertEqual(config.latest_enclosure_url, "https://azuracast.example/media/storm-surge-20260625.mp3")
        self.assertEqual(len(items), 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["step_key"], "acquire_mp3")
        self.assertEqual(events[0]["details"]["item_count"], 2)
        self.assertEqual(events[0]["details"]["latest_enclosure_type"], "audio/mpeg")

    def test_refresh_fetch_failure_updates_status_and_logs(self):
        self.store.save_config(
            {
                "enabled": True,
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
                "station_identifier": "storm_surge",
            }
        )
        http_get = Mock(side_effect=requests.Timeout("timeout"))
        logger = StructuredPipelineLogger(self.db_path)

        result = refresh_rss_source(store=self.store, http_get=http_get, event_store=logger)
        config = self.store.get_config()
        events = logger.find_events(event_name="rss_source.refresh_failed")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(config.last_refresh_status, "failed")
        self.assertIn("Timeout", config.last_error_message)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")

    def test_refresh_action_renders_latest_item(self):
        self.store.save_config(
            {
                "enabled": True,
                "feed_url": "https://azuracast.example/public/storm_surge/podcast",
            }
        )

        with patch("rss_source.requests.get", return_value=FakeResponse()):
            response = self.client.post("/settings/source/refresh")

        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("RSS source refresh succeeded with 2 items.", body)
        self.assertIn("Storm Surge 2026-06-25", body)
        self.assertIn("https://azuracast.example/media/storm-surge-20260625.mp3", body)

    def test_manual_upload_still_renders_required_fields(self):
        response = self.client.get("/manual-upload")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="podcast_id"', body)
        self.assertIn('name="save_as_draft"', body)
        self.assertIn('name="title"', body)
        self.assertIn('name="description"', body)
        self.assertIn('name="audio_file"', body)
        self.assertIn('action="/upload"', body)


if __name__ == "__main__":
    unittest.main()
