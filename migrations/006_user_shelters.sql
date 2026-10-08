-- Run through scripts.migrate_backend for structure checks and a verified backup.
-- No backfill: existing accounts retain their roles and receive NULL shelter_id.
ALTER TABLE users ADD COLUMN IF NOT EXISTS shelter_id INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attname = 'shelter_id'
        WHERE c.conrelid = 'users'::regclass AND c.contype = 'f'
          AND c.conkey = ARRAY[a.attnum]
    ) THEN
        ALTER TABLE users ADD CONSTRAINT fk_users_shelter_id
            FOREIGN KEY (shelter_id) REFERENCES shelters(id);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_users_shelter_id ON users (shelter_id);
