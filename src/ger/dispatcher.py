"""Reminder queueing, consolidated daily digest builder, and notification dispatcher."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from ger.config import AppConfig
from ger.db import Database
from ger.models import DispatchReport, EventRecord, ReminderRecord, ReminderStatus
from ger.notifiers import BaseNotifier, NotificationMessage, create_notifier
from ger.reminders import rebuild_reminders_for_event
from ger.time_utils import format_local, get_zone, utc_now

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def build_enabled_notifiers(config: AppConfig) -> list[BaseNotifier]:
    return [create_notifier(n) for n in config.notifiers if n.enabled]


def _format_relative_remaining(target: datetime | None, now: datetime) -> str:
    if target is None:
        return ""
    delta_secs = int((target - now).total_seconds())
    if delta_secs <= 0:
        return "已到点"
    mins = delta_secs // 60
    days, rem_m = divmod(mins, 1440)
    hours, m = divmod(rem_m, 60)
    if days > 0:
        return f"{days}天{hours}小时"
    if hours > 0:
        return f"{hours}小时{m}分"
    return f"{m}分钟"


def format_reminder_message(
    event: EventRecord,
    reminder: ReminderRecord,
    tz_name: str,
) -> NotificationMessage:
    anchor_cn = "开始" if reminder.anchor == "start" else "结束"
    offset = reminder.offset_label.strip()
    if offset in ("0", "0m", "+0m", "-0m", "0h"):
        timing_label = f"即将{anchor_cn}"
    elif offset.startswith("-"):
        timing_label = f"距{anchor_cn}剩余 {offset[1:]}"
    else:
        timing_label = f"{anchor_cn}后 {offset.lstrip('+')}"

    title = f"[{event.game}] {timing_label}：{event.title}"
    lines = [
        f"游戏：{event.game}",
        f"活动：{event.title} ({event.event_type})",
        f"开始：{format_local(event.start_time, tz_name)}",
        f"结束：{format_local(event.end_time, tz_name)}",
    ]
    if event.notes:
        lines.append(f"备注：{event.notes}")

    priority = 4 if reminder.anchor == "end" else 3
    tags = ["alarm_clock", "video_game"] if reminder.anchor == "end" else ["calendar", "video_game"]

    return NotificationMessage(
        title=title,
        body="\n".join(lines),
        url=event.url,
        priority=priority,
        tags=tags,
        group=event.game,
    )


def format_digest_message(
    items: list[tuple[ReminderRecord, EventRecord]],
    tz_name: str,
    now: datetime,
) -> NotificationMessage:
    """Merge all queued reminder events into a single clean daily digest notification."""
    local_date = now.astimezone(get_zone(tz_name)).strftime("%Y-%m-%d")
    ending_items = [pair for pair in items if pair[0].anchor == "end"]
    starting_items = [pair for pair in items if pair[0].anchor != "end"]

    ending_items.sort(
        key=lambda pair: pair[1].end_time or pair[0].trigger_at
    )
    starting_items.sort(
        key=lambda pair: pair[1].start_time or pair[0].trigger_at
    )

    lines: list[str] = []
    if ending_items:
        lines.append("【即将截止】")
        for _, ev in ending_items:
            rem = _format_relative_remaining(ev.end_time, now)
            end_s = format_local(ev.end_time, tz_name).rsplit(" ", 1)[0]
            lines.append(f"• [{ev.game}] {ev.title}（剩 {rem}，截止 {end_s}）")
            if ev.notes:
                lines.append(f"  备注：{ev.notes}")

    if starting_items:
        if lines:
            lines.append("")
        lines.append("【即将开始 / 新福利】")
        for _, ev in starting_items:
            rem = _format_relative_remaining(ev.start_time, now)
            start_s = format_local(ev.start_time, tz_name).rsplit(" ", 1)[0]
            time_part = f"{rem}后开始，{start_s}" if rem != "已到点" else f"已开启，{start_s}"
            lines.append(f"• [{ev.game}] {ev.title}（{time_part}）")
            if ev.notes:
                lines.append(f"  备注：{ev.notes}")

    title = f"[游戏活动汇总] 共 {len(items)} 项提醒（{local_date}）"
    priority = 4 if ending_items else 3
    first_url = next((ev.url for _, ev in ending_items + starting_items if ev.url), None)

    return NotificationMessage(
        title=title,
        body="\n".join(lines),
        url=first_url,
        priority=priority,
        tags=["calendar", "video_game"],
        group="GameEvents",
    )


async def send_notification_to_all(
    config: AppConfig,
    message: NotificationMessage,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[tuple[str, str | None]]:
    """Send a message to all enabled notifiers. Returns list of (notifier_id, error_or_none)."""
    notifiers = build_enabled_notifiers(config)
    if not notifiers:
        raise RuntimeError("No enabled notifiers configured")

    outcomes: list[tuple[str, str | None]] = []

    async def _run(http_client: httpx.AsyncClient) -> None:
        for notifier in notifiers:
            try:
                await notifier.send(http_client, message)
                outcomes.append((notifier.notifier_id, None))
            except Exception as exc:
                logger.error("Notifier '%s' failed: %s", notifier.notifier_id, exc)
                outcomes.append((notifier.notifier_id, str(exc)))

    if client is not None:
        await _run(client)
    else:
        async with httpx.AsyncClient() as owned_client:
            await _run(owned_client)

    return outcomes


def enqueue_due_reminders(
    db: Database,
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> tuple[int, int, int]:
    """Move due pending reminders into the 'queued' digest queue (or 'skipped' if stale).

    Returns (checked, queued, skipped).
    """
    current = now or utc_now()
    _, restored = db.reconcile_watched_games(config.watched_games)
    for ev in restored:
        rebuild_reminders_for_event(db, config, ev, now=current)

    due_items = db.list_due_reminders(current)
    queued = 0
    skipped = 0

    for reminder, event in due_items:
        rule = config.get_reminder_rule(event.game, event.event_type)
        lookback_minutes = max(
            rule.grace_period_minutes, config.notifications.digest_lookback_hours * 60
        )
        cutoff = current - timedelta(minutes=lookback_minutes)
        event_ended = event.end_time is not None and event.end_time <= current

        if event_ended or reminder.trigger_at < cutoff:
            db.mark_reminder_status(
                reminder.id,
                ReminderStatus.SKIPPED,
                last_error="Event already ended or past digest lookback window",
            )
            skipped += 1
            continue

        db.mark_reminder_status(reminder.id, ReminderStatus.QUEUED, last_error=None)
        queued += 1

    return len(due_items), queued, skipped


async def flush_digest_queue(
    db: Database,
    config: AppConfig,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> DispatchReport:
    """Enqueue any due reminders and send all queued reminders as a single consolidated message."""
    current = now or utc_now()
    checked, newly_queued, skipped = enqueue_due_reminders(db, config, now=current)
    report = DispatchReport(
        mode="digest",
        notifications_enabled=config.notifications.enabled,
        checked=checked,
        queued=newly_queued,
        skipped=skipped,
    )

    if not config.notifications.enabled:
        logger.info("Global notifications.enabled is false; digest queue held without sending")
        return report

    queued_items = db.list_queued_reminders()
    valid_items: list[tuple[ReminderRecord, EventRecord]] = []
    for rem, ev in queued_items:
        if ev.end_time is not None and ev.end_time <= current:
            db.mark_reminder_status(
                rem.id, ReminderStatus.SKIPPED, last_error="Event ended before digest flush"
            )
            report.skipped += 1
            continue
        valid_items.append((rem, ev))

    if not valid_items:
        return report

    notifiers = build_enabled_notifiers(config)
    if not notifiers:
        logger.warning(
            "Found %d queued reminders for digest, but no enabled notifiers configured",
            len(valid_items),
        )
        return report

    msg = format_digest_message(valid_items, config.timezone, current)

    async def _deliver(http_client: httpx.AsyncClient) -> None:
        errors: list[str] = []
        delivered_any = False
        for notifier in notifiers:
            try:
                await notifier.send(http_client, msg)
                delivered_any = True
            except Exception as exc:
                logger.error("Digest delivery via '%s' failed: %s", notifier.notifier_id, exc)
                errors.append(f"{notifier.notifier_id}: {exc}")

        if delivered_any:
            err_note = "; ".join(errors) if errors else None
            for rem, _ in valid_items:
                db.mark_reminder_status(
                    rem.id,
                    ReminderStatus.SENT,
                    sent_at=current,
                    last_error=err_note,
                )
            report.sent = len(valid_items)
            report.digest_messages_sent = 1
        else:
            err_note = "; ".join(errors)
            for rem, _ in valid_items:
                next_retry = rem.retry_count + 1
                new_status = (
                    ReminderStatus.FAILED if next_retry >= MAX_RETRIES else ReminderStatus.QUEUED
                )
                db.mark_reminder_status(
                    rem.id,
                    new_status,
                    retry_count=next_retry,
                    last_error=err_note,
                )
            report.failed = len(valid_items)

    if client is not None:
        await _deliver(client)
    else:
        async with httpx.AsyncClient() as owned_client:
            await _deliver(owned_client)

    return report


async def dispatch_due_reminders(
    db: Database,
    config: AppConfig,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
    flush_digest: bool = True,
) -> DispatchReport:
    """Process due reminders according to notifications.mode ('digest' or 'realtime').

    - In 'digest' mode:
      * flush_digest=False: only enqueues due reminders into 'queued' (waiting for digest_cron).
      * flush_digest=True: enqueues due reminders and sends 1 consolidated message.
    - In 'realtime' mode:
      * Sends each due reminder immediately (subject to notifications.enabled).
    """
    current = now or utc_now()

    if config.notifications.mode == "digest":
        if flush_digest:
            return await flush_digest_queue(db, config, now=current, client=client)
        checked, newly_queued, skipped = enqueue_due_reminders(db, config, now=current)
        return DispatchReport(
            mode="digest",
            notifications_enabled=config.notifications.enabled,
            checked=checked,
            queued=newly_queued,
            skipped=skipped,
        )

    # Realtime mode
    _, restored = db.reconcile_watched_games(config.watched_games)
    for ev in restored:
        rebuild_reminders_for_event(db, config, ev, now=current)

    due_items = db.list_due_reminders(current)
    report = DispatchReport(
        mode="realtime",
        notifications_enabled=config.notifications.enabled,
        checked=len(due_items),
    )
    if not due_items or not config.notifications.enabled:
        return report

    notifiers = build_enabled_notifiers(config)
    if not notifiers:
        logger.warning("Found %d due reminders, but no enabled notifiers configured", len(due_items))
        return report

    async def _process(http_client: httpx.AsyncClient) -> None:
        for reminder, event in due_items:
            rule = config.get_reminder_rule(event.game, event.event_type)
            grace_cutoff = current - timedelta(minutes=rule.grace_period_minutes)

            if reminder.trigger_at < grace_cutoff:
                db.mark_reminder_status(
                    reminder.id,
                    ReminderStatus.SKIPPED,
                    last_error="Missed grace period window",
                )
                report.skipped += 1
                continue

            msg = format_reminder_message(event, reminder, config.timezone)
            errors: list[str] = []
            delivered_any = False

            for notifier in notifiers:
                try:
                    await notifier.send(http_client, msg)
                    delivered_any = True
                except Exception as exc:
                    logger.error(
                        "Failed to deliver reminder #%d via '%s': %s",
                        reminder.id,
                        notifier.notifier_id,
                        exc,
                    )
                    errors.append(f"{notifier.notifier_id}: {exc}")

            if delivered_any:
                db.mark_reminder_status(
                    reminder.id,
                    ReminderStatus.SENT,
                    sent_at=current,
                    last_error="; ".join(errors) if errors else None,
                )
                report.sent += 1
            else:
                next_retry = reminder.retry_count + 1
                new_status = (
                    ReminderStatus.FAILED if next_retry >= MAX_RETRIES else ReminderStatus.PENDING
                )
                db.mark_reminder_status(
                    reminder.id,
                    new_status,
                    retry_count=next_retry,
                    last_error="; ".join(errors),
                )
                report.failed += 1

    if client is not None:
        await _process(client)
    else:
        async with httpx.AsyncClient() as owned_client:
            await _process(owned_client)

    return report
