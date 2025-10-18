import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from croniter import croniter

# ---------------------------------------------
# Часовые пояса и форматирование
# ---------------------------------------------

# Базовый (системный) часовой пояс бота: по умолчанию МСК, берём из ENV для гибкости.
DEFAULT_TZ_NAME = os.getenv("DEFAULT_TZ", "Europe/Moscow")
DEFAULT_TZ = ZoneInfo(DEFAULT_TZ_NAME)
MSK_TZ = ZoneInfo("Europe/Moscow")  # фикс для турниров и совместимости


def _safe_zone(tz_name: str | None) -> ZoneInfo:
    """
    Возвращает корректный ZoneInfo.
    Если tz_name не задан или не найден, используется DEFAULT_TZ.
    """
    try:
        if tz_name:
            return ZoneInfo(tz_name)
    except Exception:
        pass
    return DEFAULT_TZ


def _is_american_tz(tz_name: str | None) -> bool:
    return bool(tz_name) and tz_name.startswith("America/")


def _hour_format_for(tz_name: str | None) -> str:
    """
    Выбираем 12/24-часовой формат.
    - America/* -> 12h с AM/PM
    - Остальные -> 24h
    """
    if _is_american_tz(tz_name):
        # %-I — Linux/Unix; на Windows это %#I. Делаем совместимо.
        try:
            datetime.now().strftime("%-I")
            return "%-I:%M %p"
        except Exception:
            return "%#I:%M %p"
    return "%H:%M"


def format_local_time(dt_utc: datetime, user_tz_name: str | None = None, with_tz_abbr: bool = False) -> str:
    """
    Форматирует момент времени (в UTC) в строку в часовом поясе пользователя.
    - user_tz_name: строка вида "America/New_York", "Asia/Yekaterinburg" и т.п.
    - with_tz_abbr: добавить ли аббревиатуру зоны (например, MSK, EDT)
    Возвращает строку "16:57" или "4:57 PM", опционально "4:57 PM (EDT)".
    """
    tz = _safe_zone(user_tz_name)
    local_dt = dt_utc.astimezone(tz)
    fmt = _hour_format_for(user_tz_name)
    base = local_dt.strftime(fmt)
    if with_tz_abbr:
        return f"{base} ({local_dt.tzname()})"
    return base


def msk_to_local_time_str(dt_msk: datetime, user_tz_name: str | None = None, with_tz_abbr: bool = False) -> str:
    """
    Удобный помощник: принимает время в МСК (aware datetime с tzinfo=Europe/Moscow),
    переводит в user_tz_name и форматирует как format_local_time.
    Нужен, если где-то специально храним/создаём МСК-время (например, турниры).
    """
    dt_utc = dt_msk.astimezone(ZoneInfo("UTC"))
    return format_local_time(dt_utc, user_tz_name=user_tz_name, with_tz_abbr=with_tz_abbr)


# ---------------------------------------------
# Русская морфология для "минут(а/ы)"
# ---------------------------------------------

def pluralize_minute_acc(n: int) -> str:
    """
    Подбирает форму слова 'минута' после предлога 'через' (винительный падеж):
    1 минуту, 2/3/4 минуты, 5–20 минут, 21 минуту, 22 минуты и т.д.
    """
    n = abs(int(n))
    n100 = n % 100
    if 11 <= n100 <= 14:
        return "минут"
    n10 = n % 10
    if n10 == 1:
        return "минуту"
    if 2 <= n10 <= 4:
        return "минуты"
    return "минут"


def humanize_repeat_suffix(cron_expr: str) -> str:
    """
    Возвращает человекочитаемую подпись для повторок:
    - */N * * * *  -> 'Повтор через N минут(у/ы/…)'
    - M H * * *    -> 'Повтор ежедневно'
    - иначе        -> 'Повтор по расписанию'
    """
    m = re.match(r"^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$", cron_expr)
    if m:
        n = int(m.group(1))
        return f"Повтор через {n} {pluralize_minute_acc(n)}"

    if re.match(r"^\d{1,2}\s+\d{1,2}\s+\*\s+\*\s+\*$", cron_expr):
        return "Повтор ежедневно"

    return "Повтор по расписанию"


# ---------------------------------------------
# Парсинг пользовательского времени (локально)
# ---------------------------------------------

def _apply_12h(hh: int, ampm: str) -> int:
    """Пересчитать часы из 12-часового формата в 24-часовой."""
    if hh == 12:
        hh = 0
    if ampm == "pm":
        hh += 12
    return hh


