"""SQLite persistence layer with WAL mode and manual-override protection."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator

from ger.models import (
    EventRecord,
    EventStatus,
    EventType,
    RawEvent,
    ReminderRecord,
    ReminderStatus,
    normalize_event_type,
)
from ger.time_utils import format_utc_iso, parse_iso_datetime, utc_now

_TITLE_NOISE_RE = re.compile(
    r"(?:限时活动|网页活动|主题活动|版本活动|角色活动祈愿|武器活动祈愿|特别节目|"
    r"前瞻直播|限时|活动|开启|上线|公告|预告|国服|即将|全新|limited|event)",
    re.IGNORECASE,
)


def normalize_title_for_dedup(title: str) -> str:
    """NFKC normalize, strip all punctuation/symbols/emojis/spaces and common wrapper words."""
    nfkc = unicodedata.normalize("NFKC", title).lower()
    cleaned = "".join(
        ch for ch in nfkc if unicodedata.category(ch)[0] not in ("P", "S", "Z", "C")
    )
    stripped = _TITLE_NOISE_RE.sub("", cleaned)
    return stripped if len(stripped) >= 2 else cleaned


def _titles_similar(a: str, b: str) -> float:
    na = normalize_title_for_dedup(a)
    nb = normalize_title_for_dedup(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 3 and shorter in longer:
        return 0.9
    return SequenceMatcher(None, na, nb).ratio()


def _times_compatible(
    s1: datetime | None,
    e1: datetime | None,
    s2: datetime | None,
    e2: datetime | None,
    *,
    max_diff_hours: float,
) -> bool:
    diffs: list[float] = []
    if s1 is not None and s2 is not None:
        diffs.append(abs((s1 - s2).total_seconds()) / 3600.0)
    if e1 is not None and e2 is not None:
        diffs.append(abs((e1 - e2).total_seconds()) / 3600.0)
    if not diffs:
        # Compare cross endpoints only if both are point-like or mismatched
        a1 = s1 or e1
        a2 = s2 or e2
        if a1 is not None and a2 is not None:
            return abs((a1 - a2).total_seconds()) / 3600.0 <= max_diff_hours
        return False
    return all(d <= max_diff_hours for d in diffs)

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    external_key TEXT NOT NULL,
    game TEXT NOT NULL,
    title TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'event',
    start_time TEXT,
    end_time TEXT,
    url TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    manual_locked INTEGER NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 0,
    is_structured INTEGER NOT NULL DEFAULT 0,
    unwatched_muted INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, external_key)
);

CREATE INDEX IF NOT EXISTS idx_events_game_status ON events(game, status);
CREATE INDEX IF NOT EXISTS idx_events_end_time ON events(end_time);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    rule_key TEXT NOT NULL,
    anchor TEXT NOT NULL,
    offset_label TEXT NOT NULL,
    trigger_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, rule_key)
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, trigger_at);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);
"""


def _row_to_event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        source_id=row["source_id"],
        external_key=row["external_key"],
        game=row["game"],
        title=row["title"],
        event_type=row["event_type"],
        start_time=parse_iso_datetime(row["start_time"]) if row["start_time"] else None,
        end_time=parse_iso_datetime(row["end_time"]) if row["end_time"] else None,
        url=row["url"],
        notes=row["notes"],
        status=EventStatus(row["status"]),
        manual_locked=bool(row["manual_locked"]),
        needs_review=bool(row["needs_review"]),
        created_at=parse_iso_datetime(row["created_at"]),
        updated_at=parse_iso_datetime(row["updated_at"]),
    )


def _row_to_reminder(row: sqlite3.Row) -> ReminderRecord:
    return ReminderRecord(
        id=row["id"],
        event_id=row["event_id"],
        rule_key=row["rule_key"],
        anchor=row["anchor"],
        offset_label=row["offset_label"],
        trigger_at=parse_iso_datetime(row["trigger_at"]),
        status=ReminderStatus(row["status"]),
        retry_count=row["retry_count"],
        last_error=row["last_error"],
        sent_at=parse_iso_datetime(row["sent_at"]) if row["sent_at"] else None,
        created_at=parse_iso_datetime(row["created_at"]),
        updated_at=parse_iso_datetime(row["updated_at"]),
    )


