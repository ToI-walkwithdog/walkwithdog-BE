-- F-06 산책 중 지도·경로 기록 (walks 테이블에 마지막 위치 및 종료 사유 컬럼 추가)
ALTER TABLE walks ADD COLUMN IF NOT EXISTS last_latitude NUMERIC(9, 6);
ALTER TABLE walks ADD COLUMN IF NOT EXISTS last_longitude NUMERIC(9, 6);
ALTER TABLE walks ADD COLUMN IF NOT EXISTS last_accuracy_m NUMERIC(10, 2);
ALTER TABLE walks ADD COLUMN IF NOT EXISTS last_point_recorded_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE walks ADD COLUMN IF NOT EXISTS last_point_received_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE walks ADD COLUMN IF NOT EXISTS end_reason VARCHAR(20);

-- 제약조건 추가: 위도/경도 쌍 유효성 및 범위, 정확도, 종료 사유
ALTER TABLE walks DROP CONSTRAINT IF EXISTS ck_walk_last_coordinate_pair;
ALTER TABLE walks ADD CONSTRAINT ck_walk_last_coordinate_pair
    CHECK ((last_latitude IS NULL) = (last_longitude IS NULL));

ALTER TABLE walks DROP CONSTRAINT IF EXISTS ck_walk_last_latitude;
ALTER TABLE walks ADD CONSTRAINT ck_walk_last_latitude
    CHECK (last_latitude IS NULL OR (last_latitude BETWEEN -90 AND 90));

ALTER TABLE walks DROP CONSTRAINT IF EXISTS ck_walk_last_longitude;
ALTER TABLE walks ADD CONSTRAINT ck_walk_last_longitude
    CHECK (last_longitude IS NULL OR (last_longitude BETWEEN -180 AND 180));

ALTER TABLE walks DROP CONSTRAINT IF EXISTS ck_walk_last_accuracy;
ALTER TABLE walks ADD CONSTRAINT ck_walk_last_accuracy
    CHECK (last_accuracy_m IS NULL OR last_accuracy_m >= 0);

ALTER TABLE walks DROP CONSTRAINT IF EXISTS ck_walk_end_reason;
ALTER TABLE walks ADD CONSTRAINT ck_walk_end_reason
    CHECK (end_reason IS NULL OR end_reason IN ('USER', 'STAFF', 'AUTO_TIMEOUT'));
