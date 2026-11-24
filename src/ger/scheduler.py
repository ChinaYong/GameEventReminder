"""Lightweight asyncio scheduler for daily sync, queue polling, and daily digest flush."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from croniter import croniter

from ger.config import AppConfig
from ger.time_utils import get_zone, utc_now

logger = logging.getLogger(__name__)


class BackgroundScheduler:
    """Zero-dependency-bloat asyncio loops for sync cron, digest cron, and queue polling."""

    def __init__(
        self,
        config: AppConfig,
        sync_callback: Callable[[], Awaitable[object]],
        dispatch_callback: Callable[[], Awaitable[object]],
        digest_callback: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self.config = config
        self._sync_callback = sync_callback
        self._dispatch_callback = dispatch_callback
        self._digest_callback = digest_callback
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._sync_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()

    def start(self) -> None:
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(self._sync_loop(), name="ger-sync-loop"),
            asyncio.create_task(self._dispatch_loop(), name="ger-dispatch-loop"),
        ]
        if self.config.notifications.mode == "digest" and self._digest_callback is not None:
            self._tasks.append(
                asyncio.create_task(self._digest_loop(), name="ger-digest-loop")
            )
        logger.info(
            "Background scheduler started (sync_cron='%s', mode='%s', digest_cron='%s', poll=%ss)",
            self.config.scheduler.sync_cron,
            self.config.notifications.mode,
            self.config.notifications.digest_cron,
            self.config.scheduler.dispatch_interval_seconds,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Background scheduler stopped")

    def _next_cron_time(self, expr: str, base: datetime | None = None) -> datetime:
        tz = get_zone(self.config.timezone)
        ref = (base or utc_now()).astimezone(tz)
        return croniter(expr, ref).get_next(datetime)

    def next_sync_time(self, base: datetime | None = None) -> datetime:
        return self._next_cron_time(self.config.scheduler.sync_cron, base=base)

    def next_digest_time(self, base: datetime | None = None) -> datetime:
        return self._next_cron_time(self.config.notifications.digest_cron, base=base)

    async def _sync_loop(self) -> None:
        while not self._stop_event.is_set():
            next_run = self.next_sync_time()
            delay = max(1.0, (next_run - utc_now().astimezone(next_run.tzinfo)).total_seconds())
            logger.info("Next scheduled sync at %s (in %.0fs)", next_run.isoformat(), delay)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                pass

            if self._sync_lock.locked():
                logger.warning("Skipping scheduled sync because a previous sync is still running")
                continue

            async with self._sync_lock:
                try:
                    await self._sync_callback()
                except Exception:
                    logger.exception("Scheduled sync failed")

    async def _digest_loop(self) -> None:
        assert self._digest_callback is not None
        while not self._stop_event.is_set():
            next_run = self.next_digest_time()
            delay = max(1.0, (next_run - utc_now().astimezone(next_run.tzinfo)).total_seconds())
            logger.info("Next scheduled digest at %s (in %.0fs)", next_run.isoformat(), delay)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                pass

            async with self._dispatch_lock:
                try:
                    await self._digest_callback()
                except Exception:
                    logger.exception("Scheduled digest flush failed")

    async def _dispatch_loop(self) -> None:
        interval = max(5, self.config.scheduler.dispatch_interval_seconds)
        while not self._stop_event.is_set():
            async with self._dispatch_lock:
                try:
                    await self._dispatch_callback()
                except Exception:
                    logger.exception("Scheduled reminder poll failed")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
            except TimeoutError:
                pass
