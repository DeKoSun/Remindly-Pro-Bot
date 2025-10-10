from datetime import datetime
from typing import List


# Банк вариативных фраз (без внешнего ИИ)
TOURNAMENT_VARIANTS: List[str] = [
"Через 5 минут стартует {title} — начало в {time}!",
"{title} уже на пороге: старт в {time}, готовьтесь!",
"{title} начинается в {time}. Осталось 5 минут!",
"{title} через 5 минут ({time}). Поехали!",
"Финишируем в подготовке: {title} стартует в {time}."
]


REMINDER_VARIANTS: List[str] = [
"Напоминание: {title}",
"Пора: {title}",
"Не забудь: {title}",
"Через 5 минут: {title}",
"Скоро начало: {title}"
]


def pick_phrase(variants: List[str], *, title: str, time: str | None = None) -> str:
import random
base = random.choice(variants)
return base.format(title=title, time=time or "")


HELP_TEXT = (
"Привет! Я бот-напоминалка для турниров и событий.\n\n"
"Команды:\n"
"• /subscribe_tournaments — включить напоминания турниров в этом чате\n"
"• /unsubscribe_tournaments — выключить напоминания турниров\n"
"• /add — создать напоминание (мастер-диалог)\n"
"• /list — список напоминаний\n"
"• /delete <id> — удалить\n"
"• /pause <id> — поставить на паузу\n"
"• /resume <id> — возобновить\n"
"• /test — прислать пробное уведомление\n"
"• /schedule — показать ближайшие события\n"
)