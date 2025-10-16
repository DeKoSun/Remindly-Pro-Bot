# RemindlyProBot

Телеграм-бот напоминаний: турнирные «Быстрый турнир» (МСК) + универсальные одноразовые/повторяющиеся, хранение в PostgreSQL (Supabase), фоновый планировщик без внешних очередей. Готов к деплою на Railway.

## 1) Подготовка
- Python 3.12+
- Создай бота у @BotFather → получи BOT_TOKEN
- Создай БД (Supabase/Postgres) → возьми DATABASE_URL
- Клонируй репозиторий (или создай папку) → помести файлы
- Скопируй `.env.sample` → `.env` и заполни значения

## 2) База данных
Выполни `schema.sql` в своей БД (через Supabase SQL Editor или psql).

## 3) Локальный запуск
```bash
python -m venv .venv
. .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)  # Windows: set переменные вручную
python main.py
