"""Base source plugin interface, registry, and zero-dependency HTML text extractor."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from html.parser import HTMLParser
from typing import Callable

import httpx

from ger.config import AppConfig, SourceConfig
from ger.models import EventRecord, RawEvent
from ger.time_utils import format_local


class _HTMLTextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag.lower() in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def get_text(self) -> str:
        joined = " ".join(self._chunks)
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n\s*\n+", "\n", joined)
        return joined.strip()


def extract_text_from_html(html: str, *, max_chars: int = 8000) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.get_text()[:max_chars]


def build_stable_event_key(
    game: str,
    event_type: str,
    title: str,
    start_date_hint: str = "",
) -> str:
    """Create a deterministic external_key so repeated AI syncs deduplicate reliably."""
    norm_title = re.sub(r"[\s\-_·•【】\[\]()（）《》:：]+", "", title.lower())
    raw = f"{game.lower().strip()}|{event_type.lower().strip()}|{norm_title}|{start_date_hint}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def format_existing_events_for_ai(
    existing_events: list[EventRecord] | None,
    *,
    game: str,
    tz_name: str,
    max_items: int = 30,
) -> str:
    """Format active/pending events of the same game for LLM deduplication context."""
    if not existing_events:
        return ""
    matched = [e for e in existing_events if e.game == game][:max_items]
    if not matched:
        return ""
    lines = []
    for ev in matched:
        s = format_local(ev.start_time, tz_name)
        e = format_local(ev.end_time, tz_name)
        lines.append(f"- [{ev.event_type}] {ev.title} | {s} ~ {e}")
    return "\n".join(lines)


class BaseSource(ABC):
    def __init__(self, source_config: SourceConfig, app_config: AppConfig) -> None:
        self.source_config = source_config
        self.app_config = app_config

    @property
    def source_id(self) -> str:
        return self.source_config.id

    @abstractmethod
    async def fetch(
        self,
        client: httpx.AsyncClient,
        *,
        existing_events: list[EventRecord] | None = None,
    ) -> list[RawEvent]:
        """Collect and normalize events for this source."""


SOURCE_REGISTRY: dict[str, type[BaseSource]] = {}


def register_source(name: str) -> Callable[[type[BaseSource]], type[BaseSource]]:
    def decorator(cls: type[BaseSource]) -> type[BaseSource]:
        SOURCE_REGISTRY[name] = cls
        return cls

    return decorator


def create_source(source_config: SourceConfig, app_config: AppConfig) -> BaseSource:
    cls = SOURCE_REGISTRY.get(source_config.type)
    if cls is None:
        raise ValueError(f"Unsupported source type: {source_config.type}")
    return cls(source_config, app_config)
