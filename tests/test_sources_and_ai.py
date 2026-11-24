"""Tests for gameg.site YAML parsing, Tavily/Exa + OpenAI-compat, Gemini native search, and RSS."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import yaml

from ger.config import AppConfig, LLMProviderConfig, SearchProviderConfig, SourceConfig
from ger.sources.ai_collector import AICollectorSource
from ger.sources.gameg_site import parse_gameg_yaml_document
from ger.sources.rss import RssAiSource, parse_feed_items


def test_parse_gameg_yaml_document() -> None:
    sample_yaml = """
game: genshin-impact
region: cn
versions:
  - id: "7.0"
    name: "7.0 新版本"
    start: "2026-11-20T06:00:00+08:00"
    end: "2026-12-31T18:00:00+08:00"
    sources:
      - "https://ys.mihoyo.com/main/news/detail/1"
events:
  - id: banner-7-0-upper
    name: "7.0 上半卡池"
    type: banner
    start: "2026-11-20T06:00:00+08:00"
    end: "2026-12-10T17:59:00+08:00"
    url: "https://ys.mihoyo.com/"
  - id: livestream-7-1
    name: "7.1 前瞻直播"
    type: livestream
    start: "2026-12-18T20:00:00+08:00"
    sources:
      - "https://live.bilibili.com/21987615"
  - id: old-event
    name: "很久以前的活动"
    type: event
    start: "2025-01-01T10:00:00+08:00"
    end: "2025-01-10T10:00:00+08:00"
