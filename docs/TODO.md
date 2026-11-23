# F-10 행동마커 저장 (SC-07) TODO

> F-03~F-09 이력은 `docs/team/TODO.md`를 본다. 이 파일은 F-10 브랜치(`feature/f-10-behavior-marker`) 작업만 다룬다.

- [x] DB 마이그레이션 (`migrations/017_f10_behavior_marker.sql`): 마커 유형 `CROSSWALK_WAIT`, `OTHER` 추가, `location_source` 컬럼, OTHER 메모 필수 제약
- [x] `WalkMarker` 모델 제약·컬럼 반영 (`app/walk_models.py`)
- [x] `POST /walks/{walk_id}/markers` 확장: 좌표 선택, `accuracy_m`, OTHER 메모 필수(공백 제거, 최대 100자)
- [x] GPS 약함(좌표 없음 또는 `accuracy_m` 50m 초과) 시 `walks.last_*` 마지막 위치로 저장, `location_source` 기록
- [x] 마지막 위치도 없으면 409, 산책당 마커 500개 상한(409)
- [x] 보호소 실시간 화면(F-08)에 `BEHAVIOR` 마커 추가 (START 먼저, 나머지 시각순)
- [x] `.env.example`에 `WALK_MARKER_MAX_ACCURACY_M`, `WALK_MARKER_MAX_PER_WALK` 추가
- [x] 완료 기준 검증 테스트 작성 (`tests/test_f10_behavior_marker.py` 15건 통과)
- [x] `tests/test_walk_domain.py` 007 DDL 프로젝션 보정 (017 추가분 제외)
- [ ] (사용자) 로컬 PostgreSQL에 017 적용 후 확인 (`handoff` 6번 참고)
- [ ] (사용자) 커밋·push 후 PR 생성 (feature → dev)
- [ ] (앱) BottomSheetDialog, 지도 Marker 렌더링, `location_source=LAST_KNOWN` 표시 방식 결정
