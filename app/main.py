from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import BotCommandScopeAllPrivateChats

from app.bot import bot_command_menu, build_dispatcher
from app.config import load_settings
from app.session_manager import SessionManager


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger(__name__).info(
        "Starting bot: workdir=%s max_tail_lines=%s max_upload_bytes=%s log_level=%s",
        settings.workdir,
        settings.max_tail_lines,
        settings.max_upload_bytes,
        settings.log_level,
    )
    bot = Bot(token=settings.bot_token)
    await bot.set_my_commands(
        bot_command_menu(),
        scope=BotCommandScopeAllPrivateChats(),
    )
    session_manager = SessionManager(default_workdir=settings.workdir)
    dp = build_dispatcher(settings, session_manager)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
