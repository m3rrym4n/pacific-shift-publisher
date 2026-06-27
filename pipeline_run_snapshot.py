import json
import re
import uuid
from contextlib import closing
from datetime import datetime, timezone

from pipeline_constants import PIPELINE_STATUSES, PIPELINE_STEP_KEYS
from pipeline_logging import StructuredPipelineLogger


SNAPSHOT_SCHEMA_VERSION = 1
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(authorization|cookie|api[_-]?key|apikey|password|secret|token|bearer|credential)"
)
BLOCKED_KEYS = {
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
SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)(authorization:\s*bearer)\s+[^\s,;\"']+"),
    re.compile(r"(?i)(bearer)\s+[^\s,;\"']+"),
    re.compile(r"(?i)((?:api[_-]?key|apikey|token|password|secret|authorization)\s*[:=]\s*)[^\s,;\"']+"),
)


class SnapshotImportError(ValueError):
    pass


def export_run_snapshot(run_id, store, event_store=None):
    run = store.get_run(run_id)
    if not run:
        return None

    event_store = event_store or StructuredPipelineLogger(store.db_path)
    events = event_store.find_events(run_id=run_id)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "exported_at": _utc_now(),
        "run": _redact(run),
        "events": [_redact(event) for event in events],
    }


def import_run_snapshot(snapshot, store):
    snapshot = _validate_snapshot(snapshot)
    run = snapshot["run"]
    events = snapshot.get("events") or []
    run_id = run["run_id"]

    store.initialize()
    StructuredPipelineLogger(store.db_path).initialize()
    if store.get_run(run_id):
        raise SnapshotImportError(f"Pipeline run already exists: {run_id}")

    now = _utc_now()
    with closing(store.connect()) as connection:
        connection.execute("BEGIN")
        try:
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                    run_id, station, show_name, streamer, started_at, ended_at,
                    overall_status, current_step, session_id, broadcast_id, recording_reference,
                    assembled_episode_payload,
                    tracklist_status, castopod_episode_id, castopod_episode_url,
                    error_summary, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    run.get("station"),
                    run.get("show_name"),
                    run.get("streamer"),
                    run.get("started_at"),
                    run.get("ended_at"),
                    run.get("overall_status"),
                    run.get("current_step"),
                    run.get("session_id") or run_id,
                    run.get("broadcast_id"),
                    run.get("recording_reference"),
                    _serialize_error_details(_redact(run.get("assembled_episode_payload"))),
                    run.get("tracklist_status"),
                    run.get("castopod_episode_id"),
                    run.get("castopod_episode_url"),
                    run.get("error_summary"),
                    run.get("created_at") or now,
                    run.get("updated_at") or now,
                ),
            )
            for sort_order, step in enumerate(_steps_by_pipeline_order(run), start=1):
                connection.execute(
                    """
                    INSERT INTO pipeline_steps (
                        run_id, step_key, status, started_at, ended_at, duration_ms,
                        message, error_details, retry_count, sort_order, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        step["step_key"],
                        step["status"],
                        step.get("started_at"),
                        step.get("ended_at"),
                        step.get("duration_ms"),
                        _redact_text(step.get("message")),
                        _serialize_error_details(_redact(step.get("error_details"))),
                        int(step.get("retry_count") or 0),
                        sort_order,
                        run.get("created_at") or now,
                        step.get("updated_at") or run.get("updated_at") or now,
                    ),
                )
            for event in events:
                connection.execute(
                    """
                    INSERT INTO pipeline_events (
                        event_id, timestamp, level, run_id, session_id, step_key,
                        event_name, status, message, details_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        event.get("timestamp") or now,
                        str(event.get("level") or "INFO").upper(),
                        run_id,
                        event.get("session_id") or run.get("session_id"),
                        event["step_key"],
                        str(event.get("event_name") or "pipeline_run.imported"),
                        event["status"],
                        _redact_text(event.get("message") or ""),
                        json.dumps(_redact(event.get("details") or {}), sort_keys=True),
                    ),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    return store.get_run(run_id)


def load_snapshot_file(file_storage):
    try:
        payload = file_storage.read()
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotImportError("Import file must be a valid JSON snapshot.") from exc


def snapshot_filename(run_id, exported_at=None):
    exported_at = exported_at or datetime.now(timezone.utc)
    return f"publisher-run-{run_id[:8]}-{exported_at.strftime('%Y%m%dT%H%M%SZ')}.json"


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise SnapshotImportError("Import file must contain a JSON object.")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotImportError("Unsupported pipeline run snapshot schema version.")
    run = snapshot.get("run")
    if not isinstance(run, dict):
        raise SnapshotImportError("Snapshot is missing run data.")
    if not run.get("run_id"):
        raise SnapshotImportError("Snapshot is missing run id.")
    if run.get("overall_status") not in PIPELINE_STATUSES:
        raise SnapshotImportError("Snapshot has an unsupported run status.")
    if run.get("current_step") and run.get("current_step") not in PIPELINE_STEP_KEYS:
        raise SnapshotImportError("Snapshot has an unsupported current step.")
    steps = run.get("steps")
    if not isinstance(steps, list):
        raise SnapshotImportError("Snapshot is missing pipeline step state.")
    for step in steps:
        if not isinstance(step, dict):
            raise SnapshotImportError("Snapshot contains an invalid pipeline step.")
        if step.get("step_key") not in PIPELINE_STEP_KEYS:
            raise SnapshotImportError("Snapshot contains an unsupported pipeline step.")
        if step.get("status") not in PIPELINE_STATUSES:
            raise SnapshotImportError("Snapshot contains an unsupported pipeline step status.")
    events = snapshot.get("events") or []
    if not isinstance(events, list):
        raise SnapshotImportError("Snapshot events must be a list.")
    for event in events:
        if not isinstance(event, dict):
            raise SnapshotImportError("Snapshot contains an invalid pipeline event.")
        if event.get("step_key") not in PIPELINE_STEP_KEYS:
            raise SnapshotImportError("Snapshot contains an unsupported event step.")
        if event.get("status") not in PIPELINE_STATUSES:
            raise SnapshotImportError("Snapshot contains an unsupported event status.")
    return snapshot


def _steps_by_pipeline_order(run):
    by_key = {step["step_key"]: step for step in run.get("steps") or []}
    steps = []
    for step_key in PIPELINE_STEP_KEYS:
        if step_key in by_key:
            steps.append(by_key[step_key])
        else:
            steps.append({"step_key": step_key, "status": "pending"})
    return steps


def _redact(value, key=None):
    if key and _is_blocked_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {item_key: _redact(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(value)


def _redact_text(value):
    text = str(value)
    for pattern in SECRET_VALUE_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)} [redacted]" if "bearer" in match.group(1).lower() else f"{match.group(1)}[redacted]", text)
    return text


def _is_blocked_key(key):
    normalized = str(key).lower()
    return normalized in BLOCKED_KEYS or SENSITIVE_KEY_PATTERN.search(normalized) is not None


def _serialize_error_details(value):
    if value in (None, "", {}, []):
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
