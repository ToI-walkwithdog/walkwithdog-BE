-- Remove users.email (Issues #30, #32): email is no longer collected or used to log in.
--
-- Deploy together with the code that logs in by login_id. Code before this change
-- reads users.email and the new code no longer writes it, so neither works on the
-- other's schema. Stop the API, apply this file, then start the new code.
--
-- Checked below. If any check fails, the whole transaction aborts and nothing changes:
--   * 016 is applied: users.login_id is NOT NULL and uq_users_login_id_lower exists.
--   * Every account has a non-blank login_id. Accounts without one need the approved
--     login_id mapping first; this migration never invents or rewrites IDs.
-- Not checked here, do it first:
--   * A verified pg_dump backup of users. Dropping the column discards every email.
-- No CASCADE: if a view or other object still uses users.email, DROP COLUMN fails
-- and the transaction rolls back instead of removing that object silently.
--
-- Run with psql so an error rolls the transaction back:
--   psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/017_remove_user_email.sql
-- Re-running after a successful apply is a no-op.
BEGIN;

-- Block sign-ups and logins between the checks and the DDL.
LOCK TABLE users IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'users'
                     AND column_name = 'login_id' AND is_nullable = 'NO') THEN
        RAISE EXCEPTION '017: users.login_id is missing or nullable. Apply 016 or the approved login_id conversion first. Nothing was changed.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = current_schema() AND tablename = 'users'
                     AND indexname = 'uq_users_login_id_lower'
                     AND indexdef LIKE 'CREATE UNIQUE INDEX%') THEN
        RAISE EXCEPTION '017: unique index uq_users_login_id_lower is missing. Nothing was changed.';
    END IF;
    IF EXISTS (SELECT 1 FROM users WHERE btrim(login_id) = '') THEN
        RAISE EXCEPTION '017: users has accounts with a blank login_id. Apply the approved login_id mapping first. Nothing was changed.';
    END IF;
END;
$$;

DROP INDEX IF EXISTS uq_users_email_lower;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key;
ALTER TABLE users DROP COLUMN IF EXISTS email;

COMMIT;
