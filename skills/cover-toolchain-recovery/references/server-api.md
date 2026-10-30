# COVER Open API v1 계약

## 인증과 주소

- `COVER_ACCESS_KEY`에서 키를 읽어 모든 요청에 `Authorization: Bearer <키>`를 붙인다. 환경변수 값에는 `Bearer `를 넣지 않는다.
- 다른 변수 이름은 `--access-key-env`로 지정한다. 키 값 자체를 받는 CLI 옵션은 없다.
- 사용자명·암호·로그인 요청·쿠키 저장은 사용하지 않는다.
- `--server`는 서버 루트 또는 `/cover-open-api`, `/cover-open-api/v1`로 끝나는 주소다. 실제 호출 접두어는 `/cover-open-api/v1`이다. 기존 `/cover-api` 주소는 거절한다.
- 오류 자료·Agent 조회·수정·툴체인 조회·등록에는 관리자 Access key가 필요하다. `/agents`는 키 소유자가 접근할 수 있는 Agent만 반환한다. 401은 키, 403은 권한을 확인하며 자동 재인증하지 않는다.

## 사용하는 요청

아래 경로에는 `/cover-open-api/v1` 접두어를 생략했다.

| 목적 | 요청 | 입력과 응답 |
| --- | --- | --- |
| 최신 PA 오류 ZIP | `GET /issue/download` | `macAddress`, `sourceFilePath`, `withSource=true`, `issueWorkType=PA_ERROR_RESULT`. 200 ZIP |
| Agent 목록 | `GET /agents` | `agentName`, `macAddress` 배열 |
| Agent 구성 | `GET /agent` | `macAddress`. Agent 상세 객체 |
| 툴체인 정의 목록 | `GET /toolchain-infos` | ID·이름·종류·버전·original 배열. 설정 본문 없음 |
| 새 툴체인 정의 생성 | `POST /toolchain-infos` | 설정 본문과 표시 정보. 201 JSON으로 생성된 ID와 표시 정보 반환 |
| 지정 Wrapper 연결 | `PATCH /agent` | `macAddress`와 `{toolChains: [...]}`. 변경된 Agent 구성 반환 |

## 오류 자료

소스 전체 경로와 MAC 주소를 URL 인코딩해 보낸다. MAC 주소의 콜론·소문자는 하이픈·대문자로 정규화하며 파일명만 입력하면 거절한다.

최신 오류 선택은 서버가 한다. 서버는 구분자·대소문자를 무시한 경로 끝부분을 비교하며 Windows와 POSIX의 전체 경로 구분까지 보장하지 않는다. 받은 ZIP은 복사 후 DB의 대상 소스 경로를 재검사하고, 대상이 없으면 검증하지 않는다.

응답에는 Issue ID·발생 시각·HardwareID가 없다. ZIP 파일명에서 ID를 추정하지 않는다. `case.json.collection`에는 서버 주소, 요청 MAC·소스 경로·오류 종류, API·ZIP 경로, `issue_metadata_available=false`를 기록한다. 이는 요청 출처이지 응답 메타데이터 검증이 아니다.

206, 다운로드 실패 개수 헤더, 잘못된 ZIP·MIME·응답 크기·수신 길이는 실패로 처리한다. 압축 해제의 경로 안전성과 내부 DB 검증은 자료 수집 계층에서 수행한다.

## 툴체인과 Wrapper

생성 요청 필드는 `aliasName`, `majorKind`, `minorKind`, `majorVersion`, `minorVersion`, `targetKind`, `toolChainContent`다. `id`, `original` 등 서버 필드는 보내지 않는다. 이름 중복을 확인하고 201 JSON의 새 ID·표시 정보를 목록 재조회와 대조한다. 응답에 설정 본문이 없으므로 저장 본문까지 확인했다고 하지 않는다.

새 설정의 유효성은 복사본 PA 검증에서 확인한다. `publish-plan`과 `publish`는 그 검증 결과·배포용 설정 해시·계획 digest를 확인하고, 검증된 본문으로 새 툴체인을 생성한다. 기존 서버 설정 본문 조회나 오류 자료와의 본문 비교는 필요하지 않다. `previous_toolchain`에는 ID·이름·종류·버전·original만 기록하고 현재 메타데이터와 대조한다. 서버 본문의 제자리 변경은 이 검사로 감지하지 않으며, 기존 정의는 수정하지 않고 이전 ID를 되돌리기 연결 대상으로 보존한다.

