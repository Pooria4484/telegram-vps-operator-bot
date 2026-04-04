from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from datetime import datetime
from html import escape
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import shutil

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.auth import is_allowed
from app.chat_manager import ChatConversation, ChatManager, ChatMessage, UsageSnapshot
from app.command_runner import (
    build_command_env,
    send_ctrl_c,
    send_pty_input,
    start_live_command,
    stop_live_command,
)
from app.config import Settings
from app.models import Session
from app.session_manager import PendingUpload, SessionManager

SESSION_CONTROL_PREFIX = "sessctl"
CHAT_RESUME_PREFIX = "chatresume"
SESSION_STREAM_INTERVAL_SECONDS = 2.0
SESSION_STREAM_MAX_LINES = 20
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CHAT_REQUEST_TIMEOUT_SECONDS = 120
TELEGRAM_EDIT_TIMEOUT_SECONDS = 4.0
CHAT_RECONNECT_FAILFAST_SECONDS = 75
logger = logging.getLogger(__name__)


def resolve_cd_target(raw_target: str, current_dir: Path) -> Path:
    target = raw_target.strip() or "~"
    expanded = Path(target).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (current_dir / expanded).resolve()


def resolve_user_path(raw_path: str, current_dir: Path) -> Path:
    expanded = Path(raw_path.strip()).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (current_dir / expanded).resolve()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def save_telegram_file(message: Message, file_id: str, target_path: Path) -> None:
    telegram_file = await message.bot.get_file(file_id)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("wb") as out:
        await message.bot.download_file(telegram_file.file_path, destination=out)


def upload_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Overwrite", callback_data="upload_overwrite"),
                InlineKeyboardButton(text="Cancel", callback_data="upload_cancel"),
            ]
        ]
    )


def persistent_control_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="/status"),
                KeyboardButton(text="/tail"),
                KeyboardButton(text="/stop"),
                KeyboardButton(text="/clear"),
            ],
            [
                KeyboardButton(text="/ctrl c"),
                KeyboardButton(text="/ctrl d"),
                KeyboardButton(text="/n"),
                KeyboardButton(text="/stream toggle"),
            ],
            [
                KeyboardButton(text="/stream status"),
                KeyboardButton(text="/stream on"),
                KeyboardButton(text="/stream off"),
                KeyboardButton(text="/id"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def chat_mode_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="/chat usage"),
                KeyboardButton(text="/chat new"),
                KeyboardButton(text="/chat resume"),
                KeyboardButton(text="/chat exit"),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def session_control_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Stop", callback_data=f"{SESSION_CONTROL_PREFIX}:stop:{session_id}"),
                InlineKeyboardButton(text="Ctrl+C", callback_data=f"{SESSION_CONTROL_PREFIX}:ctrl_c:{session_id}"),
                InlineKeyboardButton(text="Ctrl+D", callback_data=f"{SESSION_CONTROL_PREFIX}:ctrl_d:{session_id}"),
                InlineKeyboardButton(text="Enter", callback_data=f"{SESSION_CONTROL_PREFIX}:enter:{session_id}"),
            ],
            [
                InlineKeyboardButton(text="Tail", callback_data=f"{SESSION_CONTROL_PREFIX}:tail:{session_id}"),
                InlineKeyboardButton(text="Status", callback_data=f"{SESSION_CONTROL_PREFIX}:status:{session_id}"),
                InlineKeyboardButton(text="Stream", callback_data=f"{SESSION_CONTROL_PREFIX}:stream_toggle:{session_id}"),
                InlineKeyboardButton(text="Clear Output", callback_data=f"{SESSION_CONTROL_PREFIX}:clear:{session_id}"),
            ],
        ]
    )


