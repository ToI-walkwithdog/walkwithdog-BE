-- Sign-up privacy confirmation (Issue #31): users.age_14_plus_confirmed_at and user_consents.
--
-- Additive only. Existing rows are not changed: the new column stays NULL for
-- accounts created before #31 (their declaration time is unknown and must not be
-- invented), and user_consents starts empty.
--
-- Order: after 017_remove_user_email.sql. It does not depend on 017's changes, but
-- the code that writes these objects ships after 016 and 017, so apply 016, 017,
-- then 018 before starting that code. The new code selects users.age_14_plus_confirmed_at
-- on every user lookup, so login also fails until this file is applied.
--
-- Run with psql so an error rolls the transaction back:
--   psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/018_user_consents.sql
-- Re-running after a successful apply is a no-op.
BEGIN;

ALTER TABLE users ADD COLUMN IF NOT EXISTS age_14_plus_confirmed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS user_consents (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL CONSTRAINT fk_user_consents_user_id REFERENCES users(id),
    consent_type VARCHAR(40) NOT NULL,
    document_version VARCHAR(32) NOT NULL,
    action VARCHAR(16) NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_user_consents_consent_type
        CHECK (consent_type IN ('PRIVACY_COLLECTION_USE', 'AI_OVERSEAS_TRANSFER')),
    CONSTRAINT ck_user_consents_action CHECK (action IN ('AGREE', 'WITHDRAW'))
);

CREATE INDEX IF NOT EXISTS ix_user_consents_user_type_recorded
    ON user_consents (user_id, consent_type, recorded_at);

COMMIT;
