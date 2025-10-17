import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllGroupChats,
)

import db
from scheduler_core import delivery_loop, DEFAULT_TZ
from time_parse import parse_once_when, parse_repeat_spec, to_utc, to_local
from texts import *
from utils import short_rid, is_owner
from croniter import croniter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("remindly")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_USER_ID = os.getenv("OWNER_USER_ID", "0")
PARSE_MODE = os.getenv("PARSE_MODE", "HTML")

# aiogram 3.7+: parse_mode через DefaultBotProperties
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


class AddOnce(StatesGroup):
    waiting_text = State()
    waiting_when = State()


class AddCron(StatesGroup):
    waiting_text = State()
    waiting_spec = State()


@dp.message(Command("start"))
async def cmd_start(m: Message):
    await db.upsert_chat(m.chat.id, m.chat.type, getattr(m.chat, "title", None))
    await m.answer(START)


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP)


@dp.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer(PING)


def _owner_guard(m: Message) -> bool:
    if m.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return is_owner(m.from_user.id, OWNER_USER_ID)
    return True


# ===== Одноразовые =====
@dp.message(Command("add"))
async def cmd_add(m: Message, state: FSMContext):
    await db.upsert_chat(m.chat.id, m.chat.type, getattr(m.chat, "title", None))
    await state.set_state(AddOnce.waiting_text)
    await m.answer(ASK_TEXT_ONCE)


@dp.message(AddOnce.waiting_text)
async def add_once_text(m: Message, state: FSMContext):
    await state.update_data(text=m.text.strip())
    await state.set_state(AddOnce.waiting_when)
    await m.answer(ASK_WHEN_ONCE)


@dp.message(AddOnce.waiting_when)
async def add_once_when(m: Message, state: FSMContext):
    data = await state.get_data()
    text = data["text"]

    now_local = datetime.now(tz=DEFAULT_TZ)
    try:
        when_local, human = parse_once_when(m.text, now_local, DEFAULT_TZ)
    except Exception as e:
        await m.answer(f"❗️ {e}")
        return

    remind_at_utc = to_utc(when_local, DEFAULT_TZ)
    _ = await db.create_once(m.chat.id, m.from_user.id, text, remind_at_utc)
    await state.clear()
    await m.answer(CONFIRM_ONCE_SAVED.format(when_human=human))


# ===== Повторяющиеся =====
@dp.message(Command("repeat"))
async def cmd_repeat(m: Message, state: FSMContext):
    await db.upsert_chat(m.chat.id, m.chat.type, getattr(m.chat, "title", None))
    await state.set_state(AddCron.waiting_text)
    await m.answer(ASK_TEXT_CRON)


# Фоллбэк: поймает /repeat@BotName в группах
@dp.message(F.text.regexp(r"^/repeat(?:@[\w_]+)?\b"))
async def cmd_repeat_alias(m: Message, state: FSMContext):
    return await cmd_repeat(m, state)


@dp.message(AddCron.waiting_text)
async def add_cron_text(m: Message, state: FSMContext):
    await state.update_data(text=m.text.strip())
    await state.set_state(AddCron.waiting_spec)
    await m.answer(ASK_SPEC_CRON)


@dp.message(AddCron.waiting_spec)
async def add_cron_spec(m: Message, state: FSMContext):
    data = await state.get_data()
    text = data["text"]

    now_local = datetime.now(tz=DEFAULT_TZ)
    try:
        cron_expr, human_suffix, next_local = parse_repeat_spec(m.text, now_local)
    except Exception as e:
        await m.answer(f"❗️ {e}")
        return

    next_utc = to_utc(next_local, DEFAULT_TZ)
    _ = await db.create_cron(m.chat.id, m.from_user.id, text, cron_expr, next_utc, category=None)
    await state.clear()
    await m.answer(CONFIRM_CRON_SAVED.format(next_local=next_local.strftime("%Y-%m-%d %H:%M")))


# ===== /list =====
def _row_to_line(row) -> str:
    kind = row["kind"]
    text = row["text"]
    paused = row["paused"]
    _rid = short_rid(row["id"])
    if kind == "once":
        when = row["remind_at"]
        return f"• ⏱ {to_local(when, DEFAULT_TZ).strftime('%Y-%m-%d %H:%M')} — “{text}” {'(⏸)' if paused else ''}"
    else:
        nxt = row["next_at"]
        expr = row["cron_expr"]
        return f"• 🔁 {expr} → {to_local(nxt, DEFAULT_TZ).strftime('%Y-%m-%d %H:%M')} — “{text}” {'(⏸)' if paused else ''}"


