import argparse
import json
import time

from azuracast_config import get_azuracast_config
from pipeline_mp3 import acquire_mp3_for_run
from pipeline_state import get_pipeline_store


DEFAULT_POLL_INTERVAL_MINUTES = 5


def _print_output(message):
    print(message, flush=True)


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


def resolve_poll_interval_minutes(config):
    try:
        interval = int(config.transcode_poll_interval_minutes)
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_MINUTES
    return interval if 1 <= interval <= 30 else DEFAULT_POLL_INTERVAL_MINUTES


def run_transcode_scheduler(
    *,
    once=False,
    config_loader=None,
    sync_func=None,
    sleep_func=None,
    output_func=None,
):
    config_loader = config_loader or get_azuracast_config
    sync_func = sync_func or sync_waiting_transcodes
    sleep_func = sleep_func or time.sleep
    output_func = output_func or _print_output

    try:
        config = config_loader()
    except Exception:
        config = None
    interval_minutes = resolve_poll_interval_minutes(config)
    output_func(
        f"Transcode sync running every {interval_minutes} minutes "
        "(from AzuraCast settings)"
    )

    while True:
        results = sync_func()
        output_func(
            json.dumps(
                {"runs_checked": len(results), "results": results},
                sort_keys=True,
            )
        )
        if once:
            return results
        sleep_func(interval_minutes * 60)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sync waiting AzuraCast transcodes.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one transcode sync cycle and exit.",
    )
    args = parser.parse_args(argv)
    run_transcode_scheduler(once=args.once)


if __name__ == "__main__":
    main()
