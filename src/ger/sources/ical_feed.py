"""Zero-dependency RFC 5545 iCalendar (.ics) feed source adapter.

Works with any community-maintained game event .ics subscription, such as:
- Trrrrw/hoyo_calendar (Genshin / HSR / ZZZ)
- Astrian/ArkBulletinGPT (Arknights CN)
- Any self-hosted or third-party game calendar .ics URL
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from ger.models import EventRecord, EventType, RawEvent, normalize_event_type
from ger.sources.base import BaseSource, build_stable_event_key, register_source
from ger.time_utils import ensure_utc, get_zone, utc_now

_DT_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?(Z)?$")


def _unfold_ics_lines(ics_text: str) -> list[str]:
    """Unfold RFC 5545 folded lines (CRLF/LF followed by single space or tab)."""
    raw_lines = ics_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded: list[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _unescape_ics_text(text: str) -> str:
    return (
        text.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _parse_ics_datetime(
    value: str,
    params: dict[str, str],
    default_tz: str,
) -> datetime | None:
    text = value.strip()
    match = _DT_COMPACT_RE.match(text)
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if match.group(4) is None:
        dt = datetime(year, month, day, 0, 0, 0, tzinfo=get_zone(default_tz))
        return dt.astimezone(timezone.utc)

    hour, minute, second = int(match.group(4)), int(match.group(5)), int(match.group(6))
    is_utc = bool(match.group(7))
    if is_utc:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)

    tz_name = params.get("TZID", default_tz).strip('"')
    try:
        tz = get_zone(tz_name)
    except Exception:
        tz = get_zone(default_tz)
    return ensure_utc(datetime(year, month, day, hour, minute, second, tzinfo=tz), default_tz)


def _infer_event_type_from_text(title: str, categories: str, default_type: str) -> str:
    blob = f"{title} {categories}".lower()
    if any(k in blob for k in ("卡池", "祈愿", "跃迁", "寻访", "唤取", "调频", "banner", "gacha")):
        return EventType.BANNER.value
    if any(k in blob for k in ("兑换码", "签到", "福利", "redeem", "checkin", "code")):
        return EventType.CHECKIN_REDEEM.value
    if any(k in blob for k in ("前瞻", "直播", "特别节目", "livestream")):
        return "livestream"
    if any(k in blob for k in ("网页", "h5", "web_event", "web event")):
        return EventType.WEB_EVENT.value
    if any(k in blob for k in ("版本更新", "停服维护", "停机维护", "version", "maintenance")):
        return EventType.VERSION.value
    return normalize_event_type(default_type)


def parse_ics_events(
    ics_text: str,
    *,
    source_id: str,
    game: str,
    default_tz: str = "Asia/Shanghai",
    default_type: str = EventType.INGAME_EVENT.value,
    include_types: set[str] | None = None,
    exclude_keywords: list[str] | None = None,
    cutoff_utc: datetime | None = None,
) -> list[RawEvent]:
    """Parse VEVENT blocks from an iCalendar (.ics) document."""
    lines = _unfold_ics_lines(ics_text)
    normalized_include = (
        {normalize_event_type(t) for t in include_types} if include_types else None
    )
    excludes = [kw.lower() for kw in (exclude_keywords or []) if kw.strip()]

    events: list[RawEvent] = []
    in_vevent = False
    props: dict[str, tuple[str, dict[str, str]]] = {}

    def _flush() -> None:
        summary_tuple = props.get("SUMMARY")
        if not summary_tuple:
            return
        title = _unescape_ics_text(summary_tuple[0])
        if not title:
            return
        if excludes and any(kw in title.lower() for kw in excludes):
            return

        dtstart_tuple = props.get("DTSTART")
        dtend_tuple = props.get("DTEND")
        start_dt = (
            _parse_ics_datetime(dtstart_tuple[0], dtstart_tuple[1], default_tz)
            if dtstart_tuple
            else None
        )
        end_dt = (
            _parse_ics_datetime(dtend_tuple[0], dtend_tuple[1], default_tz)
            if dtend_tuple
            else None
        )
        if start_dt is None and end_dt is None:
            return

        if cutoff_utc is not None:
            anchor = end_dt or start_dt
            if anchor is not None and anchor < cutoff_utc:
                return

        categories = _unescape_ics_text(props["CATEGORIES"][0]) if "CATEGORIES" in props else ""
        ev_type = _infer_event_type_from_text(title, categories, default_type)
        if normalized_include and ev_type not in normalized_include:
            return

        uid = props["UID"][0].strip() if "UID" in props else ""
        ext_key = uid or build_stable_event_key(
            game, ev_type, title, start_dt.strftime("%Y-%m") if start_dt else ""
        )
        url = props["URL"][0].strip() if "URL" in props else None
        notes = _unescape_ics_text(props["DESCRIPTION"][0])[:500] if "DESCRIPTION" in props else None

        events.append(
            RawEvent(
                source_id=source_id,
                external_key=ext_key,
                game=game,
                title=title,
                event_type=ev_type,
                start_time=start_dt,
                end_time=end_dt,
                url=url or None,
                notes=notes or None,
                needs_review=False,
                is_structured_source=True,
                raw_payload={"uid": uid, "summary": title, "categories": categories},
            )
        )

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_vevent = True
            props = {}
            continue
        if line == "END:VEVENT":
            if in_vevent:
                _flush()
            in_vevent = False
            props = {}
            continue
        if not in_vevent or ":" not in line:
            continue

        left, value = line.split(":", 1)
        parts = left.split(";")
        prop_name = parts[0].strip().upper()
        params: dict[str, str] = {}
        for p in parts[1:]:
            if "=" in p:
                pk, pv = p.split("=", 1)
                params[pk.strip().upper()] = pv.strip()
        props[prop_name] = (value, params)

    return events


@register_source("ical_feed")
class ICalFeedSource(BaseSource):
    """Fetches a remote .ics iCalendar feed (structured source, zero LLM cost)."""

    async def fetch(
        self,
        client: httpx.AsyncClient,
        *,
        existing_events: list[EventRecord] | None = None,
    ) -> list[RawEvent]:
        opts = self.source_config.options
        game = str(opts.get("game") or "").strip()
        url = str(opts.get("url") or "").strip()
        if not game or not url:
            raise ValueError(f"ical_feed source '{self.source_id}' requires 'game' and 'url'")
        if not self.app_config.is_game_watched(game):
            return []

        default_type = str(opts.get("default_type") or EventType.INGAME_EVENT.value)
        raw_include_types = opts.get("include_types")
        if raw_include_types is None:
            include_types = {normalize_event_type(t) for t in self.app_config.enabled_event_types}
        else:
            include_types = {
                normalize_event_type(str(t)) for t in raw_include_types if str(t).strip()
            } or None

        exclude_keywords = [str(k) for k in (opts.get("exclude_keywords") or [])]
        default_lookback = 0 if self.app_config.ignore_ended_events else 7
        lookback_days = int(opts.get("lookback_days", default_lookback))
        cutoff = utc_now() - timedelta(days=lookback_days) if lookback_days >= 0 else None

        resp = await client.get(
            url,
            headers={"User-Agent": "GameEventReminder/0.1"},
            follow_redirects=True,
            timeout=30.0,
        )
        resp.raise_for_status()

        return parse_ics_events(
            resp.text,
            source_id=self.source_id,
            game=game,
            default_tz=self.app_config.timezone,
            default_type=default_type,
            include_types=include_types,
            exclude_keywords=exclude_keywords,
            cutoff_utc=cutoff,
        )
