# main.py
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
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder
from croniter import croniter

# ===== локальные модули =====
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

# ---------- безопасное создание бота ----------
def _default_props():
    """
    Aiogram 3 использует Pydantic. В v2 есть model_validate, в v1 — обычный конструктор.
    Хелпер делает создание совместимым и убирает BaseModel.__init__() ошибки.
    """
    try:
        # Pydantic v2
        return DefaultBotProperties.model_validate({"parse_mode": ParseMode.HTML})
    except AttributeError:
        # Pydantic v1
        return DefaultBotProperties(parse_mode=ParseMode.HTML)

bot = Bot(BOT_TOKEN, default=_default_props())
dp = Dispatcher()

_tourney = TournamentScheduler(bot)
_universal = UniversalReminderScheduler(bot)

# ---------- корректное закрытие HTTP-сессии ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        # важно закрывать HTTP-сессию, иначе остаются «Unclosed client session/connector»
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
    """
    Поддерживаем: «через N», «+N», «HH:MM», «завтра HH:MM».
    Возвращаем UTC datetime (по умолчанию сейчас + 2 мин).
    """
    s = (raw or "").strip().lower()
    now = datetime.now(timezone.utc)

    # "через 5 [минут/мин]"
    if s.startswith("через "):
        parts = s.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return now + timedelta(minutes=int(parts[1]))

    # "+10", "+10 мин"
    if s.startswith("+"):
        t = s[1:].strip().replace(" мин", "").strip()
        if t.isdigit():
            return now + timedelta(minutes=int(t))

    # "завтра 14:30"
    if s.startswith("завтра"):
        rest = s.replace("завтра", "").strip()
        if ":" in rest:
            hh, mm = rest.split(":", 1)
            return (now + timedelta(days=1)).replace(
                hour=int(hh), minute=int(mm), second=0, microsecond=0
            )

    # "14:30" (если время прошло — на завтра)
    if ":" in s:
        hh, mm = s.split(":", 1)
        if hh.isdigit() and mm.isdigit():
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target

    return now + timedelta(minutes=2)

def _parse_repeat_to_cron(raw: str) -> str:
    """
    Поддерживаем:
     • "cron: */5 * * * *"
     • "каждую минуту"
     • "каждые N минут/мин/минуты"
     • "ежедневно HH:MM"
     • "HH:MM" (ежедневно)
    """
    s = (raw or "").strip().lower()
    if s.startswith("cron:"):
        return s.split("cron:", 1)[1].strip()

    if s == "каждую минуту":
        return "* * * * *"

    # каждые 2 минуты / каждые 10 мин / каждые 2 минут(ы)
    m = re.match(r"^кажд(ый|ые)\s+(\d+)\s*(минут(у|ы)?|мин)\b", s)
    if m:
        n = max(1, min(59, int(m.group(2))))
        return f"*/{n} * * * *"

    # ежедневно 14:30
    if s.startswith("ежедневно"):
        rest = s.replace("ежедневно", "").strip()
        if ":" in rest:
            hh, mm = rest.split(":", 1)
            return f"{int(mm)} {int(hh)} * * *"

    # 14:30 → ежедневно
    if ":" in s:
        hh, mm = s.split(":", 1)
        return f"{int(mm)} {int(hh)} * * *"

    # по умолчанию — каждую минуту
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

# ========= вспомогательные для /list =========
def _build_reminders_list_text(rows: list[dict]) -> str:
    if not rows:
        return "📭 Пока нет напоминаний."
    lines = ["<b>🧾 Напоминания этого чата:</b>"]
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
        await m.answer(f"🏓 pong — ❌ db error: <code>{e}</code>")

