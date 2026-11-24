"""Reminder node calculation and idempotent rebuild engine."""

from __future__ import annotations

from datetime import datetime, timedelta

from ger.config import AppConfig
from ger.db import Database
from ger.models import EventRecord, EventStatus, ReminderStatus
from ger.time_utils import format_utc_iso, parse_iso_datetime, parse_offset, utc_now


def compute_desired_reminders(
    event: EventRecord,
    config: AppConfig,
) -> dict[str, tuple[str, str, datetime]]:
    """Build {rule_key: (anchor, offset_label, trigger_at_utc)} for an event."""
    rule = config.get_reminder_rule(event.game, event.event_type)
    desired: dict[str, tuple[str, str, datetime]] = {}

    if event.start_time is not None:
        for offset_str in rule.start_offsets:
            delta = parse_offset(offset_str)
            trigger_at = event.start_time + delta
            desired[f"start:{offset_str}"] = ("start", offset_str, trigger_at)

    if event.end_time is not None:
        for offset_str in rule.end_offsets:
            delta = parse_offset(offset_str)
            trigger_at = event.end_time + delta
            desired[f"end:{offset_str}"] = ("end", offset_str, trigger_at)

    return desired


def rebuild_reminders_for_event(
    db: Database,
    config: AppConfig,
    event: EventRecord,
    *,
    now: datetime | None = None,
) -> None:
    """Idempotently synchronize reminder rows for a single event.

    - Muted/done events have all pending reminders moved to 'muted'.
    - Active events compute desired reminder nodes.
    - Nodes that are already past `now - grace_period` are marked 'skipped' to avoid
      flooding notifications for historical events on first import.
    - Already-sent nodes stay 'sent' unless the event's schedule shifted to a new future time.
    """
    current = now or utc_now()
    now_iso = format_utc_iso(current)
    rule = config.get_reminder_rule(event.game, event.event_type)
    lookback_minutes = rule.grace_period_minutes
    if config.notifications.mode == "digest":
        lookback_minutes = max(lookback_minutes, config.notifications.digest_lookback_hours * 60)
    grace_cutoff = current - timedelta(minutes=lookback_minutes)
    event_already_ended = event.end_time is not None and event.end_time <= current

    with db.connect() as conn:
        if event.status != EventStatus.ACTIVE:
            conn.execute(
                """
                UPDATE reminders
                SET status = ?, updated_at = ?
                WHERE event_id = ? AND status IN (?, ?)
                """,
                (
                    ReminderStatus.MUTED.value,
                    now_iso,
                    event.id,
                    ReminderStatus.PENDING.value,
                    ReminderStatus.QUEUED.value,
                ),
            )
            return

        desired = compute_desired_reminders(event, config)
        existing_rows = conn.execute(
            "SELECT * FROM reminders WHERE event_id = ?",
            (event.id,),
        ).fetchall()
        existing_by_key = {row["rule_key"]: row for row in existing_rows}

        # Remove obsolete non-sent reminders whose rule no longer applies
        for rule_key, row in existing_by_key.items():
            if rule_key not in desired and row["status"] != ReminderStatus.SENT.value:
                conn.execute("DELETE FROM reminders WHERE id = ?", (row["id"],))

        for rule_key, (anchor, offset_label, trigger_at) in desired.items():
            trigger_iso = format_utc_iso(trigger_at)
            # For start reminders on already-started events, skip if the start time itself has passed
            # beyond grace_period_minutes (so ongoing events imported mid-way only trigger end reminders).
            start_already_past = (
                anchor == "start"
                and event.start_time is not None
                and event.start_time < current - timedelta(minutes=rule.grace_period_minutes)
            )
            is_stale = event_already_ended or start_already_past or trigger_at < grace_cutoff
            initial_status = (
                ReminderStatus.SKIPPED.value if is_stale else ReminderStatus.PENDING.value
            )

            existing = existing_by_key.get(rule_key)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO reminders (
                        event_id, rule_key, anchor, offset_label, trigger_at,
                        status, retry_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        event.id,
                        rule_key,
                        anchor,
                        offset_label,
                        trigger_iso,
                        initial_status,
                        now_iso,
                        now_iso,
                    ),
                )
                continue

            if existing["status"] == ReminderStatus.SENT.value:
                old_trigger = parse_iso_datetime(existing["trigger_at"])
                if abs((trigger_at - old_trigger).total_seconds()) >= 60 and trigger_at > current:
                    conn.execute(
                        """
                        UPDATE reminders
                        SET trigger_at = ?, status = ?, retry_count = 0,
                            last_error = NULL, sent_at = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (trigger_iso, ReminderStatus.PENDING.value, now_iso, existing["id"]),
                    )
                continue

            # Preserve queued status if trigger_at did not move into the future
            new_status = initial_status
            if existing["status"] == ReminderStatus.QUEUED.value and not is_stale and trigger_at <= current:
                new_status = ReminderStatus.QUEUED.value
            elif existing["status"] == ReminderStatus.FAILED.value and existing["trigger_at"] == trigger_iso:
                new_status = ReminderStatus.FAILED.value

            conn.execute(
                """
                UPDATE reminders
                SET anchor = ?, offset_label = ?, trigger_at = ?,
                    status = ?, updated_at = ?
                WHERE id = ?
                """,
                (anchor, offset_label, trigger_iso, new_status, now_iso, existing["id"]),
            )
