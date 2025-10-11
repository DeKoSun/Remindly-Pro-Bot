# FILE: db.py
import os
from contextlib import contextmanager
from datetime import datetime, timezone
import psycopg2
import psycopg2.extras
from supabase import create_client
from croniter import croniter

# ---------- Supabase (HTTP-клиент) ----------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_URL и SUPABASE_SERVICE_KEY должны быть заданы")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- Прямое подключение к Postgres (тот же кластер Supabase) ----------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()

# ============================================================
# ЧАТЫ И ПОДПИСКИ НА ТУРНИРЫ
# Поддержка новой схемы (telegram_chats / tournament_subscribers)
# и fallback на старую (chats).
# ============================================================

def _table_exists(table_name: str) -> bool:
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("""
            select 1 from information_schema.tables
            where table_schema='public' and table_name=%s
            limit 1
        """, (table_name,))
        return cur.fetchone() is not None

def upsert_chat(chat_id: int, type_: str, title: str | None):
    if _table_exists("telegram_chats"):
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """
                insert into telegram_chats(chat_id, type, title)
                values (%s, %s, %s)
                on conflict (chat_id) do update
                set title = excluded.title
                """,
                (chat_id, type_, title),
            )
            c.commit()
    else:
        # старый вариант
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """
                insert into chats(chat_id, type, title)
                values (%s, %s, %s)
                on conflict (chat_id) do update
                set title = excluded.title, updated_at = now()
                """,
                (chat_id, type_, title),
            )
            c.commit()

def set_tournament_subscription(chat_id: int, value: bool, user_id: int | None = None):
    """
    Новый вариант: public.tournament_subscribers(user_id, chat_id).
    Если таблицы нет —fallback в старую колонку chats.tournament_subscribed.
    """
    if _table_exists("tournament_subscribers"):
        if user_id is None:
            # если не передали — сохраняем заглушку для совместимости
            user_id = 0
        if value:
            supabase.table("tournament_subscribers").upsert({
                "user_id": user_id,
                "chat_id": chat_id,
            }).execute()
        else:
            supabase.table("tournament_subscribers").delete().eq("chat_id", chat_id).eq("user_id", user_id).execute()
    else:
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                "update chats set tournament_subscribed=%s, updated_at=now() where chat_id=%s",
                (value, chat_id),
            )
            c.commit()

def get_tournament_subscribed_chats():
    if _table_exists("tournament_subscribers"):
        rows = (
            supabase.table("tournament_subscribers")
            .select("chat_id")
            .execute().data or []
        )
        # возвращаем в формате [(chat_id, tz_name), ...]; tz не ведём здесь → None
        return [(r["chat_id"], None) for r in rows]
    else:
        with get_conn() as c:
            cur = c.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute("select chat_id, tz from chats where tournament_subscribed = true")
            return cur.fetchall()

# ============================================================
# УНИВЕРСАЛЬНЫЕ НАПОМИНАНИЯ (таблица public.reminders)
#   id uuid PK, user_id bigint, chat_id bigint, text text,
#   remind_at timestamptz, paused bool,
#   kind text ('once'|'cron'), cron_expr text, next_at timestamptz,
#   created_by bigint, created_at timestamptz default now()
# ============================================================

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def add_reminder(user_id: int, chat_id: int, text: str, remind_at: datetime):
    """Одноразовое напоминание (kind='once')."""
    return supabase.table("reminders").insert({
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        "remind_at": _iso(remind_at),
        "paused": False,
        "kind": "once",
        "created_by": user_id,
    }).execute()

def add_recurring_reminder(user_id: int, chat_id: int, text: str, cron_expr: str):
    """Повторяющееся (kind='cron'): рассчитываем next_at от текущего момента UTC."""
    now = datetime.now(timezone.utc)
    next_at = croniter(cron_expr, now).get_next(datetime)
    return supabase.table("reminders").insert({
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        "kind": "cron",
        "cron_expr": cron_expr,
        "next_at": _iso(next_at),
        "paused": False,
        "created_by": user_id,
    }).execute()

