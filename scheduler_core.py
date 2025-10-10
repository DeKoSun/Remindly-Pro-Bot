import os
from datetime import datetime, time
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from aiogram import Bot
from texts import pick_phrase, TOURNAMENT_VARIANTS
from db import get_tournament_subscribed_chats


DEFAULT_TZ = os.getenv("DEFAULT_TZ", "America/New_York")
TITLE_TOURNAMENT = "Быстрый турнир"


# Турнирные времена (напоминание за 5 минут до начала):
# Начала: 13:00, 15:00, 17:00, 19:00, 21:00, 23:00 — значит уведомления: 13:55, 15:55, 17:55, 19:55, 21:55, 23:55
TOURNAMENT_MINUTES = [
(13, 55), (15, 55), (17, 55), (19, 55), (21, 55), (23, 55)
]


class TournamentScheduler:
def __init__(self, bot: Bot):
self.bot = bot
self.scheduler = AsyncIOScheduler(
jobstores={"default": MemoryJobStore()},
executors={"default": AsyncIOExecutor()},
job_defaults={"misfire_grace_time": 86400}, # Ловим "пропуски" за сутки
timezone=pytz.timezone(DEFAULT_TZ)
)


def start(self):
self.scheduler.start()
# Ежеминутная проверка чатов и регистрация джобов по cron (без дублей)
self.scheduler.add_job(self._ensure_tournament_jobs, CronTrigger.from_crontab("*/5 * * * *", timezone=self.scheduler.timezone))
# Быстрый запуск при старте
self.scheduler.add_job(self._ensure_tournament_jobs, next_run_time=datetime.now(self.scheduler.timezone))


async def _send_tournament(self, chat_id: int, notify_time: time):
# notify_time — это время начала турнира (10:00 и т.п.), мы показываем его в тексте
start_str = notify_time.strftime("%H:%M")
text = pick_phrase(TOURNAMENT_VARIANTS, title=TITLE_TOURNAMENT, time=start_str)
await self.bot.send_message(chat_id, text)


def _register_daily_jobs_for_chat(self, chat_id: int, tz_name: str):
tz = pytz.timezone(tz_name or DEFAULT_TZ)
# Создаём 5 ежедневных cron-задач (дедупликация через job_id)
for hour, minute in TOURNAMENT_MINUTES:
start_hour = hour + (0 if minute == 55 else 0) # actual start hours mapping already embedded
# Соответствующее реальное начало турнира для текста
start_display = { (13,55): (14,0), (15,55): (16,0), (17,55): (18,0), (19,55): (20,0), (21,55): (22,0), (23,55): (24,0) }[(hour,minute)]
job_id = f"tour_{chat_id}_{hour:02d}{minute:02d}"
# Удаляем старую, если есть (обновление tz/cron)
old = self.scheduler.get_job(job_id)
if old:
old.remove()
self.scheduler.add_job(
self._send_tournament,
CronTrigger(hour=hour, minute=minute, timezone=tz),
id=job_id,
args=[chat_id, time(*start_display)]
)


def _ensure_tournament_jobs(self):
# читаем все подписанные чаты и гарантируем наличие джобов
rows = get_tournament_subscribed_chats()
for r in rows:
self._register_daily_jobs_for_chat(r[0], r[1])