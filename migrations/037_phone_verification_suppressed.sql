-- Issue #110: a send request for a number registered to another account is accepted like
-- any other but stored as SUPPRESSED: never sent and never verifiable. Only the status CHECK
-- of phone_verifications (024) changes; existing rows are not touched. Re-running the file
-- leaves the same definition.
BEGIN;

ALTER TABLE phone_verifications DROP CONSTRAINT IF EXISTS ck_phone_verifications_send_status;
ALTER TABLE phone_verifications ADD CONSTRAINT ck_phone_verifications_send_status
    CHECK (send_status IN ('PENDING', 'SENDING', 'SENT', 'UNKNOWN', 'FAILED', 'SUPPRESSED'));

COMMIT;
