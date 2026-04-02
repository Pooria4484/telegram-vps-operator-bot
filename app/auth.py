from __future__ import annotations

from app.config import Settings


def is_allowed(user_id: int, settings: Settings) -> bool:
    return user_id in settings.allowed_user_ids
