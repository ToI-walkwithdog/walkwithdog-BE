# 백엔드 후속 검증 기록

- 대상: localhost PostgreSQL `toi_db`, 외부 공개 없음.
- 002 인증 세션, 003 공공데이터 컬럼, 004 대화 테이블을 각 백업 후 적용.
- 002/003/004 재실행 시 적용할 변경 없음 확인.
- 실제 PostgreSQL 회원가입·로그인·본인 접근·타인 접근 차단·매칭 재호출·로그아웃 통과.
- 성향 차트·시뮬레이션·행동 선택·대화 저장과 재수집을 PostgreSQL에서 검증.
- `gemini-3-flash-preview` 실제 생성 응답을 포함한 대화 흐름 통과. 검증 데이터는 부모 트랜잭션 롤백.
- 공식 공공 API 조회 성공. 백업 후 5건 추가, 재조회 미리보기에서 신규 0건/갱신 5건.
- 테스트 92개 통과. `git diff --check` 통과. 전체 앱 OpenAPI 경로 등록 확인.
- 업데이트된 서버: `127.0.0.1:8001`, Swagger `/docs`.

작업 전 백업 `.demo-backups/toi-before-demo-1xlpdjt1.dump`의 기존 행 내용과 비교:

| 테이블 | 기존 행 | 결과 |
| --- | ---: | --- |
| users | 3 | 보존 |
| shelters | 9 | 보존 |
| dogs | 26 | 보존 |
| surveys | 3 | 보존 |
| matching_results | 15 | 보존 |

최종 건수: users 3, shelters 10, dogs 31, surveys 3, matching_results 15,
chat_sessions 0, chat_messages 0. 원래 데이터 삭제·덮어쓰기 없음.

각 백업은 gitignored `.demo-backups/`의 권한 600 파일로 보관합니다.
API 키·DB 비밀번호·행 원문은 검증 로그에 출력하지 않았습니다.

Gemini 키와 `GEMINI_MODEL=gemini-3-flash-preview`를 로컬 `.env`에서 확인했고 키 값은
로그·문서에 출력하지 않았습니다. 운영 전에는 다양한 질문에 대한 응답 품질·안전성·비용 제한을
별도로 평가해야 합니다. 관리용 수집 HTTP 요청에는 `PUBLIC_IMPORT_KEY` 설정이 필요합니다.
프론트엔드·배포·커밋은 하지 않았습니다.
