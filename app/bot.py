from __future__ import annotations

from datetime import datetime
from html import escape
import hashlib
from pathlib import Path

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.auth import is_allowed
from app.command_runner import run_command
from app.config import Settings
from app.session_manager import PendingUpload, SessionManager


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
            "/get <path>\n"
            "/status\n"
            "/tail\n\n"
            "Upload behavior:\n"
            "- send a file directly\n"
            "- it will be saved in your current dir\n\n"
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
