from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime
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
from app.session_manager import PendingUpload, SessionManager

SESSION_CONTROL_PREFIX = "sessctl"
CONTEXT_CONTROL_PREFIX = "ctxctl"
SESSION_LIST_PREFIX = "sesslist"
SESSION_PAGE_PREFIX = "sesspage"
KILL_CONFIRM_PREFIX = "killcfm"
SESSION_STREAM_INTERVAL_SECONDS = 2.0
SESSION_STREAM_MAX_LINES = 20
logger = logging.getLogger(__name__)

BTN_STATUS = "Status"
BTN_TAIL = "Tail"
BTN_STOP = "Stop"
BTN_SESSIONS = "Sessions"
BTN_DETACH = "Detach"
BTN_CTRL_C = "Ctrl+C"
BTN_ENTER = "Enter"
BTN_CLEAR = "Clear"
BTN_STREAM = "Stream"
BTN_HELP = "Help"

QUICK_ACTION_BY_TEXT: dict[str, str] = {
    BTN_STATUS: "status",
    BTN_TAIL: "tail",
    BTN_STOP: "stop",
    BTN_SESSIONS: "sessions",
    BTN_DETACH: "detach",
    BTN_CTRL_C: "ctrl_c",
    BTN_ENTER: "enter",
    BTN_CLEAR: "clear",
    BTN_STREAM: "stream_toggle",
    BTN_HELP: "help",
}


