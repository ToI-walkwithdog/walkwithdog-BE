# 산책 루틴 필수 동의 인수인계

- 루틴 저장 전 공개 `GET /policies/terms`와 `GET /policies/privacy`에서 문서와 버전을 받는다. 사용자가 둘 다 확인하고 동의하면 기존 네 루틴 입력과 함께 `PUT /users/me/onboarding/walking-routine`의 `agreements.terms`와 `agreements.privacy`에 각각 `document_version`과 `agreed: true`를 보낸다.
- 신규 제출의 버전이 이미 바뀌었으면 409 `POLICY_VERSION_MISMATCH`를 반환한다. FE는 해당 문서를 다시 조회하고 동의를 받는다. 문서를 제공할 수 없으면 503이며 루틴이 완료되지 않는다. 저장 성공 뒤 `GET /users/me`에서 완료 상태를 확인한다.
- 루틴과 지역·시간대 연결, 필요한 신규 동의 이력이 함께 커밋된다. 기존 같은 목적과 버전의 유효한 개인정보 동의는 재사용한다. 완료된 신규 요청에서 실제 제출한 버전은 재시도 식별에 쓰이며, 응답에는 추가되지 않는다. 이전 완료 행은 두 버전이 비어 있는 채로 보존된다.
- 배포 전 `018_user_consents.sql`과 `026_onboarding_walking_routine.sql` 이후에 `027_onboarding_agreements.sql`을 적용한다. 새 API에는 승인된 활성 문서 설정도 필요하다. 실제 DB 적용과 서버 배포는 별도 승인 후 진행한다.
