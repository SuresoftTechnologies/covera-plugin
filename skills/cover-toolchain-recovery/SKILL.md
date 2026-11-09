---
name: cover-toolchain-recovery
description: COVER C/C++ 복구 요청 또는 분석 오류 발견 시, 제공 자료나 서버 Issue에서 오류 자료를 확보하고 COVER
  서버의 tmpa-run API로 복구 설정을 검증한다. 검증 성공 후 서버 반영·Wrapper 연결을 자동으로 진행하고 재빌드를 인계한다. 원본
  소스 수정은 대상이 아니다.
user-invocable: true
disable-model-invocation: false
---

## 설치 경로와 호스트 연결

PLUGIN_ROOT는 이 진입점이 속한 설치 플러그인 루트다. skills/<id>/SKILL.md에서는 ../..이다. 공용 scripts/assets/data는 플러그인 루트에 한 번만 배치한다. 스킬의 references와 보조 문서는 해당 스킬 파일 기준이다.

`${PLUGIN_ROOT}`와 `${PLUGIN_DATA}`가 셸에 자동 설정됐다고 가정하지 않는다. 로드한 파일의 실제 위치로 루트를 확인하고, 실행할 때 실제 절대경로로 대체하거나 동일 셸 호출에서 변수를 설정한다. PLUGIN_DATA는 호스트가 제공하는 영속 데이터 경로를 우선 사용하고, 없으면 사용자 데이터 디렉터리 아래 이 플러그인 전용 경로를 사용한다. 사용자의 분석 대상 작업 디렉터리는 바꾸지 않는다. bash 예제는 bash에서 실행한다.

SKILL_ROOT는 지금 읽은 SKILL.md의 부모 디렉터리다. 이 스킬 전용 scripts/assets는 SKILL_ROOT 아래에 있다. `${SKILL_ROOT}`는 설명용 표기이며 자동 설정된 환경변수가 아니다. 스크립트 실행 시 로드한 SKILL.md의 실제 위치로 절대경로를 구성한다. 프로젝트 작업 디렉터리를 스킬 폴더로 바꾸지 않는다.

# COVER 툴체인 복구

오류 원인과 설정 변경안은 LLM이 판단한다. 보조 코드는 자료 탐색·수집·보존, DB 조회, 서버 PA 실행 요청, 설정 반영과 결과 확인을 맡는다. 과거 오류를 고정된 정답 표에 대입하지 않는다.

## 진입 조건과 요청 범위

사용자 복구 요청 또는 작업 중 분석 오류 발견 시 사용한다. 자료 확보 → 진단 → 서버 PA 검증 → 서버 반영 → 결과 전달까지 추가 승인 없이 진행한다. 사용자가 진단·설명·검증만 명시한 경우에는 해당 범위에서 종료한다. 시작 단서와 CLI가 요구하는 오류 자료를 구분한다.

## 범위와 완료 기준

1. **설정 검증:** 오류 자료 확보 → 진단 → 서버 PA 검증. 성공 상태는 `Recovered`다.
2. **서버 반영:** 검증된 설정의 반영 계획 → 대상·변경 내용 재검증 → 새 툴체인·Wrapper 연결. 사용자 승인을 기다리지 않는다. 자세한 절차는 [서버 반영과 되돌리기](references/server-publication.md)를 읽는다.
3. **재빌드 인계:** 서버 반영과 Wrapper 연결을 확인하면 `ReadyForRebuild`로 빌드 작업에 넘긴다. 재빌드·측정 성공과는 별개다.

“복구해줘” 또는 커버리지 빌드 중 발생한 분석 오류의 복구에는 검증된 설정의 서버 반영과 해당 Wrapper 연결까지 포함한다. 계획 작성·서버 반영·실패 복구에 별도 승인 질문이나 승인 옵션을 요구하지 않는다. 서버 주소·인증 정보·Wrapper 등 필수 입력이 없거나 대상이 모호할 때만 누락 정보를 요청한다. 스킬 코드·문서만 수정하는 작업에서는 실제 서버를 변경하지 않는다.

## 실행 위치와 입력

