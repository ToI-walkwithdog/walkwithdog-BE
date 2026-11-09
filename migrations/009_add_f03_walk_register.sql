-- 1. 강아지 나이 '개월' 컬럼 추가 (기존 호환 유지)
ALTER TABLE dogs ADD COLUMN age_months INTEGER NOT NULL DEFAULT 0;

-- 2. 난이도 'INTERMEDIATE' 확장
-- (주의: 기존 difficulty가 VARCHAR CHECK로 구현된 경우)
ALTER TABLE dog_walk_settings DROP CONSTRAINT IF EXISTS dog_walk_settings_difficulty_check;
ALTER TABLE dog_walk_settings ADD CONSTRAINT dog_walk_settings_difficulty_check 
    CHECK (difficulty IN ('BEGINNER', 'INTERMEDIATE', 'EXPERIENCED'));

-- 3. 날짜 기반 산책 슬롯 테이블 신설
CREATE TABLE dog_walk_date_slots (
    id SERIAL PRIMARY KEY,
    dog_id INTEGER NOT NULL REFERENCES dogs(id) ON DELETE CASCADE,
    slot_date DATE NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes IN (30, 60, 90)),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_dog_walk_date_slots_dog_date ON dog_walk_date_slots(dog_id, slot_date);