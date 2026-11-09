-- walk_requests 테이블에 F-03 날짜 슬롯 외래키 추가
ALTER TABLE walk_requests ADD COLUMN date_slot_id INTEGER REFERENCES dog_walk_date_slots(id) ON DELETE CASCADE;

-- 조회를 위한 인덱스 추가
CREATE INDEX idx_walk_requests_date_slot ON walk_requests(date_slot_id);