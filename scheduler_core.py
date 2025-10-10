# FILE: scheduler_core.py
import os
from datetime import datetime, time
import pytz

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore

from aiogram import Bot
from texts import pick_phrase, TOURNAMENT_VARIANTS
from db import get_tournament_subscribed_chats

# По умолчанию МСК (можно переопределить через переменную окружения)
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Moscow")

TITLE_TOURNAMENT = "Быстрый турнир"

# Напоминания за 5 минут до старта турниров: 14, 16, 18, 20, 22, 00 (MSK)
TOURNAMENT_MINUTES = [
    (13, 55),  # 14:00
    (15, 55),  # 16:00
    (17, 55),  # 18:00
    (19, 55),  # 20:00
    (21, 55),  # 22:00
    (23, 55),  # 00:00 (следующего дня)
]

# Для текста в сообщении показываем реальное время старта
START_DISPLAY_MAP = {
    (13, 55): (14, 0),
    (15, 55): (16, 0),
    (17, 55): (18, 0),
    (19, 55): (20, 0),
    (21, 55): (22, 0),
    (23, 55): (0, 0),   # полночь
}


class TournamentScheduler:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            executors={"default": AsyncIOExecutor()},
            job_defaults={"misfire_grace_time": 86400},  # Ловим "пропуски" за сутки
            timezone=pytz.timezone(DEFAULT_TZ),
        )

    def start(self) -> None:
        self.scheduler.start()
        # Периодически гарантируем, что для подписанных чатов стоят ежедневные джобы
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            CronTrigger.from_crontab("*/5 * * * *", timezone=self.scheduler.timezone),
        )
        # И сразу при старте
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            next_run_time=datetime.now(self.scheduler.timezone),
        )

    async def _send_tournament(self, chat_id: int, notify_time: time) -> None:
        # notify_time — время СТАРТА турнира (для текста)
        start_str = notify_time.strftime("%H:%M")
        text = pick_phrase(TOURNAMENT_VARIANTS, title=TITLE_TOURNAMENT, time=start_str)
        await self.bot.send_message(chat_id, text)

    def _register_daily_jobs_for_chat(self, chat_id: int, tz_name: str | None) -> None:
        tz = pytz.timezone(tz_name or DEFAULT_TZ)
        for hour, minute in TOURNAMENT_MINUTES:
            job_id = f"tour_{chat_id}_{hour:02d}{minute:02d}"
            # Пересоздаём, чтобы избежать дублей и учесть смену TZ
            old = self.scheduler.get_job(job_id)
            if old:
                old.remove()
            self.scheduler.add_job(
                self._send_tournament,
                CronTrigger(hour=hour, minute=minute, timezone=tz),
                id=job_id,
                args=[chat_id, time(*START_DISPLAY_MAP[(hour, minute)])],
                replace_existing=True,
            )

    def _ensure_tournament_jobs(self) -> None:
        rows = get_tournament_subscribed_chats()
        for r in rows:
            chat_id = r[0]
            tz_name = r[1]
            self._register_daily_jobs_for_chat(chat_id, tz_name)
