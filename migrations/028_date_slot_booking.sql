BEGIN;

ALTER TABLE shelters ADD COLUMN region_code VARCHAR(20),
    ADD CONSTRAINT fk_shelters_region_code FOREIGN KEY (region_code) REFERENCES regions(region_code);
CREATE INDEX ix_shelters_region_code ON shelters(region_code);

ALTER TABLE dog_walk_settings
    ADD COLUMN size_code VARCHAR(10),
    ADD COLUMN difficulty_score NUMERIC(2,1),
    ADD COLUMN max_walk_minutes INTEGER,
    ADD CONSTRAINT ck_size_code CHECK (size_code IN ('SMALL', 'MEDIUM', 'LARGE')),
    ADD CONSTRAINT ck_walk_difficulty_score CHECK (
        difficulty_score BETWEEN 1 AND 5 AND difficulty_score * 2 = CAST(difficulty_score * 2 AS INTEGER)
    ),
    ADD CONSTRAINT ck_walk_max_minutes CHECK (max_walk_minutes > 0);

ALTER TABLE dog_walk_date_slots
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true,
    ADD CONSTRAINT ck_date_slot_time_order CHECK (start_time < end_time),
    ADD CONSTRAINT uq_date_slot_dog UNIQUE (id, dog_id);

-- Environments that manually installed the branch-only 010 keep their links.
-- Replace its cascading FK with a dog/slot FK that preserves all reservation history.
ALTER TABLE walk_requests ADD COLUMN IF NOT EXISTS date_slot_id INTEGER;
ALTER TABLE walk_requests DROP CONSTRAINT IF EXISTS walk_requests_date_slot_id_fkey;
DROP INDEX IF EXISTS idx_walk_requests_date_slot;
ALTER TABLE walk_requests
    ADD COLUMN idempotency_key UUID,
    ADD COLUMN request_fingerprint VARCHAR(64),
    ADD COLUMN creation_receipt JSON,
    ADD CONSTRAINT fk_walk_requests_date_slot FOREIGN KEY (date_slot_id, dog_id)
        REFERENCES dog_walk_date_slots(id, dog_id) ON DELETE NO ACTION,
    ADD CONSTRAINT ck_walk_request_receipt CHECK (
        (idempotency_key IS NULL AND request_fingerprint IS NULL AND creation_receipt IS NULL)
        OR (idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL
            AND creation_receipt IS NOT NULL AND date_slot_id IS NOT NULL)
    );
CREATE INDEX ix_walk_requests_date_slot ON walk_requests(date_slot_id);
CREATE UNIQUE INDEX uq_walk_requests_user_idempotency ON walk_requests(user_id, idempotency_key);
CREATE UNIQUE INDEX uq_walk_requests_occupied_date_slot ON walk_requests(date_slot_id)
    WHERE date_slot_id IS NOT NULL AND status IN ('PENDING', 'APPROVED');

COMMIT;
