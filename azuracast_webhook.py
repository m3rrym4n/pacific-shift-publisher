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
    }


def handle_azuracast_webhook(payload, store=None, event_store=None):
    store = store or get_pipeline_store()
    event_store = event_store or get_pipeline_logger()
    received_at = utc_now()
    parsed = parse_webhook_payload(payload, received_at=received_at)

    if not parsed["event_type"]:
        LOGGER.warning(
            "Unsupported AzuraCast webhook event: %s",
            sanitize_log_value(first_present(payload, "event", "event_type", "type")),
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
        "azuracast_webhook_received",
        "success",
        "AzuraCast streamer start webhook received.",
        parsed,
        "stream_start",
    )
    _emit_webhook_event(
        event_store,
        run,
        "azuracast_webhook_duplicate" if duplicate else "azuracast_stream_start_recorded",
        "success",
        "Duplicate streamer start ignored." if duplicate else "Streamer start recorded.",
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
        "azuracast_webhook_received",
        "success",
        "AzuraCast streamer stop webhook received.",
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
        event_name = "azuracast_stream_stop_recorded"
        message = "Streamer stop recorded."
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
    return store.find_active_run(station=parsed["station"], streamer=parsed["streamer"])


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
