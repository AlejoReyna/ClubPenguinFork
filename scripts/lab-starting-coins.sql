-- Apply on existing Postgres volumes (init only runs 002 on first cluster create).
ALTER TABLE penguin ALTER COLUMN coins SET DEFAULT 10000;
UPDATE penguin SET coins = 10000;
