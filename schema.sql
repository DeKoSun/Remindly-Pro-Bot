-- Supabase uses Postgres; run this in the SQL editor or with psql.
chat_id bigint PRIMARY KEY,
type text NOT NULL, -- 'group', 'supergroup', etc.
title text,
tournament_subscribed boolean NOT NULL DEFAULT false,
tz text NOT NULL DEFAULT 'Europe/Moscow',
created_at timestamptz NOT NULL DEFAULT now(),
updated_at timestamptz NOT NULL DEFAULT now()
);


-- schedule_type: one_off | cron | preset
CREATE TYPE schedule_type AS ENUM ('one_off', 'cron', 'preset');


CREATE TABLE IF NOT EXISTS reminders (
id bigserial PRIMARY KEY,
owner_id bigint NOT NULL,
chat_id bigint NOT NULL,
title text NOT NULL,
schedule_kind schedule_type NOT NULL,
payload_json jsonb NOT NULL, -- stores cron expr, datetime, or preset name
tz text NOT NULL DEFAULT 'Europe/Moscow',
is_active boolean NOT NULL DEFAULT true,
next_run_at timestamptz,
last_fired_at timestamptz,
created_at timestamptz NOT NULL DEFAULT now(),
updated_at timestamptz NOT NULL DEFAULT now()
);


CREATE INDEX IF NOT EXISTS reminders_active_idx ON reminders(is_active, next_run_at);


CREATE TABLE IF NOT EXISTS runs (
id bigserial PRIMARY KEY,
reminder_id bigint NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
fired_at timestamptz NOT NULL,
status text NOT NULL, -- 'ok' | 'error'
error_text text,
created_at timestamptz NOT NULL DEFAULT now()
);


-- Simple trigger to auto-update updated_at
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
NEW.updated_at = now();
RETURN NEW;
END;
$$ LANGUAGE plpgsql;


DROP TRIGGER IF EXISTS trg_users_updated ON users;
CREATE TRIGGER trg_users_updated BEFORE UPDATE ON users
FOR EACH ROW EXECUTE PROCEDURE set_updated_at();


DROP TRIGGER IF EXISTS trg_chats_updated ON chats;
CREATE TRIGGER trg_chats_updated BEFORE UPDATE ON chats
FOR EACH ROW EXECUTE PROCEDURE set_updated_at();


DROP TRIGGER IF EXISTS trg_reminders_updated ON reminders;
CREATE TRIGGER trg_reminders_updated BEFORE UPDATE ON reminders
FOR EACH ROW EXECUTE PROCEDURE set_updated_at();