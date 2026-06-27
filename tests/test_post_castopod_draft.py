import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from azuracast_config import AzuraCastConfigStore
from pipeline_logging import StructuredPipelineLogger
from pipeline_retry import can_retry_run
from pipeline_state import PipelineStateStore
from post_castopod_draft import (
    generate_episode_slug,
    parse_castopod_error_body,
    post_castopod_draft_for_run,
)


class AudioResponse:
    status_code = 200
    headers = {"content-type": "audio/mpeg"}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1):
        yield b"mp3-data"


class JsonResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class PostCastopodDraftTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "publisher_state.sqlite")
        self.store = PipelineStateStore(self.db_path)
        self.events = StructuredPipelineLogger(self.db_path)
        self.config_store = AzuraCastConfigStore(self.db_path)
        self.config_store.save_config(
            {
                "enabled": True,
                "base_url": "https://azuracast.example",
                "station_id": "1",
                "streamer_id": "1",
                "api_key": "managed-secret",
            }
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_http_201_succeeds_and_completes_run(self):
        self._assert_success_status(201)

    def test_http_200_succeeds_and_completes_run(self):
        self._assert_success_status(200)

    def _assert_success_status(self, status_code):
        run = self._assembled_run()
        http_get = Mock(return_value=AudioResponse())
        draft_result = {
            "ok": False,
            "episode_id": "episode-101",
            "episode_url": "https://castopod.example/episodes/101",
            "status_code": status_code,
        }

        with patch(
            "post_castopod_draft.create_castopod_draft_episode",
            return_value=draft_result,
        ) as create_draft:
            posted = post_castopod_draft_for_run(
                run["run_id"],
                self.store,
                http_get=http_get,
                http_post=Mock(),
                event_store=self.events,
            )

        self.assertEqual(self._step(posted)["status"], "success")
        self.assertEqual(posted["overall_status"], "success")
        self.assertEqual(posted["castopod_episode_id"], "episode-101")
        self.assertEqual(
            posted["castopod_episode_url"],
            "https://castopod.example/episodes/101",
        )
        self.assertEqual(http_get.call_args.kwargs["headers"], {"X-API-Key": "managed-secret"})
        self.assertEqual(create_draft.call_args.kwargs["title"], "Storm Surge 20260627")
        self.assertIn("0:00:00 Artist - Track", create_draft.call_args.kwargs["description"])

    def test_http_409_reconciles_existing_episode_by_slug(self):
        run = self._assembled_run()
        http_get = Mock(
            side_effect=[
                AudioResponse(),
                JsonResponse(
                    [
                        {
                            "id": "existing-409",
                            "slug": "storm-surge-20260627",
                            "url": "https://castopod.example/episodes/existing-409",
                        }
                    ]
                ),
            ]
        )

        with patch.dict(
            os.environ,
            {
                "CASTOPOD_URL": "https://castopod.example",
                "API_USER": "user",
                "API_PASS": "pass",
                "PODCAST_ID": "1",
            },
            clear=False,
        ), patch(
            "post_castopod_draft.create_castopod_draft_episode",
            return_value={"ok": False, "status_code": 409},
        ):
            posted = post_castopod_draft_for_run(
                run["run_id"], self.store, http_get=http_get, event_store=self.events
            )

        self.assertEqual(self._step(posted)["status"], "success")
        self.assertEqual(posted["overall_status"], "success")
        self.assertEqual(posted["castopod_episode_id"], "existing-409")
        self.assertEqual(len(http_get.call_args_list), 2)
        self.assertEqual(
            http_get.call_args_list[1].args[0],
            "https://castopod.example/api/rest/v1/podcasts/1/episodes",
        )

    def test_http_409_reconciliation_not_found_fails_clearly(self):
        run = self._assembled_run()
        http_get = Mock(side_effect=[AudioResponse(), JsonResponse([])])

        with patch.dict(
            os.environ,
            {
                "CASTOPOD_URL": "https://castopod.example",
                "API_USER": "user",
                "API_PASS": "pass",
            },
            clear=False,
        ), patch(
            "post_castopod_draft.create_castopod_draft_episode",
            return_value={"ok": False, "status_code": 409},
        ):
            posted = post_castopod_draft_for_run(
                run["run_id"], self.store, http_get=http_get, event_store=self.events
            )

        self.assertEqual(self._step(posted)["status"], "failed")
        self.assertEqual(
            self._step(posted)["message"],
            "Episode already exists in Castopod but could not be located by slug. "
            "Manual reconciliation required.",
        )
        self.assertTrue(can_retry_run(posted))

    def test_human_readable_status_messages_leave_run_retryable(self):
        expectations = {
            400: "Castopod rejected the episode data.",
            401: "Castopod authentication failed.",
            404: "Castopod could not find the podcast or user",
            500: "Castopod hit an internal server error.",
            418: "Unexpected response from Castopod (HTTP 418).",
        }
        for status_code, expected in expectations.items():
            with self.subTest(status_code=status_code):
                posted = self._post_failure(status_code)
                self.assertEqual(self._step(posted)["status"], "failed")
                self.assertIn(expected, self._step(posted)["message"])
                self.assertTrue(can_retry_run(posted))

    def test_slug_is_deterministic_and_stored_on_run(self):
        run = self._assembled_run()
        expected = "storm-surge-20260627"

        self.assertEqual(generate_episode_slug(run), expected)
        self.assertEqual(generate_episode_slug(run), expected)

        with patch(
            "post_castopod_draft.create_castopod_draft_episode",
            return_value={"ok": False, "status_code": 400},
        ):
            posted = post_castopod_draft_for_run(
                run["run_id"],
                self.store,
                http_get=Mock(return_value=AudioResponse()),
                event_store=self.events,
            )

        self.assertEqual(posted["assembled_episode_payload"]["slug"], expected)
        self.assertEqual(generate_episode_slug(posted), expected)

    def test_error_body_parser_supports_both_shapes_and_fallback(self):
        self.assertEqual(
            parse_castopod_error_body(
                {"messages": {"error": "Handled error"}}, 400
            ),
            "Handled error",
        )
        self.assertEqual(
            parse_castopod_error_body({"error": "Unhandled error"}, 500),
            "Unhandled error",
        )
        self.assertEqual(parse_castopod_error_body({}, 404), "HTTP 404")

    def test_http_401_does_not_parse_error_body(self):
        class ExplodingBody:
            def __str__(self):
                raise AssertionError("401 body must not be parsed")

        self.assertEqual(parse_castopod_error_body(ExplodingBody(), 401), "HTTP 401")

    def test_missing_assembled_payload_fails_clearly(self):
        run = self._assembled_run(payload=None)

        posted = post_castopod_draft_for_run(run["run_id"], self.store)

        self.assertEqual(self._step(posted)["status"], "failed")
        self.assertIn("assembled episode payload", self._step(posted)["message"])
        self.assertTrue(can_retry_run(posted))

    def test_missing_title_fails_clearly(self):
        self._assert_missing_field("title")

    def test_missing_description_fails_clearly(self):
        self._assert_missing_field("description")

    def test_missing_audio_url_fails_clearly(self):
        self._assert_missing_field("audio_url")

    def test_existing_episode_id_prevents_duplicate_draft(self):
        run = self._assembled_run()
        self.store.set_castopod_draft(
            run["run_id"],
            "existing-episode",
            "https://castopod.example/episodes/existing",
        )
        http_get = Mock()

        with patch("post_castopod_draft.create_castopod_draft_episode") as create_draft:
            posted = post_castopod_draft_for_run(
                run["run_id"], self.store, http_get=http_get, event_store=self.events
            )

        http_get.assert_not_called()
        create_draft.assert_not_called()
        self.assertEqual(self._step(posted)["status"], "success")
        self.assertEqual(posted["overall_status"], "success")
        self.assertIn("duplicate creation skipped", self._step(posted)["message"])

    def test_castopod_failure_includes_status_and_remains_retryable(self):
        run = self._assembled_run()
        with patch(
            "post_castopod_draft.create_castopod_draft_episode",
            return_value={
                "ok": False,
                "error": "Castopod rejected the episode upload.",
                "status_code": 422,
            },
        ):
            posted = post_castopod_draft_for_run(
                run["run_id"],
                self.store,
                http_get=Mock(return_value=AudioResponse()),
                event_store=self.events,
            )

        self.assertEqual(self._step(posted)["status"], "failed")
        self.assertIn("HTTP 422", self._step(posted)["message"])
        self.assertTrue(can_retry_run(posted))

    def _post_failure(self, status_code):
        run = self._assembled_run()
        with patch(
            "post_castopod_draft.create_castopod_draft_episode",
            return_value={
                "ok": False,
                "detail": '{"messages":{"error":"Castopod detail"}}',
                "status_code": status_code,
            },
        ):
            return post_castopod_draft_for_run(
                run["run_id"],
                self.store,
                http_get=Mock(return_value=AudioResponse()),
                event_store=self.events,
            )

    def _assert_missing_field(self, field):
        payload = self._payload()
        payload[field] = ""
        run = self._assembled_run(payload=payload)

        posted = post_castopod_draft_for_run(run["run_id"], self.store)

        self.assertEqual(self._step(posted)["status"], "failed")
        self.assertIn(field, self._step(posted)["message"])

    def _assembled_run(self, payload="default"):
        run = self.store.mark_stream_start(
            session_id=f"post-{len(self.store.get_recent_runs())}",
            started_at="2026-06-27T22:00:00+00:00",
            station="Storm Surge",
            show_name="Storm Surge",
        )
        run = self.store.mark_stream_end(
            run_id=run["run_id"], ended_at="2026-06-27T23:00:00+00:00"
        )
        if payload == "default":
            payload = self._payload()
        if payload is not None:
            run = self.store.set_assembled_episode_payload(run["run_id"], payload)
        return run

    def _payload(self):
        return {
            "title": "Storm Surge 20260627",
            "description": "Tracklist\n\n0:00:00 Artist - Track",
            "audio_url": "https://azuracast.example/show.mp3",
        }

    def _step(self, run):
        return next(
            step for step in run["steps"] if step["step_key"] == "post_castopod_draft"
        )


if __name__ == "__main__":
    unittest.main()
