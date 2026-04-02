from __future__ import annotations

import secrets
from typing import Dict

from app.models import Session


class SessionManager:
    def __init__(self) -> None:
        self.sessions_by_id: Dict[str, Session] = {}
        self.active_session_by_user: Dict[int, str] = {}

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
