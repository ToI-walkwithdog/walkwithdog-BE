-- First walk pledge confirmations (Issue #33): user_pledge_acknowledgements.
--
-- Additive only: one new table, no change to existing tables or rows. Accounts that
-- exist before this file simply have no pledge record and are asked to confirm.
--
-- Order: after 018_user_consents.sql. It references only users(id). Without it the
-- rest of the API keeps working; PUT /users/me/pledge fails until it is applied.
--
-- Run with psql so an error rolls the transaction back:
--   psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/023_user_pledge_acknowledgements.sql
-- Re-running after a successful apply is a no-op.
BEGIN;

CREATE TABLE IF NOT EXISTS user_pledge_acknowledgements (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL CONSTRAINT fk_user_pledge_ack_user_id REFERENCES users(id),
    pledge_version VARCHAR(32) NOT NULL,
    acknowledged_at TIMESTAMPTZ NOT NULL,
    -- One row per user and version; it keeps the first confirmation time. Its index
    -- also serves lookups by user_id.
    CONSTRAINT uq_user_pledge_ack_user_version UNIQUE (user_id, pledge_version)
);

COMMIT;
