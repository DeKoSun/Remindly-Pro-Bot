import os
(chat_id, type_, title)
)
c.commit()


def set_tournament_subscription(chat_id: int, value: bool):
with get_conn() as c:
cur = c.cursor()
cur.execute(
"UPDATE chats SET tournament_subscribed = %s WHERE chat_id = %s",
(value, chat_id)
)
c.commit()


def get_tournament_subscribed_chats():
with get_conn() as c:
cur = c.cursor(cursor_factory=psycopg2.extras.DictCursor)
cur.execute("SELECT chat_id, tz FROM chats WHERE tournament_subscribed = true")
return cur.fetchall()


def create_reminder(owner_id: int, chat_id: int, title: str, schedule_kind: str, payload_json: dict, tz: str):
with get_conn() as c:
cur = c.cursor()
cur.execute(
"""
INSERT INTO reminders(owner_id, chat_id, title, schedule_kind, payload_json, tz)
VALUES (%s, %s, %s, %s, %s::jsonb, %s) RETURNING id
""",
(owner_id, chat_id, title, schedule_kind, psycopg2.extras.Json(payload_json), tz)
)
new_id = cur.fetchone()[0]
c.commit()
return new_id


def list_reminders(chat_id: int):
with get_conn() as c:
cur = c.cursor(cursor_factory=psycopg2.extras.DictCursor)
cur.execute(
"SELECT id, title, schedule_kind, payload_json, is_active FROM reminders WHERE chat_id = %s ORDER BY id",
(chat_id,)
)
return cur.fetchall()


def set_active(reminder_id: int, active: bool, chat_id: int):
with get_conn() as c:
cur = c.cursor()
cur.execute("UPDATE reminders SET is_active = %s WHERE id = %s AND chat_id = %s", (active, reminder_id, chat_id))
c.commit()


def delete_reminder(reminder_id: int, chat_id: int):
with get_conn() as c:
cur = c.cursor()
cur.execute("DELETE FROM reminders WHERE id = %s AND chat_id = %s", (reminder_id, chat_id))
c.commit()