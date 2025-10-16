import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from croniter import croniter

# Простая нормализация и парсинг
def parse_once_when(s: str, now_local: datetime, tz: ZoneInfo):
    """
    Возвращает (when_local: datetime, human: str).
    Поддерживает:
      +15 / "+ 15" -> через 15 минут
      "через N минут/минуту/минуты"
      "завтра HH:MM"
      "HH:MM" -> сегодня или завтра, если уже прошло
    """
    src = s.strip().lower().replace("  ", " ")
    m = re.match(r"^\+?\s*(\d{1,3})\s*$", src)
    if m:
        minutes = int(m.group(1))
        when = now_local + timedelta(minutes=minutes)
        return when, f"через {minutes} мин."

    m = re.match(r"^через\s+(\d{1,3})\s*мин(уту|уты|ут|)\.?$", src)
    if m:
        minutes = int(m.group(1))
        when = now_local + timedelta(minutes=minutes)
        return when, f"через {minutes} мин."

    m = re.match(r"^завтра\s+(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        base = (now_local + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        return base, base.strftime("завтра в %H:%M")

    m = re.match(r"^(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
            return candidate, candidate.strftime("завтра в %H:%M")
        return candidate, candidate.strftime("сегодня в %H:%M")

    raise ValueError("Не удалось распознать время. Примеры: +15, через 30 минут, завтра 09:00, 12:00")

def parse_repeat_spec(s: str, now_local: datetime):
    """
    Возвращает (cron_expr: str, human_suffix: str, next_local: datetime).
    Поддерживает:
      "каждую минуту"
      "каждые N минут"
      "ежедневно HH:MM"
      "HH:MM"  (ежедневно)
      "cron: */15 * * * *"
    """
    src = s.strip().lower()

    if src.startswith("cron:"):
        expr = src.split("cron:", 1)[1].strip()
        _ = croniter(expr, now_local)  # валидация
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "по cron", next_local

    if src == "каждую минуту":
        expr = "*/1 * * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "через 1 минуту", next_local

    m = re.match(r"^кажд(ую|ые)\s+(\d{1,3})\s+мин(уту|уты|ут|)$", src)
    if m:
        n = int(m.group(2))
        expr = f"*/{n} * * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, f"через {n} мин.", next_local

    m = re.match(r"^ежедневно\s+(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        expr = f"{mm} {hh} * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежедневно", next_local

    m = re.match(r"^(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        expr = f"{mm} {hh} * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежедневно", next_local

    raise ValueError("Не удалось распознать расписание. Примеры: каждую минуту, каждые 2 минуты, ежедневно 09:30, 12:00, cron: */15 * * * *")

def to_utc(dt_local: datetime, tz: ZoneInfo):
    return dt_local.astimezone(ZoneInfo("UTC"))

def to_local(dt_utc: datetime, tz: ZoneInfo):
    return dt_utc.astimezone(tz)
