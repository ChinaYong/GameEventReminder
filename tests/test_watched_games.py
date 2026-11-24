"""Tests for global watched_games master switch, source slug/gid mapping, and auto-mute/restore."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ger.config import AppConfig, NotifierConfig, ReminderRuleConfig, SourceConfig
from ger.db import Database
from ger.dispatcher import dispatch_due_reminders
from ger.models import EventStatus, RawEvent, ReminderStatus
from ger.reminders import rebuild_reminders_for_event
from ger.sources.gameg_site import DEFAULT_GAMEG_NAME_MAP, resolve_gameg_target_slugs
from ger.sync import run_sync


def test_gameg_slug_reverse_mapping_from_watched_games() -> None:
    # Auto reverse-lookup from watched_games when source options.games is omitted
    slugs = resolve_gameg_target_slugs(
        configured_games=set(),
        watched_games=["原神", "绝区零"],
        game_name_map=DEFAULT_GAMEG_NAME_MAP,
    )
    assert slugs is not None
    assert "genshin-impact" in slugs
    assert "zenless-zone-zero" in slugs
    assert "ZenlessZoneZero" in slugs
    assert "arknights" not in slugs

    # Intersection when source also specifies a subset
    subset = resolve_gameg_target_slugs(
        configured_games={"genshin-impact", "arknights"},
        watched_games=["原神"],
        game_name_map=DEFAULT_GAMEG_NAME_MAP,
    )
    assert subset == {"genshin-impact"}

    # Empty set when master switch excludes everything in options.games
    excluded = resolve_gameg_target_slugs(
        configured_games={"arknights"},
        watched_games=["原神"],
        game_name_map=DEFAULT_GAMEG_NAME_MAP,
    )
    assert excluded == set()


@pytest.mark.asyncio
async def test_unwatched_game_mutes_without_deleting_and_restores_on_rewatch(tmp_path) -> None:
    db = Database(tmp_path / "watched.db")
    now = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    base_cfg = AppConfig(
        db_path=str(tmp_path / "watched.db"),
        watched_games=["原神", "明日方舟"],
        reminders=ReminderRuleConfig(start_offsets=["0m"], end_offsets=["-24h"], grace_period_minutes=60),
        notifiers=[NotifierConfig(id="ntfy", type="ntfy", server="https://ntfy.example.com", topic="t")],
    )

    # Seed two active events (原神 + 明日方舟) and one manually muted event (明日方舟)
    # Verify digest mode sends only 1 consolidated message containing 原神活动 and excludes unwatched 明日方舟
    ev_ys, _ = db.upsert_from_source(
        RawEvent(
            source_id="gameg-site",
            external_key="ys:1",
            game="原神",
            title="原神活动",
            start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(days=5),
            is_structured_source=True,
        )
    )
    ev_ark_active, _ = db.upsert_from_source(
        RawEvent(
            source_id="gameg-site",
            external_key="ark:1",
            game="明日方舟",
            title="方舟活动A",
            start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(days=5),
            is_structured_source=True,
        )
    )
    ev_ark_manual_mute, _ = db.upsert_from_source(
        RawEvent(
            source_id="gameg-site",
            external_key="ark:2",
            game="明日方舟",
            title="方舟手动静音活动",
            start_time=now + timedelta(days=1),
            end_time=now + timedelta(days=5),
            is_structured_source=True,
        )
    )
    db.update_event(ev_ark_manual_mute.id, fields={"status": EventStatus.MUTED}, lock_manual=True)
    for ev in (ev_ys, ev_ark_active, ev_ark_manual_mute):
        rebuild_reminders_for_event(db, base_cfg, db.get_event(ev.id), now=now)

    # Remove 明日方舟 from watched_games -> dispatch must immediately mute it and NOT notify for it
    cfg_removed = base_cfg.model_copy(update={"watched_games": ["原神"]})
    sent_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rep = await dispatch_due_reminders(db, cfg_removed, now=now, client=client)

    assert rep.sent == 1
    assert len(sent_payloads) == 1
    assert "原神活动" in sent_payloads[0]["message"]
    assert "方舟活动A" not in sent_payloads[0]["message"]

    # 方舟活动A is muted (NOT deleted!) and its pending reminders are muted
    ark_a = db.get_event(ev_ark_active.id)
    assert ark_a is not None
    assert ark_a.status == EventStatus.MUTED
    assert all(r.status == ReminderStatus.MUTED for r in db.list_reminders_for_event(ark_a.id))

    # Re-add 明日方舟 to watched_games -> 方舟活动A auto-restores, but 方舟手动静音活动 stays muted!
    cfg_restored = base_cfg.model_copy(update={"watched_games": ["原神", "明日方舟"]})
    muted_cnt, restored_list = db.reconcile_watched_games(cfg_restored.watched_games)
    assert muted_cnt == 0
    assert {e.id for e in restored_list} == {ev_ark_active.id}
    assert db.get_event(ev_ark_active.id).status == EventStatus.ACTIVE
    assert db.get_event(ev_ark_manual_mute.id).status == EventStatus.MUTED

    # Eventually after event ends + retention days, cleanup removes it
    deleted = db.cleanup_ended_events(retention_days=3, now=now + timedelta(days=10))
    assert deleted == 3


@pytest.mark.asyncio
async def test_gameg_source_only_fetches_watched_game_directories(tmp_path) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested_paths.append(url)
        if "git/trees" in url:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"type": "blob", "path": "data/genshin-impact/cn-2026.yaml"},
                        {"type": "blob", "path": "data/genshin-impact/meta.yaml"},
                        {"type": "blob", "path": "data/arknights/cn-2026.yaml"},
                    ]
                },
            )
        return httpx.Response(
            200,
            text=(
                "game: genshin-impact\n"
                "region: cn\n"
                "events:\n"
                "  - id: ev-ys\n"
                "    name: '海灯节'\n"
                "    type: event\n"
                "    start: '2099-01-20T10:00:00+08:00'\n"
                "    end: '2099-02-05T03:59:00+08:00'\n"
            ),
        )

    cfg = AppConfig(
        db_path=str(tmp_path / "g.db"),
        watched_games=["原神"],
        sources=[SourceConfig(id="gameg-site", type="gameg_site", options={})],
    )
    db = Database(cfg.db_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_sync(db, cfg, client=client)

    assert report.sources[0].inserted == 1
    assert any("genshin-impact/cn-2026.yaml" in p for p in requested_paths)
    assert not any("meta.yaml" in p for p in requested_paths)
    assert not any("arknights" in p for p in requested_paths)
