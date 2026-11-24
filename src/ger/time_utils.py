"""Time, timezone, and relative offset utilities."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_OFFSET_PATTERN = re.compile(r"^([+-]?)(\d+)([dhm])$", re.IGNORECASE)


def get_zone(tz_name: str) -> ZoneInfo:
    return ZoneInfo(tz_name)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime, default_tz: str = "Asia/Shanghai") -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_zone(default_tz))
    return dt.astimezone(timezone.utc)


def parse_iso_datetime(value: str, default_tz: str = "Asia/Shanghai") -> datetime:
    """Parse an ISO-8601 datetime string (or YYYY-MM-DD date) and normalize to UTC."""
    text = value.strip()
    if not text:
        raise ValueError("Empty datetime string")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Support pure date like "2026-11-24"
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text}T00:00:00"
    dt = datetime.fromisoformat(text)
    return ensure_utc(dt, default_tz=default_tz)


def format_utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


def format_local(dt: datetime | None, tz_name: str = "Asia/Shanghai") -> str:
    if dt is None:
        return "-"
    local_dt = ensure_utc(dt, tz_name).astimezone(get_zone(tz_name))
    return local_dt.strftime("%Y-%m-%d %H:%M %Z")


def parse_offset(offset_str: str) -> timedelta:
    """Parse a relative offset string such as '-24h', '-30m', '0m', '+1d'."""
    s = offset_str.strip()
    if s in ("0", "+0", "-0"):
        return timedelta(0)
    match = _OFFSET_PATTERN.match(s)
    if not match:
        raise ValueError(
            f"Invalid offset '{offset_str}'. Expected format like '-24h', '-30m', '0m', '+1d'."
        )
    sign_str, amount_str, unit = match.groups()
    sign = -1 if sign_str == "-" else 1
    amount = int(amount_str)
    unit = unit.lower()
    if unit == "d":
        return timedelta(days=sign * amount)
    if unit == "h":
        return timedelta(hours=sign * amount)
    if unit == "m":
        return timedelta(minutes=sign * amount)
    raise ValueError(f"Unsupported offset unit: {unit}")
