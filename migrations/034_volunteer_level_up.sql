-- Each completed evaluation credits one walk. Existing levels and statistics are not backfilled.
BEGIN;

CREATE TABLE IF NOT EXISTS volunteer_level_evaluations (
    walk_id INTEGER PRIMARY KEY REFERENCES walks (id) ON DELETE NO ACTION,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE NO ACTION,
    status VARCHAR(9) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    evaluated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    next_retry_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_volunteer_level_evaluations_walk_user UNIQUE (walk_id, user_id),
    CONSTRAINT ck_volunteer_level_evaluation_state CHECK (
        (status = 'COMPLETED' AND next_retry_at IS NULL) OR (status = 'FAILED' AND next_retry_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_volunteer_level_evaluations_user_status
    ON volunteer_level_evaluations (user_id, status);

CREATE TABLE IF NOT EXISTS volunteer_level_events (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    walk_id INTEGER NOT NULL,
    before_level INTEGER NOT NULL,
    after_level INTEGER NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    earned_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT fk_volunteer_level_events_evaluation
        FOREIGN KEY (walk_id, user_id) REFERENCES volunteer_level_evaluations (walk_id, user_id) ON DELETE NO ACTION,
    CONSTRAINT uq_volunteer_level_events_user_walk UNIQUE (user_id, walk_id),
    CONSTRAINT ck_volunteer_level_event_increase
        CHECK (before_level >= 1 AND after_level > before_level AND after_level <= 5)
);

COMMIT;
