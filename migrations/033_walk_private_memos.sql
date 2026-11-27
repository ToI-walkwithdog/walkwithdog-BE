-- A walk may have one private memo. Existing walks need no backfill.
BEGIN;

CREATE TABLE IF NOT EXISTS walk_private_memos (
    id SERIAL PRIMARY KEY,
    walk_id INTEGER NOT NULL REFERENCES walks (id) ON DELETE NO ACTION,
    author_user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE NO ACTION,
    memo VARCHAR(1000) NOT NULL,
    version INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_walk_private_memos_walk UNIQUE (walk_id),
    CONSTRAINT ck_walk_private_memo_content CHECK (trim(memo) <> '' AND length(memo) <= 1000),
    CONSTRAINT ck_walk_private_memo_version CHECK (version >= 1)
);

COMMIT;
