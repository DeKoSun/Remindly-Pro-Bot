import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from croniter import croniter

# ---------------------------------------------
# Часовые пояса и форматирование
# ---------------------------------------------

DEFAULT_TZ_NAME = os.getenv("DEFAULT_TZ", "Europe/Moscow")
DEFAULT_TZ = ZoneInfo(DEFAULT_TZ_NAME)
MSK_TZ = ZoneInfo("Europe/Moscow")  # фикс для турниров и совместимости


def _safe_zone(tz_name: str | None) -> ZoneInfo:
    try:
        if tz_name:
            return ZoneInfo(tz_name)
    except Exception:
        pass
    return DEFAULT_TZ


def _is_american_tz(tz_name: str | None) -> bool:
    return bool(tz_name) and tz_name.startswith("America/")


def _hour_format_for(tz_name: str | None) -> str:
    if _is_american_tz(tz_name):
        try:
            datetime.now().strftime("%-I")
            return "%-I:%M %p"
        except Exception:
            return "%#I:%M %p"
    return "%H:%M"


def format_local_time(dt_utc: datetime, user_tz_name: str | None = None, with_tz_abbr: bool = False) -> str:
    tz = _safe_zone(user_tz_name)
    local_dt = dt_utc.astimezone(tz)
    fmt = _hour_format_for(user_tz_name)
    base = local_dt.strftime(fmt)
    if with_tz_abbr:
        return f"{base} ({local_dt.tzname()})"
    return base


def msk_to_local_time_str(dt_msk: datetime, user_tz_name: str | None = None, with_tz_abbr: bool = False) -> str:
    dt_utc = dt_msk.astimezone(ZoneInfo("UTC"))
    return format_local_time(dt_utc, user_tz_name=user_tz_name, with_tz_abbr=with_tz_abbr)


# ---------------------------------------------
# Русская морфология и словари числительных
# ---------------------------------------------

_RU_NUM_WORDS = {
    "ноль": 0, "пол": 0.5,  # «полчаса» обработаем отдельно
    "одну": 1, "одна": 1, "один": 1, "раз": 1, "минутку": 1,
    "две": 2, "два": 2, "пара": 2, "пару": 2,
    "три": 3, "тройку": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12,
}

def _word_to_number(token: str) -> float | None:
    token = token.lower()
    return _RU_NUM_WORDS.get(token)


def pluralize_minute_acc(n: int) -> str:
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


# ---------------------------------------------
# Человечный суффикс для повторов
# ---------------------------------------------

def humanize_repeat_suffix(cron_expr: str) -> str:
    m = re.match(r"^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$", cron_expr)
    if m:
        n = int(m.group(1))
        return f"Повтор через {n} {pluralize_minute_acc(n)}"

    # Ежедневно (H M * * *)
    if re.match(r"^\d{1,2}\s+\d{1,2}\s+\*\s+\*\s+\*$", cron_expr):
        return "Повтор ежедневно"

    # По будням (H M * * 1-5)
    if re.match(r"^\d{1,2}\s+\d{1,2}\s+\*\s+\*\s+1-5$", cron_expr):
        return "Повтор по будням"

    # Ежемесячно (H M D * *)
    if re.match(r"^\d{1,2}\s+\d{1,2}\s+\d{1,2}\s+\*\s+\*$", cron_expr):
        return "Повтор ежемесячно"

    return "Повтор по расписанию"


# ---------------------------------------------
# Вспомогательные штуки для парсинга
# ---------------------------------------------

_AMPM_WORDS = {
    "am": "am", "pm": "pm",
    "утра": "am", "ночи": "am",   # 1–5 обычно «ночи» → am
    "дня": "pm", "вечера": "pm",
}

def _apply_12h(hh: int, ampm: str) -> int:
    if hh == 12:
        hh = 0
    if ampm == "pm":
        hh += 12
    return hh


