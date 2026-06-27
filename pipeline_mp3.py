import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

from azuracast_config import (
    AzuraCastConfigStore,
    get_azuracast_api_key,
    get_azuracast_config,
)
from pipeline_logging import StructuredPipelineLogger


BROADCAST_MATCH_TOLERANCE_SECONDS = 60
DEFAULT_API_TIMEOUT_SECONDS = 15
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 120
MP3_CONTENT_TYPES = {"audio/mpeg", "audio/mp3", "audio/x-mpeg", "application/octet-stream"}


@dataclass(frozen=True)
class AudioAsset:
    path: str
    filename: str
    size_bytes: int
    content_type: str | None
    enclosure_url: str


def acquire_mp3_for_run(
    run_id,
    store,
    *,
    config=None,
    http_get=None,
    event_store=None,
):
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Unknown pipeline run: {run_id}")

    config_store = AzuraCastConfigStore(store.db_path)
    config = config or get_azuracast_config(config_store)
    event_store = event_store or StructuredPipelineLogger(store.db_path)
    http_get = http_get or requests.get

    result = acquire_broadcast_audio_for_run(
        run,
        store=store,
        config=config,
        api_key=get_azuracast_api_key(config_store),
        http_get=http_get,
        event_store=event_store,
    )

    details = result.get("details") or {}
    if result.get("waiting"):
        return store.update_step_status(
            run_id,
            "acquire_mp3",
            "waiting_transcode",
            message="Waiting for AzuraCast broadcast transcoding.",
            error_details=details,
        )
    if result.get("ok"):
        return store.update_step_status(
            run_id,
            "acquire_mp3",
            "success",
            message="AzuraCast broadcast audio downloaded and validated.",
            error_details=details,
        )

    status = "skipped" if result.get("skipped") else "failed"
    message = result.get("error") or "AzuraCast broadcast audio acquisition failed."
    if status == "skipped":
        details["skip_reason"] = message
        return store.update_step_status(
            run_id,
            "acquire_mp3",
            "skipped",
            message=f"MP3 acquisition skipped: {message}",
            error_details=details,
        )
    details["failure_reason"] = message
    return store.mark_step_failed(
        run_id,
        "acquire_mp3",
        message=f"MP3 acquisition failed: {message}",
        error_details=details,
    )


