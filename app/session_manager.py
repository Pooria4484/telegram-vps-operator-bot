from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

from app.models import (
    CodexRun,
    CodexRunStatus,
    CodexSession,
    CodexSessionMode,
    CodexSessionState,
    Session,
    SessionState,
)

ANSI_OSC_RE = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
ANSI_CSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
ANSI_DCS_RE = re.compile(r"\x1B[P^_].*?\x1B\\", re.DOTALL)
# 2-char escape sequences like ESC= / ESC> (keypad mode), ESC( / ESC) (charset),
# and similar terminal mode toggles that should never appear in user-visible output.
ANSI_ESC_2CHAR_RE = re.compile(r"\x1B[()<=>]")
ANSI_ESC_RE = re.compile(r"\x1B[@-_]")
CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
VALID_SESSION_STATES: set[str] = {"starting", "running", "finished", "failed", "stopped"}
VALID_CODEX_SESSION_STATES: set[str] = {"active", "closed"}
VALID_CODEX_SESSION_MODES: set[str] = {"continue", "new"}
VALID_CODEX_RUN_STATUSES: set[str] = {"queued", "running", "success", "partial", "failed", "cancelled"}

logger = logging.getLogger(__name__)


def sanitize_terminal_text(text: str) -> str:
    cleaned = ANSI_OSC_RE.sub("", text)
    cleaned = ANSI_DCS_RE.sub("", cleaned)
    cleaned = ANSI_CSI_RE.sub("", cleaned)
    cleaned = ANSI_ESC_2CHAR_RE.sub("", cleaned)
    cleaned = ANSI_ESC_RE.sub("", cleaned)
    cleaned = CTRL_RE.sub("", cleaned)
    return cleaned


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


@dataclass(slots=True)
class PendingUpload:
    request_id: str
    telegram_user_id: int
    chat_id: int
    file_id: str
    file_name: str
    target_path: Path


