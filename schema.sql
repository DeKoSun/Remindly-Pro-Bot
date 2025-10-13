-- Database schema for the local SQLite storage used by the reminder bot.
-- Execute with: sqlite3 remindly.db < schema.sql

CREATE TABLE IF NOT EXISTS telegram_users (
    user_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    title TEXT,
    tournament_subscribed INTEGER NOT NULL DEFAULT 0,
    tz TEXT NOT NULL DEFAULT 'Europe/Moscow',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    cron_expr TEXT,
    remind_at TEXT,
    next_at TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS reminders_due_idx
    ON reminders(paused, remind_at, next_at);
