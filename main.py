import asyncio
import logging
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandObject
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

from croniter import croniter

import db
from scheduler_core import delivery_loop
from time_parse import (
    parse_once_when,
    parse_repeat_spec,
    to_utc,
    format_local_time,
    DEFAULT_TZ,  # обычно Europe/Moscow (МСК) — используется для турниров
)
from texts import *
from texts import TOURNEY_TEMPLATES
from utils import short_rid, is_owner

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("remindly")

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_USER_ID = os.getenv("OWNER_USER_ID", "0")

# aiogram 3.7+: parse_mode через DefaultBotProperties
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# =========================
# FSM состояния
# =========================
class AddOnce(StatesGroup):
    waiting_text = State()
    waiting_when = State()


class AddCron(StatesGroup):
    waiting_text = State()
    waiting_spec = State()


# =========================
# TZ utils
# =========================
async def tz_for_user_only(user_id: int) -> ZoneInfo | None:
    """Возвращает TZ, сохранённую пользователем, либо None."""
    tz_name = await db.get_user_timezone(user_id)
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


async def effective_tz(user_id: int, chat_id: int) -> ZoneInfo | None:
    """
    Эффективная TZ:
      1) персональная TZ пользователя (tg_users.timezone)
      2) default_timezone чата (chats.default_timezone)
    Если обе не заданы — None (попросим пользователя/админа указать).
    """
    utz = await tz_for_user_only(user_id)
    if utz:
        return utz
    ctz_name = await db.get_chat_timezone(chat_id)
    if ctz_name:
        try:
            return ZoneInfo(ctz_name)
        except Exception:
            return None
    return None


def tz_key(tz: ZoneInfo) -> str:
    """Вернуть ключ TZ, например 'America/New_York'."""
    return getattr(tz, "key", str(tz))


def _owner_guard(m: Message) -> bool:
    if m.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return is_owner(m.from_user.id, OWNER_USER_ID)
    return True


# =========================
# Общие команды
# =========================
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


# =========================
# Управление таймзонами
# =========================
@dp.message(Command("set_timezone"))
async def cmd_set_timezone(m: Message, command: CommandObject):
    """
    /set_timezone Europe/Moscow
    /set_timezone America/New_York
    /set_timezone Asia/Yekaterinburg
    """
    arg = (command.args or "").strip()
    if not arg:
        await m.answer(
            "Укажи свой часовой пояс, например:\n"
            "<code>/set_timezone Europe/Moscow</code>\n"
            "<code>/set_timezone America/New_York</code>\n"
            "<code>/set_timezone Asia/Yekaterinburg</code>"
        )
        return
    try:
        _ = ZoneInfo(arg)  # валидация
    except Exception:
        await m.answer("Неизвестный часовой пояс. Проверь написание (Region/City).")
        return

    await db.set_user_timezone(m.from_user.id, arg)
    await m.answer(f"✅ Часовой пояс сохранен: <b>{arg}</b>")


@dp.message(Command("my_timezone"))
async def cmd_my_timezone(m: Message):
    utz = await tz_for_user_only(m.from_user.id)
    ctz = await db.get_chat_timezone(m.chat.id)
    eff = await effective_tz(m.from_user.id, m.chat.id)
    await m.answer(
        "🕒 Твои TZ-настройки:\n"
        f"• Личная: <b>{tz_key(utz) if utz else '—'}</b>\n"
        f"• TZ чата: <b>{ctz or '—'}</b>\n"
        f"• Эффективная: <b>{tz_key(eff) if eff else 'не установлена'}</b>"
    )


@dp.message(Command("set_chat_timezone"))
async def cmd_set_chat_timezone(m: Message, command: CommandObject):
    """Установка дефолтной TZ для чата (используется, когда у пользователя личная TZ не задана)."""
    if not _owner_guard(m):
        await m.answer(NOT_ALLOWED)
        return

    arg = (command.args or "").strip()
    if not arg:
        await m.answer(
            "Укажи часовой пояс для чата, например:\n"
            "<code>/set_chat_timezone Asia/Yekaterinburg</code>\n"
            "<code>/set_chat_timezone Europe/Moscow</code>"
        )
        return

    try:
        _ = ZoneInfo(arg)
    except Exception:
        await m.answer("Неизвестный часовой пояс. Проверь написание (Region/City).")
        return

    await db.set_chat_timezone(m.chat.id, arg)
    await m.answer(f"✅ Для этого чата установлен часовой пояс: <b>{arg}</b>")