def _row_buttons(row):
    rid = row["id"]
    paused = row["paused"]
    btns = []
    if paused:
        btns.append(InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"resume:{rid}"))
    else:
        btns.append(InlineKeyboardButton(text="⏸ Пауза", callback_data=f"pause:{rid}"))
    btns.append(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{rid}"))
    return InlineKeyboardMarkup(inline_keyboard=[btns])


@dp.message(Command("list"))
async def cmd_list(m: Message):
    rows = await db.list_by_chat(m.chat.id)
    if not rows:
        await m.answer(LIST_EMPTY)
        return
    await m.answer(LIST_HEADER)
    for r in rows:
        await m.answer(_row_to_line(r), reply_markup=_row_buttons(r))


@dp.callback_query(F.data.startswith(("pause:", "resume:", "del:")))
async def cb_list_actions(c: CallbackQuery):
    action, rid = c.data.split(":", 1)
    if action == "pause":
        await db.set_paused(rid, True)
        await c.answer(PAUSED, show_alert=False)
    elif action == "resume":
        await db.set_paused(rid, False)
        await c.answer(RESUMED, show_alert=False)
    elif action == "del":
        await db.delete_reminder(rid)
        await c.answer(DELETED, show_alert=False)
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ===== Турнирные подписки =====
def _tournament_crons_local():
    """
    Напоминания за 5 минут до старта "Быстрого турнира" по МСК.
    Старты: 14:00,16:00,18:00,20:00,22:00,00:00
    Напоминания отправляются: 13:55,15:55,17:55,19:55,21:55,23:55 (МСК)
    Возвращаем список cron-выражений в МЕСТНОМ (DEFAULT_TZ) времени.
    """
    times = [(13, 55), (15, 55), (17, 55), (19, 55), (21, 55), (23, 55)]  # МСК
    # "mm hh * * *"
    return [f"{mm} {hh} * * *" for hh, mm in times]


async def _install_tournament_crons_for_chat(chat_id: int, user_id: int):
    # Удалять существующие турнирные не будем — обновлять next_at при первом срабатывании.
    now_local = datetime.now(tz=DEFAULT_TZ)
    for expr in _tournament_crons_local():
        next_local = croniter(expr, now_local).get_next(datetime)
        next_utc = to_utc(next_local, DEFAULT_TZ)
        text = "🏆 «Быстрый турнир» начнётся через 5 минут!"
        await db.create_cron(chat_id, user_id, text, expr, next_utc, category="tournament")


@dp.message(Command("subscribe_tournaments"))
async def cmd_sub(m: Message):
    if not _owner_guard(m):
        await m.answer(NOT_ALLOWED)
        return
    await db.upsert_chat(m.chat.id, m.chat.type, getattr(m.chat, "title", None))
    await db.set_tournament(m.chat.id, True)
    await _install_tournament_crons_for_chat(m.chat.id, m.from_user.id)
    await m.answer(SUB_ON)


@dp.message(Command("unsubscribe_tournaments"))
async def cmd_unsub(m: Message):
    if not _owner_guard(m):
        await m.answer(NOT_ALLOWED)
        return
    await db.set_tournament(m.chat.id, False)
    await m.answer(SUB_OFF)


# ===== Запуск =====
async def on_startup():
    # Переключаемся на polling
    await bot.delete_webhook(drop_pending_updates=False)

    # Меню команд: /repeat отображается и в ЛС, и в группах
    cmds = [
        BotCommand(command="help", description="Справка"),
        BotCommand(command="add", description="Одноразовое напоминание"),
        BotCommand(command="repeat", description="Повторяющееся напоминание"),
        BotCommand(command="list", description="Список напоминаний"),
        BotCommand(command="subscribe_tournaments", description="Включить турнирные напоминания"),
        BotCommand(command="unsubscribe_tournaments", description="Выключить турнирные"),
        BotCommand(command="ping", description="Проверка связи"),
    ]
    await bot.set_my_commands(cmds, scope=BotCommandScopeDefault())
    await bot.set_my_commands(cmds, scope=BotCommandScopeAllGroupChats())

    me = await bot.get_me()
    logging.info("Bot is up: @%s (id=%s)", me.username, me.id)

    # Стартуем фоновый планировщик
    asyncio.create_task(delivery_loop(bot))


def main():
    if not os.getenv("BOT_TOKEN") or not os.getenv("DATABASE_URL"):
        raise RuntimeError("BOT_TOKEN / DATABASE_URL не заданы")
    dp.startup.register(on_startup)
    dp.run_polling(bot)


if __name__ == "__main__":
    main()
