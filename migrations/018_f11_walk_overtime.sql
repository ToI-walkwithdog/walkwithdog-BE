-- F-11 산책 시간 초과 경고 (SC-15)

-- 1. 예정 산책 소요시간(분) 및 예정 종료 시각 컬럼 추가 (기존 행 호환을 위해 NULL 허용)
ALTER TABLE walks ADD COLUMN IF NOT EXISTS planned_duration_minutes INTEGER NULL;
ALTER TABLE walks ADD COLUMN IF NOT EXISTS expected_end_at TIMESTAMPTZ NULL;

-- 2. 데이터 무결성 제약조건
ALTER TABLE walks DROP CONSTRAINT IF EXISTS ck_walk_planned_duration;
ALTER TABLE walks ADD CONSTRAINT ck_walk_planned_duration CHECK
    (planned_duration_minutes IS NULL OR planned_duration_minutes > 0);

ALTER TABLE walks DROP CONSTRAINT IF EXISTS ck_walk_expected_end_order;
ALTER TABLE walks ADD CONSTRAINT ck_walk_expected_end_order CHECK
    (expected_end_at IS NULL OR expected_end_at > started_at);
