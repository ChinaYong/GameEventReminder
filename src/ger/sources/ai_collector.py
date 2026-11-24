"""AI-powered web-search & URL collector source (supports Tavily/Exa + OpenAI-compat & Gemini native search)."""

from __future__ import annotations

import logging

import httpx

from ger.llm import ExtractedEventItem, create_llm_client
from ger.models import EventRecord, RawEvent, normalize_event_type
from ger.search import SearchResult, create_search_provider
from ger.sources.base import (
    BaseSource,
    build_stable_event_key,
    extract_text_from_html,
    format_existing_events_for_ai,
    register_source,
)
from ger.time_utils import get_zone, parse_iso_datetime, utc_now

logger = logging.getLogger(__name__)


def _format_context_blocks(
    search_results: list[SearchResult],
    fetched_pages: list[tuple[str, str]],
) -> str:
    blocks: list[str] = []
    for idx, res in enumerate(search_results, start=1):
        body = res.content or res.snippet
        pub = f" | 发布日期: {res.published_date}" if res.published_date else ""
        blocks.append(
            f"[搜索结果 {idx} ({res.provider})]\n"
            f"标题: {res.title}{pub}\n"
            f"链接: {res.url}\n"
            f"内容:\n{body}\n"
        )
    for idx, (url, text) in enumerate(fetched_pages, start=1):
        blocks.append(f"[直抓网页 {idx}]\n链接: {url}\n正文:\n{text}\n")
    return "\n---\n".join(blocks)


def _convert_extracted_items(
    items: list[ExtractedEventItem],
    *,
    source_id: str,
    game: str,
    default_tz: str,
    include_types: set[str] | None,
) -> list[RawEvent]:
    normalized_include = (
        {normalize_event_type(t) for t in include_types} if include_types else None
    )
    events: list[RawEvent] = []
    for item in items:
        ev_type = normalize_event_type(item.event_type)
        if normalized_include and ev_type not in normalized_include:
            continue

        start_dt = None
        end_dt = None
        needs_review = item.needs_review
        notes_parts: list[str] = [item.notes.strip()] if item.notes else []

        if item.start_time:
            try:
                start_dt = parse_iso_datetime(item.start_time, default_tz=default_tz)
            except ValueError:
                needs_review = True
                notes_parts.append(f"无法解析开始时间: {item.start_time}")

        if item.end_time:
            try:
                end_dt = parse_iso_datetime(item.end_time, default_tz=default_tz)
            except ValueError:
                needs_review = True
                notes_parts.append(f"无法解析结束时间: {item.end_time}")

        if start_dt is None and end_dt is None:
            continue

        date_hint = ""
        if start_dt:
            date_hint = start_dt.astimezone(get_zone(default_tz)).strftime("%Y-%m")
        elif end_dt:
            date_hint = end_dt.astimezone(get_zone(default_tz)).strftime("%Y-%m")

        ext_key = build_stable_event_key(game, ev_type, item.title, start_date_hint=date_hint)
        events.append(
            RawEvent(
                source_id=source_id,
                external_key=ext_key,
                game=game,
                title=item.title.strip(),
                event_type=ev_type,
                start_time=start_dt,
                end_time=end_dt,
                url=item.url.strip() if item.url else None,
                notes=" | ".join(notes_parts) if notes_parts else None,
                needs_review=needs_review,
                raw_payload=item.model_dump(),
            )
        )
    return events


