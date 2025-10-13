# scheduler_core.py
import asyncio
import logging
import re
from datetime import datetime, timezone, time
from typing import Optional

import pytz
from aiogram import Bot
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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

# Для текста «во сколько старт»
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
            job_defaults={"misfire_grace_time": 24 * 3600},
            timezone=pytz.timezone(DEFAULT_TZ),
        )

    def start(self) -> None:
        # Запускаем APScheduler и ставим периодическую задачу пересборки расписания
        self.scheduler.start()
        # Перебинд — каждые 5 минут
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            CronTrigger.from_crontab("*/5 * * * *", timezone=self.scheduler.timezone),
            id="tour_ensure_jobs",
            replace_existing=True,
        )
        # И сразу один прогон на запуске
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            next_run_time=datetime.now(self.scheduler.timezone),
            id="tour_ensure_jobs_boot",
            replace_existing=True,
        )
        logger.info("TournamentScheduler started")

    def stop(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass

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
            logger.debug("Registered tour job %s in tz=%s", job_id, tz.key)

    def _ensure_tournament_jobs(self) -> None:
        rows = get_tournament_subscribed_chats()  # [(chat_id, tz_name?)]
        for r in rows:
            chat_id = r[0]
            tz_name = r[1] if len(r) > 1 else None
            self._register_daily_jobs_for_chat(chat_id, tz_name)


# ----------------- Универсальные (once/cron) напоминания ----------------- #
class UniversalReminderScheduler:
    """Фоновый поллер напоминаний (every N seconds). Работает строго в UTC."""

    def __init__(self, bot: Bot, poll_interval_sec: int = 30):
        self.bot = bot
        self.poll_interval_sec = max(5, int(poll_interval_sec))  # защита от слишком маленьких значений
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def start(self):
        if self._task and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="universal-reminders")
        logger.info("UniversalReminderScheduler started (interval=%ss)", self.poll_interval_sec)

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass

    async def _loop(self):
        # Первый «мгновенный» тик — чтобы не ждать интервал
        try:
            await self._tick()
        except Exception as e:
            logger.exception("first scheduler tick failed: %s", e)

        while not self._stopping:
            try:
                await self._tick()
            except Exception as e:
                logger.exception("scheduler tick failed: %s", e)
            # Небольшой джиттер, чтобы не «липнуть» на ровные секунды
            await asyncio.sleep(self.poll_interval_sec)

    async def _tick(self):
        # now в UTC — вся логика выборки в БД должна ориентироваться на это же
        now = datetime.now(timezone.utc)

        # Берём все, что просрочено к текущему моменту (с окном 60 секунд)
        once_items, cron_items = get_due_once_and_recurring(window_minutes=1)
        logger.info(
            "[universal] %s due_once=%s due_cron=%s",
            now.isoformat(),
            len(once_items),
            len(cron_items),
        )

        # --- Одноразовые ---
        for r in once_items:
            rid = r.get("id")
            chat_id = r.get("chat_id")
            text = (r.get("text") or "").strip()
            try:
                if not chat_id:
                    logger.warning("once reminder without chat_id, id=%s", rid)
                    continue
                msg_txt = f"⏰ Напоминание: <b>{text}</b>" if text else "⏰ Напоминание!"
                await self.bot.send_message(chat_id, msg_txt)
                delete_reminder_by_id(rid)
                logger.info("[sent-once] id=%s chat=%s", rid, chat_id)
            except Exception as e:
                logger.exception("send once failed (id=%s): %s", rid, e)

        # --- Повторяющиеся (cron) ---
        for r in cron_items:
            rid = r.get("id")
            chat_id = r.get("chat_id")
            text = (r.get("text") or "").strip()
            cron_expr = (r.get("cron_expr") or "").strip() or "* * * * *"
            try:
                if not chat_id:
                    logger.warning("cron reminder without chat_id, id=%s", rid)
                    continue

                footer = self._repeat_footer(cron_expr)
                msg_txt = f"⏰ Напоминание: <b>{text}</b>\n{footer}" if text else f"⏰ Напоминание!\n{footer}"
                await self.bot.send_message(chat_id, msg_txt)

                # Сдвигаем next_at вперёд согласно cron_expr (на стороне БД)
                advance_recurring(rid, cron_expr)
                logger.info("[sent-cron] id=%s chat=%s advanced", rid, chat_id)
            except Exception as e:
                logger.exception("send cron failed (id=%s): %s", rid, e)

    # Подпись для повторяющихся
    def _repeat_footer(self, cron_expr: str) -> str:
        # */N * * * *  → каждые N минут
        m = re.match(r"^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$", cron_expr)
        if m:
            n = int(m.group(1))
            return f"🔁 Повтор через {n} мин"

        # M H * * * → ежедневно HH:MM
        m2 = re.match(r"^(\d+)\s+(\d+)\s+\*\s+\*\s+\*$", cron_expr)
        if m2:
            mm = int(m2.group(1))
            hh = int(m2.group(2))
            return f"🔁 Ежедневно в {hh:02d}:{mm:02d}"

        # Любой другой cron
        return "🔁 Повтор по расписанию"


__all__ = ["TournamentScheduler", "UniversalReminderScheduler"]
