from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


SessionState = Literal["starting", "running", "finished", "failed", "stopped"]
SessionMode = Literal["exec"]
CodexSessionState = Literal["active", "closed"]
CodexSessionMode = Literal["continue", "new"]
CodexRunStatus = Literal["queued", "running", "success", "partial", "failed", "cancelled"]


@dataclass(slots=True)
class Session:
    session_id: str
    telegram_user_id: int
    chat_id: int
    command: str
    is_attached: bool = False
    detached_at: datetime | None = None
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
    stream_pending_text: str = ""
    stream_live_message_id: int | None = None
    stream_frame_index: int = 0
    stream_frame_body: str = ""
    stream_current_line_start: int = 0
    pending_echo_inputs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CodexSession:
    codex_session_id: str
    telegram_chat_id: int
    telegram_user_id: int
    workspace_path: Path
    state: CodexSessionState = "active"
    default_mode: CodexSessionMode = "continue"
    default_model: str = ""
    default_reasoning_effort: str = ""
    thread_ref: str | None = None
    last_run_id: str | None = None
    last_prompt: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None


@dataclass(slots=True)
class CodexRun:
    codex_run_id: str
    codex_session_id: str
    telegram_chat_id: int
    telegram_user_id: int
    workspace_path: Path
    prompt_text: str
    run_mode: CodexSessionMode = "continue"
    model_name: str = ""
    reasoning_effort: str = ""
    status: CodexRunStatus = "queued"
    summary_short: str = ""
    started_at: datetime = field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    exit_code: int | None = None
    stdout_artifact_path: Path | None = None
    stderr_artifact_path: Path | None = None
    structured_result_json: str = ""
    failure_summary: str = ""
