import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from azuracast_config import (
    AzuraCastConfigStore,
    get_azuracast_api_key,
    get_azuracast_config,
)
from pipeline_logging import StructuredPipelineLogger
from pipeline_run_snapshot import export_run_snapshot
from pipeline_state import PipelineStateStore


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
        self.assertEqual(config.streamer_id, "1")
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
        self.assertIn("Station ID", body)
        self.assertIn("Streamer ID", body)
        self.assertIn("API key", body)
        self.assertIn('name="api_key"', body)
        self.assertIn("Test Connection", body)
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
                "streamer_id": "7",
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
        self.assertEqual(config.streamer_id, "7")
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
                "streamer_id": "invalid",
            },
        )

        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 400)
        self.assertIn("Base Url must start with http:// or https://.", body)
        self.assertIn("Station ID must be numeric.", body)
        self.assertIn("Streamer ID must be numeric.", body)
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

    def test_api_key_can_be_saved_updated_and_cleared_without_display(self):
        first_response = self.client.post(
            "/settings/azuracast",
            data={
                "base_url": "https://azuracast.example",
                "station_id": "1",
                "streamer_id": "1",
                "api_key": "first-managed-secret",
            },
        )
        first_body = first_response.get_data(as_text=True)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(get_azuracast_api_key(self.store), "first-managed-secret")
        self.assertTrue(self.store.get_config().api_key_configured)
        self.assertNotIn("first-managed-secret", first_body)

        self.client.post(
            "/settings/azuracast",
            data={
                "base_url": "https://azuracast.example",
                "station_id": "1",
                "streamer_id": "4",
                "api_key": "updated-managed-secret",
            },
        )
        self.assertEqual(get_azuracast_api_key(self.store), "updated-managed-secret")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AZURACAST_API_KEY", None)
            clear_response = self.client.post("/settings/azuracast/api-key/clear")
            self.assertIsNone(get_azuracast_api_key(self.store))
            self.assertFalse(self.store.get_config().api_key_configured)

        clear_body = clear_response.get_data(as_text=True)
        self.assertIn("Saved AzuraCast API key cleared", clear_body)
        self.assertNotIn("updated-managed-secret", clear_body)

    def test_managed_api_key_precedes_environment_and_environment_remains_fallback(self):
        self.store.save_config(
            {
                "base_url": "https://azuracast.example",
                "station_id": "1",
                "streamer_id": "1",
                "api_key": "managed-secret",
            }
        )

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "environment-secret"}):
            self.assertEqual(get_azuracast_api_key(self.store), "managed-secret")
            self.store.clear_api_key()
            self.assertEqual(get_azuracast_api_key(self.store), "environment-secret")

    def test_saved_api_key_is_absent_from_logs_download_events_and_config_dict(self):
        self.store.save_config(
            {
                "base_url": "https://azuracast.example",
                "station_id": "1",
                "streamer_id": "1",
                "api_key": "never-render-this-secret",
            }
        )

        settings_body = self.client.get("/settings").get_data(as_text=True)
        logs_body = self.client.get("/logs/download?detail_mode=raw").get_data(as_text=True)
        events_body = self.client.get("/api/pipeline-events").get_data(as_text=True)
        config_dict = self.store.get_config().as_dict()
        run_store = PipelineStateStore(self.db_path)
        run = run_store.mark_stream_start(session_id="secret-export-check")
        snapshot = export_run_snapshot(run["run_id"], run_store)

        for rendered in (settings_body, logs_body, events_body, str(config_dict), str(snapshot)):
            self.assertNotIn("never-render-this-secret", rendered)


if __name__ == "__main__":
    unittest.main()