def get_active_reminders(user_id: int):
    return (
        supabase.table("reminders")
        .select("*")
        .eq("user_id", user_id)
        .eq("paused", False)
        .order("remind_at", nulls_first=True)
        .order("next_at", nulls_first=True)
        .execute()
    )

def get_reminder_by_id(reminder_id: str):
    res = supabase.table("reminders").select("*").eq("id", reminder_id).limit(1).execute()
    arr = res.data or []
    return arr[0] if arr else None

def delete_reminder_by_id(reminder_id: str):
    return supabase.table("reminders").delete().eq("id", reminder_id).execute()

def set_paused(reminder_id: str, paused: bool):
    return supabase.table("reminders").update({
        "paused": paused,
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).eq("id", reminder_id).execute()

def set_paused_by_id(reminder_id: str, value: bool):
    return supabase.table("reminders").update({
        "paused": value,
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).eq("id", reminder_id).execute()

def update_reminder_text(reminder_id: str, new_text: str):
    return supabase.table("reminders").update({
        "text": new_text,
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).eq("id", reminder_id).execute()

def update_remind_at(reminder_id: str, when_utc_dt: datetime):
    return supabase.table("reminders").update({
        "remind_at": _iso(when_utc_dt),
        "paused": False,
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).eq("id", reminder_id).execute()

def get_due_once_and_recurring(window_minutes: int = 10):
    """
    Возвращает (once_list, cron_list) для доставки за последние window_minutes.
    Это даёт «окно догонки», если процесс спал/рестартовал.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=window_minutes)
    now_iso = _iso(now)
    win_iso = _iso(window_start)

    once = (
        supabase.table("reminders")
        .select("*")
        .eq("kind", "once")
        .eq("paused", False)
        .lte("remind_at", now_iso)
        .gte("remind_at", win_iso)
        .execute().data or []
    )
    cron = (
        supabase.table("reminders")
        .select("*")
        .eq("kind", "cron")
        .eq("paused", False)
        .lte("next_at", now_iso)
        .gte("next_at", win_iso)
        .execute().data or []
    )
    return once, cron

def advance_recurring(reminder_id: str, cron_expr: str):
    nxt = croniter(cron_expr, datetime.now(timezone.utc)).get_next(datetime)
    return supabase.table("reminders").update({
        "next_at": _iso(nxt),
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).eq("id", reminder_id).execute()

# ============================================================
# ПОЛЬЗОВАТЕЛЬСКИЕ НАСТРОЙКИ (таймзона, «тихие часы»)
# ============================================================

def set_user_tz(user_id: int, tz_name: str):
    return supabase.table("user_prefs").upsert({
        "user_id": user_id,
        "tz_name": tz_name,
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).execute()

def set_quiet_hours(user_id: int, quiet_from: int | None, quiet_to: int | None):
    return supabase.table("user_prefs").upsert({
        "user_id": user_id,
        "quiet_from": quiet_from,
        "quiet_to": quiet_to,
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).execute()

def get_user_prefs(user_id: int):
    res = supabase.table("user_prefs").select("*").eq("user_id", user_id).single().execute()
    return res.data or {}

# ============================================================
# РОЛИ В ЧАТАХ (editor/viewer)
# ============================================================

def grant_role(chat_id: int, user_id: int, role: str):
    return supabase.table("chat_roles").upsert({
        "chat_id": chat_id,
        "user_id": user_id,
        "role": role,
        "granted_at": _iso(datetime.now(timezone.utc)),
    }).execute()

def revoke_role(chat_id: int, user_id: int):
    return supabase.table("chat_roles").delete().eq("chat_id", chat_id).eq("user_id", user_id).execute()

def has_editor_role(chat_id: int, user_id: int) -> bool:
    res = supabase.table("chat_roles").select("role").eq("chat_id", chat_id).eq("user_id", user_id).execute()
    return bool(res.data)

def list_roles(chat_id: int):
    res = supabase.table("chat_roles").select("*").eq("chat_id", chat_id).execute()
    return res.data or []
