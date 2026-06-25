import unittest
from io import BytesIO
from unittest.mock import Mock, patch

from app import app
from navigation import get_navigation


class PublisherShellTest(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_navigation_config_contains_required_items(self):
        labels = [item.label for item in get_navigation()]

        self.assertEqual(
            labels,
            ["Dashboard", "Runs", "Logs", "Manual Upload", "Settings"],
        )

    def test_manual_upload_alias_preserves_form_and_post_target(self):
        response = self.client.get("/manual-upload")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Manual Upload", body)
        self.assertIn('action="/upload"', body)
        self.assertIn('name="podcast_id"', body)
        self.assertIn('name="title"', body)
        self.assertIn('name="description"', body)
        self.assertIn('name="audio_file"', body)
        self.assertIn('name="save_as_draft"', body)

    def test_root_redirects_to_dashboard(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard")

    def test_root_follow_redirect_renders_dashboard(self):
        response = self.client.get("/", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Dashboard", response.get_data(as_text=True))

    def test_dashboard_renders_sidebar_from_navigation(self):
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Pacific Shift Publisher", body)
        self.assertIn("Dashboard", body)
        self.assertIn("Runs", body)
        self.assertIn("Logs", body)
        self.assertIn("Manual Upload", body)
        self.assertIn("Settings", body)

    def test_active_navigation_marks_manual_upload_alias(self):
        response = self.client.get("/manual-upload")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('<li class="nav-item active">', body)

    def test_active_navigation_marks_dashboard(self):
        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('<li class="nav-item active">', body)

    def test_placeholder_routes_render(self):
        for path in ["/dashboard", "/runs", "/logs", "/settings"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_local_tabler_assets_are_served(self):
        css_response = self.client.get("/static/vendor/tabler/1.4.0/css/tabler.min.css")
        js_response = self.client.get("/static/vendor/tabler/1.4.0/js/tabler.min.js")

        self.assertEqual(css_response.status_code, 200)
        self.assertEqual(js_response.status_code, 200)

    def test_healthz_is_preserved(self):
        response = self.client.get("/healthz")

        self.assertIn(response.status_code, (200, 500))
        self.assertIn("status", response.get_json())

    def test_latest_pipeline_run_endpoint_returns_stable_empty_shape(self):
        response = self.client.get("/api/pipeline-runs/latest")

        self.assertEqual(response.status_code, 200)
        self.assertIn("run", response.get_json())

    def test_pipeline_events_endpoint_returns_stable_shape(self):
        response = self.client.get("/api/pipeline-events")

        self.assertEqual(response.status_code, 200)
        self.assertIn("events", response.get_json())

    def test_upload_validation_behavior_is_preserved(self):
        response = self.client.post(
            "/upload",
            data={"title": "", "description": ""},
        )

        self.assertEqual(response.status_code, 500)
        self.assertIn(
            "Publisher is missing required configuration",
            response.get_data(as_text=True),
        )

    def test_save_as_draft_skips_publish_call(self):
        episode_response = Mock(status_code=201)
        episode_response.json.return_value = {"id": 123}

        with patch("app.CASTOPOD_URL", "http://castopod:8080"), \
                patch("app.API_USER", "user"), \
                patch("app.API_PASS", "pass"), \
                patch("app.requests.post", return_value=episode_response) as post:
            response = self.client.post(
                "/upload",
                data={
                    "podcast_id": "1",
                    "title": "Draft Episode",
                    "description": "Draft description",
                    "save_as_draft": "1",
                    "audio_file": (BytesIO(b"mp3"), "episode.mp3"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_count, 1)
        self.assertIn("saved as draft", response.get_data(as_text=True))

    def test_publish_path_still_calls_publish_endpoint(self):
        episode_response = Mock(status_code=201)
        episode_response.json.return_value = {"id": 123}
        publish_response = Mock(status_code=200, text="published")

        with patch("app.CASTOPOD_URL", "http://castopod:8080"), \
                patch("app.API_USER", "user"), \
                patch("app.API_PASS", "pass"), \
                patch("app.requests.post", side_effect=[episode_response, publish_response]) as post:
            response = self.client.post(
                "/upload",
                data={
                    "podcast_id": "1",
                    "title": "Published Episode",
                    "description": "Publish description",
                    "audio_file": (BytesIO(b"mp3"), "episode.mp3"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_count, 2)
        self.assertIn("/episodes/123/publish", post.call_args_list[1].args[0])
        self.assertIn("uploaded and published", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