Agent 응답은 `macAddress`, `userName`, `licenseActivation` 및 `toolChains[].toolChainInfo` 요약을 사용한다. 툴체인 ID는 컴파일러 Wrapper에만 필수다. `C_LINKER`, `CPP_LINKER`, `ARCHIVER`는 `toolChainInfo` 또는 그 `id`가 null이어도 정상이다. PATCH에는 컴파일러만 요약 대신 `toolChainInfoId`를 보내고 Linker·Archiver에서는 이 필드를 생략한다. `toolChains` 전체를 교체하므로 다른 Wrapper도 보내고 컴파일러·Wrapper 이름, 도구 종류, 표준, 빌드 추적, 인코딩·include 옵션을 보존한다. 사용자·경로·정책·온라인 상태는 생략한다.

계획 이후 설정 변경을 확인하고 PATCH 응답과 이후 GET을 의도한 구성과 비교한다. Set 성격의 목록은 순서를 무시하고 include 플래그 순서는 유지한다. 원자적 조건부 갱신 계약이 없어 최종 GET과 PATCH 사이 동시 편집은 막지 못한다.

## 클라이언트와 실패 처리

```text
CoverOpenApiClient(server_url, timeout=30, allow_http=False, insecure_tls=True,
                   max_response_bytes=16MiB, max_archive_bytes=256MiB)
access_key_from_env(access_key_env='COVER_ACCESS_KEY')
download_issue_archive(source_path, hardware_id) -> bytes
list_agents() -> list[dict]
find_agent(hardware_id) / get_agent(hardware_id) -> Agent 상세
list_toolchains() / get_toolchain(toolchain_id) -> 툴체인 표시 정보
create_toolchain(payload) -> 재조회로 ID·표시 정보를 확인한 정의
update_agent_mapping(agent_id, hardware_id, wrapper_name, new_toolchain_id,
                     expected_old_toolchain_id, expected_snapshot=None)
close() -> 메모리에 보관한 키 참조 해제
```

표준 라이브러리 `urllib.request`만 사용한다. 키·Authorization 헤더·서버 오류 본문을 보고서에 넣지 않는다. HTTPS 연결을 유지하면서 인증서 체인·유효기간·hostname 검증은 기본으로 생략한다. `start`, `collect`, `publish-plan`, `publish` 모두 옵션 없이 같은 정책을 사용한다. 인증서 경고 때문에 사용자에게 옵션이나 인증서 설치를 요구하지 않는다. 사용자가 검증을 명시적으로 요청한 경우에만 `--verify-tls` 또는 Python의 `insecure_tls=False`를 사용한다. `allow_http=True`는 로컬 테스트 전용이며 CLI에서는 HTTP와 환경변수 프록시를 허용하지 않는다.

모든 리다이렉트를 차단하고 요청을 재전송하지 않는다. HTML, 잘못된 JSON·중복 키·불완전 응답은 성공으로 처리하지 않는다. 생성·변경 중 timeout, 5xx, 예상 밖 응답, 변경 후 재조회 실패는 `AmbiguousWrite`다. 상태 확인 전 같은 쓰기를 반복하지 않는다.

PA 검증에 성공하면 사용자 승인 없이 `publish-plan` → `publish` 순서로 실행한다. `publish-plan`은 서버를 조회해 대상 Wrapper와 적용할 설정을 기록한다. `publish`는 검증 결과·설정·대상이 계획과 일치하는지 다시 확인한 뒤, 새 툴체인을 생성하고 해당 Wrapper에 연결한다.

기존 툴체인과 다른 Wrapper는 유지한다. 재빌드는 서버 반영이 끝난 뒤 호출한 빌드 작업에서 수행한다.

## 검증 범위

`tests/test_cover_api.py`는 loopback 모의 HTTP 서버로 Bearer 헤더, 로그인 요청 부재, 새 요청·응답 형식, 다른 Wrapper 보존, 다운로드 실패, 비밀값 비노출, 리다이렉트 차단, 모호한 쓰기의 재시도 금지를 검사한다. 공개 명세 확인은 실제 Access key를 사용한 통합 검증과 구분한다.
