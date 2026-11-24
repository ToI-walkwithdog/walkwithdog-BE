-- Issue #89: optional client retry key and position time for behavior markers.
-- Additive only; existing markers keep NULL in all three columns. Re-running the file is a no-op.
BEGIN;

ALTER TABLE walk_markers
    ADD COLUMN IF NOT EXISTS client_marker_id UUID,
    ADD COLUMN IF NOT EXISTS request_fingerprint VARCHAR(64),
    ADD COLUMN IF NOT EXISTS position_recorded_at TIMESTAMP WITH TIME ZONE;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_marker_client_key'
                   AND conrelid = 'walk_markers'::regclass) THEN
        ALTER TABLE walk_markers ADD CONSTRAINT ck_marker_client_key
            CHECK ((client_marker_id IS NULL) = (request_fingerprint IS NULL));
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_walk_markers_client_marker_id
    ON walk_markers (walk_id, client_marker_id) WHERE client_marker_id IS NOT NULL;

COMMIT;
