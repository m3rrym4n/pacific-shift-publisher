import re

from pipeline_constants import PIPELINE_STEP_KEYS
from pipeline_logging import get_pipeline_logger, sanitize_log_value


SAFE_DETAIL_KEYS = (
    "station",
    "station_name",
    "station_shortcode",
    "streamer",
    "show_name",
    "started_at",
    "ended_at",
    "history_url_used",
    "endpoint_url",
    "track_count_total",
    "track_count_filtered",
    "song_history_count",
    "parser_decision",
    "parser_reason",
    "payload_kind",
    "error_summary",
    "json_parse_method",
    "top_level_keys",
    "top_level_value_types",
    "form_keys",
    "np_present",
    "np_value_type",
    "np_keys",
    "content_type",
    "content_length",
    "live_is_live",
    "live_streamer_name_present",
    "now_playing_streamer_present",
)

SENSITIVE_KEY_PATTERN = re.compile(r"(?i)(authorization|cookie|api[_-]?key|password|secret|token)")
BLOCKED_DETAIL_KEYS = {
    "raw",
    "raw_body",
    "raw_payload",
    "request_body",
    "request_payload",
    "payload",
    "body",
    "headers",
    "authorization",
    "cookie",
    "cookies",
    "song_history",
}

STATUS_CLASSES = {
    "pending": "secondary",
    "waiting": "info",
    "in_progress": "warning",
    "success": "success",
    "failed": "danger",
    "skipped": "secondary",
}

LEVEL_CLASSES = {
    "DEBUG": "secondary",
    "INFO": "info",
    "WARNING": "warning",
    "ERROR": "danger",
    "CRITICAL": "danger",
}


def build_logs_view_model(args=None, logger=None):
    args = args or {}
    filters = {
        "run_id": (args.get("run_id") or "").strip(),
        "session_id": (args.get("session_id") or "").strip(),
        "step_key": (args.get("step_key") or "").strip(),
        "event_name": (args.get("event_name") or "").strip(),
    }
    active_filters = {key: value for key, value in filters.items() if value}
    errors = []

    if filters["step_key"] and filters["step_key"] not in PIPELINE_STEP_KEYS:
        errors.append(f"Unsupported step key: {filters['step_key']}")
        events = []
    else:
        logger = logger or get_pipeline_logger()
        events = logger.find_events(**active_filters)

    rows = [serialize_log_event(event) for event in reversed(events)]
    return {
        "rows": rows,
        "has_events": bool(rows),
        "filters": filters,
        "active_filters": active_filters,
        "errors": errors,
        "step_options": PIPELINE_STEP_KEYS,
        "empty_message": "No pipeline events yet.",
        "empty_detail": "Publisher will show webhook, stream, and tracklist events here as the pipeline runs.",
    }


def serialize_log_event(event):
    return {
        "timestamp": event.get("timestamp"),
        "level": event.get("level") or "INFO",
        "level_class": LEVEL_CLASSES.get(event.get("level") or "INFO", "secondary"),
        "run_id": event.get("run_id"),
        "short_run_id": shorten_identifier(event.get("run_id")),
        "session_id": event.get("session_id"),
        "short_session_id": shorten_identifier(event.get("session_id")),
        "step_key": event.get("step_key"),
        "event_name": event.get("event_name"),
        "status": event.get("status"),
        "status_class": STATUS_CLASSES.get(event.get("status"), "secondary"),
        "message": event.get("message"),
        "details": safe_detail_pairs(event.get("details") or {}),
    }


def safe_detail_pairs(details):
    sanitized = sanitize_log_value(details or {})
    pairs = []
    for key in SAFE_DETAIL_KEYS:
        if key not in sanitized:
            continue
        value = safe_detail_value(key, sanitized[key])
        if value is not None:
            pairs.append({"key": key, "label": labelize(key), "value": value})
    return pairs


def safe_detail_value(key, value):
    normalized_key = str(key).lower()
    if normalized_key in BLOCKED_DETAIL_KEYS:
        return None
    if SENSITIVE_KEY_PATTERN.search(str(key)):
        return "[redacted]"
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return None
        if len(value) > 12:
            return f"{len(value)} values"
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        if not value:
            return None
        return ", ".join(f"{item_key}: {item_value}" for item_key, item_value in value.items())
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 240:
        return f"{text[:237]}..."
    return text


def labelize(key):
    return str(key).replace("_", " ").title()


def shorten_identifier(value):
    if not value:
        return None
    text = str(value)
    return text if len(text) <= 12 else text[:8]
