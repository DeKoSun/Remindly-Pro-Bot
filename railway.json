# main.py  — aiogram v2.25.2
import os
import json
import logging

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ENV ----------
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://github-production-83c6.up.railway.app/")  # <-- твой публичный HTTPS

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN/BOT_TOKEN is not set")

# ---------- BOT/DP ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ---------- Supabase (опционально) ----------
supabase = None
try:
    if SUPABASE_URL and SUPABASE_KEY:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("Supabase client initialized")
    else:
        logging.warning("SUPABASE_URL/SUPABASE_KEY не заданы — сохранение в БД отключено.")
except Exception:
    logging.exception("Не удалось инициализировать Supabase")

# ---------- Клавиатура с WebApp ----------
def register_kb() -> ReplyKeyboardMarkup:
    url = WEBAPP_URL.strip()
    if not url.startswith("http"):
        url = "https://" + url  # страховка, если забудем схему
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton(text="📝 Заполнить форму", web_app=WebAppInfo(url=url)))
    return kb

# ---------- Команды ----------
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Нажми кнопку ниже, чтобы открыть форму регистрации.",
        reply_markup=register_kb()
    )

@dp.message_handler(commands=["register"])
async def cmd_register(message: types.Message):
    await message.answer(
        "Нажми кнопку ниже, чтобы открыть форму регистрации:",
        reply_markup=register_kb()
    )

# ---------- Приём данных из WebApp ----------
@dp.message_handler(content_types=types.ContentType.WEB_APP_DATA)
async def handle_webapp(message: types.Message):
    try:
        raw = message.web_app_data.data or "{}"
        data = json.loads(raw)
        logging.info(f"WebApp data from {message.from_user.id}: {data}")

        # Сохраняем в Supabase, если включено
        if supabase:
            payload = {
                "telegram_id": str(message.from_user.id),
                "nickname": data.get("nickname"),
                "telegram": data.get("telegram"),
                "expectations": data.get("expectations"),
                "play_other": data.get("play_other"),
                "clan_life": data.get("clan_life"),
                # Если у тебя колонка JSON/array — оставляй как есть, иначе сериализуй строкой:
                "decks": data.get("decks"),
            }
            res = supabase.table("players").insert(payload).execute()
            logging.info(f"Supabase insert result: {res}")

        await message.answer("✅ Регистрация получена! Спасибо.")
    except Exception:
        logging.exception("Ошибка в обработчике WEB_APP_DATA")
        await message.answer("❌ Произошла ошибка. Попробуй ещё раз позже.")

# ---------- RUN ----------
if __name__ == "__main__":
    logging.info("Bot starting…")
    executor.start_polling(dp, skip_updates=True)
