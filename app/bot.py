from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.auth import is_allowed
from app.command_runner import (
    build_command_env,
    send_ctrl_c,
    send_pty_input,
    start_live_command,
    stop_live_command,
)
from app.config import Settings
from app.models import Session
from app.session_manager import PendingUpload, SessionManager, sanitize_terminal_text

SESSION_CONTROL_PREFIX = "sessctl"
CONTEXT_CONTROL_PREFIX = "ctxctl"
SESSION_LIST_PREFIX = "sesslist"
SESSION_PAGE_PREFIX = "sesspage"
KILL_CONFIRM_PREFIX = "killcfm"
SHELL_PICKER_PREFIX = "shellpick"
SESSION_STREAM_INTERVAL_SECONDS = 1.0
STREAM_FRAME_MAX_BODY_CHARS = 2400
STREAM_FRAME_MAX_BODY_ENTITIES = 80
RESULT_CHUNK_MAX_CHARS = 3600
RESULT_CHUNK_MAX_ENTITIES = 350
RENDER_LINE_SPLIT_MAX_CHARS = 3200
logger = logging.getLogger(__name__)

BTN_STATUS = "Status"
BTN_TAIL = "Tail"
BTN_STOP = "Stop"
BTN_SESSIONS = "Sessions"
BTN_DETACH = "Detach"
BTN_CTRL_C = "Ctrl+C"
BTN_CTRL_D = "Ctrl+D"
BTN_ENTER = "Enter"
BTN_STREAM = "Stream"
BTN_HELP = "Help"
BTN_SHELL = "Open Shell"

QUICK_ACTION_BY_TEXT: dict[str, str] = {
    BTN_STATUS: "status",
    BTN_TAIL: "tail",
    BTN_STOP: "stop",
    BTN_SESSIONS: "sessions",
    BTN_DETACH: "detach",
    BTN_CTRL_C: "ctrl_c",
    BTN_CTRL_D: "ctrl_d",
    BTN_ENTER: "enter",
    BTN_STREAM: "stream_toggle",
    BTN_HELP: "help",
    BTN_SHELL: "shell_menu",
}


@dataclass(slots=True)
class PendingKill:
    request_id: str
    telegram_user_id: int
    session_id: str


@dataclass(slots=True)
class RenderedOutputLine:
    html: str
    entity_count: int
    mode: str = "token"


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


def format_size(num_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num_bytes)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if value < 1024 or candidate == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def sanitize_uploaded_filename(file_name: str) -> str:
    # Telegram file names can contain path fragments. Keep only a plain filename
    # to guarantee uploads stay inside the user's current working directory.
    safe_name = Path(file_name).name.strip()
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError("invalid file name")
    return safe_name


async def save_telegram_file(message: Message, file_id: str, target_path: Path) -> None:
    telegram_file = await message.bot.get_file(file_id)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("wb") as out:
        await message.bot.download_file(telegram_file.file_path, destination=out)


def upload_confirm_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Overwrite", callback_data=f"upload_overwrite:{request_id}"),
                InlineKeyboardButton(text="Cancel", callback_data=f"upload_cancel:{request_id}"),
            ]
        ]
    )


def parse_upload_callback(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None
    if data in {"upload_cancel", "upload_overwrite"}:
        return data, ""
    parts = data.split(":", 1)
    if len(parts) != 2:
        return None
    action, request_id = parts
    if action not in {"upload_cancel", "upload_overwrite"}:
        return None
    if not request_id:
        return None
    return action, request_id


def persistent_control_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_STATUS),
                KeyboardButton(text=BTN_TAIL),
                KeyboardButton(text=BTN_SESSIONS),
                KeyboardButton(text=BTN_STOP),
            ],
            [
                KeyboardButton(text=BTN_DETACH),
                KeyboardButton(text=BTN_CTRL_C),
                KeyboardButton(text=BTN_CTRL_D),
                KeyboardButton(text=BTN_ENTER),
            ],
            [
                KeyboardButton(text=BTN_STREAM),
                KeyboardButton(text=BTN_SHELL),
                KeyboardButton(text=BTN_HELP),
            ],
        ],
        resize_keyboard=True,
        is_persistent=False,
    )


def session_control_keyboard_main(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Stop", callback_data=f"{SESSION_CONTROL_PREFIX}:stop:{session_id}"),
                InlineKeyboardButton(text="Ctrl+C", callback_data=f"{SESSION_CONTROL_PREFIX}:ctrl_c:{session_id}"),
                InlineKeyboardButton(text="Status", callback_data=f"{SESSION_CONTROL_PREFIX}:status:{session_id}"),
                InlineKeyboardButton(text="Detach", callback_data=f"{SESSION_CONTROL_PREFIX}:detach:{session_id}"),
            ],
            [
                InlineKeyboardButton(text="Controls", callback_data=f"{SESSION_CONTROL_PREFIX}:menu_controls:{session_id}"),
                InlineKeyboardButton(text="Output", callback_data=f"{SESSION_CONTROL_PREFIX}:menu_output:{session_id}"),
            ],
        ]
    )


def session_control_keyboard_controls(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Ctrl+D", callback_data=f"{SESSION_CONTROL_PREFIX}:ctrl_d:{session_id}"),
                InlineKeyboardButton(text="Enter", callback_data=f"{SESSION_CONTROL_PREFIX}:enter:{session_id}"),
            ],
            [
                InlineKeyboardButton(text="Back", callback_data=f"{SESSION_CONTROL_PREFIX}:menu_main:{session_id}"),
            ],
        ]
    )


def session_control_keyboard_output(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Tail", callback_data=f"{SESSION_CONTROL_PREFIX}:tail:{session_id}"),
                InlineKeyboardButton(text="Stream", callback_data=f"{SESSION_CONTROL_PREFIX}:stream_toggle:{session_id}"),
            ],
            [
                InlineKeyboardButton(text="Back", callback_data=f"{SESSION_CONTROL_PREFIX}:menu_main:{session_id}"),
            ],
        ]
    )


def stream_frame_keyboard(session_id: str, stream_enabled: bool) -> InlineKeyboardMarkup:
    stream_label = "Stream: ON" if stream_enabled else "Stream: OFF"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Status", callback_data=f"{SESSION_CONTROL_PREFIX}:status:{session_id}"),
                InlineKeyboardButton(text="Tail", callback_data=f"{SESSION_CONTROL_PREFIX}:tail:{session_id}"),
            ],
            [
                InlineKeyboardButton(text="Stop", callback_data=f"{SESSION_CONTROL_PREFIX}:stop:{session_id}"),
                InlineKeyboardButton(text=stream_label, callback_data=f"{SESSION_CONTROL_PREFIX}:stream_toggle:{session_id}"),
            ],
        ]
    )


def context_control_keyboard(*actions: str) -> InlineKeyboardMarkup:
    label_by_action = {
        "help": "Help",
        "status": "Status",
        "tail": "Tail",
    }
    buttons: list[InlineKeyboardButton] = []
    for action in actions:
        label = label_by_action.get(action)
        if not label:
            continue
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"{CONTEXT_CONTROL_PREFIX}:{action}",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else InlineKeyboardMarkup(inline_keyboard=[])


def shell_picker_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Start zsh", callback_data=f"{SHELL_PICKER_PREFIX}:zsh"),
                InlineKeyboardButton(text="Start bash", callback_data=f"{SHELL_PICKER_PREFIX}:bash"),
            ]
        ]
    )


def parse_context_control_callback(data: str | None) -> str | None:
    if not data:
        return None
    parts = data.split(":", 1)
    if len(parts) != 2:
        return None
    prefix, action = parts
    if prefix != CONTEXT_CONTROL_PREFIX:
        return None
    if action not in {"help", "status", "tail"}:
        return None
    return action


def parse_shell_picker_callback(data: str | None) -> str | None:
    if not data:
        return None
    parts = data.split(":", 1)
    if len(parts) != 2:
        return None
    prefix, shell_name = parts
    if prefix != SHELL_PICKER_PREFIX:
        return None
    if shell_name not in {"zsh", "bash"}:
        return None
    return shell_name


