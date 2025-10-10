# FILE: scheduler_core.py
import os
import asyncio
import logging
from datetime import datetime, time, timezone, timedelta

import pytz
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram import Bot

from texts import pick_phrase, TOURNAMENT_VARIANTS
from db import (
    get_tournament_subscribed_chats,
    supabase,
    get_due_once_and_recurring,
    advance_recurring,
    get_user_prefs,
)

logger = logging.getLogger("remindly")

DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Moscow")
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


class TournamentScheduler:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            executors={"default": AsyncIOExecutor()},
            job_defaults={"misfire_grace_time": 86400},  # 24h
            timezone=pytz.timezone(DEFAULT_TZ),
        )

    def start(self) -> None:
        self.scheduler.start()
        # раз в 5 минут сверяем список подписанных чатов
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            CronTrigger.from_crontab("*/5 * * * *", timezone=self.scheduler.timezone),
            id="ensure_tourney_jobs",
            replace_existing=True,
        )
        # и сразу при старте
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            next_run_time=datetime.now(self.scheduler.timezone),
            id="ensure_tourney_jobs_boot",
            replace_existing=True,
        )

    async def _send_tournament(self, chat_id: int, notify_time: time) -> None:
        start_str = notify_time.strftime("%H:%M")
        text = pick_phrase(TOURNAMENT_VARIANTS, title=TITLE_TOURNAMENT, time=start_str)
        await self.bot.send_message(chat_id, text)

    def _register_daily_jobs_for_chat(self, chat_id: int, tz_name: str | None) -> None:
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
            tz_name = r[1]
            self._register_daily_jobs_for_chat(chat_id, tz_name)


class UniversalReminderScheduler:
    """Пуллер универсальных напоминаний из таблицы Supabase `reminders`."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._task: asyncio.Task | None = None
        # «догоняющая доставка»: сколько минут назад ещё считаем напоминание актуальным
        self.late_delivery_min = int(os.getenv("LATE_DELIVERY_MIN", "10"))

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._check_reminders())

    async def _check_reminders(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            window_start = now - timedelta(minutes=self.late_delivery_min)

            # забираем due once + due cron по нашему окну
            once, recur = get_due_once_and_recurring(window_start.isoformat(), now.isoformat())
            due: list[tuple[str, dict]] = []
            due.extend(("once", r) for r in (once or []))
            due.extend(("cron", r) for r in (recur or []))

            for kind, r in due:
                try:
                    # учёт пользовательских настроек (таймзона/тихие часы)
                    prefs = get_user_prefs(r["user_id"])
                    tz_name = (prefs.get("tz_name") or "Europe/Moscow") if prefs else "Europe/Moscow"
                    quiet_from = prefs.get("quiet_from") if prefs else None
                    quiet_to = prefs.get("quiet_to") if prefs else None

                    if quiet_from is not None and quiet_to is not None:
                        user_tz = pytz.timezone(tz_name)
                        local_now_hour = datetime.now(user_tz).hour
                        in_quiet = (
                            (quiet_from <= quiet_to and quiet_from <= local_now_hour < quiet_to)
                            or (quiet_from > quiet_to and (local_now_hour >= quiet_from or local_now_hour < quiet_to))
                        )
                        if in_quiet:
                            # переносим на конец «тихого окна»
                            target_local = datetime.now(user_tz).replace(
                                hour=quiet_to, minute=0, second=0, microsecond=0
                            )
                            if target_local < datetime.now(user_tz):
                                target_local += timedelta(days=1)
                            target_utc = target_local.astimezone(timezone.utc)

                            if kind == "once":
                                supabase.table("reminders").update(
                                    {"remind_at": target_utc.isoformat()}
                                ).eq("id", r["id"]).execute()
                            else:
                                supabase.table("reminders").update(
                                    {"next_at": target_utc.isoformat()}
                                ).eq("id", r["id"]).execute()
                            logger.info(
                                f"defer reminder id={r['id']} to {target_utc.isoformat()} due to quiet hours"
                            )
                            continue

                    # отправляем
                    await self.bot.send_message(r["chat_id"], f"⏰ Напоминание: <b>{r['text']}</b>")
                    logger.info(f"sent reminder id={r['id']} chat_id={r['chat_id']} kind={kind}")

                    # пост-действия
                    if kind == "once":
                        supabase.table("reminders").delete().eq("id", r["id"]).execute()
                    else:
                        advance_recurring(r["id"], r["cron_expr"])

                except Exception as e:
                    logger.exception(f"Ошибка при отправке напоминания id={r.get('id')}: {e}")

            await asyncio.sleep(60)
