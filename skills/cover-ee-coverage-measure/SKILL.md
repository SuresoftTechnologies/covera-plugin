---
name: cover-ee-coverage-measure
description: COVER EE 서버에 저장된 특정 모듈의 커버리지 측정 결과를 조회하고 모듈 요약과 파일·함수별 결과를 설명한다. 모듈 커버리지,
  미달성 파일 또는 함수 조회 요청에 사용한다. 프로그램 실행이나 새 측정, 빌드, STM32 메모리 수집은 수행하지 않는다.
user-invocable: true
disable-model-invocation: false
---

## 설치 경로와 호스트 연결

PLUGIN_ROOT는 이 진입점이 속한 설치 플러그인 루트다. skills/<id>/SKILL.md에서는 ../..이다. 공용 scripts/assets/data는 플러그인 루트에 한 번만 배치한다. 스킬의 references와 보조 문서는 해당 스킬 파일 기준이다.

`${PLUGIN_ROOT}`와 `${PLUGIN_DATA}`가 셸에 자동 설정됐다고 가정하지 않는다. 로드한 파일의 실제 위치로 루트를 확인하고, 실행할 때 실제 절대경로로 대체하거나 동일 셸 호출에서 변수를 설정한다. PLUGIN_DATA는 호스트가 제공하는 영속 데이터 경로를 우선 사용하고, 없으면 사용자 데이터 디렉터리 아래 이 플러그인 전용 경로를 사용한다. 사용자의 분석 대상 작업 디렉터리는 바꾸지 않는다. bash 예제는 bash에서 실행한다.

SKILL_ROOT는 지금 읽은 SKILL.md의 부모 디렉터리다. 이 스킬 전용 scripts/assets는 SKILL_ROOT 아래에 있다. `${SKILL_ROOT}`는 설명용 표기이며 자동 설정된 환경변수가 아니다. 스크립트 실행 시 로드한 SKILL.md의 실제 위치로 절대경로를 구성한다. 프로젝트 작업 디렉터리를 스킬 폴더로 바꾸지 않는다.

# COVER EE 커버리지 측정 정보 조회

## 범위

서버에 이미 계산되어 있는 커버리지를 읽는다. 이름의 measure는 측정 결과 조회를 뜻하며, 테스트를 실행하거나 새 데이터를 수집하는 기능이 아니다. 빌드는 `cover-ee-coverage-build`로 구분한다.

현재 API가 제공하는 범위는 선택한 빌드 모듈의 **최신 누적 계산 결과**다. 특정 테스트 실행·기간·과거 측정 이력을 선택하는 기능은 제공하지 않는다. 사용자가 해당 범위를 요청하면 지원하지 않는다고 설명하고, 최신 누적으로 임의 대체하지 않는다.

## 실행 위치와 인증

`SKILL_ROOT`는 이 파일의 부모 디렉터리다. 진입점은 `scripts/measure.py`이며, 같은 플러그인의 빌드 스킬에 있는 인증 정보 읽기와 curl 통신 함수를 재사용한다. 별도 빌드 job이나 Local Agent 기동은 필요하지 않다. 다른 플러그인 설치도 필요하지 않다.

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
python <SKILL_ROOT>/scripts/measure.py --module-name <모듈파일명>
python <SKILL_ROOT>/scripts/measure.py --server-url <HTTPS메인서버> --module-id <빌드모듈ID> --details
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