def parse_once_when(s: str, now_local: datetime, tz: ZoneInfo):
    """
    Возвращает (when_local: datetime, human: str).
    Поддерживает:
      +15 / "+ 15" -> через 15 минут
      "через N минут/минуту/минуты"
      "завтра HH:MM" / "завтра 7:10 pm"
      "HH:MM" или "7:10 pm" -> сегодня или завтра, если уже прошло
    """
    src = s.strip().lower().replace("  ", " ")

    # +N (минут)
    m = re.match(r"^\+?\s*(\d{1,3})\s*$", src)
    if m:
        minutes = int(m.group(1))
        when = now_local + timedelta(minutes=minutes)
        return when, f"через {minutes} {pluralize_minute_acc(minutes)}"

    # через N минут
    m = re.match(r"^через\s+(\d{1,3})\s*мин(уту|уты|ут|)\.?$", src)
    if m:
        minutes = int(m.group(1))
        when = now_local + timedelta(minutes=minutes)
        return when, f"через {minutes} {pluralize_minute_acc(minutes)}"

    # завтра HH:MM (24h)
    m = re.match(r"^завтра\s+(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        base = (now_local + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        return base, base.strftime("завтра в %H:%M")

    # завтра 12h: "завтра 7:10 pm" / "завтра 7 pm"
    m = re.match(r"^завтра\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", src)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3)
        hh24 = _apply_12h(hh, ampm)
        base = (now_local + timedelta(days=1)).replace(hour=hh24, minute=mm, second=0, microsecond=0)
        return base, base.strftime("завтра в %H:%M")

    # 12h: "7:10 pm", "7 pm", "07:10 AM"
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", src)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3)
        hh24 = _apply_12h(hh, ampm)
        candidate = now_local.replace(hour=hh24, minute=mm, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
            return candidate, candidate.strftime("завтра в %H:%M")
        return candidate, candidate.strftime("сегодня в %H:%M")

    # 24h: "HH:MM"
    m = re.match(r"^(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
            return candidate, candidate.strftime("завтра в %H:%M")
        return candidate, candidate.strftime("сегодня в %H:%M")

    raise ValueError("Не удалось распознать время. Примеры: +15, через 30 минут, завтра 09:00, 7:10 pm, 12:00")


def parse_repeat_spec(s: str, now_local: datetime):
    """
    Возвращает (cron_expr: str, human_suffix: str, next_local: datetime).
    Поддерживает:
      "каждую минуту"
      "каждые N минут"
      "ежедневно HH:MM"
      "HH:MM"  (ежедневно)
      "7:10 pm" / "7 pm"  (ежедневно, 12h)
      "cron: */15 * * * *"
    """
    src = s.strip().lower()

    # cron: RAW
    if src.startswith("cron:"):
        expr = src.split("cron:", 1)[1].strip()
        _ = croniter(expr, now_local)  # валидация
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "по cron", next_local

    # каждую минуту
    if src == "каждую минуту":
        expr = "*/1 * * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "через 1 минуту", next_local

    # каждые N минут
    m = re.match(r"^кажд(ую|ые)\s+(\d{1,3})\s+мин(уту|уты|ут|)$", src)
    if m:
        n = int(m.group(2))
        expr = f"*/{n} * * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, f"через {n} {pluralize_minute_acc(n)}", next_local

    # ежедневно HH:MM (24h)
    m = re.match(r"^ежедневно\s+(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        expr = f"{mm} {hh} * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежедневно", next_local

    # ежедневно 12h: "ежедневно 7:10 pm" / "ежедневно 7 pm"
    m = re.match(r"^ежедневно\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", src)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3)
        hh24 = _apply_12h(hh, ampm)
        expr = f"{mm} {hh24} * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежедневно", next_local

    # просто время -> ежедневно (24h)
    m = re.match(r"^(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        expr = f"{mm} {hh} * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежедневно", next_local

    # просто время -> ежедневно (12h)
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", src)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3)
        hh24 = _apply_12h(hh, ampm)
        expr = f"{mm} {hh24} * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежедневно", next_local

    raise ValueError("Не удалось распознать расписание. Примеры: каждую минуту, каждые 2 минуты, ежедневно 09:30, 7:10 pm, cron: */15 * * * *")


# ---------------------------------------------
# Конвертации (совместимость со старым кодом)
# ---------------------------------------------

def to_utc(dt_local: datetime, tz: ZoneInfo):
    """
    Преобразует локальное время (aware dt в tz) в UTC.
    Сигнатура сохранена для совместимости.
    """
    return dt_local.astimezone(ZoneInfo("UTC"))


def to_local(dt_utc: datetime, tz: ZoneInfo):
    """
    Преобразует UTC-время в указанный tz (обычно DEFAULT_TZ).
    Сигнатура сохранена для совместимости.
    """
    return dt_utc.astimezone(tz)


# ---------------------------------------------
# Удобные новые хелперы для отображения
# ---------------------------------------------

def to_local_by_name(dt_utc: datetime, tz_name: str | None) -> datetime:
    """
    Возвращает datetime в часовом поясе tz_name.
    Полезно, если в коде нужно dt, а не строка.
    """
    return dt_utc.astimezone(_safe_zone(tz_name))
