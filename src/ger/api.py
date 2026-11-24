"""Lightweight FastAPI management API with Bearer Token auth and Swagger /docs."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from ger import __version__
from ger.config import AppConfig
from ger.db import Database
from ger.dispatcher import (
    dispatch_due_reminders,
    enqueue_due_reminders,
    flush_digest_queue,
    format_digest_message,
    send_notification_to_all,
)
from ger.models import (
    DispatchReport,
    EventRecord,
    EventStatus,
    ReminderRecord,
    SyncReport,
)
from ger.notifiers import NotificationMessage
from ger.reminders import rebuild_reminders_for_event
from ger.scheduler import BackgroundScheduler
from ger.sync import run_sync
from ger.time_utils import parse_iso_datetime, utc_now

logger = logging.getLogger(__name__)
_bearer_scheme = HTTPBearer(auto_error=False)
_WEB_INDEX_PATH = Path(__file__).resolve().parent / "web" / "index.html"


class EventCreateRequest(BaseModel):
    game: str
    title: str
    event_type: str = "ingame_event"
    start_time: str | None = None
    end_time: str | None = None
    url: str | None = None
    notes: str | None = None


class EventPatchRequest(BaseModel):
    game: str | None = None
    title: str | None = None
    event_type: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    url: str | None = None
    notes: str | None = None
    status: EventStatus | None = None
    needs_review: bool | None = None
    manual_locked: bool | None = None


class EventDetailResponse(BaseModel):
    event: EventRecord
    reminders: list[ReminderRecord]


class NotifyTestRequest(BaseModel):
    title: str = "[GER] 测试通知"
    body: str = "这是一条来自 Game Event Reminder 的测试通知。"
    url: str | None = None


def create_app(
    config: AppConfig,
    *,
    db: Database | None = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    database = db or Database(config.db_path)
    scheduler: BackgroundScheduler | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal scheduler
        _, restored = database.reconcile_watched_games(config.watched_games)
        for ev in restored:
            rebuild_reminders_for_event(database, config, ev)
        if enable_scheduler:
            scheduler = BackgroundScheduler(
                config=config,
                sync_callback=lambda: run_sync(database, config),
                dispatch_callback=lambda: dispatch_due_reminders(
                    database,
                    config,
                    flush_digest=(config.notifications.mode == "realtime"),
                ),
                digest_callback=lambda: flush_digest_queue(database, config),
            )
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                await scheduler.stop()

    app = FastAPI(
        title="Game Event Reminder API",
        version=__version__,
        description="Lightweight management API for inspecting and editing game events & reminders.",
        lifespan=lifespan,
    )

    def verify_auth(
        credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    ) -> None:
        expected = config.api.auth_token.strip()
        if not expected:
            return
        if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing Bearer token",
            )

    @app.get("/", include_in_schema=False)
    async def root() -> HTMLResponse:
        return HTMLResponse(content=_WEB_INDEX_PATH.read_text(encoding="utf-8"))

    @app.get("/auth/verify", tags=["system"])
    async def auth_verify(_: None = Depends(verify_auth)) -> dict[str, bool]:
        return {"ok": True, "auth_required": bool(config.api.auth_token.strip())}

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "timezone": config.timezone,
            "scheduler_running": scheduler is not None,
            "notifications_enabled": config.notifications.enabled,
            "notification_mode": config.notifications.mode,
            "digest_cron": config.notifications.digest_cron,
        }

    @app.get("/events", response_model=list[EventRecord], tags=["events"])
    async def list_events(
        game: str | None = Query(default=None),
        event_status: EventStatus | None = Query(default=None, alias="status"),
        needs_review: bool | None = Query(default=None),
        include_past: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
        _: None = Depends(verify_auth),
    ) -> list[EventRecord]:
        return database.list_events(
            game=game,
            status=event_status,
            needs_review=needs_review,
            include_past=include_past,
            limit=limit,
        )

    @app.get("/events/{event_id}", response_model=EventDetailResponse, tags=["events"])
    async def get_event(event_id: int, _: None = Depends(verify_auth)) -> EventDetailResponse:
        event = database.get_event(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        reminders = database.list_reminders_for_event(event_id)
        return EventDetailResponse(event=event, reminders=reminders)

    @app.post(
        "/events",
        response_model=EventDetailResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["events"],
    )
    async def create_event(
        body: EventCreateRequest,
        _: None = Depends(verify_auth),
    ) -> EventDetailResponse:
        try:
            start_dt = (
                parse_iso_datetime(body.start_time, config.timezone) if body.start_time else None
            )
            end_dt = parse_iso_datetime(body.end_time, config.timezone) if body.end_time else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        event = database.create_manual_event(
            game=body.game,
            title=body.title,
            event_type=body.event_type,
            start_time=start_dt,
            end_time=end_dt,
            url=body.url,
            notes=body.notes,
        )
        rebuild_reminders_for_event(database, config, event)
        return EventDetailResponse(
            event=event,
            reminders=database.list_reminders_for_event(event.id),
        )

    @app.patch("/events/{event_id}", response_model=EventDetailResponse, tags=["events"])
    async def patch_event(
        event_id: int,
        body: EventPatchRequest,
        _: None = Depends(verify_auth),
    ) -> EventDetailResponse:
        raw_updates = body.model_dump(exclude_unset=True)
        try:
            if "start_time" in raw_updates and raw_updates["start_time"] is not None:
                raw_updates["start_time"] = parse_iso_datetime(
                    raw_updates["start_time"], config.timezone
                )
            if "end_time" in raw_updates and raw_updates["end_time"] is not None:
                raw_updates["end_time"] = parse_iso_datetime(
                    raw_updates["end_time"], config.timezone
                )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        lock_manual = raw_updates.get("manual_locked", True)
        event = database.update_event(event_id, fields=raw_updates, lock_manual=bool(lock_manual))
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        rebuild_reminders_for_event(database, config, event)
        return EventDetailResponse(
            event=event,
            reminders=database.list_reminders_for_event(event.id),
        )

    @app.delete("/events/{event_id}", tags=["events"])
    async def delete_event(event_id: int, _: None = Depends(verify_auth)) -> dict[str, bool]:
        if not database.delete_event(event_id):
            raise HTTPException(status_code=404, detail="Event not found")
        return {"deleted": True}

    @app.post("/sync", response_model=SyncReport, tags=["operations"])
    async def trigger_sync(
        source_id: str | None = Query(default=None),
        _: None = Depends(verify_auth),
    ) -> SyncReport:
        try:
            return await run_sync(database, config, source_id=source_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/sync/latest", tags=["operations"])
    async def latest_sync(_: None = Depends(verify_auth)) -> dict[str, Any]:
        latest = database.get_latest_sync_run()
        return latest or {"message": "No sync runs recorded yet"}

    @app.get("/queue", tags=["operations"])
    async def get_digest_queue(_: None = Depends(verify_auth)) -> dict[str, Any]:
        now = utc_now()
        enqueue_due_reminders(database, config, now=now)
        queued = database.list_queued_reminders()
        preview = format_digest_message(queued, config.timezone, now) if queued else None
        return {
            "notifications_enabled": config.notifications.enabled,
            "mode": config.notifications.mode,
            "digest_cron": config.notifications.digest_cron,
            "count": len(queued),
            "preview_title": preview.title if preview else None,
            "preview_body": preview.body if preview else None,
            "items": [
                {
                    "reminder_id": rem.id,
                    "event_id": ev.id,
                    "game": ev.game,
                    "title": ev.title,
                    "event_type": ev.event_type,
                    "rule_key": rem.rule_key,
                    "trigger_at": rem.trigger_at,
                }
                for rem, ev in queued
            ],
        }

    @app.post("/dispatch", response_model=DispatchReport, tags=["operations"])
    async def trigger_dispatch(
        enqueue_only: bool = Query(default=False),
        _: None = Depends(verify_auth),
    ) -> DispatchReport:
        return await dispatch_due_reminders(database, config, flush_digest=not enqueue_only)

    @app.post("/cleanup", tags=["operations"])
    async def trigger_cleanup(
        days: int | None = Query(default=None, ge=0),
        _: None = Depends(verify_auth),
    ) -> dict[str, int]:
        retention = config.cleanup_ended_after_days if days is None else days
        deleted = database.cleanup_ended_events(retention, keep_sync_runs=config.keep_sync_runs)
        return {"deleted": deleted, "retention_days": retention}

    @app.post("/notify/test", tags=["operations"])
    async def test_notify(
        body: NotifyTestRequest | None = None,
        _: None = Depends(verify_auth),
    ) -> dict[str, Any]:
        req = body or NotifyTestRequest()
        msg = NotificationMessage(title=req.title, body=req.body, url=req.url, priority=3)
        try:
            outcomes = await send_notification_to_all(config, msg)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "results": [{"notifier_id": nid, "ok": err is None, "error": err} for nid, err in outcomes]
        }

    return app
