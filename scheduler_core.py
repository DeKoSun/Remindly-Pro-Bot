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
    DEFAULT_TZ,   # базовый TZ — fallback
    to_local,
    to_utc,
    humanize_repeat_suffix,
)
from texts import REMINDER_PREFIX, REMINDER_CRON_SUFFIX, tournament_phrase_by_index

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
        SCHEDULER_INTERVAL_SEC, BATCH_LIMIT,
    )
    while True:
        try:
            rows = await db.fetch_due(BATCH_LIMIT)
            for r in rows:
                await _process_due(bot, r)
        except Exception as e:
            log.exception("Scheduler tick error: %s", e)
        await asyncio.sleep(SCHEDULER_INTERVAL_SEC)


def _tz_from_meta(meta) -> ZoneInfo:
    """
    Достаём таймзону из meta (jsonb) напоминания.
    meta может быть None/{} или {'tz': 'America/New_York'}.
    Если нет — используем DEFAULT_TZ.
    """
    try:
        tz_name = (meta or {}).get("tz")
        return ZoneInfo(tz_name) if tz_name else DEFAULT_TZ
    except Exception:
        return DEFAULT_TZ


async def _process_due(bot: Bot, r: dict):
    rid = r["id"]
    chat_id = r["chat_id"]
    kind = r["kind"]                # 'once' | 'cron'
    text = r["text"]
    cron_expr = r.get("cron_expr")
    next_at = r.get("next_at")      # UTC-aware
    meta = r.get("meta")            # jsonb -> dict (или None)
    category = (r.get("category") or "").strip()  # 'tournament' для турнирных

    # --- текст сообщения ---
    # Турниры: ротация уникальных фраз; иначе — общий шаблон
    if category == "tournament":
        kv_key = f"t_phrase_idx:{chat_id}"
        idx = await db.kv_get_int(kv_key) or 0
        phrase, next_idx = tournament_phrase_by_index(idx)
        await db.kv_set_int(kv_key, next_idx)
        message_text = phrase
    else:
        message_text = REMINDER_PREFIX.format(text=text)

    # Подпись для cron (склонение «минуту/минуты/минут») — скрываем для турниров
    suffix = ""
    if kind == "cron" and category != "tournament":
        try:
            suffix_human = humanize_repeat_suffix(cron_expr or "")
        except Exception:
            suffix_human = "Повтор по расписанию"
        suffix = REMINDER_CRON_SUFFIX.format(repeat_human=suffix_human)

    # Таймзона для расчёта следующего срабатывания (из meta или DEFAULT_TZ)
    cron_tz = _tz_from_meta(meta)

    try:
        await bot.send_message(
            chat_id,
            message_text + (suffix if kind == "cron" else ""),
            # parse_mode задан через DefaultBotProperties при создании Bot,
            # оставляем резервный параметр на случай переопределения:
            parse_mode=os.getenv("PARSE_MODE", "HTML"),
        )

        if kind == "once":
            # одноразовое — помечаем доставленным
            await db.mark_once_delivered_success(rid)
        else:
            # cron — сдвигаем next_at
            if not cron_expr:
                log.warning("Cron reminder without cron_expr, rid=%s", rid)
            else:
                base = next_at or datetime.now(tz=ZoneInfo("UTC"))
                # base(UTC) -> локаль (cron_tz) -> расчёт следующего -> снова UTC
                local_base = to_local(base, cron_tz)
                nxt_local = croniter(cron_expr, local_base).get_next(datetime)
                nxt_utc = to_utc(nxt_local, cron_tz)
                await db.shift_cron_next(rid, nxt_utc)

    except Exception as e:
        # Чтобы не зациклиться, пробуем сдвинуть cron даже при ошибке отправки
        if kind == "cron" and cron_expr:
            try:
                base = next_at or datetime.now(tz=ZoneInfo("UTC"))
                local_base = to_local(base, cron_tz)
                nxt_local = croniter(cron_expr, local_base).get_next(datetime)
                nxt_utc = to_utc(nxt_local, cron_tz)
                await db.shift_cron_next(rid, nxt_utc)
            except Exception:
                pass
        log.warning("Delivery error rid=%s chat=%s: %s", rid, chat_id, e)
