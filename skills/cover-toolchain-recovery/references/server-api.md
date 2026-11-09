# COVER Open API 계약

## 인증과 주소

- `COVER_ACCESS_KEY`에서 키를 읽어 모든 요청에 `Authorization: Bearer <키>`를 붙인다. 환경변수 값에는 `Bearer `를 넣지 않는다.
- 다른 변수 이름은 `--access-key-env`로 지정한다. 키 값 자체를 받는 CLI 옵션은 없다.
- 사용자명·암호·로그인 요청·쿠키 저장은 사용하지 않는다.
- `--server`는 서버 루트 또는 `/cover-open-api`, `/cover-open-api/v1`로 끝나는 주소다. 기존 API 접두어는 `/cover-open-api/v1`, TMPA API 접두어는 `/cover-open-api/ai-agent-kit`이다. 기존 `/cover-api` 주소는 거절한다.
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

Issue ID·HardwareID 응답은 없다. ZIP 파일명에서 ID를 추정하지 않는다. `X-Issue-Occurred-Time` 헤더가 있으면 시간대가 포함된 시각을 `collection.issue_metadata.occurred_time`에 보존한다. 없는 헤더는 만들지 않는다. `case.json.collection`에는 서버 주소, 요청 MAC·소스 경로·오류 종류, API·ZIP 경로, Access key 환경변수 이름(값 아님), TLS 정책도 기록한다. 소스 수정 시각이 이슈 발생 뒤라면 시계 차이를 고려해 재빌드로 최신 결과를 먼저 확인한다.

206, 다운로드 실패 개수 헤더, 잘못된 ZIP·MIME·응답 크기·수신 길이는 실패로 처리한다. 압축 해제의 경로 안전성과 내부 DB 검증은 자료 수집 계층에서 수행한다.

## 서버 TMPA 실행 (AI Agent Kit)

아래 경로에는 `/cover-open-api` 접두어를 붙인다.

| 목적 | 요청 | 입력·응답 |
| --- | --- | --- |
| 실행 요청 | `POST /ai-agent-kit/tmpa-run` | JSON → 200 `{runId}` (32자리 16진수) |
| 상태 조회 | `GET /ai-agent-kit/tmpa-run/{runId}` | 200 `{status, tmpaRevision}` |
| 실패 자료 | `GET /ai-agent-kit/tmpa-run/{runId}/error-archive` | 200 application/zip; FAILED/TIMEOUT만 가능 |

요청의 필수 값:

```json
{
  "macAddress": "AA-BB-CC-DD-EE-FF",
  "sourceFilePath": "C:\\work\\src\\main.cpp",
  "compilerId": 1,
  "toolChainContent": ["target=win64", "gnu_version=140200"]
}
```

