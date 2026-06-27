import json
from datetime import datetime

from azuracast_history import prepare_description_with_tracklist


def assemble_episode_for_run(run_id, store):
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Unknown pipeline run: {run_id}")

    store.update_step_status(
        run_id,
        "assemble_episode",
        "in_progress",
        message="Assembling Castopod episode payload.",
    )
    tracklist = _tracklist_content(run)
    missing = _missing_artifacts(run, tracklist)
    if missing:
        message = f"Episode assembly failed: missing {', '.join(missing)}."
        return store.mark_step_failed(
            run_id,
            "assemble_episode",
            message=message,
            error_details={"missing_artifacts": missing},
        )

    payload = {
        "title": _episode_title(run["show_name"], run["started_at"]),
        "description": prepare_description_with_tracklist("", tracklist),
        "audio_url": run["recording_reference"],
    }
    store.set_assembled_episode_payload(run_id, payload)
    return store.update_step_status(
        run_id,
        "assemble_episode",
        "success",
        message="Castopod episode payload assembled.",
        error_details={"title": payload["title"], "audio_url": payload["audio_url"]},
    )


def _missing_artifacts(run, tracklist):
    missing = []
    if not run.get("recording_reference"):
        missing.append("recording_reference")
    if not tracklist:
        missing.append("tracklist content")
    if not run.get("started_at"):
        missing.append("started_at")
    if not run.get("ended_at"):
        missing.append("ended_at")
    return missing


def _tracklist_content(run):
    step = next(
        (item for item in run.get("steps", []) if item["step_key"] == "acquire_tracklist"),
        None,
    )
    if not step:
        return None
    details = step.get("error_details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            return None
    if not isinstance(details, dict):
        return None
    tracklist = details.get("tracklist")
    return str(tracklist).strip() if tracklist else None


def _episode_title(show_name, started_at):
    timestamp = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    return f"{show_name or 'Podcast Episode'} {timestamp.strftime('%Y%m%d')}"