- 해당 COVER Agent가 설치된 빌드 PC에서 실행한다.
- 시작 단서: 오류 로그의 소스명과 빌드 작업 폴더 또는 정확한 소스 경로.
- 오류 ZIP·폴더를 직접 받지 않았으면 소스 전체 경로와 HardwareID로 서버 Issue API에서 최신 오류를 찾는다. 로컬 환경은 탐색·진단 단서다.
- 메인 서버 주소는 `--server`로 지정한다. `CODESCROLL_TM_SERVER_URL`은 Agent의 remote-service 통신 주소이며, `fixedServerUrl`은 분석 서버 힌트이므로 메인 API 주소로 자동 사용하지 않는다.
- HardwareID는 빌드 계정의 `.cstm_HardwareAddress` 끝에서 MAC 형식 6개 쌍을 추출한다.
- tmpa 검증은 COVER 서버의 실행 요청·상태 조회·오류 자료 다운로드 API로 수행한다.
- 서버 반영에는 메인 서버 주소, Access key 환경변수, HardwareID, 실제 C/C++ Wrapper 이름이 필요하다. 이미 확인한 값은 다시 묻지 않는다.

Python 3.11 이상과 표준 라이브러리를 사용한다. 먼저 실행 가능한 Python 경로를 확인한다. `${SKILL_ROOT}`는 이 SKILL.md가 있는 실제 디렉터리이며 자동 환경변수가 아니다. 예시의 스크립트 경로를 이 파일 기준 절대경로로 바꾸고 따옴표로 감싼다. 대상 프로젝트의 작업 디렉터리는 유지한다. DB를 메모리에서 읽는 `sqlite3.deserialize`가 필요하다.

## 필수 진입 절차

콘솔에 `(analysis error)`가 있으면 기존 `diagnosis.md`, 과거 복구 폴더, 소스 문법을 먼저 조사하지 않는다. 첫 작업은 이 스킬의 `start` 명령이다.

1. 로그의 작업 폴더와 소스명을 합쳐 전체 소스 경로를 만든다.
2. 기존 경로와 겹치지 않는 새 `run-dir`을 정한다.
3. `start`를 실행한다. 서버 주소가 확인돼 있으면 `--server`를 함께 넣는다.
4. `NeedsInput`이면 찾지 못한 서버 주소나 Access key 환경변수만 입력받아 같은 `start`를 재개한다. 이미 확인한 값·ZIP·`.elog`는 다시 묻지 않는다.

사용자가 실제 오류 ZIP/폴더를 직접 제공한 경우가 아니면 `--artifacts`를 지정하지 않는다. 소스 폴더, 이전 `verification-*`, 이전 `recovery-*`, 수동으로 모은 로그 폴더를 오류 ZIP 대신 넣지 않는다.

## 1. 오류 자료 확보

자세한 위치·식별 규칙은 [오류 자료 탐색](references/artifact-discovery.md)을 읽는다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" start --project-root <빌드작업폴더> --source <소스명또는전체경로> --run-dir <새실행폴더>
```

- 확인된 서버 주소·HardwareID와 Access key 환경변수가 있으면 오류 조회·다운로드까지 이어간다.
- HTTPS만 사용한다. HTTP 주소는 자격증명과 오류 자료가 평문으로 흐르므로 자동 흐름에서 사용하지 않는다.
- Open API는 HTTPS 인증서 체인·유효기간·hostname 검증을 기본으로 생략한다. 별도 옵션 입력이나 생략 승인을 요청하지 않으며, 자체 서명·만료·이름 불일치 때문에 인증서 설치를 요구하며 멈추지 않는다. 사용자가 인증서 검증을 명시적으로 요청한 경우에만 `--verify-tls`를 사용한다. 이 정책은 수집·조회·서버 반영에 공통으로 적용한다.
- 자동 발견한 로컬 자료는 완전해도 HardwareID를 증명하지 못하므로 서버 Issue API로 소스 전체 경로와 HardwareID를 확인한다.
- 여러 오류가 남으면 MAC 주소·소스 전체 경로·`issueWorkType=PA_ERROR_RESULT`로 Open API가 선택한 최신 ZIP을 받는다. 서버는 구분자와 대소문자를 무시한 경로 끝부분을 비교한다. 소스명만 보내지 않으며, 응답에 없는 Issue ID는 기록하지 않는다. `X-Issue-Occurred-Time`이 있으면 UTC 발생 시각으로 기록하고, 그 뒤 소스가 수정됐다면 시계 차이를 고려해 최신 빌드 결과를 먼저 확인한다. `--issue-id` 옵션은 없다.
- 자료 수집 전에 Wrapper를 재실행하거나 재빌드하지 않는다.
- 기존 ZIP, 소스, 분석 자료, 실행 폴더를 덮어쓰거나 삭제하지 않는다.

탐색 결과만 보려면 같은 입력으로 `discover --project-root ... --source ...`를 사용한다. 서버 설치 위치가 확인되면 `--server-root`, 관련 자료 폴더가 확인되면 `--search-root`를 추가한다.

이미 ZIP/폴더가 있으면 서버 연결 없이 시작한다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" start --artifacts <오류ZIP또는폴더> --source <원래소스경로> --run-dir <새실행폴더>
```

