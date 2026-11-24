-- Add cancellation history without inferring values for existing reservations.
BEGIN;

ALTER TABLE walk_requests
    ADD COLUMN cancel_reason_code VARCHAR(20),
    ADD COLUMN cancelled_at TIMESTAMPTZ,
    ADD COLUMN cancelled_by INTEGER,
    ADD COLUMN cancelled_actor_role VARCHAR(10),
    ADD CONSTRAINT fk_walk_requests_cancelled_by FOREIGN KEY (cancelled_by) REFERENCES users(id) ON DELETE NO ACTION,
    ADD CONSTRAINT ck_cancel_reason_code CHECK (cancel_reason_code IN ('SCHEDULE_CHANGED', 'CANNOT_VISIT')),
    ADD CONSTRAINT ck_cancel_actor_role CHECK (cancelled_actor_role IN ('USER', 'SHELTER', 'ADMIN')),
    ADD CONSTRAINT ck_walk_request_cancellation CHECK (
        (cancelled_at IS NULL AND cancelled_by IS NULL AND cancelled_actor_role IS NULL AND cancel_reason_code IS NULL)
        OR (status = 'CANCELLED' AND cancelled_at IS NOT NULL AND cancelled_by IS NOT NULL
            AND cancelled_actor_role IS NOT NULL)
    );

COMMIT;
