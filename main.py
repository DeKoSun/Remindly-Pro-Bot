import logging
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command
from aiogram.types import Update
from aiogram.utils.chat_action import ChatActionSender
from db import upsert_chat, set_tournament_subscription
from scheduler_core import TournamentScheduler
from texts import HELP_TEXT

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

@app.on_event("startup")
async def on_startup():
    _tourney.start()
    await bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}/{WEBHOOK_SECRET}",
        drop_pending_updates=True
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
