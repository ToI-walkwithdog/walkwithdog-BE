-- Issue #78: store onboarding policy agreements with the walking routine.
-- Run after 018 and 026. Older routine rows keep NULL versions and their original completion.
BEGIN;

ALTER TABLE user_consents
    DROP CONSTRAINT ck_user_consents_consent_type,
    ADD CONSTRAINT ck_user_consents_consent_type
        CHECK (consent_type IN ('PRIVACY_COLLECTION_USE', 'AI_OVERSEAS_TRANSFER', 'TERMS_OF_SERVICE'));

ALTER TABLE volunteer_onboarding_routines
    ADD COLUMN IF NOT EXISTS terms_document_version VARCHAR(32),
    ADD COLUMN IF NOT EXISTS privacy_document_version VARCHAR(32);

COMMIT;