"""
    doc = yaml.safe_load(sample_yaml)
    events = parse_gameg_yaml_document(
        doc,
        source_id="gameg-site",
        default_tz="Asia/Shanghai",
        include_types={"banner", "event", "version"},
        include_versions=True,
        game_name_map={"genshin-impact": "原神"},
        cutoff_utc=datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc),
    )

    # Pure livestream notices are normalized as "livestream" and excluded by default
    assert len(events) == 2
    titles = {e.title for e in events}
    assert titles == {"7.0 新版本", "7.0 上半卡池"}

    # Excluding "version" from include_types filters out versions even if include_versions=True
    events_no_ver = parse_gameg_yaml_document(
        doc,
        source_id="gameg-site",
        default_tz="Asia/Shanghai",
        include_types={"banner", "checkin_redeem"},
        include_versions=True,
        game_name_map={"genshin-impact": "原神"},
        cutoff_utc=datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert {e.title for e in events_no_ver} == {"7.0 上半卡池"}


@pytest.mark.asyncio
async def test_ai_collector_with_tavily_exa_and_openai_fallback() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append((str(request.url), body))

        if str(request.url) == "https://api.tavily.com/search":
            assert request.headers["authorization"] == "Bearer tvly-secret"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "绝区零 2.5 活动公告",
                            "url": "https://zzz.mihoyo.com/news/101",
                            "content": "「霓虹行动」将于 2026-11-26 10:00 至 2026-12-09 03:59 开启。",
                        }
                    ]
                },
            )
        if str(request.url) == "https://api.exa.ai/search":
            assert request.headers["x-api-key"] == "exa-secret"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Exa 补充来源",
                            "url": "https://wiki.example.com/zzz",
                            "text": "霓虹行动奖励菲林。",
                            "highlights": ["霓虹行动 11月26日开启"],
                        }
                    ]
                },
            )
        if str(request.url) == "https://relay.example.com/v1/chat/completions":
            # Simulate a relay that rejects json_schema with HTTP 400 and succeeds with json_object
            if body.get("response_format", {}).get("type") == "json_schema":
                return httpx.Response(400, json={"error": "json_schema not supported"})
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
                                                "title": "霓虹行动",
                                                "event_type": "event",
                                                "start_time": "2026-11-26T10:00:00+08:00",
                                                "end_time": "2026-12-09T03:59:00+08:00",
                                                "url": "https://zzz.mihoyo.com/news/101",
                                                "notes": "限时奖励活动",
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
        raise AssertionError(f"Unexpected URL: {request.url}")

    cfg = AppConfig(
        search_providers=[
            SearchProviderConfig(id="tavily", type="tavily", api_key="tvly-secret"),
            SearchProviderConfig(id="exa", type="exa", api_key="exa-secret"),
        ],
        llm_providers=[
            LLMProviderConfig(
                id="relay",
                type="openai_compat",
                base_url="https://relay.example.com/v1",
                api_key="sk-test",
                model="deepseek-chat",
            )
        ],
    )
    src_cfg = SourceConfig(
        id="ai-zzz",
        type="ai_collector",
        options={
            "game": "绝区零",
            "llm_provider": "relay",
            "search_providers": ["tavily", "exa"],
            "search_queries": ["绝区零 最新活动"],
        },
    )

    source = AICollectorSource(src_cfg, cfg)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await source.fetch(client)

    assert len(events) == 1
    assert events[0].title == "霓虹行动"
    assert events[0].game == "绝区零"
    assert events[0].start_time == datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc)
    # Verified fallback: tavily + exa + openai(json_schema 400) + openai(json_object 200)
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_ai_collector_with_gemini_native_google_search_two_pass() -> None:
    gemini_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "gem-secret"
        body = json.loads(request.content.decode("utf-8"))
        gemini_payloads.append(body)

        if "tools" in body:
            # Pass 1: Search Grounding (no responseMimeType conflict)
            assert body["tools"] == [{"google_search": {}}]
            assert "responseMimeType" not in body.get("generationConfig", {})
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": "鸣潮将于 2026-11-28 10:00 至 2026-12-15 03:59 开启《潮汐试炼》活动。"
                                    }
                                ]
                            },
                            "groundingMetadata": {
                                "groundingChunks": [
                                    {
                                        "web": {
                                            "uri": "https://mc.kurogames.com/news/99",
                                            "title": "鸣潮官方公告",
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                },
            )
        # Pass 2: Strict JSON Schema extraction without tools
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "https://mc.kurogames.com/news/99" in body["contents"][0]["parts"][0]["text"]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "events": [
                                                {
                                                    "title": "潮汐试炼",
                                                    "event_type": "event",
                                                    "start_time": "2026-11-28T10:00:00+08:00",
                                                    "end_time": "2026-12-15T03:59:00+08:00",
                                                    "url": "https://mc.kurogames.com/news/99",
                                                    "needs_review": False,
                                                }
                                            ]
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    cfg = AppConfig(
        llm_providers=[
            LLMProviderConfig(
                id="gem",
                type="gemini",
                api_key="gem-secret",
                model="gemini-2.5-flash",
                use_google_search=True,
            )
        ]
    )
    src_cfg = SourceConfig(
        id="ai-ww",
        type="ai_collector",
        options={
            "game": "鸣潮",
            "llm_provider": "gem",
            "search_queries": ["鸣潮 2026 最新活动"],
        },
    )

    source = AICollectorSource(src_cfg, cfg)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await source.fetch(client)

    assert len(gemini_payloads) == 2
    assert len(events) == 1
    assert events[0].title == "潮汐试炼"
    assert events[0].url == "https://mc.kurogames.com/news/99"


@pytest.mark.asyncio
async def test_rss_ai_source() -> None:
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Game News</title>
  <item>
    <title>冬季双倍活动开启</title>
    <link>https://game.example.com/winter</link>
    <pubDate>Tue, 24 Nov 2026 08:00:00 GMT</pubDate>
    <description><![CDATA[<p>活动时间：2026-11-25 10:00 至 2026-12-01 23:59</p>]]></description>
  </item>
</channel></rss>
"""
    parsed = parse_feed_items(rss_xml)
    assert len(parsed) == 1
    assert "2026-11-25" in parsed[0]["content"]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://game.example.com/feed.xml":
            return httpx.Response(200, text=rss_xml)
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
                                            "title": "冬季双倍活动",
                                            "event_type": "event",
                                            "start_time": "2026-11-25T10:00:00+08:00",
                                            "end_time": "2026-12-01T23:59:00+08:00",
                                            "url": "https://game.example.com/winter",
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
                id="openai-main",
                type="openai_compat",
                base_url="https://api.example.com/v1",
                api_key="sk-x",
                model="gpt-4o-mini",
            )
        ]
    )
    src_cfg = SourceConfig(
        id="rss-demo",
        type="rss_ai",
        options={
            "game": "DemoGame",
            "feed_url": "https://game.example.com/feed.xml",
            "llm_provider": "openai-main",
            "keyword_filter": ["活动"],
        },
    )
    source = RssAiSource(src_cfg, cfg)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await source.fetch(client)
    assert len(events) == 1
    assert events[0].title == "冬季双倍活动"


