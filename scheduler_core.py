# scheduler_core.py
import os
import asyncio
import logging
from datetime import datetime, time as dtime, timedelta, timezone

import pytz
from aiogram import Bot
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

from texts import pick_phrase, TOURNAMENT_VARIANTS
from db import get_tournament_subscribed_chats, get_conn

logger = logging.getLogger("remindly")


# =========================
#   Турнирный планировщик
# =========================

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
    """
    Планировщик «турнирных» уведомлений по фиксированным MSK-временным слотам
    для всех чатов, подписанных в БД.
    """

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
        # Переустанавливаем задания раз в 5 минут и сразу при старте
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            CronTrigger.from_crontab("*/5 * * * *", timezone=self.scheduler.timezone),
        )
        self.scheduler.add_job(
            self._ensure_tournament_jobs,
            next_run_time=datetime.now(self.scheduler.timezone),
        )

    async def _send_tournament(self, chat_id: int, notify_time: dtime) -> None:
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
                args=[chat_id, dtime(*START_DISPLAY_MAP[(hour, minute)])],
                replace_existing=True,
            )

    def _ensure_tournament_jobs(self) -> None:
        rows = get_tournament_subscribed_chats()
        for r in rows:
            # ожидаем SELECT chat_id, tz FROM chats ...
            chat_id = r[0]
            tz_name = r[1] if len(r) > 1 else None
            self._register_daily_jobs_for_chat(chat_id, tz_name)


# ==================================
#   Универсальные напоминания (DB)
# ==================================

