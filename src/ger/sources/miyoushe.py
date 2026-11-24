"""MiYouShe official static news/activity feed + AI extraction source (CN servers).

Uses the public static endpoint `bbs-api-static.miyoushe.com/painter/wapi/getNewsList`
(no login or DS signature required) to fetch official CN announcements and web/in-game
event notices, then extracts exact schedules via LLM.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ger.llm import create_llm_client
from ger.models import EventRecord, RawEvent, normalize_event_type
from ger.sources.ai_collector import _convert_extracted_items
from ger.sources.base import (
    BaseSource,
    extract_text_from_html,
    format_existing_events_for_ai,
    register_source,
)
from ger.time_utils import get_zone, utc_now

logger = logging.getLogger(__name__)

# Official MiYouShe forum game IDs (gids) -> CN display names & web article base URLs
MIYOUSHE_GAME_META: dict[int, tuple[str, str]] = {
    1: ("崩坏3", "https://www.miyoushe.com/bh3/article/"),
    2: ("原神", "https://www.miyoushe.com/ys/article/"),
    6: ("崩坏：星穹铁道", "https://www.miyoushe.com/sr/article/"),
    8: ("绝区零", "https://www.miyoushe.com/zzz/article/"),
}


def parse_miyoushe_news_payload(
    payload: dict[str, Any],
    *,
    article_base_url: str,
) -> list[dict[str, str]]:
    """Extract normalized post blocks from MiYouShe getNewsList JSON response."""
    data = payload.get("data") or {}
    items = data.get("list") or []
    posts: list[dict[str, str]] = []
    for entry in items:
        post = entry.get("post") if isinstance(entry, dict) else None
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("post_id") or "").strip()
        subject = str(post.get("subject") or "").strip()
        raw_content = str(post.get("content") or "").strip()
        if not subject:
            continue
        content = extract_text_from_html(raw_content, max_chars=2500)
        url = f"{article_base_url}{post_id}" if post_id else ""
        posts.append(
            {
                "post_id": post_id,
                "title": subject,
                "url": url,
                "content": content,
            }
        )
    return posts


@register_source("miyoushe_news")
class MiYouSheNewsSource(BaseSource):
    """Fetches official MiYouShe CN news/events (gids: 1=BH3, 2=YS, 6=HSR, 8=ZZZ) + AI extraction."""

    DEFAULT_API_URL = "https://bbs-api-static.miyoushe.com/painter/wapi/getNewsList"

    async def _fetch_single_gid(
        self,
        client: httpx.AsyncClient,
        *,
        gids: int,
        game: str,
        article_base: str,
        api_url: str,
        news_types: list[int],
        page_size: int,
        exclude_keywords: list[str],
        include_types: set[str] | None,
        extra_instructions: str,
        llm_client,
        existing_events: list[EventRecord] | None,
    ) -> list[RawEvent]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.miyoushe.com/",
        }
        dedup_posts: dict[str, dict[str, str]] = {}
        for ntype in news_types:
            try:
                resp = await client.get(
                    api_url,
                    params={"gids": gids, "type": ntype, "page_size": page_size},
                    headers=headers,
                    timeout=25.0,
                )
                resp.raise_for_status()
                posts = parse_miyoushe_news_payload(resp.json(), article_base_url=article_base)
                for p in posts:
                    if exclude_keywords and any(kw in p["title"].lower() for kw in exclude_keywords):
                        continue
                    key = p["post_id"] or p["title"]
                    dedup_posts.setdefault(key, p)
            except Exception as exc:
                logger.warning(
                    "MiYouShe news fetch failed (gids=%d, type=%d): %s", gids, ntype, exc
                )

        if not dedup_posts:
            return []

        blocks = []
        for idx, p in enumerate(dedup_posts.values(), start=1):
            blocks.append(
                f"[米游社官方公告 {idx}]\n标题: {p['title']}\n链接: {p['url']}\n正文:\n{p['content']}"
            )
        context_text = "\n---\n".join(blocks)

        tz_name = self.app_config.timezone
        current_local_iso = utc_now().astimezone(get_zone(tz_name)).isoformat(timespec="minutes")
        existing_events_text = format_existing_events_for_ai(
            existing_events, game=game, tz_name=tz_name
        )

        batch = await llm_client.extract_events_from_context(
            client,
            game=game,
            context_text=context_text,
            timezone_name=tz_name,
            current_local_iso=current_local_iso,
            region="cn",
            include_types=sorted(include_types) if include_types else None,
            extra_instructions=extra_instructions,
            existing_events_text=existing_events_text,
        )
        return _convert_extracted_items(
            batch.events,
            source_id=self.source_id,
            game=game,
            default_tz=tz_name,
            include_types=include_types,
        )

    async def fetch(
        self,
        client: httpx.AsyncClient,
        *,
        existing_events: list[EventRecord] | None = None,
    ) -> list[RawEvent]:
        opts = self.source_config.options
        llm_provider_id = str(opts.get("llm_provider") or "").strip()
        if not llm_provider_id:
            raise ValueError(f"miyoushe_news source '{self.source_id}' requires 'llm_provider'")

        # Build target (gids, canonical_game, article_base) list honoring master `watched_games`
        targets: list[tuple[int, str, str]] = []
        explicit_gids = int(opts.get("gids", 0) or 0)
        if explicit_gids > 0:
            default_name, article_base = MIYOUSHE_GAME_META.get(
                explicit_gids, (f"MiYouShe-{explicit_gids}", "https://www.miyoushe.com/ys/article/")
            )
            game = str(opts.get("game") or default_name).strip()
            if not self.app_config.is_game_watched(game):
                logger.info(
                    "Skipping miyoushe_news '%s': game '%s' is not in watched_games",
                    self.source_id,
                    game,
                )
                return []
            targets.append((explicit_gids, game, article_base))
        else:
            # Auto-map from global watched_games using MIYOUSHE_GAME_META + optional custom game_gids_map
            custom_map = {str(k): int(v) for k, v in (opts.get("game_gids_map") or {}).items()}
            name_to_meta = {name: (gid, base) for gid, (name, base) in MIYOUSHE_GAME_META.items()}
            for name, gid in custom_map.items():
                base = MIYOUSHE_GAME_META.get(gid, (name, "https://www.miyoushe.com/ys/article/"))[1]
                name_to_meta[name] = (gid, base)
            for watched in self.app_config.watched_games:
                if watched in name_to_meta:
                    gid, base = name_to_meta[watched]
                    targets.append((gid, watched, base))
            if not targets:
                return []

        api_url = str(opts.get("api_url") or self.DEFAULT_API_URL).strip()
        news_types = [int(t) for t in (opts.get("news_types") or [1, 2])]
        page_size = int(opts.get("page_size", 15))
        exclude_keywords = [
            str(k).lower()
            for k in (
                opts.get("exclude_keywords")
                or ["抽奖", "征集", "二创", "晒图", "中奖名单", "周边", "壁纸"]
            )
            if str(k).strip()
        ]
        raw_include_types = opts.get("include_types")
        if raw_include_types is None:
            include_types = {normalize_event_type(t) for t in self.app_config.enabled_event_types}
        else:
            include_types = {
                normalize_event_type(str(t)) for t in raw_include_types if str(t).strip()
            } or None
        extra_instructions = str(opts.get("extra_instructions") or "").strip()
        llm_client = create_llm_client(self.app_config.get_llm_provider(llm_provider_id))

        all_events: list[RawEvent] = []
        for gid, game, article_base in targets:
            try:
                all_events.extend(
                    await self._fetch_single_gid(
                        client,
                        gids=gid,
                        game=game,
                        article_base=article_base,
                        api_url=api_url,
                        news_types=news_types,
                        page_size=page_size,
                        exclude_keywords=exclude_keywords,
                        include_types=include_types,
                        extra_instructions=extra_instructions,
                        llm_client=llm_client,
                        existing_events=existing_events,
                    )
                )
            except Exception as exc:
                if len(targets) == 1:
                    raise
                logger.warning("miyoushe_news '%s' failed for %s: %s", self.source_id, game, exc)
        return all_events
