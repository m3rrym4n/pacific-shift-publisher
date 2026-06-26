from azuracast_history import generate_tracklist_for_run


SKIP_ERROR_FRAGMENTS = (
    "integration is disabled",
    "endpoint is not configured",
    "completed session window",
)


def acquire_tracklist_for_run(run_id, store, generator=None):
    generator = generator or generate_tracklist_for_run
    store.update_step_status(
        run_id,
        "acquire_tracklist",
        "in_progress",
        message="Acquiring AzuraCast tracklist.",
    )

    result = generator(run_id, store=store)
    details = _details_from_result(result)
    filtered_count = details.get("track_count_filtered", 0)

    if result.get("ok") and filtered_count > 0:
        return store.update_step_status(
            run_id,
            "acquire_tracklist",
            "success",
            message=f"Tracklist acquired with {filtered_count} track{'s' if filtered_count != 1 else ''}.",
            error_details=details,
        )

    if result.get("ok"):
        details["skip_reason"] = "No AzuraCast track history was found for this session window."
        return store.update_step_status(
            run_id,
            "acquire_tracklist",
            "skipped",
            message="Tracklist acquisition skipped: no tracks found for session window.",
            error_details=details,
        )

    error = result.get("error") or "Tracklist acquisition failed."
    details["failure_reason"] = error
    if _should_skip_error(error):
        details["skip_reason"] = error
        return store.update_step_status(
            run_id,
            "acquire_tracklist",
            "skipped",
            message=f"Tracklist acquisition skipped: {error}",
            error_details=details,
        )

    return store.mark_step_failed(
        run_id,
        "acquire_tracklist",
        message=f"Tracklist acquisition failed: {error}",
        error_details=details,
    )


def _details_from_result(result):
    details = {}
    if result.get("endpoint_url"):
        details["history_url_used"] = result["endpoint_url"]
    if "track_count_total" in result:
        details["track_count_total"] = result["track_count_total"]
    if "track_count_filtered" in result:
        details["track_count_filtered"] = result["track_count_filtered"]
    elif "tracks" in result:
        details["track_count_filtered"] = len(result.get("tracks") or [])
    if "tracks" in result:
        details["tracks"] = [_safe_track(track) for track in result.get("tracks") or []]
    if "tracklist" in result:
        details["tracklist"] = result["tracklist"]
    return details


def _should_skip_error(error):
    normalized = str(error or "").lower()
    return any(fragment in normalized for fragment in SKIP_ERROR_FRAGMENTS)


def _safe_track(track):
    return {
        "played_at": track.get("played_at"),
        "played_at_epoch": track.get("played_at_epoch"),
        "duration": track.get("duration"),
        "playlist": track.get("playlist"),
        "streamer": track.get("streamer"),
        "artist": track.get("artist"),
        "title": track.get("title"),
        "text": track.get("text"),
        "display": track.get("display"),
    }
