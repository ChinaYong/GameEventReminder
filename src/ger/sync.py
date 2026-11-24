"""Orchestrator for running enabled sources, persisting events, and rebuilding reminders."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from ger.config import AppConfig
from ger.db import Database
from ger.dedup import review_gray_zone_duplicate
from ger.dispatcher import build_enabled_notifiers, send_notification_to_all
from ger.models import RawEvent, SyncReport, SyncSourceSummary, normalize_event_type
from ger.notifiers import NotificationMessage
from ger.reminders import rebuild_reminders_for_event
from ger.sources import create_source
from ger.time_utils import utc_now

logger = logging.getLogger(__name__)


def _is_expired_event(raw: RawEvent, config: AppConfig, current: datetime) -> bool:
    """Hard filter preventing already-ended historical events from entering SQLite."""
    if not config.ignore_ended_events:
        return False
    if raw.end_time is not None:
        return raw.end_time <= current
    if raw.start_time is not None:
        grace = timedelta(minutes=config.get_reminder_rule(raw.game).grace_period_minutes)
        return raw.start_time < current - grace
    return True


async def run_sync(
    db: Database,
    config: AppConfig,
    *,
    source_id: str | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Execute sync across enabled sources (or a single specified source)."""
    started_at = now or utc_now()
    target_configs = [
        src
        for src in config.sources
        if (src.id == source_id if source_id else src.enabled)
    ]
    if source_id and not target_configs:
        raise KeyError(f"Source '{source_id}' not found in configuration")

    summaries: list[SyncSourceSummary] = []
    cleaned_events = 0
    muted_unwatched, restored_events = db.reconcile_watched_games(config.watched_games)
    for ev in restored_events:
        rebuild_reminders_for_event(db, config, ev, now=now or utc_now())
    restored_watched = len(restored_events)
    if muted_unwatched or restored_watched:
        logger.info(
            "Reconciled watched_games: muted=%d restored=%d",
            muted_unwatched,
            restored_watched,
        )

    global_allowed_types = {normalize_event_type(t) for t in config.enabled_event_types}

    async def _run(http_client: httpx.AsyncClient) -> None:
        nonlocal cleaned_events
        for src_cfg in target_configs:
            summary = SyncSourceSummary(source_id=src_cfg.id, ok=True)
            try:
                source = create_source(src_cfg, config)
                existing_events = db.list_events(include_past=False, limit=500)
                raw_events = await source.fetch(http_client, existing_events=existing_events)
                summary.fetched = len(raw_events)
                for raw in raw_events:
                    ref_now = now or utc_now()
                    if not config.is_game_watched(raw.game):
                        summary.skipped_filtered += 1
                        continue
                    norm_type = normalize_event_type(raw.event_type)
                    if global_allowed_types and norm_type not in global_allowed_types:
                        summary.skipped_filtered += 1
                        continue
                    if _is_expired_event(raw, config, ref_now):
                        summary.skipped_expired += 1
                        continue
                    raw.event_type = norm_type
                    forced_dup_id: int | None = None
                    if config.dedup.ai_review_enabled:
                        suspects = db.find_gray_zone_candidates(
                            raw,
                            auto_merge_similarity=config.dedup.auto_merge_similarity,
                            gray_zone_min_similarity=config.dedup.gray_zone_min_similarity,
                        )
                        if suspects:
                            verdict = await review_gray_zone_duplicate(
                                http_client, config, raw, suspects
                            )
                            if verdict and verdict.is_duplicate and verdict.matched_event_id:
                                forced_dup_id = verdict.matched_event_id

                    event, action = db.upsert_from_source(
                        raw,
                        forced_duplicate_id=forced_dup_id,
                        auto_merge_similarity=config.dedup.auto_merge_similarity,
                    )
                    if action == "inserted":
                        summary.inserted += 1
                    elif action == "updated":
                        summary.updated += 1
                    elif action == "deduped":
                        summary.deduped += 1
                    elif action == "skipped_locked":
                        summary.skipped_locked += 1
                    if raw.needs_review and action in ("inserted", "updated"):
                        summary.needs_review += 1
                    if action in ("inserted", "updated"):
                        rebuild_reminders_for_event(db, config, event, now=ref_now)
            except Exception as exc:
                logger.exception("Source '%s' sync failed", src_cfg.id)
                summary.ok = False
                summary.error = str(exc)
            summaries.append(summary)

        if config.cleanup_ended_after_days >= 0:
            cleaned_events = db.cleanup_ended_events(
                config.cleanup_ended_after_days,
                now=now or utc_now(),
                keep_sync_runs=config.keep_sync_runs,
            )
            if cleaned_events > 0:
                logger.info(
                    "Cleaned up %d ended event(s) older than %d day(s)",
                    cleaned_events,
                    config.cleanup_ended_after_days,
                )

        finished_at = utc_now()
        report = SyncReport(
            started_at=started_at,
            finished_at=finished_at,
            cleaned_events=cleaned_events,
            muted_unwatched=muted_unwatched,
            restored_watched=restored_watched,
            sources=summaries,
        )
        db.record_sync_run(started_at, finished_at, report.model_dump_json())

        if (
            config.notifications.enabled
            and config.scheduler.notify_on_sync_issues
            and build_enabled_notifiers(config)
        ):
            failed_sources = [s for s in summaries if not s.ok]
            review_count = sum(s.needs_review for s in summaries)
            if failed_sources or review_count > 0:
                lines = []
                for s in failed_sources:
                    lines.append(f"同步失败 [{s.source_id}]: {s.error}")
                if review_count > 0:
                    lines.append(f"新增/更新 {review_count} 条待人工核对事件（needs_review=true），请通过 CLI 或 API 检查。")
                msg = NotificationMessage(
                    title="[GER] 每日活动同步需要注意",
                    body="\n".join(lines),
                    priority=2,
                    tags=["warning"],
                    group="GER-System",
                )
                try:
                    await send_notification_to_all(config, msg, client=http_client)
                except Exception:
                    logger.exception("Failed to send sync issue summary notification")

    if client is not None:
        await _run(client)
    else:
        async with httpx.AsyncClient() as owned_client:
            await _run(owned_client)

    return SyncReport(
        started_at=started_at,
        finished_at=utc_now(),
        cleaned_events=cleaned_events,
        muted_unwatched=muted_unwatched,
        restored_watched=restored_watched,
        sources=summaries,
    )
