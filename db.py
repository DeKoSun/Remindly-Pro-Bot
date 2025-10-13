# db.py
import os
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Union, List, Any, Dict
import re

import psycopg2
import psycopg2.extras
from supabase import create_client
from croniter import croniter, CroniterBadCronError

# ========= ENV / CLIENTS =========

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_URL и SUPABASE_SERVICE_KEY должны быть заданы")

# HTTP-клиент (сервисный ключ обходит RLS)
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Прямое подключение к Postgres
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


# ========= HELPERS =========

def _iso(dt: datetime) -> str:
    """UTC ISO8601 для timestamptz."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _table_exists(name: str) -> bool:
    with get_conn() as c:
        cur = c.cursor()
        cur.execute(
            """
            select 1
              from information_schema.tables
             where table_schema = 'public' and table_name = %s
             limit 1
            """,
            (name,),
        )
        return cur.fetchone() is not None


# ========= CRON нормализация/валидация =========

_every_min_re = re.compile(r"^каждые?\s+(\d{1,2})\s*мин(ут|уты|ута|)$", re.IGNORECASE)
_every_hour_re = re.compile(r"^каждые?\s+(\d{1,2})\s*час(ов|а|)$", re.IGNORECASE)

def normalize_cron(expr: str) -> str:
    """
    Поддержка «человечных» шаблонов:
      - 'каждую минуту'              -> '* * * * *'
      - 'каждые N минут(ы/у/)'       -> '*/N * * * *'     (1..59)
      - 'каждые N час(ов/а/)'        -> '0 */N * * *'     (1..23)
      - 'ежедневно HH:MM'            -> 'MM HH * * *'
      - 'HH:MM'                       -> 'MM HH * * *'
      - иначе — вернуть как есть.
    """
    s = (expr or "").strip().lower()

    if s == "каждую минуту":
        return "* * * * *"

    m = _every_min_re.match(s)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 59:
            return f"*/{n} * * * *"

    m = _every_hour_re.match(s)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 23:
            return f"0 */{n} * * *"

    if s.startswith("ежедневно"):
        s = s.replace("ежедневно", "", 1).strip()

    if ":" in s:
        try:
            hh, mm = s.split(":", 1)
            hh = int(hh)
            mm = int(mm)
            if 0 <= hh < 24 and 0 <= mm < 60:
                return f"{mm} {hh} * * *"
        except Exception:
            pass

    return expr


def validate_cron(expr: str) -> bool:
    """Быстрая валидация cron-строки."""
    try:
        croniter(expr, datetime.now(timezone.utc)).get_next(datetime)
        return True
    except (CroniterBadCronError, ValueError):
        return False


# ========= Родительские строки для FK / RLS =========

def upsert_telegram_user(user_id: int):
    if not _table_exists("telegram_users"):
        return
    supabase.table("telegram_users").upsert({
        "user_id": user_id,
        "created_at": _iso(datetime.now(timezone.utc)),
    }).execute()


def upsert_telegram_chat(chat_id: int):
    if not _table_exists("telegram_chats"):
        return
    supabase.table("telegram_chats").upsert({
        "chat_id": chat_id,
        "created_at": _iso(datetime.now(timezone.utc)),
    }).execute()


def ensure_parent_rows(user_id: Optional[int], chat_id: Optional[int]):
    if user_id is not None:
        upsert_telegram_user(user_id)
    if chat_id is not None:
        upsert_telegram_chat(chat_id)


# ========= Турнирные подписки / чаты (опционально) =========

def upsert_chat(chat_id: int, type_: str, title: Optional[str]):
    upsert_telegram_chat(chat_id)
    if _table_exists("chats"):
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


def set_tournament_subscription(chat_id: int, value: bool, user_id: Optional[int] = None):
    if user_id is not None:
        upsert_telegram_user(user_id)
    upsert_telegram_chat(chat_id)

    if _table_exists("tournament_subscribers"):
        # если нет user_id — складываем как 0 (групповая подписка)
        if user_id is None:
            user_id = 0
        if value:
            supabase.table("tournament_subscribers").upsert({
                "user_id": user_id,
                "chat_id": chat_id,
                "subscribed_at": _iso(datetime.now(timezone.utc)),
            }).execute()
        else:
            supabase.table("tournament_subscribers").delete() \
                .eq("chat_id", chat_id).eq("user_id", user_id).execute()
    elif _table_exists("chats"):
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                "update chats set tournament_subscribed = %s, updated_at = now() where chat_id = %s",
                (value, chat_id),
            )
            c.commit()


def get_tournament_subscribed_chats() -> List[Tuple[int, Optional[str]]]:
    if _table_exists("tournament_subscribers"):
        rows = supabase.table("tournament_subscribers").select("chat_id").execute().data or []
        return [(r["chat_id"], None) for r in rows]
    elif _table_exists("chats"):
        with get_conn() as c:
            cur = c.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute("select chat_id, tz from chats where tournament_subscribed = true")
            return cur.fetchall()
    else:
        return []


# ========= Напоминания =========
# public.reminders:
#   id uuid PK, user_id bigint, chat_id bigint,
#   text text, kind text ('once'|'cron'),
#   remind_at timestamptz, cron_expr text, next_at timestamptz,
#   paused bool, created_by bigint, created_at timestamptz, updated_at timestamptz

def add_reminder(
    user_id: int,
    chat_id: int,
    text: str,
    remind_at: datetime,
    *,
    created_by: Optional[int] = None,
) -> Dict[str, Any]:
    """Одноразовое напоминание."""
    ensure_parent_rows(user_id, chat_id)
    creator = created_by or user_id
    res = supabase.table("reminders").insert({
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        "kind": "once",
        "remind_at": _iso(remind_at),
        "paused": False,
        "created_by": creator,
        "created_at": _iso(datetime.now(timezone.utc)),
    }).execute()
    return (res.data or [{}])[0]


def add_recurring_reminder(
    user_id: int,
    chat_id: int,
    text: str,
    cron_expr: str,
    *,
    next_at: Optional[datetime] = None,
    created_by: Optional[int] = None,
) -> Dict[str, Any]:
    """Повторяющееся напоминание (cron)."""
    ensure_parent_rows(user_id, chat_id)

    cron_expr = normalize_cron(cron_expr)
    # валидация: если невалидный cron — не записываем, чтобы не «ломать» шедулер
    try:
        base = datetime.now(timezone.utc)
        if next_at is None:
            next_at = croniter(cron_expr, base).get_next(datetime)
        else:
            # если next_at задан вручную — всё равно убедимся, что cron валиден
            croniter(cron_expr, base).get_next(datetime)
    except (CroniterBadCronError, ValueError):
        raise ValueError("Bad cron expression")

    creator = created_by or user_id
    res = supabase.table("reminders").insert({
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        "kind": "cron",
        "cron_expr": cron_expr,
        "next_at": _iso(next_at),
        "paused": False,
        "created_by": creator,
        "created_at": _iso(datetime.now(timezone.utc)),
    }).execute()
    return (res.data or [{}])[0]


def get_active_reminders(user_id: int):
    """Активные напоминания пользователя (для DM)."""
    return (
        supabase.table("reminders")
        .select("*")
        .eq("user_id", user_id)
        .order("remind_at", nullsfirst=True)
        .order("next_at", nullsfirst=True)
        .execute()
    )


def get_active_reminders_for_chat(chat_id: int, *, include_paused: bool = True) -> List[dict]:
    """
    Список напоминаний для чата:
      1) по remind_at (сначала те, у кого есть дата),
      2) затем по next_at,
      3) затем по created_at.
    """
    q = (
        supabase.table("reminders")
        .select("*")
        .eq("chat_id", chat_id)
        .order("remind_at", nullsfirst=True)
        .order("next_at", nullsfirst=True)
        .order("created_at")
    )
    if not include_paused:
        q = q.eq("paused", False)
    res = q.execute()
    return res.data or []


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


def get_due_once_and_recurring(window_minutes: int = 10) -> Tuple[List[dict], List[dict]]:
    """
    Возвращает два списка для обработчика планировщика:
      - once: напоминания, у которых remind_at ∈ [now - window; now]
      - cron:   напоминания, у которых next_at   ∈ [now - window; now]
    Это помогает «догонять» события после рестарта.
    """
    now = datetime.now(timezone.utc)
    win = now - timedelta(minutes=window_minutes)
    now_iso = _iso(now)
    win_iso = _iso(win)

    once = (
        supabase.table("reminders")
        .select("*")
        .eq("kind", "once")
        .eq("paused", False)
        .lte("remind_at", now_iso)
        .gte("remind_at", win_iso)
        .execute()
        .data
        or []
    )

    cron_list = (
        supabase.table("reminders")
        .select("*")
        .eq("kind", "cron")
        .eq("paused", False)
        .lte("next_at", now_iso)
        .gte("next_at", win_iso)
        .execute()
        .data
        or []
    )
    return once, cron_list


def advance_recurring(reminder_id: str, cron_expr: str):
    """Продвинуть cron вперёд на один шаг от «сейчас»."""
    nxt = croniter(cron_expr, datetime.now(timezone.utc)).get_next(datetime)
    return supabase.table("reminders").update({
        "next_at": _iso(nxt),
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).eq("id", reminder_id).execute()


# ========= Пользовательские настройки =========

def set_user_tz(user_id: int, tz_name: str):
    upsert_telegram_user(user_id)
    return supabase.table("user_prefs").upsert({
        "user_id": user_id,
        "tz_name": tz_name,
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).execute()


def set_quiet_hours(user_id: int, quiet_from: Optional[int], quiet_to: Optional[int]):
    upsert_telegram_user(user_id)
    return supabase.table("user_prefs").upsert({
        "user_id": user_id,
        "quiet_from": quiet_from,
        "quiet_to": quiet_to,
        "updated_at": _iso(datetime.now(timezone.utc)),
    }).execute()


def get_user_prefs(user_id: int):
    res = supabase.table("user_prefs").select("*").eq("user_id", user_id).single().execute()
    return res.data or {}


# ========= Роли в чатах =========

def grant_role(chat_id: int, user_id: int, role: str):
    ensure_parent_rows(user_id, chat_id)
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


# ========= DEBUG =========

def dbg_insert_once(user_id: int, chat_id: int, minutes: int = 1, text: Optional[str] = None):
    """Быстрый helper для тестов: one-off через N минут."""
    ensure_parent_rows(user_id, chat_id)
    when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    if not text:
        text = f"DEBUG: напоминание через {minutes} мин"
    res = supabase.table("reminders").insert({
        "user_id": user_id,
        "chat_id": chat_id,
        "text": text,
        "kind": "once",
        "remind_at": _iso(when),
        "paused": False,
        "created_by": user_id,
        "created_at": _iso(datetime.now(timezone.utc)),
    }).execute()
    return (res.data or [{}])[0]


# ========= HIGH-LEVEL «по номеру или по UUID» =========

Selector = Union[str, int]

def resolve_selector_to_id(chat_id: int, selector: Selector, user_id: Optional[int] = None) -> Optional[str]:
    """
    Возвращает UUID напоминания по:
      - UUID (строка вида 'xxxx-...') — просто валидируем наличие;
      - порядковому номеру (1..N) в списке напоминаний чата (сначала ближайшие).
    Если user_id указан — дополнительно фильтруем по автору (created_by).
    """
    # UUID?
    if isinstance(selector, str) and "-" in selector:
        row = get_reminder_by_id(selector)
        if row and row.get("chat_id") == chat_id and (user_id is None or row.get("created_by") == user_id):
            return row["id"]
        return None

    # Индекс?
    try:
        idx = int(selector)
    except Exception:
        return None
    if idx <= 0:
        return None

    rows = get_active_reminders_for_chat(chat_id, include_paused=True)
    if user_id is not None:
        rows = [r for r in rows if r.get("created_by") == user_id]
    if 1 <= idx <= len(rows):
        return rows[idx - 1]["id"]
    return None


def pause_or_resume(chat_id: int, selector: Selector, value: bool, user_id: Optional[int] = None):
    rid = resolve_selector_to_id(chat_id, selector, user_id=user_id)
    if not rid:
        return None
    return set_paused_by_id(rid, value)


def delete_by_selector(chat_id: int, selector: Selector, user_id: Optional[int] = None):
    rid = resolve_selector_to_id(chat_id, selector, user_id=user_id)
    if not rid:
        return None
    return delete_reminder_by_id(rid)


__all__ = [
    # parents
    "upsert_telegram_user", "upsert_telegram_chat", "ensure_parent_rows",
    # tournaments / chats
    "upsert_chat", "set_tournament_subscription", "get_tournament_subscribed_chats",
    # reminders
    "add_reminder", "add_recurring_reminder", "get_active_reminders", "get_active_reminders_for_chat",
    "get_reminder_by_id", "delete_reminder_by_id", "set_paused", "set_paused_by_id",
    "update_reminder_text", "update_remind_at", "get_due_once_and_recurring", "advance_recurring",
    # prefs
    "set_user_tz", "set_quiet_hours", "get_user_prefs",
    # roles
    "grant_role", "revoke_role", "has_editor_role", "list_roles",
    # debug
    "dbg_insert_once",
    # cron helpers
    "normalize_cron", "validate_cron",
    # selectors
    "resolve_selector_to_id", "pause_or_resume", "delete_by_selector",
    # shared
    "supabase", "get_conn",
]