def _to_float(num_str: str) -> float:
    # «1,5» -> 1.5 ; «0,6» -> 0.6
    return float(num_str.replace(",", "."))


def _parse_mixed_duration(ch: float | None, mn: float | None, d: float | None,
                          w: float | None, mo: float | None) -> timedelta:
    seconds = 0
    if mn:
        seconds += int(mn * 60)
    if ch:
        seconds += int(ch * 3600)
    if d:
        seconds += int(d * 86400)
    if w:
        seconds += int(w * 7 * 86400)
    if mo:
        seconds += int(mo * 30 * 86400)  # месяц ≈ 30 суток
    return timedelta(seconds=seconds)


def _normalize_time_word_hour(h: int, part: str | None) -> int:
    """
    Преобразуем «в 9 утра/вечера/дня/ночи» → 24h.
    Если part отсутствует — оставляем как есть (локальная логика «сегодня/завтра» ниже).
    """
    if not part:
        return h
    ampm = _AMPM_WORDS.get(part)
    if ampm in ("am", "pm"):
        return _apply_12h(h, ampm)
    return h


# ---------------------------------------------
# Парсинг одноразового времени (локально)
# ---------------------------------------------

def parse_once_when(s: str, now_local: datetime, tz: ZoneInfo):
    """
    Возвращает (when_local: datetime, human: str).
    Поддерживает массу форматов, в т.ч.:
      +N, + 15, +90, +1ч, + 1 ч, + 1ч 30мин
      «через минуту/минутку/мин»
      «через 0,6 мин», «через 1,5 часа», «через полчаса»
      «через 1 ч 30 мин», «через 1 час и 30 минут», «через 1ч 30мин.»
      «через пару минут/часов», «через тройку минут», «спустя 3 дня»
      «через день/два дня/неделю/месяц/сутки»
      «сегодня 21:30», «завтра 09:00», «завтра 7:10 pm»
      «7:10 pm», «19:10»
      «в 9 утра/в 7 вечера/в полночь/в полдень», «около 7 вечера», «примерно в 6»
    """
    src = " ".join(s.strip().lower().split())

    # --------- (+N / +... ч/мин) ----------
    # + 90 / +90
    m = re.match(r"^\+\s*(\d{1,4})\s*$", src)
    if m:
        minutes = int(m.group(1))
        when = now_local + timedelta(minutes=minutes)
        return when, f"через {minutes} {pluralize_minute_acc(minutes)}"

    # + 1ч / +1 ч / + 1 ч 30 мин / +1ч 30м / +1h 20m
    m = re.match(
        r"^\+\s*(?:(\d+(?:[.,]\d+)?)\s*(?:ч|часы|час|h))?(?:\s*(\d+(?:[.,]\d+)?)\s*(?:м|мин|минут[уы]?|m))?\s*$",
        src
    )
    if m and (m.group(1) or m.group(2)):
        ch = _to_float(m.group(1)) if m.group(1) else 0.0
        mn = _to_float(m.group(2)) if m.group(2) else 0.0
        when = now_local + _parse_mixed_duration(ch, mn, 0, 0, 0)
        total_min = int(round(ch * 60 + mn))
        return when, f"через {total_min} {pluralize_minute_acc(total_min)}"

    # просто +N минут, явное указание
    m = re.match(r"^\+\s*(\d{1,4})\s*(?:м|мин|минут[уы]?)\s*$", src)
    if m:
        minutes = int(m.group(1))
        when = now_local + timedelta(minutes=minutes)
        return when, f"через {minutes} {pluralize_minute_acc(minutes)}"

    # --------- «через …» ----------
    # через полчаса
    if src == "через полчаса":
        when = now_local + timedelta(minutes=30)
        return when, "через 30 минут"

    # через минуту / минутку / мин / одну минуту
    if src in ("через минуту", "через минутку", "через мин", "через одну минуту", "через одну мин"):
        when = now_local + timedelta(minutes=1)
        return when, "через 1 минуту"

    # через пару минут/часов; через тройку минут
    m = re.match(r"^через\s+(пару|тройку)\s+(минут[уы]?|мин|часа?|часов)\s*$", src)
    if m:
        word = m.group(1)
        unit = m.group(2)
        n = 2 if word == "пару" else 3
        if unit.startswith("мин"):
            when = now_local + timedelta(minutes=n)
            return when, f"через {n} {pluralize_minute_acc(n)}"
        else:
            when = now_local + timedelta(hours=n)
            mins = n * 60
            return when, f"через {mins} {pluralize_minute_acc(mins)}"

    # через X (слово-число) минут/часов (напр. «через две минуты»)
    m = re.match(r"^через\s+([а-яё]+)\s+(минут[уы]?|мин|часа?|часов)\s*$", src)
    if m:
        num = _word_to_number(m.group(1))
        unit = m.group(2)
        if num is not None:
            if unit.startswith("мин"):
                when = now_local + timedelta(minutes=num)
                return when, f"через {int(num)} {pluralize_minute_acc(int(num))}"
            else:
                when = now_local + timedelta(hours=num)
                mins = int(num * 60)
                return when, f"через {mins} {pluralize_minute_acc(mins)}"

    # через 0,6 мин / 1,5 часа / 45 минут / 2 часа / 3 дня / 1 неделя / 1 месяц
    m = re.match(
        r"^через\s+(?:(\d+(?:[.,]\d+)?)\s*(?:минут[уы]?|мин|м))?"
        r"(?:\s*(\d+(?:[.,]\d+)?)\s*(?:час(?:а|ов)?|ч))?"
        r"(?:\s*(\d+(?:[.,]\d+)?)\s*(?:д(?:ень|ня|ней)?|сут(?:ки|ок)?))?"
        r"(?:\s*(\d+(?:[.,]\d+)?)\s*(?:недел(?:ю|и|ь)?))?"
        r"(?:\s*(\d+(?:[.,]\d+)?)\s*(?:мес(?:яц)?(?:ев)?))?\s*$",
        src
    )
    if m and any(m.groups()):
        mn = _to_float(m.group(1)) if m.group(1) else 0.0
        ch = _to_float(m.group(2)) if m.group(2) else 0.0
        d  = _to_float(m.group(3)) if m.group(3) else 0.0
        w  = _to_float(m.group(4)) if m.group(4) else 0.0
        mo = _to_float(m.group(5)) if m.group(5) else 0.0
        when = now_local + _parse_mixed_duration(ch, mn, d, w, mo)
        total_min = int(round(mn + ch * 60 + d * 24 * 60 + w * 7 * 24 * 60 + mo * 30 * 24 * 60))
        return when, f"через {total_min} {pluralize_minute_acc(total_min)}"

    # через 1 ч 30 мин / через 1 час и 30 минут / через 1ч 30мин.
    m = re.match(
        r"^через\s*(\d+(?:[.,]\d+)?)\s*(?:ч|час(?:а|ов)?)"
        r"(?:\s*(?:и|,)?\s*(\d+(?:[.,]\d+)?)\s*(?:м|мин(?:ут[уы]?)?))?\s*\.?$",
        src
    )
    if m:
        ch = _to_float(m.group(1))
        mn = _to_float(m.group(2)) if m.group(2) else 0.0
        when = now_local + _parse_mixed_duration(ch, mn, 0, 0, 0)
        total_min = int(round(ch * 60 + mn))
        return when, f"через {total_min} {pluralize_minute_acc(total_min)}"

    # спустя N единиц (синоним «через»)
    m = re.match(r"^спустя\s+(\d+)\s*(д(ень|ня|ней)|сут(ки|ок)|час(а|ов)?|мин(ут[уы]?|))$", src)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("д") or unit.startswith("сут"):
            when = now_local + timedelta(days=n)
            mins = n * 24 * 60
            return when, f"через {mins} {pluralize_minute_acc(mins)}"
        if unit.startswith("час"):
            when = now_local + timedelta(hours=n)
            mins = n * 60
            return when, f"через {mins} {pluralize_minute_acc(mins)}"
        when = now_local + timedelta(minutes=n)
        return when, f"через {n} {pluralize_minute_acc(n)}"

    # через день/два дня/неделю/месяц/сутки/неделю ровно
    m = re.match(r"^через\s+(день|два дня|сутки|неделю(?:\sровно)?|месяц)$", src)
    if m:
        word = m.group(1)
        if word in ("день", "сутки"):
            when = now_local + timedelta(days=1)
            return when, "через 1440 минут"
        if word == "два дня":
            when = now_local + timedelta(days=2)
            return when, "через 2880 минут"
        if word.startswith("неделю"):
            when = now_local + timedelta(days=7)
            return when, "через 10080 минут"
        if word == "месяц":
            when = now_local + timedelta(days=30)
            return when, "через 43200 минут"

    # сегодня HH:MM
    m = re.match(r"^сегодня\s+(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
            return candidate, candidate.strftime("завтра в %H:%M")
        return candidate, candidate.strftime("сегодня в %H:%M")

    # завтра HH:MM
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

    # просто 12h: "7:10 pm", "7 pm"
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

    # «в HH:MM», «в 9 утра/вечера/ночи/дня», «около 7 вечера», «примерно в 6»
    # HH:MM
    m = re.match(r"^в\s+(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
            return candidate, candidate.strftime("завтра в %H:%M")
        return candidate, candidate.strftime("сегодня в %H:%M")

    # «в 9 утра/вечера/…» или «около 7 вечера», «примерно в 6»
    m = re.match(r"^(?:около|примерно)?\s*в\s*(\d{1,2})(?:\s*(утра|вечера|ночи|дня))?$", src)
    if m:
        hh = int(m.group(1))
        part = m.group(2)
        hh24 = _normalize_time_word_hour(hh, part)
        candidate = now_local.replace(hour=hh24, minute=0, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
            return candidate, candidate.strftime("завтра в %H:%M")
        return candidate, candidate.strftime("сегодня в %H:%M")

    # «в полночь» / «в полдень»
    if src == "в полночь":
        candidate = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate, candidate.strftime("сегодня в %H:%M") if candidate.date() == now_local.date() else candidate.strftime("завтра в %H:%M")
    if src == "в полдень":
        candidate = now_local.replace(hour=12, minute=0, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate, candidate.strftime("сегодня в %H:%M") if candidate.date() == now_local.date() else candidate.strftime("завтра в %H:%M")

    # просто 24h: "HH:MM"
    m = re.match(r"^(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
            return candidate, candidate.strftime("завтра в %H:%M")
        return candidate, candidate.strftime("сегодня в %H:%M")

    raise ValueError("Не удалось распознать время. Примеры: +15, через 30 минут, через 1 ч 30 мин, завтра 09:00, 7:10 pm, в 9 утра, 12:00")


# ---------------------------------------------
# Парсинг расписаний (повторяющиеся)
# ---------------------------------------------

def parse_repeat_spec(s: str, now_local: datetime):
    """
    Возвращает (cron_expr: str, human_suffix: str, next_local: datetime).
    Поддерживает:
      - «каждую минуту», «каждые N минут», «каждые две/три/пять минут»
      - «каждый час», «каждые 3 часа»
      - «ежедневно HH:MM», «в 12:00», «7:10 pm»
      - «по будням 10:00»
      - «каждое первое число» (по умолчанию 09:00), «ежемесячно 10 числа в 08:00»,
        «25 числа каждого месяца 18:30»
      - «cron: */15 * * * *»
    """
    src = " ".join(s.strip().lower().split())

    # cron: RAW
    if src.startswith("cron:"):
        expr = src.split("cron:", 1)[1].strip()
        _ = croniter(expr, now_local)
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "по cron", next_local

    # каждую минуту
    if src in ("каждую минуту", "каждая минута"):
        expr = "*/1 * * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "через 1 минуту", next_local

    # каждые N минут / каждые две/три/пять минут(ы)
    m = re.match(r"^кажд(ую|ые)\s+(\d{1,3}|[а-яё]+)\s+мин(уту|уты|ут|)$", src)
    if m:
        raw = m.group(2)
        n = int(raw) if raw.isdigit() else int(_word_to_number(raw) or 0)
        if n <= 0:
            raise ValueError("Некорректный интервал минут.")
        expr = f"*/{n} * * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, f"через {n} {pluralize_minute_acc(n)}", next_local

    # каждый час / каждые N часов
    m = re.match(r"^кажд(ый|ые)\s+(\d{1,2}|[а-яё]+)?\s*час(а|ов)?$", src)
    if m:
        raw = m.group(2)
        n = 1 if not raw else (int(raw) if raw.isdigit() else int(_word_to_number(raw) or 0))
        if n <= 0:
            n = 1
        # «каждый час» = «0 */1 * * *»
        expr = f"0 */{n} * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        # Для суффикса дадим «через N минут» для ближайшего интервала — но это часы,
        # поэтому пишем человекочитаемо:
        return expr, ("каждый час" if n == 1 else f"каждые {n} часа"), next_local

    # по будням HH:MM
    m = re.match(r"^по\s+будням\s+(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        expr = f"{mm} {hh} * * 1-5"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "по будням", next_local

    # ежедневно HH:MM (24h)
    m = re.match(r"^ежедневно\s+(\d{1,2}):(\d{2})$", src)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        expr = f"{mm} {hh} * * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежедневно", next_local

    # ежедневно 12h
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

    # каждое первое число (в 09:00 по умолчанию)
    if src == "каждое первое число":
        hh, mm = 9, 0
        expr = f"{mm} {hh} 1 * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежемесячно", next_local

    # ежемесячно 10 числа (в 09:00) / ежемесячно 10 числа в 08:00
    m = re.match(r"^ежемесячно\s+(\d{1,2})\s+числа(?:\s+в\s+(\d{1,2}):(\d{2}))?$", src)
    if m:
        day = int(m.group(1))
        hh = int(m.group(2)) if m.group(2) else 9
        mm = int(m.group(3)) if m.group(3) else 0
        expr = f"{mm} {hh} {day} * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежемесячно", next_local

    # 25 числа каждого месяца 18:30 / 25 числа каждого месяца
    m = re.match(r"^(\d{1,2})\s+числа\s+каждого\s+месяца(?:\s+(\d{1,2}):(\d{2}))?$", src)
    if m:
        day = int(m.group(1))
        hh = int(m.group(2)) if m.group(2) else 9
        mm = int(m.group(3)) if m.group(3) else 0
        expr = f"{mm} {hh} {day} * *"
        next_local = croniter(expr, now_local).get_next(datetime)
        return expr, "ежемесячно", next_local

    raise ValueError("Не удалось распознать расписание. Примеры: "
                     "каждую минуту, каждые 2 минуты, каждые три минуты, каждый час, каждые 3 часа, "
                     "ежедневно 09:30, по будням 10:00, в 12:00, 7:10 pm, "
                     "каждое первое число, ежемесячно 10 числа в 08:00, cron: */15 * * * *")


# ---------------------------------------------
# Конвертации (совместимость со старым кодом)
# ---------------------------------------------

def to_utc(dt_local: datetime, tz: ZoneInfo):
    return dt_local.astimezone(ZoneInfo("UTC"))


def to_local(dt_utc: datetime, tz: ZoneInfo):
    return dt_utc.astimezone(tz)


# ---------------------------------------------
# Удобные новые хелперы для отображения
# ---------------------------------------------

def to_local_by_name(dt_utc: datetime, tz_name: str | None) -> datetime:
    return dt_utc.astimezone(_safe_zone(tz_name))
