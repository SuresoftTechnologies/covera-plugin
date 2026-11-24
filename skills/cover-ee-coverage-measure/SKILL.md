---
name: cover-ee-coverage-measure
description: Looks up the coverage results of a module stored on the COVER EE server and explains the module summary and the per-file and per-function results. Use it for requests about a module's coverage or its uncovered files or functions. It does not run programs, take new measurements, build, or collect STM32 memory.
user-invocable: true
disable-model-invocation: false
---

## 설치 경로와 실행 런타임

이 스킬 패키지에는 실행 코드가 없다. 실행 코드와 Python 런타임은 COVER EE Local Agent에 포함된 COVERA Agent Kit(`agent_kit`)에 있고, 이 스킬은 그 코드를 실행하도록 안내한다. 이 문서와 `references/`의 링크는 지금 읽은 SKILL.md의 부모 디렉터리(SKILL_ROOT) 기준이다.

첫 실행 전에 아래 값을 한 번 확인하고 같은 대화에서 재사용한다. `${...}` 표기는 설명용이며 자동 설정된 환경변수가 아니다. 실행할 때 실제 절대경로로 바꾸고 따옴표로 감싼다.

- `INSTALL_PATH`: 환경 변수 `CODESCROLL_TM_INSTALL_PATH`의 값이다. 값이 없거나 아래 확인에 실패하면 Local Agent 설치 폴더 경로를 사용자에게 요청하고 입력을 기다린다. 다른 위치를 추측해 탐색하지 않고 환경변수를 수정하지 않는다.
- `KIT_PYTHON`: Agent Kit에 포함된 Python이다. Windows는 `${INSTALL_PATH}/agent_kit/python/python.exe`, Linux는 `${INSTALL_PATH}/agent_kit/python/bin/python3`이다. 시스템 Python이나 PATH의 Python을 쓰지 않는다.
- `KIT_SKILL_ROOT`: `${INSTALL_PATH}/agent_kit/plugins/skills/cover-ee-coverage-measure`이다. 이 스킬의 스크립트(`scripts/`)와 실행 중에 읽는 데이터 파일이 여기에 있다.

`KIT_PYTHON`과 `${KIT_SKILL_ROOT}/scripts/measure.py`가 모두 있어야 진행한다.
- 조회 스크립트는 같은 Agent Kit의 빌드 스킬 코드를 함께 쓰므로 `${INSTALL_PATH}/agent_kit/plugins/skills/cover-ee-coverage-build/scripts/`도 있어야 한다.
하나라도 없으면 프로젝트 조사·빌드·서버 연결을 시작하지 말고 아래 안내를 보여준 뒤 멈춘다. 실행 코드를 다른 곳에서 내려받거나 직접 작성해 대신하지 않는다.

```text
[설치 확인]
• 확인한 Local Agent 경로: {INSTALL_PATH 또는 "설정되지 않음"}
• 필요: COVERA Agent Kit(agent_kit)이 포함된 COVER EE Local Agent 4.9(4 Service Pack 9) 이상
• 제품 설치·지원: bizcenter@suresofttech.com
```

사용자의 분석 대상 작업 디렉터리는 바꾸지 않는다. bash 예제는 bash에서 실행한다.

# COVER EE 커버리지 측정 정보 조회

## 범위

서버에 이미 계산되어 있는 커버리지를 읽는다. 이름의 measure는 측정 결과 조회를 뜻하며, 테스트를 실행하거나 새 데이터를 수집하는 기능이 아니다. 빌드는 `cover-ee-coverage-build`로 구분한다.

현재 API가 제공하는 범위는 선택한 빌드 모듈의 **최신 누적 계산 결과**다. 특정 테스트 실행·기간·과거 측정 이력을 선택하는 기능은 제공하지 않는다. 사용자가 해당 범위를 요청하면 지원하지 않는다고 설명하고, 최신 누적으로 임의 대체하지 않는다.

## 실행 위치와 인증

진입점은 Agent Kit의 `${KIT_SKILL_ROOT}/scripts/measure.py`이며, 같은 Agent Kit에 있는 빌드 스킬의 인증 정보 읽기와 curl 통신 함수를 재사용한다. 별도 빌드 job이나 Local Agent 기동은 필요하지 않다. 다른 플러그인 설치도 필요하지 않다.

- COVER 메인 웹 서버 URL과 Access Key를 사용한다. `CODESCROLL_TM_SERVER_URL`은 Agent용 주소이므로 조회 서버 주소로 사용하지 않는다.
- 기본적으로 `CODESCROLL_TM_INSTALL_PATH/agent_kit/open-api-credentials.json`의 URL·키를 한 쌍으로 읽는다. 별도 서버 주소를 받으면 캐시 주소와 같은 경우에만 키를 재사용한다.
- 캐시가 없거나 서버가 다르면 필요한 URL·Access Key만 사용자에게 요청한다. `--access-key-stdin`으로 키 한 줄을 표준입력에 전달하며, 명령 인수·파일·로그·채팅 응답에 키를 출력하지 않는다. 이 스킬은 캐시를 새로 만들거나 변경하지 않는다.
- HTTPS만 사용한다. 기존 covera 정책에 따라 선택한 서버에 한해 인증서 검증을 생략하며, 엄격한 검증 요청에는 `--verify-tls`를 사용한다. 리다이렉트는 따라가지 않는다.

