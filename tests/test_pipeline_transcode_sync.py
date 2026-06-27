import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pipeline_state import PipelineStateStore
from pipeline_transcode_sync import (
    main,
    run_transcode_scheduler,
    sync_waiting_transcodes,
)


class SchedulerStopped(Exception):
    pass


class PipelineTranscodeSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PipelineStateStore(str(Path(self.temp_dir.name) / "publisher_state.sqlite"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sync_selects_only_waiting_transcode_runs(self):
        waiting = self._completed_run("waiting")
        self.store.update_step_status(waiting["run_id"], "acquire_mp3", "waiting_transcode")
        failed = self._completed_run("failed")
        self.store.mark_step_failed(failed["run_id"], "acquire_mp3", message="failed")
        runner = Mock(side_effect=lambda run_id, store: store.get_run(run_id))

        results = sync_waiting_transcodes(store=self.store, mp3_runner=runner)

        self.assertEqual([result["run_id"] for result in results], [waiting["run_id"]])
        runner.assert_called_once_with(waiting["run_id"], self.store)

    def test_sync_preserves_waiting_state_until_recording_is_ready(self):
        waiting = self._completed_run("still-waiting")
        self.store.set_recording_reference(waiting["run_id"], "broadcast-1")
        self.store.update_step_status(waiting["run_id"], "acquire_mp3", "waiting_transcode")

        def runner(run_id, store):
            return store.update_step_status(run_id, "acquire_mp3", "waiting_transcode")

        results = sync_waiting_transcodes(store=self.store, mp3_runner=runner)

        self.assertEqual(results[0]["status"], "waiting_transcode")
        self.assertEqual(self.store.get_run(waiting["run_id"])["recording_reference"], "broadcast-1")

    def test_continuous_scheduler_reads_config_and_repeats_at_interval(self):
        config_loader = Mock(
            return_value=type("Config", (), {"transcode_poll_interval_minutes": 2})()
        )
        sync_func = Mock(side_effect=[[], []])
        sleep_func = Mock(side_effect=[None, SchedulerStopped()])
        output_func = Mock()

        with self.assertRaises(SchedulerStopped):
            run_transcode_scheduler(
                config_loader=config_loader,
                sync_func=sync_func,
                sleep_func=sleep_func,
                output_func=output_func,
            )

        config_loader.assert_called_once_with()
        self.assertEqual(sync_func.call_count, 2)
        self.assertEqual(sleep_func.call_args_list[0].args, (120,))
        self.assertEqual(sleep_func.call_args_list[1].args, (120,))
        self.assertEqual(
            output_func.call_args_list[0].args[0],
            "Transcode sync running every 2 minutes (from AzuraCast settings)",
        )

    def test_once_runs_single_cycle_without_sleeping(self):
        sync_func = Mock(return_value=[])
        sleep_func = Mock()

        result = run_transcode_scheduler(
            once=True,
            config_loader=lambda: type(
                "Config", (), {"transcode_poll_interval_minutes": 7}
            )(),
            sync_func=sync_func,
            sleep_func=sleep_func,
            output_func=Mock(),
        )

        self.assertEqual(result, [])
        sync_func.assert_called_once_with()
        sleep_func.assert_not_called()

    def test_invalid_or_unreadable_config_falls_back_to_five_minutes(self):
        output_func = Mock()

        run_transcode_scheduler(
            once=True,
            config_loader=lambda: type(
                "Config", (), {"transcode_poll_interval_minutes": "invalid"}
            )(),
            sync_func=lambda: [],
            output_func=output_func,
        )

        self.assertEqual(
            output_func.call_args_list[0].args[0],
            "Transcode sync running every 5 minutes (from AzuraCast settings)",
        )

        output_func.reset_mock()
        run_transcode_scheduler(
            once=True,
            config_loader=Mock(side_effect=RuntimeError("database unavailable")),
            sync_func=lambda: [],
            output_func=output_func,
        )
        self.assertEqual(
            output_func.call_args_list[0].args[0],
            "Transcode sync running every 5 minutes (from AzuraCast settings)",
        )

    def test_main_once_flag_exits_after_single_cycle(self):
        with patch("pipeline_transcode_sync.run_transcode_scheduler") as runner:
            main(["--once"])

        runner.assert_called_once_with(once=True)

    def _completed_run(self, session_id):
        run = self.store.mark_stream_start(
            session_id=session_id,
            started_at="2026-06-25T22:00:00Z",
            station="Storm Surge",
        )
        return self.store.mark_stream_end(run_id=run["run_id"], ended_at="2026-06-25T23:00:00Z")


if __name__ == "__main__":
    unittest.main()