# -------- /add (одноразовое) --------
@dp.message(Command("add"))
async def add_once_start(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.set_state(AddOnceSG.text)
    await m.answer("📝 Введи текст напоминания:")

@dp.message(AddOnceSG.text, F.text)
async def add_once_text(m: types.Message, state: FSMContext):
    await state.update_data(text=m.text.strip())
    await state.set_state(AddOnceSG.when)
    await m.answer("⏰ Когда напомнить?\nПримеры: <b>14:30</b> · <b>завтра 10:00</b> · <b>через 25</b> · <b>+15</b>")

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
        await m.answer(f"✅ Напоминание создано:\n<b>{text}</b>\n🕒 {_fmt_utc(row.get('remind_at', when))}")
    except Exception as e:
        await m.answer(f"❌ Не удалось создать. Причина: <code>{e}</code>")
    finally:
        await state.clear()

# -------- /add_repeat (повторяющееся) --------
@dp.message(Command("add_repeat"))
async def add_repeat_start(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.set_state(AddRepeatSG.text)
    await m.answer("📝 Введи текст <b>повторяющегося</b> напоминания:")

@dp.message(AddRepeatSG.text, F.text)
async def add_repeat_text(m: types.Message, state: FSMContext):
    await state.update_data(text=m.text.strip())
    await state.set_state(AddRepeatSG.sched)
    await m.answer(
        "⏱ Какое расписание?\n"
        "• <b>каждую минуту</b>\n"
        "• <b>каждые N минут</b>\n"
        "• <b>ежедневно HH:MM</b>\n"
        "• <b>HH:MM</b> (тоже ежедневно)\n"
        "• <b>cron: * * * * *</b> (любой CRON)"
    )

@dp.message(AddRepeatSG.sched, F.text)
async def add_repeat_finish(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    data = await state.get_data()
    text = data.get("text", "").strip()
    expr = _parse_repeat_to_cron(m.text)

    try:
        # валидируем cron и сразу считаем next_at для вывода пользователю
        next_at = _cron_next_utc(expr)

        row = add_recurring_reminder(
            user_id=m.from_user.id,
            chat_id=m.chat.id,
            text=text,
            cron_expr=expr,
        )
        await m.answer(
            "✅ Повторяющееся напоминание создано:\n"
            f"<b>{text}</b>\n"
            f"🕒 Ближайшее: {_fmt_utc(row.get('next_at') or next_at)}\n"
            f"🔁 CRON: <code>{expr}</code>"
        )
    except Exception as e:
        await m.answer(
            "❌ Не удалось создать повторяющееся. Проверь формат (можно <code>cron: EXPR</code>).\n"
            f"<code>{e}</code>"
        )
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
        return await cq.answer("Некорректные данные", show_alert=True)

    try:
        if action == "pause":
            set_paused(rid, True)
            await cq.answer("⏸ Поставлено на паузу")
        elif action == "resume":
            set_paused(rid, False)
            await cq.answer("▶️ Возобновлено")
        elif action == "delete":
            delete_reminder_by_id(rid)
            await cq.answer("🗑 Удалено")
        else:
            return await cq.answer("Неизвестное действие", show_alert=True)

        # Перерисуем список под исходным сообщением
        if cq.message:
            await _refresh_list_message(cq.message.chat.id, cq.message)
    except Exception as e:
        await cq.answer(f"Ошибка: {e}", show_alert=True)

# ---------- Webhook ----------
@app.post(f"/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    # Совместимая загрузка Update (устраняет BaseModel.__init__() TypeError)
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
    # стартуем фоновый планировщик
    if _tourney:
        _tourney.start()
    if _universal:
        _universal.start()

    # регистрируем вебхук
    await bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}/{WEBHOOK_SECRET}",
        drop_pending_updates=True,
    )
    # команды
    await bot.set_my_commands(
        [
            BotCommand("help", "Справка по командам"),
            BotCommand("add", "Создать разовое 📝"),
            BotCommand("add_repeat", "Создать повторяющееся 🔁"),
            BotCommand("list", "Список с кнопками 🧾"),
            BotCommand("ping", "Проверка состояния 🏓"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )

@app.get("/")
async def root():
    return {"status": "up"}

@app.get("/health")
async def health():
    return {"ok": True}
