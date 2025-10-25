# db.py
import os
import json
from typing import Optional, Any

import asyncpg


_pool: Optional[asyncpg.Pool] = None


async def db_pool() -> asyncpg.Pool:
    """
    Singleton-пул соединений к Supabase/Postgres.
    Важно: statement_cache_size=0 — безопасно для PgBouncer (transaction mode).
    """
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.getenv("DATABASE_URL"),
            min_size=1,
            max_size=5,
            command_timeout=10,                   # сек
            max_inactive_connection_lifetime=300,
            statement_cache_size=0,               # критично для PgBouncer
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
    """
    Идемпотентная регистрация чата (тип/название могут обновляться).
    """
    pool = await db_pool()
    await pool.execute(
        """
        INSERT INTO chats (chat_id, type, title)
        VALUES ($1, $2, $3)
        ON CONFLICT (chat_id)
        DO UPDATE SET
            type = EXCLUDED.type,
            title = EXCLUDED.title,
            updated_at = NOW()
        """,
        chat_id, chat_type, title,
    )


async def set_chat_timezone(chat_id: int, tz_name: str) -> None:
    """
    Сохранить дефолтную таймзону для чата (используется, если у пользователя своя не задана).
    Требуется колонка: ALTER TABLE chats ADD COLUMN IF NOT EXISTS default_timezone text;
    """
    pool = await db_pool()
    await pool.execute(
        """
        UPDATE chats
           SET default_timezone = $2,
               updated_at = NOW()
         WHERE chat_id = $1
        """,
        chat_id, tz_name,
    )


async def get_chat_timezone(chat_id: int) -> Optional[str]:
    """
    Вернуть дефолтную таймзону чата, если задана.
    """
    pool = await db_pool()
    row = await pool.fetchrow(
        "SELECT default_timezone FROM chats WHERE chat_id=$1",
        chat_id,
    )
    return row["default_timezone"] if row and row["default_timezone"] else None


# =========================
# Reminders
# =========================
async def create_once(chat_id: int, user_id: int, text: str, remind_at_utc) -> str:
    """
    Создать одноразовое напоминание (время в UTC).
    """
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
    """
    Создать повторяющееся напоминание.
    Важно:
      - next_at хранится в UTC
      - локальная TZ для сдвига хранится в meta['tz'] (jsonb), если задана.
    """
    pool = await db_pool()
    # asyncpg корректнее принимает jsonb, если явно передать JSON-строку и привести к ::jsonb в SQL
    meta_json = json.dumps(meta, ensure_ascii=False) if meta is not None else None

    row = await pool.fetchrow(
        """
        INSERT INTO reminders (chat_id, user_id, kind, text, cron_expr, next_at, paused, category, meta)
        VALUES ($1, $2, 'cron', $3, $4, $5, FALSE, $6, $7::jsonb)
        RETURNING id::text
        """,
        chat_id, user_id, text, cron_expr, next_at_utc, category, meta_json,
    )
    return row["id"]


async def list_by_chat(chat_id: int):
    """
    Список напоминаний чата для /list — без лишних полей.
    """
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
        "UPDATE reminders SET paused=$2, updated_at=NOW() WHERE id=$1",
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
    """
    Забираем наступившие напоминания.
    Возвращаем meta — планировщик использует meta['tz'] для расчёта следующего cron.
    """
    pool = await db_pool()
    rows = await pool.fetch(
        """
        SELECT id::text,
               chat_id,
               user_id,
               kind,
               text,
               remind_at,
               cron_expr,
               next_at,
               paused,
               category,
               meta
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
    """
    Удаляем одноразовое напоминание после успешной доставки.
    """
    pool = await db_pool()
    await pool.execute(
        "DELETE FROM reminders WHERE id=$1",
        reminder_id,
    )


async def shift_cron_next(reminder_id: str, next_at_utc) -> None:
    """
    Сдвигаем следующее срабатывание cron-напоминания (UTC).
    """
    pool = await db_pool()
    await pool.execute(
        "UPDATE reminders SET next_at=$2, updated_at=NOW() WHERE id=$1",
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
# Users (timezone)
# =========================
async def set_user_timezone(user_id: int, tz_name: str) -> None:
    """
    Сохранить предпочтительный часовой пояс пользователя.
    Требуется колонка `timezone text` в таблице `tg_users`.
    """
    pool = await db_pool()
    await pool.execute(
        """
        INSERT INTO tg_users (user_id, timezone)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET timezone = EXCLUDED.timezone
        """,
        user_id, tz_name,
    )


async def get_user_timezone(user_id: int) -> Optional[str]:
    pool = await db_pool()
    row = await pool.fetchrow("SELECT timezone FROM tg_users WHERE user_id=$1", user_id)
    return (row["timezone"] if row and row["timezone"] else None)


# =========================
# KV-хранилище (универсальные настройки/счётчики)
# =========================
# Таблица (один раз создать в БД):
#   create table if not exists app_settings (
#     key   text primary key,
#     value text not null
#   );

async def kv_get_str(key: str) -> Optional[str]:
    pool = await db_pool()
    row = await pool.fetchrow("SELECT value FROM app_settings WHERE key=$1", key)
    return row["value"] if row else None


async def kv_set_str(key: str, value: str) -> None:
    pool = await db_pool()
    await pool.execute(
        """
        INSERT INTO app_settings(key, value)
        VALUES ($1, $2)
        ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value
        """,
        key, value,
    )


async def kv_get_int(key: str) -> Optional[int]:
    v = await kv_get_str(key)
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


async def kv_set_int(key: str, value: int) -> None:
    await kv_set_str(key, str(value))


# =========================
# Diagnostics
# =========================
async def db_ping() -> int:
    """Простой healthcheck соединения."""
    pool = await db_pool()
    v = await pool.fetchval("SELECT 1")
    return int(v)