class Database:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            # Idempotent migration for existing databases created before is_structured existed
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
            if "is_structured" not in cols:
                conn.execute(
                    "ALTER TABLE events ADD COLUMN is_structured INTEGER NOT NULL DEFAULT 0"
                )
            if "unwatched_muted" not in cols:
                conn.execute(
                    "ALTER TABLE events ADD COLUMN unwatched_muted INTEGER NOT NULL DEFAULT 0"
                )

    def _find_fuzzy_duplicate(
        self,
        conn: sqlite3.Connection,
        raw: RawEvent,
        norm_type: str,
        *,
        auto_merge_similarity: float = 0.80,
    ) -> sqlite3.Row | None:
        """Find a high-confidence duplicate of the same game across sources or AI title drift."""
        candidates = conn.execute(
            "SELECT * FROM events WHERE game = ? ORDER BY updated_at DESC LIMIT 200",
            (raw.game,),
        ).fetchall()

        best_row: sqlite3.Row | None = None
        best_score = 0.0
        for row in candidates:
            existing_start = parse_iso_datetime(row["start_time"]) if row["start_time"] else None
            existing_end = parse_iso_datetime(row["end_time"]) if row["end_time"] else None
            sim = _titles_similar(raw.title, row["title"])

            # Rule 1: High title similarity + compatible schedule (within 48h)
            if sim >= auto_merge_similarity and _times_compatible(
                raw.start_time, raw.end_time, existing_start, existing_end, max_diff_hours=48.0
            ):
                if sim > best_score:
                    best_score = sim
                    best_row = row
                continue

            # Rule 2: Same canonical type + tight schedule match (both endpoints within 2h) + moderate title match
            if (
                normalize_event_type(row["event_type"]) == norm_type
                and raw.start_time is not None
                and raw.end_time is not None
                and existing_start is not None
                and existing_end is not None
                and _times_compatible(
                    raw.start_time, raw.end_time, existing_start, existing_end, max_diff_hours=2.0
                )
                and sim >= 0.50
            ):
                score = sim + 0.2
                if score > best_score:
                    best_score = score
                    best_row = row

        return best_row

    def find_gray_zone_candidates(
        self,
        raw: RawEvent,
        *,
        auto_merge_similarity: float = 0.80,
        gray_zone_min_similarity: float = 0.25,
    ) -> list[EventRecord]:
        """Return same-game events that might be aliases/duplicates and warrant AI verification.

        Returns empty list if an exact or high-confidence rule match already exists.
        """
        norm_type = normalize_event_type(raw.event_type)
        with self.connect() as conn:
            exact = conn.execute(
                "SELECT id FROM events WHERE source_id = ? AND external_key = ?",
                (raw.source_id, raw.external_key),
            ).fetchone()
            if exact is not None:
                return []

            if (
                self._find_fuzzy_duplicate(
                    conn, raw, norm_type, auto_merge_similarity=auto_merge_similarity
                )
                is not None
            ):
                return []

            rows = conn.execute(
                "SELECT * FROM events WHERE game = ? ORDER BY updated_at DESC LIMIT 100",
                (raw.game,),
            ).fetchall()

            suspects: list[EventRecord] = []
            for row in rows:
                ex_start = parse_iso_datetime(row["start_time"]) if row["start_time"] else None
                ex_end = parse_iso_datetime(row["end_time"]) if row["end_time"] else None
                sim = _titles_similar(raw.title, row["title"])
                same_type = normalize_event_type(row["event_type"]) == norm_type

                # Case A: Partial title overlap + schedules within 72h
                if gray_zone_min_similarity <= sim < auto_merge_similarity and _times_compatible(
                    raw.start_time, raw.end_time, ex_start, ex_end, max_diff_hours=72.0
                ):
                    suspects.append(_row_to_event(row))
                    continue

                # Case B: Same canonical category with very close start/end (<=12h) even if titles
                # look completely different (e.g. "7.0 上半卡池" vs specific character banner name)
                if same_type and _times_compatible(
                    raw.start_time, raw.end_time, ex_start, ex_end, max_diff_hours=12.0
                ):
                    suspects.append(_row_to_event(row))

            return suspects[:8]

    def upsert_from_source(
        self,
        raw: RawEvent,
        *,
        forced_duplicate_id: int | None = None,
        auto_merge_similarity: float = 0.80,
    ) -> tuple[EventRecord, str]:
        """Insert or update an event from an automated source with cross-source/fuzzy deduplication.

        Authority priority: manual_locked > structured source > AI source.
        Returns (event_record, action) where action is one of:
        'inserted' | 'updated' | 'unchanged' | 'deduped' | 'skipped_locked'
        """
        now_iso = format_utc_iso(utc_now())
        norm_type = normalize_event_type(raw.event_type)
        start_iso = format_utc_iso(raw.start_time)
        end_iso = format_utc_iso(raw.end_time)
        raw_json = json.dumps(raw.raw_payload, ensure_ascii=False) if raw.raw_payload else None
        is_struct_int = 1 if raw.is_structured_source else 0

        with self.connect() as conn:
            existing: sqlite3.Row | None = None
            matched_via_fuzzy = False

            if forced_duplicate_id is not None:
                existing = conn.execute(
                    "SELECT * FROM events WHERE id = ?",
                    (forced_duplicate_id,),
                ).fetchone()
                matched_via_fuzzy = existing is not None

            if existing is None:
                existing = conn.execute(
                    "SELECT * FROM events WHERE source_id = ? AND external_key = ?",
                    (raw.source_id, raw.external_key),
                ).fetchone()

            if existing is None:
                existing = self._find_fuzzy_duplicate(
                    conn, raw, norm_type, auto_merge_similarity=auto_merge_similarity
                )
                matched_via_fuzzy = existing is not None

            if existing is None:
                cur = conn.execute(
                    """
                    INSERT INTO events (
                        source_id, external_key, game, title, event_type,
                        start_time, end_time, url, notes, status,
                        manual_locked, needs_review, is_structured, raw_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        raw.source_id,
                        raw.external_key,
                        raw.game,
                        raw.title,
                        norm_type,
                        start_iso,
                        end_iso,
                        raw.url,
                        raw.notes,
                        1 if raw.needs_review else 0,
                        is_struct_int,
                        raw_json,
                        now_iso,
                        now_iso,
                    ),
                )
                row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
                return _row_to_event(row), "inserted"

            if existing["manual_locked"]:
                return _row_to_event(existing), "skipped_locked"

            # Structured source takes precedence over AI source during cross-source dedup
            existing_is_struct = bool(existing["is_structured"])
            if matched_via_fuzzy and existing_is_struct and not raw.is_structured_source:
                # Optionally enrich missing url/notes without touching authoritative schedule
                enrich_url = existing["url"] or raw.url
                enrich_notes = existing["notes"] or raw.notes
                if enrich_url != existing["url"] or enrich_notes != existing["notes"]:
                    conn.execute(
                        "UPDATE events SET url = ?, notes = ?, updated_at = ? WHERE id = ?",
                        (enrich_url, enrich_notes, now_iso, existing["id"]),
                    )
                    row = conn.execute("SELECT * FROM events WHERE id = ?", (existing["id"],)).fetchone()
                    return _row_to_event(row), "deduped"
                return _row_to_event(existing), "deduped"

            # If a structured source supersedes an earlier AI-collected record, upgrade its key
            upgrade_source = matched_via_fuzzy and raw.is_structured_source and not existing_is_struct
            new_source_id = raw.source_id if upgrade_source else existing["source_id"]
            new_ext_key = raw.external_key if upgrade_source else existing["external_key"]
            new_is_struct = 1 if (existing_is_struct or raw.is_structured_source) else 0

            # Avoid overwriting a good title with a noisier one when fuzzy-matched within AI sources
            new_title = raw.title if (not matched_via_fuzzy or upgrade_source) else existing["title"]

            changed = (
                existing["game"] != raw.game
                or existing["title"] != new_title
                or existing["event_type"] != norm_type
                or existing["start_time"] != start_iso
                or existing["end_time"] != end_iso
                or existing["url"] != (raw.url or existing["url"])
                or existing["notes"] != (raw.notes or existing["notes"])
                or bool(existing["needs_review"]) != raw.needs_review
                or upgrade_source
            )
            if not changed:
                return _row_to_event(existing), ("deduped" if matched_via_fuzzy else "unchanged")

            conn.execute(
                """
                UPDATE events
                SET source_id = ?, external_key = ?, game = ?, title = ?, event_type = ?,
                    start_time = ?, end_time = ?, url = ?, notes = ?,
                    needs_review = ?, is_structured = ?, raw_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_source_id,
                    new_ext_key,
                    raw.game,
                    new_title,
                    norm_type,
                    start_iso,
                    end_iso,
                    raw.url or existing["url"],
                    raw.notes or existing["notes"],
                    1 if raw.needs_review else 0,
                    new_is_struct,
                    raw_json,
                    now_iso,
                    existing["id"],
                ),
            )
            row = conn.execute("SELECT * FROM events WHERE id = ?", (existing["id"],)).fetchone()
            return _row_to_event(row), "updated"

    def create_manual_event(
        self,
        *,
        game: str,
        title: str,
        event_type: str = EventType.INGAME_EVENT.value,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        url: str | None = None,
        notes: str | None = None,
    ) -> EventRecord:
        now_iso = format_utc_iso(utc_now())
        ext_key = f"manual-{uuid.uuid4().hex[:12]}"
        norm_type = normalize_event_type(event_type)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO events (
                    source_id, external_key, game, title, event_type,
                    start_time, end_time, url, notes, status,
                    manual_locked, needs_review, is_structured, created_at, updated_at
                ) VALUES ('manual', ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, 0, 1, ?, ?)
                """,
                (
                    ext_key,
                    game,
                    title,
                    norm_type,
                    format_utc_iso(start_time),
                    format_utc_iso(end_time),
                    url,
                    notes,
                    now_iso,
                    now_iso,
                ),
            )
            row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
            return _row_to_event(row)

    def get_event(self, event_id: int) -> EventRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return _row_to_event(row) if row else None

    def list_events(
        self,
        *,
        game: str | None = None,
        status: EventStatus | None = None,
        needs_review: bool | None = None,
        include_past: bool = False,
        limit: int = 100,
    ) -> list[EventRecord]:
        clauses: list[str] = []
        params: list[Any] = []

        if game:
            clauses.append("game = ?")
            params.append(game)
        if status:
            clauses.append("status = ?")
            params.append(status.value)
        if needs_review is not None:
            clauses.append("needs_review = ?")
            params.append(1 if needs_review else 0)
        if not include_past:
            now_iso = format_utc_iso(utc_now())
            clauses.append(
                "(end_time IS NULL AND (start_time IS NULL OR start_time >= ?)) "
                "OR (end_time IS NOT NULL AND end_time >= ?)"
            )
            params.extend([now_iso, now_iso])

        where = f"WHERE {' AND '.join(f'({c})' for c in clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM events
            {where}
            ORDER BY COALESCE(end_time, start_time, '9999-12-31T00:00:00Z') ASC
            LIMIT ?
        """
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_event(r) for r in rows]

    def update_event(
        self,
        event_id: int,
        *,
        fields: dict[str, Any],
        lock_manual: bool = True,
    ) -> EventRecord | None:
        allowed = {
            "game",
            "title",
            "event_type",
            "start_time",
            "end_time",
            "url",
            "notes",
            "status",
            "needs_review",
            "manual_locked",
        }
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("start_time", "end_time"):
                updates[key] = format_utc_iso(value) if isinstance(value, datetime) else value
            elif key == "status" and isinstance(value, EventStatus):
                updates[key] = value.value
            elif key in ("needs_review", "manual_locked"):
                updates[key] = 1 if value else 0
            else:
                updates[key] = value

        if lock_manual and "manual_locked" not in updates:
            updates["manual_locked"] = 1
        # Explicit user status changes supersede automatic unwatched-muting state
        if "status" in updates:
            updates["unwatched_muted"] = 0
        updates["updated_at"] = format_utc_iso(utc_now())

        if not updates:
            return self.get_event(event_id)

        set_sql = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [event_id]

        with self.connect() as conn:
            cur = conn.execute(f"UPDATE events SET {set_sql} WHERE id = ?", params)
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return _row_to_event(row)

    def delete_event(self, event_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            return cur.rowcount > 0

    def list_reminders_for_event(self, event_id: int) -> list[ReminderRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE event_id = ? ORDER BY trigger_at ASC",
                (event_id,),
            ).fetchall()
            return [_row_to_reminder(r) for r in rows]

    def list_due_reminders(self, now: datetime) -> list[tuple[ReminderRecord, EventRecord]]:
        now_iso = format_utc_iso(now)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.* FROM reminders r
                JOIN events e ON e.id = r.event_id
                WHERE r.status = 'pending'
                  AND e.status = 'active'
                  AND r.trigger_at <= ?
                ORDER BY r.trigger_at ASC
                """,
                (now_iso,),
            ).fetchall()
            result: list[tuple[ReminderRecord, EventRecord]] = []
            for r in rows:
                ev_row = conn.execute("SELECT * FROM events WHERE id = ?", (r["event_id"],)).fetchone()
                if ev_row:
                    result.append((_row_to_reminder(r), _row_to_event(ev_row)))
            return result

    def list_queued_reminders(self) -> list[tuple[ReminderRecord, EventRecord]]:
        """List reminders currently waiting in the consolidated digest queue."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.* FROM reminders r
                JOIN events e ON e.id = r.event_id
                WHERE r.status = 'queued'
                  AND e.status = 'active'
                ORDER BY r.trigger_at ASC
                """
            ).fetchall()
            result: list[tuple[ReminderRecord, EventRecord]] = []
            for r in rows:
                ev_row = conn.execute("SELECT * FROM events WHERE id = ?", (r["event_id"],)).fetchone()
                if ev_row:
                    result.append((_row_to_reminder(r), _row_to_event(ev_row)))
            return result

    def mark_reminder_status(
        self,
        reminder_id: int,
        status: ReminderStatus,
        *,
        retry_count: int | None = None,
        last_error: str | None = None,
        sent_at: datetime | None = None,
    ) -> None:
        now_iso = format_utc_iso(utc_now())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reminders
                SET status = ?,
                    retry_count = COALESCE(?, retry_count),
                    last_error = ?,
                    sent_at = COALESCE(?, sent_at),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    retry_count,
                    last_error,
                    format_utc_iso(sent_at) if sent_at else None,
                    now_iso,
                    reminder_id,
                ),
            )

    def record_sync_run(self, started_at: datetime, finished_at: datetime, report_json: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sync_runs (started_at, finished_at, report_json) VALUES (?, ?, ?)",
                (format_utc_iso(started_at), format_utc_iso(finished_at), report_json),
            )

    def get_latest_sync_run(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "report": json.loads(row["report_json"]),
            }

    def reconcile_watched_games(
        self,
        watched_games: list[str],
    ) -> tuple[int, list[EventRecord]]:
        """Mute active events of unwatched games (without deleting them) and restore re-watched ones.

        - When `watched_games` is empty, the master switch is disabled; restore any previously
          auto-muted (`unwatched_muted=1`) events so nothing stays silently stuck.
        - Events manually muted by the user have `unwatched_muted=0` and are never auto-restored.
        Returns (muted_count, restored_events_needing_reminder_rebuild).
        """
        now_iso = format_utc_iso(utc_now())
        muted_count = 0
        restored_events: list[EventRecord] = []

        with self.connect() as conn:
            if not watched_games:
                rows = conn.execute(
                    "SELECT * FROM events WHERE status = 'muted' AND unwatched_muted = 1"
                ).fetchall()
                for row in rows:
                    conn.execute(
                        "UPDATE events SET status = 'active', unwatched_muted = 0, updated_at = ? WHERE id = ?",
                        (now_iso, row["id"]),
                    )
                    updated = conn.execute("SELECT * FROM events WHERE id = ?", (row["id"],)).fetchone()
                    restored_events.append(_row_to_event(updated))
                return 0, restored_events

            placeholders = ",".join("?" for _ in watched_games)

            # 1. Auto-mute active events whose game was removed from watched_games
            to_mute = conn.execute(
                f"SELECT id FROM events WHERE status = 'active' AND game NOT IN ({placeholders})",
                watched_games,
            ).fetchall()
            for row in to_mute:
                ev_id = row["id"]
                conn.execute(
                    "UPDATE events SET status = 'muted', unwatched_muted = 1, updated_at = ? WHERE id = ?",
                    (now_iso, ev_id),
                )
                conn.execute(
                    "UPDATE reminders SET status = ?, updated_at = ? WHERE event_id = ? AND status IN (?, ?)",
                    (
                        ReminderStatus.MUTED.value,
                        now_iso,
                        ev_id,
                        ReminderStatus.PENDING.value,
                        ReminderStatus.QUEUED.value,
                    ),
                )
                muted_count += 1

            # 2. Restore previously auto-muted events whose game is back in watched_games
            to_restore = conn.execute(
                f"""
                SELECT id FROM events
                WHERE status = 'muted' AND unwatched_muted = 1 AND game IN ({placeholders})
                """,
                watched_games,
            ).fetchall()
            for row in to_restore:
                ev_id = row["id"]
                conn.execute(
                    "UPDATE events SET status = 'active', unwatched_muted = 0, updated_at = ? WHERE id = ?",
                    (now_iso, ev_id),
                )
                updated = conn.execute("SELECT * FROM events WHERE id = ?", (ev_id,)).fetchone()
                restored_events.append(_row_to_event(updated))

        return muted_count, restored_events

    def cleanup_ended_events(
        self,
        retention_days: int,
        *,
        now: datetime | None = None,
        keep_sync_runs: int = 30,
    ) -> int:
        """Delete events that ended more than `retention_days` ago (cascades to reminders)."""
        if retention_days < 0:
            return 0
        current = now or utc_now()
        cutoff_iso = format_utc_iso(current - timedelta(days=retention_days))
        with self.connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM events
                WHERE (end_time IS NOT NULL AND end_time <= ?)
                   OR (end_time IS NULL AND start_time IS NOT NULL AND start_time <= ?)
                """,
                (cutoff_iso, cutoff_iso),
            )
            deleted = cur.rowcount
            if keep_sync_runs > 0:
                conn.execute(
                    """
                    DELETE FROM sync_runs
                    WHERE id NOT IN (
                        SELECT id FROM sync_runs ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (keep_sync_runs,),
                )
            return deleted
