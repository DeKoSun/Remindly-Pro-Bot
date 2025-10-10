import os
import asyncio
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ChatJoinRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.webhook.serializers.json import JSONSerializer
from aiogram.filters import Command
from texts import HELP_TEXT, pick_phrase, REMINDER_VARIANTS
from utils import is_admin, DEFAULT_TZ
from db import upsert_chat, set_tournament_subscription, create_reminder, list_reminders, set_active, delete_reminder
from scheduler_core import TournamentScheduler


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "webhook")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")


if not BOT_TOKEN or not PUBLIC_BASE_URL:
raise RuntimeError("TELEGRAM_BOT_TOKEN and PUBLIC_BASE_URL must be set")


app = FastAPI()
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# Scheduler
_tourney = TournamentScheduler(bot)


@dp.message(Command("start"))
async def cmd_start(m: types.Message):
await m.answer("Бот готов. /help — список команд.")


@dp.message(Command("help"))
async def cmd_help(m: types.Message):
await m.answer(HELP_TEXT)


@dp.message(Command("subscribe_tournaments"))
async def cmd_subscribe(m: types.Message):
if m.chat.type not in ("group", "supergroup"):
return await m.answer("Эта команда работает только в группе.")
if not await is_admin(m):
return await m.answer("Только администраторы группы могут управлять подпиской.")
upsert_chat(m.chat.id, m.chat.type, m.chat.title)
set_tournament_subscription(m.chat.id, True)
await m.answer("Напоминания турниров включены в этом чате. Я пришлю уведомления за 5 минут до старта.")


@dp.message(Command("unsubscribe_tournaments"))