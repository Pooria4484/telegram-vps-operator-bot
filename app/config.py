from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re

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


def _parse_time_offset_minutes(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip()
    match = re.fullmatch(r"([+-]?)(\d{1,2}):(\d{2})", raw)
    if not match:
        raise RuntimeError(f"{name} must match [+|-]HH:MM")
    sign_raw, hours_raw, minutes_raw = match.groups()
    hours = int(hours_raw)
    minutes = int(minutes_raw)
    if hours > 23:
        raise RuntimeError(f"{name} hours must be <= 23")
    if minutes > 59:
        raise RuntimeError(f"{name} minutes must be <= 59")
    sign = -1 if sign_raw == "-" else 1
    return sign * (hours * 60 + minutes)


@dataclass(slots=True)
class Settings:
    bot_token: str
    allowed_user_ids: set[int]
    default_shell: str
    workdir: Path
    max_tail_lines: int
    max_upload_bytes: int
    max_running_sessions_per_user: int
    max_session_history_per_user: int
    sessions_page_size: int
    detached_session_ttl_seconds: int
    detached_sweep_interval_seconds: int
    time_offset_minutes: int
    log_level: str
    session_db_path: Path


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
        max_running_sessions_per_user=_parse_positive_int_env("MAX_RUNNING_SESSIONS_PER_USER", 3),
        max_session_history_per_user=_parse_positive_int_env("MAX_SESSION_HISTORY_PER_USER", 20),
        sessions_page_size=_parse_positive_int_env("SESSIONS_PAGE_SIZE", 5),
        detached_session_ttl_seconds=_parse_positive_int_env("DETACHED_SESSION_TTL_SECONDS", 3600),
        detached_sweep_interval_seconds=_parse_positive_int_env("DETACHED_SWEEP_INTERVAL_SECONDS", 30),
        time_offset_minutes=_parse_time_offset_minutes("TIME_OFFSET", "+03:30"),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        session_db_path=Path(
            os.getenv("SESSION_DB_PATH", "./session_store.sqlite3")
        ).expanduser(),
    )
