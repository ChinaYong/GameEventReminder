"""Domain models for events, reminders, and source sync results."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    INGAME_EVENT = "ingame_event"
    WEB_EVENT = "web_event"
    CHECKIN_REDEEM = "checkin_redeem"
    BANNER = "banner"
    VERSION = "version"


DEFAULT_ENABLED_EVENT_TYPES: list[str] = [t.value for t in EventType]

_EVENT_TYPE_ALIASES: dict[str, str] = {
    # ingame_event
    "ingame_event": EventType.INGAME_EVENT.value,
    "ingame": EventType.INGAME_EVENT.value,
    "in_game": EventType.INGAME_EVENT.value,
    "event": EventType.INGAME_EVENT.value,
    "activity": EventType.INGAME_EVENT.value,
    "game_event": EventType.INGAME_EVENT.value,
    # web_event
    "web_event": EventType.WEB_EVENT.value,
    "web": EventType.WEB_EVENT.value,
    "h5": EventType.WEB_EVENT.value,
    "h5_event": EventType.WEB_EVENT.value,
    # checkin_redeem (redeem codes after livestreams & checkin/welfare deadlines;
    # pure livestream broadcast notices are kept as "livestream" and excluded by default)
    "checkin_redeem": EventType.CHECKIN_REDEEM.value,
    "checkin": EventType.CHECKIN_REDEEM.value,
    "redeem": EventType.CHECKIN_REDEEM.value,
    "code": EventType.CHECKIN_REDEEM.value,
    "welfare": EventType.CHECKIN_REDEEM.value,
    # pure livestream broadcast notices (not in DEFAULT_ENABLED_EVENT_TYPES)
    "livestream": "livestream",
    "stream": "livestream",
    "program": "livestream",
    # banner
    "banner": EventType.BANNER.value,
    "gacha": EventType.BANNER.value,
    "wish": EventType.BANNER.value,
    "warp": EventType.BANNER.value,
    "pool": EventType.BANNER.value,
    # version
    "version": EventType.VERSION.value,
    "maintenance": EventType.VERSION.value,
    "update": EventType.VERSION.value,
    "patch": EventType.VERSION.value,
}


def normalize_event_type(raw_type: str | None) -> str:
    """Normalize source/LLM event type strings into the 5 canonical categories."""
    cleaned = (raw_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _EVENT_TYPE_ALIASES.get(cleaned, cleaned or EventType.INGAME_EVENT.value)


class EventStatus(StrEnum):
    ACTIVE = "active"
    MUTED = "muted"
    DONE = "done"


class ReminderStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
    MUTED = "muted"


class RawEvent(BaseModel):
    """Normalized event output produced by any Source provider."""

    source_id: str
    external_key: str
    game: str
    title: str
    event_type: str = EventType.INGAME_EVENT.value
    start_time: datetime | None = None
    end_time: datetime | None = None
    url: str | None = None
    notes: str | None = None
    needs_review: bool = False
    is_structured_source: bool = False
    raw_payload: dict[str, Any] | None = None


class EventRecord(BaseModel):
    """Persisted event record in SQLite."""

    id: int
    source_id: str
    external_key: str
    game: str
    title: str
    event_type: str = EventType.INGAME_EVENT.value
    start_time: datetime | None = None
    end_time: datetime | None = None
    url: str | None = None
    notes: str | None = None
    status: EventStatus = EventStatus.ACTIVE
    manual_locked: bool = False
    needs_review: bool = False
    created_at: datetime
    updated_at: datetime


class ReminderRecord(BaseModel):
    """Persisted reminder node tied to an event."""

    id: int
    event_id: int
    rule_key: str
    anchor: str  # "start" or "end"
    offset_label: str  # e.g. "-24h", "0m"
    trigger_at: datetime
    status: ReminderStatus = ReminderStatus.PENDING
    retry_count: int = 0
    last_error: str | None = None
    sent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SyncSourceSummary(BaseModel):
    source_id: str
    ok: bool
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    deduped: int = 0
    skipped_locked: int = 0
    skipped_expired: int = 0
    skipped_filtered: int = 0
    needs_review: int = 0
    error: str | None = None


class SyncReport(BaseModel):
    started_at: datetime
    finished_at: datetime
    cleaned_events: int = 0
    muted_unwatched: int = 0
    restored_watched: int = 0
    sources: list[SyncSourceSummary] = Field(default_factory=list)


class DispatchReport(BaseModel):
    mode: str = "digest"
    notifications_enabled: bool = True
    checked: int = 0
    queued: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    digest_messages_sent: int = 0
