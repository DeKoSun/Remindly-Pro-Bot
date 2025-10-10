# FILE: db.py
import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from supabase import create_client
from datetime import datetime

# ---------- Supabase (HTTP-клиент) ----------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_URL и SUPABASE_SERVICE_KEY должны быть заданы в переменных окружения")

# Клиент для работы через REST (таблица reminders)
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ---------- Прямое подключение к Postgres (тот же Supabase) ----------
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

# ========= ЧАТЫ И ТУРНИРНЫЕ ПОДПИСКИ (через psycopg2) =========

def upsert_chat(chat_id: int, type_: str, title: str | None):
    with get_conn() as c:
        cur = c.cursor()
        cur.execute(
            """
            INSERT INTO chats(chat_id, type, title)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET
                title = EXCLUDED.title,
                updated_at = now()
            """,
            (chat_id, type_, title),
        )
        c.commit()

def set_tournament_subscription(chat_id: int, value: bool):
    with get_conn() as c:
        cur = c.cursor()
        cur.execute(
            "UPDATE chats SET tournament_subscribed = %s, updated_at = now() WHERE chat_id = %s",
            (value, chat_id),
        )
        c.commit()

def get_tournament_subscribed_chats():
    with get_conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT chat_id, tz FROM chats WHERE tournament_subscribed = true")
        return cur.fetchall()

# ========= СТАРЫЕ ПОЛЯ ДЛЯ ВНУТРЕННИХ РЕМАЙНДЕРОВ (если нужны) =========
# Оставил, если где-то используется ваша старая схема reminders (jsonb и т.п.)

def create_reminder(owner_id: int, chat_id: int, title: str, schedule_kind: str, payload_json: dict, tz: str):
    with get_conn() as c:
        cur = c.cursor()
        cur.execute(
            """
            INSERT INTO reminders(owner_id, chat_id, title, schedule_kind, payload_json, tz)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id
            """,
            (owner_id, chat_id, title, schedule_kind, psycopg2.extras.Json(payload_json), tz),
        )
        rid = cur.fetchone()[0]
        c.commit()
        return rid

def list_reminders(chat_id: int):
    with get_conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            "SELECT id, title, schedule_kind, payload_json, is_active FROM reminders WHERE chat_id = %s ORDER BY id",
            (chat_id,),
        )
        return cur.fetchall()

def set_active(reminder_id: int, active: bool, chat_id: int):
    with get_conn() as c:
        cur = c.cursor()
        cur.execute(
            "UPDATE reminders SET is_active = %s, updated_at = now() WHERE id = %s AND chat_id = %s",
            (active, reminder_id, chat_id),
        )
        c.commit()

def delete_reminder(reminder_id: int, chat_id: int):
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM reminders WHERE id = %s AND chat_id = %s", (reminder_id, chat_id))
        c.commit()

# ========= УНИВЕРСАЛЬНЫЕ НАПОМИНАНИЯ (через Supabase REST-клиент) =========
# Эти функции используются /add, /list, /delete, /pause, /resume
# Таблица: reminders (id uuid, user_id bigint, chat_id bigint, text text, remind_at timestamptz, paused bool)

def add_reminder(user_id: int, chat_id: int, text: str, remind_at: datetime):
    return supabase.table("reminders").insert({
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        "remind_at": remind_at.isoformat(),
        "paused": False,
    }).execute()

def get_active_reminders(user_id: int):
    return supabase.table("reminders").select("*").eq("user_id", user_id).eq("paused", False).order("remind_at").execute()

def delete_reminder_by_id(reminder_id: str):
    return supabase.table("reminders").delete().eq("id", reminder_id).execute()

def set_paused(reminder_id: str, paused: bool):
    return supabase.table("reminders").update({"paused": paused}).eq("id", reminder_id).execute()
