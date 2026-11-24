"""Tests for SQLite persistence, manual-lock guard, and reminder rebuild logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ger.config import AppConfig, ReminderRuleConfig
from ger.db import Database
from ger.models import EventStatus, RawEvent, ReminderStatus
from ger.reminders import rebuild_reminders_for_event


def _make_config() -> AppConfig:
    return AppConfig(
        timezone="Asia/Shanghai",
        reminders=ReminderRuleConfig(
            start_offsets=["0m"],
            end_offsets=["-24h", "-2h"],
            grace_period_minutes=60,
        ),
    )


def test_upsert_and_manual_lock_protection(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    cfg = _make_config()
    now = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)

    raw = RawEvent(
        source_id="gameg",
        external_key="genshin:cn:ev-1",
        game="原神",
        title="深渊挑战",
        start_time=now + timedelta(hours=10),
        end_time=now + timedelta(days=5),
    )
    ev, action = db.upsert_from_source(raw)
    assert action == "inserted"
    assert not ev.manual_locked

    # User edits end_time manually -> should lock event
    edited = db.update_event(ev.id, fields={"end_time": now + timedelta(days=7)})
    assert edited is not None
    assert edited.manual_locked is True

    # Subsequent automated sync should not overwrite manual changes
    raw_updated = raw.model_copy(update={"title": "被源修改的标题", "end_time": now + timedelta(days=6)})
    ev2, action2 = db.upsert_from_source(raw_updated)
    assert action2 == "skipped_locked"
    assert ev2.title == "深渊挑战"
    assert ev2.end_time == now + timedelta(days=7)

    # Unlocking allows automated updates again
    db.update_event(ev.id, fields={"manual_locked": False}, lock_manual=False)
    ev3, action3 = db.upsert_from_source(raw_updated)
    assert action3 == "updated"
    assert ev3.title == "被源修改的标题"
    rebuild_reminders_for_event(db, cfg, ev3, now=now)
    reminders = db.list_reminders_for_event(ev3.id)
    assert len(reminders) == 3


def test_reminder_rebuild_skips_historical_and_rearms_extended(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    cfg = _make_config()
    now = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)

    # Event already started 3 days ago, ends in 2 days
    raw = RawEvent(
        source_id="gameg",
        external_key="hsr:cn:ev-1",
        game="崩坏：星穹铁道",
        title="模拟宇宙双倍",
        start_time=now - timedelta(days=3),
        end_time=now + timedelta(days=2),
    )
    ev, _ = db.upsert_from_source(raw)
    rebuild_reminders_for_event(db, cfg, ev, now=now)

    by_key = {r.rule_key: r for r in db.list_reminders_for_event(ev.id)}
    assert by_key["start:0m"].status == ReminderStatus.SKIPPED
    assert by_key["end:-24h"].status == ReminderStatus.PENDING
    assert by_key["end:-2h"].status == ReminderStatus.PENDING

    # Mark end:-24h as sent, then extend the event by 3 days -> should re-arm end:-24h
    db.mark_reminder_status(by_key["end:-24h"].id, ReminderStatus.SENT, sent_at=now)
    ev_extended = db.update_event(ev.id, fields={"end_time": now + timedelta(days=5)})
    assert ev_extended is not None
    rebuild_reminders_for_event(db, cfg, ev_extended, now=now)

    by_key_after = {r.rule_key: r for r in db.list_reminders_for_event(ev.id)}
    assert by_key_after["end:-24h"].status == ReminderStatus.PENDING
    assert by_key_after["end:-24h"].trigger_at == now + timedelta(days=4)

    # Muting the event turns pending reminders into muted
    ev_muted = db.update_event(ev.id, fields={"status": EventStatus.MUTED})
    assert ev_muted is not None
    rebuild_reminders_for_event(db, cfg, ev_muted, now=now)
    statuses = {r.rule_key: r.status for r in db.list_reminders_for_event(ev.id)}
    assert statuses["end:-24h"] == ReminderStatus.MUTED
    assert statuses["end:-2h"] == ReminderStatus.MUTED


def test_cross_source_and_ai_title_drift_dedup(tmp_path) -> None:
    db = Database(tmp_path / "dedup.db")
    start = datetime(2099, 11, 26, 2, 0, tzinfo=timezone.utc)
    end = datetime(2099, 12, 9, 19, 59, tzinfo=timezone.utc)

    # 1. Structured source (gameg.site) inserts authoritative record
    ev_struct, act1 = db.upsert_from_source(
        RawEvent(
            source_id="gameg-site",
            external_key="zzz:cn:neon-op",
            game="绝区零",
            title="霓虹行动",
            event_type="ingame_event",
            start_time=start,
            end_time=end,
            is_structured_source=True,
        )
    )
    assert act1 == "inserted"

    # 2. AI source reports the same event with noisy title and slightly shifted time -> deduped, structured preserved
    ev_ai_dup, act2 = db.upsert_from_source(
        RawEvent(
            source_id="ai-zzz",
            external_key="ai-hash-999",
            game="绝区零",
            title="「霓虹行动」限时活动开启",
            event_type="ingame_event",
            start_time=start + timedelta(hours=1),
            end_time=end - timedelta(hours=1),
            url="https://zzz.mihoyo.com/news/101",
            is_structured_source=False,
        )
    )
    assert act2 == "deduped"
    assert ev_ai_dup.id == ev_struct.id
    assert ev_ai_dup.title == "霓虹行动"
    assert ev_ai_dup.start_time == start  # structured schedule untouched
    assert ev_ai_dup.url == "https://zzz.mihoyo.com/news/101"  # missing URL enriched
    assert len(db.list_events(include_past=True)) == 1

    # 3. AI-only event with title drift across consecutive days -> deduplicated into same row
    ev_web1, act3 = db.upsert_from_source(
        RawEvent(
            source_id="ai-zzz",
            external_key="ai-web-day1",
            game="绝区零",
            title="绳网特派网页活动",
            event_type="web_event",
            start_time=start,
            end_time=end,
            is_structured_source=False,
        )
    )
    assert act3 == "inserted"
    ev_web2, act4 = db.upsert_from_source(
        RawEvent(
            source_id="ai-zzz",
            external_key="ai-web-day2",
            game="绝区零",
            title="「绳网特派」限时网页活动",
            event_type="web_event",
            start_time=start,
            end_time=end,
            is_structured_source=False,
        )
    )
    assert act4 == "deduped"
    assert ev_web2.id == ev_web1.id
    assert len(db.list_events(include_past=True)) == 2


def test_cleanup_ended_events_after_retention_days(tmp_path) -> None:
    db = Database(tmp_path / "cleanup.db")
    cfg = _make_config()
    now = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)

    # Ended 5 days ago -> should be deleted with 3-day retention
    old_ev = db.create_manual_event(
        game="原神",
        title="已过期5天",
        start_time=now - timedelta(days=10),
        end_time=now - timedelta(days=5),
    )
    rebuild_reminders_for_event(db, cfg, old_ev, now=now)

    # Ended 1 day ago -> should remain during 3-day retention window
    recent_ev = db.create_manual_event(
        game="原神",
        title="刚结束1天",
        start_time=now - timedelta(days=6),
        end_time=now - timedelta(days=1),
    )
    rebuild_reminders_for_event(db, cfg, recent_ev, now=now)

    # Ongoing -> must remain
    active_ev = db.create_manual_event(
        game="原神",
        title="进行中活动",
        start_time=now - timedelta(days=1),
        end_time=now + timedelta(days=5),
    )
    rebuild_reminders_for_event(db, cfg, active_ev, now=now)

    deleted = db.cleanup_ended_events(retention_days=3, now=now)
    assert deleted == 1
    remaining_titles = {e.title for e in db.list_events(include_past=True)}
    assert remaining_titles == {"刚结束1天", "进行中活动"}
    assert db.list_reminders_for_event(old_ev.id) == []


def test_type_specific_reminder_rules(tmp_path) -> None:
    db = Database(tmp_path / "type_rules.db")
    cfg = AppConfig(timezone="Asia/Shanghai")
    now = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)

    # checkin_redeem (redeem codes/checkin) defaults to start [0m], end [-24h]
    ev_code = db.create_manual_event(
        game="鸣潮",
        title="2.8 前瞻兑换码",
        event_type="checkin_redeem",
        start_time=now + timedelta(days=2),
        end_time=now + timedelta(days=3),
    )
    rebuild_reminders_for_event(db, cfg, ev_code, now=now)
    keys = {r.rule_key for r in db.list_reminders_for_event(ev_code.id)}
    assert keys == {"start:0m", "end:-24h"}

    # version defaults to start [-24h], end []
    ev_ver = db.create_manual_event(
        game="鸣潮",
        title="2.8 大版本更新",
        event_type="version",
        start_time=now + timedelta(days=5),
        end_time=now + timedelta(days=45),
    )
    rebuild_reminders_for_event(db, cfg, ev_ver, now=now)
    ver_keys = {r.rule_key for r in db.list_reminders_for_event(ev_ver.id)}
    assert ver_keys == {"start:-24h"}

