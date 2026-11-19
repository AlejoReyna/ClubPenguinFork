-- Apply on existing Postgres volumes (init only runs 003 on first cluster create).
-- Idempotent; does not attach the gift to penguins that already exist.
CREATE TABLE IF NOT EXISTS penguin_welcome_puffle (
  penguin_id integer NOT NULL,
  puffle_instance_id integer NOT NULL,
  PRIMARY KEY (penguin_id),
  CONSTRAINT penguin_welcome_puffle_penguin_fkey
    FOREIGN KEY (penguin_id) REFERENCES penguin (id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT penguin_welcome_puffle_puffle_fkey
    FOREIGN KEY (puffle_instance_id) REFERENCES penguin_puffle (id)
    ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT penguin_welcome_puffle_instance_unique UNIQUE (puffle_instance_id)
);

COMMENT ON TABLE penguin_welcome_puffle IS 'Welcome puffle instance per penguin; gift for new accounts only';
COMMENT ON COLUMN penguin_welcome_puffle.penguin_id IS 'Owner penguin ID';
COMMENT ON COLUMN penguin_welcome_puffle.puffle_instance_id IS 'penguin_puffle.id of the protected welcome pet';