## 조회 순서

1. 모듈 이름 또는 실제 빌드 모듈 ID를 확인한다. 웹 URL의 `symbolicmoduleid`는 빌드 모듈 ID가 아니므로 `--module-id`로 전달하지 않는다. URL만 받았으면 모듈 이름을 추가로 확인한다. URL에 있는 서버 원점은 메인 서버 주소로 사용할 수 있다.
2. 이름으로 조회하면 현재 접근 가능한 모듈을 검색한다. 후보가 하나면 진행하고, 여러 개면 Agent 이름·MAC·산출물 경로·등록 시각을 보여주고 선택을 요청한다. 가장 최근 항목을 임의로 선택하지 않는다. 오프라인 Agent라는 이유로 과거 결과를 제외하지 않는다.
3. 선택한 모듈의 정보와 최신 누적 커버리지를 조회한다. 기본 조회는 요약만 가져온다. 낮은 커버리지 파일이나 함수 정보를 요청받으면 `--details`로 전체 페이지를 가져온다.
4. 스크립트 결과를 바탕으로 답한다. 서버 수치를 임의로 보완하지 않으며 파일별 백분율 평균을 모듈 전체 비율로 사용하지 않는다.

실행 예시에서 경로는 설치 위치의 절대 경로로 바꾼다.

```text
"<KIT_PYTHON>" "<KIT_SKILL_ROOT>/scripts/measure.py" --module-name <모듈파일명>
"<KIT_PYTHON>" "<KIT_SKILL_ROOT>/scripts/measure.py" --server-url <HTTPS메인서버> --module-id <빌드모듈ID> --details
```

전체 상세 결과는 클 수 있다. `--output <작업용JSON절대경로>`를 지정하면 검증이 끝난 결과만 새 파일에 저장하고 채팅 도구 출력에는 요약만 반환한다. 기존 파일은 덮어쓰지 않는다. 프로젝트 빌드 폴더에 증거 파일을 만들 필요는 없다.

## 결과 설명

첫 줄에 조회 성공, 측정 결과 미준비, 사용자 선택 필요 또는 조회 실패를 표시한다. 다음 정보를 함께 설명한다.

- 모듈명·빌드 모듈 ID·산출물 경로, 검색에서 확인한 경우 Agent 정보
- 조회 범위: 최신 누적 계산 결과, 조회 시각, 서버가 제공한 마지막 측정 시각
- 기준별 측정 항목 수 / 전체 항목 수와 비율
- 상세 요청 시 파일 경로와 함수 signature를 함께 표시한 결과

`covered=0, total>0`은 실제 0%다. `null`은 해당 기준이 제공되지 않거나 대상 항목이 없다는 뜻이며 0%가 아니다. `COVERAGE_NOT_READY`는 계산 결과가 아직 없다는 뜻이다. 마지막 측정 시각이 없으면 실행 측정 유무를 단정하지 않는다.

## API와 오류 처리

이 스킬은 COVER Open API 애플리케이션의 Agent Kit 채널을 사용한다. 공개 v1 목록과 별도 경로이며, 기존 `/cover-api/resource` 내부 REST API를 호출하는 것은 아니다. 다음 GET만 허용한다.

- `/cover-open-api/ai-agent-kit/module/search?name=...`
- `/cover-open-api/ai-agent-kit/module/{moduleId}`
- `/cover-open-api/ai-agent-kit/module/{moduleId}/coverage?limit=...&cursor=...`

검색 결과는 SymbolicModule의 currentModule 후보다. 전달받은 웹 링크의 symbolic ID와 검색 결과의 ID가 같다고 설명하지 않는다. 현재 API는 둘의 직접 대응 관계를 반환하지 않는다.

상세 조회는 `nextCursor=null`까지 읽는다. 짧은 페이지도 마지막 페이지라고 가정하지 않는다. 페이지 사이에 모듈·요약·측정 시각이 바뀌거나 파일·커서가 반복되면 결과를 섞지 않고 실패로 처리한다. 이 검사는 가능한 변경을 탐지하지만 서버의 원자적 스냅샷을 보증하지는 않는다. `CURSOR_STALE`이나 `DATA_CHANGED`가 나면 처음부터 한 번만 다시 조회하고 반복되면 측정 반영이 끝난 뒤 재조회하도록 안내한다.

401은 인증, 403은 권한 문제다. 404는 API 미배포·권한 밖·삭제된 모듈 모두 가능하므로 모듈이 없다고 단정하지 않는다. API가 없으면 내부 REST 우회 없이 중단한다. 조회 과정에서 측정 초기화·재계산·Agent 등록·툴체인 수정은 수행하지 않는다.
