# main.py
import os
import logging
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command
from aiogram.types import (
    Update,
    BotCommand,
    ChatMemberUpdated,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    CallbackQuery,
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
    get_active_reminders,           # для /ping
    get_active_reminders_for_chat,  # для рендера списка с кнопками
    add_reminder,                   # once
    add_recurring_reminder,         # cron
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

# Планировщики
_tourney: TournamentScheduler | None = TournamentScheduler(bot)
_universal: UniversalReminderScheduler | None = UniversalReminderScheduler(bot)

# ================== FSM: мастера добавления ==================
class AddOnceSG(StatesGroup):
    text = State()
    when = State()

class AddRepeatSG(StatesGroup):
    text = State()
    sched = State()

# =============== ВСПОМОГАТЕЛЬНОЕ =================
async def _ensure_user_chat(m: types.Message) -> None:
    """Гарантируем, что в БД есть записи чата/пользователя (устраняет «первое молчание»)."""
    try:
        if m.from_user:
            upsert_telegram_user(m.from_user.id)
        upsert_chat(chat_id=m.chat.id, type_=m.chat.type, title=getattr(m.chat, "title", None))
    except Exception as e:
        logger.exception("ensure_user_chat failed: %s", e)

def _parse_when_once(raw: str) -> datetime:
    """
    Простой парсер времени для одноразовых:
    - 'через N минут' / '+N' / '+N мин'
    - 'завтра HH:MM'
    - 'HH:MM' — сегодня (если уже прошло — завтра)
    """
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

    # +N / +N мин
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

    # fail-safe
    return now + timedelta(minutes=2)

def _parse_repeat_to_cron(raw: str) -> str:
    """
    Конвертер человеко-понятного расписания → CRON:
    - 'каждую минуту' → '* * * * *'
    - 'ежедневно HH:MM' → 'MM HH * * *'
    - 'cron: <EXPR>' → <EXPR> как есть
    - 'HH:MM' → ежедневно в это время
    """
    s = (raw or "").strip().lower()
    if s.startswith("cron:"):
        expr = s.split("cron:", 1)[1].strip()
        return expr

    if s == "каждую минуту":
        return "* * * * *"

    if s.startswith("ежедневно"):
        rest = s.replace("ежедневно", "").strip()
        if ":" in rest:
            hh, mm = rest.split(":")[:2]
            if hh.isdigit() and mm.isdigit():
                return f"{int(mm)} {int(hh)} * * *"

    # если пришло просто «HH:MM» — трактуем как ежедневно в это время
    if ":" in s:
        hh, mm = s.split(":")[:2]
        if hh.isdigit() and mm.isdigit():
            return f"{int(mm)} {int(hh)} * * *"

    # по умолчанию — раз в минуту, чтобы не молчать
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

# ========= Рендер списка с кнопками =========
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
        # первая кнопка — пауза/возобновить
        if paused:
            kb.button(text="▶️ Возобновить", callback_data=f"rem:resume:{rid}")
        else:
            kb.button(text="⏸ Пауза", callback_data=f"rem:pause:{rid}")
        # вторая — удалить
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
        # если редактировать нельзя (например, слишком старое сообщение) — просто отправим новое
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
    """Быстрый health-check: БД и планировщики."""
    await _ensure_user_chat(m)
    try:
        _ = get_active_reminders(m.from_user.id)
        sched = "ok" if (_tourney is not None and _universal is not None) else "no"
        await m.answer(f"pong ✅  | db=ok | sched={sched}")
    except Exception as e:
        await m.answer(f"pong ❌  | db error: <code>{e}</code>")

# ----- Турнирные (рассылки делает scheduler_core) -----
@dp.message(Command("subscribe_tournaments"))
async def cmd_subscribe_tournaments(m: types.Message):
    await _ensure_user_chat(m)
    if m.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await m.answer("Эта команда доступна только в группах.")
    await m.answer("✅ Турнирные напоминания включены в этом чате.")

@dp.message(Command("unsubscribe_tournaments"))
async def cmd_unsubscribe_tournaments(m: types.Message):
    await _ensure_user_chat(m)
    if m.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return await m.answer("Эта команда доступна только в группах.")
    await m.answer("⏸️ Турнирные напоминания выключены в этом чате.")

@dp.message(Command("tourney_now"))
async def cmd_tourney_now(m: types.Message):
    await _ensure_user_chat(m)
    await m.answer("📣 (debug) Пробное турнирное напоминание.")

# ---------- Одноразовые ----------
@dp.message(Command("add"))
async def add_once_start(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.set_state(AddOnceSG.text)
    await m.answer("📝 Введи текст напоминания:")

@dp.message(AddOnceSG.text)
async def add_once_wait_when(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.update_data(text=m.text.strip())
    await state.set_state(AddOnceSG.when)
    await m.answer("⏰ Когда напомнить?\nПримеры: 14:30 · завтра 10:00 · через 25 минут · +15")

@dp.message(AddOnceSG.when)
async def add_once_finish(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    data = await state.get_data()
    text = data.get("text", "").strip()
    when_raw = (m.text or "").strip()

    if not text:
        await state.clear()
        return await m.answer("❌ Текст пустой. Попробуй ещё раз: /add")

    try:
        remind_at_utc = _parse_when_once(when_raw)
        _ = add_reminder(
            user_id=m.from_user.id,
            chat_id=m.chat.id,
            text=text,
            remind_at=remind_at_utc,
        )
        await state.clear()
        await m.answer(f"✅ Напоминание создано:\n<b>{text}</b>\n🕒 {_fmt_utc(remind_at_utc)}")
    except Exception as e:
        logger.exception("add_once_finish error: %s", e)
        await m.answer("❌ Не удалось создать напоминание. Попробуй ещё раз или измени формат времени.")

# ---------- Повторяющиеся ----------
@dp.message(Command("add_repeat"))
async def add_repeat_start(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.set_state(AddRepeatSG.text)
    await m.answer("📝 Введи текст повторяющегося напоминания:")

@dp.message(AddRepeatSG.text)
async def add_repeat_wait_sched(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.update_data(text=m.text.strip())
    await state.set_state(AddRepeatSG.sched)
    await m.answer(
        "⏰ Какое расписание?\n"
        "• <i>каждую минуту</i>\n"
        "• <i>ежедневно HH:MM</i>\n"
        "• <i>HH:MM</i> (тоже ежедневно)\n"
        "• <i>cron: */5 * * * *</i> (любой CRON)"
    )

@dp.message(AddRepeatSG.sched)
async def add_repeat_finish(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    data = await state.get_data()
    text = (data.get("text") or "").strip()
    sched_raw = (m.text or "").strip()

    if not text:
        await state.clear()
        return await m.answer("❌ Текст пустой. Попробуй ещё раз: /add_repeat")

    cron_expr = _parse_repeat_to_cron(sched_raw)
    try:
        if not croniter.is_valid(cron_expr):
            raise ValueError("bad cron")
        next_at = _cron_next_utc(cron_expr)
    except Exception as e:
        return await m.answer(
            "❌ Неверное расписание.\n"
            f"expr: <code>{cron_expr}</code>\n"
            "Попробуй: «каждую минуту», «ежедневно HH:MM», «HH:MM» или «cron: */5 * * * *»."
        )

    try:
        _ = add_recurring_reminder(
            user_id=m.from_user.id,
            chat_id=m.chat.id,
            text=text,
            cron_expr=cron_expr,
            next_at=next_at,
        )
        await state.clear()
        return await m.answer(
            "✅ Повторяющееся напоминание создано:\n"
            f"<b>{text}</b>\n"
            f"🔁 CRON: <code>{cron_expr}</code>\n"
            f"🕒 Ближайшее: {_fmt_utc(next_at)}"
        )
    except Exception as e:
        return await m.answer(
            "❌ DB insert failed.\n"
            f"reason: <code>{e}</code>\n"
            f"expr: <code>{cron_expr}</code>\nnext_at: <code>{_fmt_utc(next_at)}</code>"
        )

# ---------- Управление списком с кнопками ----------
@dp.message(Command("list"))
async def cmd_list(m: types.Message):
    await _ensure_user_chat(m)
    rows = get_active_reminders_for_chat(m.chat.id, include_paused=True)
    if not rows:
        return await m.answer("Пока нет напоминаний в этом чате.")
    text = _build_reminders_list_text(rows)
    kb = _build_reminders_keyboard(rows)
    await m.answer(text, reply_markup=kb.as_markup(), disable_web_page_preview=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("rem:"))
async def on_reminder_action(cb: CallbackQuery):
    try:
        _, action, rid = cb.data.split(":", 2)  # rem:pause:<uuid>
    except ValueError:
        return await cb.answer("Некорректные данные.", show_alert=True)

    try:
        if action == "pause":
            set_paused(rid, True)
            await cb.answer("Поставлено на паузу ✅")
        elif action == "resume":
            set_paused(rid, False)
            await cb.answer("Возобновлено ✅")
        elif action == "delete":
            delete_reminder_by_id(rid)
            await cb.answer("Удалено 🗑️")
        else:
            return await cb.answer("Неизвестное действие.", show_alert=True)
    except Exception as e:
        logger.exception("callback action failed: %s", e)
        return await cb.answer("Операция не удалась 😕", show_alert=True)

    # Обновляем сообщение со списком
    if cb.message:
        await _refresh_list_message(cb.message.chat.id, cb.message)

# ====== Авто-регистрация при добавлении бота в чат ======
@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    chat = update.chat
    try:
        upsert_chat(chat_id=chat.id, type_=chat.type, title=getattr(chat, "title", None))
        if update.from_user:
            upsert_telegram_user(update.from_user.id)
        logger.info("my_chat_member upsert: chat=%s user=%s", chat.id, getattr(update.from_user, "id", None))
    except Exception as e:
        logger.exception("my_chat_member upsert failed: %s", e)

# ================== Вебхук ==================
@app.post(f"/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception:
        logger.exception("Webhook handler failed")
        return {"ok": True}

@app.get("/")
async def root():
    return {"status": "up"}

@app.get("/health")
async def health():
    return {"ok": True}

# ================== Старт приложения ==================
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

    # Команды для приватных чатов
    await bot.set_my_commands(
        [
            BotCommand(command="help", description="Показать команды"),
            BotCommand(command="add", description="Создать разовое напоминание"),
            BotCommand(command="add_repeat", description="Создать повторяющееся"),
            BotCommand(command="list", description="Список (с кнопками)"),
            BotCommand(command="ping", description="Проверка состояния"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )

    # Команды для групп
    await bot.set_my_commands(
        [
            BotCommand(command="help", description="Показать команды"),
            BotCommand(command="add", description="Создать разовое напоминание"),
            BotCommand(command="add_repeat", description="Создать повторяющееся"),
            BotCommand(command="list", description="Список (с кнопками)"),
            BotCommand(command="subscribe_tournaments", description="Включить турнирные напоминания"),
            BotCommand(command="unsubscribe_tournaments", description="Выключить турнирные напоминания"),
            BotCommand(command="tourney_now", description="Пробное турнирное уведомление"),
            BotCommand(command="ping", description="Проверка состояния"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )

    logger.info("Webhook & commands registered. Public URL: %s", PUBLIC_BASE_URL)
