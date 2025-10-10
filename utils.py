import os
from aiogram.types import Message


DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Europe/Moscow")


async def is_admin(message: Message) -> bool:
if message.chat.type not in ("group", "supergroup"):
return False
bot = message.bot
admins = await bot.get_chat_administrators(message.chat.id)
return any(a.user.id == message.from_user.id for a in admins)