class SessionManager:
    def __init__(self, default_workdir: Path, db_path: Path, time_offset_minutes: int) -> None:
        self.sessions_by_id: Dict[str, Session] = {}
        self.active_session_by_user: Dict[int, str] = {}
        self.codex_sessions_by_id: Dict[str, CodexSession] = {}
        self.active_codex_session_by_chat: Dict[int, str] = {}
        self.codex_runs_by_id: Dict[str, CodexRun] = {}
        self.default_workdir = default_workdir.resolve()
        self.current_workdir_by_user: Dict[int, Path] = {}
        self.pending_upload_by_user: Dict[int, PendingUpload] = {}
        self.stream_enabled_by_user: Dict[int, bool] = {}
        self.time_offset = timedelta(minutes=time_offset_minutes)

        self.db_path = db_path.expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row

        self._init_db()
        self._load_state()

    def now(self) -> datetime:
        return datetime.utcnow() + self.time_offset

    def _now_iso(self) -> str:
        return self.now().isoformat()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_workdirs (
                    telegram_user_id INTEGER PRIMARY KEY,
                    workdir TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_snapshots (
                    session_id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    is_attached INTEGER NOT NULL DEFAULT 0,
                    detached_at TEXT,
                    state TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    exit_code INTEGER,
                    tail_lines_json TEXT NOT NULL,
                    tail_partial TEXT NOT NULL,
                    stream_pending_text TEXT NOT NULL DEFAULT '',
                    stream_live_message_id INTEGER,
                    stream_frame_index INTEGER NOT NULL DEFAULT 0,
                    stream_frame_body TEXT NOT NULL DEFAULT '',
                    stream_current_line_start INTEGER NOT NULL DEFAULT 0,
                    stream_last_sent_text TEXT NOT NULL DEFAULT '',
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session_user_started
                ON session_snapshots (telegram_user_id, started_at DESC)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    telegram_user_id INTEGER PRIMARY KEY,
                    stream_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_sessions (
                    codex_session_id TEXT PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    workspace_path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    default_mode TEXT NOT NULL,
                    default_model TEXT NOT NULL DEFAULT '',
                    default_reasoning_effort TEXT NOT NULL DEFAULT '',
                    thread_ref TEXT,
                    last_run_id TEXT,
                    last_prompt TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_codex_sessions_chat_updated
                ON codex_sessions (telegram_chat_id, updated_at DESC)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_runs (
                    codex_run_id TEXT PRIMARY KEY,
                    codex_session_id TEXT NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    workspace_path TEXT NOT NULL,
                    prompt_text TEXT NOT NULL,
                    run_mode TEXT NOT NULL,
                    model_name TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    summary_short TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    exit_code INTEGER,
                    stdout_artifact_path TEXT,
                    stderr_artifact_path TEXT,
                    structured_result_json TEXT NOT NULL DEFAULT '',
                    failure_summary TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_codex_runs_session_started
                ON codex_runs (codex_session_id, started_at DESC)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_codex_runs_workspace_status
                ON codex_runs (workspace_path, status, started_at DESC)
                """
            )

        # Lightweight migrations for existing DBs.
        self._ensure_column(
            table="codex_sessions",
            column="default_model",
            sql="ALTER TABLE codex_sessions ADD COLUMN default_model TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            table="codex_sessions",
            column="default_reasoning_effort",
            sql="ALTER TABLE codex_sessions ADD COLUMN default_reasoning_effort TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            table="session_snapshots",
            column="is_attached",
            sql="ALTER TABLE session_snapshots ADD COLUMN is_attached INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column(
            table="session_snapshots",
            column="detached_at",
            sql="ALTER TABLE session_snapshots ADD COLUMN detached_at TEXT",
        )
        self._ensure_column(
            table="session_snapshots",
            column="stream_pending_text",
            sql="ALTER TABLE session_snapshots ADD COLUMN stream_pending_text TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            table="session_snapshots",
            column="stream_live_message_id",
            sql="ALTER TABLE session_snapshots ADD COLUMN stream_live_message_id INTEGER",
        )
        self._ensure_column(
            table="session_snapshots",
            column="stream_frame_index",
            sql="ALTER TABLE session_snapshots ADD COLUMN stream_frame_index INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column(
            table="session_snapshots",
            column="stream_frame_body",
            sql="ALTER TABLE session_snapshots ADD COLUMN stream_frame_body TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            table="session_snapshots",
            column="stream_current_line_start",
            sql="ALTER TABLE session_snapshots ADD COLUMN stream_current_line_start INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column(
            table="session_snapshots",
            column="stream_last_sent_text",
            sql="ALTER TABLE session_snapshots ADD COLUMN stream_last_sent_text TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            table="codex_runs",
            column="model_name",
            sql="ALTER TABLE codex_runs ADD COLUMN model_name TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            table="codex_runs",
            column="reasoning_effort",
            sql="ALTER TABLE codex_runs ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT ''",
        )

    def _ensure_column(self, table: str, column: str, sql: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {str(row["name"]) for row in rows}
        if column in columns:
            return
        with self._conn:
            self._conn.execute(sql)

    def _persist_workdir(self, telegram_user_id: int, workdir: Path) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO user_workdirs (telegram_user_id, workdir, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    workdir = excluded.workdir,
                    updated_at = excluded.updated_at
                """,
                (telegram_user_id, str(workdir), self._now_iso()),
            )

    def _persist_stream_preference(self, telegram_user_id: int, enabled: bool) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO user_preferences (telegram_user_id, stream_enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    stream_enabled = excluded.stream_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    telegram_user_id,
                    1 if enabled else 0,
                    self._now_iso(),
                ),
            )

    def _persist_session(self, session: Session) -> None:
        started_at = session.started_at.isoformat()
        ended_at = session.ended_at.isoformat() if session.ended_at else None
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO session_snapshots (
                    session_id,
                    telegram_user_id,
                    chat_id,
                    command,
                    is_attached,
                    detached_at,
                    state,
                    started_at,
                    ended_at,
                    exit_code,
                    tail_lines_json,
                    tail_partial,
                    stream_pending_text,
                    stream_live_message_id,
                    stream_frame_index,
                    stream_frame_body,
                    stream_current_line_start,
                    stream_last_sent_text,
                    stop_requested,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    is_attached = excluded.is_attached,
                    detached_at = excluded.detached_at,
                    state = excluded.state,
                    ended_at = excluded.ended_at,
                    exit_code = excluded.exit_code,
                    tail_lines_json = excluded.tail_lines_json,
                    tail_partial = excluded.tail_partial,
                    stream_pending_text = excluded.stream_pending_text,
                    stream_live_message_id = excluded.stream_live_message_id,
                    stream_frame_index = excluded.stream_frame_index,
                    stream_frame_body = excluded.stream_frame_body,
                    stream_current_line_start = excluded.stream_current_line_start,
                    stream_last_sent_text = excluded.stream_last_sent_text,
                    stop_requested = excluded.stop_requested,
                    updated_at = excluded.updated_at
                """,
                (
                    session.session_id,
                    session.telegram_user_id,
                    session.chat_id,
                    session.command,
                    1 if session.is_attached else 0,
                    session.detached_at.isoformat() if session.detached_at else None,
                    session.state,
                    started_at,
                    ended_at,
                    session.exit_code,
                    json.dumps(session.tail_lines, ensure_ascii=True),
                    session.tail_partial,
                    session.stream_pending_text,
                    session.stream_live_message_id,
                    session.stream_frame_index,
                    session.stream_frame_body,
                    session.stream_current_line_start,
                    session.stream_last_sent_text,
                    1 if session.stop_requested else 0,
                    self._now_iso(),
                ),
            )

    def _load_state(self) -> None:
        # Load persisted workdirs.
        workdir_rows = self._conn.execute(
            "SELECT telegram_user_id, workdir FROM user_workdirs"
        ).fetchall()
        for row in workdir_rows:
            try:
                self.current_workdir_by_user[int(row["telegram_user_id"])] = Path(
                    row["workdir"]
                ).resolve()
            except Exception:
                logger.exception(
                    "Failed to restore workdir row: user_id=%s raw_path=%r",
                    row["telegram_user_id"],
                    row["workdir"],
                )

        pref_rows = self._conn.execute(
            "SELECT telegram_user_id, stream_enabled FROM user_preferences"
        ).fetchall()
        for row in pref_rows:
            user_id = int(row["telegram_user_id"])
            self.stream_enabled_by_user[user_id] = bool(row["stream_enabled"])

        codex_session_rows = self._conn.execute(
            """
            SELECT
                codex_session_id,
                telegram_chat_id,
                telegram_user_id,
                workspace_path,
                state,
                default_mode,
                default_model,
                default_reasoning_effort,
                thread_ref,
                last_run_id,
                last_prompt,
                created_at,
                updated_at,
                ended_at
            FROM codex_sessions
            ORDER BY updated_at ASC
            LIMIT 2000
            """
        ).fetchall()
        for row in codex_session_rows:
            state_raw = str(row["state"])
            state: CodexSessionState = "closed"
            if state_raw in VALID_CODEX_SESSION_STATES:
                state = state_raw  # type: ignore[assignment]

            default_mode_raw = str(row["default_mode"])
            default_mode: CodexSessionMode = "continue"
            if default_mode_raw in VALID_CODEX_SESSION_MODES:
                default_mode = default_mode_raw  # type: ignore[assignment]

            session = CodexSession(
                codex_session_id=str(row["codex_session_id"]),
                telegram_chat_id=int(row["telegram_chat_id"]),
                telegram_user_id=int(row["telegram_user_id"]),
                workspace_path=Path(str(row["workspace_path"])).resolve(),
                state=state,
                default_mode=default_mode,
                default_model=str(row["default_model"] or ""),
                default_reasoning_effort=str(row["default_reasoning_effort"] or ""),
                thread_ref=str(row["thread_ref"]) if row["thread_ref"] is not None else None,
                last_run_id=str(row["last_run_id"]) if row["last_run_id"] is not None else None,
                last_prompt=str(row["last_prompt"] or ""),
                created_at=_parse_iso(row["created_at"]) or self.now(),
                updated_at=_parse_iso(row["updated_at"]) or self.now(),
                ended_at=_parse_iso(row["ended_at"]),
            )
            self.codex_sessions_by_id[session.codex_session_id] = session
            if session.state == "active":
                self.active_codex_session_by_chat[session.telegram_chat_id] = session.codex_session_id

        codex_run_rows = self._conn.execute(
            """
            SELECT
                codex_run_id,
                codex_session_id,
                telegram_chat_id,
                telegram_user_id,
                workspace_path,
                prompt_text,
                run_mode,
                model_name,
                reasoning_effort,
                status,
                summary_short,
                started_at,
                ended_at,
                exit_code,
                stdout_artifact_path,
                stderr_artifact_path,
                structured_result_json,
                failure_summary
            FROM codex_runs
            ORDER BY started_at ASC
            LIMIT 5000
            """
        ).fetchall()
        for row in codex_run_rows:
            run_mode_raw = str(row["run_mode"])
            run_mode: CodexSessionMode = "continue"
            if run_mode_raw in VALID_CODEX_SESSION_MODES:
                run_mode = run_mode_raw  # type: ignore[assignment]

            status_raw = str(row["status"])
            status: CodexRunStatus = "failed"
            if status_raw in VALID_CODEX_RUN_STATUSES:
                status = status_raw  # type: ignore[assignment]
            if status in {"queued", "running"}:
                status = "failed"

            run = CodexRun(
                codex_run_id=str(row["codex_run_id"]),
                codex_session_id=str(row["codex_session_id"]),
                telegram_chat_id=int(row["telegram_chat_id"]),
                telegram_user_id=int(row["telegram_user_id"]),
                workspace_path=Path(str(row["workspace_path"])).resolve(),
                prompt_text=str(row["prompt_text"]),
                run_mode=run_mode,
                model_name=str(row["model_name"] or ""),
                reasoning_effort=str(row["reasoning_effort"] or ""),
                status=status,
                summary_short=str(row["summary_short"] or ""),
                started_at=_parse_iso(row["started_at"]) or self.now(),
                ended_at=_parse_iso(row["ended_at"]),
                exit_code=row["exit_code"],
                stdout_artifact_path=Path(str(row["stdout_artifact_path"])).resolve()
                if row["stdout_artifact_path"]
                else None,
                stderr_artifact_path=Path(str(row["stderr_artifact_path"])).resolve()
                if row["stderr_artifact_path"]
                else None,
                structured_result_json=str(row["structured_result_json"] or ""),
                failure_summary=(
                    str(row["failure_summary"] or "")
                    if status_raw not in {"queued", "running"}
                    else "Bot restarted while codex run was active."
                ),
            )
            if status_raw in {"queued", "running"} and run.ended_at is None:
                run.ended_at = self.now()
            self.codex_runs_by_id[run.codex_run_id] = run
            if status_raw in {"queued", "running"}:
                self._persist_codex_run(run)

        # Load session history for continuity after restart.
        session_rows = self._conn.execute(
            """
            SELECT
                session_id,
                telegram_user_id,
                chat_id,
                command,
                is_attached,
                detached_at,
                state,
                started_at,
                ended_at,
                exit_code,
                tail_lines_json,
                tail_partial,
                stream_pending_text,
                stream_live_message_id,
                stream_frame_index,
                stream_frame_body,
                stream_current_line_start,
                stream_last_sent_text,
                stop_requested
            FROM session_snapshots
            ORDER BY started_at ASC
            LIMIT 2000
            """
        ).fetchall()

        recovered_running = 0
        for row in session_rows:
            started_at = _parse_iso(row["started_at"]) or self.now()
            ended_at = _parse_iso(row["ended_at"])

            state_raw = str(row["state"])
            state: SessionState = "failed"
            if state_raw in VALID_SESSION_STATES:
                state = state_raw  # type: ignore[assignment]

            was_running = state in {"starting", "running"}
            if was_running:
                # Process references cannot survive restart.
                state = "stopped"
                ended_at = self.now()
                recovered_running += 1

            try:
                tail_lines_raw = json.loads(row["tail_lines_json"] or "[]")
                tail_lines = [str(item) for item in tail_lines_raw if isinstance(item, str)]
            except Exception:
                tail_lines = []

            session = Session(
                session_id=str(row["session_id"]),
                telegram_user_id=int(row["telegram_user_id"]),
                chat_id=int(row["chat_id"]),
                command=str(row["command"]),
                is_attached=bool(row["is_attached"]) and not was_running,
                detached_at=_parse_iso(row["detached_at"]),
                state=state,
                started_at=started_at,
                ended_at=ended_at,
                exit_code=row["exit_code"],
                tail_lines=tail_lines,
                tail_partial=str(row["tail_partial"] or ""),
                stream_pending_text=str(row["stream_pending_text"] or ""),
                stream_live_message_id=row["stream_live_message_id"],
                stream_frame_index=int(row["stream_frame_index"] or 0),
                stream_frame_body=str(row["stream_frame_body"] or ""),
                stream_current_line_start=int(row["stream_current_line_start"] or 0),
                stream_last_sent_text=str(row["stream_last_sent_text"] or ""),
                stop_requested=bool(row["stop_requested"]),
            )

            if was_running:
                # Live frame messages from previous process lifetime are stale after restart.
                session.stream_pending_text = ""
                session.stream_live_message_id = None
                session.stream_frame_body = ""
                session.stream_current_line_start = 0
                session.stream_last_sent_text = ""

            self.sessions_by_id[session.session_id] = session

            if session.state == "stopped" and session.tail_partial:
                session.tail_lines.append(session.tail_partial)
                session.tail_partial = ""

            if was_running:
                self._persist_session(session)

        if recovered_running:
            logger.info(
                "Recovered %s running sessions as stopped after restart.",
                recovered_running,
            )

    def save_session(self, session: Session) -> None:
        self._persist_session(session)

    def _persist_codex_session(self, session: CodexSession) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO codex_sessions (
                    codex_session_id,
                    telegram_chat_id,
                    telegram_user_id,
                    workspace_path,
                    state,
                    default_mode,
                    default_model,
                    default_reasoning_effort,
                    thread_ref,
                    last_run_id,
                    last_prompt,
                    created_at,
                    updated_at,
                    ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(codex_session_id) DO UPDATE SET
                    workspace_path = excluded.workspace_path,
                    state = excluded.state,
                    default_mode = excluded.default_mode,
                    default_model = excluded.default_model,
                    default_reasoning_effort = excluded.default_reasoning_effort,
                    thread_ref = excluded.thread_ref,
                    last_run_id = excluded.last_run_id,
                    last_prompt = excluded.last_prompt,
                    updated_at = excluded.updated_at,
                    ended_at = excluded.ended_at
                """,
                (
                    session.codex_session_id,
                    session.telegram_chat_id,
                    session.telegram_user_id,
                    str(session.workspace_path),
                    session.state,
                    session.default_mode,
                    session.default_model,
                    session.default_reasoning_effort,
                    session.thread_ref,
                    session.last_run_id,
                    session.last_prompt,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.ended_at.isoformat() if session.ended_at else None,
                ),
            )

    def _persist_codex_run(self, run: CodexRun) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO codex_runs (
                    codex_run_id,
                    codex_session_id,
                    telegram_chat_id,
                    telegram_user_id,
                    workspace_path,
                    prompt_text,
                    run_mode,
                    model_name,
                    reasoning_effort,
                    status,
                    summary_short,
                    started_at,
                    ended_at,
                    exit_code,
                    stdout_artifact_path,
                    stderr_artifact_path,
                    structured_result_json,
                    failure_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(codex_run_id) DO UPDATE SET
                    run_mode = excluded.run_mode,
                    model_name = excluded.model_name,
                    reasoning_effort = excluded.reasoning_effort,
                    status = excluded.status,
                    summary_short = excluded.summary_short,
                    ended_at = excluded.ended_at,
                    exit_code = excluded.exit_code,
                    stdout_artifact_path = excluded.stdout_artifact_path,
                    stderr_artifact_path = excluded.stderr_artifact_path,
                    structured_result_json = excluded.structured_result_json,
                    failure_summary = excluded.failure_summary
                """,
                (
                    run.codex_run_id,
                    run.codex_session_id,
                    run.telegram_chat_id,
                    run.telegram_user_id,
                    str(run.workspace_path),
                    run.prompt_text,
                    run.run_mode,
                    run.model_name,
                    run.reasoning_effort,
                    run.status,
                    run.summary_short,
                    run.started_at.isoformat(),
                    run.ended_at.isoformat() if run.ended_at else None,
                    run.exit_code,
                    str(run.stdout_artifact_path) if run.stdout_artifact_path else None,
                    str(run.stderr_artifact_path) if run.stderr_artifact_path else None,
                    run.structured_result_json,
                    run.failure_summary,
                ),
            )

    def _is_running(self, session: Session) -> bool:
        return session.state in {"starting", "running"} and session.process is not None

    def save_codex_session(self, session: CodexSession) -> None:
        session.updated_at = self.now()
        self.codex_sessions_by_id[session.codex_session_id] = session
        if session.state == "active":
            self.active_codex_session_by_chat[session.telegram_chat_id] = session.codex_session_id
        else:
            self.active_codex_session_by_chat.pop(session.telegram_chat_id, None)
        self._persist_codex_session(session)

    def save_codex_run(self, run: CodexRun) -> None:
        self.codex_runs_by_id[run.codex_run_id] = run
        self._persist_codex_run(run)

    def get_active_codex_session_for_chat(self, telegram_chat_id: int) -> CodexSession | None:
        session_id = self.active_codex_session_by_chat.get(telegram_chat_id)
        if not session_id:
            return None
        session = self.codex_sessions_by_id.get(session_id)
        if session is None or session.state != "active":
            self.active_codex_session_by_chat.pop(telegram_chat_id, None)
            return None
        return session

    def get_codex_session(self, codex_session_id: str) -> CodexSession | None:
        return self.codex_sessions_by_id.get(codex_session_id)

    def list_codex_sessions_for_chat(self, telegram_chat_id: int, limit: int | None = 20) -> list[CodexSession]:
        sessions = [
            session
            for session in self.codex_sessions_by_id.values()
            if session.telegram_chat_id == telegram_chat_id
        ]
        sessions.sort(key=lambda session: (session.updated_at, session.codex_session_id), reverse=True)
        if limit is None:
            return sessions
        return sessions[:limit]

    def create_codex_session(
        self,
        telegram_chat_id: int,
        telegram_user_id: int,
        workspace_path: Path,
        default_mode: CodexSessionMode = "continue",
        default_model: str = "",
        default_reasoning_effort: str = "",
    ) -> CodexSession:
        active = self.get_active_codex_session_for_chat(telegram_chat_id)
        if active is not None:
            active.state = "closed"
            active.ended_at = self.now()
            self.save_codex_session(active)

        session = CodexSession(
            codex_session_id=f"cxs_{secrets.token_hex(4)}",
            telegram_chat_id=telegram_chat_id,
            telegram_user_id=telegram_user_id,
            workspace_path=workspace_path.resolve(),
            default_mode=default_mode,
            default_model=default_model,
            default_reasoning_effort=default_reasoning_effort,
            created_at=self.now(),
            updated_at=self.now(),
        )
        self.save_codex_session(session)
        return session

    def close_codex_session(self, codex_session_id: str) -> CodexSession | None:
        session = self.codex_sessions_by_id.get(codex_session_id)
        if session is None:
            return None
        session.state = "closed"
        session.ended_at = self.now()
        self.save_codex_session(session)
        return session

    def create_codex_run(
        self,
        codex_session_id: str,
        telegram_chat_id: int,
        telegram_user_id: int,
        workspace_path: Path,
        prompt_text: str,
        run_mode: CodexSessionMode = "continue",
        model_name: str = "",
        reasoning_effort: str = "",
    ) -> CodexRun:
        run = CodexRun(
            codex_run_id=f"cxr_{secrets.token_hex(5)}",
            codex_session_id=codex_session_id,
            telegram_chat_id=telegram_chat_id,
            telegram_user_id=telegram_user_id,
            workspace_path=workspace_path.resolve(),
            prompt_text=prompt_text,
            run_mode=run_mode,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            started_at=self.now(),
        )
        session = self.codex_sessions_by_id.get(codex_session_id)
        if session is not None:
            session.last_run_id = run.codex_run_id
            session.last_prompt = prompt_text
            session.default_mode = run_mode
            self.save_codex_session(session)
        self.save_codex_run(run)
        return run

    def get_codex_run(self, codex_run_id: str) -> CodexRun | None:
        return self.codex_runs_by_id.get(codex_run_id)

    def get_last_codex_run_for_session(self, codex_session_id: str) -> CodexRun | None:
        runs = [
            run for run in self.codex_runs_by_id.values() if run.codex_session_id == codex_session_id
        ]
        if not runs:
            return None
        runs.sort(key=lambda run: (run.started_at, run.codex_run_id), reverse=True)
        return runs[0]

    def get_active_codex_run_for_chat(self, telegram_chat_id: int) -> CodexRun | None:
        runs = [
            run
            for run in self.codex_runs_by_id.values()
            if run.telegram_chat_id == telegram_chat_id and run.status in {"queued", "running"}
        ]
        if not runs:
            return None
        runs.sort(key=lambda run: (run.started_at, run.codex_run_id), reverse=True)
        return runs[0]

    def get_active_codex_run_for_workspace(self, workspace_path: Path) -> CodexRun | None:
        resolved = workspace_path.resolve()
        for run in sorted(
            self.codex_runs_by_id.values(),
            key=lambda item: (item.started_at, item.codex_run_id),
            reverse=True,
        ):
            if run.workspace_path != resolved:
                continue
            if run.status in {"queued", "running"}:
                return run
        return None

    def count_running_sessions_for_user(self, telegram_user_id: int) -> int:
        return sum(
            1
            for session in self.sessions_by_id.values()
            if session.telegram_user_id == telegram_user_id and self._is_running(session)
        )

    def list_sessions_for_user(self, telegram_user_id: int, limit: int | None = 20) -> list[Session]:
        sessions = [
            s for s in self.sessions_by_id.values() if s.telegram_user_id == telegram_user_id
        ]
        sessions.sort(key=lambda s: (s.started_at, s.session_id), reverse=True)
        if limit is None:
            return sessions
        return sessions[:limit]

    def get_session_for_user(self, telegram_user_id: int, session_id: str) -> Session | None:
        session = self.sessions_by_id.get(session_id)
        if not session:
            return None
        if session.telegram_user_id != telegram_user_id:
            return None
        return session

    def resolve_session_token_for_user(self, telegram_user_id: int, token: str) -> Session | None:
        token = token.strip()
        if not token:
            return None
        exact = self.get_session_for_user(telegram_user_id, token)
        if exact:
            return exact
        matches = [
            s
            for s in self.sessions_by_id.values()
            if s.telegram_user_id == telegram_user_id and s.session_id.endswith(token)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _prune_old_stopped_sessions_for_user(self, telegram_user_id: int, max_keep: int) -> None:
        if max_keep < 1:
            return
        sessions = [
            s
            for s in self.sessions_by_id.values()
            if s.telegram_user_id == telegram_user_id and s.state in {"finished", "failed", "stopped"}
        ]
        sessions.sort(key=lambda s: (s.started_at, s.session_id), reverse=True)
        to_remove = sessions[max_keep:]
        if not to_remove:
            return
        with self._conn:
            for session in to_remove:
                self.sessions_by_id.pop(session.session_id, None)
                self._conn.execute(
                    "DELETE FROM session_snapshots WHERE session_id = ?",
                    (session.session_id,),
                )

    def create_session(
        self,
        telegram_user_id: int,
        chat_id: int,
        command: str,
        max_session_history_per_user: int = 20,
    ) -> Session:
        if telegram_user_id in self.active_session_by_user:
            raise ValueError("user already has an attached session")

        self._prune_old_stopped_sessions_for_user(
            telegram_user_id=telegram_user_id,
            max_keep=max(1, max_session_history_per_user - 1),
        )

        session_id = f"sess_{secrets.token_hex(4)}"
        session = Session(
            session_id=session_id,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            command=command,
            is_attached=True,
            started_at=self.now(),
        )
        self.sessions_by_id[session_id] = session
        self.active_session_by_user[telegram_user_id] = session_id
        self._persist_session(session)
        return session

    def detach_active_session_for_user(self, telegram_user_id: int) -> Session | None:
        session = self.get_active_session_for_user(telegram_user_id)
        if not session:
            return None
        session.is_attached = False
        # Detached TTL starts only after the session becomes idle.
        session.detached_at = None
        self.active_session_by_user.pop(telegram_user_id, None)
        self._persist_session(session)
        return session

    def attach_session_for_user(self, telegram_user_id: int, session_id: str) -> Session:
        if telegram_user_id in self.active_session_by_user:
            raise ValueError("user already has an attached session")
        session = self.get_session_for_user(telegram_user_id, session_id)
        if not session:
            raise ValueError("session not found")
        if not self._is_running(session):
            raise ValueError("session is not running")
        session.is_attached = True
        session.detached_at = None
        self.active_session_by_user[telegram_user_id] = session.session_id
        self._persist_session(session)
        return session

    def get_active_session_for_user(self, telegram_user_id: int) -> Session | None:
        session_id = self.active_session_by_user.get(telegram_user_id)
        if not session_id:
            return None
        session = self.sessions_by_id.get(session_id)
        if session is None:
            self.active_session_by_user.pop(telegram_user_id, None)
            return None

        if session.state in {"finished", "failed", "stopped"}:
            session.is_attached = False
            self.active_session_by_user.pop(telegram_user_id, None)
            self._persist_session(session)
            return None

        waiter = session.waiter_task
        if waiter is not None and waiter.done() and session.process is None:
            session.is_attached = False
            self.active_session_by_user.pop(telegram_user_id, None)
            self._persist_session(session)
            return None

        process = session.process
        if process is not None and process.returncode is not None:
            if session.state == "running":
                session.state = "stopped"
                session.exit_code = process.returncode
                session.ended_at = session.ended_at or self.now()
            session.process = None
            session.is_attached = False
            session.detached_at = None
            self.active_session_by_user.pop(telegram_user_id, None)
            self._persist_session(session)
            return None

        return session

    def get_latest_session_for_user(self, telegram_user_id: int) -> Session | None:
        latest: Session | None = None
        for session in self.sessions_by_id.values():
            if session.telegram_user_id != telegram_user_id:
                continue
            if latest is None or session.started_at > latest.started_at:
                latest = session
        return latest

    def finish_session(self, session: Session) -> None:
        self.active_session_by_user.pop(session.telegram_user_id, None)
        session.is_attached = False
        session.detached_at = None
        self._persist_session(session)

    def list_detached_running_sessions(self) -> list[Session]:
        sessions: list[Session] = []
        for session in self.sessions_by_id.values():
            if session.is_attached:
                continue
            if session.state not in {"starting", "running"}:
                continue
            if session.process is None:
                continue
            sessions.append(session)
        return sessions

    def append_tail(self, session: Session, lines: list[str], max_tail_lines: int) -> None:
        session.tail_lines.extend(lines)
        if len(session.tail_lines) > max_tail_lines:
            session.tail_lines[:] = session.tail_lines[-max_tail_lines:]

    def append_output_text(self, session: Session, text: str, max_tail_lines: int) -> None:
        normalized = sanitize_terminal_text(text).replace("\r\n", "\n")
        data = session.tail_partial + normalized
        complete_lines: list[str] = []
        current_line = ""
        for char in data:
            if char == "\r":
                current_line = ""
                continue
            if char == "\n":
                complete_lines.append(current_line)
                current_line = ""
                continue
            current_line += char
        session.tail_partial = current_line

        if complete_lines and session.pending_echo_inputs:
            filtered_lines: list[str] = []
            for line in complete_lines:
                if session.pending_echo_inputs and line.strip() == session.pending_echo_inputs[0]:
                    session.pending_echo_inputs.pop(0)
                    continue
                filtered_lines.append(line)
            complete_lines = filtered_lines

        if complete_lines:
            self.append_tail(session, complete_lines, max_tail_lines)

        self._persist_session(session)

    def get_tail_snapshot(self, session: Session, max_tail_lines: int) -> list[str]:
        lines = list(session.tail_lines[-max_tail_lines:])
        if session.tail_partial:
            lines.append(session.tail_partial)
        if len(lines) > max_tail_lines:
            return lines[-max_tail_lines:]
        return lines

    def clear_output_buffer(self, session: Session) -> None:
        session.tail_lines.clear()
        session.tail_partial = ""
        session.stream_last_sent_text = ""
        self._persist_session(session)

    def get_current_workdir(self, telegram_user_id: int) -> Path:
        return self.current_workdir_by_user.get(telegram_user_id, self.default_workdir)

    def set_current_workdir(self, telegram_user_id: int, workdir: Path) -> None:
        resolved = workdir.resolve()
        self.current_workdir_by_user[telegram_user_id] = resolved
        self._persist_workdir(telegram_user_id, resolved)

    def set_pending_upload(self, pending: PendingUpload) -> None:
        self.pending_upload_by_user[pending.telegram_user_id] = pending

    def get_pending_upload(self, telegram_user_id: int) -> PendingUpload | None:
        return self.pending_upload_by_user.get(telegram_user_id)

    def clear_pending_upload(self, telegram_user_id: int) -> None:
        self.pending_upload_by_user.pop(telegram_user_id, None)

    def is_stream_enabled(self, telegram_user_id: int) -> bool:
        return self.stream_enabled_by_user.get(telegram_user_id, False)

    def set_stream_enabled(self, telegram_user_id: int, enabled: bool) -> None:
        self.stream_enabled_by_user[telegram_user_id] = enabled
        self._persist_stream_preference(telegram_user_id, enabled)
