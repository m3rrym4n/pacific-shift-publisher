from dashboard import STATUS_CLASSES, STATUS_TEXT, STEP_LABELS
from pipeline_constants import PIPELINE_STEP_KEYS
from pipeline_retry import can_retry_run
from pipeline_state import can_cancel_run, get_pipeline_store


def build_recent_runs_view_model(store=None, limit=20):
    store = store or get_pipeline_store()
    runs = store.get_recent_runs(limit=limit)

    return {
        "has_runs": bool(runs),
        "empty_message": "No pipeline runs yet.",
        "empty_detail": "Recent automation attempts will appear here after runs are created.",
        "rows": [_build_run_row(run) for run in runs],
    }


def _build_run_row(run):
    status = run["overall_status"]
    steps = [_build_step_summary(step) for step in run["steps"]]
    failed_step = _failed_step(steps, run)

    return {
        "run_id": run["run_id"],
        "short_run_id": run["run_id"][:8],
        "show_name": run["show_name"],
        "station": run["station"],
        "display_name": _display_name(run),
        "started_at": run["started_at"],
        "ended_at": run["ended_at"],
        "overall_status": status,
        "overall_status_text": STATUS_TEXT.get(status, str(status).replace("_", " ").title()),
        "overall_status_class": STATUS_CLASSES.get(status, "secondary"),
        "failed_step": failed_step,
        "castopod_episode_id": run["castopod_episode_id"],
        "castopod_episode_url": run["castopod_episode_url"],
        "updated_at": run["updated_at"],
        "step_summary": _step_summary_text(steps),
        "steps": steps,
        "can_cancel": can_cancel_run(run),
        "can_retry": can_retry_run(run),
    }


def _build_step_summary(step):
    status = step["status"]
    return {
        "step_key": step["step_key"],
        "label": STEP_LABELS.get(step["step_key"], step["step_key"]),
        "status": status,
        "status_text": STATUS_TEXT.get(status, str(status).replace("_", " ").title()),
        "status_class": STATUS_CLASSES.get(status, "secondary"),
        "message": step["message"] or step["error_details"] or "",
    }


def _failed_step(steps, run):
    for step in steps:
        if step["status"] == "failed":
            return step["label"]
    if run["overall_status"] == "failed" and run["current_step"] in PIPELINE_STEP_KEYS:
        return STEP_LABELS[run["current_step"]]
    return None


def _step_summary_text(steps):
    status_counts = {}
    for step in steps:
        status_counts[step["status_text"]] = status_counts.get(step["status_text"], 0) + 1
    return ", ".join(
        f"{count} {status_text}"
        for status_text, count in sorted(status_counts.items())
    )


def _display_name(run):
    if run["show_name"] and run["station"]:
        return f"{run['show_name']} / {run['station']}"
    return run["show_name"] or run["station"] or "Unknown show"
