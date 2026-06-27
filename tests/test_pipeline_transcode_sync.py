import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pipeline_state import PipelineStateStore
from pipeline_transcode_sync import sync_waiting_transcodes


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

    def _completed_run(self, session_id):
        run = self.store.mark_stream_start(
            session_id=session_id,
            started_at="2026-06-25T22:00:00Z",
            station="Storm Surge",
        )
        return self.store.mark_stream_end(run_id=run["run_id"], ended_at="2026-06-25T23:00:00Z")


if __name__ == "__main__":
    unittest.main()