def acquire_broadcast_audio_for_run(
    run,
    *,
    store,
    config,
    api_key,
    http_get,
    event_store,
):
    config_error = _validate_broadcast_config(config, api_key)
    if config_error:
        return config_error

    details = {
        "station_id": config.station_id,
        "streamer_id": config.streamer_id,
        "run_started_at": run.get("started_at"),
        "run_ended_at": run.get("ended_at"),
    }
    temp_path = None

    try:
        recording_reference = run.get("recording_reference")
        broadcast_id = run.get("broadcast_id")
        if broadcast_id and _is_http_url(recording_reference):
            broadcast = {
                "id": broadcast_id,
                "timestampStart": run.get("started_at"),
                "timestampEnd": run.get("ended_at"),
                "recording": {"downloadUrl": recording_reference},
            }
        elif recording_reference:
            broadcast_id = recording_reference
            broadcast = fetch_broadcast(
                broadcast_id,
                http_get=http_get,
                config=config,
                api_key=api_key,
            )
        else:
            _emit(
                event_store,
                run,
                "azuracast_broadcast_match_started",
                "in_progress",
                "Matching AzuraCast broadcast to completed session.",
                details,
            )
            broadcast = find_matching_broadcast(
                run,
                http_get=http_get,
                config=config,
                api_key=api_key,
            )
            if not broadcast:
                error = "No AzuraCast streamer broadcast matched the completed session."
                _emit(
                    event_store,
                    run,
                    "azuracast_broadcast_match_failed",
                    "failed",
                    error,
                    details,
                )
                return {"ok": False, "error": error, "details": details}
            broadcast_id = broadcast.get("id")
            store.set_recording_reference(run["run_id"], broadcast_id)
            run = store.get_run(run["run_id"])
            _emit(
                event_store,
                run,
                "azuracast_broadcast_match_succeeded",
                "success",
                "AzuraCast broadcast matched to completed session.",
                _broadcast_details(broadcast),
            )

        details.update(_broadcast_details(broadcast))
        recording = broadcast.get("recording")
        if recording is None:
            _emit(
                event_store,
                run,
                "azuracast_transcode_waiting",
                "waiting_transcode",
                "AzuraCast broadcast recording is still transcoding.",
                details,
            )
            return {"ok": False, "waiting": True, "details": details}
        if not isinstance(recording, dict):
            error = "AzuraCast broadcast recording data was invalid."
            _emit(
                event_store,
                run,
                "azuracast_transcode_ready",
                "failed",
                error,
                details,
            )
            return {"ok": False, "error": error, "details": details}

        download_url = recording.get("downloadUrl")
        if not download_url:
            error = "AzuraCast broadcast recording has no download URL."
            _emit(
                event_store,
                run,
                "azuracast_transcode_ready",
                "failed",
                error,
                details,
            )
            return {"ok": False, "error": error, "details": details}

        details.update(
            {
                "recording_path": recording.get("path"),
                "recording_size": recording.get("size"),
                "recording_download_url": download_url,
            }
        )
        _emit(
            event_store,
            run,
            "azuracast_transcode_ready",
            "success",
            "AzuraCast broadcast recording is ready.",
            details,
        )
        run = store.update_step_status(
            run["run_id"],
            "acquire_mp3",
            "in_progress",
            message="Downloading AzuraCast broadcast recording.",
            error_details=details,
        )

        try:
            asset = download_audio_asset(
                download_url,
                http_get=http_get,
                event_store=event_store,
                run=run,
                headers={"X-API-Key": api_key},
            )
        except PipelineMp3Error as exc:
            return {"ok": False, "error": str(exc), "details": details}
        temp_path = asset.path
        details.update(
            {
                "audio_filename": asset.filename,
                "audio_size_bytes": asset.size_bytes,
                "audio_content_type": asset.content_type,
                "download_url": asset.enclosure_url,
            }
        )

        return {"ok": True, "details": details}
    except PipelineMp3Error as exc:
        return {"ok": False, "error": str(exc), "details": details}
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def find_matching_broadcast(run, http_get, config, api_key=None):
    api_key = api_key or get_azuracast_api_key()
    url = resolve_broadcasts_url(config)
    payload = _get_json(url, http_get=http_get, api_key=api_key)
    broadcasts = normalize_broadcast_list(payload)
    run_start = parse_datetime(run.get("started_at"))
    run_end = parse_datetime(run.get("ended_at"))
    if not run_start or not run_end:
        raise PipelineMp3Error("Completed run session timestamps are required to match a broadcast.")

    candidates = []
    for broadcast in broadcasts:
        broadcast_start = parse_datetime(broadcast.get("timestampStart"))
        broadcast_end = parse_datetime(broadcast.get("timestampEnd"))
        if not broadcast_start or not broadcast_end:
            continue
        start_delta = abs((broadcast_start - run_start).total_seconds())
        end_delta = abs((broadcast_end - run_end).total_seconds())
        if start_delta <= BROADCAST_MATCH_TOLERANCE_SECONDS and end_delta <= BROADCAST_MATCH_TOLERANCE_SECONDS:
            candidates.append((start_delta + end_delta, broadcast))
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: candidate[0])[1]


def fetch_broadcast(broadcast_id, *, http_get, config, api_key):
    payload = _get_json(
        resolve_broadcasts_url(config),
        http_get=http_get,
        api_key=api_key,
    )
    broadcast = next(
        (
            item
            for item in normalize_broadcast_list(payload)
            if str(item.get("id")) == str(broadcast_id)
        ),
        None,
    )
    if broadcast is None:
        raise PipelineMp3Error("Stored AzuraCast broadcast was not found.")
    return broadcast


def resolve_broadcasts_url(config):
    station_id = quote(str(config.station_id), safe="")
    streamer_id = quote(str(config.streamer_id), safe="")
    return (
        f"{config.base_url.rstrip('/')}/api/station/{station_id}"
        f"/streamer/{streamer_id}/broadcasts"
    )


