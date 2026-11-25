-- Issue #5: additive, nullable GPS motion metrics. No legacy backfill.
-- Run through scripts.migrate_backend 008 (backup, locks and catalog checks).
ALTER TABLE public.walks ADD COLUMN IF NOT EXISTS moving_duration_seconds INTEGER;
ALTER TABLE public.walks ADD COLUMN IF NOT EXISTS stopped_duration_seconds INTEGER;
ALTER TABLE public.walks ADD COLUMN IF NOT EXISTS unknown_duration_seconds INTEGER;
ALTER TABLE public.walks ADD COLUMN IF NOT EXISTS moving_distance_m NUMERIC(12, 2);
DO $motion$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.walks'::regclass AND conname = 'ck_walk_motion_pace') THEN
        ALTER TABLE public.walks ADD CONSTRAINT ck_walk_motion_pace CHECK (moving_duration_seconds IS NULL OR average_pace IS NULL OR (moving_duration_seconds > 0 AND moving_distance_m > 0));
    END IF;
END
$motion$;
DO $motion$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.walks'::regclass AND conname = 'ck_walk_motion_partition') THEN
        ALTER TABLE public.walks ADD CONSTRAINT ck_walk_motion_partition CHECK ((moving_duration_seconds IS NULL AND stopped_duration_seconds IS NULL AND unknown_duration_seconds IS NULL AND moving_distance_m IS NULL) OR (moving_duration_seconds IS NOT NULL AND stopped_duration_seconds IS NOT NULL AND unknown_duration_seconds IS NOT NULL AND moving_distance_m IS NOT NULL AND duration_seconds IS NOT NULL AND total_distance_m IS NOT NULL AND ended_at IS NOT NULL AND status <> 'IN_PROGRESS' AND moving_duration_seconds >= 0 AND stopped_duration_seconds >= 0 AND unknown_duration_seconds >= 0 AND moving_distance_m >= 0 AND moving_distance_m <= total_distance_m AND CAST(moving_duration_seconds AS BIGINT) + CAST(stopped_duration_seconds AS BIGINT) + CAST(unknown_duration_seconds AS BIGINT) = duration_seconds));
    END IF;
END
$motion$;
