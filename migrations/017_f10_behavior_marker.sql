-- F-10 행동마커 저장 (SC-07)

-- 1. 마커 유형에 CROSSWALK_WAIT, OTHER 추가 (기존 5종은 하위 호환으로 유지)
ALTER TABLE walk_markers DROP CONSTRAINT IF EXISTS ck_marker_type;
ALTER TABLE walk_markers ADD CONSTRAINT ck_marker_type CHECK (marker_type IN
    ('SNIFF', 'POOP', 'DOG_ENCOUNTER', 'PERSON_ENCOUNTER', 'BARKING', 'CROSSWALK_WAIT', 'OTHER'));

-- 2. 위치 출처 (CURRENT: 요청 좌표, LAST_KNOWN: GPS 약함으로 마지막 위치 대체). 기존 행은 CURRENT
ALTER TABLE walk_markers ADD COLUMN IF NOT EXISTS location_source VARCHAR(12) NOT NULL DEFAULT 'CURRENT';
ALTER TABLE walk_markers DROP CONSTRAINT IF EXISTS ck_location_source;
ALTER TABLE walk_markers ADD CONSTRAINT ck_location_source CHECK (location_source IN ('CURRENT', 'LAST_KNOWN'));

-- 3. 기타(OTHER)는 내용이 비어 있으면 저장 불가
ALTER TABLE walk_markers DROP CONSTRAINT IF EXISTS ck_marker_other_memo;
ALTER TABLE walk_markers ADD CONSTRAINT ck_marker_other_memo CHECK
    (marker_type <> 'OTHER' OR (memo IS NOT NULL AND trim(memo) <> ''));