- `toolChainContent`는 수정한 **전체 conf**의 줄 배열이다. 빈 목록·null·항목 내부 줄바꿈은 불가하다. 주석·빈 줄 외에는 줄 앞 공백이나 키·플래그 안 공백이 없어야 한다(`key = value` 거절). 툴체인 등록과 동일한 줄 규칙이다.
- `compilerId`는 오류 global.db의 Compiler.idCompiler이며 양의 int32다. 보통 1이지만 DB에서 확인한다.
- 선택 `platform`: 빌드 PC의 `win` 또는 `linux`. 생략하면 이슈의 드라이브/UNC 소스 경로는 win, 나머지는 linux로 서버가 판단한다. 서버 실행 환경과 구분한다.
- 서버는 MAC·소스 경로에 해당하는 가장 최근 PA_ERROR_RESULT를 선택한다. ZIP 업로드·Issue ID 지정 기능은 없다. 접근 가능한 Agent에 일반 사용자도 요청할 수 있다.
- 상태는 RUNNING, SUCCEEDED, FAILED, TIMEOUT, ERROR다. tmpaRevision은 이번 console.log의 REV이며 null일 수 있다. SUCCEEDED는 정상 종료와 계측 산출물 생성을 서버가 확인한 결과다. 성공 작업 폴더는 즉시 삭제돼 ZIP을 받을 수 없다.
- 약 2초 간격으로 조회한다. 서버 실행 제한은 30분, 종료한 결과는 1시간 보관한다. 서버 재시작·만료·잘못된 ID는 404다.
- FAILED/TIMEOUT ZIP은 `{tuId}_{sessionId}` 루트의 이슈 ZIP 구조다. 이전 실행 로그·산출물은 제거돼 이번 실행 자료만 포함된다. 설정 오류는 console.log에만, 파싱 오류는 log/parse/*.elog에 남을 수 있다. TIMEOUT의 DB·journal은 불완전할 수 있어 진단용이다.

오류 처리:

- 400: 필수 값·MAC·compilerId·설정 줄·platform을 확인한다.
- 404: Agent 접근 권한, 이슈·이슈 파일·분산 서버 또는 만료된 runId를 확인한다. 이슈가 없으면 빌드 오류 자료가 서버에 수집됐는지 확인한다.
- 409 `TMPA_RUN_BUSY`: 동시 실행 한도(2건)·디스크·종료 중 상태. 조건 해결 후 재요청할 수 있다.
- 409 `ISSUE_OUTDATED`: 같은 조건의 후속 빌드와 서버 분석이 이미 성공한 이슈. 재빌드 결과를 확인한다.
- 409 `ERROR_ARCHIVE_NOT_AVAILABLE`: RUNNING/SUCCEEDED/ERROR에는 ZIP이 없다.
- 500: 서버 tmpa 설치 등 서버 문제. 502: 이슈 보관 분산 서버 연결 문제.
- 명세상 완료된 POST 4xx·5xx 응답은 실행이 생성되지 않은 거절이다. 자동 반복하지 않고 원인 해결 후 남은 시도 안에서 새 요청한다. 네트워크 단절·timeout·잘못된 200 응답은 생성 여부가 모호하므로 POST를 반복하지 않는다.

코드 진입점은 `start_tmpa_run`, `get_tmpa_run`, `download_tmpa_error_archive`다. 보존·조회 재개·상태 판정은 [서버 실행 계약](server-execution.md)에 있다.

## 툴체인과 Wrapper

생성 요청 필드는 `aliasName`, `majorKind`, `minorKind`, `majorVersion`, `minorVersion`, `targetKind`, `toolChainContent`다. `id`, `original` 등 서버 필드는 보내지 않는다. 이름 중복을 확인하고 201 JSON의 새 ID·표시 정보를 목록 재조회와 대조한다. 응답에 설정 본문이 없으므로 저장 본문까지 확인했다고 하지 않는다.

새 설정의 유효성은 서버 PA 검증에서 확인한다. `publish-plan`과 `publish`는 그 검증 결과·배포용 설정 해시·계획 digest를 확인하고, 검증된 본문으로 새 툴체인을 생성한다. 기존 서버 설정 본문 조회나 오류 자료와의 본문 비교는 필요하지 않다. `previous_toolchain`에는 ID·이름·종류·버전·original만 기록하고 현재 메타데이터와 대조한다. 서버 본문의 제자리 변경은 이 검사로 감지하지 않으며, 기존 정의는 수정하지 않고 이전 ID를 되돌리기 연결 대상으로 보존한다.

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

표준 라이브러리 `urllib.request`만 사용한다. 키·Authorization 헤더·서버 오류 본문을 보고서에 넣지 않는다. HTTPS 연결을 유지하면서 인증서 체인·유효기간·hostname 검증은 기본으로 생략한다. `start`, `collect`, `verify`, `publish-plan`, `publish` 모두 옵션 없이 같은 정책을 사용한다. 인증서 경고 때문에 사용자에게 옵션이나 인증서 설치를 요구하지 않는다. 사용자가 검증을 명시적으로 요청한 경우에만 `--verify-tls` 또는 Python의 `insecure_tls=False`를 사용한다. `allow_http=True`는 로컬 테스트 전용이며 CLI에서는 HTTP와 환경변수 프록시를 허용하지 않는다.

모든 리다이렉트를 차단하고 요청을 재전송하지 않는다. HTML, 잘못된 JSON·중복 키·불완전 응답은 성공으로 처리하지 않는다. 툴체인·Wrapper 생성·변경 중 timeout, 5xx, 예상 밖 응답, 변경 후 재조회 실패는 `AmbiguousWrite`다. 상태 확인 전 같은 쓰기를 반복하지 않는다.

PA 검증에 성공하면 사용자 승인 없이 `publish-plan` → `publish` 순서로 실행한다. `publish-plan`은 서버를 조회해 대상 Wrapper와 적용할 설정을 기록한다. `publish`는 검증 결과·설정·대상이 계획과 일치하는지 다시 확인한 뒤, 새 툴체인을 생성하고 해당 Wrapper에 연결한다.

기존 툴체인과 다른 Wrapper는 유지한다. 재빌드는 서버 반영이 끝난 뒤 호출한 빌드 작업에서 수행한다.

## 검증 범위

`tests/test_cover_api.py`는 loopback 모의 HTTP 서버로 Bearer 헤더, 로그인 요청 부재, 새 요청·응답 형식, 다른 Wrapper 보존, 다운로드 실패, 비밀값 비노출, 리다이렉트 차단, 모호한 쓰기의 재시도 금지를 검사한다. 공개 명세 확인은 실제 Access key를 사용한 통합 검증과 구분한다.
