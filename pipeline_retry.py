from contextlib import closing

from pipeline_logging import StructuredPipelineLogger
from pipeline_mp3 import acquire_mp3_for_run
from pipeline_state import SYSTEM_RUN_ID, utc_now
from pipeline_tracklist import acquire_tracklist_for_run


RETRYABLE_DERIVED_STEPS = ("acquire_mp3", "acquire_tracklist")


def can_retry_run(run):
    if not run:
        return False
    if run.get("overall_status") == "success":
        return False
    return bool(run.get("started_at") and run.get("ended_at"))


def retry_pipeline_run(
    run_id,
    store,
    *,
    event_store=None,
    mp3_runner=None,
    tracklist_runner=None,
):
    event_store = event_store or StructuredPipelineLogger(store.db_path)
    mp3_runner = mp3_runner or acquire_mp3_for_run
    tracklist_runner = tracklist_runner or acquire_tracklist_for_run
    run = store.get_run(run_id)

    if not run:
        _emit(
            event_store,
            SYSTEM_RUN_ID,
            None,
            "acquire_mp3",
            "run_retry.skipped",
            "skipped",
            "Run retry skipped because the run was not found.",
            {"run_id": run_id, "skip_reason": "Run was not found."},
        )
        return {"ok": False, "skipped": True, "message": "Pipeline run was not found.", "run": None}

    if not can_retry_run(run):
        _emit(
            event_store,
            run["run_id"],
            run.get("session_id"),
            "acquire_mp3",
            "run_retry.skipped",
            "skipped",
            "Run retry skipped because the run is not retryable.",
            {
                "overall_status": run.get("overall_status"),
                "started_at": run.get("started_at"),
                "ended_at": run.get("ended_at"),
                "skip_reason": "Run is not failed or incomplete with a completed session window.",
            },
        )
        return {"ok": False, "skipped": True, "message": "Pipeline run is not retryable.", "run": run}

    _emit(
        event_store,
        run["run_id"],
        run.get("session_id"),
        "acquire_mp3",
        "run_retry.started",
        "in_progress",
        "Retrying pipeline run derived automation steps.",
        {"started_at": run.get("started_at"), "ended_at": run.get("ended_at")},
    )

    run = _retry_acquire_mp3(run, store, event_store, mp3_runner)
    run = _retry_acquire_tracklist(run, store, event_store, tracklist_runner)
    run = _recalculate_retry_status(store, run["run_id"])

    failed_steps = [
        step["step_key"]
        for step in run["steps"]
        if step["step_key"] in RETRYABLE_DERIVED_STEPS and step["status"] == "failed"
    ]
    if failed_steps:
        _emit(
            event_store,
            run["run_id"],
            run.get("session_id"),
            "acquire_mp3",
            "run_retry.failed",
            "failed",
            "Run retry finished with failed derived steps.",
            {"failed_steps": failed_steps},
        )
        return {"ok": False, "message": "Run retry finished with failed steps.", "run": run}

    _emit(
        event_store,
        run["run_id"],
        run.get("session_id"),
        "acquire_mp3",
        "run_retry.succeeded",
        "success",
        "Run retry completed.",
        {"retry_steps": list(RETRYABLE_DERIVED_STEPS)},
    )
    return {"ok": True, "message": "Run retry completed.", "run": run}


def _retry_acquire_mp3(run, store, event_store, mp3_runner):
    step = _step(run, "acquire_mp3")
    if run.get("castopod_episode_id") or run.get("castopod_episode_url"):
        event_store.emit(
            run_id=run["run_id"],
            session_id=run.get("session_id"),
            step_key="acquire_mp3",
            event_name="run_retry.acquire_mp3_skipped",
            status="skipped",
            message="MP3 acquisition retry skipped because a Castopod draft is already recorded.",
            details={
                "castopod_episode_id": run.get("castopod_episode_id"),
                "castopod_episode_url": run.get("castopod_episode_url"),
                "skip_reason": "Castopod draft already exists for this run.",
            },
        )
        return store.update_step_status(
            run["run_id"],
            "acquire_mp3",
            "success",
            message="MP3 acquisition already has a recorded Castopod draft.",
            error_details={
                "castopod_episode_id": run.get("castopod_episode_id"),
                "castopod_episode_url": run.get("castopod_episode_url"),
            },
        )

    if step and step["status"] == "success":
        event_store.emit(
            run_id=run["run_id"],
            session_id=run.get("session_id"),
            step_key="acquire_mp3",
            event_name="run_retry.acquire_mp3_skipped",
            status="skipped",
            message="MP3 acquisition retry skipped because acquire_mp3 already succeeded.",
            details={"skip_reason": "acquire_mp3 already succeeded."},
        )
        return run

    return mp3_runner(run["run_id"], store, event_store=event_store)


def _retry_acquire_tracklist(run, store, event_store, tracklist_runner):
    run = store.get_run(run["run_id"])
    step = _step(run, "acquire_tracklist")
    if step and step["status"] == "success":
        event_store.emit(
            run_id=run["run_id"],
            session_id=run.get("session_id"),
            step_key="acquire_tracklist",
            event_name="run_retry.acquire_tracklist_skipped",
            status="skipped",
            message="Tracklist acquisition retry skipped because acquire_tracklist already succeeded.",
            details={"skip_reason": "acquire_tracklist already succeeded."},
        )
        return run

    acquire_mp3 = _step(run, "acquire_mp3")
    if acquire_mp3 and acquire_mp3["status"] != "success":
        event_store.emit(
            run_id=run["run_id"],
            session_id=run.get("session_id"),
            step_key="acquire_tracklist",
            event_name="run_retry.acquire_tracklist_skipped",
            status="skipped",
            message="Tracklist acquisition retry skipped because acquire_mp3 did not succeed.",
            details={
                "skip_reason": "acquire_mp3 must succeed before retrying acquire_tracklist.",
                "acquire_mp3_status": acquire_mp3["status"],
            },
        )
        return run

    return tracklist_runner(run["run_id"], store)


def _recalculate_retry_status(store, run_id):
    run = store.get_run(run_id)
    statuses = {step["step_key"]: step["status"] for step in run["steps"]}
    failed = any(status == "failed" for status in statuses.values())
    complete = all(status in {"success", "skipped"} for status in statuses.values())
    if failed:
        overall_status = "failed"
    elif complete:
        overall_status = "success"
    else:
        overall_status = "in_progress"

    now = utc_now()
    with closing(store.connect()) as connection:
        connection.execute(
            """
            UPDATE pipeline_runs
            SET overall_status = ?,
                error_summary = CASE WHEN ? = 'failed' THEN error_summary ELSE NULL END,
                updated_at = ?
            WHERE run_id = ?
            """,
            (overall_status, overall_status, now, run_id),
        )
    return store.get_run(run_id)


def _step(run, step_key):
    return next((step for step in run.get("steps", []) if step["step_key"] == step_key), None)


def _emit(event_store, run_id, session_id, step_key, event_name, status, message, details):
    return event_store.emit(
        run_id=run_id,
        session_id=session_id,
        step_key=step_key,
        event_name=event_name,
        status=status,
        message=message,
        details=details,
        level="ERROR" if status == "failed" else "INFO",
    )
