-- 1. 강아지 난이도 체크 제약조건을 3단계로 확장
ALTER TABLE dog_walk_settings DROP CONSTRAINT IF EXISTS ck_difficulty;
ALTER TABLE dog_walk_settings DROP CONSTRAINT IF EXISTS dog_walk_settings_difficulty_check;
ALTER TABLE dog_walk_settings ADD CONSTRAINT ck_difficulty 
    CHECK (difficulty IN ('BEGINNER', 'INTERMEDIATE', 'EXPERIENCED'));

-- 2. walk_requests 테이블에 거절 사유 코드 컬럼 추가
ALTER TABLE walk_requests ADD COLUMN IF NOT EXISTS rejection_reason_code VARCHAR(30);
