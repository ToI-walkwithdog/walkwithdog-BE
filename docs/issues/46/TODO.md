# 현재 위치 판별 후속 작업

- [x] 시연/개발 환경 호출 정책 확정(#117): 시연/개발 환경에서 Kakao 실제 API(`coord2regioncode`) 호출 사용 (`LOCATION_GEOCODING_PROVIDER=kakao`, `LOCATION_EXTERNAL_TRANSFER_ENABLED=true`).
- [x] 위치정보 고지 및 동의 범위 확정(#117): OS 위치 권한 허용 + 서비스 내 위치정보 이용 고지 후 호출하며, 외부 제3자 저장 없이 실시간 좌표 변환만 수행한다(서버 로그·DB·캐시에 좌표 원문 미저장, `Cache-Control: no-store`).
- [x] 호출 한도 기준 확정(#117): Kakao 무료 일일 쿼터 한도 내 사용.
- [ ] 카카오 REST API 키 발급 및 배포 호스트 `.env.api`(0600) 배치 담당 확정(운영 전 결정 예정). 저장소·Issue·로그에 비밀 값을 남기지 않는다.
- [ ] 배포 호스트 `.env.api`(0600) 설정 후 사용자 직접 실제 테스트 키로 수동 연동 검증(이번 작업에서는 실제 키 호출 없음).
- [ ] FE와 위치 권한 안내 시점 및 수동 선택 UX 연동(로컬 개발 환경에서는 `APP_ENV=local|development|test` + `LOCATION_GEOCODING_PROVIDER=mock` 사용 가능).
- [ ] FE 연동 결과와 운영 지표를 보고 호출 제한, 재시도, 캐시 도입 여부를 검토한다. 좌표 원문을 캐시 키나 값에 사용하지 않는다.
