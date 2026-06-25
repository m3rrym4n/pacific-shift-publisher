import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from azuracast_config import AzuraCastConfigStore, get_azuracast_config
from pipeline_logging import StructuredPipelineLogger


class AzuraCastConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.store = AzuraCastConfigStore(self.db_path)

    def tearDown(self):
        if self.original_db is None:
            os.environ.pop("PUBLISHER_STATE_DB", None)
        else:
            os.environ["PUBLISHER_STATE_DB"] = self.original_db
        self.temp_dir.cleanup()

    def test_empty_default_configuration_is_safe(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AZURACAST_API_KEY", None)
            config = self.store.get_config()

        self.assertFalse(config.enabled)
        self.assertIsNone(config.base_url)
        self.assertFalse(config.api_key_configured)

    def test_helper_reads_environment_defaults_without_secret_value(self):
        with patch.dict(
            os.environ,
            {
                "PUBLISHER_STATE_DB": self.db_path,
                "AZURACAST_ENABLED": "1",
                "AZURACAST_BASE_URL": "http://azuracast.local/",
                "AZURACAST_STATION_SHORTCODE": "storm_surge",
                "AZURACAST_STATION_NAME": "Storm Surge",
                "AZURACAST_NOWPLAYING_URL": "http://azuracast.local/api/nowplaying/storm_surge",
                "AZURACAST_PODCAST_FEED_URL": "http://azuracast.local/public/storm_surge/podcast",
                "AZURACAST_API_KEY": "super-secret-token",
            },
        ):
            config = get_azuracast_config()

        self.assertTrue(config.enabled)
        self.assertEqual(config.base_url, "http://azuracast.local")
        self.assertEqual(config.station_shortcode, "storm_surge")
        self.assertTrue(config.api_key_configured)
        self.assertNotIn("super-secret-token", str(config.as_dict()))

    def test_settings_page_renders_azuracast_section_and_masks_api_key(self):
        with patch.dict(os.environ, {"AZURACAST_API_KEY": "super-secret-token"}):
            response = self.client.get("/settings")

        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("AzuraCast", body)
        self.assertIn("AzuraCast base URL", body)
        self.assertIn("Station shortcode", body)
        self.assertIn("API key", body)
        self.assertIn("Configured", body)
        self.assertNotIn("super-secret-token", body)

    def test_non_secret_settings_save_and_reload(self):
        response = self.client.post(
            "/settings/azuracast",
            data={
                "enabled": "1",
                "base_url": "http://192.168.1.68/",
                "station_shortcode": "storm_surge",
                "station_id": "1",
                "station_name": "Storm Surge",
                "nowplaying_url": "http://192.168.1.68/api/nowplaying/storm_surge",
                "podcast_feed_url": "http://192.168.1.68/public/storm_surge/podcast",
            },
        )
        config = self.store.get_config()
        reload_response = self.client.get("/settings")
        reload_body = reload_response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(config.enabled)
        self.assertEqual(config.base_url, "http://192.168.1.68")
        self.assertEqual(config.station_shortcode, "storm_surge")
        self.assertEqual(config.station_id, "1")
        self.assertEqual(config.station_name, "Storm Surge")
        self.assertIn("AzuraCast settings saved.", response.get_data(as_text=True))
        self.assertIn('value="http://192.168.1.68"', reload_body)
        self.assertIn('value="storm_surge"', reload_body)

    def test_validation_rejects_invalid_enabled_configuration(self):
        response = self.client.post(
            "/settings/azuracast",
            data={
                "enabled": "1",
                "base_url": "not-a-url",
                "station_shortcode": "storm surge!",
                "station_id": "abc",
            },
        )

        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 400)
        self.assertIn("Base Url must start with http:// or https://.", body)
        self.assertIn("Station ID must be numeric.", body)
        self.assertIn("Station shortcode may only contain", body)

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

    def test_secret_value_is_not_written_to_structured_events(self):
        with patch.dict(os.environ, {"AZURACAST_API_KEY": "super-secret-token"}):
            self.client.post(
                "/settings/azuracast",
                data={
                    "enabled": "1",
                    "base_url": "http://192.168.1.68",
                    "station_shortcode": "storm_surge",
                },
            )

        events = StructuredPipelineLogger(self.db_path).find_events()
        self.assertNotIn("super-secret-token", str(events))


if __name__ == "__main__":
    unittest.main()
