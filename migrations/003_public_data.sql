-- Only additive columns; no backfill, deletion or historical analysis changes.
BEGIN;
ALTER TABLE shelters ADD COLUMN IF NOT EXISTS external_code VARCHAR(50);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shelters_external_code ON shelters(external_code);
ALTER TABLE dogs ADD COLUMN IF NOT EXISTS external_data JSON;
ALTER TABLE dogs ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ;
ALTER TABLE dogs ADD COLUMN IF NOT EXISTS process_state VARCHAR(50);
ALTER TABLE dogs ADD COLUMN IF NOT EXISTS neuter_status VARCHAR(10);
COMMIT;
