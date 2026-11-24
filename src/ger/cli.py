"""Lightweight Click CLI for running the daemon, cron jobs, and inspecting/editing events."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
import uvicorn

from ger.api import create_app
from ger.config import AppConfig, load_config
from ger.db import Database
from ger.dispatcher import (
    dispatch_due_reminders,
    enqueue_due_reminders,
    flush_digest_queue,
    format_digest_message,
    send_notification_to_all,
)
from ger.models import EventStatus
from ger.notifiers import NotificationMessage
from ger.reminders import rebuild_reminders_for_event
from ger.sync import run_sync
from ger.time_utils import format_local, parse_iso_datetime, utc_now


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _load(config_path: str) -> tuple[AppConfig, Database]:
    cfg = load_config(config_path)
    db = Database(cfg.db_path)
    return cfg, db


@click.group()
@click.option(
    "-c",
    "--config",
    "config_path",
    default="config.yaml",
    envvar="GER_CONFIG",
    show_default=True,
    help="Path to config YAML file.",
)
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, config_path: str, verbose: bool) -> None:
    """Game Event Reminder (ger) - Lightweight game event collector & reminder CLI."""
    _configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command()
@click.option("--no-scheduler", is_flag=True, help="Serve REST API only without background scheduler.")
@click.pass_context
def serve(ctx: click.Context, no_scheduler: bool) -> None:
    """Start the lightweight REST API + built-in scheduler daemon."""
    cfg, db = _load(ctx.obj["config_path"])
    if not cfg.api.enabled:
        click.echo("api.enabled is false in config; starting scheduler-only mode...")
        from ger.scheduler import BackgroundScheduler

        async def _run_scheduler_only() -> None:
            sched = BackgroundScheduler(
                config=cfg,
                sync_callback=lambda: run_sync(db, cfg),
                dispatch_callback=lambda: dispatch_due_reminders(
                    db, cfg, flush_digest=(cfg.notifications.mode == "realtime")
                ),
                digest_callback=lambda: flush_digest_queue(db, cfg),
            )
            sched.start()
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                await sched.stop()

        asyncio.run(_run_scheduler_only())
        return

    app = create_app(cfg, db=db, enable_scheduler=not no_scheduler)
    uvicorn.run(app, host=cfg.api.host, port=cfg.api.port, log_level="info")


@cli.command()
@click.option("--source", "source_id", default=None, help="Sync only a specific source ID.")
@click.pass_context
def sync(ctx: click.Context, source_id: str | None) -> None:
    """Run one-shot data collection & reminder rebuild (ideal for cron / systemd timer)."""
    cfg, db = _load(ctx.obj["config_path"])
    report = asyncio.run(run_sync(db, cfg, source_id=source_id))
    has_error = False
    for s in report.sources:
        status_str = "OK" if s.ok else f"FAIL ({s.error})"
        if not s.ok:
            has_error = True
        click.echo(
            f"[{s.source_id}] {status_str} | fetched={s.fetched} "
            f"inserted={s.inserted} updated={s.updated} deduped={s.deduped} "
            f"expired={s.skipped_expired} filtered={s.skipped_filtered} "
            f"locked={s.skipped_locked} needs_review={s.needs_review}"
        )
    if report.muted_unwatched > 0 or report.restored_watched > 0:
        click.echo(
            f"Watched games reconciled: muted={report.muted_unwatched} restored={report.restored_watched}"
        )
    if report.cleaned_events > 0:
        click.echo(f"Cleaned up {report.cleaned_events} ended event(s).")
    if has_error:
        sys.exit(1)


@cli.command()
@click.option(
    "--days",
    default=None,
    type=int,
    help="Delete events ended more than N days ago (defaults to config cleanup_ended_after_days).",
)
@click.pass_context
def cleanup(ctx: click.Context, days: int | None) -> None:
    """Manually clean up ended events and old sync logs."""
    cfg, db = _load(ctx.obj["config_path"])
    retention = cfg.cleanup_ended_after_days if days is None else days
    deleted = db.cleanup_ended_events(retention, keep_sync_runs=cfg.keep_sync_runs)
    click.echo(f"Deleted {deleted} ended event(s) (retention_days={retention}).")


@cli.command()
@click.option(
    "--enqueue-only",
    is_flag=True,
    help="In digest mode, only enqueue due reminders without sending the consolidated message yet.",
)
@click.pass_context
def dispatch(ctx: click.Context, enqueue_only: bool) -> None:
    """Enqueue due reminders and deliver notifications (1 consolidated digest in digest mode)."""
    cfg, db = _load(ctx.obj["config_path"])
    report = asyncio.run(dispatch_due_reminders(db, cfg, flush_digest=not enqueue_only))
    click.echo(
        f"mode={report.mode} enabled={report.notifications_enabled} "
        f"checked={report.checked} queued={report.queued} sent={report.sent} "
        f"digest_msgs={report.digest_messages_sent} skipped={report.skipped} failed={report.failed}"
    )
    if report.failed > 0:
        sys.exit(1)


@cli.command()
@click.pass_context
def queue(ctx: click.Context) -> None:
    """Inspect the pending digest notification queue and preview the consolidated message."""
    cfg, db = _load(ctx.obj["config_path"])
    now = utc_now()
    enqueue_due_reminders(db, cfg, now=now)
    queued = db.list_queued_reminders()
    click.echo(
        f"Notifications enabled={cfg.notifications.enabled} | mode={cfg.notifications.mode} "
        f"| digest_cron='{cfg.notifications.digest_cron}' | queued={len(queued)}"
    )
    if not queued:
        click.echo("Queue is empty.")
        return
    preview = format_digest_message(queued, cfg.timezone, now)
    click.echo("\n--- Digest Preview ---")
    click.echo(preview.title)
    click.echo(preview.body)


@cli.command("list")
@click.option("--game", default=None, help="Filter by game name.")
@click.option(
    "--status",
    "status_str",
    type=click.Choice(["active", "muted", "done"]),
    default=None,
    help="Filter by event status.",
)
@click.option("--review", is_flag=True, help="Show only events flagged as needs_review.")
@click.option("--all", "include_past", is_flag=True, help="Include ended/past events.")
@click.option("--limit", default=100, show_default=True)
@click.pass_context
def list_cmd(
    ctx: click.Context,
    game: str | None,
    status_str: str | None,
    review: bool,
    include_past: bool,
    limit: int,
) -> None:
    """List tracked game events."""
    cfg, db = _load(ctx.obj["config_path"])
    events = db.list_events(
        game=game,
        status=EventStatus(status_str) if status_str else None,
        needs_review=True if review else None,
        include_past=include_past,
        limit=limit,
    )
    if not events:
        click.echo("No events found.")
        return

    header = f"{'ID':<5} {'GAME':<14} {'TYPE':<10} {'STATUS':<7} {'FLAGS':<6} {'START':<20} {'END':<20} TITLE"
    click.echo(header)
    click.echo("-" * len(header) + "----------------")
    for ev in events:
        flags = ("L" if ev.manual_locked else "-") + ("R" if ev.needs_review else "-")
        start_s = format_local(ev.start_time, cfg.timezone)
        end_s = format_local(ev.end_time, cfg.timezone)
        click.echo(
            f"{ev.id:<5} {ev.game[:13]:<14} {ev.event_type[:9]:<10} "
            f"{ev.status.value:<7} {flags:<6} {start_s[:19]:<20} {end_s[:19]:<20} {ev.title}"
        )
    click.echo("\nFlags: L=manual_locked, R=needs_review")


@cli.command()
@click.argument("event_id", type=int)
@click.pass_context
def show(ctx: click.Context, event_id: int) -> None:
    """Inspect a single event and all of its scheduled reminders."""
    cfg, db = _load(ctx.obj["config_path"])
    ev = db.get_event(event_id)
    if ev is None:
        click.echo(f"Event #{event_id} not found.", err=True)
        sys.exit(1)

    click.echo(f"ID:            {ev.id}")
    click.echo(f"Source:        {ev.source_id} ({ev.external_key})")
    click.echo(f"Game:          {ev.game}")
    click.echo(f"Title:         {ev.title}")
    click.echo(f"Type:          {ev.event_type}")
    click.echo(f"Status:        {ev.status.value}")
    click.echo(f"Manual Locked: {ev.manual_locked}")
    click.echo(f"Needs Review:  {ev.needs_review}")
    click.echo(f"Start:         {format_local(ev.start_time, cfg.timezone)}")
    click.echo(f"End:           {format_local(ev.end_time, cfg.timezone)}")
    click.echo(f"URL:           {ev.url or '-'}")
    click.echo(f"Notes:         {ev.notes or '-'}")

    reminders = db.list_reminders_for_event(event_id)
    click.echo("\nReminders:")
    if not reminders:
        click.echo("  (none)")
        return
    for r in reminders:
        trig = format_local(r.trigger_at, cfg.timezone)
        err = f" | err={r.last_error}" if r.last_error else ""
        click.echo(f"  - [{r.status.value:<7}] {r.rule_key:<12} @ {trig}{err}")


@cli.command()
@click.option("--game", required=True, help="Game name.")
@click.option("--title", required=True, help="Event title.")
@click.option(
    "--type",
    "event_type",
    default="ingame_event",
    show_default=True,
    help="Event type: ingame_event | web_event | checkin_redeem | banner | version",
)
@click.option("--start", "start_str", default=None, help="ISO datetime, e.g. 2026-11-25T10:00:00+08:00")
@click.option("--end", "end_str", default=None, help="ISO datetime, e.g. 2026-12-05T03:59:00+08:00")
@click.option("--url", default=None)
@click.option("--notes", default=None)
@click.pass_context
def add(
    ctx: click.Context,
    game: str,
    title: str,
    event_type: str,
    start_str: str | None,
    end_str: str | None,
    url: str | None,
    notes: str | None,
) -> None:
    """Manually add a game event and generate its reminders."""
    cfg, db = _load(ctx.obj["config_path"])
    start_dt = parse_iso_datetime(start_str, cfg.timezone) if start_str else None
    end_dt = parse_iso_datetime(end_str, cfg.timezone) if end_str else None
    ev = db.create_manual_event(
        game=game,
        title=title,
        event_type=event_type,
        start_time=start_dt,
        end_time=end_dt,
        url=url,
        notes=notes,
    )
    rebuild_reminders_for_event(db, cfg, ev)
    click.echo(f"Created event #{ev.id}: [{ev.game}] {ev.title}")


@cli.command()
@click.argument("event_id", type=int)
@click.option("--game", default=None)
@click.option("--title", default=None)
@click.option("--type", "event_type", default=None)
@click.option("--start", "start_str", default=None, help="New ISO start time.")
@click.option("--end", "end_str", default=None, help="New ISO end time.")
@click.option("--url", default=None)
@click.option("--notes", default=None)
@click.option("--status", "status_str", type=click.Choice(["active", "muted", "done"]), default=None)
@click.option("--clear-review", is_flag=True, help="Mark needs_review=false.")
@click.option("--unlock", is_flag=True, help="Set manual_locked=false so future syncs can overwrite.")
@click.pass_context
def edit(
    ctx: click.Context,
    event_id: int,
    game: str | None,
    title: str | None,
    event_type: str | None,
    start_str: str | None,
    end_str: str | None,
    url: str | None,
    notes: str | None,
    status_str: str | None,
    clear_review: bool,
    unlock: bool,
) -> None:
    """Edit an event (auto-locks against sync overwrite unless --unlock is passed)."""
    cfg, db = _load(ctx.obj["config_path"])
    fields: dict[str, object] = {}
    if game is not None:
        fields["game"] = game
    if title is not None:
        fields["title"] = title
    if event_type is not None:
        fields["event_type"] = event_type
    if start_str is not None:
        fields["start_time"] = parse_iso_datetime(start_str, cfg.timezone)
    if end_str is not None:
        fields["end_time"] = parse_iso_datetime(end_str, cfg.timezone)
    if url is not None:
        fields["url"] = url
    if notes is not None:
        fields["notes"] = notes
    if status_str is not None:
        fields["status"] = EventStatus(status_str)
    if clear_review:
        fields["needs_review"] = False
    if unlock:
        fields["manual_locked"] = False

    if not fields:
        click.echo("No fields specified to update.", err=True)
        sys.exit(1)

    ev = db.update_event(event_id, fields=fields, lock_manual=not unlock)
    if ev is None:
        click.echo(f"Event #{event_id} not found.", err=True)
        sys.exit(1)
    rebuild_reminders_for_event(db, cfg, ev)
    click.echo(f"Updated event #{ev.id} (manual_locked={ev.manual_locked}, status={ev.status.value})")


@cli.command()
@click.argument("event_id", type=int)
@click.pass_context
def mute(ctx: click.Context, event_id: int) -> None:
    """Mute all remaining reminders for an event."""
    cfg, db = _load(ctx.obj["config_path"])
    ev = db.update_event(event_id, fields={"status": EventStatus.MUTED}, lock_manual=True)
    if ev is None:
        click.echo(f"Event #{event_id} not found.", err=True)
        sys.exit(1)
    rebuild_reminders_for_event(db, cfg, ev)
    click.echo(f"Muted event #{ev.id}: {ev.title}")


@cli.command()
@click.argument("event_id", type=int)
@click.pass_context
def done(ctx: click.Context, event_id: int) -> None:
    """Mark an event as done and stop remaining reminders."""
    cfg, db = _load(ctx.obj["config_path"])
    ev = db.update_event(event_id, fields={"status": EventStatus.DONE}, lock_manual=True)
    if ev is None:
        click.echo(f"Event #{event_id} not found.", err=True)
        sys.exit(1)
    rebuild_reminders_for_event(db, cfg, ev)
    click.echo(f"Marked event #{ev.id} as done: {ev.title}")


@cli.command()
@click.argument("event_id", type=int)
@click.pass_context
def delete(ctx: click.Context, event_id: int) -> None:
    """Delete an event and its reminders."""
    _, db = _load(ctx.obj["config_path"])
    if not db.delete_event(event_id):
        click.echo(f"Event #{event_id} not found.", err=True)
        sys.exit(1)
    click.echo(f"Deleted event #{event_id}")


@cli.command("test-notify")
@click.option("--message", default="这是一条来自 Game Event Reminder 的测试通知。", show_default=True)
@click.pass_context
def test_notify(ctx: click.Context, message: str) -> None:
    """Send a test notification to all enabled ntfy/Bark channels."""
    cfg, _ = _load(ctx.obj["config_path"])
    msg = NotificationMessage(title="[GER] 测试通知", body=message, priority=3)
    outcomes = asyncio.run(send_notification_to_all(cfg, msg))
    failed = False
    for nid, err in outcomes:
        if err is None:
            click.echo(f"[{nid}] OK")
        else:
            failed = True
            click.echo(f"[{nid}] FAIL: {err}", err=True)
    if failed:
        sys.exit(1)


@cli.command("init-config")
@click.option("--output", default="config.yaml", show_default=True)
def init_config(output: str) -> None:
    """Copy config.example.yaml if present to initialize a new config file."""
    dst = Path(output)
    if dst.exists():
        click.echo(f"{dst} already exists.", err=True)
        sys.exit(1)
    example = Path("config.example.yaml")
    if not example.exists():
        click.echo("config.example.yaml not found in current directory.", err=True)
        sys.exit(1)
    dst.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    click.echo(f"Created {dst} from {example}")


if __name__ == "__main__":
    cli()
