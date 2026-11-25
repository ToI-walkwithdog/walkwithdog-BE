# 정책 문서 등록

기획과 법률 승인을 받은 문서를 `terms/<버전>.md` 또는 `privacy/<버전>.md`에 UTF-8로 추가한다. `manifest.json`에 `kind`, `document_version`, `title`, 시간대가 포함된 `effective_at`, 상대 경로 `file`, UTF-8 본문의 CRLF를 LF로 정규화한 바이트의 `sha256`을 등록한다. 원문 줄바꿈은 보존하고 해시는 LF 기준으로 계산한다. 승인된 본문을 바꿀 때는 새 버전으로 등록하고, 동의 이력을 확인할 수 있도록 이전 버전도 보관한다.

**시연용 정책**: `terms/terms-demo-v1.md` 와 `privacy/privacy-demo-v1.md` 를 시연·테스트 목적에 한해 `[시연용]` 표시와 작성·승인 일시, 시연 목적 메타를 포함하여 추가한다. 실제 법률 문구는 포함하지 않는다.

모든 인스턴스에서 같은 승인 버전을 `TERMS_DOCUMENT_VERSION`과 `PRIVACY_DOCUMENT_VERSION`으로 지정한다. 문서 경로는 기본적으로 이 디렉터리이며, 필요하면 `POLICY_DOCUMENTS_DIR`로 명시한다. 활성 개인정보 문서를 제공할 수 없으면 공개 조회와 회원가입이 503을 반환한다. 운영 문서 등록과 배포는 별도 승인 후 진행한다. 합성 문서는 테스트 디렉터리에만 둔다.
