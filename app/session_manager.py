from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from app.models import Session


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
        return self.sessions_by_id.get(session_id)

    def finish_session(self, session: Session) -> None:
        self.active_session_by_user.pop(session.telegram_user_id, None)

    def append_tail(self, session: Session, lines: list[str], max_tail_lines: int) -> None:
        session.tail_lines.extend(lines)
        if len(session.tail_lines) > max_tail_lines:
            session.tail_lines[:] = session.tail_lines[-max_tail_lines:]

    def append_output_text(self, session: Session, text: str, max_tail_lines: int) -> None:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        data = session.tail_partial + normalized
        parts = data.split("\n")

        if data.endswith("\n"):
            complete_lines = parts[:-1]
            session.tail_partial = ""
        else:
            complete_lines = parts[:-1]
            session.tail_partial = parts[-1]

        if complete_lines:
            self.append_tail(session, complete_lines, max_tail_lines)

    def get_tail_snapshot(self, session: Session, max_tail_lines: int) -> list[str]:
        lines = list(session.tail_lines[-max_tail_lines:])
        if session.tail_partial:
            lines.append(session.tail_partial)
        if len(lines) > max_tail_lines:
            return lines[-max_tail_lines:]
        return lines

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
