import logging
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Update
import os
from scheduler_core import TournamentScheduler

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "webhook")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

if not BOT_TOKEN or not PUBLIC_BASE_URL:
    raise RuntimeError("TELEGRAM_BOT_TOKEN and PUBLIC_BASE_URL must be set")

app = FastAPI()
logger = logging.getLogger("remindly")
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# === Handlers ===
from texts import HELP_TEXT

@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    await m.answer("Приветствую тебя! Напишите /help, чтобы увидеть мои команды.")

@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await m.answer(HELP_TEXT, parse_mode=None, disable_web_page_preview=True)

_tourney = TournamentScheduler(bot)

@app.post(f"/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception:
        logger.exception("Webhook handler failed")
        # Возвращаем 200, чтобы Telegram не считал это 502, пока разбираемся
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
