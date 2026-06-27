import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from pipeline_constants import PIPELINE_STATUSES, PIPELINE_STEP_KEYS

TERMINAL_STATUSES = {"success", "failed", "skipped"}
SYSTEM_RUN_ID = "pipeline-control"


def is_terminal_run_status(status):
    return status in TERMINAL_STATUSES


def can_cancel_run(run):
    return bool(run and not is_terminal_run_status(run.get("overall_status")))


def utc_now():
    return datetime.now(timezone.utc).isoformat()


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

    def get_recent_runs(self, limit=20):
        self.initialize()
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))

        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM pipeline_runs
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [self.get_run(row["run_id"]) for row in rows]

    def get_runs_by_step_status(self, step_key, status):
        self._validate_step_key(step_key)
        self._validate_status(status)
        self.initialize()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT pipeline_runs.run_id
                FROM pipeline_runs
                JOIN pipeline_steps
                  ON pipeline_steps.run_id = pipeline_runs.run_id
                WHERE pipeline_steps.step_key = ?
                  AND pipeline_steps.status = ?
                ORDER BY pipeline_runs.updated_at ASC
                """,
                (step_key, status),
            ).fetchall()
        return [self.get_run(row["run_id"]) for row in rows]

    def set_recording_reference(self, run_id, recording_reference):
        self.initialize()
        if not self.get_run(run_id):
            raise ValueError(f"Unknown pipeline run: {run_id}")
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET recording_reference = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (str(recording_reference), utc_now(), run_id),
            )
        return self.get_run(run_id)

    def find_active_run(self, station=None, streamer=None):
        self.initialize()
        clauses = ["ended_at IS NULL", "overall_status IN ('waiting', 'in_progress')"]
        params = []
        if station:
            clauses.append("station = ?")
            params.append(station)
        if streamer:
            clauses.append("streamer = ?")
            params.append(streamer)

        with closing(self.connect()) as connection:
            run = connection.execute(
                f"""
                SELECT run_id FROM pipeline_runs
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()

        return self.get_run(run["run_id"]) if run else None

    def cancel_current_run(self, message="Open run cancelled by operator."):
        run = self.find_active_run()
        if not run:
            from pipeline_logging import StructuredPipelineLogger

            StructuredPipelineLogger(self.db_path).emit(
                run_id=SYSTEM_RUN_ID,
                session_id=None,
                step_key="stream_end",
                event_name="run_cancelled",
                status="skipped",
                message="No open run to cancel.",
                details={"skip_reason": "No open run was found."},
            )
            return {"cancelled": False, "run": None}
        return self.cancel_run(run["run_id"], message=message)

    def cancel_run(self, run_id, message="Open run cancelled by operator."):
        from pipeline_logging import StructuredPipelineLogger

        run = self.get_run(run_id)
        logger = StructuredPipelineLogger(self.db_path)
        if not run:
            logger.emit(
                run_id=SYSTEM_RUN_ID,
                session_id=None,
                step_key="stream_end",
                event_name="run_cancelled",
                status="skipped",
                message="No run found to cancel.",
                details={"skip_reason": "No run was found.", "run_id": run_id},
            )
            return {"cancelled": False, "run": None}
        if not can_cancel_run(run):
            logger.emit(
                run_id=run["run_id"],
                session_id=run["session_id"],
                step_key=run.get("current_step") or "stream_end",
                event_name="run_cancelled",
                status="skipped",
                message="Run cancellation skipped because run is already terminal.",
                details={
                    "skip_reason": "Run is already terminal.",
                    "overall_status": run["overall_status"],
                },
            )
            return {"cancelled": False, "run": None}

        now = utc_now()
        current_step = run.get("current_step") or "stream_end"
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET ended_at = COALESCE(ended_at, ?),
                    overall_status = ?,
                    current_step = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (now, "skipped", current_step, now, run["run_id"]),
            )
            connection.execute(
                """
                UPDATE pipeline_steps
                SET status = ?,
                    ended_at = COALESCE(ended_at, ?),
                    message = COALESCE(message, ?),
                    updated_at = ?
                WHERE run_id = ?
                    AND status NOT IN ('success', 'failed', 'skipped')
                """,
                ("skipped", now, "Skipped after operator cancelled open run.", now, run["run_id"]),
            )

        cancelled = self.get_run(run["run_id"])
        logger.emit(
            run_id=cancelled["run_id"],
            session_id=cancelled["session_id"],
            step_key=current_step,
            event_name="run_cancelled",
            status="success",
            message=message,
            details={
                "station": cancelled["station"],
                "show_name": cancelled["show_name"],
                "streamer": cancelled["streamer"],
                "ended_at": cancelled["ended_at"],
            },
        )
        return {"cancelled": True, "run": cancelled}

    def mark_stream_start(self, session_id=None, started_at=None, **run_fields):
        existing = self.get_run_by_session_id(session_id) if session_id else None
        run = existing or self.create_run(session_id=session_id, **run_fields)
        now = utc_now()
        started_at = started_at or now

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
                (started_at, "in_progress", "stream_start", now, run["run_id"]),
            )

        return self.mark_step_success(
            run["run_id"],
            "stream_start",
            message="Stream started.",
            started_at=started_at,
            ended_at=started_at,
        )

    def mark_stream_end(self, run_id=None, session_id=None, ended_at=None, message="Stream ended."):
        run = self._resolve_run(run_id=run_id, session_id=session_id)
        now = utc_now()
        ended_at = ended_at or now

        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET ended_at = ?,
                    overall_status = CASE
                        WHEN overall_status = 'waiting' THEN 'in_progress'
                        ELSE overall_status
                    END,
                    current_step = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (ended_at, "stream_end", now, run["run_id"]),
            )

        return self.mark_step_success(
            run["run_id"],
            "stream_end",
            message=message,
            started_at=ended_at,
            ended_at=ended_at,
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

        event_details = sanitize_log_value(error_details)
        stored_error_details = self._serialize_error_details(event_details)
        error_summary = sanitize_log_value(message or stored_error_details) if status == "failed" else None
        if status == "failed":
            overall_status = "failed"
        else:
            overall_status = existing["overall_status"]
        tracklist_status = status if step_key == "acquire_tracklist" else existing["tracklist_status"]

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
                    stored_error_details,
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
                    tracklist_status = ?,
                    error_summary = COALESCE(?, error_summary),
                    updated_at = ?
                WHERE run_id = ?
                """,
                (overall_status, step_key, tracklist_status, error_summary, now, run_id),
            )

        run = self.get_run(run_id)
        event_name_by_status = {
            "in_progress": f"{step_key}.started",
            "success": f"{step_key}.succeeded",
            "failed": f"{step_key}.failed",
            "skipped": f"{step_key}.skipped",
            "waiting": f"{step_key}.waiting",
            "waiting_transcode": f"{step_key}.waiting_transcode",
            "pending": f"{step_key}.pending",
        }
        StructuredPipelineLogger(self.db_path).emit(
            run_id=run["run_id"],
            session_id=run["session_id"],
            step_key=step_key,
            event_name=event_name_by_status[status],
            status=status,
            message=message or error_summary or f"{step_key} is {status}.",
            details=self._event_details(event_details),
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

    def _serialize_error_details(self, error_details):
        if error_details in (None, "", {}, []):
            return None
        if isinstance(error_details, (dict, list)):
            return json.dumps(error_details, sort_keys=True)
        return str(error_details)

    def _event_details(self, error_details):
        if not error_details:
            return {}
        if isinstance(error_details, dict):
            return error_details
        return {"error_details": error_details}


def get_pipeline_store():
    return PipelineStateStore()