@dataclass(slots=True)
class PendingKill:
    request_id: str
    telegram_user_id: int
    session_id: str


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
                KeyboardButton(text=BTN_ENTER),
                KeyboardButton(text=BTN_STREAM),
            ],
            [
                KeyboardButton(text=BTN_CLEAR),
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
                InlineKeyboardButton(text="Clear", callback_data=f"{SESSION_CONTROL_PREFIX}:clear:{session_id}"),
            ],
            [
                InlineKeyboardButton(text="Back", callback_data=f"{SESSION_CONTROL_PREFIX}:menu_main:{session_id}"),
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


def sessions_list_keyboard(
    session_ids: list[str],
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
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
    nav_row: list[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(
            InlineKeyboardButton(
                text="Prev",
                callback_data=f"{SESSION_PAGE_PREFIX}:{page - 1}",
            )
        )
    if page < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="Next",
                callback_data=f"{SESSION_PAGE_PREFIX}:{page + 1}",
            )
        )
    if nav_row:
        rows.append(nav_row)
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
        "clear",
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
        BotCommand(command="clear", description="Clear output buffer"),
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
        f"<b>Detach first:</b> <code>/detach</code>\n"
        f"<b>Controls:</b> <code>/n</code>, <code>/ctrl c</code>, <code>/ctrl d</code>, <code>/tail</code>, "
        f"<code>/status</code>, <code>/stop</code>, <code>/clear</code>\n"
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
        f"<b>Attached:</b> <code>{'yes' if session.is_attached else 'no'}</code>\n"
        f"<b>PID:</b> <code>{escape(str(pid))}</code>\n"
        f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
        f"<b>Runtime:</b> <code>{escape(str(runtime).split('.')[0])}</code>\n"
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
        "• <code>/live ...</code>: نام جایگزین برای stream\n"
        "  مثال: <code>/live status</code>\n"
        "• <code>/clear</code>: پاک کردن بافر خروجی سشن فعال\n"
        "  مثال: <code>/clear</code>\n"
        "• <code>/get &lt;path&gt;</code>: دریافت فایل از سرور\n"
        "  مثال: <code>/get logs/app.log</code>\n"
        "• ارسال فایل: آپلود فایل در مسیر کاری فعلی شما\n"
        "  مثال: فایل را مستقیم در چت ارسال کنید\n"
        "• متن ساده در حالت سشن فعال: به stdin همان سشن ارسال می‌شود\n"
        "  مثال: بعد از <code>/run bash</code> پیام <code>pwd</code> بفرستید\n\n"
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
        "• <code>/live ...</code>: alias for <code>/stream ...</code>\n"
        "  Example: <code>/live on</code>\n"
        "• <code>/clear</code>: clear output buffer\n"
        "  Example: <code>/clear</code>\n"
        "• <code>/get &lt;path&gt;</code>: download file from VPS\n"
        "  Example: <code>/get /etc/hosts</code>\n"
        "• File upload: send a file directly in chat\n"
        "  Example: upload <code>deploy.sh</code> to current dir\n"
        "• Plain text while a session is active: forwarded to session stdin\n"
        "  Example: run <code>/run zsh</code>, then send <code>ls</code>\n\n"
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


def format_sessions_list_message(sessions: list[Session], max_running: int, running_count: int) -> str:
    if not sessions:
        return "<b>No sessions found</b>"

    lines: list[str] = [
        "<b>Your Sessions</b>",
        f"<b>Running:</b> <code>{running_count}/{max_running}</code>",
        "━━━━━━━━━━━━━━",
    ]
    for session in sessions:
        runtime_end = session.ended_at or datetime.utcnow()
        runtime = str((runtime_end - session.started_at)).split(".")[0]
        attached = "yes" if session.is_attached else "no"
        lines.append(
            f"<b>{escape(session.session_id)}</b> | "
            f"<code>{escape(session.state)}</code> | "
            f"attached=<code>{attached}</code> | "
            f"runtime=<code>{escape(runtime)}</code>\n"
            f"<code>{escape(session.command)}</code>"
        )
    return "\n".join(lines)


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
        session.ended_at = datetime.utcnow()
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
    pending_kill_by_user: dict[int, PendingKill] = {}

    def parse_optional_session_id(text: str) -> str | None:
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            return None
        candidate = parts[1].strip()
        return candidate or None

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
            format_session_status_message(session, current_dir, stream_enabled),
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

        if not send_pty_input(master_fd, b"\n"):
            await message.answer("Could not send Enter. PTY is no longer available.")
            return

        await message.answer(f"Sent Enter to session {session.session_id}.")

    async def do_clear(message: Message, user_id: int) -> None:
        current_dir = session_manager.get_current_workdir(user_id)
        session = session_manager.get_active_session_for_user(user_id)
        if not session:
            await answer_no_active_session(message, current_dir)
            return

        session_manager.clear_output_buffer(session)
        await message.answer(
            f"Output buffer cleared for session <code>{escape(session.session_id)}</code>.",
            parse_mode="HTML",
        )

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
        if enabled:
            session = session_manager.get_active_session_for_user(user_id)
            if session:
                session.stream_last_sent_text = ""
        state_text = "enabled" if enabled else "disabled"
        await message.answer(f"Stream mode {state_text}.", parse_mode="HTML")

    async def detached_session_ttl_sweeper(bot: Bot) -> None:
        while True:
            await asyncio.sleep(settings.detached_sweep_interval_seconds)
            candidates = session_manager.list_detached_running_sessions()
            if not candidates:
                continue
            now = datetime.utcnow()
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
            "/clear\n"
            "/stream <on|off|toggle|status> (alias: /live)\n"
            "/get <path>\n"
            "/status [session_id]\n"
            "/tail [session_id]\n\n"
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
            await callback.answer(f"Stream: {mode_text}.", show_alert=False)
            return

        if action == "clear":
            session_manager.clear_output_buffer(session)
            await callback.answer("Output cleared.", show_alert=False)
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

            if not send_ctrl_c(process):
                await callback.answer("Process is not running.", show_alert=False)
                return

            await callback.answer("Ctrl+C sent.", show_alert=False)
            return

        master_fd = session.pty_master_fd
        if master_fd is None:
            await callback.answer("No active PTY.", show_alert=False)
            return

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
        if not callback.message:
            await callback.answer("No message context.", show_alert=False)
            return
        await do_sessions(callback.message, user.id, page=page)
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

    @dp.message(Command("clear"))
    async def clear_output_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        await do_clear(message, user.id)

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
        if action == "enter":
            await do_enter(message, user.id)
            return
        if action == "clear":
            await do_clear(message, user.id)
            return
        if action == "stream_toggle":
            await do_stream_mode(message, user.id, "toggle")
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
            session.ended_at = datetime.utcnow()
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
        session.is_attached = True
        session_manager.save_session(session)
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

            data = text.encode(errors="replace") + b"\n"
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

    return dp
