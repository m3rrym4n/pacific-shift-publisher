from pipeline_logging import get_pipeline_logger
from pipeline_state import get_pipeline_store
from tracklist import episode_relative_timestamp, format_tracklist


def build_tracklist_detail_view_model(run_id, store=None, event_store=None):
    store = store or get_pipeline_store()
    event_store = event_store or get_pipeline_logger()
    run = store.get_run(run_id)
    if not run:
        return {
            "found": False,
            "message": "Pipeline run was not found.",
            "run": None,
            "tracks": [],
            "track_count": 0,
            "formatted_tracklist": None,
        }

    event = _latest_tracklist_event(event_store, run_id)
    details = event.get("details") if event else {}
    tracks = [
        _build_track_row(track, run["started_at"], index == 0)
        for index, track in enumerate(details.get("tracks") or [])
    ]
    return {
        "found": True,
        "message": None,
        "run": {
            "run_id": run["run_id"],
            "short_run_id": run["run_id"][:8],
            "session_id": run["session_id"],
            "show_name": run["show_name"] or "Unknown show",
            "station": run["station"] or "Unknown station",
            "started_at": run["started_at"],
            "ended_at": run["ended_at"],
        },
        "tracks": tracks,
        "track_count": len(tracks),
        "formatted_tracklist": format_tracklist(details.get("tracks") or [], started_at=run["started_at"]),
    }


def _latest_tracklist_event(event_store, run_id):
    events = event_store.find_events(run_id=run_id, step_key="acquire_tracklist")
    successful = [event for event in events if event["event_name"] == "acquire_tracklist.succeeded"]
    return successful[-1] if successful else None


def _build_track_row(track, started_at, is_first_track=False):
    return {
        "played_at": episode_relative_timestamp(track, started_at, is_first_track=is_first_track),
        "artist": track.get("artist") or "Unknown artist",
        "title": track.get("title") or track.get("display") or track.get("text") or "Unknown track",
    }