@register_source("ai_collector")
class AICollectorSource(BaseSource):
    """Collects game events via web search (Tavily/Exa or Gemini Google Search) + LLM structuring."""

    async def _fetch_single_game(
        self,
        client: httpx.AsyncClient,
        *,
        game: str,
        llm_client,
        region: str,
        query_templates: list[str],
        search_provider_ids: list[str],
        urls: list[str],
        max_results: int,
        include_types: set[str] | None,
        extra_instructions: str,
        existing_events: list[EventRecord] | None,
    ) -> list[RawEvent]:
        region_kw = "国服" if region == "cn" else region
        if query_templates:
            queries = [tpl.format(game=game, region=region_kw) for tpl in query_templates]
        else:
            queries = [f"{game} {region_kw} 最新活动 网页活动 兑换码 时间 公告"]

        tz_name = self.app_config.timezone
        current_local_iso = utc_now().astimezone(get_zone(tz_name)).isoformat(timespec="minutes")
        existing_events_text = format_existing_events_for_ai(
            existing_events, game=game, tz_name=tz_name
        )

        # Mode A: Native search grounding (e.g., Gemini with use_google_search=True)
        if llm_client.supports_native_search and not search_provider_ids and not urls:
            batch = await llm_client.search_and_extract_events(
                client,
                game=game,
                queries=queries,
                timezone_name=tz_name,
                current_local_iso=current_local_iso,
                region=region,
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

        # Mode B: External search providers (Tavily/Exa) and/or direct URL fetching -> LLM extraction
        if not search_provider_ids and not urls:
            raise ValueError(
                f"ai_collector source '{self.source_id}': web search is required. "
                "Configure 'search_providers' (e.g. tavily/exa), direct 'urls', "
                "or use a Gemini LLM provider with 'use_google_search: true'."
            )

        dedup_results: dict[str, SearchResult] = {}
        for sp_id in search_provider_ids:
            sp_cfg = self.app_config.get_search_provider(sp_id)
            provider = create_search_provider(sp_cfg)
            for query in queries:
                try:
                    found = await provider.search(client, query, max_results=max_results)
                    for item in found:
                        if item.url not in dedup_results:
                            dedup_results[item.url] = item
                except Exception as exc:
                    logger.warning(
                        "Search provider '%s' failed for query '%s': %s", sp_id, query, exc
                    )

        fetched_pages: list[tuple[str, str]] = []
        for page_url in urls:
            try:
                resp = await client.get(
                    page_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; GameEventReminder/0.1)"},
                    follow_redirects=True,
                    timeout=30.0,
                )
                resp.raise_for_status()
                text = extract_text_from_html(resp.text, max_chars=6000)
                if text:
                    fetched_pages.append((page_url, text))
            except Exception as exc:
                logger.warning("Direct URL fetch failed for %s: %s", page_url, exc)

        if not dedup_results and not fetched_pages:
            raise RuntimeError(
                f"ai_collector source '{self.source_id}' ({game}) collected zero search results and zero pages"
            )

        context_text = _format_context_blocks(list(dedup_results.values()), fetched_pages)
        batch = await llm_client.extract_events_from_context(
            client,
            game=game,
            context_text=context_text,
            timezone_name=tz_name,
            current_local_iso=current_local_iso,
            region=region,
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
            raise ValueError(f"ai_collector source '{self.source_id}' requires 'llm_provider'")

        explicit_game = str(opts.get("game") or "").strip()
        if explicit_game:
            if not self.app_config.is_game_watched(explicit_game):
                logger.info(
                    "Skipping ai_collector '%s': game '%s' is not in watched_games",
                    self.source_id,
                    explicit_game,
                )
                return []
            target_games = [explicit_game]
        elif self.app_config.watched_games:
            target_games = list(self.app_config.watched_games)
        else:
            raise ValueError(
                f"ai_collector source '{self.source_id}' requires 'game' option or top-level 'watched_games'"
            )

        llm_cfg = self.app_config.get_llm_provider(llm_provider_id)
        llm_client = create_llm_client(llm_cfg)
        region = str(opts.get("region") or self.app_config.default_region).strip().lower()
        query_templates = [str(q).strip() for q in (opts.get("search_queries") or []) if str(q).strip()]
        search_provider_ids = [
            str(s).strip() for s in (opts.get("search_providers") or []) if str(s).strip()
        ]
        urls = [str(u).strip() for u in (opts.get("urls") or []) if str(u).strip()]
        max_results = int(opts.get("max_search_results", 5))
        raw_include_types = opts.get("include_types")
        if raw_include_types is None:
            include_types = {normalize_event_type(t) for t in self.app_config.enabled_event_types}
        else:
            include_types = {
                normalize_event_type(str(t)) for t in raw_include_types if str(t).strip()
            } or None
        extra_instructions = str(opts.get("extra_instructions") or "").strip()

        all_events: list[RawEvent] = []
        for game in target_games:
            try:
                evs = await self._fetch_single_game(
                    client,
                    game=game,
                    llm_client=llm_client,
                    region=region,
                    query_templates=query_templates,
                    search_provider_ids=search_provider_ids,
                    urls=urls if explicit_game else [],
                    max_results=max_results,
                    include_types=include_types,
                    extra_instructions=extra_instructions,
                    existing_events=existing_events,
                )
                all_events.extend(evs)
            except Exception as exc:
                if len(target_games) == 1:
                    raise
                logger.warning("ai_collector '%s' failed for game '%s': %s", self.source_id, game, exc)
        return all_events
