-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Chats
CREATE TABLE IF NOT EXISTS chats (
  chat_id       BIGINT PRIMARY KEY,
  type          TEXT NOT NULL,        -- "private", "group", "supergroup", "channel"
  title         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Reminders (универсальные + турнирные)
CREATE TABLE IF NOT EXISTS reminders (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  chat_id       BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
  user_id       BIGINT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('once','cron')),
  text          TEXT NOT NULL,
  remind_at     TIMESTAMPTZ,          -- для once
  cron_expr     TEXT,                  -- для cron
  next_at       TIMESTAMPTZ,          -- следующее срабатывание для cron
  paused        BOOLEAN NOT NULL DEFAULT FALSE,
  category      TEXT,                  -- NULL | 'tournament'
  meta          JSONB,                 -- произвольная мета
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Индексы для быстрого поиска "due"
CREATE INDEX IF NOT EXISTS idx_reminders_once_due
  ON reminders (remind_at)
  WHERE kind = 'once' AND paused = FALSE;

CREATE INDEX IF NOT EXISTS idx_reminders_cron_due
  ON reminders (next_at)
  WHERE kind = 'cron' AND paused = FALSE;

-- Турнирная подписка (фактический флаг для чата)
CREATE TABLE IF NOT EXISTS tournament_subscriptions (
  chat_id    BIGINT PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE,
  enabled    BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Утилиты
CREATE OR REPLACE FUNCTION touch_tournament_subscription()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_touch_tournament_subscription ON tournament_subscriptions;
CREATE TRIGGER trg_touch_tournament_subscription
BEFORE UPDATE ON tournament_subscriptions
FOR EACH ROW
EXECUTE FUNCTION touch_tournament_subscription();
