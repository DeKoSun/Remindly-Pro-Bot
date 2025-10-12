# main.py
import os
import re
import logging
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Update,
    BotCommand,
    CallbackQuery,
    BotCommandScopeAllPrivateChats,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder
from croniter import croniter

# ==== локальные модули ====
from scheduler_core import TournamentScheduler, UniversalReminderScheduler
from texts import HELP_TEXT
from db import (
    upsert_chat,
    upsert_telegram_user,
    get_active_reminders,
    get_active_reminders_for_chat,
    add_reminder,
    add_recurring_reminder,
    delete_reminder_by_id,
    set_paused,
)

# ================== Конфигурация ==================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "webhook")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

if not BOT_TOKEN or not PUBLIC_BASE_URL:
    raise RuntimeError("TELEGRAM_BOT_TOKEN and PUBLIC_BASE_URL must be set")

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("remindly")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_tourney = TournamentScheduler(bot)
_universal = UniversalReminderScheduler(bot)

# ================== FSM ==================
class AddOnceSG(StatesGroup):
    text = State()
    when = State()

class AddRepeatSG(StatesGroup):
    text = State()
    sched = State()

# =============== Утилиты =================
async def _ensure_user_chat(m: types.Message) -> None:
    try:
        if m.from_user:
            upsert_telegram_user(m.from_user.id)
        upsert_chat(chat_id=m.chat.id, type_=m.chat.type, title=getattr(m.chat, "title", None))
    except Exception as e:
        logger.exception("ensure_user_chat failed: %s", e)

def _parse_when_once(raw: str) -> datetime:
    """Примеры: '14:30', 'завтра 10:00', 'через 25 минут', '+15'."""
    s = (raw or "").strip().lower()
    s = (
        s.replace("минуту", "1 минуту")
         .replace("мин.", "мин")
         .replace("минута", "1 минута")
         .replace(" минут", " мин")
    )
    now = datetime.now(timezone.utc)

    # через N минут
    if s.startswith("через "):
        parts = s.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return now + timedelta(minutes=int(parts[1]))

    # +N (минут)
    if s.startswith("+"):
        t = s[1:].strip().replace(" мин", "").strip()
        if t.isdigit():
            return now + timedelta(minutes=int(t))

    # завтра HH:MM
    if s.startswith("завтра"):
        rest = s.replace("завтра", "").strip()
        if ":" in rest:
            hh, mm = rest.split(":")[:2]
            return (now + timedelta(days=1)).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)

    # HH:MM (сегодня/завтра)
    if ":" in s:
        hh, mm = s.split(":")[:2]
        if hh.isdigit() and mm.isdigit():
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target

    # дефолт: через 2 минуты
    return now + timedelta(minutes=2)

def _parse_repeat_to_cron(raw: str) -> str:
    """
    'каждую минуту'      -> * * * * *
    'каждые 2 минуты'    -> */2 * * * *
    'ежедневно 14:30'    -> 30 14 * * *
    'HH:MM'               -> 30 14 * * *
    'cron: */5 * * * *'   -> */5 * * * *
    """
    s = (raw or "").strip().lower()

    # Явный cron
    if s.startswith("cron:"):
        return s.split("cron:", 1)[1].strip()

    # Каждую минуту
    if s == "каждую минуту":
        return "* * * * *"

    # Каждые N минут / минуты / минуту / мин
    m = re.match(r"^кажд(ый|ые)\s+(\d+)\s*(минут(у|ы)?|мин)\b", s)
    if m:
        n = int(m.group(2))
        n = max(1, min(59, n))
        return f"*/{n} * * * *"

    # Ежедневно HH:MM
    if s.startswith("ежедневно"):
        rest = s.replace("ежедневно", "").strip()
        if ":" in rest:
            hh, mm = rest.split(":")[:2]
            if hh.isdigit() and mm.isdigit():
                return f"{int(mm)} {int(hh)} * * *"

    # Просто HH:MM
    if ":" in s:
        hh, mm = s.split(":")[:2]
        if hh.isdigit() and mm.isdigit():
            return f"{int(mm)} {int(hh)} * * *"

    # Бекап: каждую минуту
    return "* * * * *"

