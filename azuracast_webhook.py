import logging
import re
from datetime import datetime, timezone

from pipeline_logging import get_pipeline_logger, sanitize_log_value
from pipeline_state import get_pipeline_store, utc_now


LOGGER = logging.getLogger(__name__)

EVENT_ALIASES = {
    "streamer_start": "streamer_start",
    "streamer_started": "streamer_start",
    "streamer_online": "streamer_start",
    "stream_start": "streamer_start",
    "start": "streamer_start",
    "live_connected": "streamer_start",
    "streamer_stop": "streamer_stop",
    "streamer_stopped": "streamer_stop",
    "streamer_offline": "streamer_stop",
    "stream_stop": "streamer_stop",
    "stop": "streamer_stop",
    "live_disconnected": "streamer_stop",
}

EVENT_STEP = {
    "streamer_start": "stream_start",
    "streamer_stop": "stream_end",
}

NOW_PLAYING_CLASS_KEY = "App\\Entity\\Api\\NowPlaying\\NowPlaying"
WEBHOOK_EVENT_RUN_ID = "azuracast-webhook"


def normalize_event_type(payload):
    value = first_present(
        payload,
        "event",
        "event_type",
        "type",
        "name",
        "trigger",
        "webhook_event",
    )
    if not value:
        return None

    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return EVENT_ALIASES.get(normalized)


def parse_webhook_payload(payload, received_at=None):
    received_at = received_at or utc_now()
    now_playing = extract_now_playing_payload(payload)
    if now_playing:
        return parse_now_playing_payload(payload, now_playing, received_at)

    event_type = normalize_event_type(payload)
    timestamp = parse_timestamp(
        first_present(payload, "timestamp", "time", "created_at", "event_time"),
        fallback=received_at,
    )

    station = value_from_path(payload, ("station", "name")) or first_present(
        payload,
        "station",
        "station_name",
        "station_short_name",
    )
    streamer = value_from_path(payload, ("streamer", "name")) or first_present(
        payload,
        "streamer",
        "streamer_name",
        "dj",
        "username",
    )
    show_name = value_from_path(payload, ("show", "name")) or first_present(
        payload,
        "show_name",
        "show",
        "title",
    )
    session_id = first_present(
        payload,
        "session_id",
        "session",
        "session_key",
        "event_id",
        "id",
    )
    raw_event_id = first_present(payload, "event_id", "id", "uuid")

    return {
        "event_type": event_type,
        "step_key": EVENT_STEP.get(event_type),
        "station": clean_text(station),
        "show_name": clean_text(show_name),
        "streamer": clean_text(streamer),
        "timestamp": timestamp,
        "session_id": clean_text(session_id),
        "raw_event_id": clean_text(raw_event_id),
        "payload_kind": "lifecycle",
        "is_live": None,
        "station_shortcode": None,
    }


def extract_now_playing_payload(payload):
    if not isinstance(payload, dict):
        return None
    wrapper = payload.get("np")
    if not isinstance(wrapper, dict):
        return None
    if isinstance(wrapper.get("station"), dict) and isinstance(wrapper.get("live"), dict):
        return wrapper
    candidate = wrapper.get(NOW_PLAYING_CLASS_KEY)
    if isinstance(candidate, dict):
        return candidate
    for value in wrapper.values():
        if isinstance(value, dict) and isinstance(value.get("station"), dict) and isinstance(value.get("live"), dict):
            return value
    return None


def parse_now_playing_payload(payload, now_playing, received_at):
    station_data = now_playing.get("station") if isinstance(now_playing.get("station"), dict) else {}
    live_data = now_playing.get("live") if isinstance(now_playing.get("live"), dict) else {}
    current_song = now_playing.get("now_playing") if isinstance(now_playing.get("now_playing"), dict) else {}

    is_live = bool(live_data.get("is_live"))
    event_type = "streamer_start" if is_live else None
    station_name = clean_text(station_data.get("name"))
    station_shortcode = clean_text(station_data.get("shortcode"))
    streamer = clean_text(live_data.get("streamer_name") or current_song.get("streamer"))
    broadcast_start = live_data.get("broadcast_start")
    played_at = current_song.get("played_at")
    explicit_timestamp = first_present(payload, "timestamp", "time", "created_at", "event_time")
    timestamp = parse_timestamp(broadcast_start if is_live else explicit_timestamp, fallback=received_at)

    explicit_session_id = first_present(payload, "session_id", "session", "session_key")
    session_id = explicit_session_id
    if not session_id and is_live and broadcast_start:
        session_id = ":".join(
            part for part in ("azuracast", station_shortcode or station_name, streamer, str(broadcast_start)) if part
        )

    return {
        "event_type": event_type,
        "step_key": EVENT_STEP.get(event_type),
        "station": station_name or station_shortcode,
        "show_name": station_name,
        "streamer": streamer,
        "timestamp": timestamp,
        "session_id": clean_text(session_id),
        "raw_event_id": clean_text(first_present(payload, "event_id", "id", "uuid")),
        "payload_kind": "now_playing",
        "is_live": is_live,
        "station_shortcode": station_shortcode,
        "now_playing_played_at": parse_timestamp(played_at, fallback=None) if played_at else None,
    }


