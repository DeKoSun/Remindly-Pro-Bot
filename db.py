import asyncpg
import uuid
from datetime import datetime, timezone
from typing import List, Tuple, Optional, Dict

_pool: asyncpg.Pool | None = None

class Db:
    @staticmethod
    async def init_pool(dsn: str):
        global _pool
        _pool = await asyncpg.create_pool(dsn, max_size=10)

    # ---------- create ----------

    @staticmethod
    async def add_once(user_id: int, chat_id: int, text: str, remind_at: datetime, created_by: Optional[int] = None) -> str:
        q = """
        insert into reminders(id, user_id, chat_id, text, kind, remind_at, paused, created_at, created_by)
        values($1, $2, $3, $4, 'once', $5, false, now() at time zone 'utc', $6)
        returning id;
        """
        rid = str(uuid.uuid4())
        async with _pool.acquire() as con:
            await con.fetchval(q, rid, user_id, chat_id, text, remind_at.astimezone(timezone.utc), created_by)
        return rid

    @staticmethod
    async def add_cron(user_id: int, chat_id: int, text: str, cron_expr: str, next_at: datetime,
                       created_by: Optional[int] = None) -> str:
        q = """
        insert into reminders(id, user_id, chat_id, text, kind, cron_expr, next_at, paused, created_at, created_by)
        values($1, $2, $3, $4, 'cron', $5, $6, false, now() at time zone 'utc', $7)
        returning id;
        """
        rid = str(uuid.uuid4())
        async with _pool.acquire() as con:
            await con.fetchval(q, rid, user_id, chat_id, text, cron_expr, next_at.astimezone(timezone.utc), created_by)
        return rid

    # ---------- list ----------

    @staticmethod
    async def list_by_chat(chat_id: int) -> List[Dict]:
        q = """
        select id, user_id, chat_id, text, kind, remind_at, cron_expr, next_at, paused, created_at, created_by, updated_at
        from reminders
        where chat_id=$1
        order by coalesce(next_at, remind_at, created_at) asc
        """
        async with _pool.acquire() as con:
            rows = await con.fetch(q, chat_id)
        return [dict(r) for r in rows]

    # ---------- due scan ----------

    @staticmethod
    async def get_due(window_seconds: int = 30) -> List[Dict]:
        q = """
        select *
        from reminders
        where not paused
          and coalesce(next_at, remind_at) <= (now() at time zone 'utc')
        order by coalesce(next_at, remind_at) asc
        """
        async with _pool.acquire() as con:
            rows = await con.fetch(q)
        return [dict(r) for r in rows]

    # ---------- actions ----------

    @staticmethod
    async def complete_once(rem_id: str):
        q = "delete from reminders where id=$1 and kind='once';"
        async with _pool.acquire() as con:
            await con.execute(q, rem_id)

    @staticmethod
    async def set_next(rem_id: str, next_at: datetime):
        q = """
        update reminders
           set next_at=$2,
               updated_at=now() at time zone 'utc'
         where id=$1 and kind='cron';
        """
        async with _pool.acquire() as con:
            await con.execute(q, rem_id, next_at.astimezone(timezone.utc))

    @staticmethod
    async def toggle_pause(rem_id: str) -> Tuple[bool, bool]:
        q = """
        update reminders
           set paused = not paused,
               updated_at=now() at time zone 'utc'
         where id=$1
        returning paused;
        """
        async with _pool.acquire() as con:
            row = await con.fetchrow(q, rem_id)
        return (row is not None, bool(row["paused"]) if row else False)

    @staticmethod
    async def delete(rem_id: str):
        q = "delete from reminders where id=$1;"
        async with _pool.acquire() as con:
            await con.execute(q, rem_id)