def normalize_broadcast_list(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("broadcasts", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [payload] if payload.get("id") is not None else []


def download_audio_asset(
    enclosure_url,
    *,
    http_get,
    event_store,
    run,
    headers=None,
    step_key="acquire_mp3",
):
    _emit(event_store, run, "acquire_mp3.download_started", "in_progress", "Downloading RSS enclosure audio.", {"enclosure_url": enclosure_url}, step_key=step_key)
    try:
        response = http_get(
            enclosure_url,
            headers=headers or {},
            stream=True,
            timeout=DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        message = f"RSS enclosure download failed: {exc.__class__.__name__}"
        _emit(event_store, run, "acquire_mp3.download_failed", "failed", message, {"enclosure_url": enclosure_url}, step_key=step_key)
        raise PipelineMp3Error(message) from exc

    suffix = ".mp3" if ".mp3" in enclosure_url.lower() else ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        for chunk in response.iter_content(chunk_size=1024 * 128):
            if chunk:
                temp_file.write(chunk)
        temp_path = temp_file.name

    content_type = response.headers.get("content-type")
    asset = AudioAsset(
        path=temp_path,
        filename=Path(enclosure_url.split("?", 1)[0]).name or "azuracast-podcast.mp3",
        size_bytes=os.path.getsize(temp_path),
        content_type=content_type,
        enclosure_url=enclosure_url,
    )
    _emit(event_store, run, "acquire_mp3.download_succeeded", "success", "RSS enclosure audio downloaded.", {"audio_size_bytes": asset.size_bytes, "audio_content_type": content_type}, step_key=step_key)
    validate_audio_asset(asset, event_store=event_store, run=run, step_key=step_key)
    return asset


def validate_audio_asset(asset, *, event_store, run, step_key="acquire_mp3"):
    if not os.path.exists(asset.path) or asset.size_bytes <= 0:
        message = "Downloaded audio asset is empty."
        _emit(event_store, run, "acquire_mp3.validation_failed", "failed", message, {"audio_size_bytes": asset.size_bytes}, step_key=step_key)
        raise PipelineMp3Error(message)
    content_type = str(asset.content_type or "").split(";", 1)[0].lower()
    if content_type and content_type not in MP3_CONTENT_TYPES and not asset.filename.lower().endswith(".mp3"):
        message = "Downloaded audio asset does not look like an MP3."
        _emit(event_store, run, "acquire_mp3.validation_failed", "failed", message, {"audio_content_type": asset.content_type, "filename": asset.filename}, step_key=step_key)
        raise PipelineMp3Error(message)
    _emit(event_store, run, "acquire_mp3.validation_succeeded", "success", "Downloaded audio asset validated.", {"audio_size_bytes": asset.size_bytes, "audio_content_type": asset.content_type}, step_key=step_key)


def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_http_url(value):
    return str(value or "").startswith(("http://", "https://"))


def _validate_broadcast_config(config, api_key):
    if not config.enabled:
        return {"ok": False, "skipped": True, "error": "AzuraCast integration is disabled."}
    if not config.base_url:
        return {"ok": False, "skipped": True, "error": "AzuraCast base URL is not configured."}
    if not config.station_id:
        return {"ok": False, "skipped": True, "error": "AzuraCast Station ID is not configured."}
    if not config.streamer_id:
        return {"ok": False, "skipped": True, "error": "AzuraCast Streamer ID is not configured."}
    if not api_key:
        return {"ok": False, "error": "AzuraCast API key is not configured."}
    return None


def _get_json(url, *, http_get, api_key):
    try:
        response = http_get(
            url,
            headers={"X-API-Key": api_key},
            timeout=DEFAULT_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PipelineMp3Error(f"AzuraCast broadcast API request failed: {exc.__class__.__name__}") from exc
    try:
        return response.json()
    except ValueError as exc:
        raise PipelineMp3Error("AzuraCast broadcast API response was not valid JSON.") from exc


def _broadcast_details(broadcast):
    recording = broadcast.get("recording")
    return {
        "broadcast_id": broadcast.get("id"),
        "broadcast_started_at": broadcast.get("timestampStart"),
        "broadcast_ended_at": broadcast.get("timestampEnd"),
        "recording_ready": recording is not None,
        "recording_path": recording.get("path") if isinstance(recording, dict) else None,
        "recording_size": recording.get("size") if isinstance(recording, dict) else None,
    }


def _emit(event_store, run, event_name, status, message, details=None, step_key="acquire_mp3"):
    return event_store.emit(
        run_id=run["run_id"],
        session_id=run.get("session_id"),
        step_key=step_key,
        event_name=event_name,
        status=status,
        message=message,
        details=details or {},
        level="ERROR" if status == "failed" else "INFO",
    )


class PipelineMp3Error(Exception):
    pass
