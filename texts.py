from __future__ import annotations

from typing import List, Callable
import random
import html

E = {
    "ok": "✅",
    "no": "❌",
    "warn": "⚠️",
    "bell": "⏰",
    "spark": "✨",
    "ping": "🏓",
    "list": "📋",
    "repeat": "🔁",
    "pause": "⏸️",
    "play": "▶️",
    "trash": "🗑️",
    "clock": "🕒",
    "gear": "⚙️",
    "info": "ℹ️",
    "trophy": "🏆",
    "rocket": "🚀",
}

def plural_ru(n: int, f1: str, f2: str, f5: str) -> str:
    n = abs(int(n))
    n10 = n % 10
    n100 = n % 100
    if n10 == 1 and n100 != 11:
        return f1
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return f2
    return f5

TOURNAMENT_VARIANTS: List[str] = [
    f"{E['bell']} Через 5 минут стартует {{title}} — начало в {{time}}!",
    f"🔥 {{title}} начинается в {{time}}. Осталось 5 минут!",
    f"⚡ {{title}} через 5 минут ({{time}}). Поехали!",
    f"{E['rocket']} Через 5 минут стартует {{title}}! Начало в {{time}}, не пропусти!",
    f"⏳ Осталось 5 минут — {{title}} на старте! ({{time}})",
    f"🕓 Напоминание: {{title}} начинается в {{time}}.",
]

REMINDER_VARIANTS: List[str] = [
    f"{E['bell']} Напоминание: {{title}}",
    f"{E['spark']} Пора: {{title}}",
    f"{E['info']} Не забудь: {{title}}",
    f"🎯 Самое время: {{title}}",
]

REMINDER_TEMPLATES: List[str] = [
    f"{E['rocket']} Напоминание прилетело! ",
    f"{E['spark']} Время действовать! ",
    f"Тик-так! ",
    f"Поехали! ",
    f"Беру на контроль: ",
]

def pick_phrase(variants: List[str], *, title: str, time: str | None = None) -> str:
    base = random.choice(variants)
    return base.format(title=html.escape(title), time=(time or ""))

def smart_phrase(prefix: str | None = None) -> str:
    base = random.choice(REMINDER_TEMPLATES)
    return (prefix or "") + base

MSG: dict[str, str | Callable[..., str]] = {
    "enter_text": f"{E['spark']} Введи текст напоминания:",
    "when_once": (
        f"{E['clock']} Когда напомнить?\n"
        "Примеры: <b>14:30</b> · <b>завтра 10:00</b> · <b>через 25</b> · <b>+15</b>"
    ),
    "created_once": lambda txt, when_utc: (
        f"{E['ok']} Напоминание создано:\n<b>{html.escape(txt)}</b>\n"
        f"{E['clock']} {when_utc}"
    ),
    "enter_text_repeat": f"{E['spark']} Введи текст <b>повторяющегося</b> напоминания:",
    "when_repeat": (
        f"{E['clock']} Какое расписание?\n"
        "• <b>каждую минуту</b>\n"
        "• <b>каждые N минут</b>\n"
        "• <b>ежедневно HH:MM</b>\n"
        "• <b>HH:MM</b> (ежедневно)\n"
        "• <b>cron: * * * * *</b> (любой CRON)"
    ),
    "created_cron": lambda txt, cron, next_utc: (
        f"{E['ok']} Повторяющееся напоминание создано:\n"
        f"<b>{html.escape(txt)}</b>\n"
        f"{E['repeat']} CRON: <code>{html.escape(cron)}</code>\n"
        f"{E['clock']} Ближайшее: <b>{next_utc}</b>"
    ),
    "list_empty": f"{E['list']} Пока нет напоминаний.",
    "list_header": f"<b>{E['list']} Напоминания этого чата:</b>",
    "paused": f"{E['pause']} Поставлено на паузу",
    "resumed": f"{E['play']} Возобновлено",
    "deleted": f"{E['trash']} Удалено",
    "pong_ok": f"{E['ping']} pong — база и планировщик в порядке",
    "pong_db_err": lambda err: f"{E['ping']} pong — {E['no']} db error: <code>{html.escape(str(err))}</code>",
    "bad_action": f"{E['warn']} Неизвестное действие.",
    "bad_data": f"{E['warn']} Некорректные данные.",
    "create_fail": lambda err: f"{E['no']} Не удалось создать. Причина: <code>{html.escape(str(err))}</code>",
    "create_cron_fail": (
        f"{E['no']} Не удалось создать повторяющееся. "
        "Проверь формат (можно <code>cron: EXPR</code>)."
    ),
}

HELP_TEXT = (
    f"{E['spark']} Привет! Я бот-напоминалка для турниров и любых событий.\n\n"
    f"{E['trophy']} Турниры:\n"
    "• /subscribe_tournaments — включить турнирные напоминания в этом чате\n"
    "• /unsubscribe_tournaments — выключить турнирные напоминания\n"
    "\n"
    f"{E['bell']} Универсальные напоминания:\n"
    "• /add — мастер создания одноразового напоминания\n"
    "  Примеры времени: 14:30 • завтра 10:00 • через 25 • +15\n"
    "• /add_repeat — мастер создания повторяющегося (каждые N минут / ежедневно / cron)\n"
    "• /list — список с кнопками (пауза/удалить)\n"
    "\n"
    f"{E['info']} Полезное:\n"
    "• /ping — проверить состояние\n"
    "• /help — эта справка\n"
)
