-- Onboarding Step 2 dog experience (Issue #42).
--
-- Additive only: two new tables, no change to existing tables or rows.
--   volunteer_onboarding_experiences  one row per user; a row means Step 2 is complete
--   volunteer_experience_challenges   "어려웠던 상황" choices, one row per code
-- No size link table (2026-11-23 decision: one dog_size_code per answer).
--
-- Order: after 020_onboarding_basic_info.sql in this branch stack. Note: origin/dev
-- now also has 017_f10_behavior_marker.sql and 018_f11_walk_overtime.sql, so the
-- onboarding files 017 to 021 must be renumbered before they reach dev (see the PR).
-- Without this file the rest of the API keeps working; the Step 2 routes fail, and so
-- does the sign-up exit (#41), which deletes from these tables.
--
-- Run with psql so an error rolls the transaction back:
--   psql -v ON_ERROR_STOP=1 -d <DB_NAME> -f migrations/021_onboarding_dog_experience.sql
-- Re-running after a successful apply is a no-op.
-- Rollback (only before any Step 2 answer is stored, after a backup):
--   DROP TABLE IF EXISTS volunteer_experience_challenges, volunteer_onboarding_experiences;
BEGIN;

CREATE TABLE IF NOT EXISTS volunteer_onboarding_experiences (
    user_id INTEGER PRIMARY KEY CONSTRAINT fk_onboarding_experiences_user_id REFERENCES users(id),
    has_experience BOOLEAN NOT NULL,
    experienced_breeds_text VARCHAR(200),
    dog_size_code VARCHAR(10),
    walking_frequency VARCHAR(20),
    perceived_difficulty VARCHAR(20),
    completed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_onboarding_experience_dog_size CHECK (dog_size_code IN ('SMALL', 'MEDIUM', 'LARGE')),
    CONSTRAINT ck_onboarding_experience_frequency
        CHECK (walking_frequency IN ('DAILY', 'WEEKLY_3_4', 'WEEKLY_1_2', 'RARELY')),
    CONSTRAINT ck_onboarding_experience_difficulty CHECK (perceived_difficulty IN ('EASY', 'NORMAL', 'HARD')),
    -- Details are all present with experience and all absent without it.
    CONSTRAINT ck_onboarding_experience_details CHECK (
        (has_experience AND experienced_breeds_text IS NOT NULL AND dog_size_code IS NOT NULL
         AND walking_frequency IS NOT NULL AND perceived_difficulty IS NOT NULL)
        OR (NOT has_experience AND experienced_breeds_text IS NULL AND dog_size_code IS NULL
            AND walking_frequency IS NULL AND perceived_difficulty IS NULL))
);

CREATE TABLE IF NOT EXISTS volunteer_experience_challenges (
    user_id INTEGER NOT NULL
        CONSTRAINT fk_experience_challenges_experience REFERENCES volunteer_onboarding_experiences(user_id),
    challenge_code VARCHAR(30) NOT NULL,
    CONSTRAINT pk_volunteer_experience_challenges PRIMARY KEY (user_id, challenge_code),
    CONSTRAINT ck_experience_challenge_code
        CHECK (challenge_code IN ('LEASH_PULLING', 'BARKING_EXCITEMENT', 'OTHER_DOG_REACTION', 'NONE'))
);

COMMIT;
