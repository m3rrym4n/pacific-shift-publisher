import os
import json
from datetime import datetime

import requests

from azuracast_config import AzuraCastConfigStore, get_azuracast_api_key
from castopod_client import (
    castopod_config_from_environment,
    create_castopod_draft_episode,
    make_slug,
)
from pipeline_logging import StructuredPipelineLogger
from pipeline_mp3 import PipelineMp3Error, download_audio_asset


def post_castopod_draft_for_run(
    run_id,
    store,
    *,
    http_get=None,
    http_post=None,
    event_store=None,
):
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Unknown pipeline run: {run_id}")
    event_store = event_store or StructuredPipelineLogger(store.db_path)

    payload = run.get("assembled_episode_payload")
    slug = generate_episode_slug(run)
    if isinstance(payload, dict) and slug:
        payload = dict(payload)
        payload["slug"] = slug
        run = store.set_assembled_episode_payload(run_id, payload)

    if run.get("castopod_episode_id"):
        run = store.update_step_status(
            run_id,
            "post_castopod_draft",
            "success",
            message="Castopod draft already exists; duplicate creation skipped.",
            error_details={
                "castopod_episode_id": run["castopod_episode_id"],
                "castopod_episode_url": run.get("castopod_episode_url"),
                "duplicate_skipped": True,
            },
        )
        return store.mark_run_success(run_id)

    missing = _missing_payload_fields(payload)
    if missing:
        message = f"Castopod draft creation failed: missing {', '.join(missing)}."
        return store.mark_step_failed(
            run_id,
            "post_castopod_draft",
            message=message,
            error_details={"missing_fields": missing},
        )

    api_key = get_azuracast_api_key(AzuraCastConfigStore(store.db_path))
    if not api_key:
        return store.mark_step_failed(
            run_id,
            "post_castopod_draft",
            message="Castopod draft creation failed: AzuraCast API key is not configured.",
        )

    run = store.update_step_status(
        run_id,
        "post_castopod_draft",
        "in_progress",
        message="Downloading assembled episode audio for Castopod draft.",
    )
    asset = None
    try:
        try:
            asset = download_audio_asset(
                payload["audio_url"],
                http_get=http_get or requests.get,
                event_store=event_store,
                run=run,
                headers={"X-API-Key": api_key},
                step_key="post_castopod_draft",
            )
        except PipelineMp3Error as exc:
            return store.mark_step_failed(
                run_id,
                "post_castopod_draft",
                message=f"Castopod draft creation failed: {exc}",
            )

        result = create_castopod_draft_episode(
            audio_path=asset.path,
            filename=asset.filename,
            title=payload["title"],
            description=payload["description"],
            http_post=http_post,
        )
        status_code = result.get("status_code")
        if result.get("ok") or status_code in {200, 201}:
            return _record_success(run_id, store, result)

        if status_code == 409:
            event_store.emit(
                run_id=run_id,
                session_id=run.get("session_id"),
                step_key="post_castopod_draft",
                event_name="castopod_draft.already_exists",
                status="in_progress",
                message=f"Castopod episode already exists for slug {slug}; reconciling.",
                details={"episode_slug": slug},
            )
            episode = reconcile_episode_by_slug(
                slug,
                http_get=http_get or requests.get,
            )
            if episode:
                return _record_success(
                    run_id,
                    store,
                    {
                        "episode_id": episode.get("id"),
                        "episode_url": episode.get("url") or episode.get("link"),
                    },
                    message="Existing Castopod episode reconciled by slug.",
                )
            return store.mark_step_failed(
                run_id,
                "post_castopod_draft",
                message=(
                    "Episode already exists in Castopod but could not be located by slug. "
                    "Manual reconciliation required."
                ),
                error_details={"episode_slug": slug, "status_code": 409},
            )

        if not result.get("ok"):
            message = castopod_status_message(status_code)
            body_error = parse_castopod_error_body(
                result.get("detail"),
                status_code,
            )
            return store.mark_step_failed(
                run_id,
                "post_castopod_draft",
                message=message,
                error_details={
                    "status_code": status_code,
                    "castopod_error": body_error,
                },
            )
    finally:
        if asset and os.path.exists(asset.path):
            os.remove(asset.path)


def _missing_payload_fields(payload):
    if not isinstance(payload, dict):
        return ["assembled episode payload"]
    return [field for field in ("title", "description", "audio_url") if not payload.get(field)]


def generate_episode_slug(run):
    show_name = str(run.get("show_name") or "").strip()
    started_at = run.get("started_at")
    if not show_name or not started_at:
        return None
    try:
        session_date = datetime.fromisoformat(
            str(started_at).replace("Z", "+00:00")
        ).strftime("%Y%m%d")
    except ValueError:
        return None
    return make_slug(f"{show_name}-{session_date}")


def castopod_status_message(status_code):
    messages = {
        400: (
            "Castopod rejected the episode data. Check required fields, file types, "
            "slug format, and description."
        ),
        401: (
            "Castopod authentication failed. Verify Basic Auth is enabled and "
            "credentials are correct in Settings."
        ),
        404: (
            "Castopod could not find the podcast or user referenced by this request, "
            "or the REST API may be disabled."
        ),
        500: "Castopod hit an internal server error. Check Castopod logs.",
    }
    return messages.get(
        status_code,
        f"Unexpected response from Castopod (HTTP {status_code}).",
    )


def parse_castopod_error_body(body, status_code):
    if status_code == 401:
        return f"HTTP {status_code}"
    try:
        payload = json.loads(body) if isinstance(body, str) else body
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, dict) and messages.get("error"):
            return _error_text(messages["error"])
        if payload.get("error"):
            return _error_text(payload["error"])
    return f"HTTP {status_code}"


def reconcile_episode_by_slug(slug, *, http_get):
    if not slug:
        return None
    config = castopod_config_from_environment()
    if not config.get("castopod_url"):
        return None
    url = (
        f"{config['castopod_url'].rstrip('/')}/api/rest/v1/podcasts/"
        f"{config['podcast_id']}/episodes"
    )
    try:
        response = http_get(
            url,
            auth=(config.get("api_user"), config.get("api_pass")),
            headers={
                "Host": config["public_host"],
                "X-Forwarded-Proto": "https",
            },
            timeout=config["request_timeout"],
        )
        if response.status_code not in {200, 201}:
            return None
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    episodes = _episode_list(payload)
    return next((episode for episode in episodes if episode.get("slug") == slug), None)


def _record_success(run_id, store, result, message="Castopod draft created."):
    episode_id = result.get("episode_id")
    if episode_id is None:
        episode_id = result.get("id")
    if episode_id is None:
        return store.mark_step_failed(
            run_id,
            "post_castopod_draft",
            message="Castopod draft creation failed: response did not include an episode ID.",
        )
    episode_url = result.get("episode_url") or result.get("url") or result.get("link")
    store.set_castopod_draft(run_id, episode_id, episode_url)
    store.update_step_status(
        run_id,
        "post_castopod_draft",
        "success",
        message=message,
        error_details={
            "castopod_episode_id": episode_id,
            "castopod_episode_url": episode_url,
        },
    )
    return store.mark_run_success(run_id)


def _episode_list(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("episodes", "data", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _error_text(value):
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)
