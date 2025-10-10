# FILE: main.py
import logging
import os
from datetime import datetime, time, timedelta, timezone

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command
from aiogram.types import Update, BotCommand
from aiogram.utils.chat_action import ChatActionSender

# FSM для мастера /add
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db import (
    upsert_chat,
    set_tournament_subscription,
    add_reminder,
    get_active_reminders,
    delete_reminder_by_id,
    set_paused,
)
from scheduler_core import TournamentScheduler, UniversalReminderScheduler
from texts import HELP_TEXT

# ======================= Конфигурация =======================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "webhook")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

if not BOT_TOKEN or not PUBLIC_BASE_URL:
    raise RuntimeError("TELEGRAM_BOT_TOKEN and PUBLIC_BASE_URL must be set")

app = FastAPI()
logger = logging.getLogger("remindly")
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ======================= Хелперы ============================
MSK_TZ = "Europe/Moscow"
TOURNEY_SLOTS = [time(14, 0), time(16, 0), time(18, 0), time(20, 0), time(22, 0), time(0, 0)]

def _msk_now():
    import pytz
    return datetime.now(pytz.timezone(MSK_TZ))

def _parse_when(text: str) -> datetime | None:
    """
    Парсим простые фразы:
      - HH:MM (если время уже прошло — на завтра)
      - завтра HH:MM
      - через N минут / через N часов
    Возвращаем UTC datetime.
    """
    q = text.strip().lower()
    now = datetime.now(timezone.utc)

    # через N минут/часов
    if q.startswith("через"):
        parts = q.replace("через", "").strip().split()
        if not parts:
            return None
        try:
            n = int(parts[0])
        except ValueError:
            return None
        unit = parts[1] if len(parts) > 1 else "мин"
        if unit.startswith("час"):
            return now + timedelta(hours=n)
        return now + timedelta(minutes=n)

    # завтра HH:MM
    if q.startswith("завтра"):
        hhmm = q.replace("завтра", "").strip()
        try:
            hh, mm = map(int, hhmm.split(":"))
        except Exception:
            return None
        # считаем «завтра» по UTC (просто +1 день к сегодняшней дате UTC)
        dt = datetime.now(timezone.utc).replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)
        return dt

    # HH:MM
    if ":" in q:
        try:
            hh, mm = map(int, q.split(":"))
        except Exception:
            return None
        dt = datetime.now(timezone.utc).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now:
            dt = dt + timedelta(days=1)
        return dt

    return None

# ======================= Команды /start /help =======================
@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer("Приветствую тебя! Напиши /help, чтобы увидеть мои команды.")

@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await m.answer(HELP_TEXT, parse_mode=None, disable_web_page_preview=True)

# ======================= Турнирный планировщик ======================
_tourney = TournamentScheduler(bot)
_universal = UniversalReminderScheduler(bot)

# ======================= Webhook =======================
@app.post(f"/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception:
        logger.exception("Webhook handler failed")
        # Возвращаем 200, чтобы Telegram не считал ошибку как 502
        return {"ok": True}

@app.get("/")
async def root():
    return {"status": "up"}

@app.get("/health")
async def health():
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    _tourney.start()
    _universal.start()

    await bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}/{WEBHOOK_SECRET}",
        drop_pending_updates=True,
    )

    await bot.set_my_commands(
        [
            BotCommand(command="help", description="Показать команды"),
            BotCommand(command="subscribe_tournaments", description="Включить турнирные напоминания"),
            BotCommand(command="unsubscribe_tournaments", description="Выключить турнирные напоминания"),
            BotCommand(command="tourney_now", description="Прислать пробное напоминание турнира"),
            BotCommand(command="schedule", description="Показать ближайшие турниры"),
            BotCommand(command="add", description="Создать напоминание"),
            BotCommand(command="list", description="Список напоминаний"),
            BotCommand(command="delete", description="Удалить напоминание"),
            BotCommand(command="pause", description="Пауза напоминания"),
            BotCommand(command="resume", description="Возобновить напоминание"),
        ]
    )

# ======================= Проверка прав администратора =================
async def _is_admin(message: types.Message) -> bool:
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("Эта команда доступна только в групповых чатах.")
        return False
    member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    if not (member.is_chat_admin() or member.is_chat_creator()):
        await message.answer("Только администраторы чата могут это делать.")
        return False
    return True

