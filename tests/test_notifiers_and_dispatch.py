"""Tests for ntfy/Bark notifiers and reminder dispatch retry/grace-period behavior."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ger.config import AppConfig, NotificationConfig, NotifierConfig, ReminderRuleConfig
from ger.db import Database
from ger.dispatcher import dispatch_due_reminders, enqueue_due_reminders
from ger.models import ReminderStatus
from ger.reminders import rebuild_reminders_for_event


@pytest.mark.asyncio
async def test_dispatch_sends_ntfy_and_bark_json(tmp_path) -> None:
    sent_requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append((str(request.url), json.loads(request.content.decode("utf-8"))))
        return httpx.Response(200, json={"code": 200})

    cfg = AppConfig(
        timezone="Asia/Shanghai",
        notifications=NotificationConfig(enabled=True, mode="realtime"),
        reminders=ReminderRuleConfig(start_offsets=["0m"], end_offsets=["-2h"], grace_period_minutes=60),
        notifiers=[
            NotifierConfig(
                id="ntfy-1",
                type="ntfy",
                server="https://ntfy.example.com",
                topic="game-alerts",
                token="tk-123",
            ),
            NotifierConfig(
                id="bark-1",
                type="bark",
                server="https://bark.example.com",
                device_key="dev-abc",
                sound="bell",
            ),
        ],
    )
    db = Database(tmp_path / "dispatch.db")
    base = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    ev = db.create_manual_event(
        game="原神",
        title="幽境危战",
        start_time=base - timedelta(minutes=5),
        end_time=base + timedelta(days=2),
        url="https://ys.mihoyo.com/",
    )
    rebuild_reminders_for_event(db, cfg, ev, now=base)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await dispatch_due_reminders(db, cfg, now=base, client=client)

    assert report.checked == 1
    assert report.sent == 1
    assert report.failed == 0
    assert len(sent_requests) == 2

    ntfy_url, ntfy_body = sent_requests[0]
    assert ntfy_url == "https://ntfy.example.com"
    assert ntfy_body["topic"] == "game-alerts"
    assert "幽境危战" in ntfy_body["title"]
    assert ntfy_body["click"] == "https://ys.mihoyo.com/"

    bark_url, bark_body = sent_requests[1]
    assert bark_url == "https://bark.example.com/push"
    assert bark_body["device_key"] == "dev-abc"
    assert bark_body["sound"] == "bell"
    assert bark_body["url"] == "https://ys.mihoyo.com/"

    # Running dispatch again should find zero due pending reminders
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report2 = await dispatch_due_reminders(db, cfg, now=base, client=client)
    assert report2.checked == 0


@pytest.mark.asyncio
async def test_dispatch_retries_and_marks_failed(tmp_path) -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    cfg = AppConfig(
        notifications=NotificationConfig(enabled=True, mode="realtime"),
        reminders=ReminderRuleConfig(start_offsets=["0m"], end_offsets=[], grace_period_minutes=60),
        notifiers=[NotifierConfig(id="ntfy-1", type="ntfy", server="https://ntfy.example.com", topic="t")],
    )
    db = Database(tmp_path / "retry.db")
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    ev = db.create_manual_event(game="鸣潮", title="测试活动", start_time=now - timedelta(minutes=1))
    rebuild_reminders_for_event(db, cfg, ev, now=now)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)) as client:
        for expected_retry in (1, 2, 3):
            rep = await dispatch_due_reminders(db, cfg, now=now, client=client)
            assert rep.failed == 1
            rem = db.list_reminders_for_event(ev.id)[0]
            assert rem.retry_count == expected_retry
            if expected_retry < 3:
                assert rem.status == ReminderStatus.PENDING
            else:
                assert rem.status == ReminderStatus.FAILED


@pytest.mark.asyncio
async def test_digest_mode_consolidates_multiple_events_and_respects_master_switch(tmp_path) -> None:
    sent_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True})

    cfg = AppConfig(
        timezone="Asia/Shanghai",
        notifications=NotificationConfig(
            enabled=True,
            mode="digest",
            digest_cron="15 12 * * *",
            digest_lookback_hours=26,
        ),
        reminders=ReminderRuleConfig(start_offsets=["-24h"], end_offsets=["-24h"]),
        notifiers=[NotifierConfig(id="ntfy-1", type="ntfy", server="https://ntfy.example.com", topic="t")],
    )
    db = Database(tmp_path / "digest.db")
    now = datetime(2026, 11, 24, 4, 15, tzinfo=timezone.utc)  # 12:15 UTC+8

    # Event 1: starts in 18h -> start:-24h triggered 6h ago (within 26h digest window)
    ev1 = db.create_manual_event(
        game="原神",
        title="星海巡游",
        event_type="web_event",
        start_time=now + timedelta(hours=18),
        end_time=now + timedelta(days=7),
    )
    # Event 2: ends in 12h -> end:-24h triggered 12h ago
    ev2 = db.create_manual_event(
        game="崩坏：星穹铁道",
        title="前瞻兑换码",
        event_type="checkin_redeem",
        start_time=now - timedelta(hours=12),
        end_time=now + timedelta(hours=12),
        notes="CODE1 / CODE2 / CODE3",
    )
    rebuild_reminders_for_event(db, cfg, ev1, now=now)
    rebuild_reminders_for_event(db, cfg, ev2, now=now)

    # Step 1: enqueue_only puts both due items into 'queued' without sending
    checked, queued, skipped = enqueue_due_reminders(db, cfg, now=now)
    assert queued >= 2
    assert len(db.list_queued_reminders()) >= 2

    # Step 2: when notifications.enabled=False, flush sends nothing
    cfg_disabled = cfg.model_copy(
        update={"notifications": cfg.notifications.model_copy(update={"enabled": False})}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rep_off = await dispatch_due_reminders(db, cfg_disabled, now=now, client=client, flush_digest=True)
    assert rep_off.notifications_enabled is False
    assert rep_off.sent == 0
    assert len(sent_payloads) == 0

    # Step 3: when notifications.enabled=True, flush merges all queued reminders into 1 message
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rep_on = await dispatch_due_reminders(db, cfg, now=now, client=client, flush_digest=True)
    assert rep_on.digest_messages_sent == 1
    assert rep_on.sent >= 2
    assert len(sent_payloads) == 1
    body = sent_payloads[0]["message"]
    assert "星海巡游" in body
    assert "前瞻兑换码" in body
    assert "CODE1 / CODE2 / CODE3" in body
    assert db.list_queued_reminders() == []

    # Step 4: flushing again with empty queue sends nothing (zero spam)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rep_empty = await dispatch_due_reminders(db, cfg, now=now, client=client, flush_digest=True)
    assert rep_empty.digest_messages_sent == 0
    assert len(sent_payloads) == 1
