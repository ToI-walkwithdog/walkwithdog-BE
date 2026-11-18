-- 1. 위험 상황 신고 유형 7종으로 체크 제약조건 교체
ALTER TABLE walk_incidents DROP CONSTRAINT IF EXISTS ck_incident_type;
ALTER TABLE walk_incidents DROP CONSTRAINT IF EXISTS walk_incidents_incident_type_check;
ALTER TABLE walk_incidents ADD CONSTRAINT ck_incident_type
    CHECK (incident_type IN ('INJURY', 'LOST_DOG', 'SAFETY_THREAT', 'OTHER', 'POOP_NO_BAG', 'ATE_FOREIGN_OBJECT', 'ABNORMAL_BEHAVIOR'));

-- 2. walk_incidents 테이블 신규 컬럼 추가
ALTER TABLE walk_incidents ADD COLUMN IF NOT EXISTS client_report_id VARCHAR(36);
ALTER TABLE walk_incidents ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
ALTER TABLE walk_incidents ADD COLUMN IF NOT EXISTS acknowledged_by INTEGER REFERENCES users(id) ON DELETE NO ACTION;

-- 3. 확인 시각과 확인자 쌍 제약조건
ALTER TABLE walk_incidents DROP CONSTRAINT IF EXISTS ck_incident_acknowledgement;
ALTER TABLE walk_incidents DROP CONSTRAINT IF EXISTS walk_incidents_acknowledgement_check;
ALTER TABLE walk_incidents ADD CONSTRAINT ck_incident_acknowledgement
    CHECK ((acknowledged_at IS NULL AND acknowledged_by IS NULL) OR (acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL));

-- 4. 클라이언트 전송 ID 유니크 부분 인덱스 (client_report_id가 NULL이 아닌 경우 사용자별 1건 유니크)
CREATE UNIQUE INDEX IF NOT EXISTS uq_walk_incidents_reported_client_report_id
    ON walk_incidents (reported_by, client_report_id)
    WHERE client_report_id IS NOT NULL;

-- 5. 미확인 알림 조회용 부분 인덱스
CREATE INDEX IF NOT EXISTS ix_walk_incidents_unacknowledged
    ON walk_incidents (acknowledged_at)
    WHERE acknowledged_at IS NULL;