def handle_azuracast_webhook(payload, store=None, event_store=None):
    store = store or get_pipeline_store()
    event_store = event_store or get_pipeline_logger()
    received_at = utc_now()
    parsed = parse_webhook_payload(payload, received_at=received_at)

    if parsed["payload_kind"] == "now_playing" and parsed["event_type"] is None:
        return _handle_now_playing_non_live(parsed, store, event_store)

    if not parsed["event_type"]:
        LOGGER.warning(
            "Unsupported AzuraCast webhook event: %s",
            sanitize_log_value(first_present(payload, "event", "event_type", "type")),
        )
        _emit_system_webhook_event(
            event_store,
            "azuracast_webhook_unsupported",
            "failed",
            "Unsupported AzuraCast webhook event.",
            parsed,
            "stream_start",
        )
        return {
            "ok": False,
            "status_code": 400,
            "message": "Unsupported AzuraCast webhook event.",
            "event_type": None,
            "run": None,
        }

    if parsed["event_type"] == "streamer_start":
        return _handle_streamer_start(parsed, store, event_store)
    return _handle_streamer_stop(parsed, store, event_store)


def _handle_streamer_start(parsed, store, event_store):
    run = _existing_start_run(parsed, store)
    duplicate = bool(run and _step_status(run, "stream_start") == "success")
    if not run:
        run = store.mark_stream_start(
            session_id=parsed["session_id"],
            station=parsed["station"],
            show_name=parsed["show_name"],
            streamer=parsed["streamer"],
            started_at=parsed["timestamp"],
        )
    else:
        run = store.mark_stream_start(
            session_id=run["session_id"],
            station=parsed["station"],
            show_name=parsed["show_name"],
            streamer=parsed["streamer"],
            started_at=parsed["timestamp"],
        )

    _emit_webhook_event(
        event_store,
        run,
        "azuracast_nowplaying_received" if parsed["payload_kind"] == "now_playing" else "azuracast_webhook_received",
        "success",
        (
            "AzuraCast Now Playing live webhook received."
            if parsed["payload_kind"] == "now_playing"
            else "AzuraCast streamer start webhook received."
        ),
        parsed,
        "stream_start",
    )
    _emit_webhook_event(
        event_store,
        run,
        (
            "azuracast_webhook_duplicate"
            if duplicate
            else (
                "azuracast_nowplaying_live_started"
                if parsed["payload_kind"] == "now_playing"
                else "azuracast_stream_start_recorded"
            )
        ),
        "success",
        (
            "Duplicate streamer start ignored."
            if duplicate
            else (
                "Now Playing live stream start recorded."
                if parsed["payload_kind"] == "now_playing"
                else "Streamer start recorded."
            )
        ),
        parsed,
        "stream_start",
    )

    return {
        "ok": True,
        "status_code": 200,
        "message": "Streamer start recorded.",
        "event_type": parsed["event_type"],
        "run": run,
        "duplicate": duplicate,
    }


def _handle_streamer_stop(parsed, store, event_store):
    run = _matching_stop_run(parsed, store)
    out_of_order = False
    duplicate = False

    if not run:
        out_of_order = True
        run = store.create_run(
            session_id=parsed["session_id"],
            station=parsed["station"],
            show_name=parsed["show_name"],
            streamer=parsed["streamer"],
        )

    duplicate = bool(run.get("ended_at") or _step_status(run, "stream_end") == "success")
    run = store.mark_stream_end(
        run_id=run["run_id"],
        ended_at=run["ended_at"] if duplicate and run.get("ended_at") else parsed["timestamp"],
        message="Stream ended.",
    )

    _emit_webhook_event(
        event_store,
        run,
        "azuracast_nowplaying_received" if parsed["payload_kind"] == "now_playing" else "azuracast_webhook_received",
        "success",
        (
            "AzuraCast Now Playing non-live webhook received."
            if parsed["payload_kind"] == "now_playing"
            else "AzuraCast streamer stop webhook received."
        ),
        parsed,
        "stream_end",
    )
    if out_of_order:
        event_name = "azuracast_webhook_out_of_order"
        message = "Streamer stop received without a matching active start."
    elif duplicate:
        event_name = "azuracast_webhook_duplicate"
        message = "Duplicate streamer stop ignored."
    else:
        event_name = (
            "azuracast_nowplaying_live_stopped"
            if parsed["payload_kind"] == "now_playing"
            else "azuracast_stream_stop_recorded"
        )
        message = (
            "Now Playing live stream stop recorded."
            if parsed["payload_kind"] == "now_playing"
            else "Streamer stop recorded."
        )
    _emit_webhook_event(event_store, run, event_name, "success", message, parsed, "stream_end")

    return {
        "ok": True,
        "status_code": 200,
        "message": message,
        "event_type": parsed["event_type"],
        "run": run,
        "duplicate": duplicate,
        "out_of_order": out_of_order,
    }


