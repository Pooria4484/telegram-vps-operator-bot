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


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean (1/0, true/false, yes/no)")


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
    telegram_api_file_limit_bytes: int
    telegram_api_base_url: str
    telegram_api_is_local: bool
    max_running_sessions_per_user: int
    max_session_history_per_user: int
    sessions_page_size: int
    detached_session_ttl_seconds: int
    detached_sweep_interval_seconds: int
    time_offset_minutes: int
    log_level: str
    session_db_path: Path
    codex_command: str
    codex_timeout_seconds: int
    codex_artifacts_dir: Path
    codex_model: str
    codex_available_models: list[str]
    codex_reasoning_effort: str
    codex_available_reasoning_efforts: list[str]
    codex_sandbox_mode: str


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing")

    allowed_user_ids = _parse_user_ids(os.getenv("ALLOWED_USER_IDS", ""))

    codex_model = os.getenv("CODEX_MODEL", "").strip()
    raw_models = os.getenv("CODEX_AVAILABLE_MODELS", "").strip()
    if raw_models:
        codex_available_models = [item.strip() for item in raw_models.split(",") if item.strip()]
    elif codex_model:
        codex_available_models = [codex_model]
    else:
        codex_available_models = ["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2"]

    codex_reasoning_effort = os.getenv("CODEX_REASONING_EFFORT", "").strip()
    raw_efforts = os.getenv("CODEX_AVAILABLE_REASONING_EFFORTS", "").strip()
    if raw_efforts:
        codex_available_reasoning_efforts = [
            item.strip().lower() for item in raw_efforts.split(",") if item.strip()
        ]
    elif codex_reasoning_effort:
        codex_available_reasoning_efforts = [codex_reasoning_effort.lower()]
    else:
        codex_available_reasoning_efforts = ["low", "medium", "high", "xhigh"]

    telegram_api_is_local = _parse_bool_env("TELEGRAM_API_IS_LOCAL", False)
    default_telegram_api_file_limit = 2 * 1024 * 1024 * 1024 if telegram_api_is_local else 20 * 1024 * 1024

    return Settings(
        bot_token=bot_token,
        allowed_user_ids=allowed_user_ids,
        default_shell=os.getenv("DEFAULT_SHELL", "/bin/bash").strip(),
        workdir=Path(os.getenv("WORKDIR", str(Path.home()))).expanduser(),
        max_tail_lines=_parse_positive_int_env("MAX_TAIL_LINES", 30),
        max_upload_bytes=_parse_positive_int_env("MAX_UPLOAD_BYTES", 1024 * 1024 * 1024),
        telegram_api_file_limit_bytes=_parse_positive_int_env(
            "TELEGRAM_API_FILE_LIMIT_BYTES", default_telegram_api_file_limit
        ),
        telegram_api_base_url=os.getenv("TELEGRAM_API_BASE_URL", "").strip(),
        telegram_api_is_local=telegram_api_is_local,
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
        codex_command=os.getenv("CODEX_COMMAND", "codex").strip() or "codex",
        codex_timeout_seconds=_parse_positive_int_env("CODEX_TIMEOUT_SECONDS", 1800),
        codex_artifacts_dir=Path(
            os.getenv("CODEX_ARTIFACTS_DIR", "./codex_artifacts")
        ).expanduser(),
        codex_model=codex_model,
        codex_available_models=codex_available_models,
        codex_reasoning_effort=codex_reasoning_effort.lower(),
        codex_available_reasoning_efforts=codex_available_reasoning_efforts,
        codex_sandbox_mode=os.getenv("CODEX_SANDBOX_MODE", "workspace-write").strip() or "workspace-write",
    )
