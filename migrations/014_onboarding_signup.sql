-- Onboarding sign-up: login_id, case-insensitive unique account fields, NOT NULL password.
--
-- Applies only while users is EMPTY. login_id cannot be NOT NULL for existing
-- accounts without inventing IDs, which this migration never does. If users has
-- rows, the whole transaction aborts and nothing changes. Decide how to handle
-- existing accounts (and over-length or duplicate nicknames, NULL passwords)
-- first, with a verified backup, before applying.
--
-- Run with psql so an error rolls the transaction back:
--   psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/014_onboarding_signup.sql
BEGIN;

-- Block concurrent sign-ups between the emptiness check and the DDL.
LOCK TABLE users IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM users) THEN
        RAISE EXCEPTION '014: users has existing rows. Nothing was changed. Decide the existing-account plan first.';
    END IF;
END;
$$;

ALTER TABLE users ADD COLUMN IF NOT EXISTS login_id VARCHAR(20) NOT NULL;
ALTER TABLE users ALTER COLUMN nickname TYPE VARCHAR(20);
ALTER TABLE users ALTER COLUMN password SET NOT NULL;

-- Sign-up compares lower(value) with these same expressions. The existing
-- users_email_key constraint is kept as is.
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_login_id_lower ON users (lower(login_id));
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_nickname_lower ON users (lower(nickname));
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (lower(email));

COMMIT;
