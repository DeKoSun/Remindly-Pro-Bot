# FILE: main.py
import logging
import os
from datetime import datetime, time, timedelta, timezone

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command
from aiogram.types import (
    Update,
    BotCommand,
    CallbackQuery,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
)
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

# FSM
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db import (
    # базовые операции
    upsert_chat,
    upsert_telegram_user,           # ← добавили: регистрируем пользователя
    set_tournament_subscription,
    add_reminder,
    get_active_reminders,
    delete_reminder_by_id,
    set_paused,
    # расширенные
    add_recurring_reminder,
    set_user_tz,
    set_quiet_hours,
    has_editor_role,
    grant_role,
    revoke_role,
    list_roles,
    # для inline-действий
    get_reminder_by_id,
    update_reminder_text,
    set_paused_by_id,
    update_remind_at,               # для «+15 минут» и «завтра»
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
logger.setLevel(logging.INFO)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ======================= Хелперы ============================
MSK_TZ = "Europe/Moscow"
TOURNEY_SLOTS = [time(14, 0), time(16, 0), time(18, 0), time(20, 0), time(22, 0), time(0, 0)]

def _msk_now():
    import pytz
    return datetime.now(pytz.timezone(MSK_TZ))

async def _ensure_user_chat(m: types.Message | types.CallbackQuery):
    """
    Гарантируем наличие записей в telegram_users и telegram_chats,
    чтобы не ловить FK-ошибки при вставке в reminders.
    """
    if isinstance(m, types.CallbackQuery):
        user = m.from_user
        chat = m.message.chat
    else:
        user = m.from_user
        chat = m.chat

    try:
        upsert_telegram_user(user.id)
    except Exception as e:
        logger.exception("upsert_telegram_user failed: %s", e)

    try:
        # для приватных чатов записывать не обязательно, но можно — FK у нас на chat_id есть
        upsert_chat(chat_id=chat.id, type_=chat.type, title=getattr(chat, "title", None))
    except Exception as e:
        logger.exception("upsert_chat failed: %s", e)

def _parse_when(text: str) -> datetime | None:
    """
    Поддерживаем:
      - HH:MM (сегодня/завтра)
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

# ======= Карточка напоминания + inline-клавиатура =======
def _reminder_card_text(r: dict) -> str:
    rid = str(r["id"])[:8]
    when = r.get("remind_at") or r.get("next_at") or "—"
    kind = r.get("kind", "once")
    paused = "⏸️" if r.get("paused") else "▶️"
    return (
        f"<b>{r['text']}</b>\n"
        f"ID: <code>{rid}</code>  |  {paused}  |  вид: {kind}\n"
        f"Когда: <code>{when}</code>"
    )

def _reminder_kbd(rid: str, paused: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if paused:
        kb.button(text="▶️ Возобновить", callback_data=f"r:resume:{rid}")
    else:
        kb.button(text="⏸ Пауза", callback_data=f"r:pause:{rid}")
    kb.button(text="✏️ Редактировать", callback_data=f"r:edit:{rid}")
    kb.adjust(2)
    kb.button(text="🔄 +15 мин", callback_data=f"r:shift15:{rid}")
    kb.button(text="📅 Завтра (в это время)", callback_data=f"r:tomorrow:{rid}")
    kb.adjust(2)
    kb.button(text="🗑 Удалить", callback_data=f"r:del:{rid}")
    kb.adjust(2, 1)
    return kb

# ======================= /start /help =======================
@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    await _ensure_user_chat(m)
    await m.answer("Приветствую тебя! Напиши /help, чтобы увидеть мои команды.")

@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    await _ensure_user_chat(m)
    await m.answer(HELP_TEXT, parse_mode=None, disable_web_page_preview=True)

# ======================= Планировщики =======================
_tourney = TournamentScheduler(bot)
_universal = UniversalReminderScheduler(bot)

# ======================= Webhook ===========================
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

@app.on_event("startup")
async def on_startup():
    _tourney.start()
    _universal.start()

    await bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}/{WEBHOOK_SECRET}",
        drop_pending_updates=True,
    )

    # Команды для ЛИЧНЫХ чатов
    await bot.set_my_commands(
        commands=[
            BotCommand(command="help", description="Показать команды"),
            BotCommand(command="add", description="Создать напоминание"),
            BotCommand(command="list", description="Список напоминаний (с кнопками)"),
            BotCommand(command="delete", description="Удалить напоминание"),
            BotCommand(command="pause", description="Пауза напоминания"),
            BotCommand(command="resume", description="Возобновить напоминание"),
            BotCommand(command="add_repeat", description="Повторяющееся напоминание"),
            BotCommand(command="set_tz", description="Установить таймзону"),
            BotCommand(command="quiet", description="Тихие часы"),
            BotCommand(command="cancel", description="Отменить ввод"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )

    # Команды для ГРУПП
    await bot.set_my_commands(
        commands=[
            BotCommand(command="help", description="Показать команды"),
            BotCommand(command="subscribe_tournaments", description="Включить турнирные напоминания"),
            BotCommand(command="unsubscribe_tournaments", description="Выключить турнирные напоминания"),
            BotCommand(command="tourney_now", description="Пробное напоминание турнира"),
            BotCommand(command="schedule", description="Ближайшие старты/время напоминаний"),
            BotCommand(command="add", description="Создать напоминание"),
            BotCommand(command="list", description="Список напоминаний"),
            BotCommand(command="delete", description="Удалить напоминание"),
            BotCommand(command="pause", description="Пауза напоминания"),
            BotCommand(command="resume", description="Возобновить напоминание"),
            BotCommand(command="add_repeat", description="Повторяющееся напоминание"),
            BotCommand(command="set_tz", description="Установить таймзону"),
            BotCommand(command="quiet", description="Тихие часы"),
            BotCommand(command="role", description="Роли чата"),
            BotCommand(command="cancel", description="Отменить ввод"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )

# ======================= Проверка прав ======================
async def _is_admin(message: types.Message) -> bool:
    await _ensure_user_chat(message)
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("Эта команда доступна только в групповых чатах.")
        return False
    member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    if not (member.is_chat_admin() or member.is_chat_creator()):
        await message.answer("Только администраторы чата могут это делать.")
        return False
    return True

async def _is_editor_or_admin(message: types.Message) -> bool:
    await _ensure_user_chat(message)
    if message.chat.type == ChatType.PRIVATE:
        return True
    member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.is_chat_admin() or member.is_chat_creator():
        return True
    return has_editor_role(message.chat.id, message.from_user.id)

# ======================= Турниры ===========================
@dp.message(Command("subscribe_tournaments"))
async def cmd_subscribe_tournaments(m: types.Message):
    if not await _is_admin(m):
        return
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
    display = time(now.hour, (now.minute // 5) * 5)
    await m.answer("🚀 Отправляю пробное напоминание турнира прямо сейчас…")
    await _tourney._send_tournament(m.chat.id, display)

@dp.message(Command("schedule"))
async def cmd_schedule(m: types.Message):
    await _ensure_user_chat(m)
    now = _msk_now()
    today = now.date()
    slots: list[tuple[datetime, datetime]] = []
    for t in TOURNEY_SLOTS:
        dt = now.tzinfo.localize(datetime.combine(today, t))
        if t == time(0, 0):
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

# ============ Универсальные (разовые) напоминания ==========
class AddReminder(StatesGroup):
    waiting_for_text = State()
    waiting_for_time = State()

class EditReminder(StatesGroup):
    waiting_for_new_text = State()

@dp.message(Command("cancel"))
async def cmd_cancel(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    await state.clear()
    await m.answer("Отменено.")

@dp.message(Command("add"))
async def add_start(message: types.Message, state: FSMContext):
    await _ensure_user_chat(message)
    await state.set_state(AddReminder.waiting_for_text)
    await message.answer("📝 Введи текст напоминания:")

@dp.message(AddReminder.waiting_for_text)
async def add_got_text(message: types.Message, state: FSMContext):
    await _ensure_user_chat(message)
    await state.update_data(text=message.text.strip())
    await state.set_state(AddReminder.waiting_for_time)
    await message.answer(
        "⏰ Когда напомнить?\n"
        "Примеры: <code>14:30</code> • <code>завтра 10:00</code> • <code>через 25 минут</code> • <code>через 2 часа</code>"
    )

@dp.message(AddReminder.waiting_for_time)
async def add_got_time(message: types.Message, state: FSMContext):
    await _ensure_user_chat(message)
    when = _parse_when(message.text)
    if not when:
        await message.answer("⚠️ Неверный формат. Примеры: 14:30 • завтра 10:00 • через 25 минут • через 2 часа")
        return
    data = await state.get_data()
    text = data["text"]
    add_reminder(message.from_user.id, message.chat.id, text, when)
    logger.info("created reminder user=%s chat=%s when=%s text=%r",
                message.from_user.id, message.chat.id, when.isoformat(), text)
    await state.clear()
    await message.answer(f"✅ Напоминание создано:\n<b>{text}</b>\n🕒 {when.strftime('%Y-%m-%d %H:%M')} (UTC)")

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    await _ensure_user_chat(message)
    res = get_active_reminders(message.from_user.id)
    items = res.data or []
    if not items:
        await message.answer("Пока нет активных напоминаний.")
        return
    for r in items:
        rid = str(r["id"])
        await message.answer(
            _reminder_card_text(r),
            reply_markup=_reminder_kbd(rid, bool(r.get("paused"))).as_markup(),
        )

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    await _ensure_user_chat(message)
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Укажи id: /delete <id>")
        return
    rid = parts[1]
    delete_reminder_by_id(rid)
    await message.answer(f"🗑 Удалил напоминание <code>{rid}</code>")

@dp.message(Command("pause"))
async def cmd_pause(message: types.Message):
    await _ensure_user_chat(message)
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Укажи id: /pause <id>")
        return
    rid = parts[1]
    set_paused(rid, True)
    await message.answer(f"⏸️ Поставил на паузу <code>{rid}</code>")

@dp.message(Command("resume"))
async def cmd_resume(message: types.Message):
    await _ensure_user_chat(message)
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Укажи id: /resume <id>")
        return
    rid = parts[1]
    set_paused(rid, False)
    await message.answer(f"▶️ Возобновил напоминание <code>{rid}</code>")

# ===== inline-коллбэки: пауза/резюм/удалить/редактировать/перенести ===
@dp.callback_query(lambda c: c.data and c.data.startswith("r:"))
async def cb_router(c: CallbackQuery, state: FSMContext):
    await _ensure_user_chat(c)
    try:
        _, action, rid = c.data.split(":", 2)
    except Exception:
        await c.answer("Некорректные данные.", show_alert=True)
        return

    r = get_reminder_by_id(rid)
    if not r:
        await c.answer("Напоминание не найдено (возможно, уже удалено).", show_alert=True)
        try:
            await c.message.delete()
        except Exception:
            pass
        return

    if action == "pause":
        set_paused_by_id(rid, True)
        r["paused"] = True
        await c.message.edit_text(_reminder_card_text(r), reply_markup=_reminder_kbd(rid, True).as_markup())
        await c.answer("Поставлено на паузу.")
        return

    if action == "resume":
        set_paused_by_id(rid, False)
        r["paused"] = False
        await c.message.edit_text(_reminder_card_text(r), reply_markup=_reminder_kbd(rid, False).as_markup())
        await c.answer("Возобновлено.")
        return

    if action == "del":
        delete_reminder_by_id(rid)
        await c.message.edit_text("🗑 Напоминание удалено.")
        await c.answer("Удалено.")
        return

    if action == "edit":
        await state.update_data(edit_rid=rid)
        await state.set_state(EditReminder.waiting_for_new_text)
        await c.answer()
        await c.message.reply("✏️ Введи новый текст для напоминания (или /cancel):")
        return

    if action == "shift15":
        if r.get("kind") not in (None, "once"):
            await c.answer("Это действие доступно только для разовых напоминаний.", show_alert=True)
            return
        ra = r.get("remind_at")
        if not ra:
            await c.answer("Не удалось определить время напоминания.", show_alert=True)
            return
        try:
            ra_dt = datetime.fromisoformat(ra.replace("Z", "+00:00"))
        except Exception:
            await c.answer("Неверный формат времени напоминания.", show_alert=True)
            return
        new_dt = ra_dt + timedelta(minutes=15)
        update_remind_at(rid, new_dt.astimezone(timezone.utc))
        r["remind_at"] = new_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        r["paused"] = False
        await c.message.edit_text(_reminder_card_text(r), reply_markup=_reminder_kbd(rid, False).as_markup())
        await c.answer("Перенесено на +15 минут.")
        return

    if action == "tomorrow":
        if r.get("kind") not in (None, "once"):
            await c.answer("Это действие доступно только для разовых напоминаний.", show_alert=True)
            return
        ra = r.get("remind_at")
        if not ra:
            await c.answer("Не удалось определить время напоминания.", show_alert=True)
            return
        try:
            ra_dt = datetime.fromisoformat(ra.replace("Z", "+00:00"))
        except Exception:
            await c.answer("Неверный формат времени напоминания.", show_alert=True)
            return
        new_dt = ra_dt + timedelta(days=1)
        update_remind_at(rid, new_dt.astimezone(timezone.utc))
        r["remind_at"] = new_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        r["paused"] = False
        await c.message.edit_text(_reminder_card_text(r), reply_markup=_reminder_kbd(rid, False).as_markup())
        await c.answer("Перенесено на завтра.")
        return

    await c.answer("Неизвестное действие.", show_alert=True)

@dp.message(EditReminder.waiting_for_new_text)
async def edit_set_text(m: types.Message, state: FSMContext):
    await _ensure_user_chat(m)
    if m.text.strip().lower() == "/cancel":
        await state.clear()
        await m.answer("Редактирование отменено.")
        return
    data = await state.get_data()
    rid = data.get("edit_rid")
    if not rid:
        await state.clear()
        await m.answer("Что-то пошло не так. Попробуй ещё раз: /list → ✏️ Редактировать.")
        return
    new_text = m.text.strip()
    update_reminder_text(rid, new_text)
    await state.clear()

    r = get_reminder_by_id(rid)
    if r:
        await m.answer("✅ Текст обновлен.")
        await m.answer(
            _reminder_card_text(r),
            reply_markup=_reminder_kbd(rid, bool(r.get("paused"))).as_markup(),
        )
    else:
        await m.answer("✅ Текст обновлен. (карточка не найдена)")

# ========= Повторяющиеся напоминания (cron) =========
@dp.message(Command("add_repeat"))
async def cmd_add_repeat(m: types.Message):
    await _ensure_user_chat(m)
    if not await _is_editor_or_admin(m):
        await m.answer("Недостаточно прав для создания повторяющихся напоминаний.")
        return

    # /add_repeat <тип> <HH:MM> Текст
    # тип: daily | weekdays | sunday | cron
    parts = m.text.strip().split()
    if len(parts) < 3:
        await m.answer(
            "Примеры:\n"
            "/add_repeat daily 10:00 Собрание\n"
            "/add_repeat weekdays 09:45 Стендап\n"
            "/add_repeat sunday 20:00 Отчёт\n"
            "/add_repeat cron \"*/15 * * * *\" Пульс-чек"
        )
        return

    mode = parts[1].lower()
    if mode == "cron":
        cron_expr = parts[2].strip('"').strip("'")
        text = " ".join(parts[3:]).strip()
    else:
        try:
            hh, mm = map(int, parts[2].split(":"))
        except Exception:
            await m.answer("Формат времени: HH:MM")
            return
        if mode == "daily":
            cron_expr = f"{mm} {hh} * * *"
        elif mode == "weekdays":
            cron_expr = f"{mm} {hh} 1-5 * *"
        elif mode == "sunday":
            cron_expr = f"{mm} {hh} * * 0"
        else:
            await m.answer("Тип должен быть: daily | weekdays | sunday | cron")
            return
        text = " ".join(parts[3:]).strip()

    if not text:
        await m.answer("Добавь текст напоминания.")
        return

    add_recurring_reminder(m.from_user.id, m.chat.id, text, cron_expr)
    logger.info("created recurring reminder user=%s chat=%s cron=%s text=%r",
                m.from_user.id, m.chat.id, cron_expr, text)
    await m.answer(f"✅ Создал повторяющееся напоминание:\n<b>{text}</b>\nCRON: <code>{cron_expr}</code>")

# ================= Таймзона и «тихие часы» =================
@dp.message(Command("set_tz"))
async def cmd_set_tz(m: types.Message):
    await _ensure_user_chat(m)
    parts = m.text.strip().split()
    if len(parts) < 2:
        await m.answer("Укажи таймзону, например: /set_tz Europe/Moscow")
        return
    set_user_tz(m.from_user.id, parts[1])
    await m.answer(f"Таймзона обновлена: {parts[1]}")

@dp.message(Command("quiet"))
async def cmd_quiet(m: types.Message):
    await _ensure_user_chat(m)
    parts = m.text.strip().split()
    if len(parts) < 2:
        await m.answer("Формат: /quiet HH-HH  или /quiet off")
        return
    arg = parts[1].lower()
    if arg == "off":
        set_quiet_hours(m.from_user.id, None, None)
        await m.answer("Тихие часы отключены.")
        return
    try:
        qf, qt = arg.split("-")
        set_quiet_hours(m.from_user.id, int(qf), int(qt))
        await m.answer(f"Тихие часы установлены: {qf}:00–{qt}:00")
    except Exception:
        await m.answer("Формат: /quiet 23-8  или /quiet off")

# ======================= Роли в чате =======================
@dp.message(Command("role"))
async def cmd_role(m: types.Message):
    await _ensure_user_chat(m)
    # /role list
    # /role grant <user_id> editor
    # /role revoke <user_id>
    parts = m.text.strip().split()
    if len(parts) == 2 and parts[1].lower() == "list":
        rows = list_roles(m.chat.id)
        if not rows:
            await m.answer("В этом чате нет дополнительных ролей.")
            return
        lines = ["Роли чата:"]
        for r in rows:
            lines.append(f"• user_id={r['user_id']} → {r['role']}")
        await m.answer("\n".join(lines))
        return

    if len(parts) >= 3 and parts[1].lower() in ("grant", "revoke"):
        if not await _is_admin(m):
            return
        try:
            target_id = int(parts[2].replace("@", ""))
        except ValueError:
            await m.answer("Пока поддерживаю формат: /role grant <user_id> editor")
            return
        if parts[1].lower() == "grant":
            role = parts[3] if len(parts) > 3 else "editor"
            grant_role(m.chat.id, target_id, role)
            await m.answer(f"Выдал роль {role} пользователю {target_id}.")
        else:
            revoke_role(m.chat.id, target_id)
            await m.answer(f"Снял роли с пользователя {target_id}.")
        return

    await m.answer("Форматы:\n/role list\n/role grant <user_id> editor\n/role revoke <user_id>")

# ==================== DEBUG-команды (скрытые в меню) ====================
@dp.message(Command("dbg_insert"))
async def dbg_insert(m: types.Message):
    """
    /dbg_insert [минуты] [текст...]
    По умолчанию: создаёт напоминание через 1 минуту с текстом "DBG ping".
    """
    await _ensure_user_chat(m)

    parts = m.text.strip().split(maxsplit=2)
    minutes = 1
    text = "DBG ping"
    if len(parts) >= 2:
        try:
            minutes = int(parts[1])
        except ValueError:
            pass
    if len(parts) == 3:
        text = parts[2].strip()

    when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    try:
        add_reminder(m.from_user.id, m.chat.id, text, when)
        logger.info(
            "DBG: inserted reminder user=%s chat=%s when=%s text=%r",
            m.from_user.id, m.chat.id, when.isoformat(), text
        )
        await m.answer(
            f"✅ DBG: вставил напоминание\n"
            f"<b>{text}</b>\n"
            f"⏰ {when.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"(user_id={m.from_user.id}, chat_id={m.chat.id})"
        )
    except Exception as e:
        logger.exception("DBG insert failed")
        await m.answer(f"❌ DBG insert failed: <code>{e}</code>")


@dp.message(Command("dbg_list"))
async def dbg_list(m: types.Message):
    """
    /dbg_list — покажет до 10 активных напоминаний текущего пользователя,
    чтобы убедиться, что записи реально попадают в БД.
    """
    await _ensure_user_chat(m)
    try:
        res = get_active_reminders(m.from_user.id)
        items = res.data or []  # у тебя get_active_reminders возвращает .data
        if not items:
            await m.answer("DBG: активных напоминаний не найдено.")
            return
        lines = ["DBG: активные напоминания:"]
        for r in items[:10]:
            rid = str(r["id"])[:8]
            when = r.get("remind_at") or r.get("next_at")
            lines.append(f"• {rid} | {when} | {r.get('kind','once')} | {r['text']}")
        await m.answer("\n".join(lines))
    except Exception as e:
        logger.exception("DBG list failed")
        await m.answer(f"❌ DBG list failed: <code>{e}</code>")


@dp.message(Command("dbg_who"))
async def dbg_who(m: types.Message):
    """
    /dbg_who — быстрый вывод идентификаторов и факт регистрации в родительских таблицах.
    Удобно, чтобы исключить ошибки внешних ключей.
    """
    await _ensure_user_chat(m)
    await m.answer(
        "DBG who:\n"
        f"user_id = <code>{m.from_user.id}</code>\n"
        f"chat_id = <code>{m.chat.id}</code>\n"
        "Записи в telegram_users/telegram_chats должны быть созданы автоматически."
    )
