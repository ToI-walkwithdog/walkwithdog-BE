-- F-09 산책 완료·평가 (SC-08)

-- 1. walk_reviews 테이블 컬럼 추가
ALTER TABLE walk_reviews ADD COLUMN IF NOT EXISTS rating INTEGER;
ALTER TABLE walk_reviews ADD COLUMN IF NOT EXISTS comment VARCHAR(100);

-- 2. 별점 범위 체크 제약 (기존 레거시 행의 NULL 허용, 값이 있을 때 1~5)
ALTER TABLE walk_reviews DROP CONSTRAINT IF EXISTS ck_walk_review_rating;
ALTER TABLE walk_reviews DROP CONSTRAINT IF EXISTS walk_reviews_rating_check;
ALTER TABLE walk_reviews ADD CONSTRAINT ck_walk_review_rating
    CHECK (rating IS NULL OR rating BETWEEN 1 AND 5);

-- 3. 관찰 지표 또는 별점 필수 체크 제약 교체 (별점 또는 5개 지표 중 하나 이상 NOT NULL)
ALTER TABLE walk_reviews DROP CONSTRAINT IF EXISTS ck_review_observation;
ALTER TABLE walk_reviews DROP CONSTRAINT IF EXISTS walk_reviews_review_observation_check;
ALTER TABLE walk_reviews ADD CONSTRAINT ck_review_observation
    CHECK (rating IS NOT NULL OR leash_pulling IS NOT NULL OR handling_difficulty IS NOT NULL
           OR sociability_with_people IS NOT NULL OR sociability_with_dogs IS NOT NULL
           OR barking_level IS NOT NULL);

-- 4. 신규 walk_review_tags 테이블 생성
CREATE TABLE IF NOT EXISTS walk_review_tags (
    id SERIAL PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES walk_reviews(id) ON DELETE CASCADE,
    tag_code VARCHAR(30) NOT NULL,
    CONSTRAINT uq_walk_review_tags_review_code UNIQUE (review_id, tag_code),
    CONSTRAINT ck_walk_review_tag_code CHECK (
        tag_code IN ('ENERGETIC', 'CALM', 'EXCITED', 'TENSE', 'TIRED', 'SCARED', 'UNWELL')
    )
);

-- 5. 인덱스 생성
CREATE INDEX IF NOT EXISTS ix_walk_review_tags_review_id ON walk_review_tags(review_id);
