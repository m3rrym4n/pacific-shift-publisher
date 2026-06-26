import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

from azuracast_config import get_azuracast_config
from castopod_client import create_castopod_draft_episode
from pipeline_logging import StructuredPipelineLogger
from pipeline_state import utc_now
from rss_source import RssSourceStore, refresh_rss_source


DEFAULT_READY_TIMEOUT_SECONDS = 60
DEFAULT_POLL_INTERVAL_SECONDS = 5
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
    rss_store=None,
    http_get=None,
    http_post=None,
    event_store=None,
    sleep_func=None,
    readiness_timeout_seconds=DEFAULT_READY_TIMEOUT_SECONDS,
    poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
):
    run = store.update_step_status(
        run_id,
        "acquire_mp3",
        "in_progress",
        message="Acquiring AzuraCast podcast audio.",
    )
    event_store = event_store or StructuredPipelineLogger(store.db_path)
    rss_store = rss_store or RssSourceStore(store.db_path)
    http_get = http_get or requests.get
    sleep_func = sleep_func or time.sleep

    result = acquire_podcast_audio_for_run(
        run,
        config=config,
        rss_store=rss_store,
        http_get=http_get,
        http_post=http_post,
        event_store=event_store,
        sleep_func=sleep_func,
        readiness_timeout_seconds=readiness_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )

    details = result.get("details") or {}
    if result.get("ok"):
        _record_castopod_draft(store, run_id, result)
        return store.update_step_status(
            run_id,
            "acquire_mp3",
            "success",
            message="AzuraCast podcast audio acquired and Castopod draft created.",
            error_details=details,
        )

    status = "skipped" if result.get("skipped") else "failed"
    message = result.get("error") or "AzuraCast podcast audio acquisition failed."
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