def sessions_list_keyboard(
    session_ids: list[str],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(text="New zsh", callback_data=f"{SHELL_PICKER_PREFIX}:zsh"),
            InlineKeyboardButton(text="New bash", callback_data=f"{SHELL_PICKER_PREFIX}:bash"),
        ]
    )
    for session_id in session_ids:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Attach {session_id[-4:]}",
                    callback_data=f"{SESSION_LIST_PREFIX}:attach:{session_id}",
                ),
                InlineKeyboardButton(
                    text=f"Tail {session_id[-4:]}",
                    callback_data=f"{SESSION_LIST_PREFIX}:tail:{session_id}",
                ),
                InlineKeyboardButton(
                    text=f"Stop {session_id[-4:]}",
                    callback_data=f"{SESSION_LIST_PREFIX}:stop:{session_id}",
                ),
                InlineKeyboardButton(
                    text=f"Kill {session_id[-4:]}",
                    callback_data=f"{SESSION_LIST_PREFIX}:kill:{session_id}",
                ),
            ]
        )
    if total_pages > 1:
        prev_enabled = page > 1
        next_enabled = page < total_pages
        rows.append(
            [
                InlineKeyboardButton(
                    text="◀ Prev" if prev_enabled else "⛔ Prev",
                    callback_data=(
                        f"{SESSION_PAGE_PREFIX}:{page - 1}" if prev_enabled else f"{SESSION_PAGE_PREFIX}:noop"
                    ),
                ),
                InlineKeyboardButton(
                    text="Next ▶" if next_enabled else "Next ⛔",
                    callback_data=(
                        f"{SESSION_PAGE_PREFIX}:{page + 1}" if next_enabled else f"{SESSION_PAGE_PREFIX}:noop"
                    ),
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_sessions_list_callback(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, action, session_id = parts
    if prefix != SESSION_LIST_PREFIX:
        return None
    if action not in {"attach", "tail", "stop", "kill"}:
        return None
    if not session_id:
        return None
    return action, session_id


def parse_sessions_page_callback(data: str | None) -> int | None:
    if not data:
        return None
    parts = data.split(":", 1)
    if len(parts) != 2:
        return None
    prefix, page_raw = parts
    if prefix != SESSION_PAGE_PREFIX:
        return None
    if page_raw == "noop":
        return 0
    try:
        page = int(page_raw)
    except ValueError:
        return None
    if page < 1:
        return None
    return page


def kill_confirm_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Confirm Kill", callback_data=f"{KILL_CONFIRM_PREFIX}:confirm:{request_id}"),
                InlineKeyboardButton(text="Cancel", callback_data=f"{KILL_CONFIRM_PREFIX}:cancel:{request_id}"),
            ]
        ]
    )


def parse_kill_confirm_callback(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, action, request_id = parts
    if prefix != KILL_CONFIRM_PREFIX:
        return None
    if action not in {"confirm", "cancel"}:
        return None
    if not request_id:
        return None
    return action, request_id


def parse_session_control_callback(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None

    parts = data.split(":", 2)
    if len(parts) != 3:
        return None

    prefix, action, session_id = parts
    if prefix != SESSION_CONTROL_PREFIX:
        return None

    if action not in {
        "stop",
        "detach",
        "ctrl_c",
        "ctrl_d",
        "enter",
        "tail",
        "status",
        "stream_toggle",
        "menu_main",
        "menu_controls",
        "menu_output",
    }:
        return None

    if not session_id:
        return None

    return action, session_id


def bot_command_menu() -> list[BotCommand]:
    return [
        BotCommand(command="help", description="Show help"),
        BotCommand(command="id", description="Show your Telegram user id"),
        BotCommand(command="run", description="Run command or cd"),
        BotCommand(command="status", description="Show active session status"),
        BotCommand(command="tail", description="Show recent output"),
        BotCommand(command="sessions", description="List your sessions"),
        BotCommand(command="attach", description="Attach to a running session"),
        BotCommand(command="detach", description="Detach current session"),
        BotCommand(command="stop", description="Stop active session"),
        BotCommand(command="kill", description="Kill a running session"),
        BotCommand(command="ctrl", description="Send Ctrl+C or Ctrl+D"),
        BotCommand(command="n", description="Send Enter/newline"),
        BotCommand(command="stream", description="Stream on/off/toggle/status"),
        BotCommand(command="live", description="Alias for /stream"),
        BotCommand(command="get", description="Download file from VPS"),
    ]


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


def is_shell_session_command(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except Exception:
        return False
    if not parts:
        return False
    executable = Path(parts[0]).name.lower()
    return executable in {"bash", "zsh", "sh", "dash", "ash", "ksh", "fish"}


def is_codex_session_command(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except Exception:
        return False
    if not parts:
        return False
    return Path(parts[0]).name.lower() == "codex"


def is_detached_session_idle_for_ttl(session: Session) -> bool:
    process = session.process
    if process is None or process.returncode is not None:
        return False
    if not is_shell_session_command(session.command):
        # For non-shell detached commands, avoid TTL-based stop while they are executing.
        return False
    pid = process.pid
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        children_raw = children_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return not bool(children_raw)


def format_session_header(session_id: str, state: str) -> str:
    safe_session_id = escape(session_id)
    safe_state = escape(state)
    return f"<b>Session</b> <code>{safe_session_id}</code> | <b>State</b> <code>{safe_state}</code>"


URL_LIKE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|www\.)\S+$", re.IGNORECASE)
PROXY_LINK_RE = re.compile(r"^(?:vless|vmess|trojan|ss|hy2|tuic)://\S+$", re.IGNORECASE)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
HEX_HASH_RE = re.compile(r"^[0-9a-f]{7,128}$", re.IGNORECASE)
IP_PORT_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}$")
DOMAIN_PORT_RE = re.compile(r"^[a-z0-9.-]+:\d{1,5}$", re.IGNORECASE)
PATH_LIKE_RE = re.compile(r"^(?:~?/|\.{1,2}/|/)\S+$")
ENV_EXPORT_RE = re.compile(r"^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=(?:.+)?$")
KV_LINE_RE = re.compile(r"^(\s*[A-Za-z0-9_.-]+)(\s*(?:=|:)\s*)(.+)$")
HISTORY_LINE_RE = re.compile(r"^(\s*\d+\*?)(\s+)(.+)$")
LOG_LINE_RE = re.compile(
    r"^(\s*(?:\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+|[A-Z][a-z]{2}\s+\d+\s+[0-9:]{8}))"
    r"(\s+)([A-Z]+|debug|info|warn|warning|error|fatal|trace)?"
    r"(\s*)(.*)$",
    re.IGNORECASE,
)
SYSTEMD_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.(?:service|socket|timer|target|mount|path|slice|scope)$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$", re.IGNORECASE)
SHELL_SNIPPET_RE = re.compile(r"(?:\|\||&&|[|;<>]|2>/dev/null|\$\(|`)")
BLOCK_HINT_RE = re.compile(r"^\s*(?:[{[]|[-*]\s|\w+:\s|\w+=)")
OUTPUT_LINE_ENTITY_SOFT_LIMIT = 18


def normalize_output_token(token: str) -> str:
    if len(token) < 2:
        return token

    normalized = token

    # Trim a trailing comma/semicolon when it is likely punctuation rather than payload.
    if normalized[-1] in {",", ";"} and len(normalized) > 2:
        candidate = normalized[:-1]
        if candidate[-1].isalnum() or candidate[-1] in {'"', "'", "`", ")", "]", "}"}:
            normalized = candidate

    wrappers = {
        '"': '"',
        "'": "'",
        "`": "`",
        "(": ")",
        "[": "]",
        "{": "}",
    }
    left = normalized[0]
    right = wrappers.get(left)
    if right and normalized[-1] == right and len(normalized) > 2:
        inner = normalized[1:-1]
        if inner:
            normalized = inner

    return normalized


def normalize_output_parts(parts: list[str]) -> list[str]:
    normalized_parts: list[str] = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if part and part[0] in {'"', "'", "`"}:
            quote = part[0]
            if len(part) >= 2 and part.endswith(quote):
                normalized_parts.append(normalize_output_token(part))
                i += 1
                continue

            end = i + 1
            while end < len(parts):
                if parts[end].endswith(quote):
                    first = part[1:]
                    middle = parts[i + 1 : end]
                    last = parts[end][:-1]
                    joined_parts = [first, *middle, last]
                    joined = " ".join(item for item in joined_parts if item)
                    if joined:
                        normalized_parts.append(normalize_output_token(joined))
                    i = end + 1
                    break
                end += 1
            else:
                normalized_parts.append(normalize_output_token(part))
                i += 1
            continue

        normalized_parts.append(normalize_output_token(part))
        i += 1

    return normalized_parts


def render_output_lines(output: str) -> str:
    return "\n".join(fragment.html for fragment in render_output_fragments(output))


def render_output_fragments(output: str) -> list[RenderedOutputLine]:
    lines = (output or "[no output]").splitlines() or ["[no output]"]
    rendered_lines: list[RenderedOutputLine] = []
    for line in lines:
        for fragment in split_output_line_for_rendering(line):
            rendered_lines.append(render_output_line(fragment))
    return rendered_lines


def count_rendered_output_entities(output: str) -> int:
    return sum(fragment.entity_count for fragment in render_output_fragments(output))


def _split_history_line(line: str) -> tuple[str, str] | None:
    match = HISTORY_LINE_RE.match(line)
    if not match:
        return None
    index, _, command = match.groups()
    if not command:
        return None
    return index, command


def _looks_like_url(value: str) -> bool:
    return bool(URL_LIKE_RE.match(value))


def _looks_like_proxy_link(value: str) -> bool:
    return bool(PROXY_LINK_RE.match(value))


def _looks_like_identifier(value: str) -> bool:
    return bool(
        UUID_RE.match(value)
        or HEX_HASH_RE.match(value)
        or IP_PORT_RE.match(value)
        or DOMAIN_PORT_RE.match(value)
        or SYSTEMD_UNIT_RE.match(value)
        or CONTAINER_ID_RE.match(value)
    )


def _looks_like_path(value: str) -> bool:
    return bool(PATH_LIKE_RE.match(value))


def _is_shell_snippet_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if ENV_EXPORT_RE.match(stripped):
        return True
    if _looks_like_proxy_link(stripped):
        return True
    return bool(SHELL_SNIPPET_RE.search(stripped))


def _is_block_like_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) >= 90 and (stripped.startswith("{") or stripped.startswith("[")):
        return True
    if len(stripped) >= 120 and BLOCK_HINT_RE.match(stripped):
        return True
    if stripped.startswith(("-----BEGIN ", "-----END ")):
        return True
    return False


def _split_preserving_spaces(text: str) -> list[str]:
    return re.findall(r"\s+|[^\s]+", text)


def _build_code_join(parts: list[str]) -> RenderedOutputLine:
    html_parts: list[str] = []
    entity_count = 0
    for part in parts:
        if not part:
            continue
        if part.isspace():
            html_parts.append(part)
            continue
        html_parts.append(f"<code>{escape(part)}</code>")
        entity_count += 1
    if not html_parts:
        return RenderedOutputLine("<code> </code>", 1, mode="token")
    return RenderedOutputLine("".join(html_parts), max(1, entity_count), mode="token")


def _render_line_mode(line: str) -> RenderedOutputLine:
    if not line:
        return RenderedOutputLine("<code> </code>", 1, mode="line")
    return RenderedOutputLine(f"<code>{escape(line)}</code>", 1, mode="line")


def _render_history_line(line: str) -> RenderedOutputLine | None:
    history_line = _split_history_line(line)
    if history_line is None:
        return None
    index, command = history_line
    return RenderedOutputLine(
        f"<code>{escape(index)}</code> <code>{escape(command)}</code>",
        2,
        mode="history",
    )


def _render_key_value_line(line: str) -> RenderedOutputLine | None:
    match = KV_LINE_RE.match(line)
    if not match:
        return None
    key, separator, value = match.groups()
    if not value.strip():
        return None
    return RenderedOutputLine(
        f"<code>{escape(key.strip())}</code>{escape(separator)}<code>{escape(value.strip())}</code>",
        2,
        mode="kv",
    )


def _render_column_line(line: str) -> RenderedOutputLine | None:
    if "\t" not in line and not re.search(r"\S(?: {2,}|\t)\S", line):
        return None
    pieces = re.split(r"(\t+| {2,})", line)
    if len(pieces) <= 1:
        return None
    rendered = _build_code_join(pieces)
    rendered.mode = "columns"
    return rendered


def _render_standalone_value_line(line: str) -> RenderedOutputLine | None:
    stripped = line.strip()
    if not stripped:
        return None
    if _looks_like_url(stripped) or _looks_like_proxy_link(stripped) or _looks_like_path(stripped):
        return RenderedOutputLine(f"<code>{escape(stripped)}</code>", 1, mode="standalone")
    if _looks_like_identifier(stripped):
        return RenderedOutputLine(f"<code>{escape(stripped)}</code>", 1, mode="standalone")
    return None


def _render_log_like_line(line: str) -> RenderedOutputLine | None:
    match = LOG_LINE_RE.match(line)
    if not match:
        return None
    timestamp, spacing, level, level_spacing, message = match.groups()
    if not message:
        return None
    html = f"<code>{escape(timestamp.strip())}</code>{escape(spacing)}"
    entity_count = 1
    if level:
        html += f"<code>{escape(level)}</code>{escape(level_spacing or ' ')}"
        entity_count += 1
    html += f"<code>{escape(message)}</code>"
    return RenderedOutputLine(html, entity_count + 1, mode="log")


def _merge_plain_words(tokens: list[str]) -> list[str]:
    merged: list[str] = []
    buffer: list[str] = []
    for token in tokens:
        if (
            token
            and token.replace(".", "", 1).replace("-", "", 1).isalnum()
            and not _looks_like_url(token)
            and not _looks_like_path(token)
            and not _looks_like_identifier(token)
        ):
            buffer.append(token)
            continue
        if buffer:
            merged.append(" ".join(buffer))
            buffer = []
        merged.append(token)
    if buffer:
        merged.append(" ".join(buffer))
    return merged


def _render_token_line(line: str) -> RenderedOutputLine:
    if not line:
        return RenderedOutputLine("<code> </code>", 1, mode="token")
    raw_parts = [part for part in re.split(r"\s+", line.strip()) if part]
    if not raw_parts:
        return RenderedOutputLine("<code> </code>", 1, mode="token")
    normalized_parts = normalize_output_parts(raw_parts)
    merged_parts = _merge_plain_words(normalized_parts)
    return _build_code_join(_split_preserving_spaces(" ".join(merged_parts)))


def _classify_output_line(line: str) -> str:
    if not line:
        return "empty"
    if _split_history_line(line) is not None:
        return "history"
    if KV_LINE_RE.match(line):
        return "kv"
    if _render_column_line(line) is not None:
        return "columns"
    if LOG_LINE_RE.match(line):
        return "log"
    stripped = line.strip()
    if stripped and _render_standalone_value_line(stripped) is not None:
        return "standalone"
    if _is_shell_snippet_line(line):
        return "shell"
    if _is_block_like_line(line):
        return "block"
    return "token"


def render_output_line(line: str) -> RenderedOutputLine:
    if not line:
        return RenderedOutputLine("<code> </code>", 1, mode="empty")

    line_kind = _classify_output_line(line)
    if line_kind == "history":
        rendered = _render_history_line(line)
        if rendered is not None:
            return rendered
    if line_kind == "kv":
        rendered = _render_key_value_line(line)
        if rendered is not None:
            return rendered
    if line_kind == "columns":
        rendered = _render_column_line(line)
        if rendered is not None:
            return rendered
    if line_kind == "log":
        rendered = _render_log_like_line(line)
        if rendered is not None:
            return rendered
    if line_kind == "standalone":
        rendered = _render_standalone_value_line(line)
        if rendered is not None:
            return rendered
    if line_kind in {"shell", "block"}:
        return _render_line_mode(line)

    rendered = _render_token_line(line)
    if rendered.entity_count > OUTPUT_LINE_ENTITY_SOFT_LIMIT:
        return _render_line_mode(line)
    return rendered


def split_output_line_for_rendering(line: str, max_chars: int = RENDER_LINE_SPLIT_MAX_CHARS) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    remaining = line
    chunks: list[str] = []
    while len(remaining) > max_chars:
        split_at = remaining.rfind(" ", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind("/", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind(",", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:max_chars]
            split_at = len(chunk)
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


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
        f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
        f"<b>Quick start:</b> choose a shell below.",
        parse_mode="HTML",
        reply_markup=shell_picker_keyboard(),
    )


async def answer_active_session_exists(message: Message, session_id: str) -> None:
    await message.answer(
        f"<b>You already have an active session</b>\n"
        f"<b>Session:</b> <code>{escape(session_id)}</code>\n"
        f"<b>How to continue:</b> send plain text (example: <code>ls</code>)\n"
        f"<b>Detach first:</b> <code>/detach</code>\n"
        f"<b>Controls:</b> <code>/n</code>, <code>/ctrl c</code>, <code>/ctrl d</code>, <code>/tail</code>, "
        f"<code>/status</code>, <code>/stop</code>\n"
        f"<b>Session list:</b> <code>/sessions</code>\n"
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
    codex_hint = ""
    try:
        parts = shlex.split(command)
        if parts and Path(parts[0]).name.lower() == "codex":
            codex_hint = (
                "\n<b>Codex hint:</b> use <code>!/...</code> for codex slash commands "
                "(example: <code>!/init</code>)"
            )
    except Exception:
        pass
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
        f"{codex_hint}"
        f"<b>Tip:</b> use <code>/tail</code>, <code>/status</code>, <code>/stop</code>, "
        f"<code>/ctrl c</code>, <code>/ctrl d</code>, <code>/n</code>, <code>/stream status</code>"
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


def build_session_result_chunks(
    session_id: str,
    state: str,
    exit_code: int | None,
    cwd: str,
    output: str,
    max_chars: int = RESULT_CHUNK_MAX_CHARS,
) -> list[str]:
    safe_exit = escape(str(exit_code))
    safe_cwd = escape(cwd)
    header = (
        f"{format_session_header(session_id, state)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>Exit code:</b> <code>{safe_exit}</code>\n"
        f"<b>Current dir:</b> <code>{safe_cwd}</code>\n"
        f"<b>Output</b>\n"
    )
    rendered_lines = render_output_fragments(output)

    chunks: list[str] = []
    current = header
    current_entities = 4
    for rendered in rendered_lines:
        piece = f"{rendered.html}\n"
        if (
            len(current) + len(piece) <= max_chars
            and current_entities + rendered.entity_count <= RESULT_CHUNK_MAX_ENTITIES
        ):
            current += piece
            current_entities += rendered.entity_count
            continue
        if current.strip():
            chunks.append(current.rstrip())
        # Continuation messages contain only output body for readability.
        current = piece
        current_entities = rendered.entity_count
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def format_local_timestamp(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_runtime(started_at: datetime, ended_at: datetime | None, now: datetime) -> str:
    runtime_end = ended_at or now
    runtime = runtime_end - started_at
    if runtime.total_seconds() < 0:
        runtime = timedelta(0)
    return str(runtime).split(".")[0]


def format_session_status_message(
    session: Session,
    current_dir: Path,
    stream_enabled: bool,
    now: datetime,
) -> str:
    runtime = format_runtime(session.started_at, session.ended_at, now)
    pid = session.process.pid if session.process else "n/a"
    stream_mode = "on" if stream_enabled else "off"
    return (
        f"{format_session_header(session.session_id, session.state)}\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>Command:</b> <code>{escape(session.command)}</code>\n"
        f"<b>Attached:</b> <code>{'yes' if session.is_attached else 'no'}</code>\n"
        f"<b>PID:</b> <code>{escape(str(pid))}</code>\n"
        f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
        f"<b>Started at:</b> <code>{escape(format_local_timestamp(session.started_at))}</code>\n"
        f"<b>Ended at:</b> <code>{escape(format_local_timestamp(session.ended_at))}</code>\n"
        f"<b>Runtime:</b> <code>{escape(runtime)}</code>\n"
        f"<b>Stream mode:</b> <code>{stream_mode}</code>\n"
        f"<b>Exit code:</b> <code>{escape(str(session.exit_code))}</code>"
    )


def format_help_message(current_dir: Path) -> str:
    safe_dir = escape(str(current_dir))
    return (
        "<b>راهنمای بات | Bot Help</b>\n\n"
        "<b>فارسی (با مثال)</b>\n"
        "• <code>/help</code>: نمایش همین راهنما\n"
        "  مثال: <code>/help</code>\n"
        "• <code>/id</code>: نمایش شناسه تلگرام شما\n"
        "  مثال: <code>/id</code>\n"
        "• <code>/run &lt;command&gt;</code>: اجرای دستور روی سرور\n"
        "  مثال: <code>/run ls -la</code>\n"
        "• <code>/run cd &lt;path&gt;</code>: تغییر مسیر کاری شما\n"
        "  مثال: <code>/run cd /home/pooria</code>\n"
        "• <code>/run bash</code> یا <code>/run zsh</code>: ورود به شل تعاملی\n"
        "  کاربرد: اجرای چند دستور پشت‌سرهم در یک سشن\n"
        "  مثال: <code>/run zsh</code> سپس پیام <code>whoami</code>\n"
        "  خروج: <code>/ctrl d</code> یا <code>/stop</code>\n"
        "• <code>/sessions</code>: نمایش لیست سشن‌ها\n"
        "  مثال: <code>/sessions</code>\n"
        "• <code>/attach &lt;session_id&gt;</code>: اتصال دوباره به سشن درحال اجرا\n"
        "  مثال: <code>/attach sess_ab12cd34</code> یا فقط <code>/attach</code>\n"
        "• <code>/detach</code>: جدا شدن از سشن فعلی بدون stop\n"
        "  مثال: <code>/detach</code>\n"
        "• <code>/status</code>: وضعیت سشن فعال (PID, runtime, ...)\n"
        "  مثال: <code>/status</code> یا <code>/status sess_ab12cd34</code>\n"
        "• <code>/tail</code>: نمایش خروجی اخیر (یا آخرین سشن)\n"
        "  مثال: <code>/tail</code> یا <code>/tail sess_ab12cd34</code>\n"
        "• <code>/stop</code>: توقف سشن فعال\n"
        "  مثال: <code>/stop</code> یا <code>/stop sess_ab12cd34</code>\n"
        "• <code>/kill</code>: پایان فوری سشن فعال/هدف\n"
        "  مثال: <code>/kill sess_ab12cd34</code>\n"
        "• <code>/ctrl c</code>: ارسال Ctrl+C به پردازش فعال\n"
        "  مثال: <code>/ctrl c</code>\n"
        "• <code>/ctrl d</code>: ارسال Ctrl+D (EOF) به PTY\n"
        "  مثال: <code>/ctrl d</code>\n"
        "• <code>/n</code>: ارسال Enter به PTY\n"
        "  مثال: <code>/n</code>\n"
        "• <code>/stream on|off|toggle|status</code>: کنترل پخش زنده خروجی\n"
        "  مثال‌ها: <code>/stream on</code> | <code>/stream toggle</code>\n"
        "  نکته: در حالت on قبل از هر ورودی جدید بافر پاک می‌شود.\n"
        "  نکته: در حالت off با <code>/tail</code> بافر نمایش‌داده‌شده مصرف می‌شود.\n"
        "• <code>/live ...</code>: نام جایگزین برای stream\n"
        "  مثال: <code>/live status</code>\n"
        "• <code>/get &lt;path&gt;</code>: دریافت فایل از سرور\n"
        "  مثال: <code>/get logs/app.log</code>\n"
        "• ارسال فایل: آپلود فایل در مسیر کاری فعلی شما\n"
        "  مثال: فایل را مستقیم در چت ارسال کنید\n"
        "• متن ساده در حالت سشن فعال: به stdin همان سشن ارسال می‌شود\n"
        "  مثال: بعد از <code>/run bash</code> پیام <code>pwd</code> بفرستید\n"
        "• نکته Codex: برای اسلش‌های codex از <code>!/...</code> استفاده کنید\n"
        "  مثال: <code>!/init</code> یا <code>!/status</code>\n\n"
        "<b>English (with examples)</b>\n"
        "• <code>/help</code>: show this help\n"
        "  Example: <code>/help</code>\n"
        "• <code>/id</code>: show your Telegram user id\n"
        "  Example: <code>/id</code>\n"
        "• <code>/run &lt;command&gt;</code>: run a command on VPS\n"
        "  Example: <code>/run df -h</code>\n"
        "• <code>/run cd &lt;path&gt;</code>: change your working directory\n"
        "  Example: <code>/run cd /var/log</code>\n"
        "• <code>/run bash</code> or <code>/run zsh</code>: start an interactive shell session\n"
        "  Use case: keep one live shell and send multiple commands\n"
        "  Example: <code>/run bash</code>, then send plain text <code>pwd</code>\n"
        "  Exit: <code>/ctrl d</code> or <code>/stop</code>\n"
        "• <code>/sessions</code>: list your sessions\n"
        "  Example: <code>/sessions</code>\n"
        "• <code>/attach &lt;session_id&gt;</code>: attach to a running session\n"
        "  Example: <code>/attach sess_ab12cd34</code> or just <code>/attach</code>\n"
        "• <code>/detach</code>: detach from current session without stopping it\n"
        "  Example: <code>/detach</code>\n"
        "• <code>/status</code>: show active session state\n"
        "  Example: <code>/status</code> or <code>/status sess_ab12cd34</code>\n"
        "• <code>/tail</code>: show recent output (or latest session)\n"
        "  Example: <code>/tail</code> or <code>/tail sess_ab12cd34</code>\n"
        "• <code>/stop</code>: stop active session\n"
        "  Example: <code>/stop</code> or <code>/stop sess_ab12cd34</code>\n"
        "• <code>/kill</code>: force-kill active/target session\n"
        "  Example: <code>/kill sess_ab12cd34</code>\n"
        "• <code>/ctrl c</code>: send Ctrl+C\n"
        "  Example: <code>/ctrl c</code>\n"
        "• <code>/ctrl d</code>: send Ctrl+D (EOF)\n"
        "  Example: <code>/ctrl d</code>\n"
        "• <code>/n</code>: send Enter/newline\n"
        "  Example: <code>/n</code>\n"
        "• <code>/stream on|off|toggle|status</code>: live output control\n"
        "  Examples: <code>/stream off</code> | <code>/stream status</code>\n"
        "  Note: in on mode, buffer is cleared before each new input.\n"
        "  Note: in off mode, <code>/tail</code> consumes the shown buffer.\n"
        "• <code>/live ...</code>: alias for <code>/stream ...</code>\n"
        "  Example: <code>/live on</code>\n"
        "• <code>/get &lt;path&gt;</code>: download file from VPS\n"
        "  Example: <code>/get /etc/hosts</code>\n"
        "• File upload: send a file directly in chat\n"
        "  Example: upload <code>deploy.sh</code> to current dir\n"
        "• Plain text while a session is active: forwarded to session stdin\n"
        "  Example: run <code>/run zsh</code>, then send <code>ls</code>\n"
        "• Codex note: use <code>!/...</code> for codex slash commands\n"
        "  Example: <code>!/init</code> or <code>!/status</code>\n\n"
        f"<b>Current dir:</b> <code>{safe_dir}</code>"
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
        f"<b>Attached:</b> <code>{'yes' if session.is_attached else 'no'}</code>\n"
        f"<b>Tail</b>\n"
        f"{render_output_lines(text)}"
    )


def format_sessions_list_message(
    sessions: list[Session],
    max_running: int,
    running_count: int,
    now: datetime,
) -> str:
    if not sessions:
        return "<b>No sessions found</b>"

    lines: list[str] = [
        "<b>Your Sessions</b>",
        f"<b>Running:</b> <code>{running_count}/{max_running}</code>",
        "━━━━━━━━━━━━━━",
    ]
    for session in sessions:
        runtime = format_runtime(session.started_at, session.ended_at, now)
        attached = "yes" if session.is_attached else "no"
        lines.append(
            f"<b>{escape(session.session_id)}</b> | "
            f"<code>{escape(session.state)}</code> | "
            f"attached=<code>{attached}</code> | "
            f"runtime=<code>{escape(runtime)}</code> | "
            f"started=<code>{escape(format_local_timestamp(session.started_at))}</code>\n"
            f"<code>{escape(session.command)}</code>"
        )
    return "\n".join(lines)


def reset_stream_frame_state(session: Session) -> None:
    session.stream_pending_text = ""
    session.stream_live_message_id = None
    session.stream_frame_index = 0
    session.stream_frame_body = ""
    session.stream_current_line_start = 0
    session.stream_last_sent_text = ""


def reset_stream_state_for_new_input(session: Session, session_manager: SessionManager) -> None:
    # Stream-on policy: every new interactive input starts from a fresh buffer/frame.
    session_manager.clear_output_buffer(session)
    reset_stream_frame_state(session)
    session_manager.save_session(session)


def build_stream_frame_text(session: Session, now: datetime) -> str:
    frame_no = max(1, session.stream_frame_index)
    body = session.stream_frame_body if session.stream_frame_body else "[no output yet]"
    if frame_no == 1:
        return (
            f"{format_session_header(session.session_id, session.state)}\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>Live Frame:</b> <code>{frame_no}</code>\n"
            f"<b>Updated:</b> <code>{escape(format_local_timestamp(now))}</code>\n"
            f"{render_output_lines(body)}"
        )
    return render_output_lines(body)


def _split_stream_body_for_entity_budget(body: str) -> tuple[str, str] | None:
    if not body:
        return None
    lines = body.splitlines(keepends=True)
    if len(lines) < 2:
        return None

    head_lines: list[str] = []
    head_entities = 0
    for line in lines:
        candidate = line.rstrip("\n")
        fragment_entities = count_rendered_output_entities(candidate)
        if head_lines and head_entities + fragment_entities > STREAM_FRAME_MAX_BODY_ENTITIES:
            break
        head_lines.append(line)
        head_entities += fragment_entities

    if len(head_lines) >= len(lines):
        return None

    split_at = sum(len(line) for line in head_lines)
    head = body[:split_at].rstrip("\n")
    tail = body[split_at:].lstrip("\n")
    if not head or not tail:
        return None
    return head, tail


async def upsert_stream_frame_message(
    session: Session,
    bot: Bot,
    now: datetime,
    stream_enabled: bool,
) -> None:
    text = build_stream_frame_text(session, now=now)
    keyboard = stream_frame_keyboard(session.session_id, stream_enabled=stream_enabled)
    if text == session.stream_last_sent_text:
        return
    if session.stream_live_message_id is None:
        if session.stream_frame_index < 1:
            session.stream_frame_index = 1
        sent = await bot.send_message(
            session.chat_id,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        session.stream_live_message_id = sent.message_id
        session.stream_last_sent_text = text
        return

    old_message_id = session.stream_live_message_id

    try:
        await bot.edit_message_text(
            chat_id=session.chat_id,
            message_id=old_message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        session.stream_last_sent_text = text
    except TelegramBadRequest as exc:
        lowered = str(exc).lower()
        if "message is not modified" in lowered:
            session.stream_last_sent_text = text
            return
        with contextlib.suppress(Exception):
            await bot.edit_message_reply_markup(
                chat_id=session.chat_id,
                message_id=old_message_id,
                reply_markup=None,
            )
        sent = await bot.send_message(
            session.chat_id,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        session.stream_live_message_id = sent.message_id
        session.stream_last_sent_text = text
    except TelegramRetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after))
        try:
            await bot.edit_message_text(
                chat_id=session.chat_id,
                message_id=old_message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            session.stream_last_sent_text = text
        except Exception:
            with contextlib.suppress(Exception):
                await bot.edit_message_reply_markup(
                    chat_id=session.chat_id,
                    message_id=old_message_id,
                    reply_markup=None,
                )
            sent = await bot.send_message(
                session.chat_id,
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            session.stream_live_message_id = sent.message_id
            session.stream_last_sent_text = text
    except TelegramForbiddenError:
        return
    except Exception:
        # Fallback for stale/deleted/unchanged messages: emit a fresh frame message.
        with contextlib.suppress(Exception):
            await bot.edit_message_reply_markup(
                chat_id=session.chat_id,
                message_id=old_message_id,
                reply_markup=None,
            )
        sent = await bot.send_message(
            session.chat_id,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        session.stream_live_message_id = sent.message_id
        session.stream_last_sent_text = text


async def stream_rollover_frame(
    session: Session,
    bot: Bot,
    now: datetime,
    stream_enabled: bool,
) -> None:
    await upsert_stream_frame_message(
        session,
        bot,
        now=now,
        stream_enabled=stream_enabled,
    )
    if session.stream_live_message_id is not None:
        with contextlib.suppress(Exception):
            await bot.edit_message_reply_markup(
                chat_id=session.chat_id,
                message_id=session.stream_live_message_id,
                reply_markup=None,
            )
    session.stream_live_message_id = None
    session.stream_frame_body = ""
    session.stream_current_line_start = 0
    session.stream_last_sent_text = ""
    session.stream_frame_index = max(1, session.stream_frame_index) + 1


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
            text_chunk = chunk.decode(errors="replace")
            session_manager.append_output_text(
                session,
                text_chunk,
                max_tail_lines,
            )
            if session_manager.is_stream_enabled(session.telegram_user_id):
                normalized = sanitize_terminal_text(text_chunk).replace("\r\n", "\n")
                session.stream_pending_text += normalized
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(master_fd)
        with contextlib.suppress(OSError):
            os.close(master_fd)
        session.pty_master_fd = None


async def stream_session_output(
    session: Session,
    session_manager: SessionManager,
    bot: Bot,
) -> None:
    while True:
        await asyncio.sleep(SESSION_STREAM_INTERVAL_SECONDS)

        process = session.process
        if process is None:
            break

        if not session_manager.is_stream_enabled(session.telegram_user_id):
            continue

        now = session_manager.now()
        stream_enabled = session_manager.is_stream_enabled(session.telegram_user_id)
        pending = session.stream_pending_text
        if not pending:
            continue
        session.stream_pending_text = ""

        for char in pending:
            if char == "\r":
                session.stream_frame_body = session.stream_frame_body[: session.stream_current_line_start]
                continue

            if len(session.stream_frame_body) + 1 > STREAM_FRAME_MAX_BODY_CHARS:
                # Prefer rolling at line boundaries to keep output readable.
                if 0 < session.stream_current_line_start < len(session.stream_frame_body):
                    carry = session.stream_frame_body[session.stream_current_line_start :]
                    session.stream_frame_body = session.stream_frame_body[: session.stream_current_line_start]
                    await stream_rollover_frame(
                        session,
                        bot,
                        now=now,
                        stream_enabled=stream_enabled,
                    )
                    session.stream_frame_body = carry
                    session.stream_current_line_start = 0
                else:
                    # Fallback for very long single-line output without newline.
                    await stream_rollover_frame(
                        session,
                        bot,
                        now=now,
                        stream_enabled=stream_enabled,
                    )

            session.stream_frame_body += char
            if char == "\n":
                session.stream_current_line_start = len(session.stream_frame_body)
                split_body = _split_stream_body_for_entity_budget(session.stream_frame_body)
                if split_body is not None:
                    head, tail = split_body
                    session.stream_frame_body = head
                    session.stream_current_line_start = len(session.stream_frame_body)
                    await stream_rollover_frame(
                        session,
                        bot,
                        now=now,
                        stream_enabled=stream_enabled,
                    )
                    session.stream_frame_body = tail
                    session.stream_current_line_start = len(session.stream_frame_body)

        with contextlib.suppress(Exception):
            await upsert_stream_frame_message(
                session,
                bot,
                now=now,
                stream_enabled=stream_enabled,
            )
        session_manager.save_session(session)


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
        session.ended_at = session_manager.now()
        logger.info(
            "Session exited: user_id=%s session_id=%s state=%s exit_code=%s command=%r",
            session.telegram_user_id,
            session.session_id,
            session.state,
            session.exit_code,
            session.command,
        )
    except Exception as exc:
        session.state = "failed"
        session.ended_at = session_manager.now()
        session_manager.append_output_text(
            session,
            f"ERROR: {exc!r}\n",
            settings.max_tail_lines,
        )
        logger.exception(
            "Session wait failed: user_id=%s session_id=%s command=%r",
            session.telegram_user_id,
            session.session_id,
            session.command,
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
    chunks = build_session_result_chunks(
        session_id=session.session_id,
        state=session.state,
        exit_code=session.exit_code,
        cwd=str(current_dir),
        output=tail_text,
    )
    with contextlib.suppress(Exception):
        for chunk in chunks:
            await bot.send_message(session.chat_id, chunk, parse_mode="HTML")


def build_dispatcher(settings: Settings, session_manager: SessionManager) -> Dispatcher:
    dp = Dispatcher()
    pending_kill_by_user: dict[int, PendingKill] = {}

    def parse_optional_session_id(text: str) -> str | None:
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            return None
        candidate = parts[1].strip()
        return candidate or None

    async def do_start_shell(message: Message, user_id: int, shell_name: str) -> None:
        shell_name = shell_name.strip().lower()
        if shell_name not in {"zsh", "bash"}:
            await message.answer("Unsupported shell.")
            return

        current_dir = session_manager.get_current_workdir(user_id)
        active = session_manager.get_active_session_for_user(user_id)
        if active:
            await answer_active_session_exists(message, active.session_id)
            return

        running_count = session_manager.count_running_sessions_for_user(user_id)
        if running_count >= settings.max_running_sessions_per_user:
            await message.answer(
                f"<b>Run blocked</b>\n"
                f"<b>Reason:</b> <code>running session limit reached</code>\n"
                f"<b>Running:</b> <code>{running_count}/{settings.max_running_sessions_per_user}</code>\n"
                f"<b>Use:</b> <code>/sessions</code> then stop/attach/detach as needed.",
                parse_mode="HTML",
            )
            return

        session = session_manager.create_session(
            telegram_user_id=user_id,
            chat_id=message.chat.id,
            command=shell_name,
            max_session_history_per_user=settings.max_session_history_per_user,
        )
        logger.info(
            "Starting quick shell session: user_id=%s session_id=%s command=%r cwd=%s",
            user_id,
            session.session_id,
            shell_name,
            current_dir,
        )
        try:
            live = await start_live_command(
                command=shell_name,
                shell=settings.default_shell,
                cwd=current_dir,
            )
        except Exception as exc:
            session.state = "failed"
            session.ended_at = session_manager.now()
            session_manager.append_output_text(
                session,
                f"ERROR: {exc!r}\n",
                settings.max_tail_lines,
            )
            session_manager.finish_session(session)
            logger.exception(
                "Failed to start quick shell session: user_id=%s session_id=%s command=%r cwd=%s",
                user_id,
                session.session_id,
                shell_name,
                current_dir,
            )
            tail_lines = session_manager.get_tail_snapshot(session, settings.max_tail_lines)
            tail_text = "\n".join(tail_lines) if tail_lines else "[no output]"
            chunks = build_session_result_chunks(
                session_id=session.session_id,
                state=session.state,
                exit_code=session.exit_code,
                cwd=str(current_dir),
                output=tail_text,
            )
            for chunk in chunks:
                await message.answer(chunk, parse_mode="HTML")
            return

        session.state = "running"
        session.process = live.process
        session.pty_master_fd = live.pty_master_fd
        reset_stream_frame_state(session)
        session.is_attached = True
        session_manager.save_session(session)
        session.reader_task = asyncio.create_task(
            read_session_output(session, session_manager, settings.max_tail_lines)
        )
        session.streamer_task = asyncio.create_task(
            stream_session_output(session, session_manager, message.bot)
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
                command=shell_name,
                stream_enabled=session_manager.is_stream_enabled(user_id),
            ),
            parse_mode="HTML",
            reply_markup=session_control_keyboard_main(session.session_id),
        )

    async def do_help(message: Message, user_id: int) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        await message.answer(
            format_help_message(current_dir),
            parse_mode="HTML",
            reply_markup=persistent_control_keyboard(),
        )

    def resolve_session_token(user_id: int, token: str | None) -> Session | None:
        if not token:
            return None
        return session_manager.resolve_session_token_for_user(user_id, token)

    async def do_status(message: Message, user_id: int, session_id: str | None = None) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        if session_id:
            session = resolve_session_token(user_id, session_id)
        else:
            session = session_manager.get_active_session_for_user(user_id)
        if not session:
            if session_id:
                await message.answer(
                    f"<b>Session not found</b>\n"
                    f"<b>Session:</b> <code>{escape(session_id)}</code>",
                    parse_mode="HTML",
                )
                return
            await answer_no_active_session(message, current_dir)
            return

        stream_enabled = session_manager.is_stream_enabled(user_id)
        await message.answer(
            format_session_status_message(
                session,
                current_dir,
                stream_enabled,
                now=session_manager.now(),
            ),
            parse_mode="HTML",
        )

    async def do_tail(message: Message, user_id: int, session_id: str | None = None) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        if session_id:
            session = resolve_session_token(user_id, session_id)
        else:
            session = session_manager.get_active_session_for_user(user_id)
        if not session and not session_id:
            session = session_manager.get_latest_session_for_user(user_id)
        if not session:
            if session_id:
                await message.answer(
                    f"<b>Session not found</b>\n"
                    f"<b>Session:</b> <code>{escape(session_id)}</code>",
                    parse_mode="HTML",
                )
                return
            await answer_no_active_session(message, current_dir)
            return

        tail_lines = session_manager.get_tail_snapshot(session, settings.max_tail_lines)
        await message.answer(format_tail_message(session, tail_lines), parse_mode="HTML")
        if not session_manager.is_stream_enabled(user_id):
            session_manager.clear_output_buffer(session)

    async def do_sessions(message: Message, user_id: int, page: int = 1) -> None:
        sessions = session_manager.list_sessions_for_user(user_id, limit=None)
        running_count = session_manager.count_running_sessions_for_user(user_id)
        if not sessions:
            await message.answer("<b>No sessions found</b>", parse_mode="HTML")
            return
        page_size = settings.sessions_page_size
        total_pages = max(1, (len(sessions) + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        page_sessions = sessions[start : start + page_size]
        session_ids = [s.session_id for s in page_sessions if s.state in {"starting", "running"}]
        body = format_sessions_list_message(
            sessions=page_sessions,
            max_running=settings.max_running_sessions_per_user,
            running_count=running_count,
            now=session_manager.now(),
        )
        await message.answer(
            f"{body}\n<b>Page:</b> <code>{page}/{total_pages}</code>",
            parse_mode="HTML",
            reply_markup=(
                sessions_list_keyboard(session_ids, page=page, total_pages=total_pages)
                if session_ids or total_pages > 1
                else None
            ),
        )

    async def do_sessions_edit(message: Message, user_id: int, page: int = 1) -> None:
        sessions = session_manager.list_sessions_for_user(user_id, limit=None)
        running_count = session_manager.count_running_sessions_for_user(user_id)
        if not sessions:
            with contextlib.suppress(Exception):
                await message.edit_text("<b>No sessions found</b>", parse_mode="HTML")
            return
        page_size = settings.sessions_page_size
        total_pages = max(1, (len(sessions) + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        page_sessions = sessions[start : start + page_size]
        session_ids = [s.session_id for s in page_sessions if s.state in {"starting", "running"}]
        body = format_sessions_list_message(
            sessions=page_sessions,
            max_running=settings.max_running_sessions_per_user,
            running_count=running_count,
            now=session_manager.now(),
        )
        text = f"{body}\n<b>Page:</b> <code>{page}/{total_pages}</code>"
        markup = (
            sessions_list_keyboard(session_ids, page=page, total_pages=total_pages)
            if session_ids or total_pages > 1
            else None
        )
        try:
            await message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                await message.answer(
                    text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )

    async def do_detach(message: Message, user_id: int) -> None:
        session = session_manager.detach_active_session_for_user(user_id)
        if not session:
            current_dir = session_manager.get_current_workdir(user_id)
            await answer_no_active_session(message, current_dir)
            return
        await message.answer(
            f"<b>Detached</b>\n"
            f"<b>Session:</b> <code>{escape(session.session_id)}</code>\n"
            f"<b>Use:</b> <code>/attach {escape(session.session_id)}</code> to re-attach later.",
            parse_mode="HTML",
        )

    async def do_attach(message: Message, user_id: int, session_id: str | None = None) -> None:
        if not session_id:
            candidates = [
                s
                for s in session_manager.list_sessions_for_user(user_id, limit=None)
                if s.state in {"starting", "running"} and s.process is not None and not s.is_attached
            ]
            if len(candidates) == 1:
                session_id = candidates[0].session_id
            elif not candidates:
                await message.answer(
                    "<b>Attach failed</b>\n<code>no running detached session found</code>",
                    parse_mode="HTML",
                )
                return
            else:
                await message.answer(
                    "<b>Attach failed</b>\n"
                    "<code>multiple detached sessions found, use /sessions then pick one</code>",
                    parse_mode="HTML",
                )
                return

        try:
            session = session_manager.attach_session_for_user(user_id, session_id)
        except ValueError as exc:
            await message.answer(
                f"<b>Attach failed</b>\n"
                f"<code>{escape(str(exc))}</code>",
                parse_mode="HTML",
            )
            return
        current_dir = session_manager.get_current_workdir(user_id)
        await message.answer(
            f"<b>Attached</b>\n"
            f"<b>Session:</b> <code>{escape(session.session_id)}</code>\n"
            f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>",
            parse_mode="HTML",
            reply_markup=session_control_keyboard_main(session.session_id),
        )

    async def do_stop(message: Message, user_id: int, session_id: str | None = None) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        if session_id:
            session = resolve_session_token(user_id, session_id)
        else:
            session = session_manager.get_active_session_for_user(user_id)
        if not session:
            if session_id:
                await message.answer(
                    f"<b>Session not found</b>\n"
                    f"<b>Session:</b> <code>{escape(session_id)}</code>",
                    parse_mode="HTML",
                )
                return
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
        logger.info(
            "Stop requested: user_id=%s session_id=%s command=%r",
            user_id,
            session.session_id,
            session.command,
        )
        await message.answer(f"Stop requested for session {session.session_id}.")
        await stop_live_command(process)

    async def do_kill(message: Message, user_id: int, session_id: str | None = None) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        if session_id:
            session = resolve_session_token(user_id, session_id)
        else:
            session = session_manager.get_active_session_for_user(user_id)
        if not session:
            if session_id:
                await message.answer(
                    f"<b>Session not found</b>\n"
                    f"<b>Session:</b> <code>{escape(session_id)}</code>",
                    parse_mode="HTML",
                )
                return
            await answer_no_active_session(message, current_dir)
            return

        process = session.process
        if process is None or process.returncode is not None:
            await message.answer("No live process is attached to the target session.")
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await message.answer(f"Kill requested for session {session.session_id}.")

    async def do_kill_request(message: Message, user_id: int, session_id: str | None = None) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        if session_id:
            session = resolve_session_token(user_id, session_id)
        else:
            session = session_manager.get_active_session_for_user(user_id)
        if not session:
            if session_id:
                await message.answer(
                    f"<b>Session not found</b>\n"
                    f"<b>Session:</b> <code>{escape(session_id)}</code>",
                    parse_mode="HTML",
                )
                return
            await answer_no_active_session(message, current_dir)
            return
        process = session.process
        if process is None or process.returncode is not None:
            await message.answer("No live process is attached to the target session.")
            return
        request_id = secrets.token_hex(4)
        pending_kill_by_user[user_id] = PendingKill(
            request_id=request_id,
            telegram_user_id=user_id,
            session_id=session.session_id,
        )
        await message.answer(
            f"<b>Kill confirmation</b>\n"
            f"<b>Session:</b> <code>{escape(session.session_id)}</code>\n"
            f"<b>Command:</b> <code>{escape(session.command)}</code>",
            parse_mode="HTML",
            reply_markup=kill_confirm_keyboard(request_id),
        )

    async def do_ctrl_c(message: Message, user_id: int) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        session = session_manager.get_active_session_for_user(user_id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        process = session.process
        if process is None:
            await message.answer("No live process is attached to the active session.")
            return

        if session_manager.is_stream_enabled(user_id):
            reset_stream_state_for_new_input(session, session_manager)
        if not send_ctrl_c(process):
            await message.answer("Could not send Ctrl+C. Process is no longer running.")
            return

        await message.answer(f"Sent Ctrl+C to session {session.session_id}.")

    async def do_ctrl_d(message: Message, user_id: int) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        session = session_manager.get_active_session_for_user(user_id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        master_fd = session.pty_master_fd
        if master_fd is None:
            await message.answer("No active PTY is attached to the session.")
            return

        if session_manager.is_stream_enabled(user_id):
            reset_stream_state_for_new_input(session, session_manager)
        if not send_pty_input(master_fd, b"\x04"):
            await message.answer("Could not send Ctrl+D. PTY is no longer available.")
            return

        await message.answer(f"Sent Ctrl+D (EOF) to session {session.session_id}.")

    async def do_enter(message: Message, user_id: int) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        session = session_manager.get_active_session_for_user(user_id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        master_fd = session.pty_master_fd
        if master_fd is None:
            await message.answer("No active PTY is attached to the session.")
            return

        if session_manager.is_stream_enabled(user_id):
            reset_stream_state_for_new_input(session, session_manager)
        enter_payload = b"\r" if is_codex_session_command(session.command) else b"\n"
        if not send_pty_input(master_fd, enter_payload):
            await message.answer("Could not send Enter. PTY is no longer available.")
            return

        await message.answer(f"Sent Enter to session {session.session_id}.")

    async def do_stream_mode(message: Message, user_id: int, mode: str) -> None:
        if mode not in {"on", "off", "toggle", "status"}:
            await message.answer("Usage: /stream <on|off|toggle|status> (alias: /live)")
            return

        if mode == "status":
            enabled = session_manager.is_stream_enabled(user_id)
            state_text = "on" if enabled else "off"
            await message.answer(f"Stream mode: <code>{state_text}</code>", parse_mode="HTML")
            return

        if mode == "toggle":
            enabled = not session_manager.is_stream_enabled(user_id)
        else:
            enabled = mode == "on"
        session_manager.set_stream_enabled(user_id, enabled)
        session = session_manager.get_active_session_for_user(user_id)
        if session:
            reset_stream_frame_state(session)
            if enabled:
                session_manager.clear_output_buffer(session)
            session_manager.save_session(session)
        state_text = "enabled" if enabled else "disabled"
        await message.answer(f"Stream mode {state_text}.", parse_mode="HTML")

    async def detached_session_ttl_sweeper(bot: Bot) -> None:
        while True:
            await asyncio.sleep(settings.detached_sweep_interval_seconds)
            candidates = session_manager.list_detached_running_sessions()
            if not candidates:
                continue
            now = session_manager.now()
            for session in candidates:
                if not is_detached_session_idle_for_ttl(session):
                    if session.detached_at is not None:
                        session.detached_at = None
                        session_manager.save_session(session)
                    continue
                if session.detached_at is None:
                    session.detached_at = now
                    session_manager.save_session(session)
                    continue
                idle_age = (now - session.detached_at).total_seconds()
                if idle_age < settings.detached_session_ttl_seconds:
                    continue
                process = session.process
                if process is None:
                    continue
                session.stop_requested = True
                logger.info(
                    "Auto-stop detached idle session by TTL: user_id=%s session_id=%s command=%r idle_age_seconds=%s",
                    session.telegram_user_id,
                    session.session_id,
                    session.command,
                    int(idle_age),
                )
                await stop_live_command(process)
                with contextlib.suppress(Exception):
                    await bot.send_message(
                        session.chat_id,
                        (
                            "<b>Session auto-stopped</b>\n"
                            f"<b>Session:</b> <code>{escape(session.session_id)}</code>\n"
                            "<b>Reason:</b> <code>detached idle TTL expired</code>"
                        ),
                        parse_mode="HTML",
                    )

    async def on_startup(bot: Bot) -> None:
        if getattr(dp, "_detached_sweeper_task", None) is not None:
            return
        dp._detached_sweeper_task = asyncio.create_task(detached_session_ttl_sweeper(bot))

    async def on_shutdown(bot: Bot) -> None:
        del bot
        task = getattr(dp, "_detached_sweeper_task", None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=2)
        dp._detached_sweeper_task = None

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    @dp.message(CommandStart())
    async def start_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        current_dir = session_manager.get_current_workdir(user.id)
        await message.answer(
            "Bot is ready.\n\n"
            "Commands:\n"
            "/help\n"
            "/id\n"
            "/run <command>\n"
            "/sessions\n"
            "/attach <session_id>\n"
            "/detach\n"
            "/stop [session_id]\n"
            "/kill [session_id]\n"
            "/ctrl <c|d>\n"
            "/n\n"
            "/stream <on|off|toggle|status> (alias: /live)\n"
            "/get <path>\n"
            "/status [session_id]\n"
            "/tail [session_id]\n\n"
            "Codex in active session: use !/... for codex slash commands (example: !/init)\n\n"
            "Upload behavior:\n"
            "- send a file directly\n"
            "- it will be saved in your current dir\n\n"
            f"Current dir: {current_dir}",
            reply_markup=shell_picker_keyboard(),
        )
        await message.answer(
            "Quick action controls are available on your keyboard.",
            reply_markup=persistent_control_keyboard(),
        )

    @dp.message(Command("id"))
    async def id_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        await message.answer(f"Your Telegram user id: {user.id}")

    @dp.message(Command("help"))
    async def help_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        await do_help(message, user.id)

    @dp.message(Command("status"))
    async def status_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        session_id = parse_optional_session_id(message.text or "")
        await do_status(message, user.id, session_id=session_id)

    @dp.message(Command("tail"))
    async def tail_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        session_id = parse_optional_session_id(message.text or "")
        await do_tail(message, user.id, session_id=session_id)

    @dp.message(Command("sessions"))
    async def sessions_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        token = parse_optional_session_id(message.text or "")
        page = 1
        if token:
            try:
                page = int(token)
            except ValueError:
                page = 1
        await do_sessions(message, user.id, page=page)

    @dp.message(Command("attach"))
    async def attach_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return
        session_id = parse_optional_session_id(message.text or "")
        await do_attach(message, user.id, session_id)

    @dp.message(Command("detach"))
    async def detach_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return
        await do_detach(message, user.id)

    @dp.message(Command("get"))
    async def get_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer(
                "Usage: /get <path>",
                reply_markup=context_control_keyboard("help", "status"),
            )
            return

        current_dir = session_manager.get_current_workdir(user.id)
        raw_path = parts[1].strip()
        target_path = resolve_user_path(raw_path, current_dir)

        if not target_path.exists():
            logger.info(
                "Get failed (missing path): user_id=%s path=%s",
                user.id,
                target_path,
            )
            await message.answer(
                f"<b>Get failed</b>\n"
                f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
                f"<b>Path:</b> <code>{escape(str(target_path))}</code>\n"
                f"<b>Reason:</b> <code>path does not exist</code>",
                parse_mode="HTML",
                reply_markup=context_control_keyboard("status", "help"),
            )
            return

        if not target_path.is_file():
            logger.info(
                "Get failed (not file): user_id=%s path=%s",
                user.id,
                target_path,
            )
            await message.answer(
                f"<b>Get failed</b>\n"
                f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
                f"<b>Path:</b> <code>{escape(str(target_path))}</code>\n"
                f"<b>Reason:</b> <code>path is not a file</code>",
                parse_mode="HTML",
                reply_markup=context_control_keyboard("status", "help"),
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
            logger.exception(
                "Get failed with exception: user_id=%s path=%s",
                user.id,
                target_path,
            )
            await message.answer(
                f"<b>Get failed</b>\n"
                f"<b>Path:</b> <code>{escape(str(target_path))}</code>\n"
                f"<b>Error:</b> <code>{escape(str(exc))}</code>",
                parse_mode="HTML",
                reply_markup=context_control_keyboard("status", "help"),
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

        if document.file_size and document.file_size > settings.max_upload_bytes:
            logger.warning(
                "Upload rejected (size limit): user_id=%s name=%r size=%s limit=%s",
                user.id,
                document.file_name,
                document.file_size,
                settings.max_upload_bytes,
            )
            await message.answer(
                f"<b>Upload failed</b>\n"
                f"<b>Reason:</b> <code>file is too large</code>\n"
                f"<b>File size:</b> <code>{format_size(document.file_size)}</code>\n"
                f"<b>Limit:</b> <code>{format_size(settings.max_upload_bytes)}</code>",
                parse_mode="HTML",
            )
            return

        try:
            safe_name = sanitize_uploaded_filename(document.file_name)
        except ValueError:
            await message.answer("Upload failed: invalid file name.")
            return

        current_dir = session_manager.get_current_workdir(user.id)
        target_path = (current_dir / safe_name).resolve()

        if target_path.exists():
            pending = PendingUpload(
                request_id=secrets.token_hex(4),
                telegram_user_id=user.id,
                chat_id=message.chat.id,
                file_id=document.file_id,
                file_name=safe_name,
                target_path=target_path,
            )
            session_manager.set_pending_upload(
                pending
            )
            await message.answer(
                f"<b>File already exists</b>\n"
                f"<b>Path:</b> <code>{escape(str(target_path))}</code>\n"
                f"<b>Action:</b> <code>overwrite?</code>",
                parse_mode="HTML",
                reply_markup=upload_confirm_keyboard(pending.request_id),
            )
            return

        try:
            await save_telegram_file(message, document.file_id, target_path)
            actual_size = target_path.stat().st_size
            if actual_size > settings.max_upload_bytes:
                with contextlib.suppress(OSError):
                    target_path.unlink()
                logger.warning(
                    "Upload removed after save (size limit): user_id=%s path=%s size=%s limit=%s",
                    user.id,
                    target_path,
                    actual_size,
                    settings.max_upload_bytes,
                )
                await message.answer(
                    f"<b>Upload failed</b>\n"
                    f"<b>Reason:</b> <code>file is too large</code>\n"
                    f"<b>File size:</b> <code>{format_size(actual_size)}</code>\n"
                    f"<b>Limit:</b> <code>{format_size(settings.max_upload_bytes)}</code>",
                    parse_mode="HTML",
                )
                return
            file_sha256 = sha256_file(target_path)
            logger.info(
                "Upload saved: user_id=%s path=%s size=%s",
                user.id,
                target_path,
                actual_size,
            )
            await message.answer(
                f"<b>Uploaded</b>\n"
                f"<b>SHA256:</b> <code>{file_sha256}</code>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.exception(
                "Upload failed with exception: user_id=%s file_name=%r",
                user.id,
                document.file_name if document else None,
            )
            await message.answer(
                f"<b>Upload failed</b>\n"
                f"<b>Error:</b> <code>{escape(str(exc))}</code>",
                parse_mode="HTML",
            )

    @dp.callback_query(F.data.startswith("upload_cancel"))
    async def upload_cancel_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return

        parsed = parse_upload_callback(callback.data)
        if not parsed:
            await callback.answer("Invalid action.", show_alert=False)
            return
        _, request_id = parsed

        pending = session_manager.get_pending_upload(user.id)
        if not pending:
            await callback.answer("No pending upload.", show_alert=False)
            return
        if not request_id:
            await callback.answer("Stale upload prompt.", show_alert=False)
            return
        if pending.request_id != request_id:
            await callback.answer("Stale upload prompt.", show_alert=False)
            return

        session_manager.clear_pending_upload(user.id)

        if callback.message:
            text = "<b>Upload cancelled</b>"
            if pending:
                text += f"\n<b>Path:</b> <code>{escape(str(pending.target_path))}</code>"
            await callback.message.edit_text(text, parse_mode="HTML")

        await callback.answer("Upload cancelled.", show_alert=False)

    @dp.callback_query(F.data.startswith("upload_overwrite"))
    async def upload_overwrite_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return

        parsed = parse_upload_callback(callback.data)
        if not parsed:
            await callback.answer("Invalid action.", show_alert=False)
            return
        _, request_id = parsed

        pending = session_manager.get_pending_upload(user.id)
        if not pending:
            await callback.answer("No pending upload.", show_alert=False)
            if callback.message:
                await callback.message.edit_text("<b>No pending upload</b>", parse_mode="HTML")
            return
        if not request_id:
            await callback.answer("Stale upload prompt.", show_alert=False)
            return
        if pending.request_id != request_id:
            await callback.answer("Stale upload prompt.", show_alert=False)
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
            await callback.answer("Upload overwritten.", show_alert=False)
        except Exception as exc:
            if callback.message:
                await callback.message.edit_text(
                    f"<b>Upload failed</b>\n"
                    f"<b>Error:</b> <code>{escape(str(exc))}</code>",
                    parse_mode="HTML",
                )
            await callback.answer("Upload failed.", show_alert=False)
        finally:
            session_manager.clear_pending_upload(user.id)

    @dp.callback_query(F.data.startswith(f"{SESSION_CONTROL_PREFIX}:"))
    async def session_control_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return

        parsed = parse_session_control_callback(callback.data)
        if not parsed:
            await callback.answer("Invalid action.", show_alert=False)
            return

        action, target_session_id = parsed
        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_session_for_user(user.id, target_session_id)
        if not session:
            await callback.answer("Session not found.", show_alert=False)
            return

        if action == "detach":
            active = session_manager.get_active_session_for_user(user.id)
            if not active:
                await callback.answer("No attached session.", show_alert=False)
                return
            if active.session_id != session.session_id:
                await callback.answer("Detach only works for attached session.", show_alert=False)
                return
            detached = session_manager.detach_active_session_for_user(user.id)
            if not detached:
                await callback.answer("No attached session.", show_alert=False)
                return
            await callback.answer("Detached.", show_alert=False)
            return

        if action == "menu_main":
            if callback.message:
                with contextlib.suppress(Exception):
                    await callback.message.edit_reply_markup(
                        reply_markup=session_control_keyboard_main(session.session_id)
                    )
            await callback.answer("Main menu.", show_alert=False)
            return

        if action == "menu_controls":
            if callback.message:
                with contextlib.suppress(Exception):
                    await callback.message.edit_reply_markup(
                        reply_markup=session_control_keyboard_controls(session.session_id)
                    )
            await callback.answer("Controls menu.", show_alert=False)
            return

        if action == "menu_output":
            if callback.message:
                with contextlib.suppress(Exception):
                    await callback.message.edit_reply_markup(
                        reply_markup=session_control_keyboard_output(session.session_id)
                    )
            await callback.answer("Output menu.", show_alert=False)
            return

        if action == "status":
            if callback.message:
                await callback.message.answer(
                    format_session_status_message(
                        session,
                        current_dir,
                        session_manager.is_stream_enabled(user.id),
                        now=session_manager.now(),
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
            if not session_manager.is_stream_enabled(user.id):
                session_manager.clear_output_buffer(session)
            await callback.answer("Tail sent.", show_alert=False)
            return

        if action == "stream_toggle":
            enabled = not session_manager.is_stream_enabled(user.id)
            session_manager.set_stream_enabled(user.id, enabled)
            reset_stream_frame_state(session)
            if enabled:
                session_manager.clear_output_buffer(session)
            session_manager.save_session(session)
            mode_text = "on" if enabled else "off"
            await callback.answer(f"Stream: {mode_text}.", show_alert=False)
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
            logger.info(
                "Stop requested from inline control: user_id=%s session_id=%s command=%r",
                user.id,
                session.session_id,
                session.command,
            )
            await callback.answer("Stop requested.", show_alert=False)
            await stop_live_command(process)
            return

        if action == "ctrl_c":
            process = session.process
            if process is None:
                await callback.answer("No live process.", show_alert=False)
                return

            if session_manager.is_stream_enabled(user.id):
                reset_stream_state_for_new_input(session, session_manager)
            if not send_ctrl_c(process):
                await callback.answer("Process is not running.", show_alert=False)
                return

            await callback.answer("Ctrl+C sent.", show_alert=False)
            return

        master_fd = session.pty_master_fd
        if master_fd is None:
            await callback.answer("No active PTY.", show_alert=False)
            return

        if action in {"ctrl_d", "enter"} and session_manager.is_stream_enabled(user.id):
            reset_stream_state_for_new_input(session, session_manager)
        payload = b"\x04" if action == "ctrl_d" else b"\n"
        action_name = "Ctrl+D" if action == "ctrl_d" else "Enter"
        if not send_pty_input(master_fd, payload):
            await callback.answer("PTY is unavailable.", show_alert=False)
            return

        await callback.answer(f"{action_name} sent.", show_alert=False)

    @dp.callback_query(F.data.startswith(f"{CONTEXT_CONTROL_PREFIX}:"))
    async def context_control_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return

        action = parse_context_control_callback(callback.data)
        if not action:
            await callback.answer("Invalid action.", show_alert=False)
            return

        if not callback.message:
            await callback.answer("No message context.", show_alert=False)
            return

        if action == "help":
            await do_help(callback.message, user.id)
            await callback.answer("Help sent.", show_alert=False)
            return
        if action == "status":
            await do_status(callback.message, user.id)
            await callback.answer("Status sent.", show_alert=False)
            return

        await do_tail(callback.message, user.id)
        await callback.answer("Tail sent.", show_alert=False)

    @dp.callback_query(F.data.startswith(f"{SHELL_PICKER_PREFIX}:"))
    async def shell_picker_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return
        shell_name = parse_shell_picker_callback(callback.data)
        if shell_name is None:
            await callback.answer("Invalid shell.", show_alert=False)
            return
        if not callback.message:
            await callback.answer("No message context.", show_alert=False)
            return
        await do_start_shell(callback.message, user.id, shell_name)
        await callback.answer(f"{shell_name} requested.", show_alert=False)

    @dp.callback_query(F.data.startswith(f"{SESSION_LIST_PREFIX}:"))
    async def session_list_control_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return
        parsed = parse_sessions_list_callback(callback.data)
        if not parsed:
            await callback.answer("Invalid action.", show_alert=False)
            return
        action, session_id = parsed
        if not callback.message:
            await callback.answer("No message context.", show_alert=False)
            return

        if action == "attach":
            await do_attach(callback.message, user.id, session_id)
            await callback.answer("Attach requested.", show_alert=False)
            return
        if action == "tail":
            await do_tail(callback.message, user.id, session_id=session_id)
            await callback.answer("Tail sent.", show_alert=False)
            return
        if action == "stop":
            await do_stop(callback.message, user.id, session_id=session_id)
            await callback.answer("Stop requested.", show_alert=False)
            return
        await do_kill_request(callback.message, user.id, session_id=session_id)
        await callback.answer("Kill confirmation sent.", show_alert=False)

    @dp.callback_query(F.data.startswith(f"{SESSION_PAGE_PREFIX}:"))
    async def sessions_page_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return
        page = parse_sessions_page_callback(callback.data)
        if page is None:
            await callback.answer("Invalid page.", show_alert=False)
            return
        if page == 0:
            await callback.answer("No more pages.", show_alert=False)
            return
        if not callback.message:
            await callback.answer("No message context.", show_alert=False)
            return
        await do_sessions_edit(callback.message, user.id, page=page)
        await callback.answer(f"Page {page}.", show_alert=False)

    @dp.callback_query(F.data.startswith(f"{KILL_CONFIRM_PREFIX}:"))
    async def kill_confirm_handler(callback: CallbackQuery) -> None:
        user = callback.from_user
        if not user or not is_allowed(user.id, settings):
            return
        parsed = parse_kill_confirm_callback(callback.data)
        if not parsed:
            await callback.answer("Invalid action.", show_alert=False)
            return
        action, request_id = parsed
        pending = pending_kill_by_user.get(user.id)
        if not pending or pending.request_id != request_id:
            await callback.answer("Stale kill request.", show_alert=False)
            return
        if action == "cancel":
            pending_kill_by_user.pop(user.id, None)
            if callback.message:
                with contextlib.suppress(Exception):
                    await callback.message.edit_text("<b>Kill cancelled</b>", parse_mode="HTML")
            await callback.answer("Kill cancelled.", show_alert=False)
            return
        if not callback.message:
            await callback.answer("No message context.", show_alert=False)
            return
        pending_kill_by_user.pop(user.id, None)
        await do_kill(callback.message, user.id, session_id=pending.session_id)
        await callback.answer("Kill requested.", show_alert=False)

    @dp.message(Command("stop"))
    async def stop_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        session_id = parse_optional_session_id(message.text or "")
        await do_stop(message, user.id, session_id=session_id)

    @dp.message(Command("kill"))
    async def kill_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        session_id = parse_optional_session_id(message.text or "")
        await do_kill_request(message, user.id, session_id=session_id)

    @dp.message(Command("ctrl"))
    async def ctrl_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Usage: /ctrl <c|d>",
                reply_markup=context_control_keyboard("help", "status"),
            )
            return

        action = parts[1].strip().lower()
        if action not in {"c", "d"}:
            await message.answer(
                "Usage: /ctrl <c|d>",
                reply_markup=context_control_keyboard("help", "status"),
            )
            return

        if action == "c":
            await do_ctrl_c(message, user.id)
            return

        await do_ctrl_d(message, user.id)

    @dp.message(Command("n"))
    async def newline_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        await do_enter(message, user.id)

    @dp.message(Command(commands=["stream", "live"]))
    async def stream_mode_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        mode = parts[1].strip().lower() if len(parts) >= 2 else "status"
        await do_stream_mode(message, user.id, mode)

    @dp.message(F.text.in_(list(QUICK_ACTION_BY_TEXT.keys())))
    async def quick_action_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        action = QUICK_ACTION_BY_TEXT.get(text)
        if action == "help":
            await do_help(message, user.id)
            return
        if action == "status":
            await do_status(message, user.id)
            return
        if action == "tail":
            await do_tail(message, user.id)
            return
        if action == "sessions":
            await do_sessions(message, user.id)
            return
        if action == "detach":
            await do_detach(message, user.id)
            return
        if action == "stop":
            await do_stop(message, user.id)
            return
        if action == "ctrl_c":
            await do_ctrl_c(message, user.id)
            return
        if action == "ctrl_d":
            await do_ctrl_d(message, user.id)
            return
        if action == "enter":
            await do_enter(message, user.id)
            return
        if action == "stream_toggle":
            await do_stream_mode(message, user.id, "toggle")
            return
        if action == "shell_menu":
            await message.answer(
                "<b>Choose a shell</b>\n"
                "<b>Use:</b> quick start for a new interactive session.",
                parse_mode="HTML",
                reply_markup=shell_picker_keyboard(),
            )
            return

    @dp.message(Command("run"))
    async def run_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "Usage: /run <command>",
                reply_markup=context_control_keyboard("help", "status"),
            )
            return

        command = parts[1].strip()
        if not command:
            await message.answer(
                "Usage: /run <command>",
                reply_markup=context_control_keyboard("help", "status"),
            )
            return

        if command in {"zsh", "bash"}:
            await do_start_shell(message, user.id, command)
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

        running_count = session_manager.count_running_sessions_for_user(user.id)
        if running_count >= settings.max_running_sessions_per_user:
            await message.answer(
                f"<b>Run blocked</b>\n"
                f"<b>Reason:</b> <code>running session limit reached</code>\n"
                f"<b>Running:</b> <code>{running_count}/{settings.max_running_sessions_per_user}</code>\n"
                f"<b>Use:</b> <code>/sessions</code> then stop/attach/detach as needed.",
                parse_mode="HTML",
            )
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
            chunks = build_session_result_chunks(
                session_id="oneshot",
                state=state,
                exit_code=exit_code,
                cwd=str(current_dir),
                output=combined_output,
            )
            for i, chunk in enumerate(chunks):
                await message.answer(
                    chunk,
                    parse_mode="HTML",
                    reply_markup=persistent_control_keyboard() if i == 0 else None,
                )
            return

        session = session_manager.create_session(
            telegram_user_id=user.id,
            chat_id=message.chat.id,
            command=command,
            max_session_history_per_user=settings.max_session_history_per_user,
        )
        logger.info(
            "Starting live session: user_id=%s session_id=%s command=%r cwd=%s",
            user.id,
            session.session_id,
            command,
            current_dir,
        )
        try:
            live = await start_live_command(
                command=command,
                shell=settings.default_shell,
                cwd=current_dir,
            )
        except Exception as exc:
            session.state = "failed"
            session.ended_at = session_manager.now()
            session_manager.append_output_text(
                session,
                f"ERROR: {exc!r}\n",
                settings.max_tail_lines,
            )
            session_manager.finish_session(session)
            logger.exception(
                "Failed to start live session: user_id=%s session_id=%s command=%r cwd=%s",
                user.id,
                session.session_id,
                command,
                current_dir,
            )

            tail_lines = session_manager.get_tail_snapshot(session, settings.max_tail_lines)
            tail_text = "\n".join(tail_lines) if tail_lines else "[no output]"
            chunks = build_session_result_chunks(
                session_id=session.session_id,
                state=session.state,
                exit_code=session.exit_code,
                cwd=str(current_dir),
                output=tail_text,
            )
            for chunk in chunks:
                await message.answer(chunk, parse_mode="HTML")
            return

        session.state = "running"
        session.process = live.process
        session.pty_master_fd = live.pty_master_fd
        reset_stream_frame_state(session)
        session.is_attached = True
        session_manager.save_session(session)
        session.reader_task = asyncio.create_task(
            read_session_output(session, session_manager, settings.max_tail_lines)
        )
        session.streamer_task = asyncio.create_task(
            stream_session_output(session, session_manager, message.bot)
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
            reply_markup=session_control_keyboard_main(session.session_id),
        )
        await message.answer(
            "Quick action controls are available on your keyboard.",
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
            outbound = text
            if is_codex_session_command(session.command) and outbound.startswith("!/"):
                outbound = "/" + outbound[2:]

            if session_manager.is_stream_enabled(user.id):
                reset_stream_state_for_new_input(session, session_manager)
            stripped = outbound.strip()
            if stripped:
                session.pending_echo_inputs.append(stripped)
                if len(session.pending_echo_inputs) > 20:
                    session.pending_echo_inputs = session.pending_echo_inputs[-20:]
            line_ending = b"\r" if is_codex_session_command(session.command) else b"\n"
            data = outbound.encode(errors="replace") + line_ending
            if not send_pty_input(master_fd, data):
                await message.answer("Could not send text to active session.")
            return

        current_dir = session_manager.get_current_workdir(user.id)
        running_count = session_manager.count_running_sessions_for_user(user.id)
        hint = (
            f"\n<b>Hint:</b> <code>/sessions</code> to attach a running detached session."
            if running_count > 0
            else ""
        )
        await message.answer(
            f"<b>No active route for plain text</b>\n"
            f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
            f"<b>Use:</b> <code>/run &lt;command&gt;</code> for shell."
            f"{hint}",
            parse_mode="HTML",
            reply_markup=context_control_keyboard("help", "status"),
        )

    @dp.message(F.text.startswith("/"))
    async def unknown_slash_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return
        session = session_manager.get_active_session_for_user(user.id)
        if session and is_codex_session_command(session.command):
            await message.answer(
                "<b>Codex slash hint</b>\n"
                "For codex internal slash commands, use <code>!/...</code>\n"
                "Example: <code>!/init</code>, <code>!/status</code>",
                parse_mode="HTML",
            )
            return
        await message.answer(
            "Unknown command.\nUse <code>/help</code>.",
            parse_mode="HTML",
            reply_markup=context_control_keyboard("help", "status"),
        )

    return dp