def parse_session_control_callback(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None

    parts = data.split(":", 2)
    if len(parts) != 3:
        return None

    prefix, action, session_id = parts
    if prefix != SESSION_CONTROL_PREFIX:
        return None

    if action not in {"stop", "ctrl_c", "ctrl_d", "enter", "tail", "status", "stream_toggle", "clear"}:
        return None

    if not session_id:
        return None

    return action, session_id


def parse_chat_resume_callback(data: str | None) -> str | None:
    if not data:
        return None
    prefix = f"{CHAT_RESUME_PREFIX}:"
    if not data.startswith(prefix):
        return None
    chat_id = data[len(prefix) :].strip()
    if not chat_id:
        return None
    return chat_id


def should_run_without_pty(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except Exception:
        return False
    if not parts:
        return False

    executable = Path(parts[0]).name.lower()
    if executable != "codex":
        return False

    flags = set(parts[1:])
    return bool({"--version", "-V", "--help", "-h"} & flags)


def format_reset_at(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def format_chat_usage(snapshot: UsageSnapshot) -> str:
    return (
        f"<b>5-hour usage:</b> <code>{snapshot.used_5h}/{snapshot.limit_5h}</code>\n"
        f"<b>5-hour reset:</b> <code>{format_reset_at(snapshot.reset_5h_at)}</code>\n"
        f"<b>Weekly usage:</b> <code>{snapshot.used_week}/{snapshot.limit_week}</code>\n"
        f"<b>Weekly reset:</b> <code>{format_reset_at(snapshot.reset_week_at)}</code>"
    )


def format_chat_summary(conversation: ChatConversation) -> str:
    return (
        f"<b>Chat mode</b>\n"
        f"<b>Active chat:</b> <code>{escape(conversation.conversation_id)}</code>\n"
        f"<b>Title:</b> <code>{escape(conversation.title)}</code>\n"
        f"<b>Messages:</b> <code>{len(conversation.messages)}</code>\n"
        f"<b>Use:</b> send plain text to chat with Codex"
    )


def chat_resume_keyboard(chats: list[ChatConversation]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for chat in chats[:20]:
        label = f"{chat.conversation_id} | {chat.title[:28]}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CHAT_RESUME_PREFIX}:{chat.conversation_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_codex_chat_prompt(messages: list[ChatMessage]) -> str:
    lines = [
        "You are Codex chat mode in a Telegram bot.",
        "Respond directly and concisely to the user.",
        "Do not execute shell commands unless explicitly asked.",
        "",
        "Conversation:",
    ]
    for msg in messages:
        role = "USER" if msg.role == "user" else "ASSISTANT"
        lines.append(f"{role}: {msg.text}")
    lines.append("")
    lines.append("Reply to the latest USER message.")
    return "\n".join(lines)


def prepare_codex_launch(codex_bin: str) -> tuple[list[str], dict[str, str]]:
    expanded = Path(codex_bin).expanduser()
    codex_path = expanded if expanded.is_absolute() else Path.cwd() / expanded
    codex_dir = codex_path.parent
    node_candidate = codex_dir / "node"
    env = os.environ.copy()

    if node_candidate.is_file() and os.access(node_candidate, os.X_OK):
        return [str(node_candidate), str(codex_path)], env

    path_value = env.get("PATH", "")
    env["PATH"] = f"{codex_dir}{os.pathsep}{path_value}" if path_value else str(codex_dir)
    return [str(codex_path)], env


def _call_codex_cli_chat(
    codex_bin: str,
    model: str,
    reasoning_effort: str,
    chat_messages: list[ChatMessage],
    workdir: Path,
) -> str:
    prompt = build_codex_chat_prompt(chat_messages)
    output_path = ""
    codex_cmd, codex_env = prepare_codex_launch(codex_bin)
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            output_path = tmp.name

        cmd = [
            *codex_cmd,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            str(workdir),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "-o",
            output_path,
            prompt,
        ]
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            env=codex_env,
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"codex exec failed ({completed.returncode}): {completed.stderr.strip()[:1200]}"
            )

        text = Path(output_path).read_text(encoding="utf-8").strip()
        if text:
            return text

        stdout = completed.stdout.strip()
        if stdout:
            return stdout

        return "[no response text]"
    finally:
        if output_path:
            with contextlib.suppress(Exception):
                Path(output_path).unlink()


async def ask_codex_chat(
    codex_bin: str,
    model: str,
    reasoning_effort: str,
    chat_messages: list[ChatMessage],
    workdir: Path,
    on_stream_update: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    prompt = build_codex_chat_prompt(chat_messages)
    output_path = ""
    codex_cmd, codex_env = prepare_codex_launch(codex_bin)
    stderr_chunks: list[str] = []
    stdout_fallback_chunks: list[str] = []
    streamed_text = ""
    completed_text = ""
    reconnect_level = 0
    last_status_text = ""

    def extract_completed_text(response_obj: object) -> str:
        if not isinstance(response_obj, dict):
            return ""
        output_items = response_obj.get("output")
        if not isinstance(output_items, list):
            return ""

        parts: list[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for chunk in content:
                if not isinstance(chunk, dict):
                    continue
                chunk_type = chunk.get("type")
                if chunk_type in {"output_text", "text"}:
                    text_value = chunk.get("text")
                    if isinstance(text_value, str):
                        parts.append(text_value)
        return "".join(parts).strip()

    def parse_event_text(event_obj: object) -> tuple[str, bool]:
        if not isinstance(event_obj, dict):
            return "", False
        event_type = str(event_obj.get("type", ""))
        if event_type == "item.completed":
            item = event_obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text_value = item.get("text")
                return (text_value, False) if isinstance(text_value, str) else ("", False)
        if event_type == "error":
            message = event_obj.get("message")
            if isinstance(message, str) and message.strip():
                return (f"[status] {message.strip()}", True)
            return "", False
        if "response.output_text.delta" in event_type:
            delta = event_obj.get("delta")
            return (delta, True) if isinstance(delta, str) else ("", False)
        if "response.completed" in event_type:
            return extract_completed_text(event_obj.get("response")), False
        if "response.output_text.done" in event_type:
            text_value = event_obj.get("text")
            return (text_value, False) if isinstance(text_value, str) else ("", False)
        return "", False

    async def read_stderr(proc: asyncio.subprocess.Process) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        while True:
            chunk = await stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk.decode(errors="replace"))

    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            output_path = tmp.name

        cmd = [
            *codex_cmd,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            str(workdir),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "--json",
            "-o",
            output_path,
            prompt,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=codex_env,
        )

        stderr_task = asyncio.create_task(read_stderr(proc))
        started_at = asyncio.get_running_loop().time()
        stdout = proc.stdout
        if stdout is not None:
            while True:
                if asyncio.get_running_loop().time() - started_at > CHAT_REQUEST_TIMEOUT_SECONDS:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(stderr_task, timeout=2.0)
                    raise RuntimeError(
                        f"codex exec timeout after {CHAT_REQUEST_TIMEOUT_SECONDS} seconds"
                    )
                elapsed = asyncio.get_running_loop().time() - started_at
                if (
                    reconnect_level >= 4
                    and elapsed >= CHAT_RECONNECT_FAILFAST_SECONDS
                    and not streamed_text.strip()
                    and not completed_text
                ):
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(stderr_task, timeout=2.0)
                    detail = last_status_text or "reconnecting"
                    raise RuntimeError(
                        f"codex reconnect fail-fast after {int(elapsed)}s: {detail}"
                    )

                try:
                    raw_line = await asyncio.wait_for(stdout.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                if not raw_line:
                    if proc.returncode is None:
                        continue
                    break
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event_obj = json.loads(line)
                except Exception:
                    stdout_fallback_chunks.append(line)
                    continue

                event_text, is_delta = parse_event_text(event_obj)
                if not event_text:
                    continue
                if is_delta:
                    if event_text.startswith("[status] "):
                        status_raw = event_text[len("[status] ") :].strip()
                        last_status_text = status_raw
                        match = re.search(r"Reconnecting\.\.\.\s+(\d+)/5", status_raw)
                        if match:
                            with contextlib.suppress(Exception):
                                reconnect_level = int(match.group(1))
                        if on_stream_update is not None:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(on_stream_update(event_text), timeout=2.0)
                    else:
                        streamed_text += event_text
                        if on_stream_update is not None:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(on_stream_update(streamed_text), timeout=2.0)
                else:
                    completed_text = event_text

        return_code = await proc.wait()
        await stderr_task
        if return_code != 0:
            stderr_text = "".join(stderr_chunks).strip()
            stdout_text = "\n".join(stdout_fallback_chunks).strip()
            details = stderr_text or stdout_text or "unknown error"
            raise RuntimeError(f"codex exec failed ({return_code}): {details[:1200]}")

        output_text = Path(output_path).read_text(encoding="utf-8").strip()
        if output_text:
            return output_text
        if streamed_text.strip():
            return streamed_text.strip()
        if completed_text:
            return completed_text
        if stdout_fallback_chunks:
            fallback = "\n".join(stdout_fallback_chunks).strip()
            if fallback:
                return fallback
        return "[no response text]"
    finally:
        if output_path:
            with contextlib.suppress(Exception):
                Path(output_path).unlink()


def resolve_codex_binary(configured: str) -> str:
    if configured:
        expanded = str(Path(configured).expanduser())
        if os.path.isabs(expanded) and os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded

    found = shutil.which(configured or "codex")
    if found:
        return found

    candidates = sorted(
        Path.home().glob(".nvm/versions/node/*/bin/codex"),
        reverse=True,
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    raise RuntimeError(
        "codex binary not found. Set CODEX_BIN in .env (example: /home/pooria/.nvm/versions/node/v20.20.2/bin/codex)"
    )


def format_session_header(session_id: str, state: str) -> str:
    safe_session_id = escape(session_id)
    safe_state = escape(state)
    return f"<b>Session</b> <code>{safe_session_id}</code> | <b>State</b> <code>{safe_state}</code>"


def render_output_lines(output: str) -> str:
    lines = (output or "[no output]").splitlines() or ["[no output]"]
    rendered: list[str] = []
    for line in lines:
        if not line:
            rendered.append("<code> </code>")
            continue

        parts = [part for part in re.split(r"\s+", line.strip()) if part]
        if not parts:
            rendered.append("<code> </code>")
            continue

        rendered.append(" ".join(f"<code>{escape(part)}</code>" for part in parts))
    return "\n".join(rendered)


def normalize_stream_text(text: str, limit: int = 3500) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", text).replace("\r", "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"...\n{cleaned[-limit:]}"


def run_oneshot_shell_command(
    command: str,
    shell: str,
    cwd: Path,
    timeout_seconds: float,
) -> tuple[int, str, str]:
    env = build_command_env()
    completed = subprocess.run(
        [shell, "-lc", command],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=env,
    )
    return completed.returncode, completed.stdout, completed.stderr


async def answer_no_active_session(message: Message, current_dir: Path) -> None:
    await message.answer(
        f"<b>No active session</b>\n"
        f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>",
        parse_mode="HTML",
        reply_markup=persistent_control_keyboard(),
    )


async def answer_active_session_exists(message: Message, session_id: str) -> None:
    await message.answer(
        f"<b>You already have an active session</b>\n"
        f"<b>Session:</b> <code>{escape(session_id)}</code>\n"
        f"<b>How to continue:</b> send plain text (example: <code>ls</code>)\n"
        f"<b>Controls:</b> <code>/n</code>, <code>/ctrl c</code>, <code>/ctrl d</code>, <code>/tail</code>, "
        f"<code>/status</code>, <code>/stop</code>, <code>/clear</code>\n"
        f"<b>Stream toggle:</b> <code>/stream toggle</code> (or on/off)",
        parse_mode="HTML",
        reply_markup=persistent_control_keyboard(),
    )


def format_live_start_message(
    session_id: str,
    state: str,
    pid: int,
    cwd: str,
    command: str,
    stream_enabled: bool,
) -> str:
    stream_mode = "on" if stream_enabled else "off"
    return (
        f"<b>Live Session Started</b>\n"
        f"{format_session_header(session_id, state)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>PID:</b> <code>{pid}</code>\n"
        f"<b>Current dir:</b> <code>{escape(cwd)}</code>\n"
        f"<b>Command:</b> <code>{escape(command)}</code>\n"
        f"<b>Stream mode:</b> <code>{stream_mode}</code> "
        f"(toggle with <code>/stream toggle</code>)\n"
        f"<b>Interactive:</b> send plain text to active session (example: <code>ls</code>)\n"
        f"<b>Tip:</b> use <code>/tail</code>, <code>/status</code>, <code>/stop</code>, "
        f"<code>/ctrl c</code>, <code>/ctrl d</code>, <code>/n</code>, <code>/clear</code>, <code>/stream status</code>"
    )


def format_session_result(
    session_id: str,
    state: str,
    exit_code: int | None,
    cwd: str,
    output: str,
) -> str:
    safe_exit = escape(str(exit_code))
    safe_cwd = escape(cwd)
    rendered_lines = render_output_lines(output)

    return (
        f"{format_session_header(session_id, state)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>Exit code:</b> <code>{safe_exit}</code>\n"
        f"<b>Current dir:</b> <code>{safe_cwd}</code>\n"
        f"<b>Output</b>\n"
        f"{rendered_lines}"
    )


def format_session_status_message(session: Session, current_dir: Path, stream_enabled: bool) -> str:
    runtime_end = session.ended_at or datetime.utcnow()
    runtime = runtime_end - session.started_at
    pid = session.process.pid if session.process else "n/a"
    stream_mode = "on" if stream_enabled else "off"
    return (
        f"{format_session_header(session.session_id, session.state)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>Command:</b> <code>{escape(session.command)}</code>\n"
        f"<b>PID:</b> <code>{escape(str(pid))}</code>\n"
        f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
        f"<b>Runtime:</b> <code>{escape(str(runtime).split('.')[0])}</code>\n"
        f"<b>Stream mode:</b> <code>{stream_mode}</code>\n"
        f"<b>Exit code:</b> <code>{escape(str(session.exit_code))}</code>"
    )


def format_tail_message(session: Session, tail_lines: list[str]) -> str:
    if not tail_lines:
        return (
            f"{format_session_header(session.session_id, session.state)}\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>Tail</b>\n"
            f"<code>[no output]</code>"
        )

    text = "\n".join(tail_lines)
    return (
        f"{format_session_header(session.session_id, session.state)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>Tail</b>\n"
        f"{render_output_lines(text)}"
    )


async def read_session_output(
    session: Session,
    session_manager: SessionManager,
    max_tail_lines: int,
) -> None:
    master_fd = session.pty_master_fd
    if master_fd is None:
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def on_readable() -> None:
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            chunk = b""

        if not chunk:
            with contextlib.suppress(Exception):
                loop.remove_reader(master_fd)
            queue.put_nowait(None)
            return

        queue.put_nowait(chunk)

    loop.add_reader(master_fd, on_readable)
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            session_manager.append_output_text(
                session,
                chunk.decode(errors="replace"),
                max_tail_lines,
            )
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(master_fd)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        session.pty_master_fd = None


async def stream_session_output(
    session: Session,
    session_manager: SessionManager,
    settings: Settings,
    bot: Bot,
) -> None:
    while True:
        await asyncio.sleep(SESSION_STREAM_INTERVAL_SECONDS)

        process = session.process
        if process is None:
            break

        if not session_manager.is_stream_enabled(session.telegram_user_id):
            continue

        tail_lines = session_manager.get_tail_snapshot(session, settings.max_tail_lines)
        if not tail_lines:
            continue

        snapshot_lines = tail_lines[-SESSION_STREAM_MAX_LINES:]
        snapshot_text = "\n".join(snapshot_lines)
        if snapshot_text == session.stream_last_sent_text:
            continue

        session.stream_last_sent_text = snapshot_text
        stream_message = (
            f"{format_session_header(session.session_id, session.state)}\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>Live stream</b>\n"
            f"{render_output_lines(snapshot_text)}"
        )

        with contextlib.suppress(Exception):
            await bot.send_message(session.chat_id, stream_message, parse_mode="HTML")


async def wait_session_exit(
    session: Session,
    session_manager: SessionManager,
    settings: Settings,
    bot: Bot,
) -> None:
    process = session.process
    if process is None:
        session_manager.finish_session(session)
        session.waiter_task = None
        return

    try:
        exit_code = await process.wait()
        session.exit_code = exit_code
        session.state = "stopped" if session.stop_requested else ("finished" if exit_code == 0 else "failed")
        session.ended_at = datetime.utcnow()
    except Exception as exc:
        session.state = "failed"
        session.ended_at = datetime.utcnow()
        session_manager.append_output_text(
            session,
            f"ERROR: {exc!r}\n",
            settings.max_tail_lines,
        )
    finally:
        # Release active-session lock first, then perform best-effort cleanup.
        session_manager.finish_session(session)
        session.waiter_task = None

        streamer_task = session.streamer_task
        if streamer_task:
            streamer_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(streamer_task, timeout=1.5)
            session.streamer_task = None

        reader_task = session.reader_task
        if reader_task:
            if not reader_task.done():
                reader_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(reader_task, timeout=1.5)
            session.reader_task = None

        session.process = None

    current_dir = session_manager.get_current_workdir(session.telegram_user_id)
    tail_lines = session_manager.get_tail_snapshot(session, settings.max_tail_lines)
    tail_text = "\n".join(tail_lines) if tail_lines else "[no output]"
    final_text = format_session_result(
        session_id=session.session_id,
        state=session.state,
        exit_code=session.exit_code,
        cwd=str(current_dir),
        output=tail_text,
    )

    with contextlib.suppress(Exception):
        await bot.send_message(session.chat_id, final_text, parse_mode="HTML")


def build_dispatcher(settings: Settings, session_manager: SessionManager) -> Dispatcher:
    dp = Dispatcher()
    chat_manager = ChatManager()

    @dp.message(CommandStart())
    async def start_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        current_dir = session_manager.get_current_workdir(user.id)
        await message.answer(
            "Bot is ready.\n\n"
            "Commands:\n"
            "/id\n"
            "/run <command>\n"
            "/chat [new|resume|usage|exit]\n"
            "/stop\n"
            "/ctrl <c|d>\n"
            "/n\n"
            "/clear\n"
            "/stream <on|off|toggle|status> (alias: /live)\n"
            "/get <path>\n"
            "/status\n"
            "/tail\n\n"
            "Upload behavior:\n"
            "- send a file directly\n"
            "- it will be saved in your current dir\n\n"
            f"Current dir: {current_dir}",
            reply_markup=persistent_control_keyboard(),
        )

    @dp.message(Command("id"))
    async def id_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        await message.answer(f"Your Telegram user id: {user.id}")

    @dp.message(Command("chat"))
    async def chat_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        action = parts[1].strip().lower() if len(parts) >= 2 else "enter"
        if action not in {"enter", "new", "resume", "usage", "exit"}:
            await message.answer(
                "Usage: /chat [new|resume|usage|exit]",
                reply_markup=chat_mode_keyboard(),
            )
            return

        if action == "exit":
            chat_manager.set_chat_mode(user.id, False)
            current_dir = session_manager.get_current_workdir(user.id)
            await message.answer(
                f"Chat mode disabled.\nCurrent dir: {current_dir}",
                reply_markup=persistent_control_keyboard(),
            )
            return

        chat_manager.set_chat_mode(user.id, True)

        if action == "new":
            conversation = chat_manager.create_new_chat(user.id)
            usage = chat_manager.get_usage_snapshot(
                telegram_user_id=user.id,
                limit_5h=settings.chat_limit_5h_requests,
                limit_week=settings.chat_limit_week_requests,
            )
            await message.answer(
                f"{format_chat_summary(conversation)}\n\n{format_chat_usage(usage)}",
                parse_mode="HTML",
                reply_markup=chat_mode_keyboard(),
            )
            return

        if action == "resume":
            chats = chat_manager.list_user_chats(user.id)
            if not chats:
                conversation = chat_manager.create_new_chat(user.id)
                usage = chat_manager.get_usage_snapshot(
                    telegram_user_id=user.id,
                    limit_5h=settings.chat_limit_5h_requests,
                    limit_week=settings.chat_limit_week_requests,
                )
                await message.answer(
                    "<b>No previous chat found.</b>\nStarted a new chat.\n\n"
                    f"{format_chat_summary(conversation)}\n\n{format_chat_usage(usage)}",
                    parse_mode="HTML",
                    reply_markup=chat_mode_keyboard(),
                )
                return

            await message.answer(
                "<b>Select a chat to resume</b>",
                parse_mode="HTML",
                reply_markup=chat_resume_keyboard(chats),
            )
            await message.answer(
                "Chat controls are ready.",
                reply_markup=chat_mode_keyboard(),
            )
            return

        if action == "usage":
            usage = chat_manager.get_usage_snapshot(
                telegram_user_id=user.id,
                limit_5h=settings.chat_limit_5h_requests,
                limit_week=settings.chat_limit_week_requests,
            )
            active = chat_manager.get_or_create_active_chat(user.id)
            await message.answer(
                f"{format_chat_summary(active)}\n\n{format_chat_usage(usage)}",
                parse_mode="HTML",
                reply_markup=chat_mode_keyboard(),
            )
            return

        conversation = chat_manager.get_or_create_active_chat(user.id)
        usage = chat_manager.get_usage_snapshot(
            telegram_user_id=user.id,
            limit_5h=settings.chat_limit_5h_requests,
            limit_week=settings.chat_limit_week_requests,
        )
        active_session = session_manager.get_active_session_for_user(user.id)
        session_hint = ""
        if active_session:
            session_hint = (
                "\n\n<b>Note:</b> you have an active shell session "
                f"<code>{escape(active_session.session_id)}</code>. "
                "Plain text will be routed to that session until it is stopped."
            )
        await message.answer(
            f"{format_chat_summary(conversation)}\n\n{format_chat_usage(usage)}{session_hint}",
            parse_mode="HTML",
            reply_markup=chat_mode_keyboard(),
        )

    @dp.message(Command("status"))
    async def status_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_active_session_for_user(user.id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        stream_enabled = session_manager.is_stream_enabled(user.id)
        await message.answer(
            format_session_status_message(session, current_dir, stream_enabled),
            parse_mode="HTML",
        )

    @dp.message(Command("tail"))
    async def tail_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_active_session_for_user(user.id)
        if not session:
            session = session_manager.get_latest_session_for_user(user.id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        tail_lines = session_manager.get_tail_snapshot(session, settings.max_tail_lines)
        await message.answer(format_tail_message(session, tail_lines), parse_mode="HTML")

    @dp.message(Command("get"))
    async def get_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("Usage: /get <path>")
            return

        current_dir = session_manager.get_current_workdir(user.id)
        raw_path = parts[1].strip()
        target_path = resolve_user_path(raw_path, current_dir)

        if not target_path.exists():
            await message.answer(
                f"<b>Get failed</b>\n"
                f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
                f"<b>Path:</b> <code>{escape(str(target_path))}</code>\n"
                f"<b>Reason:</b> <code>path does not exist</code>",
                parse_mode="HTML",
            )
            return

        if not target_path.is_file():
            await message.answer(
                f"<b>Get failed</b>\n"
                f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
                f"<b>Path:</b> <code>{escape(str(target_path))}</code>\n"
                f"<b>Reason:</b> <code>path is not a file</code>",
                parse_mode="HTML",
            )
            return

        try:
            file_size = target_path.stat().st_size
            file_sha256 = sha256_file(target_path)
            file_to_send = FSInputFile(str(target_path))
            await message.answer_document(
                file_to_send,
                caption=(
                    f"<b>File sent</b>\n"
                    f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
                    f"<b>Path:</b> <code>{escape(str(target_path))}</code>\n"
                    f"<b>Size:</b> <code>{file_size}</code>\n"
                    f"<b>SHA256:</b> <code>{file_sha256}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            await message.answer(
                f"<b>Get failed</b>\n"
                f"<b>Path:</b> <code>{escape(str(target_path))}</code>\n"
                f"<b>Error:</b> <code>{escape(str(exc))}</code>",
                parse_mode="HTML",
            )

    @dp.message(F.document)
    async def upload_document_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        document = message.document
        if not document or not document.file_name:
            await message.answer("Upload failed: missing file name.")
            return

        current_dir = session_manager.get_current_workdir(user.id)
        target_path = (current_dir / document.file_name).resolve()

        if target_path.exists():
            session_manager.set_pending_upload(
                PendingUpload(
                    telegram_user_id=user.id,
                    chat_id=message.chat.id,
                    file_id=document.file_id,
                    file_name=document.file_name,
                    target_path=target_path,
                )
            )
            await message.answer(
                f"<b>File already exists</b>\n"
                f"<b>Path:</b> <code>{escape(str(target_path))}</code>\n"
                f"<b>Action:</b> <code>overwrite?</code>",
                parse_mode="HTML",
                reply_markup=upload_confirm_keyboard(),
            )
            return

        try:
            await save_telegram_file(message, document.file_id, target_path)
            file_sha256 = sha256_file(target_path)
            await message.answer(
                f"<b>Uploaded</b>\n"
                f"<b>SHA256:</b> <code>{file_sha256}</code>",
                parse_mode="HTML",
            )
        except Exception as exc:
            await message.answer(
                f"<b>Upload failed</b>\n"
                f"<b>Error:</b> <code>{escape(str(exc))}</code>",
                parse_mode="HTML",
            )

    @dp.callback_query(F.data == "upload_cancel")
    async def upload_cancel_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return

        pending = session_manager.get_pending_upload(user.id)
        session_manager.clear_pending_upload(user.id)

        if callback.message:
            text = "<b>Upload cancelled</b>"
            if pending:
                text += f"\n<b>Path:</b> <code>{escape(str(pending.target_path))}</code>"
            await callback.message.edit_text(text, parse_mode="HTML")

        await callback.answer("Cancelled")

    @dp.callback_query(F.data == "upload_overwrite")
    async def upload_overwrite_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return

        pending = session_manager.get_pending_upload(user.id)
        if not pending:
            await callback.answer("No pending upload", show_alert=False)
            if callback.message:
                await callback.message.edit_text("<b>No pending upload</b>", parse_mode="HTML")
            return

        if callback.message:
            await callback.message.edit_text(
                f"<b>Overwriting</b>\n"
                f"<b>Path:</b> <code>{escape(str(pending.target_path))}</code>",
                parse_mode="HTML",
            )

        try:
            telegram_file = await callback.bot.get_file(pending.file_id)
            pending.target_path.parent.mkdir(parents=True, exist_ok=True)
            with pending.target_path.open("wb") as out:
                await callback.bot.download_file(telegram_file.file_path, destination=out)

            file_sha256 = sha256_file(pending.target_path)

            if callback.message:
                await callback.message.edit_text(
                    f"<b>Uploaded</b>\n"
                    f"<b>SHA256:</b> <code>{file_sha256}</code>",
                    parse_mode="HTML",
                )
            await callback.answer("Overwritten")
        except Exception as exc:
            if callback.message:
                await callback.message.edit_text(
                    f"<b>Upload failed</b>\n"
                    f"<b>Error:</b> <code>{escape(str(exc))}</code>",
                    parse_mode="HTML",
                )
            await callback.answer("Failed", show_alert=False)
        finally:
            session_manager.clear_pending_upload(user.id)

    @dp.callback_query(F.data.startswith(f"{CHAT_RESUME_PREFIX}:"))
    async def chat_resume_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return

        chat_id = parse_chat_resume_callback(callback.data)
        if not chat_id:
            await callback.answer("Invalid chat.", show_alert=False)
            return

        resumed = chat_manager.resume_chat(user.id, chat_id)
        if not resumed:
            await callback.answer("Chat not found.", show_alert=False)
            return

        chat_manager.set_chat_mode(user.id, True)
        usage = chat_manager.get_usage_snapshot(
            telegram_user_id=user.id,
            limit_5h=settings.chat_limit_5h_requests,
            limit_week=settings.chat_limit_week_requests,
        )
        if callback.message:
            await callback.message.answer(
                f"<b>Resumed chat</b>\n{format_chat_summary(resumed)}\n\n{format_chat_usage(usage)}",
                parse_mode="HTML",
                reply_markup=chat_mode_keyboard(),
            )
        await callback.answer("Chat resumed.", show_alert=False)

    @dp.callback_query(F.data.startswith(f"{SESSION_CONTROL_PREFIX}:"))
    async def session_control_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return

        parsed = parse_session_control_callback(callback.data)
        if not parsed:
            await callback.answer("Invalid control.", show_alert=False)
            return

        action, target_session_id = parsed
        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_active_session_for_user(user.id)
        if not session:
            await callback.answer("No active session.", show_alert=False)
            return

        if session.session_id != target_session_id:
            await callback.answer("Stale control message.", show_alert=False)
            return

        if action == "status":
            if callback.message:
                await callback.message.answer(
                    format_session_status_message(
                        session,
                        current_dir,
                        session_manager.is_stream_enabled(user.id),
                    ),
                    parse_mode="HTML",
                )
            await callback.answer("Status sent.", show_alert=False)
            return

        if action == "tail":
            if callback.message:
                tail_lines = session_manager.get_tail_snapshot(session, settings.max_tail_lines)
                await callback.message.answer(
                    format_tail_message(session, tail_lines),
                    parse_mode="HTML",
                )
            await callback.answer("Tail sent.", show_alert=False)
            return

        if action == "stream_toggle":
            enabled = not session_manager.is_stream_enabled(user.id)
            session_manager.set_stream_enabled(user.id, enabled)
            if enabled:
                session.stream_last_sent_text = ""
            mode_text = "on" if enabled else "off"
            await callback.answer(f"Stream mode: {mode_text}", show_alert=False)
            return

        if action == "clear":
            session_manager.clear_output_buffer(session)
            await callback.answer("Output buffer cleared.", show_alert=False)
            return

        if action == "stop":
            process = session.process
            if process is None:
                await callback.answer("No live process.", show_alert=False)
                return

            if session.stop_requested:
                await callback.answer("Stop already requested.", show_alert=False)
                return

            session.stop_requested = True
            await callback.answer("Stop requested.", show_alert=False)
            await stop_live_command(process)
            return

        if action == "ctrl_c":
            process = session.process
            if process is None:
                await callback.answer("No live process.", show_alert=False)
                return

            if not send_ctrl_c(process):
                await callback.answer("Process is no longer running.", show_alert=False)
                return

            await callback.answer("Sent Ctrl+C.", show_alert=False)
            return

        master_fd = session.pty_master_fd
        if master_fd is None:
            await callback.answer("No active PTY.", show_alert=False)
            return

        payload = b"\x04" if action == "ctrl_d" else b"\n"
        action_name = "Ctrl+D" if action == "ctrl_d" else "Enter"
        if not send_pty_input(master_fd, payload):
            await callback.answer("PTY is no longer available.", show_alert=False)
            return

        await callback.answer(f"Sent {action_name}.", show_alert=False)

    @dp.message(Command("stop"))
    async def stop_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_active_session_for_user(user.id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        process = session.process
        if process is None:
            await message.answer("No live process is attached to the active session.")
            return

        if session.stop_requested:
            await message.answer("Stop is already requested for this session.")
            return

        session.stop_requested = True
        await message.answer(f"Stop requested for session {session.session_id}.")
        await stop_live_command(process)

    @dp.message(Command("ctrl"))
    async def ctrl_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: /ctrl <c|d>")
            return

        action = parts[1].strip().lower()
        if action not in {"c", "d"}:
            await message.answer("Usage: /ctrl <c|d>")
            return

        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_active_session_for_user(user.id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        if action == "c":
            process = session.process
            if process is None:
                await message.answer("No live process is attached to the active session.")
                return

            if not send_ctrl_c(process):
                await message.answer("Could not send Ctrl+C. Process is no longer running.")
                return

            await message.answer(f"Sent Ctrl+C to session {session.session_id}.")
            return

        master_fd = session.pty_master_fd
        if master_fd is None:
            await message.answer("No active PTY is attached to the session.")
            return

        if not send_pty_input(master_fd, b"\x04"):
            await message.answer("Could not send Ctrl+D. PTY is no longer available.")
            return

        await message.answer(f"Sent Ctrl+D (EOF) to session {session.session_id}.")

    @dp.message(Command("n"))
    async def newline_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_active_session_for_user(user.id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        master_fd = session.pty_master_fd
        if master_fd is None:
            await message.answer("No active PTY is attached to the session.")
            return

        if not send_pty_input(master_fd, b"\n"):
            await message.answer("Could not send Enter. PTY is no longer available.")
            return

        await message.answer(f"Sent Enter to session {session.session_id}.")

    @dp.message(Command("clear"))
    async def clear_output_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_active_session_for_user(user.id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        session_manager.clear_output_buffer(session)
        await message.answer(
            f"Output buffer cleared for session <code>{escape(session.session_id)}</code>.",
            parse_mode="HTML",
        )

    @dp.message(Command(commands=["stream", "live"]))
    async def stream_mode_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        mode = parts[1].strip().lower() if len(parts) >= 2 else "status"
        if mode not in {"on", "off", "toggle", "status"}:
            await message.answer("Usage: /stream <on|off|toggle|status> (alias: /live)")
            return

        if mode == "status":
            enabled = session_manager.is_stream_enabled(user.id)
            state_text = "on" if enabled else "off"
            await message.answer(f"Stream mode: <code>{state_text}</code>", parse_mode="HTML")
            return

        if mode == "toggle":
            enabled = not session_manager.is_stream_enabled(user.id)
        else:
            enabled = mode == "on"
        session_manager.set_stream_enabled(user.id, enabled)
        if enabled:
            session = session_manager.get_active_session_for_user(user.id)
            if session:
                session.stream_last_sent_text = ""
        state_text = "enabled" if enabled else "disabled"
        await message.answer(f"Stream mode {state_text}.", parse_mode="HTML")

    @dp.message(Command("run"))
    async def run_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: /run <command>")
            return

        command = parts[1].strip()
        if not command:
            await message.answer("Usage: /run <command>")
            return

        current_dir = session_manager.get_current_workdir(user.id)

        if command == "cd" or command.startswith("cd "):
            raw_target = command[2:].strip()
            new_dir = resolve_cd_target(raw_target, current_dir)

            if not new_dir.exists():
                await message.answer(
                    f"<b>cd failed</b>\n"
                    f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
                    f"<b>Reason:</b> <code>target does not exist</code>",
                    parse_mode="HTML",
                )
                return

            if not new_dir.is_dir():
                await message.answer(
                    f"<b>cd failed</b>\n"
                    f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
                    f"<b>Reason:</b> <code>target is not a directory</code>",
                    parse_mode="HTML",
                )
                return

            session_manager.set_current_workdir(user.id, new_dir)
            await message.answer(
                f"<b>Directory changed</b>\n"
                f"<b>Current dir:</b> <code>{escape(str(new_dir))}</code>",
                parse_mode="HTML",
            )
            return

        active = session_manager.get_active_session_for_user(user.id)
        if active:
            await answer_active_session_exists(message, active.session_id)
            return

        if should_run_without_pty(command):
            try:
                exit_code, stdout_text, stderr_text = await asyncio.to_thread(
                    run_oneshot_shell_command,
                    command=command,
                    shell=settings.default_shell,
                    cwd=current_dir,
                    timeout_seconds=20,
                )
            except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                await message.answer(
                    f"<b>Command timed out</b>\n"
                    f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
                    f"<b>Command:</b> <code>{escape(command)}</code>",
                    parse_mode="HTML",
                    reply_markup=persistent_control_keyboard(),
                )
                return
            except Exception as exc:
                await message.answer(
                    f"<b>Run failed</b>\n<code>{escape(str(exc))}</code>",
                    parse_mode="HTML",
                    reply_markup=persistent_control_keyboard(),
                )
                return

            combined_output = f"{stdout_text}{stderr_text}".strip() or "[no output]"
            state = "finished" if exit_code == 0 else "failed"
            await message.answer(
                format_session_result(
                    session_id="oneshot",
                    state=state,
                    exit_code=exit_code,
                    cwd=str(current_dir),
                    output=combined_output,
                ),
                parse_mode="HTML",
                reply_markup=persistent_control_keyboard(),
            )
            return

        session = session_manager.create_session(
            telegram_user_id=user.id,
            chat_id=message.chat.id,
            command=command,
        )
        try:
            live = await start_live_command(
                command=command,
                shell=settings.default_shell,
                cwd=current_dir,
            )
        except Exception as exc:
            session.state = "failed"
            session.ended_at = datetime.utcnow()
            session_manager.append_output_text(
                session,
                f"ERROR: {exc!r}\n",
                settings.max_tail_lines,
            )
            session_manager.finish_session(session)

            tail_lines = session_manager.get_tail_snapshot(session, settings.max_tail_lines)
            tail_text = "\n".join(tail_lines) if tail_lines else "[no output]"
            final_text = format_session_result(
                session_id=session.session_id,
                state=session.state,
                exit_code=session.exit_code,
                cwd=str(current_dir),
                output=tail_text,
            )
            await message.answer(final_text, parse_mode="HTML")
            return

        session.state = "running"
        session.process = live.process
        session.pty_master_fd = live.pty_master_fd
        session.stream_last_sent_text = ""
        session.reader_task = asyncio.create_task(
            read_session_output(session, session_manager, settings.max_tail_lines)
        )
        session.streamer_task = asyncio.create_task(
            stream_session_output(session, session_manager, settings, message.bot)
        )
        session.waiter_task = asyncio.create_task(
            wait_session_exit(session, session_manager, settings, message.bot)
        )

        await message.answer(
            format_live_start_message(
                session_id=session.session_id,
                state=session.state,
                pid=live.process.pid,
                cwd=str(current_dir),
                command=command,
                stream_enabled=session_manager.is_stream_enabled(user.id),
            ),
            parse_mode="HTML",
            reply_markup=session_control_keyboard(session.session_id),
        )
        await message.answer(
            "Persistent controls are available on your keyboard.",
            reply_markup=persistent_control_keyboard(),
        )

    @dp.message(F.text & ~F.text.startswith("/"))
    async def text_routing_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        if not text:
            return

        session = session_manager.get_active_session_for_user(user.id)
        if session:
            master_fd = session.pty_master_fd
            if master_fd is None:
                await message.answer("No active PTY is attached to the session.")
                return

            text = message.text or ""
            if not text:
                return

            data = text.encode(errors="replace") + b"\n"
            if not send_pty_input(master_fd, data):
                await message.answer("Could not send text to active session.")
            return

        if not chat_manager.is_chat_mode(user.id):
            current_dir = session_manager.get_current_workdir(user.id)
            await message.answer(
                f"<b>No active route for plain text</b>\n"
                f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
                f"<b>Use:</b> <code>/chat</code> for Codex chat or <code>/run &lt;command&gt;</code> for shell.",
                parse_mode="HTML",
                reply_markup=persistent_control_keyboard(),
            )
            return

        if not settings.codex_auth_path.exists():
            await message.answer(
                f"Chat mode auth is not available.\n"
                f"Expected auth file: <code>{escape(str(settings.codex_auth_path))}</code>\n"
                "<code>auth file not found</code>",
                reply_markup=chat_mode_keyboard(),
                parse_mode="HTML",
            )
            return

        allowed, usage = chat_manager.consume_usage(
            telegram_user_id=user.id,
            limit_5h=settings.chat_limit_5h_requests,
            limit_week=settings.chat_limit_week_requests,
        )
        if not allowed:
            await message.answer(
                f"<b>Chat limit reached</b>\n{format_chat_usage(usage)}",
                parse_mode="HTML",
                reply_markup=chat_mode_keyboard(),
            )
            return

        conversation = chat_manager.get_or_create_active_chat(user.id)
        chat_manager.append_message(conversation, "user", text)
        history = chat_manager.recent_messages(conversation, settings.chat_history_messages)
        try:
            codex_bin = resolve_codex_binary(settings.codex_bin)
        except Exception as exc:
            await message.answer(
                f"<b>Chat request failed</b>\n<code>{escape(str(exc))}</code>",
                parse_mode="HTML",
                reply_markup=chat_mode_keyboard(),
            )
            return

        progress_message = await message.answer("Codex stream: ...")
        loop = asyncio.get_running_loop()
        last_stream_edit_at = 0.0
        last_stream_view = ""
        stream_finished = False
        last_status_hint = ""
        last_status_hint_at = 0.0

        async def update_progress_message(payload: str, parse_mode: str | None = None) -> None:
            nonlocal progress_message
            try:
                if parse_mode:
                    await asyncio.wait_for(
                        progress_message.edit_text(payload, parse_mode=parse_mode),
                        timeout=TELEGRAM_EDIT_TIMEOUT_SECONDS,
                    )
                else:
                    await asyncio.wait_for(
                        progress_message.edit_text(payload),
                        timeout=TELEGRAM_EDIT_TIMEOUT_SECONDS,
                    )
                return
            except Exception as exc:
                logger.warning("chat stream edit failed; sending fallback message: %s", exc)
                try:
                    if parse_mode:
                        progress_message = await asyncio.wait_for(
                            message.answer(payload, parse_mode=parse_mode),
                            timeout=TELEGRAM_EDIT_TIMEOUT_SECONDS,
                        )
                    else:
                        progress_message = await asyncio.wait_for(
                            message.answer(payload),
                            timeout=TELEGRAM_EDIT_TIMEOUT_SECONDS,
                        )
                except Exception as fallback_exc:
                    logger.warning("chat stream fallback send failed: %s", fallback_exc)

        async def on_stream_update(stream_text: str) -> None:
            nonlocal last_stream_edit_at, last_stream_view, last_status_hint, last_status_hint_at
            now = loop.time()
            if now - last_stream_edit_at < 1.0:
                return

            normalized = normalize_stream_text(stream_text)
            if not normalized:
                return
            if normalized.startswith("[status] "):
                last_status_hint = normalized[len("[status] ") :].strip()
                last_status_hint_at = now
                view = f"Codex stream status: {last_status_hint}"
            else:
                view = f"Codex stream:\n{normalized}"
            if view == last_stream_view:
                return

            last_stream_view = view
            last_stream_edit_at = now
            await update_progress_message(view)

        async def heartbeat() -> None:
            nonlocal last_stream_edit_at, last_stream_view
            started = loop.time()
            while not stream_finished:
                await asyncio.sleep(4.0)
                if stream_finished:
                    return
                now = loop.time()
                if now - last_stream_edit_at < 4.0 and last_stream_view.startswith("Codex stream:\n"):
                    continue
                wait_seconds = int(now - started)
                hint = ""
                if last_status_hint and (now - last_status_hint_at) <= 45:
                    hint = f" | last status: {last_status_hint}"
                view = f"Codex stream: waiting for model response... {wait_seconds}s{hint}"
                if view == last_stream_view:
                    continue
                last_stream_view = view
                last_stream_edit_at = now
                await update_progress_message(view)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            answer_text = await asyncio.wait_for(
                ask_codex_chat(
                    codex_bin=codex_bin,
                    model=settings.chat_model,
                    reasoning_effort=settings.chat_reasoning_effort,
                    chat_messages=history,
                    workdir=settings.workdir,
                    on_stream_update=on_stream_update,
                ),
                timeout=CHAT_REQUEST_TIMEOUT_SECONDS + 5,
            )
        except Exception as exc:
            stream_finished = True
            heartbeat_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(heartbeat_task, timeout=1.0)
            error_payload = f"<b>Chat request failed</b>\n<code>{escape(str(exc))}</code>"
            try:
                await update_progress_message(error_payload, parse_mode="HTML")
            except Exception:
                await message.answer(
                    error_payload,
                    parse_mode="HTML",
                    reply_markup=chat_mode_keyboard(),
                )
            return

        stream_finished = True
        heartbeat_task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(heartbeat_task, timeout=1.0)
        chat_manager.append_message(conversation, "assistant", answer_text)
        usage_view = chat_manager.get_usage_snapshot(
            telegram_user_id=user.id,
            limit_5h=settings.chat_limit_5h_requests,
            limit_week=settings.chat_limit_week_requests,
        )
        final_answer = normalize_stream_text(answer_text, limit=3000) or "[no response text]"
        final_payload = f"{escape(final_answer)}\n\n{format_chat_usage(usage_view)}"
        try:
            await update_progress_message(final_payload, parse_mode="HTML")
        except Exception:
            await message.answer(
                final_payload,
                parse_mode="HTML",
                reply_markup=chat_mode_keyboard(),
            )
        return

    return dp
