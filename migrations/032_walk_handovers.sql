-- Existing walks remain legacy records. Only the application start flow opts a new walk into handover.
BEGIN;

ALTER TABLE walks ADD COLUMN IF NOT EXISTS handover_required BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS walk_handovers (
    id SERIAL PRIMARY KEY,
    walk_id INTEGER NOT NULL REFERENCES walks (id) ON DELETE NO ACTION,
    volunteer_user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE NO ACTION,
    handover_result VARCHAR(8) NOT NULL,
    dog_returned BOOLEAN NOT NULL,
    equipment_returned BOOLEAN NOT NULL,
    condition_shared BOOLEAN NOT NULL,
    acknowledge_auto_timeout BOOLEAN NOT NULL DEFAULT FALSE,
    issue_description VARCHAR(500),
    self_confirmed_at TIMESTAMP WITH TIME ZONE,
    issue_reported_at TIMESTAMP WITH TIME ZONE,
    level_evaluation_pending_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_walk_handovers_walk UNIQUE (walk_id),
    CONSTRAINT ck_walk_handover_result CHECK (
        (handover_result = 'COMPLETE' AND dog_returned AND equipment_returned AND condition_shared
         AND issue_description IS NULL AND self_confirmed_at IS NOT NULL AND issue_reported_at IS NULL)
        OR
        (handover_result = 'ISSUE' AND NOT (dog_returned AND equipment_returned AND condition_shared)
         AND issue_description IS NOT NULL AND trim(issue_description) <> '' AND length(issue_description) <= 500
         AND self_confirmed_at IS NULL AND issue_reported_at IS NOT NULL)
    ),
    CONSTRAINT ck_walk_handover_pending CHECK (
        level_evaluation_pending_at IS NULL OR handover_result = 'COMPLETE'
    )
);

COMMIT;
