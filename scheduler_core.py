# scheduler_core.py
import asyncio
import logging
import re
from datetime import datetime, timezone, time
from typing import Optional

import pytz
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore

from db import (
    get_due_once_and_recurring,
    delete_reminder_by_id,
    advance_recurring,
    get_tournament_subscribed_chats,
)
from texts import pick_phrase, TOURNAMENT_VARIANTS

logger = logging.getLogger("remindly")

DEFAULT_TZ = "Europe/Moscow"
TITLE_TOURNAMENT = "Быстрый турнир"

# Напоминания за 5 минут до стартов (MSK): 14, 16, 18, 20, 22, 00
TOURNAMENT_MINUTES = [
    (13, 55),  # 14:00
    (15, 55),  # 16:00
    (17, 55),  # 18:00
    (19, 55),  # 20:00
    (21, 55),  # 22:00
    (23, 55),  # 00:00 следующего дня
]

START_DISPLAY_MAP = {
    (13, 55): (14, 0),
    (15, 55): (16, 0),
    (17, 55): (18, 0),
    (19, 55): (20, 0),
    (21, 55): (22, 0),
    (23, 55): (0, 0),
}


# ---------------------- Турнирные напоминания ---------------------- #
class TournamentScheduler:
    """Планировщик турнирных напоминаний (через APScheduler)."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            executors={"default": AsyncIOExecutor()},
            job_defaults={"misfire_grace_time": 86400},
            timezone=pytz.timezone(DEFAULT_TZ),
        )

    def start(self) -> None:
        self.scheduler.start()
        # Переустанавливаем ежедневные задачи каждые 5 минут + на старте
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            CronTrigger.from_crontab("*/5 * * * *", timezone=self.scheduler.timezone),
        )
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            next_run_time=datetime.now(self.scheduler.timezone),
        )
        logger.info("TournamentScheduler started")

    async def _send_tournament(self, chat_id: int, notify_time: time) -> None:
        start_str = f"{notify_time.hour:02d}:{notify_time.minute:02d}"
        text = pick_phrase(TOURNAMENT_VARIANTS, title=TITLE_TOURNAMENT, time=start_str)
        await self.bot.send_message(chat_id, text)

    def _register_daily_jobs_for_chat(self, chat_id: int, tz_name: Optional[str]) -> None:
        tz = pytz.timezone(tz_name or DEFAULT_TZ)
        for hour, minute in TOURNAMENT_MINUTES:
            job_id = f"tour_{chat_id}_{hour:02d}{minute:02d}"
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
            tz_name = r[1] if len(r) > 1 else None
            self._register_daily_jobs_for_chat(chat_id, tz_name)


# ----------------- Универсальные (once/cron) напоминания ----------------- #
class UniversalReminderScheduler:
    """Фоновый проверяльщик напоминаний (каждые 30 сек)."""

    def __init__(self, bot: Bot, poll_interval_sec: int = 30):
        self.bot = bot
        self.poll_interval_sec = poll_interval_sec
        self._task: Optional[asyncio.Task] = None

    def start(self):
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info(
                "UniversalReminderScheduler started (interval=%ss)", self.poll_interval_sec
            )

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.exception("scheduler tick failed: %s", e)
            await asyncio.sleep(self.poll_interval_sec)

    async def _tick(self):
        # now в UTC — вся логика выборки в БД тоже должна быть в UTC
        now = datetime.now(timezone.utc)

        # Берём все, что просрочено к текущему моменту (с небольшим окном в 60 сек)
        # Окно страхует от погрешностей расписания/сетевых лагов.
        once, cron = get_due_once_and_recurring(window_minutes=1)
        logger.info("[universal] tick %s: due_once=%s, due_cron=%s", now.isoformat(), len(once), len(cron))

        # Одноразовые напоминания
        for r in once:
            try:
                text = r.get("text") or ""
                chat_id = r["chat_id"]
                await self.bot.send_message(chat_id, f"⏰ Напоминание: <b>{text}</b>")
                delete_reminder_by_id(r["id"])
                logger.info("[sent-once] id=%s chat=%s", r["id"], chat_id)
            except Exception as e:
                logger.exception("send once failed (id=%s): %s", r.get("id"), e)

        # Повторяющиеся (cron)
        for r in cron:
            try:
                text = r.get("text") or ""
                chat_id = r["chat_id"]
                footer = self._repeat_footer(r.get("cron_expr") or "")
                await self.bot.send_message(chat_id, f"⏰ Напоминание: <b>{text}</b>\n{footer}")

                # Сдвигаем next_at вперёд согласно cron_expr (делается на стороне БД)
                ce = r.get("cron_expr") or "* * * * *"
                advance_recurring(r["id"], ce)
                logger.info("[sent-cron] id=%s chat=%s next->advance", r["id"], chat_id)
            except Exception as e:
                logger.exception("send cron failed (id=%s): %s", r.get("id"), e)

    # Подписи для повторяющихся
    def _repeat_footer(self, cron_expr: str) -> str:
        # */N * * * *  → каждые N минут
        m = re.match(r"^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$", cron_expr.strip())
        if m:
            n = int(m.group(1))
            return f"🔁 Повтор через {n} мин"

        # X Y * * * → ежедневно HH:MM
        m2 = re.match(r"^(\d+)\s+(\d+)\s+\*\s+\*\s+\*$", cron_expr.strip())
        if m2:
            mm = int(m2.group(1))
            hh = int(m2.group(2))
            return f"🔁 Ежедневно в {hh:02d}:{mm:02d}"

        # Любой другой cron
        return "🔁 Повтор по расписанию"