# =========================
# Одноразовые
# =========================
@dp.message(Command("add"))
async def cmd_add(m: Message, state: FSMContext):
    await db.upsert_chat(m.chat.id, m.chat.type, getattr(m.chat, "title", None))
    await state.set_state(AddOnce.waiting_text)
    await m.answer(ASK_TEXT_ONCE)


# Алиас: /add@BotName
@dp.message(F.text.regexp(r"^/add(?:@[\w_]+)?\b"))
async def _alias_add(m: Message, state: FSMContext):
    return await cmd_add(m, state)


@dp.message(AddOnce.waiting_text)
async def add_once_text(m: Message, state: FSMContext):
    await state.update_data(text=m.text.strip())
    await state.set_state(AddOnce.waiting_when)
    await m.answer(ASK_WHEN_ONCE)


@dp.message(AddOnce.waiting_when)
async def add_once_when(m: Message, state: FSMContext):
    data = await state.get_data()
    text = data["text"]

    user_tz = await effective_tz(m.from_user.id, m.chat.id)
    if not user_tz:
        await m.answer(
            "Не могу определить местное время. Укажи свой часовой пояс:\n"
            "<code>/set_timezone America/New_York</code>\n"
            "или админ чата может задать часовой пояс чата:\n"
            "<code>/set_chat_timezone Asia/Yekaterinburg</code>"
        )
        return

    now_local = datetime.now(tz=user_tz)
    try:
        when_local, _ = parse_once_when(m.text, now_local, user_tz)
    except Exception as e:
        await m.answer(f"❗️ {e}")
        return

    remind_at_utc = to_utc(when_local, user_tz)
    try:
        _ = await db.create_once(m.chat.id, m.from_user.id, text, remind_at_utc)
    except Exception as e:
        logging.exception("CREATE once failed")
        await m.answer(f"⚠️ Не удалось сохранить напоминание: {e}")
        return

    await state.clear()

    # Локальное подтверждение — в эффективной TZ
    local_time = format_local_time(remind_at_utc, user_tz_name=tz_key(user_tz), with_tz_abbr=True)
    await m.answer(CONFIRM_ONCE_SAVED.format(when_human=f"{local_time}"))


# =========================
# Повторяющиеся
# =========================
@dp.message(Command("repeat"))
async def cmd_repeat(m: Message, state: FSMContext):
    await db.upsert_chat(m.chat.id, m.chat.type, getattr(m.chat, "title", None))
    await state.set_state(AddCron.waiting_text)
    await m.answer(ASK_TEXT_CRON)


# Фолбэк: /repeat@BotName в группах
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

    user_tz = await effective_tz(m.from_user.id, m.chat.id)
    if not user_tz:
        await m.answer(
            "Не могу определить местное время. Укажи свой часовой пояс:\n"
            "<code>/set_timezone America/New_York</code>\n"
            "или админ чата может задать часовой пояс чата:\n"
            "<code>/set_chat_timezone Asia/Yekaterinburg</code>"
        )
        return

    now_local = datetime.now(tz=user_tz)
    try:
        cron_expr, human_suffix, next_local = parse_repeat_spec(m.text, now_local)
    except Exception as e:
        await m.answer(f"❗️ {e}")
        return

    next_utc = to_utc(next_local, user_tz)
    meta = {"tz": tz_key(user_tz)}  # чтобы scheduler сдвигал в этой TZ
    try:
        _ = await db.create_cron(m.chat.id, m.from_user.id, text, cron_expr, next_utc, category=None, meta=meta)
    except Exception as e:
        logging.exception("CREATE cron failed")
        await m.answer(f"⚠️ Не удалось сохранить повторяющееся напоминание: {e}")
        return

    await state.clear()

    # Покажем пользователю его локальное ближайшее срабатывание
    local_next = format_local_time(next_utc, user_tz_name=tz_key(user_tz), with_tz_abbr=True)
    await m.answer(CONFIRM_CRON_SAVED.format(next_local=local_next))


