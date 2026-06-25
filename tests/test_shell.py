import unittest

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
        self.assertIn('name="title"', body)
        self.assertIn('name="description"', body)
        self.assertIn('name="audio_file"', body)

    def test_root_preserves_existing_manual_upload_form(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('action="/upload"', response.get_data(as_text=True))

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


if __name__ == "__main__":
    unittest.main()
