# 현재 위치 판별 후속 작업

- [x] 시연 환경 설정은 `LOCATION_GEOCODING_PROVIDER=mock`으로 시작한다. 배포 호스트 `.env.api`에 Kakao REST API 키를 주입하고 2-6 위치 조회 화면의 외부 전송·이용 목적 고지를 확인한 뒤에만 `LOCATION_GEOCODING_PROVIDER=kakao`, `LOCATION_EXTERNAL_TRANSFER_ENABLED=true`로 전환한다.
- [x] 외부 좌표 전송 전제: 온보딩 위치 조회 화면(2-6)에서 현재 위치 좌표가 Kakao로 전송·이용되는 목적을 사용자에게 고지한다. 해당 고지가 배포되어 확인되기 전에는 외부 전송을 활성화하지 않는다.
- [x] 키 발급 및 보관 담당: 16-3 배포 담당자가 Kakao Developers에서 REST API 키를 발급해 배포 호스트 `.env.api`에만 주입한다. 키는 저장소, Issue, 로그에 기록하지 않는다.
- [x] 호출 한도와 안전 제한: Kakao 로컬 역지오코딩 기본 무료 쿼터(일 100,000건) 내에서 운영한다. 백엔드 timeout 3초와 응답 크기 64KB 제한을 유지한다.
- [x] 16-3 담당자 전달 환경변수:
  - `LOCATION_GEOCODING_PROVIDER`: 시연 기본 `mock`; 키 주입과 2-6 고지 확인 뒤 `kakao`.
  - `LOCATION_EXTERNAL_TRANSFER_ENABLED`: 외부 전송을 허용하기 전까지 활성화하지 않으며, 사전 조건 완료 후 `true`.
  - `KAKAO_REST_API_KEY`: Kakao Developers REST API 키. 배포 호스트 `.env.api` 전용 비밀값.
- [x] Mock HTTP 검증: 정상 조회 및 활성 지역 변환, 미지원 지역 422 `REGION_NOT_SUPPORTED`, timeout/외부 오류 503 `GEOCODING_UNAVAILABLE`, 키 누락과 `transfer_disabled`, 로그·응답 캐시 제어 및 좌표 비노출을 확인한다.
- [ ] 실제 배포 전 2-6 고지 완료를 확인하고, 16-3 담당자가 배포 호스트 `.env.api`를 설정한다. 설정 전에는 Kakao 실제 호출을 하지 않는다.
- [ ] 고지 및 설정이 완료된 후 사용자가 승인된 테스트 키로 Kakao 실제 연동을 수동 검증한다. 테스트 키, 키 값, 좌표는 저장소·Issue·로그에 남기지 않는다.
- [ ] FE와 위치 권한 안내 및 수동 선택 UX를 연동한다. 사용자 선택은 위치 권한 거부나 외부 서비스 장애 시에도 가능해야 한다.
- [ ] 호출량과 운영 지표를 검토해 추가 제한이나 재시도 정책을 결정한다. 좌표 원문을 로그, DB 또는 캐시에 저장하지 않는다.

실제 Kakao 호출은 이번 Issue의 자동 테스트 범위에 포함하지 않는다. 모든 검증은 Mock HTTP를 사용하며, 실제 키 주입과 운영 활성화는 위 선행 조건을 충족한 뒤 별도 수동 확인으로 진행한다.
