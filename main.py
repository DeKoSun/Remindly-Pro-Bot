import logging
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command
from aiogram.types import Update
from aiogram.utils.chat_action import ChatActionSender
from db import upsert_chat, set_tournament_subscription, add_reminder, get_active_reminders, delete_reminder_by_id, set_paused
from scheduler_core import TournamentScheduler, UniversalReminderScheduler
from texts import HELP_TEXT
from datetime import datetime, time

# === Конфигурация ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "webhook")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

if not BOT_TOKEN or not PUBLIC_BASE_URL:
    raise RuntimeError("TELEGRAM_BOT_TOKEN and PUBLIC_BASE_URL must be set")

app = FastAPI()
logger = logging.getLogger("remindly")
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# === Команды ===
@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer("Приветствую тебя! Напиши /help, чтобы увидеть мои команды.")

@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await m.answer(HELP_TEXT, parse_mode=None, disable_web_page_preview=True)

# === Турнирный планировщик ===
_tourney = TournamentScheduler(bot)

# === Webhook ===
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

_tourney = TournamentScheduler(bot)
_universal = UniversalReminderScheduler(bot)

@app.on_event("startup")
async def on_startup():
    _tourney.start()
    _universal.start()

    await bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}/{WEBHOOK_SECRET}",
        drop_pending_updates=True
    )

    await bot.set_my_commands(
        [
            BotCommand(command="help", description="Показать команды"),
            BotCommand(command="subscribe_tournaments", description="Включить турнирные напоминания"),
            BotCommand(command="unsubscribe_tournaments", description="Выключить турнирные напоминания"),
            BotCommand(command="tourney_now", description="Прислать пробное напоминание турнира"),
            BotCommand(command="add", description="Создать напоминание"),
            BotCommand(command="list", description="Список напоминаний"),
            BotCommand(command="delete", description="Удалить напоминание"),
            BotCommand(command="pause", description="Пауза напоминания"),
            BotCommand(command="resume", description="Возобновить напоминание"),
        ]
    )

# === Проверка прав администратора ===
async def _is_admin(message: types.Message) -> bool:
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("Эта команда доступна только в групповых чатах.")
        return False

    member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    if not (member.is_chat_admin() or member.is_chat_creator()):
        await message.answer("Только администраторы чата могут это делать.")
        return False

    return True

# === Команды для турниров ===
@dp.message(Command("subscribe_tournaments"))
async def cmd_subscribe_tournaments(m: types.Message):
    if not await _is_admin(m):
        return
    await m.chat.do(ChatActionSender.typing())
    upsert_chat(chat_id=m.chat.id, type_=m.chat.type, title=m.chat.title)
    set_tournament_subscription(chat_id=m.chat.id, value=True)
    await m.answer(
        "✅ Турнирные напоминания включены.\n"
        "Напоминания приходят за 5 минут до стартов: "
        "14:00, 16:00, 18:00, 20:00, 22:00, 00:00 (МСК)."
    )

@dp.message(Command("unsubscribe_tournaments"))
async def cmd_unsubscribe_tournaments(m: types.Message):
    if not await _is_admin(m):
        return
    set_tournament_subscription(chat_id=m.chat.id, value=False)
    await m.answer("⏸️ Турнирные напоминания выключены в этом чате.")

@dp.message(Command("test"))
async def cmd_test(m: types.Message):
    await m.answer("✅ Я на связи. Вебхук активен, расписание турниров запущено.")
@dp.message(Command("tourney_now"))
async def cmd_tourney_now(m: types.Message):
    # Только для админов групп
    if not await _is_admin(m):
        return
    now = datetime.now()
    # Передаём в отправку «время старта», чтобы текст был корректным
    display = time(now.hour, (now.minute // 5) * 5)  # округлим до 5 минут
    await m.answer("🚀 Отправляю пробное напоминание турнира прямо сейчас…")
    await _tourney._send_tournament(m.chat.id, display)