# FILE: main.py
import os
import logging
from datetime import datetime, timedelta

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
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# ==== локальные модули ====
from scheduler_core import TournamentScheduler, UniversalReminderScheduler
from texts import HELP_TEXT
from db import (
    upsert_chat,
    upsert_telegram_user,
    get_active_reminders,
    add_reminder,
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

# ================== FSM: мастер добавления ==================
class AddReminderSG(StatesGroup):
    text = State()
    when = State()

# =============== ВСПОМОГАТЕЛЬНОЕ =================
async def _ensure_user_chat(m: types.Message) -> None:
    """Гарантируем, что в БД есть записи чата/пользователя (устраняет «первое молчание»)."""
    try:
        if m.from_user:
            upsert_telegram_user(m.from_user.id)
        upsert_chat(chat_id=m.chat.id, type_=m.chat.type, title=getattr(m.chat, "title", None))
    except Exception as e:
        logger.exception("ensure_user_chat failed: %s", e)

def _parse_when(raw: str) -> datetime:
    """
    Простой парсер времени:
    - 'через N минут' / '+N мин' / '+N'
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
    now = datetime.utcnow()

    # через N минут
    if s.startswith("через "):
        parts = s.split()
        # «через 15», «через 15 мин», «через 1 минуту»
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
        hh, mm = rest.split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0) + timedelta(days=1)
        return target

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

def _fmt_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M (UTC)")

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
        # если запрос к БД проходит — уже хорошо
        _ = get_active_reminders(m.from_user.id)
        sched = "ok" if (_tourney is not None and _universal is not None) else "no"
        await m.answer(f"pong ✅  | db=ok | sched={sched}")
    except Exception as e:
        await m.answer(f"pong ❌  | db error: <code>{e}</code>")

# ----- Турнирные (заглушки, сами рассылки делает scheduler_core) -----
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

# ----- Универсальные напоминания -----
@dp.message(Command("add"))
async def add_start(m: types.Message, state: FSMContext):
    # Поддерживает и /add, и /add@BotName (в группе)
    await _ensure_user_chat(m)
    await state.set_state(AddReminderSG.text)
    await m.answer("📝 Введи текст напоминания:")

@dp.message(AddReminderSG.text)
async def add_wait_when(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.update_data(text=m.text.strip())
    await state.set_state(AddReminderSG.when)
    await m.answer("⏰ Когда напомнить?\nПримеры: 14:30 · завтра 10:00 · через 25 минут · +15")

@dp.message(AddReminderSG.when)
async def add_finish(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    data = await state.get_data()
    text = data.get("text", "").strip()
    when_raw = (m.text or "").strip()

    if not text:
        await state.clear()
        return await m.answer("❌ Текст пустой. Попробуй ещё раз: /add")

    try:
        remind_at_utc = _parse_when(when_raw)
        _ = add_reminder(
            user_id=m.from_user.id,
            chat_id=m.chat.id,
            text=text,
            remind_at=remind_at_utc,
        )
        await state.clear()
        when_str = _fmt_utc(remind_at_utc)
        # ВЫВОД БЕЗ ID, «по-человечески»
        await m.answer(f"✅ Напоминание создано:\n<b>{text}</b>\n🕒 {when_str}")
    except Exception as e:
        logger.exception("add_finish error: %s", e)
        await m.answer("❌ Не удалось создать напоминание. Попробуй ещё раз или измени формат времени.")

@dp.message(Command("list"))
async def cmd_list(m: types.Message):
    await _ensure_user_chat(m)
    items = get_active_reminders(m.from_user.id)
    if not items:
        return await m.answer("Пока нет активных напоминаний.")
    lines = []
    for r in items:
        # поддерживаем dict/tuple (в зависимости от реализации db)
        rid = r["id"] if isinstance(r, dict) else r[0]
        text = r["text"] if isinstance(r, dict) else r[1]
        remind_at = r.get("remind_at") if isinstance(r, dict) else r[2]
        when_str = remind_at if isinstance(remind_at, str) else _fmt_utc(remind_at)
        # ID нужен только здесь — для управления:
        lines.append(f"• <code>{rid}</code> — {text} — {when_str}")
    await m.answer("🔔 Активные напоминания:\n" + "\n".join(lines))

@dp.message(Command("delete"))
async def cmd_delete(m: types.Message):
    await _ensure_user_chat(m)
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Укажи ID: /delete <id>")
    rid = parts[1].strip()
    try:
        delete_reminder_by_id(rid, m.chat.id)
        await m.answer("🗑️ Напоминание удалено.")
    except Exception as e:
        logger.exception("delete error: %s", e)
        await m.answer("❌ Не удалось удалить напоминание.")

@dp.message(Command("pause"))
async def cmd_pause(m: types.Message):
    await _ensure_user_chat(m)
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Укажи ID: /pause <id>")
    rid = parts[1].strip()
    try:
        set_paused(reminder_id=rid, chat_id=m.chat.id, paused=True)
        await m.answer("⏸️ Напоминание поставлено на паузу.")
    except Exception as e:
        logger.exception("pause error: %s", e)
        await m.answer("❌ Не удалось поставить на паузу.")

@dp.message(Command("resume"))
async def cmd_resume(m: types.Message):
    await _ensure_user_chat(m)
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Укажи ID: /resume <id>")
    rid = parts[1].strip()
    try:
        set_paused(reminder_id=rid, chat_id=m.chat.id, paused=False)
        await m.answer("▶️ Напоминание возобновлено.")
    except Exception as e:
        logger.exception("resume error: %s", e)
        await m.answer("❌ Не удалось возобновить напоминание.")

# ====== Авто-регистрация чата/пользователя при добавлении бота в группу ======
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
        # Возвращаем 200, чтобы Telegram не считал это 502
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
    # Запустить планировщики
    if _tourney:
        _tourney.start()
    if _universal:
        _universal.start()

    # Вебхук
    await bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}/{WEBHOOK_SECRET}",
        drop_pending_updates=True,
    )

    # Команды для приватных чатов
    await bot.set_my_commands(
        [
            BotCommand(command="help", description="Показать команды"),
            BotCommand(command="add", description="Создать напоминание"),
            BotCommand(command="list", description="Список напоминаний"),
            BotCommand(command="delete", description="Удалить напоминание по ID"),
            BotCommand(command="pause", description="Пауза напоминания"),
            BotCommand(command="resume", description="Возобновить напоминание"),
            BotCommand(command="ping", description="Проверка состояния"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )

    # Команды для групп
    await bot.set_my_commands(
        [
            BotCommand(command="help", description="Показать команды"),
            BotCommand(command="add", description="Создать напоминание"),
            BotCommand(command="list", description="Список напоминаний"),
            BotCommand(command="delete", description="Удалить напоминание по ID"),
            BotCommand(command="pause", description="Пауза напоминания"),
            BotCommand(command="resume", description="Возобновить напоминание"),
            BotCommand(command="subscribe_tournaments", description="Включить турнирные напоминания"),
            BotCommand(command="unsubscribe_tournaments", description="Выключить турнирные напоминания"),
            BotCommand(command="tourney_now", description="Пробное турнирное уведомление"),
            BotCommand(command="ping", description="Проверка состояния"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )

    logger.info("Webhook & commands registered. Public URL: %s", PUBLIC_BASE_URL)
