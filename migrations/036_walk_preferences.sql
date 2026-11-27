-- Issue #105: my page walk preferences (/10-3). Duration and dog size only; the
-- onboarding Step 3 routine is a separate record and is not touched.
-- New table only. Depends on the base users table alone. Re-running the file is a no-op.
BEGIN;

CREATE TABLE IF NOT EXISTS volunteer_walk_preferences (
    user_id INTEGER NOT NULL,
    duration_code VARCHAR(30),
    size_code VARCHAR(20),
    version INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT volunteer_walk_preferences_pkey PRIMARY KEY (user_id),
    CONSTRAINT fk_walk_preferences_user_id FOREIGN KEY (user_id) REFERENCES users (id),
    CONSTRAINT ck_walk_preferences_duration
        CHECK (duration_code IN ('UP_TO_30_MIN', 'BETWEEN_30_AND_60_MIN', 'OVER_60_MIN')),
    CONSTRAINT ck_walk_preferences_size CHECK (size_code IN ('SMALL', 'MEDIUM', 'LARGE')),
    CONSTRAINT ck_walk_preferences_version CHECK (version >= 1)
);

COMMIT;