class UniversalReminderScheduler:
    """
    Фоновый поллер БД: берёт «просроченные» одноразовые/повторяющиеся напоминания
    и отправляет сообщения. Для повторяющихся рассчитывает следующее next_at.
    Ожидаемые колонки в public.reminders:
      id (uuid PK), user_id int8, chat_id int8, text text,
      paused bool default false, created_at timestamptz default now(),
      kind text, cron_expr text, remind_at timestamptz, next_at timestamptz,
      created_by int8 NULL, updated_at timestamptz NULL
    """

    def __init__(self, bot: Bot, poll_interval_sec: int = 30):
        self.bot = bot
        self.poll_interval_sec = max(5, poll_interval_sec)
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._check_reminders(), name="reminders-poller")

    def stop(self):
        if self._task:
            try:
                self._task.cancel()
            except Exception:
                pass

    # ---------- helpers ----------

    @staticmethod
    def _parse_hhmm(s: str) -> tuple[int, int] | None:
        try:
            hh, mm = s.strip().split(":")
            return int(hh), int(mm)
        except Exception:
            return None

    def _calc_next_for_kind(self, r: dict, now_utc: datetime) -> datetime | None:
        """
        Возвращает следующий момент (UTC) для повторяющегося напоминания.
        Ожидаемые значения:
          kind: repeat_daily / repeat_weekdays / repeat_weekend / repeat_cron / cron
          cron_expr: для daily/… ожидаем 'HH:MM', для cron — стандартный cron.
        """
        kind = (r.get("kind") or "").strip().lower()
        cron_expr = (r.get("cron_expr") or "").strip()

        # Если для одноразовых сюда попали — вернём None (они будут удалены)
        if not kind or kind == "once":
            return None

        # cron-режим (поддерживаем и 'repeat_cron', и исторический 'cron')
        if kind in ("repeat_cron", "cron"):
            try:
                it = croniter(cron_expr, now_utc + timedelta(seconds=1))
                nxt = it.get_next(datetime)
                # приводим к UTC
                if nxt.tzinfo is None:
                    nxt = nxt.replace(tzinfo=timezone.utc)
                else:
                    nxt = nxt.astimezone(timezone.utc)
                return nxt
            except Exception:
                logger.warning("Bad cron_expr for reminder id=%s: %r", r.get("id"), cron_expr)
                return None

        # Ежедневно/по будням/по выходным — как HH:MM (UTC)
        hhmm = self._parse_hhmm(cron_expr)
        if not hhmm:
            logger.warning("Bad HH:MM for reminder id=%s kind=%s expr=%r", r.get("id"), kind, cron_expr)
            return None
        hh, mm = hhmm

        # ближайшее HH:MM на сегодня/завтра
        candidate = now_utc.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now_utc:
            candidate += timedelta(days=1)

        if kind == "repeat_daily":
            return candidate

        if kind == "repeat_weekdays":
            # Пн=0 ... Вс=6
            while candidate.weekday() >= 5:  # 5,6 = сб, вс
                candidate += timedelta(days=1)
            return candidate

        if kind == "repeat_weekend":
            while candidate.weekday() < 5:
                candidate += timedelta(days=1)
            return candidate

        # неизвестный тип — не зацикливаем
        return None

    def _backfill_next_at_for_active_repeats(self, now_utc: datetime):
        """
        Подстраховка: для активных повторных напоминаний с NULL next_at —
        проставляем ближайшее будущее значение next_at (чтобы они «завелись»).
        """
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """
                SELECT id, kind, cron_expr, remind_at, next_at
                FROM reminders
                WHERE paused = false
                  AND kind IS NOT NULL
                  AND lower(kind) <> 'once'
                  AND next_at IS NULL
                LIMIT 100
                """
            )
            to_fix = cur.fetchall()

        if not to_fix:
            return

        for rid, kind, cron_expr, remind_at, next_at in to_fix:
            r_dict = {
                "id": rid,
                "kind": (kind or "").strip().lower(),
                "cron_expr": cron_expr,
                "remind_at": remind_at,
                "next_at": next_at,
            }
            nxt = self._calc_next_for_kind(r_dict, now_utc)
            if nxt:
                try:
                    with get_conn() as c2:
                        cur2 = c2.cursor()
                        cur2.execute("UPDATE reminders SET next_at = %s WHERE id = %s", (nxt, rid))
                        c2.commit()
                except Exception:
                    logger.exception("Failed to backfill next_at for reminder id=%s", rid)

    # ---------- main loop ----------

    async def _check_reminders(self):
        while True:
            try:
                now_utc = datetime.now(timezone.utc)

                # 1) Проставим next_at там, где он пустой у повторов
                self._backfill_next_at_for_active_repeats(now_utc)

                # 2) Забираем due-напоминания (сравнение с параметром now_utc)
                with get_conn() as c:
                    cur = c.cursor()
                    cur.execute(
                        """
                        SELECT id, chat_id, text, kind, cron_expr
                             , remind_at, next_at
                        FROM reminders
                        WHERE paused = false
                          AND COALESCE(next_at, remind_at) <= %s
                        ORDER BY COALESCE(next_at, remind_at) ASC
                        LIMIT 50
                        """,
                        (now_utc,),
                    )
                    rows = cur.fetchall()

                # 3) Отправляем пользователю
                for row in rows:
                    # row как tuple (с psycopg2 без DictCursor); индексы строго по SELECT
                    rid, chat_id, text, kind, cron_expr, remind_at, next_at = row
                    try:
                        # В сообщении — только человекочитаемый текст
                        await self.bot.send_message(chat_id, f"⏰ Напоминание: <b>{text}</b>")
                        logger.info("sent reminder id=%s chat_id=%s", rid, chat_id)
                    except Exception as e:
                        logger.exception("Failed to send reminder id=%s chat_id=%s: %s", rid, chat_id, e)

                    # 4) Пост-обработка: удалить once или сдвинуть next_at для повторов
                    try:
                        with get_conn() as c2:
                            cur2 = c2.cursor()
                            k = (kind or "").strip().lower()
                            if not k or k == "once":
                                # одноразовое — удаляем
                                cur2.execute("DELETE FROM reminders WHERE id = %s", (rid,))
                            else:
                                # повторяющееся — пересчитать next_at
                                r_dict = {
                                    "id": rid,
                                    "kind": k,
                                    "cron_expr": cron_expr,
                                    "remind_at": remind_at,
                                    "next_at": next_at,
                                }
                                nxt = self._calc_next_for_kind(r_dict, now_utc)
                                if nxt is None:
                                    # предохранитель: не зацикливаем и не спамим
                                    cur2.execute(
                                        "UPDATE reminders SET next_at = NULL, paused = true WHERE id = %s",
                                        (rid,),
                                    )
                                else:
                                    cur2.execute("UPDATE reminders SET next_at = %s WHERE id = %s", (nxt, rid))
                            c2.commit()
                    except Exception:
                        logger.exception("Post-process failed for reminder id=%s", rid)

            except asyncio.CancelledError:
                # корректная остановка при отмене таска (редеплой и т.п.)
                break
            except Exception:
                logger.exception("Reminders loop iteration failed")

            await asyncio.sleep(self.poll_interval_sec)