def _cron_next_utc(expr: str) -> datetime:
    now = datetime.now(timezone.utc)
    return croniter(expr, now).get_next(datetime)

def _fmt_utc(dt: datetime) -> str:
    if not isinstance(dt, datetime):
        try:
            dt = datetime.fromisoformat(str(dt))
        except Exception:
            return str(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M (UTC)")

# ========= Вспомогательные для /list =========
def _build_reminders_list_text(rows: list[dict]) -> str:
    if not rows:
        return "Пока нет напоминаний."
    lines = ["<b>Напоминания этого чата:</b>"]
    for idx, r in enumerate(rows, start=1):
        text = r.get("text", "—")
        kind = r.get("kind") or "once"
        paused = bool(r.get("paused"))
        remind_at = r.get("remind_at")
        next_at = r.get("next_at")
        when = remind_at if kind == "once" else next_at
        status = "⏸" if paused else ("🔁" if kind != "once" else "•")
        lines.append(f"{idx}. {status} <b>{text}</b> — {_fmt_utc(when) if when else '—'}")
    return "\n".join(lines)

def _build_reminders_keyboard(rows: list[dict]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for r in rows:
        rid = r["id"]
        paused = bool(r.get("paused"))
        if paused:
            kb.button(text="▶️ Возобновить", callback_data=f"rem:resume:{rid}")
        else:
            kb.button(text="⏸ Пауза", callback_data=f"rem:pause:{rid}")
        kb.button(text="🗑 Удалить", callback_data=f"rem:delete:{rid}")
        kb.adjust(2)
    return kb

async def _refresh_list_message(chat_id: int, message: types.Message):
    rows = get_active_reminders_for_chat(chat_id, include_paused=True)
    text = _build_reminders_list_text(rows)
    kb = _build_reminders_keyboard(rows)
    try:
        await message.edit_text(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)
    except Exception:
        await message.answer(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)

# ================== ХЕНДЛЕРЫ ==================
@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    await _ensure_user_chat(m)
    await m.answer("Привет! Напиши /help, чтобы увидеть мои команды.")

@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await _ensure_user_chat(m)
    await m.answer(HELP_TEXT, disable_web_page_preview=True)

@dp.message(Command("ping"))
async def cmd_ping(m: types.Message):
    await _ensure_user_chat(m)
    try:
        _ = get_active_reminders(m.from_user.id)
        sched = "ok" if (_tourney and _universal) else "no"
        await m.answer(f"pong ✅ | db=ok | sched={sched}")
    except Exception as e:
        await m.answer(f"pong ❌ | db error: <code>{e}</code>")

# ---------- Добавление одноразовых ----------
@dp.message(Command("add"))
async def add_once_start(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.set_state(AddOnceSG.text)
    await m.answer("📝 Введи текст напоминания:")

@dp.message(AddOnceSG.text)
async def add_once_wait_when(m: types.Message, state: FSMContext):
    await state.update_data(text=m.text.strip())
    await state.set_state(AddOnceSG.when)
    await m.answer("⏰ Когда напомнить?\nПримеры: 14:30 · завтра 10:00 · через 25 минут · +15")

@dp.message(AddOnceSG.when)
async def add_once_finish(m: types.Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("text", "").strip()
    when_raw = (m.text or "").strip()
    if not text:
        await state.clear()
        return await m.answer("❌ Текст пустой.")
    try:
        remind_at_utc = _parse_when_once(when_raw)
        add_reminder(m.from_user.id, m.chat.id, text, remind_at_utc)
        await state.clear()
        await m.answer(f"✅ Напоминание создано:\n<b>{text}</b>\n🕒 {_fmt_utc(remind_at_utc)}")
    except Exception as e:
        logger.exception("add_once_finish: %s", e)
        await m.answer("❌ Ошибка при создании напоминания.")

# ---------- Повторяющиеся ----------
@dp.message(Command("add_repeat"))
async def add_repeat_start(m: types.Message, state: FSMContext):
    await state.set_state(AddRepeatSG.text)
    await m.answer("📝 Введи текст повторяющегося напоминания:")

@dp.message(AddRepeatSG.text)
async def add_repeat_wait_sched(m: types.Message, state: FSMContext):
    await state.update_data(text=m.text.strip())
    await state.set_state(AddRepeatSG.sched)
    await m.answer(
        "⏰ Какое расписание?\n"
        "• <i>каждую минуту</i>\n"
        "• <i>каждые N минут</i>\n"
        "• <i>ежедневно HH:MM</i>\n"
        "• <i>HH:MM</i>\n"
        "• <i>cron: */5 * * * *</i>"
    )

@dp.message(AddRepeatSG.sched)
async def add_repeat_finish(m: types.Message, state: FSMContext):
    data = await state.get_data()
    text = (data.get("text") or "").strip()
    sched_raw = (m.text or "").strip()
    if not text:
        await state.clear()
        return await m.answer("❌ Текст пустой.")
    cron_expr = _parse_repeat_to_cron(sched_raw)
    try:
        if not croniter.is_valid(cron_expr):
            raise ValueError
        next_at = _cron_next_utc(cron_expr)
    except Exception:
        return await m.answer(f"❌ Неверное расписание.\nexpr: <code>{cron_expr}</code>")
    add_recurring_reminder(m.from_user.id, m.chat.id, text, cron_expr, next_at)
    await state.clear()
    await m.answer(
        "✅ Повторяющееся напоминание создано:\n"
        f"<b>{text}</b>\n🔁 CRON: <code>{cron_expr}</code>\n🕒 Ближайшее: {_fmt_utc(next_at)}"
    )

# ---------- Список ----------
@dp.message(Command("list"))
async def cmd_list(m: types.Message):
    rows = get_active_reminders_for_chat(m.chat.id, include_paused=True)
    if not rows:
        return await m.answer("Пока нет напоминаний.")
    text = _build_reminders_list_text(rows)
    kb = _build_reminders_keyboard(rows)
    await m.answer(text, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("rem:"))
async def on_reminder_action(cb: CallbackQuery):
    try:
        _, action, rid = cb.data.split(":", 2)
    except ValueError:
        return await cb.answer("Некорректные данные.", show_alert=True)
    try:
        if action == "pause":
            set_paused(rid, True)
            await cb.answer("⏸ Пауза")
        elif action == "resume":
            set_paused(rid, False)
            await cb.answer("▶ Возобновлено")
        elif action == "delete":
            delete_reminder_by_id(rid)
            await cb.answer("🗑 Удалено")
    except Exception as e:
        logger.exception("callback action failed: %s", e)
        await cb.answer("Ошибка выполнения.")
    if cb.message:
        await _refresh_list_message(cb.message.chat.id, cb.message)

# ---------- Webhook ----------
@app.post(f"/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()

    # Совместимость Pydantic v1/v2
    try:
        # v2
        update = Update.model_validate(data)  # type: ignore[attr-defined]
    except AttributeError:
        # v1
        update = Update(**data)

    await dp.feed_update(bot, update)
    return {"ok": True}
    
@app.on_event("startup")
async def on_startup():
    if _tourney:
        _tourney.start()
    if _universal:
        _universal.start()
    await bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}/{WEBHOOK_SECRET}",
        drop_pending_updates=True,
    )
    await bot.set_my_commands(
        [
            BotCommand("help", "Показать команды"),
            BotCommand("add", "Создать разовое напоминание"),
            BotCommand("add_repeat", "Создать повторяющееся"),
            BotCommand("list", "Список с кнопками"),
            BotCommand("ping", "Проверка состояния"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )

@app.get("/")
async def root():
    return {"status": "up"}

@app.get("/health")
async def health():
    return {"ok": True}
