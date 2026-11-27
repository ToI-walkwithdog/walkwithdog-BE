-- F-08 실시간 산책 확인 (보호소, SC-15)
-- 산책자 연락처 조회를 위한 users 테이블 전화번호 컬럼 추가
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(50);
