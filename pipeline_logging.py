import json
import logging
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from pipeline_constants import PIPELINE_STATUSES, PIPELINE_STEP_KEYS
from pipeline_state import default_db_path


PIPELINE_LOGGER_NAME = "publisher.pipeline"
SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)=([^\s]+)"),
    re.compile(r"(?i)(authorization:\s*bearer)\s+[^\s]+"),
    re.compile(r"(?i)(bearer)\s+[^\s]+"),
)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_log_value(value):
    if isinstance(value, dict):
        return {key: sanitize_log_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_log_value(item) for item in value]
    if value is None:
        return None

    sanitized = str(value)
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub(lambda match: f"{match.group(1)}=[redacted]", sanitized)
    return sanitized


class StructuredPipelineLogger:
    def __init__(self, db_path=None, logger=None):
        self.db_path = db_path or default_db_path()
        self.logger = logger or logging.getLogger(PIPELINE_LOGGER_NAME)

    def connect(self):
        path = Path(self.db_path)
        if path.parent and str(path.parent) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        with closing(self.connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_events (
                    event_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    session_id TEXT,
                    step_key TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT
                )
                """
            )

    def emit(
        self,
        *,
        run_id,
        step_key,
        event_name,
        status,
        message,
        level="INFO",
        session_id=None,
        details=None,
        timestamp=None,
    ):
        self._validate_step_key(step_key)
        self._validate_status(status)

        event = {
            "timestamp": timestamp or utc_now(),
            "level": str(level or "INFO").upper(),
            "run_id": run_id,
            "session_id": session_id,
            "step_key": step_key,
            "event_name": event_name,
            "status": status,
            "message": sanitize_log_value(message),
            "details": sanitize_log_value(details or {}),
        }

        self.initialize()
        with closing(self.connect()) as connection:
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
                    event["timestamp"],
                    event["level"],
                    event["run_id"],
                    event["session_id"],
                    event["step_key"],
                    event["event_name"],
                    event["status"],
                    event["message"],
                    json.dumps(event["details"], sort_keys=True),
                ),
            )

        self.logger.log(self._level_number(event["level"]), json.dumps(event, sort_keys=True))
        return event

    def emit_step_started(self, run, step_key, message=None, details=None):
        return self.emit(
            run_id=run["run_id"],
            session_id=run.get("session_id"),
            step_key=step_key,
            event_name=f"{step_key}.started",
            status="in_progress",
            message=message or f"{step_key} started.",
            details=details,
        )

    def emit_step_succeeded(self, run, step_key, message=None, details=None):
        return self.emit(
            run_id=run["run_id"],
            session_id=run.get("session_id"),
            step_key=step_key,
            event_name=f"{step_key}.succeeded",
            status="success",
            message=message or f"{step_key} succeeded.",
            details=details,
        )

    def emit_step_failed(self, run, step_key, message=None, details=None):
        return self.emit(
            run_id=run["run_id"],
            session_id=run.get("session_id"),
            step_key=step_key,
            event_name=f"{step_key}.failed",
            status="failed",
            message=message or f"{step_key} failed.",
            details=details,
            level="ERROR",
        )

    def find_events(self, run_id=None, session_id=None, step_key=None):
        self.initialize()
        clauses = []
        params = []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if step_key:
            self._validate_step_key(step_key)
            clauses.append("step_key = ?")
            params.append(step_key)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM pipeline_events
                {where}
                ORDER BY timestamp ASC
                """,
                params,
            ).fetchall()

        return [self.serialize_event(row) for row in rows]

    def serialize_event(self, row):
        return {
            "timestamp": row["timestamp"],
            "level": row["level"],
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "step_key": row["step_key"],
            "event_name": row["event_name"],
            "status": row["status"],
            "message": row["message"],
            "details": json.loads(row["details_json"] or "{}"),
        }

    def _validate_step_key(self, step_key):
        if step_key not in PIPELINE_STEP_KEYS:
            raise ValueError(f"Unsupported pipeline step: {step_key}")

    def _validate_status(self, status):
        if status not in PIPELINE_STATUSES:
            raise ValueError(f"Unsupported pipeline status: {status}")

    def _level_number(self, level):
        return getattr(logging, str(level).upper(), logging.INFO)


def get_pipeline_logger():
    return StructuredPipelineLogger()
