import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app import app
from azuracast_config import AzuraCastConfigStore
from azuracast_connection import test_azuracast_connection as check_connection


class FakeResponse:
    def __init__(self, payload=None, status_code=200, json_error=False):
        self.payload = payload if payload is not None else []
        self.status_code = status_code
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise ValueError("invalid JSON")
        return self.payload


class AzuraCastConnectionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.original_db = os.environ.get("PUBLISHER_STATE_DB")
        self.original_api_key = os.environ.get("AZURACAST_API_KEY")
        os.environ["PUBLISHER_STATE_DB"] = self.db_path
        os.environ.pop("AZURACAST_API_KEY", None)
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.store = AzuraCastConfigStore(self.db_path)

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

    def configure(self, **overrides):
        values = {
            "enabled": True,
            "base_url": "https://azuracast.example",
            "station_id": "12",
            "streamer_id": "34",
            "api_key": "managed-api-secret",
        }
        values.update(overrides)
        self.store.save_config(values)

    def test_connection_success_uses_station_streamer_and_managed_key(self):
        self.configure()
        http_get = Mock(return_value=FakeResponse(payload=[]))

        with patch.dict(os.environ, {"AZURACAST_API_KEY": "environment-secret"}):
            result = check_connection(store=self.store, http_get=http_get)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(
            http_get.call_args.args[0],
            "https://azuracast.example/api/station/12/streamer/34/broadcasts",
        )
        self.assertEqual(http_get.call_args.kwargs["headers"]["X-API-Key"], "managed-api-secret")
        self.assertNotIn("Authorization", http_get.call_args.kwargs["headers"])
        self.assertNotIn("managed-api-secret", str(result))
        config = self.store.get_config()
        self.assertIsNotNone(config.last_successful_check_at)
        self.assertEqual(config.last_check_message, "AzuraCast API connection succeeded.")

    def test_connection_route_reports_success_without_exposing_key(self):
        self.configure()
        with patch("azuracast_connection.requests.get", return_value=FakeResponse(payload=[])):
            response = self.client.post("/settings/azuracast/test")

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("AzuraCast API connection succeeded.", body)
        self.assertNotIn("managed-api-secret", body)

    def test_connection_reports_missing_base_url_station_and_api_key(self):
        missing_base = check_connection(store=self.store, http_get=Mock())
        self.assertEqual(missing_base["status"], "missing_base_url")

        self.store.save_config({"base_url": "https://azuracast.example", "streamer_id": "1"})
        missing_station = check_connection(store=self.store, http_get=Mock())
        self.assertEqual(missing_station["status"], "missing_station")

        self.store.save_config(
            {
                "base_url": "https://azuracast.example",
                "station_id": "1",
                "streamer_id": "1",
            }
        )
        missing_key = check_connection(store=self.store, http_get=Mock())
        self.assertEqual(missing_key["status"], "missing_api_key")

    def test_connection_reports_authentication_network_api_and_unexpected_failures(self):
        self.configure()

        authentication = check_connection(
            store=self.store,
            http_get=Mock(return_value=FakeResponse(status_code=401)),
        )
        network = check_connection(
            store=self.store,
            http_get=Mock(side_effect=requests.ConnectionError("offline")),
        )
        api_failure = check_connection(
            store=self.store,
            http_get=Mock(return_value=FakeResponse(status_code=500)),
        )
        unexpected = check_connection(
            store=self.store,
            http_get=Mock(return_value=FakeResponse(json_error=True)),
        )

        self.assertEqual(authentication["status"], "authentication_failed")
        self.assertEqual(network["status"], "network_error")
        self.assertEqual(api_failure["status"], "api_error")
        self.assertEqual(unexpected["status"], "unexpected_response")
        self.assertNotIn("managed-api-secret", str([authentication, network, api_failure, unexpected]))


if __name__ == "__main__":
    unittest.main()
