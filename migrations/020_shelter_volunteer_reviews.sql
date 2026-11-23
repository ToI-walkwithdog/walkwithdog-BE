-- F-15: Shelter-to-volunteer review and tags (one review per completed walk, immutable after creation).
CREATE TABLE IF NOT EXISTS shelter_volunteer_reviews (
    id SERIAL PRIMARY KEY,
    walk_id INTEGER NOT NULL REFERENCES walks(id) ON DELETE NO ACTION,
    shelter_id INTEGER NOT NULL REFERENCES shelters(id) ON DELETE NO ACTION,
    volunteer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE NO ACTION,
    reviewed_by INTEGER NOT NULL REFERENCES users(id) ON DELETE NO ACTION,
    rating NUMERIC(2, 1) NOT NULL,
    comment VARCHAR(500) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shelter_volunteer_reviews_walk UNIQUE (walk_id),
    CONSTRAINT ck_shelter_volunteer_review_rating
        CHECK (rating >= 0.5 AND rating <= 5.0 AND CAST(ROUND(rating * 10) AS INTEGER) % 5 = 0),
    CONSTRAINT ck_shelter_volunteer_review_comment_len
        CHECK (comment IS NULL OR length(comment) <= 500)
);

CREATE INDEX IF NOT EXISTS ix_shelter_volunteer_reviews_volunteer
    ON shelter_volunteer_reviews (volunteer_user_id, created_at);

CREATE INDEX IF NOT EXISTS ix_shelter_volunteer_reviews_shelter
    ON shelter_volunteer_reviews (shelter_id, created_at);

CREATE TABLE IF NOT EXISTS shelter_volunteer_review_tags (
    id SERIAL PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES shelter_volunteer_reviews(id) ON DELETE CASCADE,
    tag_code VARCHAR(30) NOT NULL,
    CONSTRAINT uq_shelter_volunteer_review_tags_review_code UNIQUE (review_id, tag_code),
    CONSTRAINT ck_tag_code CHECK (tag_code IN ('PUNCTUALITY', 'SAFETY_CARE', 'CONSIDERATE', 'DISAPPOINTING'))
);

CREATE INDEX IF NOT EXISTS ix_shelter_volunteer_review_tags_review_id
    ON shelter_volunteer_review_tags (review_id);
