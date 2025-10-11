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
    raise RuntimeError("SUPABASE_URL и SUPABASE_SERVICE_KEY должны быть заданы в переменных окружения")

# Клиент Supabase (REST)
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

# ============================================================
# ЧАТЫ И ТУРНИРНЫЕ ПОДПИСКИ (через psycopg2)
# ============================================================

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

# ============================================================
# (Опционально) СТАРАЯ СХЕМА reminders в Postgres (jsonb)
# Оставлено на случай совместимости со старым кодом
# ============================================================

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

# ============================================================
# УНИВЕРСАЛЬНЫЕ НАПОМИНАНИЯ (через Supabase REST-клиент)
# Таблица public.reminders:
#   id uuid PK, user_id bigint, chat_id bigint, text text,
#   remind_at timestamptz, paused bool,
#   kind text ('once'|'cron'), cron_expr text, next_at timestamptz,
#   created_by bigint
# ============================================================

def add_reminder(user_id: int, chat_id: int, text: str, remind_at: datetime):
    """Одноразовое напоминание (kind='once')."""
    if remind_at.tzinfo is None:
        # храним всегда в UTC
        remind_at = remind_at.replace(tzinfo=timezone.utc)
    return supabase.table("reminders").insert({
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        "remind_at": remind_at.isoformat(),
        "paused": False,
        "kind": "once",
        "created_by": user_id,
    }).execute()

def get_active_reminders(user_id: int):
    """Список активных (не на паузе) напоминаний пользователя (both once & cron)."""
    return (
        supabase.table("reminders")
        .select("*")
        .eq("user_id", user_id)
        .eq("paused", False)
        .order("remind_at", nulls_first=True)
        .order("next_at", nulls_first=True)
        .execute()
    )

def delete_reminder_by_id(reminder_id: str):
    return supabase.table("reminders").delete().eq("id", reminder_id).execute()

def set_paused(reminder_id: str, paused: bool):
    return supabase.table("reminders").update({"paused": paused}).eq("id", reminder_id).execute()

# ---------------- Повторяющиеся (CRON) ----------------

def add_recurring_reminder(user_id: int, chat_id: int, text: str, cron_expr: str):
    """Создаёт повторяющееся напоминание (kind='cron') и рассчитывает next_at от now(UTC)."""
    now = datetime.now(timezone.utc)
    next_at = croniter(cron_expr, now).get_next(datetime)
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=timezone.utc)
    return supabase.table("reminders").insert({
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        "kind": "cron",
        "cron_expr": cron_expr,
        "next_at": next_at.isoformat(),
        "paused": False,
        "created_by": user_id,
    }).execute()

def get_due_once_and_recurring(window_start_iso: str, now_iso: str):
    """Возвращает 2 списка: готовые к доставке once и cron (с учётом окна догонки)."""
    once = (
        supabase.table("reminders")
        .select("*")
        .eq("kind", "once")
        .eq("paused", False)
        .lte("remind_at", now_iso)
        .gte("remind_at", window_start_iso)
        .execute()
        .data
        or []
    )
    recur = (
        supabase.table("reminders")
        .select("*")
        .eq("kind", "cron")
        .eq("paused", False)
        .lte("next_at", now_iso)
        .gte("next_at", window_start_iso)
        .execute()
        .data
        or []
    )
    return once, recur

def advance_recurring(reminder_id: str, cron_expr: str):
    """Сдвигает next_at на следующий запуск по cron_expr от текущего времени (UTC)."""
    now = datetime.now(timezone.utc)
    nxt = croniter(cron_expr, now).get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return supabase.table("reminders").update({"next_at": nxt.isoformat()}).eq("id", reminder_id).execute()

# ============================================================
# ПОЛЬЗОВАТЕЛЬСКИЕ НАСТРОЙКИ: таймзона, «тихие часы»
# Таблица public.user_prefs(user_id PK, tz_name text, quiet_from smallint, quiet_to smallint)
# ============================================================

def set_user_tz(user_id: int, tz_name: str):
    return supabase.table("user_prefs").upsert({"user_id": user_id, "tz_name": tz_name}).execute()

def set_quiet_hours(user_id: int, quiet_from: int | None, quiet_to: int | None):
    return supabase.table("user_prefs").upsert({
        "user_id": user_id,
        "quiet_from": quiet_from,
        "quiet_to": quiet_to,
    }).execute()

def get_user_prefs(user_id: int):
    res = supabase.table("user_prefs").select("*").eq("user_id", user_id).single().execute()
    return res.data or {}

def update_remind_at(reminder_id: str, when_utc_dt):
    # when_utc_dt: datetime (UTC)
    supabase.table("reminders").update({
        "remind_at": when_utc_dt.replace(tzinfo=None).isoformat() + "Z",  # ISO с Z
        "paused": False,  # на всякий случай снимаем паузу
        "updated_at": "now()"
    }).eq("id", reminder_id).execute()


# ============================================================
# РОЛИ В ЧАТАХ (editor/viewer) — для тонкой настройки прав
# Таблица public.chat_roles(chat_id, user_id, role)
# ============================================================

def grant_role(chat_id: int, user_id: int, role: str):
    # role ∈ {'editor','viewer'}
    return supabase.table("chat_roles").upsert({
        "chat_id": chat_id,
        "user_id": user_id,
        "role": role,
    }).execute()

def revoke_role(chat_id: int, user_id: int):
    return supabase.table("chat_roles").delete().eq("chat_id", chat_id).eq("user_id", user_id).execute()

def has_editor_role(chat_id: int, user_id: int) -> bool:
    res = supabase.table("chat_roles").select("role").eq("chat_id", chat_id).eq("user_id", user_id).execute()
    return bool(res.data)

def list_roles(chat_id: int):
    res = supabase.table("chat_roles").select("*").eq("chat_id", chat_id).execute()
    return res.data or []

def get_reminder_by_id(reminder_id: str):
    """Вернёт одну запись напоминания (dict) по id или None."""
    res = supabase.table("reminders").select("*").eq("id", reminder_id).limit(1).execute()
    arr = res.data or []
    return arr[0] if arr else None

def update_reminder_text(reminder_id: str, new_text: str):
    supabase.table("reminders").update({"text": new_text, "updated_at": "now()"}).eq("id", reminder_id).execute()

def set_paused_by_id(reminder_id: str, value: bool):
    supabase.table("reminders").update({"paused": value, "updated_at": "now()"}).eq("id", reminder_id).execute()
