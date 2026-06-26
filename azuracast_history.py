import os
from dataclasses import dataclass
from urllib.parse import quote

import requests

from azuracast_config import get_azuracast_config
from pipeline_state import get_pipeline_store
from tracklist import (
    append_tracklist_to_description,
    filter_tracks_for_session,
    format_tracklist,
    parse_song_history,
)


DEFAULT_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class AzuraCastHistoryResult:
    ok: bool
    payload: dict | None = None
    endpoint_url: str | None = None
    error: str | None = None
    status_code: int | None = None


def resolve_nowplaying_history_url(config):
    if config.nowplaying_url:
        return config.nowplaying_url
    if not config.base_url:
        return None
    base_url = config.base_url.rstrip("/")
    if config.station_shortcode:
        shortcode = quote(config.station_shortcode, safe="")
        return f"{base_url}/api/nowplaying_static/{shortcode}.json"
    if config.station_id:
        station_id = quote(str(config.station_id), safe="")
        return f"{base_url}/api/nowplaying/{station_id}"
    return None


def load_azuracast_history(config=None, http_get=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    config = config or get_azuracast_config()
    if not config.enabled:
        return AzuraCastHistoryResult(ok=False, error="AzuraCast integration is disabled.")

    endpoint_url = resolve_nowplaying_history_url(config)
    if not endpoint_url:
        return AzuraCastHistoryResult(
            ok=False,
            error="AzuraCast Now Playing history endpoint is not configured.",
        )

    headers = {}
    api_key = os.getenv("AZURACAST_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    http_get = http_get or requests.get
    try:
        response = http_get(endpoint_url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        return AzuraCastHistoryResult(
            ok=False,
            endpoint_url=endpoint_url,
            error=f"AzuraCast history request failed: {exc.__class__.__name__}",
            status_code=getattr(getattr(exc, "response", None), "status_code", None),
        )

    try:
        payload = response.json()
    except ValueError:
        return AzuraCastHistoryResult(
            ok=False,
            endpoint_url=endpoint_url,
            error="AzuraCast history response was not valid JSON.",
            status_code=response.status_code,
        )

    if not isinstance(payload, dict):
        return AzuraCastHistoryResult(
            ok=False,
            endpoint_url=endpoint_url,
            error="AzuraCast history response was not a JSON object.",
            status_code=response.status_code,
        )
    return AzuraCastHistoryResult(
        ok=True,
        payload=payload,
        endpoint_url=endpoint_url,
        status_code=response.status_code,
    )


def generate_tracklist_for_run(run_id, store=None, config=None, http_get=None):
    store = store or get_pipeline_store()
    run = store.get_run(run_id)
    if not run:
        return {
            "ok": False,
            "error": "Pipeline run was not found.",
            "tracklist": format_tracklist([]),
            "tracks": [],
        }
    if not run.get("started_at") or not run.get("ended_at"):
        return {
            "ok": False,
            "error": "Pipeline run does not have a completed session window.",
            "tracklist": format_tracklist([]),
            "tracks": [],
            "run": run,
        }

    history = load_azuracast_history(config=config, http_get=http_get)
    if not history.ok:
        return {
            "ok": False,
            "error": history.error,
            "endpoint_url": history.endpoint_url,
            "tracklist": format_tracklist([]),
            "tracks": [],
            "run": run,
        }

    parsed_tracks = parse_song_history(history.payload)
    tracks = filter_tracks_for_session(
        parsed_tracks,
        started_at=run["started_at"],
        ended_at=run["ended_at"],
    )
    return {
        "ok": True,
        "endpoint_url": history.endpoint_url,
        "tracklist": format_tracklist(tracks, started_at=run["started_at"]),
        "tracks": [track.as_dict() for track in tracks],
        "track_count_total": len(parsed_tracks),
        "track_count_filtered": len(tracks),
        "run": run,
    }


def prepare_description_with_tracklist(description, tracklist_text):
    return append_tracklist_to_description(description, tracklist_text)
