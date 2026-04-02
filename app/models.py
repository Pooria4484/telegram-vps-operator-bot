from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


SessionState = Literal["starting", "running", "finished", "failed"]
SessionMode = Literal["exec"]


@dataclass(slots=True)
class Session:
    session_id: str
    telegram_user_id: int
    chat_id: int
    command: str
    mode: SessionMode = "exec"
    state: SessionState = "starting"
    started_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    exit_code: int | None = None
    tail_lines: list[str] = field(default_factory=list)
