-- Onboarding Step 1 and account status (Issue #40).
--
-- users: account_status (NOT NULL, default ACTIVE) and age_review_required_at.
--   Existing rows become ACTIVE through the column default; no other column or row
--   changes. users.phone (used by live walk, #28) is not touched.
-- New tables: volunteer_onboarding_basics, phone_verifications,
--   phone_verification_daily_counts. They start empty.
--
-- Order: after 019_user_pledge_acknowledgements.sql. The code from this change reads
-- users.account_status on every user lookup, so login fails until this file is
-- applied: stop the API, apply 016 to 020 in order, then start the new code.
--
-- Run with psql so an error rolls the transaction back:
--   psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/020_onboarding_basic_info.sql
-- Re-running after a successful apply is a no-op.
--
-- Rollback (only before any account changed state or saved Step 1; takes a backup first):
--   BEGIN;
--   DROP TABLE IF EXISTS phone_verification_daily_counts, phone_verifications, volunteer_onboarding_basics;
--   ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_account_status,
--     DROP COLUMN IF EXISTS age_review_required_at, DROP COLUMN IF EXISTS account_status;
--   COMMIT;
BEGIN;

-- ACCESS EXCLUSIVE briefly: the new NOT NULL column with a constant default is
-- metadata-only on PostgreSQL 11+, so existing rows are not rewritten.
ALTER TABLE users ADD COLUMN IF NOT EXISTS account_status VARCHAR(24) NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE users ADD COLUMN IF NOT EXISTS age_review_required_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'users'::regclass AND conname = 'ck_users_account_status') THEN
        ALTER TABLE users ADD CONSTRAINT ck_users_account_status
            CHECK (account_status IN ('ACTIVE', 'AGE_REVIEW_REQUIRED'));
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS volunteer_onboarding_basics (
    user_id INTEGER PRIMARY KEY CONSTRAINT fk_onboarding_basics_user_id REFERENCES users(id),
    full_name VARCHAR(10) NOT NULL,
    birth_date DATE NOT NULL,
    -- One account per verified number.
    phone_e164 VARCHAR(16) NOT NULL CONSTRAINT volunteer_onboarding_basics_phone_e164_key UNIQUE,
    phone_verified_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS phone_verifications (
    id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL CONSTRAINT fk_phone_verifications_user_id REFERENCES users(id),
    phone_e164 VARCHAR(16) NOT NULL,
    -- HMAC-SHA256 of the request ID and code; the code itself is never stored.
    code_digest VARCHAR(64) NOT NULL,
    send_status VARCHAR(16) NOT NULL,
    provider_message_id VARCHAR(100),
    provider_group_id VARCHAR(100),
    provider_request_key UUID NOT NULL,
    last_checked_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    attempt_count SMALLINT NOT NULL DEFAULT 0,
    verified_at TIMESTAMPTZ,
    verified_valid_until TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_phone_verifications_send_status
        CHECK (send_status IN ('PENDING', 'SENDING', 'SENT', 'UNKNOWN', 'FAILED')),
    CONSTRAINT ck_phone_verifications_attempt_count CHECK (attempt_count >= 0)
);
CREATE INDEX IF NOT EXISTS ix_phone_verifications_user_created ON phone_verifications (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_phone_verifications_phone_created ON phone_verifications (phone_e164, created_at);
CREATE INDEX IF NOT EXISTS ix_phone_verifications_expires_at ON phone_verifications (expires_at);

-- Daily send counters (Asia/Seoul date). subject is the user ID for USER and an HMAC
-- of the number for PHONE, so no phone number is stored here.
CREATE TABLE IF NOT EXISTS phone_verification_daily_counts (
    scope VARCHAR(8) NOT NULL,
    subject VARCHAR(64) NOT NULL,
    kst_date DATE NOT NULL,
    send_count SMALLINT NOT NULL,
    CONSTRAINT pk_phone_verification_daily_counts PRIMARY KEY (scope, subject, kst_date),
    CONSTRAINT ck_phone_verification_daily_counts_scope CHECK (scope IN ('USER', 'PHONE')),
    CONSTRAINT ck_phone_verification_daily_counts_send_count CHECK (send_count >= 0)
);

COMMIT;
