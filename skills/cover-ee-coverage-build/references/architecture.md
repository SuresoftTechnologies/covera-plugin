# 구현 구조와 유지보수 시 지킬 기준

## 처음 읽는 순서

1. `cover_build/workflow.py`에서 전체 단계 순서를 확인한다.
2. `cover_build/pipelines/registry.py`에서 선택한 빌드 시스템의 파이프라인을 찾는다.
3. `cover_build/pipelines/<환경>/__init__.py`에서 해당 환경의 처음부터 끝까지를 읽는다.
4. 파이프라인이 호출하는 같은 패키지의 모듈과 `planning/`, `services/`, `common/`의 필요한 파일만 내려가서 읽는다.
5. 하위 명령 CLI 자체를 수정할 때만 `cover_build_executor.py`를 읽는다. 하위 명령의 구현은 `common/`과 환경 패키지에 있다.

## 실행 계층

환경별 실행 순서와 여러 환경이 함께 쓰는 기능을 분리한다. 새 기능을 추가할 때에는 먼저 아래에서 담당 파일을 찾는다.

- `cover_workflow_runner.py`: 공개 CLI, 공통 작업 보조 함수, 이전 함수 호출 호환
- `cover_build/workflow.py`: 환경조사부터 최종 보고까지의 전체 실행 순서 관리
- `cover_build/pipelines/registry.py`: 지원 빌드 시스템과 파이프라인의 단일 등록 지점
- `cover_build/pipelines/<환경>/__init__.py`: Visual Studio, CMake/Make, STM32CubeIDE, C#, Java의 환경별 전체 수행 절차
- `cover_build/pipelines/gcc/selection.py`: CMake/Make 진입점·build 디렉터리·generator·구성 선택과 원본 선택값 보존
- `cover_build/pipelines/gcc/original.py`: GCC 원본 빌드와 실제 툴체인 관찰
- `cover_build/pipelines/visual_studio/tools.py`: Visual Studio 설치·도구 탐색과 diagnostic 로그의 실제 도구 추출
- `cover_build/pipelines/visual_studio/msbuild.py`: MSBuild 원본·커버리지 실행과 적용 후 재검증
- `cover_build/pipelines/visual_studio/planning.py`: VS 환경 캡처, 오버레이 생성, MSBuild 커버리지 계획
- `cover_build/pipelines/stm32cubeide/tools.py`: STM32CubeIDE 프로젝트·headless 실행 파일·타깃 도구 확인
- `cover_build/pipelines/java/jacov_config.py`: 누락된 홈 JACOV 설정 생성. 현재 PC의 Agent ID를 Open API로 조회하고 제품 설정 파일 외에는 key를 남기지 않는다
- `cover_build/pipelines/<환경>/persistent.py`: Visual Studio props import, STM32CubeIDE CDT 환경·`gcc.config`, Gradle JAR·init script처럼 환경별 재사용 설정의 적용과 검증
- `cover_build/planning/selection.py`: 빌드 시스템·진입점·구성 선택
- `cover_build/planning/build_plan.py`: 빌드 근거 수집과 원본 계획 검증
- `cover_build/planning/survey.py`: COVER 적용 전 환경조사 JSON
- `cover_build/planning/decision_file.py`: 모델·사용자 결정 파일
- `cover_build/planning/resolution.py`: 중단된 결정의 검증·재개와 대기 상태
- `cover_build/services/gcc_coverage.py`: GCC 래퍼 매핑과 커버리지 빌드(CMake/Make와 STM32CubeIDE가 공유)
- `cover_build/services/glue_compiler.py`: 실제 타깃 C++ 컴파일러 드라이버 선택, Linux `COVER_CC`, Windows의 래퍼 호환 작업별 `GlueCodeCompiler` 설정. GNU `base.config`를 MSVC 등 다른 래퍼 형식에 재사용하지 않는다
- `cover_build/services/agent_open_api.py`: Access Key 기반 Open API v1 Agent 조회·생성, `updateAgent.toolChains` 기존 매핑 보존·누락 매핑 추가와 ToolChainInfo 정의 선택
- `cover_build/services/agent_local.py`: Local Agent 탐색, 런타임 로그와 래퍼 파일 검증
- `cover_build/services/project_backup.py`: CMake/Make 대체 처리용 고객 파일 백업·복구
- `cover_build/core/job_evidence.py`: 단계 상태와 작업 증거 기록
- `cover_build/core/environment_profile.py`: 성공 작업의 빌드 환경 프로필 저장·검증·새 작업 재사용
- `cover_build/core/persistent_setup.py`: 성공한 계측 설정을 재사용 위치인 `.cover-ee`로 옮기고 수동 진입점·원본 백업·다음 원본 빌드의 일시 해제/복귀를 관리. 환경별 연결은 각 파이프라인 패키지의 `persistent.py`에 위임
- `cover_build/core/`: 원본 빌드 증거, 보고, 상태 저장, 공통 오류(`ExecutorError`, `WorkflowWaiting`)
- `cover_build/common/`: 여러 계층이 함께 쓰는 기능. `pathutil`(경로·해시), `fileio`(원자적 쓰기·무결성), `envelopes`(결과 봉투·현재 작업 ID), `servers`(서버 원점), `jobs`(작업 컨텍스트), `project_files`(프로젝트 파일 판별·VS/CMake 파싱), `agent_host`(현재 PC Agent 식별·시작), `toolchains`(실제 도구 검증·래퍼 설정 파싱), `gcc_tools`(GCC 래퍼 준비·CMake/Make 계획), `processes`(명령 실행·계획 실행·산출물 스냅샷), `native_logs`(빌드 로그·응답 파일 판독과 래퍼 호출 검증), `open_api`(Open API v1 요청), `project_edit`(고객 파일 편집·백업·복구)
- `cover_build_executor.py`: 하위 명령 파서와 `main`만 가진 CLI. 하위 명령은 `common/`과 환경 패키지의 함수를 그대로 호출한다