로컬 ZIP/폴더는 진단용으로 읽을 수 있다. 서버 API는 ZIP 업로드를 받지 않으므로 실제 검증에는 `start` 또는 `collect`로 서버 Issue에서 수집한 case가 필요하다. 이미 받은 로컬 ZIP을 서버에서 실행했다고 표시하지 않는다. [PA 실행 계약](references/server-execution.md)을 따른다.

서버 수집이 필요하면 [서버 API 계약](references/server-api.md)을 읽는다. Access key는 `COVER_ACCESS_KEY` 환경변수 또는 `--access-key-env`로 지정한 환경변수에서 받는다. 값에는 `Bearer ` 접두어를 넣지 않는다. 채팅, 명령행 인수, 보고서에 키를 넣지 않고 환경변수 전체를 출력하지 않는다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" collect --server <HTTPS서버URL> --hardware-id <HardwareID> --source <원래소스경로> --archive <새ZIP경로> --run-dir <새실행폴더>
```

## 2. 진단과 후보 작성

[분석 설정 가이드](references/analysis-guide.md)를 필요한 부분만 읽는다. 변경 근거는 실제 `.elog`, `.conf`, 전처리 소스, 컴파일 명령·버전, DB, 인코딩·preinclude 자료에서 확인한다.

옵션의 문법·허용값·적용 조건은 동봉된 [PA 옵션 참고서](references/pa-options.md)에서 해당 절만 읽는다. 원문 출처는 [출처와 적용 범위](references/guide-sources.md)에 보존한 기록이며, 실행 중 원문 열람은 전제로 하지 않는다. 로컬 참고서와 서버 tmpa 버전에 대응하는 자료·서버 실행 결과로 확인하지 못한 문법·지원 여부는 추측하지 않고 미확인으로 남긴다. 문서에 적힌 지원 버전과 이번 PA 검증 결과는 구분한다.

- 최초 오류와 뒤따르는 오류를 구분한다. 가설을 확인된 원인으로 적지 않는다.
- 일반 컴파일러 경고나 소스 문법만으로 COVER PA 원인을 확정하지 않는다.
- 소스를 빌드한 PC의 `platform`(win/linux), 서버 tmpa의 OS, 컴파일 대상의 `target`을 구분한다.
- 수정은 확인된 원인에 필요한 최소 설정 변경으로 제한한다.
- `cs_ignore_*`, `cs_replace_code*`, `cs_disable_keyword`, `cs_allow_*`처럼 분석 의미를 없애거나 느슨하게 만들 수 있는 옵션은 자동 후보에서 제외한다.
- 소스 수정이 필요하거나 설정으로 해결할 근거가 없으면 수동 조치와 필요한 자료를 안내하고 종료한다.

판단 결과는 실행 폴더의 `diagnosis.md`에 확인된 사실, 가설, 후보 또는 후보를 만들지 않은 이유, 다음에 필요한 자료로 나눠 기록한다. `evidence.missing=[]`는 보조 코드가 확인한 항목에 누락이 없다는 뜻이지, 원인 판단 증거가 모두 있다는 뜻은 아니다.

후보 JSON은 [로컬 작업 계약](references/local-workflow.md)의 `reason`, `evidence`, `replacements`, `append_lines`를 사용한다. 원본 설정의 정확한 줄만 바꾸며 `.conf` 전체를 임의로 재작성하지 않는다.

## 3. 서버에서 설정 검증

[PA 실행 계약](references/server-execution.md)과 [서버 API 계약](references/server-api.md)을 읽는다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" verify --case <실행폴더/case.json> --candidate <후보.json>
```

