"""Tests for ical_feed, miyoushe_news, NFKC normalization, and web-grounded AI dedup review."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from ger.config import (
    AppConfig,
    DedupConfig,
    LLMProviderConfig,
    SearchProviderConfig,
    SourceConfig,
)
from ger.db import Database, normalize_title_for_dedup
from ger.models import RawEvent
from ger.sources.ical_feed import parse_ics_events
from ger.sources.miyoushe import MiYouSheNewsSource
from ger.sync import run_sync


def test_nfkc_title_normalization() -> None:
    a = normalize_title_for_dedup("【限时活动】 ７．０ 「霓虹 行动」！🎮")
    b = normalize_title_for_dedup("7.0 霓虹行动 活动开启")
    assert a == b == "70霓虹行动"


def test_parse_ics_events() -> None:
    sample_ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:ark-ev-1@example.com\r\n"
        "SUMMARY:【明日方舟】SideStory 孤星\r\n"
        " 复刻\r\n"
        "DTSTART;TZID=Asia/Shanghai:20991125T160000\r\n"
        "DTEND;TZID=Asia/Shanghai:20991209T035900\r\n"
        "URL:https://ak.hypergryph.com/news/1\r\n"
        "DESCRIPTION:参与可得源石\\n和限定干员\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:ark-code-1@example.com\r\n"
        "SUMMARY:2099 感谢庆典兑换码截止\r\n"
        "DTSTART:20991120T113000Z\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:ark-stream-only@example.com\r\n"
        "SUMMARY:2099 感谢庆典前瞻直播\r\n"
        "DTSTART:20991119T120000Z\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:ark-bday@example.com\r\n"
        "SUMMARY:阿米娅生日快乐\r\n"
        "DTSTART;VALUE=DATE:20991223\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    events = parse_ics_events(
        sample_ics,
        source_id="ical-ark",
        game="明日方舟",
        include_types={"ingame_event", "checkin_redeem"},
        exclude_keywords=["生日"],
    )
    assert len(events) == 2
    ev1 = next(e for e in events if e.external_key == "ark-ev-1@example.com")
    assert ev1.title == "【明日方舟】SideStory 孤星复刻"
    assert ev1.event_type == "ingame_event"
    assert ev1.is_structured_source is True
    assert ev1.start_time == datetime(2099, 11, 25, 8, 0, tzinfo=timezone.utc)

    ev2 = next(e for e in events if e.external_key == "ark-code-1@example.com")
    assert ev2.event_type == "checkin_redeem"
    assert ev2.end_time is None


@pytest.mark.asyncio
async def test_miyoushe_news_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "miyoushe.com" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "retcode": 0,
                    "data": {
                        "list": [
                            {
                                "post": {
                                    "post_id": "556677",
                                    "subject": "「星海巡游」网页活动现已开启，参与领原石！",
                                    "content": "<p>活动时间：2099-11-26 12:00 至 2099-12-03 23:59</p>",
                                }
                            },
                            {
                                "post": {
                                    "post_id": "556678",
                                    "subject": "社区转发抽奖赢周边",
                                    "content": "<p>关注转发抽奖</p>",
                                }
                            },
                        ]
                    },
                },
            )
        body = json.loads(request.content.decode("utf-8"))
        user_msg = body["messages"][1]["content"]
        # Marketing raffle post should be filtered out by exclude_keywords before reaching LLM
        assert "社区转发抽奖" not in user_msg
        assert "星海巡游" in user_msg
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "events": [
                                        {
                                            "title": "星海巡游网页活动",
                                            "event_type": "web_event",
                                            "start_time": "2099-11-26T12:00:00+08:00",
                                            "end_time": "2099-12-03T23:59:00+08:00",
                                            "url": "https://www.miyoushe.com/ys/article/556677",
                                            "needs_review": False,
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    cfg = AppConfig(
        llm_providers=[
            LLMProviderConfig(
                id="llm",
                type="openai_compat",
                base_url="https://api.example.com/v1",
                api_key="sk",
                model="m",
            )
        ]
    )
    src = MiYouSheNewsSource(
        SourceConfig(
            id="mys-ys",
            type="miyoushe_news",
            options={"gids": 2, "llm_provider": "llm", "news_types": [2]},
        ),
        cfg,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await src.fetch(client)
    assert len(events) == 1
    assert events[0].game == "原神"
    assert events[0].event_type == "web_event"


@pytest.mark.asyncio
async def test_web_grounded_ai_dedup_merges_alias_and_keeps_distinct(tmp_path) -> None:
    """Verify web-grounded AI dedup merges alias banners ('7.0 上半卡池' vs '丝柯克角色祈愿')
    while keeping genuinely separate concurrent events."""
    db = Database(tmp_path / "ai_dedup.db")
    start = datetime(2099, 8, 12, 2, 0, tzinfo=timezone.utc)
    end = datetime(2099, 9, 1, 9, 59, tzinfo=timezone.utc)

    # Existing structured event in DB from gameg.site
    existing_ev, _ = db.upsert_from_source(
        RawEvent(
            source_id="gameg-site",
            external_key="genshin:cn:banner-7-0-upper",
            game="原神",
            title="7.0 上半卡池",
            event_type="banner",
            start_time=start,
            end_time=end,
            is_structured_source=True,
        )
    )

    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        url = str(request.url)
        if url == "https://api.tavily.com/search":
            search_calls += 1
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "原神 7.0 上半卡池角色丝柯克",
                            "url": "https://ys.mihoyo.com/news/700",
                            "content": "原神 7.0 上半卡池为丝柯克限定角色祈愿，时间 8月12日至9月1日。",
                        }
                    ]
                },
            )
        if url == "https://api.example.com/v1/chat/completions":
            body = json.loads(request.content.decode("utf-8"))
            sys_msg = body["messages"][0]["content"]
            user_msg = body["messages"][1]["content"]

            # Extraction call from ai_collector
            if "信息提取器" in sys_msg:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "events": [
                                                {
                                                    "title": "丝柯克角色祈愿",
                                                    "event_type": "banner",
                                                    "start_time": "2099-08-12T10:00:00+08:00",
                                                    "end_time": "2099-09-01T17:59:00+08:00",
                                                    "url": "https://ys.mihoyo.com/news/700",
                                                    "needs_review": False,
                                                },
                                                {
                                                    "title": "限定武器神铸赋形",
                                                    "event_type": "banner",
                                                    "start_time": "2099-08-12T10:00:00+08:00",
                                                    "end_time": "2099-09-01T17:59:00+08:00",
                                                    "needs_review": False,
                                                },
                                            ]
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            }
                        ]
                    },
                )

            # AI Dedup Reviewer call
            if "丝柯克角色祈愿" in user_msg:
                verdict = {
                    "is_duplicate": True,
                    "matched_event_id": existing_ev.id,
                    "reason": "7.0 上半卡池即为丝柯克角色祈愿",
                }
            else:
                verdict = {
                    "is_duplicate": False,
                    "matched_event_id": None,
                    "reason": "武器池与角色池是独立卡池",
                }
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(verdict, ensure_ascii=False)}}]},
            )
        raise AssertionError(f"Unexpected URL: {url}")

    cfg = AppConfig(
        db_path=str(tmp_path / "ai_dedup.db"),
        dedup=DedupConfig(
            ai_review_enabled=True,
            llm_provider="llm",
            search_providers=["tavily"],
        ),
        search_providers=[SearchProviderConfig(id="tavily", type="tavily", api_key="tv")],
        llm_providers=[
            LLMProviderConfig(
                id="llm",
                type="openai_compat",
                base_url="https://api.example.com/v1",
                api_key="sk",
                model="m",
            )
        ],
        sources=[
            SourceConfig(
                id="ai-ys",
                type="ai_collector",
                options={"game": "原神", "llm_provider": "llm", "search_providers": ["tavily"]},
            )
        ],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_sync(db, cfg, client=client)

    assert report.sources[0].fetched == 2
    assert report.sources[0].deduped == 1  # 丝柯克角色祈愿 merged into 7.0 上半卡池
    assert report.sources[0].inserted == 1  # 限定武器神铸赋形 kept as distinct event
    assert search_calls >= 3  # 1 for collection + 2 for web-grounded dedup reviews

    all_events = db.list_events(include_past=True)
    assert len(all_events) == 2
    assert {e.title for e in all_events} == {"7.0 上半卡池", "限定武器神铸赋形"}
