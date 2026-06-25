from pipeline_constants import PIPELINE_STEP_KEYS
from pipeline_logging import get_pipeline_logger
from pipeline_state import get_pipeline_store


STEP_LABELS = {
    "stream_start": "Stream Start",
    "stream_end": "Stream End",
    "acquire_mp3": "Acquire MP3",
    "acquire_tracklist": "Acquire Tracklist",
    "assemble_episode": "Assemble Podcast Episode",
    "post_castopod_draft": "Post Episode to Castopod as Draft",
}

STATUS_CLASSES = {
    "pending": "secondary",
    "waiting": "azure",
    "in_progress": "warning",
    "success": "success",
    "failed": "danger",
    "skipped": "secondary",
}

STATUS_TEXT = {
    "pending": "Pending",
    "waiting": "Waiting",
    "in_progress": "In Progress",
    "success": "Success",
    "failed": "Failed",
    "skipped": "Skipped",
}


def build_dashboard_view_model(store=None, event_store=None):
    store = store or get_pipeline_store()
    event_store = event_store or get_pipeline_logger()
    run = store.get_latest_run()

    if not run:
        return {
            "has_run": False,
            "empty_message": "No pipeline runs yet.",
            "empty_detail": "Publisher will show automation progress here after a stream starts.",
            "run": None,
            "cards": [_empty_card(step_key) for step_key in PIPELINE_STEP_KEYS],
            "draft": {
                "available": False,
                "message": "Castopod draft not created yet.",
                "episode_id": None,
                "episode_url": None,
            },
        }

    events = event_store.find_events(run_id=run["run_id"])
    events_by_step = {}
    for event in events:
        events_by_step.setdefault(event["step_key"], []).append(event)

    steps_by_key = {step["step_key"]: step for step in run["steps"]}
    cards = []
    for step_key in PIPELINE_STEP_KEYS:
        step = steps_by_key.get(step_key, {"status": "pending"})
        latest_event = events_by_step.get(step_key, [])[-1] if events_by_step.get(step_key) else None
        status = step.get("status") or "pending"
        cards.append(
            {
                "label": STEP_LABELS[step_key],
                "step_key": step_key,
                "status": status,
                "status_text": STATUS_TEXT.get(status, status.replace("_", " ").title()),
                "status_class": STATUS_CLASSES.get(status, "secondary"),
                "message": _latest_message(step, latest_event),
                "updated_at": _latest_timestamp(step, latest_event),
            }
        )

    return {
        "has_run": True,
        "empty_message": None,
        "empty_detail": None,
        "run": {
            "run_id": run["run_id"],
            "session_id": run["session_id"],
            "show_name": run["show_name"] or "Unknown show",
            "station": run["station"] or "Unknown station",
            "streamer": run["streamer"],
            "started_at": run["started_at"],
            "ended_at": run["ended_at"],
            "overall_status": run["overall_status"],
            "overall_status_text": STATUS_TEXT.get(
                run["overall_status"],
                str(run["overall_status"]).replace("_", " ").title(),
            ),
            "overall_status_class": STATUS_CLASSES.get(run["overall_status"], "secondary"),
            "current_step": run["current_step"],
            "error_summary": run["error_summary"],
            "can_cancel": bool(
                not run["ended_at"]
                and run["overall_status"] in {"waiting", "in_progress"}
            ),
        },
        "cards": cards,
        "draft": {
            "available": bool(run["castopod_episode_id"] or run["castopod_episode_url"]),
            "message": (
                "Castopod draft created."
                if run["castopod_episode_id"] or run["castopod_episode_url"]
                else "Castopod draft not created yet."
            ),
            "episode_id": run["castopod_episode_id"],
            "episode_url": run["castopod_episode_url"],
        },
    }


def _empty_card(step_key):
    return {
        "label": STEP_LABELS[step_key],
        "step_key": step_key,
        "status": "pending",
        "status_text": STATUS_TEXT["pending"],
        "status_class": STATUS_CLASSES["pending"],
        "message": "Waiting for pipeline activity.",
        "updated_at": None,
    }


def _latest_message(step, latest_event):
    if latest_event and latest_event.get("message"):
        return latest_event["message"]
    if step.get("message"):
        return step["message"]
    if step.get("error_details"):
        return step["error_details"]
    return "No activity recorded yet."


def _latest_timestamp(step, latest_event):
    if latest_event and latest_event.get("timestamp"):
        return latest_event["timestamp"]
    return step.get("ended_at") or step.get("started_at")
