# scheduler_core.py
import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter
from aiogram import Bot

import db
from time_parse import (
    DEFAULT_TZ,   # единый базовый TZ (обычно Europe/Moscow)
    to_local,
    to_utc,
    humanize_repeat_suffix,
)
from texts import REMINDER_PREFIX, REMINDER_CRON_SUFFIX

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


SCHEDULER_INTERVAL_SEC = _env_int("SCHEDULER_INTERVAL_SEC", 10)
BATCH_LIMIT = _env_int("BATCH_LIMIT", 50)


async def delivery_loop(bot: Bot):
    """Фоновая задача: каждые N секунд доставляет due-напоминания батчами."""
    await asyncio.sleep(2.0)
    log.info(
        "Scheduler started with interval=%s sec, batch=%s",
        SCHEDULER_INTERVAL_SEC,
        BATCH_LIMIT,
    )
    while True:
        try:
            rows = await db.fetch_due(BATCH_LIMIT)
            for r in rows:
                await _process_due(bot, r)
        except Exception as e:
            log.exception("Scheduler tick error: %s", e)
        await asyncio.sleep(SCHEDULER_INTERVAL_SEC)


async def _process_due(bot: Bot, r):
    rid = r["id"]
    chat_id = r["chat_id"]
    kind = r["kind"]                # 'once' | 'cron'
    text = r["text"]
    cron_expr = r["cron_expr"]
    next_at = r["next_at"]          # UTC
    # category = r["category"]      # сейчас не влияет на текст

    # Базовый текст напоминания
    message_text = REMINDER_PREFIX.format(text=text)

    # Подпись для cron (склоняем «минуту/минуты/минут» корректно)
    suffix = ""
    if kind == "cron":
        try:
            suffix_human = humanize_repeat_suffix(cron_expr or "")
        except Exception:
            suffix_human = "Повтор по расписанию"
        suffix = REMINDER_CRON_SUFFIX.format(repeat_human=suffix_human)

    try:
        await bot.send_message(
            chat_id,
            message_text + (suffix if kind == "cron" else ""),
            parse_mode=os.getenv("PARSE_MODE", "HTML"),
        )

        if kind == "once":
            # одноразовое — удаляем по успеху
            await db.mark_once_delivered_success(rid)
        else:
            # cron — всегда сдвигаем next_at
            base = next_at or datetime.now(tz=ZoneInfo("UTC"))
            # base(UTC) -> локаль (DEFAULT_TZ) -> расчёт следующего -> снова UTC
            local_base = to_local(base, DEFAULT_TZ)
            nxt_local = croniter(cron_expr, local_base).get_next(datetime)
            nxt_utc = to_utc(nxt_local, DEFAULT_TZ)
            await db.shift_cron_next(rid, nxt_utc)

    except Exception as e:
        # Логируем, и чтобы не зациклиться, сдвигаем cron даже при ошибке отправки
        if kind == "cron":
            try:
                base = next_at or datetime.now(tz=ZoneInfo("UTC"))
                local_base = to_local(base, DEFAULT_TZ)
                nxt_local = croniter(cron_expr, local_base).get_next(datetime)
                nxt_utc = to_utc(nxt_local, DEFAULT_TZ)
                await db.shift_cron_next(rid, nxt_utc)
            except Exception:
                pass
        log.warning("Delivery error rid=%s chat=%s: %s", rid, chat_id, e)
