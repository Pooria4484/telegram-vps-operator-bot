from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from aiogram import Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.auth import is_allowed
from app.command_runner import run_command
from app.config import Settings
from app.session_manager import SessionManager


def resolve_cd_target(raw_target: str, current_dir: Path) -> Path:
    target = raw_target.strip() or "~"
    expanded = Path(target).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (current_dir / expanded).resolve()


def format_session_result(
    session_id: str,
    state: str,
    exit_code: int | None,
    cwd: str,
    output: str,
) -> str:
    safe_session_id = escape(session_id)
    safe_state = escape(state)
    safe_exit = escape(str(exit_code))
    safe_cwd = escape(cwd)

    lines = (output or "[no output]").splitlines() or ["[no output]"]
    rendered_lines = "\n".join(
        f"<code>{escape(line) if line else ' '}</code>" for line in lines
    )

    return (
        f"<b>Session</b> <code>{safe_session_id}</code>\n"
        f"<b>State:</b> <code>{safe_state}</code>\n"
        f"<b>Exit code:</b> <code>{safe_exit}</code>\n"
        f"<b>Current dir:</b> <code>{safe_cwd}</code>\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>Output</b>\n"
        f"{rendered_lines}"
    )


def build_dispatcher(settings: Settings, session_manager: SessionManager) -> Dispatcher:
    dp = Dispatcher()

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
            "/status\n"
            "/tail\n\n"
            f"Current dir: {current_dir}"
        )

    @dp.message(Command("id"))
    async def id_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        await message.answer(f"Your Telegram user id: {user.id}")

    @dp.message(Command("status"))
    async def status_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_active_session_for_user(user.id)
        if not session:
            await message.answer(f"No active session.\nCurrent dir: {current_dir}")
            return

        runtime = datetime.utcnow() - session.started_at
        await message.answer(
            f"Session: {session.session_id}\n"
            f"State: {session.state}\n"
            f"Command: {session.command}\n"
            f"Current dir: {current_dir}\n"
            f"Runtime: {str(runtime).split('.')[0]}\n"
            f"Exit code: {session.exit_code}"
        )

    @dp.message(Command("tail"))
    async def tail_handler(message: Message) -> None:
        user = message.from_user
        if not user or not is_allowed(user.id, settings):
            return

        current_dir = session_manager.get_current_workdir(user.id)
        session = session_manager.get_active_session_for_user(user.id)
        if not session:
            await message.answer(f"No active session.\nCurrent dir: {current_dir}")
            return

        if not session.tail_lines:
            await message.answer("[no output]")
            return

        text = "\n".join(session.tail_lines[-settings.max_tail_lines :])
        await message.answer(text)

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
            await message.answer("You already have an active session.")
            return

        session = session_manager.create_session(
            telegram_user_id=user.id,
            chat_id=message.chat.id,
            command=command,
        )

        await message.answer(
            f"<b>Started session</b> <code>{escape(session.session_id)}</code>\n"
            f"<b>Current dir:</b> <code>{escape(str(current_dir))}</code>\n"
            f"<b>Command:</b> <code>{escape(command)}</code>",
            parse_mode="HTML",
        )
        session.state = "running"

        try:
            result = await run_command(
                command=command,
                shell=settings.default_shell,
                cwd=current_dir,
            )
            output_lines: list[str] = []
            if result.stdout.strip():
                output_lines.extend(result.stdout.splitlines())
            if result.stderr.strip():
                output_lines.extend(result.stderr.splitlines())

            session_manager.append_tail(session, output_lines, settings.max_tail_lines)
            session.exit_code = result.exit_code
            session.state = "finished" if result.exit_code == 0 else "failed"
        except Exception as exc:
            session_manager.append_tail(
                session,
                [f"ERROR: {exc!r}"],
                settings.max_tail_lines,
            )
            session.state = "failed"
        finally:
            session.ended_at = datetime.utcnow()
            session_manager.finish_session(session)

        tail_text = "\n".join(session.tail_lines) if session.tail_lines else "[no output]"
        current_dir = session_manager.get_current_workdir(user.id)
        final_text = format_session_result(
            session_id=session.session_id,
            state=session.state,
            exit_code=session.exit_code,
            cwd=str(current_dir),
            output=tail_text,
        )

        print("FINAL TEXT TO SEND:", repr(final_text))
        await message.answer(final_text, parse_mode="HTML")

    return dp