def _handle_now_playing_non_live(parsed, store, event_store):
    run = _matching_stop_run(parsed, store)
    if run:
        parsed["event_type"] = "streamer_stop"
        parsed["step_key"] = "stream_end"
        return _handle_streamer_stop(parsed, store, event_store)

    _emit_system_webhook_event(
        event_store,
        "azuracast_nowplaying_received",
        "success",
        "AzuraCast Now Playing non-live webhook received.",
        parsed,
        "stream_start",
    )
    _emit_system_webhook_event(
        event_store,
        "azuracast_nowplaying_non_live",
        "skipped",
        "Now Playing payload is not live; no active run matched.",
        parsed,
        "stream_start",
    )
    return {
        "ok": True,
        "status_code": 200,
        "message": "Now Playing payload is not live; no active run matched.",
        "event_type": "now_playing_non_live",
        "run": None,
    }


def _existing_start_run(parsed, store):
    if parsed["session_id"]:
        run = store.get_run_by_session_id(parsed["session_id"])
        if run:
            return run
    return store.find_active_run(station=parsed["station"], streamer=parsed["streamer"])


def _matching_stop_run(parsed, store):
    if parsed["session_id"]:
        run = store.get_run_by_session_id(parsed["session_id"])
        if run:
            return run
    run = store.find_active_run(station=parsed["station"], streamer=parsed["streamer"])
    if run:
        return run
    if parsed.get("station") and not parsed.get("streamer"):
        return store.find_active_run(station=parsed["station"])
    return None


def _emit_webhook_event(event_store, run, event_name, status, message, parsed, step_key):
    event_store.emit(
        run_id=run["run_id"],
        session_id=run["session_id"],
        step_key=step_key,
        event_name=event_name,
        status=status,
        message=message,
        details={
            "azuracast_event_type": parsed["event_type"],
            "station": parsed["station"],
            "show_name": parsed["show_name"],
            "streamer": parsed["streamer"],
            "event_timestamp": parsed["timestamp"],
            "raw_event_id": parsed["raw_event_id"],
            "payload_kind": parsed["payload_kind"],
            "station_shortcode": parsed["station_shortcode"],
            "now_playing_played_at": parsed.get("now_playing_played_at"),
        },
    )


def _emit_system_webhook_event(event_store, event_name, status, message, parsed, step_key):
    event_store.emit(
        run_id=WEBHOOK_EVENT_RUN_ID,
        session_id=parsed.get("station_shortcode") or parsed.get("station"),
        step_key=step_key,
        event_name=event_name,
        status=status,
        message=message,
        details={
            "azuracast_event_type": parsed["event_type"],
            "station": parsed["station"],
            "show_name": parsed["show_name"],
            "streamer": parsed["streamer"],
            "event_timestamp": parsed["timestamp"],
            "raw_event_id": parsed["raw_event_id"],
            "payload_kind": parsed["payload_kind"],
            "station_shortcode": parsed["station_shortcode"],
            "is_live": parsed["is_live"],
            "now_playing_played_at": parsed.get("now_playing_played_at"),
        },
    )


def _step_status(run, step_key):
    for step in run.get("steps", []):
        if step["step_key"] == step_key:
            return step["status"]
    return None


def first_present(payload, *keys):
    for key in keys:
        value = payload.get(key) if isinstance(payload, dict) else None
        if value not in (None, ""):
            return value
    return None


def value_from_path(payload, path):
    value = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def clean_text(value):
    if value is None:
        return None
    value = str(sanitize_log_value(value)).strip()
    return value or None


def parse_timestamp(value, fallback=None):
    if not value:
        return fallback or utc_now()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return fallback or utc_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()