- `verify`는 수집한 서버·HardwareID·소스 경로와 DB의 `Compiler.idCompiler`, 수정한 전체 설정 줄을 POST한다. DB·INI·소스를 업로드하지 않는다. 서버가 최신 `PA_ERROR_RESULT`의 복사본에서 실행한다.
- `platform`은 **소스를 빌드한 PC의 OS**다. 서버 OS나 `.conf`의 `target`이 아니다. 기본은 서버의 이슈 경로 추론이며, 확인된 값만 `--platform win|linux`로 지정한다.
- 서버 Issue가 수집 후 바뀌지 않도록 검증 중 동시 빌드를 피한다. 이 API에는 Issue ID 고정이나 성공 산출물 다운로드가 없다. 검증 범위는 서버가 선택한 최신 오류이며 프로젝트 전체 성공을 뜻하지 않는다.
- `runId`를 저장한 뒤 약 2초 간격으로 GET한다. `--timeout`은 이번 조회 대기 시간(기본 60초, 1~60초)이며 서버 실행을 취소하지 않는다. `PollingPending`이면 같은 명령으로 계속 조회한다. 조회 재개는 시도를 추가하거나 POST를 반복하지 않는다.
- 새 검증 시도는 case당 최대 3회다. 동일 변경의 무의미한 반복·새 case로 한도 우회는 하지 않는다. 차단 원인을 해결한 경우에만 남은 시도 안에서 새 요청을 한다. 응답 유실로 POST 결과가 모호하면 서버 확인 전 다시 요청하지 않는다.
- `SUCCEEDED`만 `Recovered`로 처리한다. 서버가 정상 종료와 계측 결과 생성을 확인한 결과다.
- `FAILED`와 `TIMEOUT`은 오류 ZIP을 내려받아 이번 `console.log`, `log/parse/*.elog`, full log를 진단한다. TIMEOUT DB는 쓰다 만 상태일 수 있어 진단용이며 성공 증거로 쓰지 않는다. `ERROR`는 서버 로그에서 runId를 확인해야 하며 ZIP을 요청하지 않는다.
- `tmpaRevision`과 원래 오류 ZIP의 REV를 비교한다. 버전이 다르거나 null이면 그 한계를 기록한다. 버전이 같다고 추측하지 않는다.
- 진단·검증만 요청했으면 `Recovered`에서 끝낸다. 로컬 작업만 허용된 요청에서는 API 실행 없이 진단까지만 수행한다. 복구 요청이면 다음 단계로 이어간다.

## 4. 실제 설정 반영

[서버 반영과 되돌리기](references/server-publication.md), [서버 API 계약](references/server-api.md)을 읽는다. 서버 PA 실행과 툴체인 등록을 분리하며, 새 툴체인은 검증을 통과한 배포용 설정에서 만든다. 기존 서버 설정 본문은 조회·비교하지 않는다. 검증 파일 해시·계획 digest와 대상 Agent·Wrapper 연결·툴체인 표시 정보는 반영 직전까지 확인한다.

`publish-plan`은 서버를 읽고 대상·설정·표시 정보·이전 연결을 고정한다. 검증된 GCC `gnu_version`으로 표시 버전을 맞추고, 확인된 `target`과 복구 식별자만 이름에 넣는다. `win64`만으로 MinGW라고 추측하지 않는다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" publish-plan --case <실행폴더/case.json> --server <서버URL> --hardware-id <HardwareID> --wrapper <Wrapper이름>
```

대상 서버·HardwareID·Wrapper, 기존/새 툴체인과 설정 diff를 확인하고 바로 다음 명령을 실행한다. 변경 내용은 진행 상황으로 알리되 사용자 응답을 기다리지 않는다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" publish --case <실행폴더/case.json> --server <서버URL> --plan-digest <생성한 plan_digest>
```

계획이나 검증 설정이 바뀌면 기존 계획으로 진행하지 않고 변경된 조건을 다시 검증한다. 실패 시 자동 재전송·되돌리기를 하지 않고 `rollback.json`과 서버 상태를 대조한다. 현재 상태와 되돌릴 범위가 확인되면 별도 승인 없이 복구 절차를 진행한다.

## 5. 재빌드 인계

서버 반영과 새 Wrapper 연결을 확인해야 `ReadyForRebuild`다. 이 스킬은 `handoff.json`으로 인계하고 종료한다. 원래 요청에 빌드가 포함돼 있으면 호출한 빌드 작업이 이어서 수행한다. PA·서버 반영 성공을 빌드나 측정 성공으로 대신하지 않는다.

## 결과 전달

CLI 상태 이름은 그대로 기록하고 막힌 단계와 필요한 조치를 설명한다. 검증한 소스와 공유 설정의 영향 범위를 구분하며, 확인하지 않은 범위는 미검증으로 남긴다.

`report.md`는 자동 기록, `diagnosis.md`는 LLM의 한국어 진단, `case.json`은 단계별 상태, `publish-plan.json`은 서버 반영 계획, `rollback.json`은 현재 서버 상태를 대조해 되돌리기에 사용하는 식별값, `handoff.json`은 재빌드 인계 기록이다.

네이티브 컴파일, COVER 전처리, PA, 서버 반영, 계측 컴파일, `.cvd`는 서로 다른 성공 조건으로 보고한다. 서버 반영까지 성공해도 네이티브 빌드·계측 컴파일·커버리지 수집 성공으로 표현하지 않는다.
