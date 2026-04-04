from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import secrets
from typing import Dict, Literal


ChatRole = Literal["user", "assistant"]


@dataclass(slots=True)
class ChatMessage:
    role: ChatRole
    text: str
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True)
class ChatConversation:
    conversation_id: str
    telegram_user_id: int
    title: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    messages: list[ChatMessage] = field(default_factory=list)


@dataclass(slots=True)
class UsageWindow:
    started_at: datetime
    used: int = 0


@dataclass(slots=True)
class UsageSnapshot:
    used_5h: int
    limit_5h: int
    reset_5h_at: datetime
    used_week: int
    limit_week: int
    reset_week_at: datetime


class ChatManager:
    def __init__(self) -> None:
        self.chat_mode_by_user: Dict[int, bool] = {}
        self.active_chat_by_user: Dict[int, str] = {}
        self.chat_by_id: Dict[str, ChatConversation] = {}
        self.chat_ids_by_user: Dict[int, list[str]] = {}
        self.window_5h_by_user: Dict[int, UsageWindow] = {}
        self.window_week_by_user: Dict[int, UsageWindow] = {}

    def is_chat_mode(self, telegram_user_id: int) -> bool:
        return self.chat_mode_by_user.get(telegram_user_id, False)

    def set_chat_mode(self, telegram_user_id: int, enabled: bool) -> None:
        self.chat_mode_by_user[telegram_user_id] = enabled

    def create_new_chat(self, telegram_user_id: int) -> ChatConversation:
        conversation_id = f"chat_{secrets.token_hex(4)}"
        conversation = ChatConversation(
            conversation_id=conversation_id,
            telegram_user_id=telegram_user_id,
            title="New chat",
        )
        self.chat_by_id[conversation_id] = conversation
        self.chat_ids_by_user.setdefault(telegram_user_id, []).append(conversation_id)
        self.active_chat_by_user[telegram_user_id] = conversation_id
        return conversation

    def get_active_chat(self, telegram_user_id: int) -> ChatConversation | None:
        conversation_id = self.active_chat_by_user.get(telegram_user_id)
        if not conversation_id:
            return None
        return self.chat_by_id.get(conversation_id)

    def get_or_create_active_chat(self, telegram_user_id: int) -> ChatConversation:
        active = self.get_active_chat(telegram_user_id)
        if active:
            return active
        return self.create_new_chat(telegram_user_id)

    def list_user_chats(self, telegram_user_id: int) -> list[ChatConversation]:
        chat_ids = self.chat_ids_by_user.get(telegram_user_id, [])
        items = [self.chat_by_id[cid] for cid in chat_ids if cid in self.chat_by_id]
        items.sort(key=lambda c: c.updated_at, reverse=True)
        return items

    def resume_chat(self, telegram_user_id: int, conversation_id: str) -> ChatConversation | None:
        conversation = self.chat_by_id.get(conversation_id)
        if not conversation:
            return None
        if conversation.telegram_user_id != telegram_user_id:
            return None
        self.active_chat_by_user[telegram_user_id] = conversation_id
        return conversation

    def append_message(self, conversation: ChatConversation, role: ChatRole, text: str) -> None:
        conversation.messages.append(ChatMessage(role=role, text=text))
        conversation.updated_at = datetime.utcnow()
        if conversation.title == "New chat" and role == "user":
            conversation.title = (text.strip() or "New chat")[:60]

    def recent_messages(self, conversation: ChatConversation, max_messages: int) -> list[ChatMessage]:
        if max_messages <= 0:
            return []
        return conversation.messages[-max_messages:]

    def _get_window(
        self,
        storage: Dict[int, UsageWindow],
        telegram_user_id: int,
        duration: timedelta,
        now: datetime,
    ) -> UsageWindow:
        current = storage.get(telegram_user_id)
        if current is None or now >= (current.started_at + duration):
            current = UsageWindow(started_at=now, used=0)
            storage[telegram_user_id] = current
        return current

    def get_usage_snapshot(
        self,
        telegram_user_id: int,
        limit_5h: int,
        limit_week: int,
        now: datetime | None = None,
    ) -> UsageSnapshot:
        now = now or datetime.utcnow()
        window_5h = self._get_window(self.window_5h_by_user, telegram_user_id, timedelta(hours=5), now)
        window_week = self._get_window(self.window_week_by_user, telegram_user_id, timedelta(days=7), now)
        return UsageSnapshot(
            used_5h=window_5h.used,
            limit_5h=limit_5h,
            reset_5h_at=window_5h.started_at + timedelta(hours=5),
            used_week=window_week.used,
            limit_week=limit_week,
            reset_week_at=window_week.started_at + timedelta(days=7),
        )

    def consume_usage(
        self,
        telegram_user_id: int,
        limit_5h: int,
        limit_week: int,
        now: datetime | None = None,
    ) -> tuple[bool, UsageSnapshot]:
        now = now or datetime.utcnow()
        snapshot = self.get_usage_snapshot(
            telegram_user_id=telegram_user_id,
            limit_5h=limit_5h,
            limit_week=limit_week,
            now=now,
        )
        if snapshot.used_5h >= limit_5h or snapshot.used_week >= limit_week:
            return False, snapshot

        window_5h = self.window_5h_by_user[telegram_user_id]
        window_week = self.window_week_by_user[telegram_user_id]
        window_5h.used += 1
        window_week.used += 1

        updated = UsageSnapshot(
            used_5h=window_5h.used,
            limit_5h=limit_5h,
            reset_5h_at=window_5h.started_at + timedelta(hours=5),
            used_week=window_week.used,
            limit_week=limit_week,
            reset_week_at=window_week.started_at + timedelta(days=7),
        )
        return True, updated
