import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from aiogram import Bot
from croniter import croniter
import pytz

from db import Db

log = logging.getLogger("scheduler")

FOOTER_RECURRING = "🔁 Повтор через 15 минут"

class SchedulerCore:
    def __init__(self, bot: Bot, tz_name: str = "UTC", interval_seconds: int = 30, debug: bool = False):
        self.bot = bot
        self.tz = pytz.timezone(tz_name)
        self.interval = interval_seconds
        self._task: asyncio.Task | None = None
        self.debug = debug

    async def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._runner())

    async def _runner(self):
        log.info("Scheduler started; interval=%ss", self.interval)
        while True:
            try:
                await self._tick()
            except Exception as e:
                log.exception("scheduler tick error: %s", e)
            await asyncio.sleep(self.interval)

    async def _tick(self):
        due = await Db.get_due(window_seconds=self.interval)
        if self.debug:
            log.debug("due count: %s", len(due))
        for r in due:
            try:
                await self._deliver(r)
                if r["kind"] == "once":
                    await Db.complete_once(r["id"])
                else:
                    # compute next by cron
                    next_at = self._next_from_cron(r["cron_expr"])
                    await Db.set_next(r["id"], next_at)
            except Exception as e:
                log.exception("deliver error id=%s: %s", r["id"], e)

    async def _deliver(self, r: dict):
        chat_id = r["chat_id"]
        text = r["text"]
        if r["kind"] == "cron":
            text = f"{text}\n\n{FOOTER_RECURRING}"
        await self.bot.send_message(chat_id, f"⏰ Напоминание: <b>{text}</b>")

    # ------------- parsing helpers -------------

    @staticmethod
    async def parse_when(raw: str, tz_name: str = "UTC") -> Optional[datetime]:
        """
        Примеры:
          14:30
          завтра 10:00
          через 25 минут
          +15  (минут)
        """
        raw = raw.strip().lower()
        now = datetime.now(pytz.timezone(tz_name))

        # +N (минут)
        if raw.startswith("+"):
            try:
                minutes = int(raw[1:].strip())
                return now + timedelta(minutes=minutes)
            except:
                pass

        # через N минут (склонения)
        for kw in ("минуты", "минуту", "минут", "минута"):
            if "через" in raw and kw in raw:
                try:
                    n = int("".join(ch for ch in raw if ch.isdigit()))
                    return now + timedelta(minutes=n)
                except:
                    pass

        # завтра HH:MM
        if raw.startswith("завтра"):
            parts = raw.split()
            if len(parts) >= 2 and ":" in parts[1]:
                hh, mm = parts[1].split(":")[:2]
                base = (now + timedelta(days=1)).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                return base

        # HH:MM сегодня/ближайшее
        if ":" in raw:
            try:
                hh, mm = raw.split(":")[:2]
                cand = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if cand <= now:
                    cand += timedelta(days=1)
                return cand
            except:
                pass

        return None

    @staticmethod
    async def parse_repeat(raw: str, tz_name: str = "UTC") -> Tuple[Optional[str], Optional[datetime]]:
        """
        Возвращает (cron_expr, next_at)
        Поддержка:
          • "каждую минуту"
          • "ежедневно HH:MM"
          • "HH:MM"
          • "cron: * * * * *"
        """
        raw = raw.strip().lower()
        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)

        if raw.startswith("cron:"):
            expr = raw.replace("cron:", "").strip()
            try:
                nxt = datetime.fromtimestamp(croniter(expr, now).get_next(float), tz)
                return expr, nxt
            except:
                return None, None

        if "каждую минуту" in raw:
            expr = "* * * * *"
            nxt = datetime.fromtimestamp(croniter(expr, now).get_next(float), tz)
            return expr, nxt

        if raw.startswith("ежедневно") and ":" in raw:
            hh, mm = raw.split()[-1].split(":")[:2]
            expr = f"{int(mm)} {int(hh)} * * *"
            nxt = datetime.fromtimestamp(croniter(expr, now).get_next(float), tz)
            return expr, nxt

        if ":" in raw:
            hh, mm = raw.split(":")[:2]
            expr = f"{int(mm)} {int(hh)} * * *"
            nxt = datetime.fromtimestamp(croniter(expr, now).get_next(float), tz)
            return expr, nxt

        return None, None

    def _next_from_cron(self, expr: str) -> datetime:
        now = datetime.now(self.tz)
        ts = croniter(expr, now).get_next(float)
        return datetime.fromtimestamp(ts, self.tz)
