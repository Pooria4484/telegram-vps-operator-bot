from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict

from app.models import Session

ANSI_OSC_RE = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
ANSI_CSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
ANSI_DCS_RE = re.compile(r"\x1B[P^_].*?\x1B\\", re.DOTALL)
ANSI_ESC_RE = re.compile(r"\x1B[@-_]")
CTRL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def sanitize_terminal_text(text: str) -> str:
    cleaned = ANSI_OSC_RE.sub("", text)
    cleaned = ANSI_DCS_RE.sub("", cleaned)
    cleaned = ANSI_CSI_RE.sub("", cleaned)
    cleaned = ANSI_ESC_RE.sub("", cleaned)
    cleaned = CTRL_RE.sub("", cleaned)
    return cleaned


@dataclass(slots=True)
class PendingUpload:
    telegram_user_id: int
    chat_id: int
    file_id: str
    file_name: str
    target_path: Path


class SessionManager:
    def __init__(self, default_workdir: Path) -> None:
        self.sessions_by_id: Dict[str, Session] = {}
        self.active_session_by_user: Dict[int, str] = {}
        self.default_workdir = default_workdir.resolve()
        self.current_workdir_by_user: Dict[int, Path] = {}
        self.pending_upload_by_user: Dict[int, PendingUpload] = {}
        self.stream_enabled_by_user: Dict[int, bool] = {}

    def create_session(self, telegram_user_id: int, chat_id: int, command: str) -> Session:
        if telegram_user_id in self.active_session_by_user:
            raise ValueError("user already has an active session")

        session_id = f"sess_{secrets.token_hex(4)}"
        session = Session(
            session_id=session_id,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            command=command,
        )
        self.sessions_by_id[session_id] = session
        self.active_session_by_user[telegram_user_id] = session_id
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
            self.active_session_by_user.pop(telegram_user_id, None)
            return None

        waiter = session.waiter_task
        if waiter is not None and waiter.done() and session.process is None:
            self.active_session_by_user.pop(telegram_user_id, None)
            return None

        process = session.process
        if process is not None and process.returncode is not None:
            if session.state == "running":
                session.state = "stopped"
                session.exit_code = process.returncode
                session.ended_at = session.ended_at or datetime.utcnow()
            session.process = None
            self.active_session_by_user.pop(telegram_user_id, None)
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

    def get_current_workdir(self, telegram_user_id: int) -> Path:
        return self.current_workdir_by_user.get(telegram_user_id, self.default_workdir)

    def set_current_workdir(self, telegram_user_id: int, workdir: Path) -> None:
        self.current_workdir_by_user[telegram_user_id] = workdir.resolve()

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
