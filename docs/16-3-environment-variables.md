# 16‑3 Environment Variables (전달 목록)

| 변수 | 비밀/비비밀 | 설명 | 비고 |
|---|---|---|---|
| **SHELTER_CODE_HMAC_KEY** | 비밀 | 보호소 코드 발급·검증 HMAC 키 (32바이트 난수 권장, 64자리 hex). `deploy/.env.api`에만 기록하고 파일 권한 `0600`으로 제한. 예: `python -c "import secrets; print(secrets.token_hex(32))"`로 생성하며 실제 값은 저장소에 두지 않는다. |
| **VOLUNTEER_LEVEL_POLICY_VERSION** | 비비밀 | 봉사자 레벨업 정책 버전 (예: `level-policy-v1`). |
| **VOLUNTEER_LEVEL_POLICY_START_AT** | 비비밀 | 정책 적용 시작 시각, UTC ISO‑8601 (`YYYY‑MM‑DDTHH:MM:SSZ`). 두 변수를 함께 설정해야 평가가 활성화되며 시작 시각 이전 산책은 인정하지 않는다. |
| **TERMS_DOCUMENT_VERSION** | 비비밀 | 약관 파일 버전 (`terms-demo-v1`). |
| **PRIVACY_DOCUMENT_VERSION** | 비비밀 | 개인정보 처리방침 파일 버전 (`privacy-demo-v1`). |
| **PRIVACY_REQUIRED_CONSENTS** | 비비밀 | 필수 동의 항목 콤마 구분 (예 `PRIVACY_COLLECTION_USE`). |
| **PRIVACY_OPTIONAL_CONSENTS** | 비비밀 | 선택 동의 항목 콤마 구분 (없으면 비워 둠). |
| **PLEDGE_CURRENT_VERSION** | 비비밀 | 현재 서약 문서 버전 (기존 사용). |
| **POLICY_DOCUMENTS_DIR** | 비비밀 | 정책 파일 위치 디렉터리 (기본 `policies/`). |
| **SHELTER_CODE_HMAC_KEY** 등 기존 변수는 `.env.example`·`.env.api.example`에 빈값으로 존재한다. |

**비밀 값**은 `deploy/.env.api`에만 기록하고 절대로 소스 트리에 커밋하지 않는다. 시연용 정책 문서는 공개용 법률 문구가 아니며, 실제 약관과 개인정보 처리방침으로 교체할 때는 승인된 문서와 버전을 사용한다. 배포 환경 변수의 실제 입력과 운영 활성화는 별도 승인 후 진행한다.