def acquire_podcast_audio_for_run(
    run,
    *,
    config=None,
    rss_store=None,
    http_get=None,
    http_post=None,
    event_store=None,
    sleep_func=None,
    readiness_timeout_seconds=DEFAULT_READY_TIMEOUT_SECONDS,
    poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
):
    config = config or get_azuracast_config()
    rss_store = rss_store or RssSourceStore()
    event_store = event_store or StructuredPipelineLogger()
    http_get = http_get or requests.get
    sleep_func = sleep_func or time.sleep
    details = {}
    temp_path = None

    try:
        readiness = wait_for_podcast_readiness(
            run,
            config=config,
            source_config=rss_store.get_config(),
            http_get=http_get,
            event_store=event_store,
            timeout_seconds=readiness_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            sleep_func=sleep_func,
        )
        if not readiness["ok"]:
            return {
                "ok": False,
                "skipped": readiness.get("skipped", False),
                "error": readiness["error"],
                "details": readiness.get("details", {}),
            }
        details.update(readiness.get("details", {}))

        refresh = refresh_rss_source(store=rss_store, http_get=http_get, event_store=event_store)
        if not refresh["ok"]:
            error = refresh["message"]
            _emit(event_store, run, "rss_source.refresh_failed", "failed", error, {"rss_refresh_status": refresh["status"]})
            return {"ok": False, "error": error, "details": details}
        _emit(
            event_store,
            run,
            "rss_source.refresh_succeeded",
            "success",
            refresh["message"],
            {"rss_item_count": len(refresh.get("items") or [])},
        )

        match = select_matching_enclosure(refresh.get("items") or [], run)
        if not match:
            error = "No matching RSS enclosure was found for the completed session."
            _emit(event_store, run, "rss_enclosure.match_failed", "failed", error, {"item_count": len(refresh.get("items") or [])})
            return {"ok": False, "error": error, "details": details}
        _emit(event_store, run, "rss_enclosure.match_succeeded", "success", "RSS enclosure matched.", match)
        details["rss_item"] = match

        try:
            asset = download_audio_asset(match["enclosure_url"], http_get=http_get, event_store=event_store, run=run)
        except PipelineMp3Error as exc:
            return {"ok": False, "error": str(exc), "details": details}
        temp_path = asset.path
        details.update(
            {
                "audio_filename": asset.filename,
                "audio_size_bytes": asset.size_bytes,
                "audio_content_type": asset.content_type,
                "enclosure_url": asset.enclosure_url,
            }
        )

        draft = create_castopod_draft_from_asset(
            run,
            asset,
            http_post=http_post,
            event_store=event_store,
        )
        if not draft["ok"]:
            return {"ok": False, "error": draft["error"], "details": details}
        details.update({"castopod_episode_id": draft.get("episode_id"), "castopod_episode_url": draft.get("episode_url")})
        return {
            "ok": True,
            "details": details,
            "castopod_episode_id": draft.get("episode_id"),
            "castopod_episode_url": draft.get("episode_url"),
        }
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def wait_for_podcast_readiness(
    run,
    *,
    config,
    source_config,
    http_get,
    event_store,
    timeout_seconds,
    poll_interval_seconds,
    sleep_func,
):
    if not config.enabled:
        return {"ok": False, "skipped": True, "error": "AzuraCast integration is disabled."}
    if not config.base_url:
        return {"ok": False, "skipped": True, "error": "AzuraCast base URL is not configured."}
    if not (config.station_shortcode or config.station_id):
        return {"ok": False, "skipped": True, "error": "AzuraCast station shortcode or station ID is not configured."}
    api_key = os.getenv("AZURACAST_API_KEY")
    if not api_key:
        return {"ok": False, "error": "AZURACAST_API_KEY is not configured."}

    url = resolve_podcast_api_url(config, source_config)
    headers = {"Authorization": f"Bearer {api_key}"}
    _emit(event_store, run, "azuracast_podcast_readiness_started", "in_progress", "Checking AzuraCast podcast readiness.", {"podcast_api_url": url})

    attempts = max(1, int(timeout_seconds // max(1, poll_interval_seconds)) + 1)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = http_get(url, headers=headers, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            last_error = f"AzuraCast podcast API request failed: {exc.__class__.__name__}"
            _emit(event_store, run, "azuracast_podcast_readiness_failed", "failed", last_error, {"podcast_api_url": url})
            return {"ok": False, "error": last_error, "details": {"podcast_api_url": url}}
        except ValueError:
            last_error = "AzuraCast podcast API response was not valid JSON."
            _emit(event_store, run, "azuracast_podcast_readiness_failed", "failed", last_error, {"podcast_api_url": url})
            return {"ok": False, "error": last_error, "details": {"podcast_api_url": url}}

        episode = find_published_episode(payload, source_config=source_config)
        if episode:
            details = {
                "podcast_api_url": url,
                "podcast_episode_id": episode.get("id") or episode.get("guid"),
                "podcast_episode_title": episode.get("title") or episode.get("name"),
                "podcast_episode_status": episode.get("status"),
                "readiness_attempt": attempt,
            }
            _emit(event_store, run, "azuracast_podcast_readiness_succeeded", "success", "AzuraCast podcast episode is published.", details)
            return {"ok": True, "details": details}

        last_error = "AzuraCast podcast episode is not published yet."
        if attempt < attempts:
            sleep_func(poll_interval_seconds)

    _emit(event_store, run, "azuracast_podcast_readiness_timeout", "failed", last_error, {"podcast_api_url": url, "attempts": attempts})
    return {"ok": False, "error": "AzuraCast podcast readiness timed out.", "details": {"podcast_api_url": url, "attempts": attempts}}


def resolve_podcast_api_url(config, source_config):
    station = quote(str(config.station_id or config.station_shortcode), safe="")
    base_url = config.base_url.rstrip("/")
    podcast = getattr(source_config, "podcast_identifier", None)
    if podcast:
        return f"{base_url}/api/station/{station}/podcasts/{quote(str(podcast), safe='')}/episodes"
    return f"{base_url}/api/station/{station}/podcasts"


def find_published_episode(payload, source_config=None):
    episodes = normalize_episode_list(payload)
    podcast_identifier = str(getattr(source_config, "podcast_identifier", "") or "").lower()
    if podcast_identifier:
        episodes = [episode for episode in episodes if episode_matches_podcast(episode, podcast_identifier)]
    for episode in episodes:
        if is_episode_published(episode):
            return episode
    return None


def normalize_episode_list(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("episodes", "items", "data", "podcasts"):
        value = payload.get(key)
        if isinstance(value, list):
            if key == "podcasts":
                nested = []
                for podcast in value:
                    if isinstance(podcast, dict) and isinstance(podcast.get("episodes"), list):
                        nested.extend(item for item in podcast["episodes"] if isinstance(item, dict))
                    elif isinstance(podcast, dict):
                        nested.append(podcast)
                return nested
            return [item for item in value if isinstance(item, dict)]
    return [payload]


def episode_matches_podcast(episode, podcast_identifier):
    values = [
        episode.get("podcast_id"),
        episode.get("podcast"),
        episode.get("podcast_slug"),
        episode.get("podcast_short_name"),
        episode.get("slug"),
    ]
    nested = episode.get("podcast")
    if isinstance(nested, dict):
        values.extend([nested.get("id"), nested.get("slug"), nested.get("name"), nested.get("short_name")])
    return any(str(value or "").lower() == podcast_identifier for value in values)


def is_episode_published(episode):
    if episode.get("is_published") is True or episode.get("published") is True:
        return True
    status = str(episode.get("status") or episode.get("publish_status") or "").lower()
    if status in {"published", "complete", "completed"}:
        return True
    return bool(episode.get("published_at") or episode.get("publish_at"))


def select_matching_enclosure(items, run):
    start = parse_datetime(run.get("started_at"))
    if not start:
        return None
    candidates = []
    for item in items:
        if not item.get("enclosure_url"):
            continue
        published_at = parse_datetime(item.get("pub_date"))
        if not published_at or published_at < start:
            continue
        candidate = dict(item)
        candidate["published_at"] = published_at.isoformat()
        candidates.append(candidate)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: parse_datetime(item.get("published_at")) or start, reverse=True)[0]


def download_audio_asset(enclosure_url, *, http_get, event_store, run):
    _emit(event_store, run, "acquire_mp3.download_started", "in_progress", "Downloading RSS enclosure audio.", {"enclosure_url": enclosure_url})
    try:
        response = http_get(enclosure_url, stream=True, timeout=DEFAULT_DOWNLOAD_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        message = f"RSS enclosure download failed: {exc.__class__.__name__}"
        _emit(event_store, run, "acquire_mp3.download_failed", "failed", message, {"enclosure_url": enclosure_url})
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
    _emit(event_store, run, "acquire_mp3.download_succeeded", "success", "RSS enclosure audio downloaded.", {"audio_size_bytes": asset.size_bytes, "audio_content_type": content_type})
    validate_audio_asset(asset, event_store=event_store, run=run)
    return asset


def validate_audio_asset(asset, *, event_store, run):
    if not os.path.exists(asset.path) or asset.size_bytes <= 0:
        message = "Downloaded audio asset is empty."
        _emit(event_store, run, "acquire_mp3.validation_failed", "failed", message, {"audio_size_bytes": asset.size_bytes})
        raise PipelineMp3Error(message)
    content_type = str(asset.content_type or "").split(";", 1)[0].lower()
    if content_type and content_type not in MP3_CONTENT_TYPES and not asset.filename.lower().endswith(".mp3"):
        message = "Downloaded audio asset does not look like an MP3."
        _emit(event_store, run, "acquire_mp3.validation_failed", "failed", message, {"audio_content_type": asset.content_type, "filename": asset.filename})
        raise PipelineMp3Error(message)
    _emit(event_store, run, "acquire_mp3.validation_succeeded", "success", "Downloaded audio asset validated.", {"audio_size_bytes": asset.size_bytes, "audio_content_type": asset.content_type})


def create_castopod_draft_from_asset(run, asset, *, http_post, event_store):
    title = run.get("show_name") or run.get("station") or "AzuraCast Podcast Episode"
    description = "Draft created from AzuraCast podcast RSS enclosure."
    _emit(event_store, run, "castopod_draft.create_started", "in_progress", "Creating Castopod draft from acquired audio.", {"audio_filename": asset.filename})
    result = create_castopod_draft_episode(
        audio_path=asset.path,
        filename=asset.filename,
        title=title,
        description=description,
        http_post=http_post,
    )
    if result["ok"]:
        _emit(event_store, run, "castopod_draft.create_succeeded", "success", "Castopod draft created from acquired audio.", {"castopod_episode_id": result.get("episode_id"), "castopod_episode_url": result.get("episode_url")})
    else:
        _emit(event_store, run, "castopod_draft.create_failed", "failed", result["error"], {"status_code": result.get("status_code")})
    return result


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


def _record_castopod_draft(store, run_id, result):
    run = store.get_run(run_id)
    if not run:
        return
    now = utc_now()
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE pipeline_runs
            SET castopod_episode_id = COALESCE(?, castopod_episode_id),
                castopod_episode_url = COALESCE(?, castopod_episode_url),
                updated_at = ?
            WHERE run_id = ?
            """,
            (result.get("castopod_episode_id"), result.get("castopod_episode_url"), now, run_id),
        )


def _emit(event_store, run, event_name, status, message, details=None):
    return event_store.emit(
        run_id=run["run_id"],
        session_id=run.get("session_id"),
        step_key="acquire_mp3",
        event_name=event_name,
        status=status,
        message=message,
        details=details or {},
        level="ERROR" if status == "failed" else "INFO",
    )


class PipelineMp3Error(Exception):
    pass
