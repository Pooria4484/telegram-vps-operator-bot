from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


SessionState = Literal["starting", "running", "finished", "failed", "stopped"]
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
    tail_partial: str = ""
    stop_requested: bool = False
    pty_master_fd: int | None = None
    process: asyncio.subprocess.Process | None = None
    reader_task: asyncio.Task[None] | None = None
    waiter_task: asyncio.Task[None] | None = None
    streamer_task: asyncio.Task[None] | None = None
    stream_last_sent_text: str = ""
