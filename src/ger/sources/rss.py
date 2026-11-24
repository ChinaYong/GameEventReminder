"""Generic RSS/Atom feed + AI structured extraction source (zero extra XML dependencies)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

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


def parse_feed_items(xml_text: str, *, max_items: int = 10) -> list[dict[str, str]]:
    """Parse RSS 2.0 or Atom feed XML using stdlib ElementTree."""
    root = ET.fromstring(xml_text)
    items: list[dict[str, str]] = []

    # RSS 2.0: <rss><channel><item>
    channel = root.find("channel")
    if channel is not None:
        for node in channel.findall("item")[:max_items]:
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            desc = (node.findtext("description") or "").strip()
            pub_date = (node.findtext("pubDate") or "").strip()
            items.append(
                {
                    "title": title,
                    "url": link,
                    "published": pub_date,
                    "content": extract_text_from_html(desc, max_chars=2000),
                }
            )
        return items

    # Atom: <feed xmlns="http://www.w3.org/2005/Atom"><entry>
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns) or root.findall("entry")
    for entry in entries[:max_items]:
        title = (entry.findtext("atom:title", default="", namespaces=ns) or entry.findtext("title") or "").strip()
        link_el = entry.find("atom:link", ns)
        if link_el is None:
            link_el = entry.find("link")
        link = (link_el.attrib.get("href", "") if link_el is not None else "").strip()
        summary = (
            entry.findtext("atom:content", default="", namespaces=ns)
            or entry.findtext("atom:summary", default="", namespaces=ns)
            or entry.findtext("content")
            or entry.findtext("summary")
            or ""
        )
        updated = (
            entry.findtext("atom:updated", default="", namespaces=ns)
            or entry.findtext("updated")
            or ""
        ).strip()
        items.append(
            {
                "title": title,
                "url": link,
                "published": updated,
                "content": extract_text_from_html(summary, max_chars=2000),
            }
        )
    return items


@register_source("rss_ai")
class RssAiSource(BaseSource):
    """Fetches an RSS/Atom feed (official news or RSSHub) and extracts events via LLM."""

    async def fetch(
        self,
        client: httpx.AsyncClient,
        *,
        existing_events: list[EventRecord] | None = None,
    ) -> list[RawEvent]:
        opts = self.source_config.options
        game = str(opts.get("game") or "").strip()
        feed_url = str(opts.get("feed_url") or "").strip()
        llm_provider_id = str(opts.get("llm_provider") or "").strip()
        if not game or not feed_url or not llm_provider_id:
            raise ValueError(
                f"rss_ai source '{self.source_id}' requires 'game', 'feed_url', and 'llm_provider'"
            )
        if not self.app_config.is_game_watched(game):
            return []

        max_items = int(opts.get("max_items", 10))
        keywords = [str(k).lower() for k in (opts.get("keyword_filter") or []) if str(k).strip()]
        raw_include_types = opts.get("include_types")
        if raw_include_types is None:
            include_types = {normalize_event_type(t) for t in self.app_config.enabled_event_types}
        else:
            include_types = {
                normalize_event_type(str(t)) for t in raw_include_types if str(t).strip()
            } or None
        extra_instructions = str(opts.get("extra_instructions") or "").strip()

        resp = await client.get(
            feed_url,
            headers={"User-Agent": "GameEventReminder/0.1"},
            follow_redirects=True,
            timeout=30.0,
        )
        resp.raise_for_status()

        feed_items = parse_feed_items(resp.text, max_items=max_items)
        if keywords:
            filtered = [
                it
                for it in feed_items
                if any(kw in f"{it['title']} {it['content']}".lower() for kw in keywords)
            ]
            feed_items = filtered

        if not feed_items:
            return []

        blocks = []
        for idx, it in enumerate(feed_items, start=1):
            blocks.append(
                f"[公告 {idx}]\n标题: {it['title']}\n发布时间: {it['published']}\n"
                f"链接: {it['url']}\n正文:\n{it['content']}"
            )
        context_text = "\n---\n".join(blocks)

        llm_client = create_llm_client(self.app_config.get_llm_provider(llm_provider_id))
        region = str(opts.get("region") or self.app_config.default_region).strip().lower()
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
