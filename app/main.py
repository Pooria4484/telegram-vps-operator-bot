from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommandScopeAllPrivateChats

from app.bot import bot_command_menu, build_dispatcher
from app.codex_runner import CodexRunner
from app.config import load_settings
from app.session_manager import SessionManager


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger(__name__).info(
        "Starting bot: workdir=%s max_tail_lines=%s max_upload_bytes=%s telegram_api_file_limit_bytes=%s telegram_api_base_url=%s telegram_api_is_local=%s max_running_sessions_per_user=%s max_session_history_per_user=%s time_offset_minutes=%s log_level=%s session_db_path=%s codex_command=%s codex_artifacts_dir=%s",
        settings.workdir,
        settings.max_tail_lines,
        settings.max_upload_bytes,
        settings.telegram_api_file_limit_bytes,
        settings.telegram_api_base_url or "<default>",
        settings.telegram_api_is_local,
        settings.max_running_sessions_per_user,
        settings.max_session_history_per_user,
        settings.time_offset_minutes,
        settings.log_level,
        settings.session_db_path,
        settings.codex_command,
        settings.codex_artifacts_dir,
    )
    bot_session: AiohttpSession | None = None
    if settings.telegram_api_base_url:
        api_server = TelegramAPIServer.from_base(
            settings.telegram_api_base_url,
            is_local=settings.telegram_api_is_local,
        )
        bot_session = AiohttpSession(api=api_server)
    bot = Bot(token=settings.bot_token, session=bot_session)
    await bot.set_my_commands(
        bot_command_menu(),
        scope=BotCommandScopeAllPrivateChats(),
    )
    session_manager = SessionManager(
        default_workdir=settings.workdir,
        db_path=settings.session_db_path,
        time_offset_minutes=settings.time_offset_minutes,
    )
    codex_runner = CodexRunner(settings, session_manager)
    dp = build_dispatcher(settings, session_manager, codex_runner=codex_runner)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
