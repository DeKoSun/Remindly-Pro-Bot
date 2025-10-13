import asyncio
import logging
import os
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from db import Db
from scheduler_core import SchedulerCore

DEBUG = os.getenv("DEBUG", "true").lower() == "true"
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "UTC")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- helpers ----------

def render_list_item(idx: int, r: dict) -> str:
    when = r.get("next_at") or r.get("remind_at")
    when_txt = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M (UTC)") if when else "—"
    tag = "♻️" if r["kind"] == "cron" else "⏰"
    return f"{idx}. {tag} {r['text']} — {when_txt}"

def row_pause_delete(rem_id: str, paused: bool) -> InlineKeyboardMarkup:
    pause_label = "▶️ Возобновить" if paused else "⏸️ Пауза"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=pause_label, callback_data=f"rem:toggle:{rem_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rem:delete:{rem_id}")
    ]])

# ---------- commands ----------

@dp.message(Command("ping"))
async def cmd_ping(m: Message):
    await m.answer("✅ pong")

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "📖 Команды:\n"
        "/add — одноразовое\n"
        "/add_repeat — повторяющееся (cron)\n"
        "/list — список\n"
        "/pause <№> — пауза\n"
        "/resume <№> — возобновить\n"
        "/delete <№> — удалить\n"
        "\nВремя хранится в UTC."
    )

@dp.message(Command("add"))
async def cmd_add(m: Message):
    await m.answer("✍️ Введи текст напоминания:")
    dp.fsm.storage = {"state": ("add_text", m.chat.id, m.from_user.id)}

@dp.message(Command("add_repeat"))
async def cmd_add_repeat(m: Message):
    await m.answer("✍️ Введи текст повторяющегося напоминания:")
    dp.fsm.storage = {"state": ("addr_text", m.chat.id, m.from_user.id)}

@dp.message(Command("list"))
async def cmd_list(m: Message):
    items = await Db.list_by_chat(m.chat.id)
    if not items:
        await m.answer("ℹ️ Пока нет напоминаний.")
        return
    lines = [render_list_item(i+1, r) for i, r in enumerate(items)]
    await m.answer(
        "🗒 Напоминания этого чата:\n" + "\n".join(lines)
    )
    # Отдельно отправим карточки с кнопками
    for r in items:
        txt = f"{'♻️' if r['kind']=='cron' else '⏰'} <b>{r['text']}</b>\n"
        when = r.get("next_at") or r.get("remind_at")
        if when:
            txt += f"🕒 {when.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M (%Z)')}"
        await m.answer(txt, reply_markup=row_pause_delete(r["id"], r["paused"]))

@dp.message(F.text & (~F.text.startswith("/")))
async def step_flow(m: Message):
    st = getattr(dp, "fsm", None)
    if not st or "state" not in st.storage:
        return
    state, chat_id, user_id = st.storage["state"]

    if state == "add_text" and m.chat.id == chat_id and m.from_user.id == user_id:
        dp.fsm.storage["pending_text"] = m.text
        await m.answer("⏰ Когда напомнить?\nНапример: 14:30 • завтра 10:00 • через 25 минут • +15")
        dp.fsm.storage["state"] = ("add_when", chat_id, user_id)
        return

    if state == "add_when" and m.chat.id == chat_id and m.from_user.id == user_id:
        text = dp.fsm.storage.get("pending_text", "").strip()
        when_dt = await SchedulerCore.parse_when(m.text, tz_name=DEFAULT_TZ)
        if not when_dt:
            await m.answer("❌ Не понял время. Попробуй ещё раз.")
            return
        rid = await Db.add_once(
            user_id=user_id, chat_id=chat_id, text=text, remind_at=when_dt
        )
        await m.answer(f"✅ Напоминание создано:\n<b>{text}</b>\n🕒 {when_dt.strftime('%Y-%m-%d %H:%M (%Z)')}")
        dp.fsm.storage.clear()
        return

    if state == "addr_text" and m.chat.id == chat_id and m.from_user.id == user_id:
        dp.fsm.storage["pending_text"] = m.text
        await m.answer(
            "⏱ Какое расписание?\n• каждую минуту\n• ежедневно HH:MM\n• HH:MM (каждый день)\n• cron: * * * * *"
        )
        dp.fsm.storage["state"] = ("addr_rule", chat_id, user_id)
        return

    if state == "addr_rule" and m.chat.id == chat_id and m.from_user.id == user_id:
        text = dp.fsm.storage.get("pending_text", "").strip()
        cron_expr, next_at = await SchedulerCore.parse_repeat(m.text, tz_name=DEFAULT_TZ)
        if not cron_expr or not next_at:
            await m.answer("❌ Не удалось создать повторяющееся. Проверь формат (можно cron: EXPR).")
            return
        rid = await Db.add_cron(
            user_id=user_id, chat_id=chat_id, text=text, cron_expr=cron_expr, next_at=next_at
        )
        await m.answer(
            "✅ Повторяющееся напоминание создано:\n"
            f"<b>{text}</b>\n"
            f"🧭 CRON: <code>{cron_expr}</code>\n"
            f"🕒 Ближайшее: {next_at.strftime('%Y-%m-%d %H:%M (%Z)')}"
        )
        dp.fsm.storage.clear()
        return

# ---------- callbacks ----------

@dp.callback_query(F.data.startswith("rem:toggle:"))
async def cb_toggle(q: CallbackQuery):
    rid = q.data.split(":", 2)[2]
    ok, paused = await Db.toggle_pause(rid)
    if not ok:
        await q.answer("Ошибка", show_alert=True)
        return
    await q.message.edit_reply_markup(reply_markup=row_pause_delete(rid, paused))
    await q.answer("Готово")

@dp.callback_query(F.data.startswith("rem:delete:"))
async def cb_delete(q: CallbackQuery):
    rid = q.data.split(":", 2)[2]
    await Db.delete(rid)
    await q.message.edit_text("🗑 Удалено")
    await q.answer("Готово")

# ---------- bootstrap ----------

async def main():
    await Db.init_pool(os.environ["DATABASE_URL"])
    scheduler = SchedulerCore(bot=bot, tz_name=DEFAULT_TZ, interval_seconds=30, debug=DEBUG)
    await scheduler.start()
    log.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
