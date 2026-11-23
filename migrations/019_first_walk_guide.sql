-- F-14: First walk safety guide completion timestamp on users.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS first_walk_guide_completed_at TIMESTAMPTZ NULL;
