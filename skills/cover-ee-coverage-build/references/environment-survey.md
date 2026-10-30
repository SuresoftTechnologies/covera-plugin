# COVER 적용 전용 환경조사서

`cover-environment-survey.json`은 커버리지 작업 초기에 원본 빌드 방법을 결정하기 위한 사전 조사 증거다. 최종 작업 보고서, Excel, 전체 PC 인벤토리로 사용하지 않는다.

## 생성과 확정

1. 프로젝트 탐색 직후 `ENVIRONMENT_SURVEY_CREATED`로 생성한다.
2. 원본 빌드 계획을 선택하고 검증한 뒤 `ORIGINAL_BUILD_METHOD_SELECTED`로 확정한다.
3. 원본 빌드를 시작한 뒤에는 조사서를 갱신하지 않는다.

## 기록 항목

이 조사서는 원본 빌드를 실행하기 전에 무엇을 근거로 빌드 방법을 골랐는지 보여준다. 예를 들어 프리셋에 적힌 컴파일러 경로는 계획의 근거이며, 실제로 실행된 컴파일러의 확인 결과는 원본 빌드 후 별도 파일에 기록한다.

- `host`: OS와 아키텍처
- `coverEnvironment`: 서버 URL과 Agent 설치 경로의 설정·존재 여부, 계획 모니터링 경로
- `project`: 언어·빌드 시스템 후보, 발견 파일 종류, PATH의 후보 도구
- `stm32CubeIDE`: 확정/후보 상태, 프로젝트, 빌드 구성과 확인 근거의 종류
- `buildEvidence`
  - 사용자 제공 원본 명령 또는 스크립트를 구조화한 후보
  - 고객 빌드 스크립트와 CI 파일의 경로·해시
  - CMake 프리셋·캐시와 보수적으로 수집한 기존 빌드 로그
  - Make 도구 변수와 직접 호출 증거
- `originalBuildDecision`
  - 선택 상태와 근거 출처
  - 작업 디렉터리와 환경 초기화 방식
  - 실행 파일·인자 배열·작업 디렉터리로 구조화한 명령
  - 빌드 구성, 타깃과 판단에 사용한 파일

근거 출처는 `USER_PROVIDED`, `SCRIPT`, `CI`, `CACHE`, `PRESET`, `MODEL` 중 하나로 기록한다. 명령 인자에 인증성 값이 있으면 `[REDACTED]`로 바꾸고 환경 변수는 이름만 저장한다.

## 별도 증거로 분리할 항목

- 실제 원본 빌드에서 관찰한 컴파일러·링커·아카이버와 산출물: `original-build-evidence.json`
- Agent 준비와 래퍼 동기화: 해당 Agent 단계 JSON
- CMake/Make `COMMAND_LINE` 시도·실패와 `PROJECT_FILE_PATCH`·복구: 커버리지 빌드 단계 JSON
- 래퍼 호출 검증, 신규 산출물: 각 검증 단계 JSON
- 빌드 시스템·실제 커버리지 명령·선택 타깃·산출물의 공통 요약: `workflow-state.json.result.buildSummary`
- 최종 결과: 위 증거와 공통 요약을 읽어 작성한 자연어 요약

## 금지 항목

- Access Key, Java key
- 서버 URL 원문과 환경변수 값
- 하드웨어 ID, MAC, IP, DNS, 전체 디스크·네트워크 인벤토리
- 실제로 관찰하지 않은 툴체인 또는 커버리지 성공 판정
- Agent, 래퍼 대체 처리, 산출물 같은 사후 작업 결과

조사서의 `integrity`와 `jobId`를 확인하고 다른 작업의 조사서를 재사용하지 않는다.
