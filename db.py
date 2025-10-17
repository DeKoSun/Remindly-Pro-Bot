# db.py
import os
import asyncpg
from typing import Optional, Any

_pool: Optional[asyncpg.Pool] = None


async def db_pool() -> asyncpg.Pool:
    """
    Singleton-пул соединений к Supabase/Postgres.
    ВАЖНО: statement_cache_size=0 — безопасно для PgBouncer (transaction mode).
    """
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.getenv("DATABASE_URL"),
            min_size=1,
            max_size=5,
            command_timeout=10,                  # сек
            max_inactive_connection_lifetime=300,
            statement_cache_size=0,              # ключевое для PgBouncer
        )
    return _pool


async def close_db_pool() -> None:
    """Закрыть пул (если используешь on_shutdown)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# =========================
# Chats
# =========================
async def upsert_chat(chat_id: int, chat_type: str, title: Optional[str]) -> None:
    pool = await db_pool()
    await pool.execute(
        """
        INSERT INTO chats (chat_id, type, title)
        VALUES ($1, $2, $3)
        ON CONFLICT (chat_id)
        DO UPDATE SET type = EXCLUDED.type, title = EXCLUDED.title
        """,
        chat_id, chat_type, title,
    )


# =========================
# Reminders
# =========================
async def create_once(chat_id: int, user_id: int, text: str, remind_at_utc) -> str:
    pool = await db_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO reminders (chat_id, user_id, kind, text, remind_at, paused)
        VALUES ($1, $2, 'once', $3, $4, FALSE)
        RETURNING id::text
        """,
        chat_id, user_id, text, remind_at_utc,
    )
    return row["id"]


async def create_cron(
    chat_id: int,
    user_id: int,
    text: str,
    cron_expr: str,
    next_at_utc,
    category: Optional[str] = None,
    meta: Any = None,
) -> str:
    pool = await db_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO reminders (chat_id, user_id, kind, text, cron_expr, next_at, paused, category, meta)
        VALUES ($1, $2, 'cron', $3, $4, $5, FALSE, $6, $7)
        RETURNING id::text
        """,
        chat_id, user_id, text, cron_expr, next_at_utc, category, meta,
    )
    return row["id"]


async def list_by_chat(chat_id: int):
    pool = await db_pool()
    rows = await pool.fetch(
        """
        SELECT id::text, kind, text, remind_at, cron_expr, next_at, paused, category, created_at
        FROM reminders
        WHERE chat_id = $1
        ORDER BY COALESCE(next_at, remind_at) NULLS LAST, created_at
        """,
        chat_id,
    )
    return rows


async def set_paused(reminder_id: str, paused: bool) -> None:
    pool = await db_pool()
    await pool.execute(
        "UPDATE reminders SET paused=$2 WHERE id=$1",
        reminder_id, paused,
    )


async def delete_reminder(reminder_id: str) -> None:
    pool = await db_pool()
    await pool.execute(
        "DELETE FROM reminders WHERE id=$1",
        reminder_id,
    )


# Идемпотентность турнирной подписки: чистим старые слоты перед созданием новых
async def delete_tournament_crons(chat_id: int) -> None:
    pool = await db_pool()
    await pool.execute(
        "DELETE FROM reminders WHERE chat_id=$1 AND category='tournament'",
        chat_id,
    )


# =========================
# Due fetching / Delivery
# =========================
async def fetch_due(limit: int):
    pool = await db_pool()
    rows = await pool.fetch(
        """
        SELECT id::text, chat_id, user_id, kind, text, remind_at, cron_expr, next_at, paused, category
        FROM reminders
        WHERE paused = FALSE
          AND (
                (kind='once' AND remind_at IS NOT NULL AND remind_at <= NOW())
             OR (kind='cron' AND next_at   IS NOT NULL AND next_at   <= NOW())
          )
        ORDER BY COALESCE(next_at, remind_at) ASC
        LIMIT $1
        """,
        limit,
    )
    return rows


async def mark_once_delivered_success(reminder_id: str) -> None:
    pool = await db_pool()
    await pool.execute(
        "DELETE FROM reminders WHERE id=$1",
        reminder_id,
    )


async def shift_cron_next(reminder_id: str, next_at_utc) -> None:
    pool = await db_pool()
    await pool.execute(
        "UPDATE reminders SET next_at=$2 WHERE id=$1",
        reminder_id, next_at_utc,
    )


# =========================
# Tournament subscriptions
# =========================
async def set_tournament(chat_id: int, enabled: bool) -> None:
    pool = await db_pool()
    await pool.execute(
        """
        INSERT INTO tournament_subscriptions (chat_id, enabled)
        VALUES ($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET enabled=EXCLUDED.enabled
        """,
        chat_id, enabled,
    )


async def get_tournament(chat_id: int) -> bool:
    pool = await db_pool()
    row = await pool.fetchrow(
        "SELECT enabled FROM tournament_subscriptions WHERE chat_id=$1",
        chat_id,
    )
    return row["enabled"] if row else False


# =========================
# Diagnostics
# =========================
async def db_ping() -> int:
    """Простой healthcheck соединения."""
    pool = await db_pool()
    v = await pool.fetchval("SELECT 1")
    return int(v)