# =========================
# /list
# =========================
def _row_to_line(row, user_tz_name: str) -> str:
    kind = row["kind"]
    text = row["text"]
    paused = row["paused"]

    if kind == "once":
        when_utc = row["remind_at"]
        when_str = format_local_time(when_utc, user_tz_name=user_tz_name, with_tz_abbr=False)
        return f"• ⏱ {when_str} — “{text}” {'(⏸)' if paused else ''}"
    else:
        nxt_utc = row["next_at"]
        expr = row["cron_expr"]
        nxt_str = format_local_time(nxt_utc, user_tz_name=user_tz_name, with_tz_abbr=False)
        return f"• 🔁 {expr} → {nxt_str} — “{text}” {'(⏸)' if paused else ''}"


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

    eff = await effective_tz(m.from_user.id, m.chat.id)
    user_tz_name = tz_key(eff) if eff else tz_key(DEFAULT_TZ)

    await m.answer(LIST_HEADER)
    for r in rows:
        await m.answer(_row_to_line(r, user_tz_name), reply_markup=_row_buttons(r))


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


# =========================
# Турнирные подписки (МСК)
# =========================
def _tournament_crons_local():
    """
    Напоминания за 5 минут до старта «Быстрого турнира» по МСК.
    Старты: 14:00,16:00,18:00,20:00,22:00,00:00
    Отправляем в 13:55,15:55,17:55,19:55,21:55,23:55 (МСК).
    Возвращаем cron в МЕСТНОМ (DEFAULT_TZ=MSK) времени.
    """
    times = [(13, 55), (15, 55), (17, 55), (19, 55), (21, 55), (23, 55)]
    return [f"{mm} {hh} * * *" for hh, mm in times]


async def _install_tournament_crons_for_chat(chat_id: int, user_id: int):
    # Идемпотентно чистим старые
    if hasattr(db, "delete_tournament_crons"):
        await db.delete_tournament_crons(chat_id)

    now_local = datetime.now(tz=DEFAULT_TZ)  # МСК
    for expr in _tournament_crons_local():
        next_local = croniter(expr, now_local).get_next(datetime)  # в МСК
        next_utc = to_utc(next_local, DEFAULT_TZ)                  # храним в UTC
        text = random.choice(TOURNEY_TEMPLATES)
        meta = {"tz": "Europe/Moscow"}
        await db.create_cron(chat_id, user_id, text, expr, next_utc, category="tournament", meta=meta)


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
    if hasattr(db, "delete_tournament_crons"):
        await db.delete_tournament_crons(m.chat.id)
    await m.answer(SUB_OFF)


# ======= NEW: мгновенный турнирный пинг =======
@dp.message(Command("tourney_now"))
async def cmd_tourney_now(m: Message):
    """
    Ручной мгновенный пинг «Быстрого турнира».
    Не зависит от подписки/кронов — просто публикует одно сообщение по шаблону.
    """
    text = random.choice(TOURNEY_TEMPLATES)
    await m.answer(f"<b>Напоминание:</b>\n{text}")


# =========================
# Запуск
# =========================
async def on_startup():
    # Переключаемся на polling (снимаем вебхук)
    await bot.delete_webhook(drop_pending_updates=False)

    # Меню команд (и в ЛС, и в группах)
    cmds = [
        BotCommand(command="help", description="Список доступных команд"),
        BotCommand(command="add", description="Одноразовое напоминание"),
        BotCommand(command="repeat", description="Повторяющееся напоминание"),
        BotCommand(command="list", description="Список напоминаний"),
        BotCommand(command="set_timezone", description="Установить Ваш часовой пояс"),
        BotCommand(command="my_timezone", description="Показать Ваш часовой пояс"),
        BotCommand(command="set_chat_timezone", description="Часовой пояс по умолчанию для этого чата"),
        BotCommand(command="subscribe_tournaments", description="Включить турнирные напоминания"),
        BotCommand(command="unsubscribe_tournaments", description="Выключить турнирные напоминания"),
        BotCommand(command="tourney_now", description="Сразу турнирное напоминание"),
        BotCommand(command="ping", description="Проверка связи"),
    ]
    await bot.set_my_commands(cmds, scope=BotCommandScopeDefault())
    await bot.set_my_commands(cmds, scope=BotCommandScopeAllGroupChats())

    me = await bot.get_me()
    logging.info("Bot is up: @%s (id=%s) DEFAULT_TZ=%s", me.username, me.id, DEFAULT_TZ.key)

    # Фоновый планировщик
    asyncio.create_task(delivery_loop(bot))


def main():
    if not os.getenv("BOT_TOKEN") or not os.getenv("DATABASE_URL"):
        raise RuntimeError("BOT_TOKEN / DATABASE_URL не заданы")
    dp.startup.register(on_startup)
    dp.run_polling(bot)


if __name__ == "__main__":
    main()
