from datetime import timezone

import requests

from assemble_episode import assemble_episode_for_run
from azuracast_config import (
    AzuraCastConfigStore,
    get_azuracast_api_key,
    get_azuracast_config,
)
from pipeline_logging import StructuredPipelineLogger
from pipeline_mp3 import parse_datetime, resolve_broadcasts_url
from pipeline_tracklist import acquire_tracklist_for_run
from post_castopod_draft import post_castopod_draft_for_run


API_TIMEOUT_SECONDS = 15
INCOMPLETE_RUN_STATUSES = {"waiting", "in_progress", "failed"}


class BroadcastSelectionError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def get_ready_broadcasts(*, store, config=None, http_get=None):
    config_store = AzuraCastConfigStore(store.db_path)
    config = config or get_azuracast_config(config_store)
    api_key = get_azuracast_api_key(config_store)
    _validate_config(config, api_key)
    http_get = http_get or requests.get
    url = resolve_broadcasts_url(config)
    try:
        response = http_get(
            url,
            headers={"X-API-Key": api_key},
            timeout=API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise BroadcastSelectionError(
            f"AzuraCast broadcast request failed: {exc.__class__.__name__}.",
            502,
        ) from exc
    except ValueError as exc:
        raise BroadcastSelectionError("AzuraCast returned invalid broadcast JSON.", 502) from exc

    items = payload if isinstance(payload, list) else []
    broadcasts = [normalized for item in items if (normalized := _normalize_broadcast(item))]
    return sorted(broadcasts, key=lambda item: item["started_at"], reverse=True)


def select_broadcast_for_pipeline(
    broadcast_id,
    *,
    store,
    config=None,
    http_get=None,
    event_store=None,
    tracklist_runner=None,
    mp3_runner=None,
    assemble_runner=None,
    post_runner=None,
):
    from pipeline_mp3 import acquire_mp3_for_run

    config_store = AzuraCastConfigStore(store.db_path)
    config = config or get_azuracast_config(config_store)
    broadcasts = get_ready_broadcasts(store=store, config=config, http_get=http_get)
    broadcast = next(
        (item for item in broadcasts if str(item["broadcast_id"]) == str(broadcast_id)),
        None,
    )
    if not broadcast:
        raise BroadcastSelectionError("The selected ready broadcast was not found.", 404)

    run = _find_matching_run(store, broadcast)
    created = run is None
    station = config.station_name or config.station_shortcode or config.station_id
    streamer = str(config.streamer_id) if config.streamer_id else None
    if created:
        run = store.create_run(
            station=station,
            show_name=config.station_name,
            streamer=streamer,
            session_id=f"azuracast-broadcast-{broadcast['broadcast_id']}",
        )

    run = store.assign_broadcast(
        run["run_id"],
        broadcast_id=broadcast["broadcast_id"],
        started_at=broadcast["started_at"],
        ended_at=broadcast["ended_at"],
        recording_reference=broadcast["download_url"],
        station=station,
        streamer=streamer,
    )
    event_store = event_store or StructuredPipelineLogger(store.db_path)
    event_store.emit(
        run_id=run["run_id"],
        session_id=run["session_id"],
        step_key="stream_end",
        event_name="azuracast_broadcast_selected",
        status="success",
        message="AzuraCast broadcast confirmed by operator.",
        details={
            "broadcast_id": broadcast["broadcast_id"],
            "started_at": broadcast["started_at"],
            "ended_at": broadcast["ended_at"],
            "recording_download_url": broadcast["download_url"],
            "run_created": created,
        },
    )

    tracklist_runner = tracklist_runner or acquire_tracklist_for_run
    run = tracklist_runner(run["run_id"], store)
    if _step_status(run, "acquire_tracklist") == "success":
        mp3_runner = mp3_runner or acquire_mp3_for_run
        run = mp3_runner(run["run_id"], store)
    if (
        _step_status(run, "acquire_mp3") == "success"
        and _step_status(run, "acquire_tracklist") == "success"
    ):
        assemble_runner = assemble_runner or assemble_episode_for_run
        run = assemble_runner(run["run_id"], store)
    if _step_status(run, "assemble_episode") == "success":
        post_runner = post_runner or post_castopod_draft_for_run
        run = post_runner(run["run_id"], store)
    return {"run": run, "created": created, "broadcast": broadcast}


def _normalize_broadcast(item):
    if not isinstance(item, dict) or not isinstance(item.get("recording"), dict):
        return None
    recording = item["recording"]
    download_url = recording.get("downloadUrl")
    started_at = parse_datetime(item.get("timestampStart"))
    ended_at = parse_datetime(item.get("timestampEnd"))
    broadcast_id = _integer(item.get("id"))
    if broadcast_id is None or not download_url or not started_at or not ended_at:
        return None
    size_bytes = _number(recording.get("size"))
    return {
        "broadcast_id": broadcast_id,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "ended_at": ended_at.astimezone(timezone.utc).isoformat(),
        "duration_minutes": round((ended_at - started_at).total_seconds() / 60, 1),
        "size_mb": round(size_bytes / (1024 * 1024), 1) if size_bytes is not None else None,
        "download_url": download_url,
    }


def _find_matching_run(store, broadcast):
    for run in store.get_recent_runs(limit=100):
        if str(run.get("broadcast_id") or "") == str(broadcast["broadcast_id"]):
            return run
    broadcast_start = parse_datetime(broadcast["started_at"])
    broadcast_end = parse_datetime(broadcast["ended_at"])
    candidates = []
    for run in store.get_recent_runs(limit=100):
        if run.get("overall_status") not in INCOMPLETE_RUN_STATUSES:
            continue
        run_start = parse_datetime(run.get("started_at"))
        run_end = parse_datetime(run.get("ended_at")) or broadcast_end
        if run_start and run_start <= broadcast_end and run_end >= broadcast_start:
            candidates.append(run)
    return candidates[0] if candidates else None


def _validate_config(config, api_key):
    if not config.enabled:
        raise BroadcastSelectionError("AzuraCast integration is disabled.", 409)
    missing = []
    if not config.base_url:
        missing.append("base URL")
    if not config.station_id:
        missing.append("Station ID")
    if not config.streamer_id:
        missing.append("Streamer ID")
    if not api_key:
        missing.append("API key")
    if missing:
        raise BroadcastSelectionError(
            f"AzuraCast integration is missing: {', '.join(missing)}.",
            400,
        )


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _step_status(run, step_key):
    return next(
        (step["status"] for step in run.get("steps", []) if step["step_key"] == step_key),
        None,
    )
