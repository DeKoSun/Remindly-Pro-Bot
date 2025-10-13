"""Database helpers for the reminder bot.

The original project relied on Supabase/Postgres.  In this simplified
version we use a local SQLite database so the bot can run out of the box
without external services.  The module exposes a small set of helper
functions that mirror the old public API and are used by the bot and the
background schedulers.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from uuid import uuid4

from croniter import croniter, CroniterBadCronError

DB_URL = os.getenv("DATABASE_URL", "sqlite:///remindly.db")


def _resolve_path(url: str) -> Path:
    """Return a filesystem path for the SQLite database."""
    if url.startswith("sqlite:///"):
        return Path(url.replace("sqlite:///", "", 1)).expanduser()
    if url.startswith("file:"):
        return Path(url.replace("file:", "", 1)).expanduser()
    if "://" in url and not url.startswith("sqlite://"):
        raise RuntimeError(
            "Only SQLite DATABASE_URL values are supported in this demo build."
        )
    return Path(url).expanduser()


DB_PATH = _resolve_path(DB_URL)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    """Context manager that yields a SQLite connection."""
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | str | None) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _ensure_tables() -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_users (
                user_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT,
                tournament_subscribed INTEGER NOT NULL DEFAULT 0,
                tz TEXT NOT NULL DEFAULT 'Europe/Moscow',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                kind TEXT NOT NULL,
                cron_expr TEXT,
                remind_at TEXT,
                next_at TEXT,
                paused INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS reminders_due_idx
            ON reminders(paused, remind_at, next_at)
            """
        )


_ensure_tables()


# ---------------------------------------------------------------------------
# Telegram meta helpers
# ---------------------------------------------------------------------------

def upsert_telegram_user(user_id: int) -> None:
    now = _iso(_utcnow())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO telegram_users(user_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (user_id, now, now),
        )


def upsert_chat(chat_id: int, type_: str, title: Optional[str]) -> None:
    now = _iso(_utcnow())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO chats(chat_id, type, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                type=excluded.type,
                title=excluded.title,
                updated_at=excluded.updated_at
            """,
            (chat_id, type_, title, now, now),
        )


def set_tournament_subscription(
    chat_id: int, value: bool, user_id: Optional[int] = None
) -> None:
    if user_id:
        upsert_telegram_user(user_id)
    now = _iso(_utcnow())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO chats(chat_id, type, title, tournament_subscribed,
                              created_at, updated_at)
            VALUES (?, 'group', NULL, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                tournament_subscribed=excluded.tournament_subscribed,
                updated_at=excluded.updated_at
            """,
            (chat_id, 1 if value else 0, now, now),
        )


def get_tournament_subscribed_chats() -> list[tuple[int, Optional[str]]]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT chat_id, tz FROM chats WHERE tournament_subscribed = 1"
        )
        return [(row["chat_id"], row["tz"]) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def add_reminder(
    *,
    user_id: int,
    chat_id: int,
    text: str,
    remind_at: datetime,
    created_by: Optional[int] = None,
) -> dict:
    upsert_telegram_user(user_id)
    rid = str(uuid4())
    now = _iso(_utcnow())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO reminders(id, user_id, chat_id, text, kind, remind_at,
                                  paused, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'once', ?, 0, ?, ?, ?)
            """,
            (rid, user_id, chat_id, text, _iso(remind_at), created_by, now, now),
        )
        cur = conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,))
        return _row_to_dict(cur.fetchone())


def normalize_cron(expr: str) -> str:
    s = (expr or "").strip().lower()
    if s == "каждую минуту":
        return "* * * * *"
    if s.startswith("ежедневно"):
        s = s.replace("ежедневно", "", 1).strip()
    if ":" in s:
        try:
            hh, mm = s.split(":", 1)
            hh = int(hh)
            mm = int(mm)
        except ValueError:
            pass
        else:
            if 0 <= hh < 24 and 0 <= mm < 60:
                return f"{mm} {hh} * * *"
    return expr.strip()


def add_recurring_reminder(
    *,
    user_id: int,
    chat_id: int,
    text: str,
    cron_expr: str,
    created_by: Optional[int] = None,
    next_at: Optional[datetime] = None,
) -> dict:
    upsert_telegram_user(user_id)
    cron_expr = normalize_cron(cron_expr)
    try:
        if next_at is None:
            next_at = croniter(cron_expr, _utcnow()).get_next(datetime)
    except (CroniterBadCronError, ValueError) as exc:  # pragma: no cover - guard rail
        raise ValueError("Bad cron expression") from exc

    rid = str(uuid4())
    now = _iso(_utcnow())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO reminders(id, user_id, chat_id, text, kind, cron_expr,
                                  next_at, paused, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'repeat_cron', ?, ?, 0, ?, ?, ?)
            """,
            (rid, user_id, chat_id, text, cron_expr, _iso(next_at), created_by, now, now),
        )
        cur = conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,))
        return _row_to_dict(cur.fetchone())


def _fetch_reminders(where: str, params: Iterable) -> list[dict]:
    with get_conn() as conn:
        cur = conn.execute(
            f"""
            SELECT * FROM reminders
            WHERE {where}
            ORDER BY
                CASE WHEN remind_at IS NULL THEN 1 ELSE 0 END,
                remind_at,
                CASE WHEN next_at IS NULL THEN 1 ELSE 0 END,
                next_at,
                created_at
            """,
            tuple(params),
        )
        return [_row_to_dict(row) for row in cur.fetchall()]


def get_active_reminders(user_id: int) -> list[dict]:
    return _fetch_reminders("user_id = ? AND paused = 0", (user_id,))


def get_active_reminders_for_chat(
    chat_id: int, *, include_paused: bool = True
) -> list[dict]:
    where = "chat_id = ?"
    params: list = [chat_id]
    if not include_paused:
        where += " AND paused = 0"
    return _fetch_reminders(where, params)


def get_reminder_by_id(reminder_id: str) -> Optional[dict]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


def delete_reminder_by_id(reminder_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))


def set_paused(reminder_id: str, paused: bool) -> None:
    now = _iso(_utcnow())
    with get_conn() as conn:
        conn.execute(
            "UPDATE reminders SET paused = ?, updated_at = ? WHERE id = ?",
            (1 if paused else 0, now, reminder_id),
        )


__all__ = [
    "get_conn",
    "upsert_telegram_user",
    "upsert_chat",
    "set_tournament_subscription",
    "get_tournament_subscribed_chats",
    "add_reminder",
    "add_recurring_reminder",
    "get_active_reminders",
    "get_active_reminders_for_chat",
    "get_reminder_by_id",
    "delete_reminder_by_id",
    "set_paused",
    "normalize_cron",
]
