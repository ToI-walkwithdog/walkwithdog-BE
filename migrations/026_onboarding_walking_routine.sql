-- Onboarding Step 3 walking routine (Issue #44).
--
-- Additive only: three new tables, no change to existing tables or rows.
--   volunteer_onboarding_routines  one row per user; a row means Step 3 is complete
--   volunteer_routine_regions      chosen walk areas (1 to 3, checked by the server)
--   volunteer_routine_time_slots   available time slots (1 to 4, checked by the server)
-- Codes: duration UP_TO_30_MIN (30 minutes or less), BETWEEN_30_AND_60_MIN (more than 30,
-- less than 60), OVER_60_MIN (60 or more), per the Notion Step 3 save spec of 2026-11-24.
-- Comfortable sizes use the Step 2 size codes; time slots are the four fixed codes.
--
-- Order: after 022_regions.sql (region FK) and 025_onboarding_dog_experience.sql.
-- The current API reads volunteer_onboarding_routines to find the next onboarding step,
-- so apply this file before starting code that includes Issue #44.
--
-- Run with psql so an error rolls the transaction back:
--   psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/026_onboarding_walking_routine.sql
-- Re-running after a successful apply is a no-op.
-- Rollback (only before any Step 3 answer is stored, after a backup):
--   DROP TABLE IF EXISTS volunteer_routine_time_slots, volunteer_routine_regions, volunteer_onboarding_routines;
BEGIN;

CREATE TABLE IF NOT EXISTS volunteer_onboarding_routines (
    user_id INTEGER PRIMARY KEY CONSTRAINT fk_onboarding_routines_user_id REFERENCES users(id),
    walking_duration_code VARCHAR(30) NOT NULL,
    comfortable_dog_size_code VARCHAR(20) NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_onboarding_routine_duration
        CHECK (walking_duration_code IN ('UP_TO_30_MIN', 'BETWEEN_30_AND_60_MIN', 'OVER_60_MIN')),
    CONSTRAINT ck_onboarding_routine_dog_size CHECK (comfortable_dog_size_code IN ('SMALL', 'MEDIUM', 'LARGE'))
);

CREATE TABLE IF NOT EXISTS volunteer_routine_regions (
    user_id INTEGER NOT NULL
        CONSTRAINT fk_routine_regions_routine REFERENCES volunteer_onboarding_routines(user_id),
    region_code VARCHAR(20) NOT NULL
        CONSTRAINT fk_routine_regions_region_code REFERENCES regions(region_code),
    CONSTRAINT pk_volunteer_routine_regions PRIMARY KEY (user_id, region_code)
);

CREATE TABLE IF NOT EXISTS volunteer_routine_time_slots (
    user_id INTEGER NOT NULL
        CONSTRAINT fk_routine_time_slots_routine REFERENCES volunteer_onboarding_routines(user_id),
    time_slot_code VARCHAR(40) NOT NULL,
    CONSTRAINT pk_volunteer_routine_time_slots PRIMARY KEY (user_id, time_slot_code),
    CONSTRAINT ck_routine_time_slot_code
        CHECK (time_slot_code IN ('WEEKDAY_MORNING', 'WEEKDAY_AFTERNOON', 'WEEKEND_MORNING', 'WEEKEND_AFTERNOON'))
);

COMMIT;