새 환경 지원은 `pipelines/registry.py`와 새 `pipelines/<환경>/` 패키지에 추가한다. 패키지의 `__init__.py`에는 그 환경의 단계 순서를 두고, 그 환경만 쓰는 선택·실행·재사용 설정 코드는 같은 패키지에 둔다. 여러 환경이 함께 쓰는 Agent·API·글루 컴파일러·백업은 `services/`, 계층을 가리지 않는 공용 기능은 `common/`에 둔다. 환경 판정과 원본 계획 작성 규칙은 `planning/`, 상태와 보고는 `core/`에만 둔다.

이전 import 경로를 위한 호환 모듈은 두지 않는다. 테스트는 실제 구현 모듈을 직접 사용하고, `mock.patch.object`의 대상은 함수를 정의한 모듈이다.

새 구현이나 환경별 분기를 추가하지 않는다. `cover_build_executor.py`에도 기존 CLI 호환과 공통 개별 작업 외의 새 실행 흐름을 추가하지 않는다.

## 기능 보존 기준

다음 기능은 정리나 재구성 과정에서도 삭제하지 않는다.

- 메모리 전용 Access Key 인증과 비밀값 비기록
- Agent 식별·조회·생성·실행 준비
- 상세 로그를 남기는 원본 빌드에서 실제 도구와 산출물 관찰
- Open API Agent 조회·생성, 기존 ToolChainInfo 정의 선택, STM32CubeIDE의 누락 정의 생성, `updateAgent.toolChains` 기존 매핑 보존·누락 매핑 추가, Agent 재조회. 다른 환경에서 호환 정의가 없으면 사용자 작업을 기다린다. 정의 생성·수정 API 지원과 자동 호출 범위는 [인증과 서버 API 사용 규칙](security-and-api.md#open-api-요청과-agent-설정)을 따른다.
- Visual Studio, CMake/Make, STM32CubeIDE, C#, Java 환경별 파이프라인 패키지
- 원본과 같은 절대 경로의 신규 산출물 검증
- 중단된 동일 작업의 결정·인증 재개
- 성공 후 IDE·평소 빌드에서 재사용할 수 있는 프로젝트 로컬 COVER 적용 상태

## 작업 기록과 재사용 설정의 보관 위치

- `.cover-build/jobs/<jobId>`: 한 작업의 로그, 결정, 진단, 임시 설정. 이후 수동 빌드가 참조하면 안 된다
- `.cover-build/cache/environment-profile.json`: 원본 빌드 환경을 다시 찾는 비용을 줄이는 조사 캐시. COVER 적용 상태가 아니다
- `.cover-ee/applied-state.json`: 현재 프로젝트에 남아 있는 COVER 적용 상태의 단일 기록
- `.cover-ee/config`: IDE 연결 또는 수동 명령이 다시 읽는 props, COVER 설정, JACOV init 등의 검증 사본
- `.cover-ee/backup`: 스킬이 연결을 갱신하거나 해제할 때 사용하는 원본 바이트 백업
- `.cover-ee/build-coverage.cmd`, `.cover-ee/build-coverage.sh`: IDE 밖에서 같은 설정을 재실행하는 보조 진입점. 이것만으로 IDE 적용 성공을 선언하지 않는다

### 빌드 성공 후 남기는 설정

파이프라인 성공 직후 `persistent_setup.publish()`가 검증한 설정을 재사용 위치에 저장하고 환경별 연결을 적용한다.

- Visual Studio는 선택한 `.vcxproj`의 import를 유지한다.
- C#은 Toolbox가 적용한 프로젝트 설정을 유지한다.
- STM32CubeIDE는 `.cproject`에서 선택 빌드 구성 ID를 찾아 `.settings/org.eclipse.cdt.core.prefs`에 래퍼를 맨 앞에 둔 `PATH`와 `COVER_BR_CONFIG=gcc.config`를 적용한다. 검증한 Agent 설정도 `gcc.config`에 적용한다.

  도구 절대 경로 변경이 필요했다면 `.cproject`와 CDT 환경 설정을 각각 복구할 수 있도록 기록한다. 원본은 `gcc.config.cover-ee-backup`과 `.cover-ee/backup`에 보관하고 검증용 `cover-glue-compiler-*.config`는 제거한다.
- CMake와 Make는 명령행 주입이 성공하면 프로젝트 파일을 바꾸지 않고 검증된 명령을 남긴다. 명령행 주입 실패 후 프로젝트 파일 수정으로 성공한 경우에만 수정본을 유지하고 원본을 같은 폴더의 `.cover-ee-backup`에 남긴다.
- Maven은 설치한 JACOV 플러그인과 `setup` goal이 포함된 명령을 남긴다. POM은 수정하지 않는다.
- Gradle은 선택한 플러그인 JAR와 init script를 프로젝트 루트에 복사하고 `--init-script` 명령을 남긴다. Gradle 설정 파일은 수정하지 않는다.

### 다음 작업에서의 일시 해제와 복구

새 작업을 시작할 때 `persistent_setup.suspend()`가 이전에 추가한 프로젝트 파일 연결과 STM32 Agent `gcc.config`를 원본 바이트로 일시 복원한다. 작업이 실패하거나 사용자·모델 입력 대기로 끝나면 `resume()`으로 이전 적용 상태를 되살린다.

새 계측 빌드와 재사용할 설정의 저장과 연결까지 성공했을 때만 새 `APPLIED` 상태로 교체한다. 현재 파일이 기록된 적용 해시와 다르면 사용자 변경을 덮어쓰지 않고 중단한다.

`tests/test_skill_contract.py`는 위 공개 실행 경로의 파일과 조건별 참조 문서 연결을 보호한다. 기능 파일을 제거하거나 이름을 바꿀 때는 대체 구현과 입력·출력 및 동작 규칙 테스트를 같은 변경에 포함한다.
