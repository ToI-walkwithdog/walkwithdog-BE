# 현재 위치 행정구역 판별 인수인계

- 활성 일반 사용자 세션으로 `POST /regions/resolve-location`에 `latitude`, `longitude`를 JSON 숫자로 보낸다. 성공하면 `data.region`에서 `sido_code`, `sido_name`, `region_code`, `region_name`을 받는다. 이름과 코드는 기존 지역 마스터에서 가져오며 `36110`은 세종특별자치시 전체(`36_SPECIAL_ALL`)로 매핑한다.
- 위치 권한이 거부되거나 좌표를 얻지 못하면 FE는 API를 건너뛰고 수동 지역 선택을 제공한다. `422 REGION_NOT_SUPPORTED` 또는 `503 GEOCODING_UNAVAILABLE`도 수동 선택으로 이어진다. 판별 결과는 선택 후보로 사용하며 기존 선택과 중복되면 유지한다. 지역이 세 개면 기존 선택을 유지하고 조정을 안내한다. FE는 후보 수정과 삭제를 제공한다. 산책 루틴 저장과 온보딩 완료는 위치 조회와 별개다.
- 개발 연동에는 `APP_ENV=local`, `development`, `test` 중 하나와 `LOCATION_GEOCODING_PROVIDER=mock`을 명시한다. Mock 응답에는 기존 지역 마스터의 활성 `11680`이 필요하다. 실제 키 없이 동작하며 데이터베이스 쓰기와 외부 호출은 없다. `LOCATION_GEOCODING_PROVIDER`가 비어 있거나 운영에서 Mock을 지정하면 503을 반환한다.
- 실제 연동은 위치정보 처리 목적과 외부 제공 고지, 필요한 동의 절차를 승인하고 적용한 뒤에 진행한다. 운영 설정에서 `LOCATION_GEOCODING_PROVIDER=kakao`, `KAKAO_REST_API_KEY`, `LOCATION_EXTERNAL_TRANSFER_ENABLED=true`를 지정하면 카카오 로컬 API에 WGS84 좌표를 전송한다. 자동 재시도 없이 연결 및 읽기에 3초 소켓 제한을 사용한다. 운영 모니터링에서 요청 본문, 카카오 요청 쿼리와 인증 헤더가 수집되지 않는지 확인한다.
- 애플리케이션은 좌표나 키, 공급자 응답 원문을 저장하거나 기록하지 않는다. 공급자 오류에는 시간 초과, 인증 실패, 할당량 초과, 서버 오류 등 분류만 남긴다. 기존 지역 마스터를 읽으므로 마이그레이션 추가는 없다. 실제 키 검증, FE 수정, 개발 DB 적용 및 배포는 별도 작업이다.
