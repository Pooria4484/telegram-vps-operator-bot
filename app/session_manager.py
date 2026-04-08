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

from app.models import Session, SessionState

ANSI_OSC_RE = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
ANSI_CSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
ANSI_DCS_RE = re.compile(r"\x1B[P^_].*?\x1B\\", re.DOTALL)
ANSI_ESC_RE = re.compile(r"\x1B[@-_]")
CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
VALID_SESSION_STATES: set[str] = {"starting", "running", "finished", "failed", "stopped"}

logger = logging.getLogger(__name__)


def sanitize_terminal_text(text: str) -> str:
    cleaned = ANSI_OSC_RE.sub("", text)
    cleaned = ANSI_DCS_RE.sub("", cleaned)
    cleaned = ANSI_CSI_RE.sub("", cleaned)
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

        # Lightweight migrations for existing DBs.
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

    def _is_running(self, session: Session) -> bool:
        return session.state in {"starting", "running"} and session.process is not None

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