@pytest.mark.asyncio
async def test_sync_orchestrator_isolates_failures_and_notifies_issues(tmp_path) -> None:
    from ger.config import NotifierConfig, SchedulerConfig
    from ger.db import Database
    from ger.sync import run_sync

    notifications: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.github.com/repos/"):
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"type": "blob", "path": "data/genshin-impact/cn-2026.yaml"},
                    ]
                },
            )
        if url.startswith("https://raw.githubusercontent.com/"):
            return httpx.Response(
                200,
                text=(
                    "game: genshin-impact\n"
                    "region: cn\n"
                    "events:\n"
                    "  - id: ev-1\n"
                    "    name: '海灯节'\n"
                    "    type: event\n"
                    "    start: '2099-01-20T10:00:00+08:00'\n"
                    "    end: '2099-02-05T03:59:00+08:00'\n"
                ),
            )
        if url == "https://ntfy.example.com":
            notifications.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"id": "ok"})
        return httpx.Response(500, text="boom")

    cfg = AppConfig(
        db_path=str(tmp_path / "sync.db"),
        scheduler=SchedulerConfig(notify_on_sync_issues=True),
        notifiers=[
            NotifierConfig(id="ntfy", type="ntfy", server="https://ntfy.example.com", topic="t")
        ],
        sources=[
            SourceConfig(
                id="bad-source",
                type="ai_collector",
                options={"game": "Broken", "llm_provider": "missing-llm"},
            ),
            SourceConfig(
                id="gameg",
                type="gameg_site",
                options={"games": ["genshin-impact"], "game_name_map": {"genshin-impact": "原神"}},
            ),
        ],
    )
    db = Database(cfg.db_path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_sync(db, cfg, client=client)

    assert len(report.sources) == 2
    assert report.sources[0].ok is False
    assert report.sources[1].ok is True
    assert report.sources[1].inserted == 1
    assert len(db.list_events()) == 1
    assert len(notifications) == 1
    assert "bad-source" in notifications[0]["message"]


def test_dotenv_autoload_and_empty_default_fallback(tmp_path, monkeypatch) -> None:
    from ger.config import load_config

    monkeypatch.delenv("TEST_API_TOKEN", raising=False)
    monkeypatch.delenv("TEST_EMPTY_MODEL", raising=False)
    monkeypatch.setenv("TEST_OVERRIDE_KEY", "from-os-env")

    (tmp_path / ".env").write_text(
        'TEST_API_TOKEN="token-from-dotenv"\n'
        "TEST_EMPTY_MODEL=\n"
        "TEST_OVERRIDE_KEY=from-dotenv\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        'api:\n'
        '  auth_token: "${TEST_API_TOKEN}"\n'
        'llm_providers:\n'
        '  - id: "p1"\n'
        '    type: "openai_compat"\n'
        '    api_key: "${TEST_OVERRIDE_KEY}"\n'
        '    model: "${TEST_EMPTY_MODEL:-gpt-4o-mini}"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.api.auth_token == "token-from-dotenv"
    assert cfg.llm_providers[0].model == "gpt-4o-mini"
    assert cfg.llm_providers[0].api_key == "from-os-env"


def test_gameg_default_cn_region_filter() -> None:
    jp_doc = yaml.safe_load(
        """
game: genshin-impact
region: jp
events:
  - id: jp-ev
    name: "日服限定"
    type: event
    start: "2099-01-01T10:00:00+09:00"
    end: "2099-01-10T10:00:00+09:00"
"""
    )
    events = parse_gameg_yaml_document(
        jp_doc,
        source_id="gameg",
        default_tz="Asia/Shanghai",
        regions={"cn"},
    )
    assert events == []


@pytest.mark.asyncio
async def test_sync_hard_blocks_expired_events(tmp_path) -> None:
    from ger.db import Database
    from ger.sync import run_sync

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.github.com/repos/"):
            return httpx.Response(
                200,
                json={"tree": [{"type": "blob", "path": "data/genshin-impact/cn-2026.yaml"}]},
            )
        return httpx.Response(
            200,
            text=(
                "game: genshin-impact\n"
                "region: cn\n"
                "events:\n"
                "  - id: past-ev\n"
                "    name: '已结束旧活动'\n"
                "    type: event\n"
                "    start: '2026-01-01T10:00:00+08:00'\n"
                "    end: '2026-01-10T03:59:00+08:00'\n"
                "  - id: future-ev\n"
                "    name: '进行中/未来活动'\n"
                "    type: event\n"
                "    start: '2099-01-01T10:00:00+08:00'\n"
                "    end: '2099-01-10T03:59:00+08:00'\n"
            ),
        )

    cfg = AppConfig(
        db_path=str(tmp_path / "exp.db"),
        ignore_ended_events=True,
        sources=[
            SourceConfig(
                id="gameg",
                type="gameg_site",
                options={"games": ["genshin-impact"], "lookback_days": -1},
            )
        ],
    )
    db = Database(cfg.db_path)
    fixed_now = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await run_sync(db, cfg, client=client, now=fixed_now)

    assert report.sources[0].fetched == 2
    assert report.sources[0].skipped_expired == 1
    assert report.sources[0].inserted == 1
    stored = db.list_events(include_past=True)
    assert len(db.list_events(include_past=True)) == 1
    assert db.list_events(include_past=True)[0].title == "进行中/未来活动"


@pytest.mark.asyncio
async def test_ai_collector_receives_existing_events_context() -> None:
    captured_user_prompt = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_user_prompt
        if str(request.url) == "https://api.tavily.com/search":
            return httpx.Response(
                200,
                json={"results": [{"title": "公告", "url": "https://ex.com/1", "content": "活动内容"}]},
            )
        body = json.loads(request.content.decode("utf-8"))
        captured_user_prompt = body["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"events": []}'}}]},
        )

    from ger.models import EventRecord

    cfg = AppConfig(
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
    )
    src = AICollectorSource(
        SourceConfig(
            id="ai-1",
            type="ai_collector",
            options={"game": "原神", "llm_provider": "llm", "search_providers": ["tavily"]},
        ),
        cfg,
    )
    now = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
    existing = [
        EventRecord(
            id=1,
            source_id="gameg",
            external_key="k1",
            game="原神",
            title="已有幽境危战活动",
            event_type="ingame_event",
            start_time=now,
            end_time=now,
            created_at=now,
            updated_at=now,
        ),
        EventRecord(
            id=2,
            source_id="gameg",
            external_key="k2",
            game="鸣潮",
            title="其他游戏活动不应混入",
            event_type="ingame_event",
            start_time=now,
            end_time=now,
            created_at=now,
            updated_at=now,
        ),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await src.fetch(client, existing_events=existing)

    assert "已有幽境危战活动" in captured_user_prompt
    assert "其他游戏活动不应混入" not in captured_user_prompt



