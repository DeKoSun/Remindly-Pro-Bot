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
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from croniter import croniter

# ==== локальные модули ====
from scheduler_core import TournamentScheduler, UniversalReminderScheduler
from texts import HELP_TEXT
from db import (
    upsert_chat,
    upsert_telegram_user,
    get_active_reminders,
    add_reminder,              # once
    add_recurring_reminder,    # cron
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

# ================== ХЕНДЛЕРЫ ==================
@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    await _ensure_user_chat(m)
    await m.answer("Привет! Напиши /help, чтобы увидеть мои команды.")

@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await _ensure_user_chat(m)
    await m.answer(HELP_TEXT, disable_web_page_preview=True)

@dp.message(Command("ping")))
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

    # 1) Парсим в CRON и показываем, что получилось
    cron_expr = _parse_repeat_to_cron(sched_raw)
    is_valid = False
    try:
        is_valid = croniter.is_valid(cron_expr)
    except Exception as e:
        # чтобы видеть редкие ошибки в croniter
        return await m.answer(f"❌ croniter error: <code>{e}</code>\nexpr: <code>{cron_expr}</code>")

    if not is_valid:
        return await m.answer(
            "❌ Неверное расписание.\n"
            f"expr: <code>{cron_expr}</code>\n"
            "Попробуй: «каждую минуту», «ежедневно HH:MM», «HH:MM» или «cron: */5 * * * *»."
        )

    # 2) Считаем next_at и пробуем вставить в БД. В ответ отдадим ПОЛНУЮ причину сбоя
    try:
        next_at = _cron_next_utc(cron_expr)
    except Exception as e:
        return await m.answer(f"❌ Не удалось вычислить ближайшее время: <code>{e}</code>\nexpr: <code>{cron_expr}</code>")

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
        # здесь хотим видеть реальную SQL-ошибку/ограничение/NULL-поле
        return await m.answer(
            "❌ DB insert failed.\n"
            f"reason: <code>{e}</code>\n"
            f"expr: <code>{cron_expr}</code>\nnext_at: <code>{_fmt_utc(next_at)}</code>"
        )

# ---------- Управление ----------
@dp.message(Command("list"))
async def cmd_list(m: types.Message):
    await _ensure_user_chat(m)
    items = get_active_reminders(m.from_user.id)
    if not items:
        return await m.answer("Пока нет активных напоминаний.")
    lines = []
    for r in items:
        rid = r["id"] if isinstance(r, dict) else r[0]
        text = r["text"] if isinstance(r, dict) else r[1]
        kind = (r.get("kind") if isinstance(r, dict) else None) or "once"
        remind_at = r.get("remind_at") if isinstance(r, dict) else None
        next_at = r.get("next_at") if isinstance(r, dict) else None

        when_dt = remind_at if kind == "once" else next_at
        when_str = _fmt_utc(when_dt)

        # ID показываем только здесь — чтобы было чем управлять:
        lines.append(f"• <code>{rid}</code> — {text} — {when_str} — {('🔁' if kind!='once' else '•')}")
    await m.answer("🔔 Активные напоминания:\n" + "\n".join(lines))

@dp.message(Command("delete"))
async def cmd_delete(m: types.Message):
    await _ensure_user_chat(m)
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
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
    if len(parts) < 2 or not parts[1].strip():
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
    if len(parts) < 2 or not parts[1].strip():
        return await m.answer("Укажи ID: /resume <id>")
    rid = parts[1].strip()
    try:
        set_paused(reminder_id=rid, chat_id=m.chat.id, paused=False)
        await m.answer("▶️ Напоминание возобновлено.")
    except Exception as e:
        logger.exception("resume error: %s", e)
        await m.answer("❌ Не удалось возобновить напоминание.")

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
            BotCommand(command="list", description="Список напоминаний"),
            BotCommand(command="delete", description="Удалить по ID"),
            BotCommand(command="pause", description="Пауза по ID"),
            BotCommand(command="resume", description="Возобновить по ID"),
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
            BotCommand(command="list", description="Список напоминаний"),
            BotCommand(command="delete", description="Удалить по ID"),
            BotCommand(command="pause", description="Пауза по ID"),
            BotCommand(command="resume", description="Возобновить по ID"),
            BotCommand(command="subscribe_tournaments", description="Включить турнирные напоминания"),
            BotCommand(command="unsubscribe_tournaments", description="Выключить турнирные напоминания"),
            BotCommand(command="tourney_now", description="Пробное турнирное уведомление"),
            BotCommand(command="ping", description="Проверка состояния"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )

    logger.info("Webhook & commands registered. Public URL: %s", PUBLIC_BASE_URL)
