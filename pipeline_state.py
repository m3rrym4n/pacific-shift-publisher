import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from pipeline_constants import PIPELINE_STATUSES, PIPELINE_STEP_KEYS

TERMINAL_STATUSES = {"success", "failed", "skipped"}


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_db_path():
    return os.getenv("PUBLISHER_STATE_DB", "/app/data/publisher_state.sqlite")


class PipelineStateStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or default_db_path()

    def connect(self):
        path = Path(self.db_path)
        if path.parent and str(path.parent) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id TEXT PRIMARY KEY,
                    station TEXT,
                    show_name TEXT,
                    streamer TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    overall_status TEXT NOT NULL,
                    current_step TEXT,
                    session_id TEXT UNIQUE,
                    recording_reference TEXT,
                    tracklist_status TEXT,
                    castopod_episode_id TEXT,
                    castopod_episode_url TEXT,
                    error_summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pipeline_steps (
                    run_id TEXT NOT NULL,
                    step_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    duration_ms INTEGER,
                    message TEXT,
                    error_details TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, step_key),
                    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
                );
                """
            )

    def create_run(
        self,
        station=None,
        show_name=None,
        streamer=None,
        session_id=None,
        recording_reference=None,
        run_id=None,
    ):
        from pipeline_logging import StructuredPipelineLogger

        self.initialize()
        now = utc_now()
        run_id = run_id or str(uuid.uuid4())
        session_id = session_id or run_id

        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO pipeline_runs (
                    run_id, station, show_name, streamer, started_at, ended_at,
                    overall_status, current_step, session_id, recording_reference,
                    tracklist_status, castopod_episode_id, castopod_episode_url,
                    error_summary, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    run_id,
                    station,
                    show_name,
                    streamer,
                    "waiting",
                    "stream_start",
                    session_id,
                    recording_reference,
                    now,
                    now,
                ),
            )
            for sort_order, step_key in enumerate(PIPELINE_STEP_KEYS, start=1):
                connection.execute(
                    """
                    INSERT INTO pipeline_steps (
                        run_id, step_key, status, started_at, ended_at, duration_ms,
                        message, error_details, retry_count, sort_order, created_at, updated_at
                    )
                    VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL, 0, ?, ?, ?)
                    """,
                    (run_id, step_key, "pending", sort_order, now, now),
                )

        run = self.get_run(run_id)
        StructuredPipelineLogger(self.db_path).emit(
            run_id=run["run_id"],
            session_id=run["session_id"],
            step_key="stream_start",
            event_name="pipeline_run.created",
            status=run["overall_status"],
            message="Pipeline run created.",
            details={
                "station": run["station"],
                "show_name": run["show_name"],
                "streamer": run["streamer"],
            },
        )
        return run

    def get_run(self, run_id):
        self.initialize()
        with closing(self.connect()) as connection:
            run = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                return None

            steps = connection.execute(
                """
                SELECT * FROM pipeline_steps
                WHERE run_id = ?
                ORDER BY sort_order
                """,
                (run_id,),
            ).fetchall()

        return self.serialize_run(run, steps)

    def get_run_by_session_id(self, session_id):
        self.initialize()
        with closing(self.connect()) as connection:
            run = connection.execute(
                "SELECT run_id FROM pipeline_runs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self.get_run(run["run_id"]) if run else None

    def get_latest_run(self):
        self.initialize()
        with closing(self.connect()) as connection:
            run = connection.execute(
                """
                SELECT run_id FROM pipeline_runs
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self.get_run(run["run_id"]) if run else None

    def mark_stream_start(self, session_id=None, **run_fields):
        existing = self.get_run_by_session_id(session_id) if session_id else None
        run = existing or self.create_run(session_id=session_id, **run_fields)
        now = utc_now()

        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET started_at = COALESCE(started_at, ?),
                    overall_status = ?,
                    current_step = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (now, "in_progress", "stream_start", now, run["run_id"]),
            )

        return self.mark_step_success(
            run["run_id"],
            "stream_start",
            message="Stream started.",
            started_at=now,
            ended_at=now,
        )

    def mark_stream_end(self, run_id=None, session_id=None, message="Stream ended."):
        run = self._resolve_run(run_id=run_id, session_id=session_id)
        now = utc_now()

        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET ended_at = ?, current_step = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (now, "stream_end", now, run["run_id"]),
            )

        return self.mark_step_success(
            run["run_id"],
            "stream_end",
            message=message,
            started_at=now,
            ended_at=now,
        )

    def update_step_status(
        self,
        run_id,
        step_key,
        status,
        message=None,
        error_details=None,
        started_at=None,
        ended_at=None,
        retry_count=None,
    ):
        self._validate_step_key(step_key)
        self._validate_status(status)
        self.initialize()

        existing = self.get_run(run_id)
        if not existing:
            raise ValueError(f"Unknown pipeline run: {run_id}")

        now = utc_now()
        started_at = started_at or (now if status == "in_progress" else None)
        ended_at = ended_at or (now if status in TERMINAL_STATUSES else None)
        duration_ms = self._duration_ms(started_at, ended_at)
        from pipeline_logging import StructuredPipelineLogger, sanitize_log_value

        error_details = sanitize_log_value(error_details)
        error_summary = sanitize_log_value(message or error_details) if status == "failed" else None
        overall_status = "failed" if status == "failed" else existing["overall_status"]

        with closing(self.connect()) as connection:
            current = connection.execute(
                """
                SELECT retry_count FROM pipeline_steps
                WHERE run_id = ? AND step_key = ?
                """,
                (run_id, step_key),
            ).fetchone()
            if not current:
                raise ValueError(f"Unknown pipeline step: {step_key}")

            connection.execute(
                """
                UPDATE pipeline_steps
                SET status = ?,
                    started_at = COALESCE(?, started_at),
                    ended_at = COALESCE(?, ended_at),
                    duration_ms = COALESCE(?, duration_ms),
                    message = COALESCE(?, message),
                    error_details = COALESCE(?, error_details),
                    retry_count = ?,
                    updated_at = ?
                WHERE run_id = ? AND step_key = ?
                """,
                (
                    status,
                    started_at,
                    ended_at,
                    duration_ms,
                    message,
                    error_details,
                    current["retry_count"] if retry_count is None else retry_count,
                    now,
                    run_id,
                    step_key,
                ),
            )
            connection.execute(
                """
                UPDATE pipeline_runs
                SET overall_status = ?,
                    current_step = ?,
                    error_summary = COALESCE(?, error_summary),
                    updated_at = ?
                WHERE run_id = ?
                """,
                (overall_status, step_key, error_summary, now, run_id),
            )

        run = self.get_run(run_id)
        event_name_by_status = {
            "in_progress": f"{step_key}.started",
            "success": f"{step_key}.succeeded",
            "failed": f"{step_key}.failed",
            "skipped": f"{step_key}.skipped",
            "waiting": f"{step_key}.waiting",
            "pending": f"{step_key}.pending",
        }
        StructuredPipelineLogger(self.db_path).emit(
            run_id=run["run_id"],
            session_id=run["session_id"],
            step_key=step_key,
            event_name=event_name_by_status[status],
            status=status,
            message=message or error_summary or f"{step_key} is {status}.",
            details={"error_details": error_details} if error_details else {},
            level="ERROR" if status == "failed" else "INFO",
        )
        return run

    def mark_step_success(self, run_id, step_key, message=None, started_at=None, ended_at=None):
        return self.update_step_status(
            run_id,
            step_key,
            "success",
            message=message,
            started_at=started_at,
            ended_at=ended_at,
        )

    def mark_step_failed(self, run_id, step_key, message=None, error_details=None):
        return self.update_step_status(
            run_id,
            step_key,
            "failed",
            message=message,
            error_details=error_details,
        )

    def serialize_run(self, run, steps):
        return {
            "run_id": run["run_id"],
            "station": run["station"],
            "show_name": run["show_name"],
            "streamer": run["streamer"],
            "started_at": run["started_at"],
            "ended_at": run["ended_at"],
            "overall_status": run["overall_status"],
            "current_step": run["current_step"],
            "session_id": run["session_id"],
            "recording_reference": run["recording_reference"],
            "tracklist_status": run["tracklist_status"],
            "castopod_episode_id": run["castopod_episode_id"],
            "castopod_episode_url": run["castopod_episode_url"],
            "error_summary": run["error_summary"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "steps": [self.serialize_step(step) for step in steps],
        }

    def serialize_step(self, step):
        return {
            "step_key": step["step_key"],
            "status": step["status"],
            "started_at": step["started_at"],
            "ended_at": step["ended_at"],
            "duration_ms": step["duration_ms"],
            "message": step["message"],
            "error_details": step["error_details"],
            "retry_count": step["retry_count"],
        }

    def _resolve_run(self, run_id=None, session_id=None):
        if run_id:
            run = self.get_run(run_id)
        elif session_id:
            run = self.get_run_by_session_id(session_id)
        else:
            run = self.get_latest_run()

        if not run:
            raise ValueError("No pipeline run is available.")
        return run

    def _validate_step_key(self, step_key):
        if step_key not in PIPELINE_STEP_KEYS:
            raise ValueError(f"Unsupported pipeline step: {step_key}")

    def _validate_status(self, status):
        if status not in PIPELINE_STATUSES:
            raise ValueError(f"Unsupported pipeline status: {status}")

    def _duration_ms(self, started_at, ended_at):
        if not started_at or not ended_at:
            return None
        try:
            start = datetime.fromisoformat(started_at)
            end = datetime.fromisoformat(ended_at)
        except ValueError:
            return None
        return max(0, int((end - start).total_seconds() * 1000))


def get_pipeline_store():
    return PipelineStateStore()
