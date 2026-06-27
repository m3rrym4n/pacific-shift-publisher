import json

from pipeline_mp3 import acquire_mp3_for_run
from pipeline_state import get_pipeline_store


def sync_waiting_transcodes(store=None, mp3_runner=None):
    store = store or get_pipeline_store()
    mp3_runner = mp3_runner or acquire_mp3_for_run
    runs = store.get_runs_by_step_status("acquire_mp3", "waiting_transcode")
    results = []
    for run in runs:
        updated = mp3_runner(run["run_id"], store)
        step = next(
            step for step in updated["steps"] if step["step_key"] == "acquire_mp3"
        )
        results.append(
            {
                "run_id": updated["run_id"],
                "status": step["status"],
            }
        )
    return results


def main():
    results = sync_waiting_transcodes()
    print(json.dumps({"runs_checked": len(results), "results": results}, sort_keys=True))


if __name__ == "__main__":
    main()
