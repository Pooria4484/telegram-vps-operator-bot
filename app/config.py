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


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be > 0")
    return value


@dataclass(slots=True)
class Settings:
    bot_token: str
    allowed_user_ids: set[int]
    default_shell: str
    workdir: Path
    max_tail_lines: int
    max_upload_bytes: int
    log_level: str


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
        max_tail_lines=_parse_positive_int_env("MAX_TAIL_LINES", 30),
        max_upload_bytes=_parse_positive_int_env("MAX_UPLOAD_BYTES", 20 * 1024 * 1024),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )
