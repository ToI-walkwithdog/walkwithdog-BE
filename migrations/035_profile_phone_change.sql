-- Issue #103: profile version for concurrent profile edits, and the purpose of each SMS
-- verification so onboarding and profile phone-change proofs cannot be used for each other.
-- Additive only: existing users start at version 0 and existing requests are ONBOARDING.
-- Re-running the file is a no-op.
BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS profile_version INTEGER NOT NULL DEFAULT 0;

ALTER TABLE phone_verifications
    ADD COLUMN IF NOT EXISTS purpose VARCHAR(30) NOT NULL DEFAULT 'ONBOARDING';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_phone_verifications_purpose'
                   AND conrelid = 'phone_verifications'::regclass) THEN
        ALTER TABLE phone_verifications ADD CONSTRAINT ck_phone_verifications_purpose
            CHECK (purpose IN ('ONBOARDING', 'PROFILE_PHONE_CHANGE'));
    END IF;
END
$$;

COMMIT;
