"""Structured source adapter for gameg.site (Oven2002/game-event-gantt YAML repo)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
import yaml

from ger.models import EventRecord, EventType, RawEvent, normalize_event_type
from ger.sources.base import BaseSource, register_source
from ger.time_utils import parse_iso_datetime, utc_now

logger = logging.getLogger(__name__)

# Built-in mapping between Oven2002/game-event-gantt directory slugs and canonical CN game names.
# User-supplied `options.game_name_map` merges on top of this map.
DEFAULT_GAMEG_NAME_MAP: dict[str, str] = {
    "genshin-impact": "原神",
    "honkai-star-rail": "崩坏：星穹铁道",
    "zenless-zone-zero": "绝区零",
    "ZenlessZoneZero": "绝区零",
    "wuthering-waves": "鸣潮",
    "arknights": "明日方舟",
    "arknights-endfield": "明日方舟：终末地",
    "azur-lane": "碧蓝航线",
    "neverness-to-everness": "异环",
}


def resolve_gameg_target_slugs(
    *,
    configured_games: set[str],
    watched_games: list[str],
    game_name_map: dict[str, str],
) -> set[str] | None:
    """Resolve which repository `data/<slug>/` directories to fetch under the master switch.

    Returns:
      - None: no slug restriction (fetch all directories in repo).
      - empty set(): master switch excludes all configured slugs (fetch nothing).
      - non-empty set(): fetch only these directory slugs.
    """
    if not watched_games:
        return configured_games or None

    watched_set = set(watched_games)
    mapped_slugs = {
        slug for slug, canonical in game_name_map.items() if canonical in watched_set
    }
    # Also allow direct slug entries in watched_games
    mapped_slugs |= watched_set

    if configured_games:
        return configured_games & mapped_slugs
    return mapped_slugs


def parse_gameg_yaml_document(
    doc: dict[str, Any],
    *,
    source_id: str,
    default_tz: str,
    regions: set[str] | None = None,
    include_types: set[str] | None = None,
    include_versions: bool = False,
    game_name_map: dict[str, str] | None = None,
    cutoff_utc: datetime | None = None,
) -> list[RawEvent]:
    """Parse a single YAML document from Oven2002/game-event-gantt."""
    if not isinstance(doc, dict):
        return []

    game_id = str(doc.get("game") or "").strip()
    region = str(doc.get("region") or "cn").strip().lower()
    if not game_id:
        return []
    if regions and region not in regions:
        return []

    display_game = (game_name_map or {}).get(game_id, game_id)
    normalized_include = (
        {normalize_event_type(t) for t in include_types} if include_types else None
    )
    results: list[RawEvent] = []

    def _first_url(item: dict[str, Any]) -> str | None:
        if item.get("url"):
            return str(item["url"]).strip()
        sources = item.get("sources")
        if isinstance(sources, list) and sources:
            return str(sources[0]).strip()
        return None

    def _is_recent(start_dt: datetime | None, end_dt: datetime | None) -> bool:
        if cutoff_utc is None:
            return True
        anchor = end_dt or start_dt
        if anchor is None:
            return True
        return anchor >= cutoff_utc

    if include_versions and (
        normalized_include is None or EventType.VERSION.value in normalized_include
    ):
        for ver in doc.get("versions") or []:
            if not isinstance(ver, dict):
                continue
            ver_id = str(ver.get("id") or "").strip()
            name = str(ver.get("name") or ver_id).strip()
            if not ver_id or not name:
                continue
            start_dt = parse_iso_datetime(str(ver["start"]), default_tz) if ver.get("start") else None
            end_dt = parse_iso_datetime(str(ver["end"]), default_tz) if ver.get("end") else None
            if not _is_recent(start_dt, end_dt):
                continue
            results.append(
                RawEvent(
                    source_id=source_id,
                    external_key=f"{game_id}:{region}:version:{ver_id}",
                    game=display_game,
                    title=name,
                    event_type=EventType.VERSION.value,
                    start_time=start_dt,
                    end_time=end_dt,
                    url=_first_url(ver),
                    notes=str(ver["note"]).strip() if ver.get("note") else None,
                    needs_review=False,
                    is_structured_source=True,
                    raw_payload=ver,
                )
            )

    for ev in doc.get("events") or []:
        if not isinstance(ev, dict):
            continue
        ev_id = str(ev.get("id") or "").strip()
        name = str(ev.get("name") or "").strip()
        ev_type = normalize_event_type(str(ev.get("type") or "event"))
        if not ev_id or not name:
            continue
        if normalized_include and ev_type not in normalized_include:
            continue

        start_dt = parse_iso_datetime(str(ev["start"]), default_tz) if ev.get("start") else None
        end_dt = parse_iso_datetime(str(ev["end"]), default_tz) if ev.get("end") else None
        if start_dt is None and end_dt is None:
            continue
        if not _is_recent(start_dt, end_dt):
            continue

        note = str(ev["note"]).strip() if ev.get("note") else None
        results.append(
            RawEvent(
                source_id=source_id,
                external_key=f"{game_id}:{region}:{ev_id}",
                game=display_game,
                title=name,
                event_type=ev_type,
                start_time=start_dt,
                end_time=end_dt,
                url=_first_url(ev),
                notes=note,
                needs_review=False,
                is_structured_source=True,
                raw_payload=ev,
            )
        )

    return results


@register_source("gameg_site")
class GameGSiteSource(BaseSource):
    """Fetches community-maintained YAML schedules from Oven2002/game-event-gantt."""

    async def fetch(
        self,
        client: httpx.AsyncClient,
        *,
        existing_events: list[EventRecord] | None = None,
    ) -> list[RawEvent]:
        opts = self.source_config.options
        repo = str(opts.get("repo") or "Oven2002/game-event-gantt").strip()
        branch = str(opts.get("branch") or "main").strip()
        api_base = str(opts.get("github_api_base") or "https://api.github.com").rstrip("/")
        raw_base = str(opts.get("raw_base_url") or "https://raw.githubusercontent.com").rstrip("/")
        github_token = str(opts.get("github_token") or "").strip()

        configured_games = {str(g).strip() for g in (opts.get("games") or []) if str(g).strip()}
        raw_regions = opts.get("regions")
        if raw_regions is None:
            regions = {self.app_config.default_region.lower().strip()}
        else:
            regions = {str(r).strip().lower() for r in raw_regions if str(r).strip()} or None
        raw_include_types = opts.get("include_types")
        if raw_include_types is None:
            include_types = {normalize_event_type(t) for t in self.app_config.enabled_event_types}
        else:
            include_types = {
                normalize_event_type(str(t)) for t in raw_include_types if str(t).strip()
            } or None
        include_versions = bool(opts.get("include_versions", True))
        default_lookback = 0 if self.app_config.ignore_ended_events else 7
        lookback_days = int(opts.get("lookback_days", default_lookback))
        game_name_map = dict(DEFAULT_GAMEG_NAME_MAP)
        game_name_map.update(
            {str(k): str(v) for k, v in (opts.get("game_name_map") or {}).items()}
        )
        target_games = resolve_gameg_target_slugs(
            configured_games=configured_games,
            watched_games=self.app_config.watched_games,
            game_name_map=game_name_map,
        )
        if target_games is not None and len(target_games) == 0:
            logger.info("Source '%s' skipped: no matching games in watched_games", self.source_id)
            return []
        cutoff = utc_now() - timedelta(days=lookback_days) if lookback_days >= 0 else None

        headers = {"Accept": "application/vnd.github+json", "User-Agent": "game-event-reminder/0.1"}
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"

        tree_url = f"{api_base}/repos/{repo}/git/trees/{branch}?recursive=1"
        tree_resp = await client.get(tree_url, headers=headers, timeout=30.0)
        tree_resp.raise_for_status()
        tree_data = tree_resp.json()

        yaml_paths: list[str] = []
        for entry in tree_data.get("tree", []):
            if entry.get("type") != "blob":
                continue
            path = str(entry.get("path") or "")
            if not path.startswith("data/") or not path.endswith((".yaml", ".yml")):
                continue
            if path.endswith(("/meta.yaml", "/meta.yml")):
                continue
            parts = path.split("/")
            if len(parts) < 3:
                continue
            game_dir = parts[1]
            if target_games is not None and game_dir not in target_games:
                continue
            yaml_paths.append(path)

        events: list[RawEvent] = []
        for path in yaml_paths:
            file_url = f"{raw_base}/{repo}/{branch}/{path}"
            try:
                file_resp = await client.get(file_url, headers=headers, timeout=30.0)
                file_resp.raise_for_status()
                doc = yaml.safe_load(file_resp.text)
            except Exception as exc:
                logger.warning("Failed to fetch/parse %s: %s", file_url, exc)
                continue

            parsed = parse_gameg_yaml_document(
                doc,
                source_id=self.source_id,
                default_tz=self.app_config.timezone,
                regions=regions,
                include_types=include_types,
                include_versions=include_versions,
                game_name_map=game_name_map,
                cutoff_utc=cutoff,
            )
            events.extend(ev for ev in parsed if self.app_config.is_game_watched(ev.game))

        return events
