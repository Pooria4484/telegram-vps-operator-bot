from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _parse_user_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        result.add(int(part))
    return result


@dataclass(slots=True)
class Settings:
    bot_token: str
    allowed_user_ids: set[int]
    default_shell: str
    workdir: Path
    max_tail_lines: int


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing")

    allowed_user_ids = _parse_user_ids(os.getenv("ALLOWED_USER_IDS", ""))

    return Settings(
        bot_token=bot_token,
        allowed_user_ids=allowed_user_ids,
        default_shell=os.getenv("DEFAULT_SHELL", "/bin/bash").strip(),
        workdir=Path(os.getenv("WORKDIR", str(Path.home()))).expanduser(),
        max_tail_lines=int(os.getenv("MAX_TAIL_LINES", "30")),
    )
