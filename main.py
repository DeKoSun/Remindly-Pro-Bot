import os
import re
import logging
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

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
    BotCommandScopeAllGroupChats,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder
from croniter import croniter

from scheduler_core import TournamentScheduler, UniversalReminderScheduler
from texts import HELP_TEXT, MSG, E
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

def _default_props():
    try:
        return DefaultBotProperties.model_validate({"parse_mode": ParseMode.HTML})
    except AttributeError:
        return DefaultBotProperties(parse_mode=ParseMode.HTML)

bot = Bot(BOT_TOKEN, default=_default_props())
dp = Dispatcher()

_tourney = TournamentScheduler(bot)
_universal = UniversalReminderScheduler(bot, poll_interval_sec=30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await bot.session.close()

app = FastAPI(lifespan=lifespan)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("remindly")

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
    s = (raw or "").strip().lower()
    now = datetime.now(timezone.utc)

    if s.startswith("через "):
        parts = s.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return now + timedelta(minutes=int(parts[1]))

    if s.startswith("+"):
        t = s[1:].strip().replace(" мин", "").strip()
        if t.isdigit():
            return now + timedelta(minutes=int(t))

    if s.startswith("завтра"):
        rest = s.replace("завтра", "").strip()
        if ":" in rest:
            hh, mm = rest.split(":", 1)
            return (now + timedelta(days=1)).replace(
                hour=int(hh), minute=int(mm), second=0, microsecond=0
            )

    if ":" in s:
        hh, mm = s.split(":", 1)
        if hh.isdigit() and mm.isdigit():
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target

    return now + timedelta(minutes=2)

def _parse_repeat_to_cron(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s.startswith("cron:"):
        return s.split("cron:", 1)[1].strip()

    if s == "каждую минуту":
        return "* * * * *"

    m = re.match(r"^кажд(ый|ые)\s+(\d+)\s*(минут(у|ы)?|мин)\b", s)
    if m:
        n = max(1, min(59, int(m.group(2))))
        return f"*/{n} * * * *"

    if s.startswith("ежедневно"):
        rest = s.replace("ежедневно", "").strip()
        if ":" in rest:
            hh, mm = rest.split(":", 1)
            return f"{int(mm)} {int(hh)} * * *"

    if ":" in s:
        hh, mm = s.split(":", 1)
        return f"{int(mm)} {int(hh)} * * *"

    return "* * * * *"

def _cron_next_utc(expr: str) -> datetime:
    return croniter(expr, datetime.now(timezone.utc)).get_next(datetime)

def _fmt_utc(dt: datetime) -> str:
    if not isinstance(dt, datetime):
        try:
            dt = datetime.fromisoformat(str(dt))
        except Exception:
            return str(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M (UTC)")

def _build_reminders_list_text(rows: list[dict]) -> str:
    if not rows:
        return MSG["list_empty"]
    lines = [MSG["list_header"]]
    for idx, r in enumerate(rows, start=1):
        text = r.get("text", "—")
        kind = r.get("kind") or "once"
        paused = bool(r.get("paused"))
        remind_at = r.get("remind_at")
        next_at = r.get("next_at")
        when = remind_at if kind == "once" else next_at
        status = E["pause"] if paused else ("🔁" if kind != "once" else "•")
        lines.append(f"{idx}. {status} <b>{text}</b> — {_fmt_utc(when) if when else '—'}")
    return "\n".join(lines)

def _build_reminders_keyboard(rows: list[dict]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for r in rows:
        rid = r["id"]
        paused = bool(r.get("paused"))
        if paused:
            kb.button(text=f"{E['play']} Возобновить", callback_data=f"rem:resume:{rid}")
        else:
            kb.button(text=f"{E['pause']} Пауза", callback_data=f"rem:pause:{rid}")
        kb.button(text=f"{E['trash']} Удалить", callback_data=f"rem:delete:{rid}")
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
    await m.answer("👋 Привет! Напиши /help, чтобы увидеть мои команды.")

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
        await m.answer(f"🏓 pong — ✅ db=ok | 🗓 sched={sched}")
    except Exception as e:
        await m.answer(str(MSG["pong_db_err"](e)))

# -------- /add (одноразовое) --------
class AddOnceSG(StatesGroup):
    text = State()
    when = State()

@dp.message(Command("add"))
async def add_once_start(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.set_state(AddOnceSG.text)
    await m.answer(str(MSG["enter_text"]))

@dp.message(AddOnceSG.text, F.text)
async def add_once_text(m: types.Message, state: FSMContext):
    await state.update_data(text=m.text.strip())
    await state.set_state(AddOnceSG.when)
    await m.answer(str(MSG["when_once"]))

@dp.message(AddOnceSG.when, F.text)
async def add_once_finish(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    data = await state.get_data()
    text = data.get("text", "").strip()
    when = _parse_when_once(m.text)
    try:
        row = add_reminder(
            user_id=m.from_user.id,
            chat_id=m.chat.id,
            text=text,
            remind_at=when,
        )
        out = str(MSG["created_once"](text, _fmt_utc(row.get("remind_at", when))))
        await m.answer(out)
    except Exception as e:
        await m.answer(str(MSG["create_fail"](e)))
    finally:
        await state.clear()

# -------- /add_repeat (повторяющееся) --------
class AddRepeatSG(StatesGroup):
    text = State()
    sched = State()

@dp.message(Command("add_repeat"))
async def add_repeat_start(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.set_state(AddRepeatSG.text)
    await m.answer(str(MSG["enter_text_repeat"]))

@dp.message(AddRepeatSG.text, F.text)
async def add_repeat_text(m: types.Message, state: FSMContext):
    await state.update_data(text=m.text.strip())
    await state.set_state(AddRepeatSG.sched)
    await m.answer(str(MSG["when_repeat"]))

@dp.message(AddRepeatSG.sched, F.text)
async def add_repeat_finish(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    data = await state.get_data()
    text = data.get("text", "").strip()
    expr = _parse_repeat_to_cron(m.text)
    try:
        next_at = _cron_next_utc(expr)
        row = add_recurring_reminder(
            user_id=m.from_user.id,
            chat_id=m.chat.id,
            text=text,
            cron_expr=expr,
        )
        out = str(MSG["created_cron"](text, expr, _fmt_utc(row.get("next_at") or next_at)))
        await m.answer(out)
    except Exception as e:
        await m.answer(str(MSG["create_cron_fail"]) + f"\n<code>{e}</code>")
    finally:
        await state.clear()

# -------- /list (список + кнопки) --------
@dp.message(Command("list"))
async def cmd_list(m: types.Message):
    await _ensure_user_chat(m)
    rows = get_active_reminders_for_chat(m.chat.id, include_paused=True)
    text = _build_reminders_list_text(rows)
    kb = _build_reminders_keyboard(rows)
    await m.answer(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)

# -------- Колбэки: пауза / возобновить / удалить --------
@dp.callback_query(F.data.startswith("rem:"))
async def cb_reminders(cq: CallbackQuery):
    try:
        _, action, rid = cq.data.split(":", 2)
    except Exception:
        return await cq.answer(str(MSG["bad_data"]), show_alert=True)

    try:
        if action == "pause":
            set_paused(rid, True)
            await cq.answer(str(MSG["paused"]))
        elif action == "resume":
            set_paused(rid, False)
            await cq.answer(str(MSG["resumed"]))
        elif action == "delete":
            delete_reminder_by_id(rid)
            await cq.answer(str(MSG["deleted"]))
        else:
            return await cq.answer(str(MSG["bad_action"]), show_alert=True)

        if cq.message:
            await _refresh_list_message(cq.message.chat.id, cq.message)
    except Exception as e:
        await cq.answer(f"Ошибка: {e}", show_alert=True)

# ---------- Webhook ----------
@app.post(f"/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    try:
        update = Update.model_validate(data)        # Pydantic v2
    except AttributeError:
        try:
            update = Update.parse_obj(data)         # Pydantic v1
        except AttributeError:
            update = Update(**data)                 # fallback
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
            BotCommand("help", "Справка по командам ℹ️"),
            BotCommand("add", "Создать разовое 📝"),
            BotCommand("add_repeat", "Создать повторяющееся 🔁"),
            BotCommand("list", "Список с кнопками 📋"),
            BotCommand("ping", "Проверка состояния 🏓"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand("help", "Справка по командам ℹ️"),
            BotCommand("add", "Создать разовое 📝"),
            BotCommand("add_repeat", "Создать повторяющееся 🔁"),
            BotCommand("list", "Список с кнопками 📋"),
            BotCommand("ping", "Проверка состояния 🏓"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )

@app.get("/")
async def root():
    return {"status": "up"}

@app.get("/health")
async def health():
    return {"ok": True}
