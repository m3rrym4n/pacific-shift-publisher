import os

import requests

from azuracast_config import AzuraCastConfigStore, get_azuracast_api_key
from castopod_client import create_castopod_draft_episode
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

    payload = run.get("assembled_episode_payload")
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
        if not result.get("ok"):
            status_code = result.get("status_code")
            suffix = f" (HTTP {status_code})" if status_code is not None else ""
            error = str(result.get("error") or "unknown error").rstrip(".")
            message = f"Castopod draft creation failed: {error}{suffix}."
            return store.mark_step_failed(
                run_id,
                "post_castopod_draft",
                message=message,
                error_details={"status_code": status_code},
            )
        if result.get("episode_id") is None:
            return store.mark_step_failed(
                run_id,
                "post_castopod_draft",
                message="Castopod draft creation failed: response did not include an episode ID.",
            )

        store.set_castopod_draft(
            run_id,
            result["episode_id"],
            result.get("episode_url"),
        )
        store.update_step_status(
            run_id,
            "post_castopod_draft",
            "success",
            message="Castopod draft created.",
            error_details={
                "castopod_episode_id": result["episode_id"],
                "castopod_episode_url": result.get("episode_url"),
            },
        )
        return store.mark_run_success(run_id)
    finally:
        if asset and os.path.exists(asset.path):
            os.remove(asset.path)


def _missing_payload_fields(payload):
    if not isinstance(payload, dict):
        return ["assembled episode payload"]
    return [field for field in ("title", "description", "audio_url") if not payload.get(field)]
