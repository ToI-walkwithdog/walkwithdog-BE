# 온보딩 정책 문서 조회 인수인계

- 로그인 전에도 `GET /policies/terms`와 `GET /policies/privacy`를 호출할 수 있다. 응답은 문서 제목, 버전, UTC 시행 시각, Markdown 본문을 제공하고 `Cache-Control: no-store`를 사용한다.
- 가입 화면은 사용자가 실제로 확인한 개인정보 문서의 `document_version`과 실제 동의값을 `POST /users/`에 보낸다. 버전이 바뀌어 409가 오면 새 문서를 조회하고 다시 동의를 받는다. 문서를 제공할 수 없으면 조회와 가입은 503으로 중단되고, 동의 항목 설정 오류는 500이다.
- FE는 잘못된 `kind`를 개인정보로 대체하는 현재 목업 동작을 정리하고, 개인정보 동의 체크박스를 사용자의 입력에 연결한다.
- 후속 Step 3 저장은 `app.policy_documents.active_policy_document(kind)`로 활성 문서를 읽고 `check_document_version(submitted, active, field)`로 제출 버전을 확인한다. 합성 테스트 문서는 테스트에서 `POLICY_DOCUMENTS_DIR`로 명시한다.
- 운영 문서와 활성 버전의 등록 절차는 [정책 문서 README](../../../policies/README.md)를 참고한다. 이번 변경에는 스키마 변경이 없다.
