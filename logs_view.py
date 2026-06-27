import json
import re
from datetime import datetime, timezone

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
    "waiting_transcode": "info",
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

DETAIL_MODES = (
    {"value": "safe", "label": "Safe"},
    {"value": "verbose", "label": "Verbose"},
    {"value": "raw", "label": "Raw Debug"},
)

SECRET_VALUE_PATTERNS = (
    (
        re.compile(r"(?i)(\"(?:authorization|cookie|api[_-]?key|apikey|token|password|secret|bearer|azuracast_api_key)\"\s*:\s*\")[^\"]+(\")"),
        r"\1[redacted]\2",
    ),
    (
        re.compile(r"(?i)((?:authorization|cookie|api[_-]?key|apikey|token|password|secret|azuracast_api_key)\s*[:=]\s*)[^\s,;\"']+"),
        r"\1[redacted]",
    ),
    (
        re.compile(r"(?i)(authorization:\s*bearer)\s+[^\s,;\"']+"),
        r"\1 [redacted]",
    ),
    (
        re.compile(r"(?i)(bearer)\s+[^\s,;\"']+"),
        r"\1 [redacted]",
    ),
)


def build_logs_view_model(args=None, logger=None):
    args = args or {}
    detail_mode = resolve_log_detail_mode(args.get("detail_mode"))
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

    rows = [serialize_log_event(event, detail_mode["value"]) for event in events]
    return {
        "rows": rows,
        "has_events": bool(rows),
        "filters": filters,
        "active_filters": active_filters,
        "detail_mode": detail_mode,
        "detail_modes": DETAIL_MODES,
        "errors": errors,
        "step_options": PIPELINE_STEP_KEYS,
        "empty_message": "No pipeline events yet.",
        "empty_detail": "Publisher will show webhook, stream, and tracklist events here as the pipeline runs.",
    }


def build_logs_download(args=None, logger=None, generated_at=None):
    logs = build_logs_view_model(args=args, logger=logger)
    generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines = [
        "Pacific Shift Publisher Logs",
        f"Generated: {generated_at}",
        f"Detail mode: {logs['detail_mode']['value']}",
    ]
    for key, value in logs["active_filters"].items():
        lines.append(f"{key} filter: {value}")
    lines.append("")

    if not logs["rows"]:
        lines.append(logs["empty_message"])
    for row in logs["rows"]:
        lines.append(row["line_text"])
        if row.get("detail_json"):
            lines.append(row["detail_json"])
    return "\n".join(lines).rstrip() + "\n"


def logs_download_filename(generated_at=None):
    generated_at = generated_at or datetime.now(timezone.utc)
    return f"pacific-shift-publisher-logs-{generated_at.strftime('%Y%m%dT%H%M%SZ')}.txt"


def resolve_log_detail_mode(requested_mode):
    requested_mode = (requested_mode or "safe").strip().lower()
    notice = None
    warning = None
    if requested_mode not in {"safe", "verbose", "raw"}:
        requested_mode = "safe"
    if requested_mode == "raw":
        warning = "Raw Debug mode is enabled. Use only for local troubleshooting. Do not expose this view publicly."
    return {
        "value": requested_mode,
        "requested": requested_mode,
        "label": next(mode["label"] for mode in DETAIL_MODES if mode["value"] == requested_mode),
        "notice": notice,
        "warning": warning,
    }


def serialize_log_event(event, detail_mode="safe"):
    details = event.get("details") or {}
    row = {
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
        "details": safe_detail_pairs(details),
        "detail_json": render_event_details(details, detail_mode),
    }
    row["inline_details"] = inline_detail_pairs(row)
    row["line_text"] = format_log_line(row)
    return row


def format_log_line(row):
    tags = [
        row.get("timestamp") or "-",
        f"[{row.get('step_key') or '-'}]",
        f"[{row.get('event_name') or '-'}]",
        f"[{row.get('status') or '-'}]",
    ]
    line = " ".join(tags)
    if row.get("message"):
        line = f"{line} {row['message']}"
    if row["inline_details"]:
        detail_text = " ".join(f"{item['key']}={item['value']}" for item in row["inline_details"])
        line = f"{line} {detail_text}"
    return line


def inline_detail_pairs(row):
    pairs = []
    if row.get("run_id"):
        pairs.append({"key": "run", "value": row["short_run_id"]})
    if row.get("session_id"):
        pairs.append({"key": "session", "value": row["short_session_id"]})
    for detail in row.get("details") or []:
        if detail["key"] in {
            "station",
            "station_name",
            "station_shortcode",
            "streamer",
            "started_at",
            "ended_at",
            "history_url_used",
            "endpoint_url",
            "track_count_total",
            "track_count_filtered",
            "song_history_count",
            "parser_decision",
            "payload_kind",
            "error_summary",
        }:
            pairs.append({"key": compact_detail_key(detail["key"]), "value": detail["value"]})
    return pairs[:10]


def render_event_details(details, detail_mode):
    if detail_mode == "safe":
        return None
    if detail_mode == "raw":
        rendered = redact_hard_secrets(details or {})
    else:
        rendered = redact_hard_secrets(sanitize_log_value(details or {}))
    return json.dumps(rendered, indent=2, sort_keys=True)


def redact_hard_secrets(value, key=None):
    if key and is_secret_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {item_key: redact_hard_secrets(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_hard_secrets(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value

    text = str(value)
    for pattern, replacement in SECRET_VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def is_secret_key(key):
    return re.search(r"(?i)(authorization|cookie|api[_-]?key|apikey|password|secret|token|bearer|azuracast_api_key)", str(key)) is not None


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


def compact_detail_key(key):
    mapping = {
        "station_name": "station",
        "station_shortcode": "station_shortcode",
        "history_url_used": "history_url",
        "endpoint_url": "history_url",
        "track_count_total": "tracks_total",
        "track_count_filtered": "tracks_filtered",
        "song_history_count": "history_count",
        "parser_decision": "parser",
        "payload_kind": "payload",
        "error_summary": "error",
    }
    return mapping.get(key, key)


def shorten_identifier(value):
    if not value:
        return None
    text = str(value)
    return text if len(text) <= 12 else text[:8]