# ======================= Команды для турниров =======================
@dp.message(Command("subscribe_tournaments"))
async def cmd_subscribe_tournaments(m: types.Message):
    if not await _is_admin(m):
        return
    # Сохраним чат в БД и включим подписку
    await m.chat.do(ChatActionSender.typing())
    upsert_chat(chat_id=m.chat.id, type_=m.chat.type, title=m.chat.title)
    set_tournament_subscription(chat_id=m.chat.id, value=True)
    await m.answer(
        "✅ Турнирные напоминания включены.\n"
        "Напоминания приходят за 5 минут до стартов: 14:00, 16:00, 18:00, 20:00, 22:00, 00:00 (МСК)."
    )

@dp.message(Command("unsubscribe_tournaments"))
async def cmd_unsubscribe_tournaments(m: types.Message):
    if not await _is_admin(m):
        return
    set_tournament_subscription(chat_id=m.chat.id, value=False)
    await m.answer("⏸️ Турнирные напоминания выключены в этом чате.")

@dp.message(Command("tourney_now"))
async def cmd_tourney_now(m: types.Message):
    if not await _is_admin(m):
        return
    now = datetime.now()
    display = time(now.hour, (now.minute // 5) * 5)  # округлим до 5 минут
    await m.answer("🚀 Отправляю пробное напоминание турнира прямо сейчас…")
    await _tourney._send_tournament(m.chat.id, display)

@dp.message(Command("schedule"))
async def cmd_schedule(m: types.Message):
    now = _msk_now()
    today = now.date()
    # соберём ближайшие слоты с «напоминанием за 5 минут»
    slots: list[tuple[datetime, datetime]] = []
    for t in TOURNEY_SLOTS:
        dt = now.tzinfo.localize(datetime.combine(today, t))
        if t == time(0, 0):  # полночь => следующий день
            dt = dt + timedelta(days=1)
        reminder = dt - timedelta(minutes=5)
        if reminder >= now:
            slots.append((dt, reminder))
    if not slots:
        next_day = today + timedelta(days=1)
        for t in TOURNEY_SLOTS:
            dt = now.tzinfo.localize(datetime.combine(next_day, t))
            reminder = dt - timedelta(minutes=5)
            slots.append((dt, reminder))

    lines = ["📅 Ближайшие старты турниров (МСК):"]
    for dt, rem in slots[:6]:
        lines.append(f"• старт {dt.strftime('%d.%m %H:%M')} — напоминание в {rem.strftime('%H:%M')}")
    await m.answer("\n".join(lines))

# ======================= Универсальные напоминания ===================
class AddReminder(StatesGroup):
    waiting_for_text = State()
    waiting_for_time = State()

@dp.message(Command("add"))
async def add_start(message: types.Message, state: FSMContext):
    await state.set_state(AddReminder.waiting_for_text)
    await message.answer("📝 Введи текст напоминания:")

@dp.message(AddReminder.waiting_for_text)
async def add_got_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text.strip())
    await state.set_state(AddReminder.waiting_for_time)
    await message.answer("⏰ Когда напомнить?\nПримеры: <code>14:30</code> • <code>завтра 10:00</code> • <code>через 25 минут</code> • <code>через 2 часа</code>")

@dp.message(AddReminder.waiting_for_time)
async def add_got_time(message: types.Message, state: FSMContext):
    when = _parse_when(message.text)
    if not when:
        await message.answer("⚠️ Неверный формат. Примеры: 14:30 • завтра 10:00 • через 25 минут • через 2 часа")
        return
    data = await state.get_data()
    text = data["text"]
    add_reminder(message.from_user.id, message.chat.id, text, when)
    await state.clear()
    await message.answer(f"✅ Напоминание создано:\n<b>{text}</b>\n🕒 {when.strftime('%Y-%m-%d %H:%M')} (UTC)")

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    res = get_active_reminders(message.from_user.id)
    items = res.data or []
    if not items:
        await message.answer("Пока нет активных напоминаний.")
        return
    lines = ["📋 Твои напоминания:"]
    for r in items:
        rid = str(r["id"])[:8]
        when = r["remind_at"]
        paused = "⏸️" if r.get("paused") else "▶️"
        lines.append(f"• <code>{rid}</code> [{paused}] — {r['text']} — {when}")
    await message.answer("\n".join(lines))

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Укажи id: /delete <id>")
        return
    rid = parts[1]
    delete_reminder_by_id(rid)
    await message.answer(f"🗑 Удалил напоминание <code>{rid}</code>")

@dp.message(Command("pause"))
async def cmd_pause(message: types.Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Укажи id: /pause <id>")
        return
    rid = parts[1]
    set_paused(rid, True)
    await message.answer(f"⏸️ Поставил на паузу <code>{rid}</code>")

@dp.message(Command("resume"))
async def cmd_resume(message: types.Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Укажи id: /resume <id>")
        return
    rid = parts[1]
    set_paused(rid, False)
    await message.answer(f"▶️ Возобновил напоминание <code>{rid}</code>")
