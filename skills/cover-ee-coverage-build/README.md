# COVER EE 커버리지 빌드 스킬

프로젝트 경로와 자연어 요청을 받아 COVER EE 커버리지 빌드를 수행하는 스킬이다. 프로젝트가 평소 어떻게 빌드되는지 확인하고, 같은 빌드에 COVER 계측 도구를 연결한다. 성공한 설정은 프로젝트에 남겨 사용자가 이후에는 스킬을 호출하지 않고 IDE나 명령으로 다시 빌드할 수 있게 한다.

이 문서는 스킬을 처음 인수받는 개발자와 기술지원 엔지니어를 위한 안내서이자 코드 리뷰 기준이다. 사용자 요청이 어떤 순서로 처리되는지, 각 파일이 무엇을 담당하는지, 변경할 때 어떤 동작을 유지해야 하는지를 설명한다. AI가 실제 작업에서 따라야 할 지침은 [SKILL.md](SKILL.md)에 있다.

기능을 변경할 때에는 README의 기준, 해당 참조 문서, Python 구현과 테스트를 함께 검토한다. 서로 다르면 문서나 코드를 임의로 정답으로 정하지 않고 차이를 확인한다. README에 새 규칙을 적는 것만으로 구현이 바뀌는 것은 아니다.

## 1. 무엇을 수행하는가

기본 작업은 다음 세 가지로 구성된다.

1. **원본 빌드 확인**: 고객의 빌드 방법으로 실제 빌드하고, 사용된 도구와 산출물을 확인한다.
2. **커버리지 빌드**: 원본 빌드에 wrapper, CscHooker 또는 JACOV를 연결하고 계측 결과를 검증한다.
3. **이후 빌드 준비**: 검증한 설정을 유지하고 IDE 또는 수동 명령으로 다시 빌드하는 방법을 제공한다.

커버리지 빌드 성공은 빌드 종료 코드만으로 판단하지 않는다. 실제 계측 처리가 확인되고, 원본 산출물과 **같은 절대 경로**에 이번 빌드의 산출물이 생성·갱신되어야 한다. 출력 경로는 COVER가 빌드 대상을 식별하는 데 중요한 정보이므로 임시 프로젝트나 별도 출력 경로로 바꾸지 않는다.

계측 빌드는 성공했지만 재사용 설정 적용에 실패했다면 두 결과를 구분해 보고한다. 전체 작업은 재사용 설정 적용까지 끝나야 성공으로 기록한다.

프로그램 실행, 별도 테스트 시나리오 수행, 서버 Module 조회, 측정 데이터 수집은 기본 빌드 작업에 포함하지 않는다. 다만 고객이 선택한 원본 goal이나 task 자체에 테스트가 포함되어 있으면 그 빌드 절차는 유지한다. STM32CubeIDE의 메모리 덤프와 CVD 수집은 별도 `cover-ee-coverage-collect` 스킬이 담당한다.

### 이 문서에서 사용하는 용어

- **wrapper**: 원래 빌드 도구 대신 먼저 호출되어 COVER 처리를 수행하고 실제 도구 실행으로 연결하는 프로그램이다.
- **글루 코드**: 계측 결과와 COVER 런타임을 연결하기 위해 생성하는 코드다. 빌드 PC가 아닌 측정 타깃에 맞는 컴파일러로 빌드해야 한다.
- **reporting**: Local Agent와 서버 사이의 상태 보고 활동을 뜻한다. 준비 상태를 확인하는 근거 중 하나다.
- **재사용 설정**: 이번 작업이 끝난 뒤에도 IDE나 수동 명령이 다시 읽을 수 있도록 남기는 COVER 설정이다.

### 지원 환경

| 언어 | 빌드 환경 | COVER 연결 방식 |
| --- | --- | --- |
| C/C++ | Visual Studio / MSBuild | 네이티브 컴파일·링크·아카이브 wrapper |
| C/C++ | CMake | configure 시 실제 도구에 대응하는 wrapper 지정 |
| C/C++ | 직접 Make | 빌드 도구 변수에 wrapper 지정 |
| C/C++ | STM32CubeIDE | IDE headless 빌드에 wrapper와 타깃용 설정 적용 |
| C# | Windows의 MSBuild / dotnet | COVER Toolbox CLI로 CscHooker 연결 |
| Java | Maven / Gradle | JACOV plugin |
| Java | 직접 빌드 | 구조화된 빌드 계획과 JACOV CLI 계측 명령 |

CMake가 생성한 빌드를 내부적으로 Ninja, Make 또는 Visual Studio가 수행하더라도 진입점이 CMake이면 CMake 파이프라인을 사용한다. CMake 소유 관계가 없는 직접 Ninja 빌드는 지원하지 않는다.

한 작업은 하나의 빌드 환경을 처리한다. 같은 솔루션이나 Maven reactor 안의 여러 프로젝트·모듈은 함께 처리할 수 있지만, C++와 Java가 섞인 저장소를 하나의 작업에서 자동으로 순회하지는 않는다.

## 2. 사용자 요청부터 완료까지

흐름을 읽기 전에 실행 구조를 간단히 정리하면 다음과 같다. 자세한 파일 목록은 5절에 있다.

```text
사용자 ↔ AI Agent                     요청 해석, 필요한 선택과 설명
           │ SKILL.md + 해당 참조 문서
           ▼
cover_workflow_runner.py              공개 실행 진입점
           │ cover_auth.py            필요한 시점에 메모리 전용 인증
           ▼
cover_build/workflow.py               작업 생성·재개, 환경 분기, 적용·보고
           ├─ planning/               조사, 원본 계획, 결정 검증
           ├─ pipelines/              환경별 원본 빌드와 계측 순서
           │    └─ services/common    Agent/API, 명령 실행, 도구·산출물 확인
           └─ core/                   상태, 증거, 재사용 설정, 복구, 보고
```

문서는 AI가 판단할 기준을 설명하고, 스크립트는 반복 실행과 검증을 담당한다. 환경마다 빌드 방식이 다르므로 공통 실행기가 모든 환경에 같은 세부 단계를 강제하지 않는다.

예를 들어 사용자가 다음과 같이 요청한다.

> 이 프로젝트를 커버리지 빌드해줘. 평소에는 Release 구성으로 빌드해.

AI는 필요한 입력을 확인한 뒤 Python 실행기를 호출한다. 실행기는 판단 근거가 부족한 지점에서 멈추고, AI가 선택값을 전달하면 같은 작업을 이어 간다.

```text
필수 입력 확인
  → 기존 COVER 설정 일시 해제
  → 프로젝트 조사와 빌드 계획 확정
  → 원본 빌드 및 실제 도구·산출물 확인
  → 인증과 환경별 COVER 준비
  → 커버리지 빌드
  → 실제 계측 처리와 동일 경로 산출물 검증
  → 재사용 설정 적용 및 필요한 재빌드 검증
  → 결과와 이후 빌드 방법 안내
```

### 2.1. 필수 입력 확인

- 프로젝트 경로, COVER Access Key, Open API 메인 웹 서버 URL을 확인한다. `${CODESCROLL_TM_INSTALL_PATH}/agent_kit/open-api-credentials.json`에 유효한 값이 캐시돼 있으면 재사용하고, 없을 때만 빠진 값을 최초 대화에서 한 번에 요청한다.
- `CODESCROLL_TM_SERVER_URL`에서 Local Agent의 remote-service 주소를 읽는다. 메인 웹 서버 주소가 아니므로 Open API 주소로 대신 쓰지 않는다. 사용자 입력이나 유효한 캐시가 없으면 환경변수가 있어도 메인 웹 서버 URL을 요청하고 입력을 기다린다.
- `CODESCROLL_TM_INSTALL_PATH`에서 Local Agent 설치 경로를 읽는다.
- 환경변수가 없거나 잘못됐을 때만 해당 값을 추가로 요청한다.
- 평소 사용하는 원본 빌드 명령이나 스크립트는 선택적으로 받는다. Visual Studio 사용자는 명령 대신 솔루션과 `Configuration|Platform`으로 설명할 수 있다.

Access Key는 이때 받지만 원본 빌드 전에는 서버 인증에 사용하지 않는다. 첫 서버 작업 직전에 현재 인증 프로세스에 전달하며, 명령 인수·파일·로그에는 저장하지 않는다. 첫 인증이 성공하면 Access Key와 Open API URL을 `agent_kit/open-api-credentials.json`에 캐시해 다음 실행부터 재사용한다(일반적인 비저장 정책의 명시적 예외).

현재 공통 입력 절차는 Java에도 적용한다. 기존 JACOV 설정이 있더라도 Access Key, Open API URL, Local Agent 설치 경로를 확인한다. Java에서 Local Agent를 시작하지 않는다는 것은 설치본과 인증 입력도 필요 없다는 뜻이 아니다.

### 2.2. 기존 설정 일시 해제

이전에 이 스킬로 설정한 프로젝트라면 `.cover-ee/applied-state.json`을 먼저 확인한다. 기존 COVER 연결을 그대로 두면 원본 빌드도 wrapper를 거치므로, 기록된 설정을 잠시 해제한다.

현재 파일이 마지막 적용 당시의 해시와 다르면 사용자 변경으로 보고 중단한다. 이전 설정이 있다는 이유로 덮어쓰지 않는다. 이번 작업이 실패하거나 입력 대기로 끝나면 해제했던 이전 설정을 복구한다.

### 2.3. 프로젝트 조사와 계획 확정

프로젝트를 한 번 인덱싱하고 그 목록에서 언어, 빌드 시스템, 진입점, 구성, 타깃 후보를 조사한다. 이전 성공 작업의 환경 프로필이 현재 입력과 맞으면 조사 결과를 재사용할 수 있다.

후보가 하나로 정해지면 스크립트가 선택한다. 후보 간 관계를 읽어야 하면 AI가 빌드 파일과 스크립트를 해석한다. 저장소에 고객의 실제 선택을 알 근거가 없을 때만 사용자에게 묻는다.

사용자가 준 명령도 문자열 그대로 실행하지 않는다. 실행 파일, 인자, 작업 디렉터리, 환경 초기화를 구분한 원본 빌드 계획으로 만들고 검증한다. 이때 고객의 타깃, 프로필, 속성을 임의의 기본 명령으로 바꾸지 않는다.

현재 근거로 결정할 수 있는 항목은 한 묶음으로 정한다. 예를 들어 Java 프로젝트에서 진입점·goal·모듈을 이미 알 수 있는데도 하나씩 질문하며 같은 조사를 반복하지 않는다. 원본 빌드를 실행해야만 알 수 있는 JDK나 산출물은 빌드 후 확인한다. 아직 관찰하지 않은 결과를 미리 채워 넣어 왕복 횟수를 줄이지 않는다.

### 2.4. 원본 빌드와 실제 도구 확인

확정한 계획으로 상세 로그를 남기는 원본 빌드를 수행한다. 네이티브 빌드는 실제 컴파일러·링커·아카이버를, C#은 컴파일러 실행 방식을, Java는 JDK와 컴파일 방식을 확인한다. 최종 산출물의 절대 경로도 함께 기록한다.

설치 폴더에 도구가 있거나 PATH에서 검색된다는 사실만으로 원본 도구를 확정하지 않는다. 네이티브 명령의 옵션이 `@response-file`에 들어 있으면 제한된 로컬 파일을 읽어 역할과 산출물을 판정한다. 응답 파일 내용은 셸 명령으로 실행하지 않는다.

원본 빌드가 실패하면 Agent 설정이나 계측 빌드로 넘어가지 않는다.

### 2.5. COVER 준비와 계측 빌드

원본 도구와 산출물이 확인된 뒤 인증을 수행한다. 이후 단계는 환경에 따라 다르다.

- **네이티브**: 현재 PC의 Agent를 조회·등록하고 실제 도구를 매핑한다. Agent와 wrapper가 준비되면 계측 빌드한다.
- **C#**: Agent의 C# 서비스를 준비하고 Toolbox CLI로 프로젝트에 CscHooker 연결을 적용한다.
- **Java**: JACOV 설정과 설치된 CLI/plugin을 확인한다. JACOV는 설정 파일의 서버로 직접 연결하며, 빌드만을 위해 Local Agent를 시작하지 않는다.

Agent 프로세스가 존재한다는 이유만으로 준비 완료로 보지 않는다. 해당 환경에 필요한 서비스, reporting, 서버 online 상태와 wrapper 동기화 등을 확인한다. 이미 준비된 Agent가 있으면 재사용한다.

### 2.6. 검증과 재사용 설정 적용

환경별 실제 계측 처리와 빌드 성공을 확인하고, 산출물을 원본 빌드 기록과 비교한다. 원본 목록을 그대로 복사해 자기 자신과 비교하지 않으며 현재 빌드에서의 생성·갱신 여부를 별도로 확인한다.

검증이 끝나면 작업용 임시 설정을 재사용 가능한 위치로 옮긴다. Visual Studio와 STM32CubeIDE는 임시 주입을 제거한 빌드도 검증한다. 보조 실행 스크립트가 생성됐다는 사실만으로 IDE 재사용 성공을 선언하지 않는다.

최종 보고는 성공·실패·대기 여부를 첫 줄에 표시한다. 이어서 빌드 환경, 실제 명령, 계측 결과, 산출물, 변경·백업 파일과 이후 빌드 방법을 설명한다.

## 3. 환경별 수행 시나리오

### Visual Studio

1. 솔루션 또는 프로젝트, `Configuration|Platform`, MSBuild를 확정한다. 솔루션을 빌드하면 프로젝트별 구성 매핑을 따른다.
2. diagnostic Rebuild로 실제 MSVC 도구와 산출물을 확인한다.
3. 관찰된 도구를 Agent에 매핑하고 wrapper 준비 상태를 확인한다.
4. 작업용 MSBuild 설정으로 `ExecutablePath` 앞에 wrapper 디렉터리를 두고 커버리지 Rebuild를 수행한다.
5. 성공하면 선택한 `.vcxproj`의 해당 구성에 `.cover-ee/config/cover-msbuild.props`를 연결한다.
6. 임시 `ForceImportBeforeCppTargets`와 `CoverWrapperDirectory` 인자 없이 같은 Rebuild를 수행해 재사용을 검증한다.

props 연결은 선택 구성의 `ImportGroup Label="PropertySheets"`에 둔다. 같은 구성의 기존 그룹을 사용하고 중복 그룹을 만들지 않는다. 이 연결을 통해 속성 관리자와 `VC++ 디렉터리 > 실행 파일 디렉터리`에서 `$(CoverWrapperDirectory)`가 반영된 것을 확인할 수 있다. 빌드 로그에서만 wrapper가 확인되고 IDE 설정에는 연결이 보이지 않는 방식으로 바꾸지 않는다.

이후 사용자는 IDE에서 같은 구성으로 Rebuild한다. 사용자 전역 props는 수정하지 않는다. 상세 절차는 [Visual Studio 문서](references/visual-studio.md)를 참고한다.

### CMake

1. 고객 스크립트, preset, cache와 프로젝트 설정에서 source·build 디렉터리, generator, 구성과 target을 확정한다. preset이 없다는 이유만으로 중단하지 않고 원본 명령과 프로젝트 근거로 계획을 작성한다.
2. 확정한 디렉터리에서 원본 configure와 상세 빌드를 수행하고 실제 도구와 산출물을 확인한다.
3. 원본 빌드에 사용한 선택값을 같은 작업의 기록에 저장한다. 이후 재개 시 인자가 생략돼도 일반적인 `<프로젝트>/build`로 다시 선택하지 않는다.
4. 원본과 같은 build 디렉터리에서 커버리지 configure를 수행한다. 원본에서 관찰된 역할에 맞춰 `CMAKE_C_COMPILER`, `CMAKE_CXX_COMPILER`, `CMAKE_AR`, 필요한 경우 `CMAKE_LINKER`에 wrapper를 명령행으로 지정한다.
5. configure 또는 실제 상세 빌드에서 명령행 지정이 무시된 사실을 확인한 경우에만 프로젝트 소유 `CMakeLists.txt`와 직접 참조 `.cmake`의 해당 도구 설정을 백업·최소 수정한다.
6. 실제 wrapper 처리와 동일 경로의 신규 산출물이 확인되면, 성공한 configure/build 명령과 필요한 수정본을 재사용할 수 있도록 남긴다.

원본 configure와 커버리지 configure는 각각 수행하고 둘 사이의 캐시를 그대로 재사용하지 않는다. 이는 **빌드 디렉터리를 새 경로로 바꾼다는 뜻이 아니다.** 선택한 build 디렉터리와 최종 산출물 경로는 유지한다. 재개 시 선택값을 보존하는 방식은 8절에서 설명한다.

### 직접 Make

1. CMake가 생성한 Makefile이 아닌 고객의 직접 Make 진입점인지 확인한다.
2. 실제 사용하는 target, 도구 변수, 환경 초기화와 clean/build 명령을 확정한다.
3. 원본 빌드의 상세 명령에서 실제 컴파일러·링커·아카이버와 산출물을 확인한다.
4. 원본에서 관찰된 역할에 해당하는 `CC`, `CXX`, `LD`, `AR` 또는 확인된 고객 도구 변수를 명령행으로 전달해 wrapper를 적용한다.
5. 하드코딩·`override`·하위 Make 호출 때문에 wrapper가 적용되지 않은 사실을 확인한 경우에만 프로젝트 소유 Makefile, 포함된 `.mk`, 도구를 직접 호출하는 스크립트를 백업·최소 수정한다.
6. 원래 빌드 위치에서 clean/build하고 실제 wrapper 처리와 동일 경로의 신규 산출물을 확인한다. 성공한 Make 명령과 필요한 수정본을 남긴다.

### CMake·Make에서 공통으로 지키는 변경 범위

두 환경 모두 **명령행 주입 → 실제 실패 증거 확인 → 프로젝트 파일 최소 수정** 순서를 따른다. 명령행 지정이 실패했다고 환경변수, PATH 선두 변경, toolchain overlay를 차례로 시도하는 방식으로 바꾸지 않는다.

파일 수정 전에는 기존 사용자 변경을 포함한 현재 바이트를 백업한다. 수정으로 성공하면 원본과 같은 폴더에 `<원본파일명>.cover-ee-backup`을 남기고 수정본을 유지한다. 실패하면 이번 변경을 복구하되 동시 변경이 있으면 덮어쓰지 않는다. 공유 SDK나 프로젝트 밖 파일은 자동 수정하지 않는다.

명령행으로 성공했다면 고객 빌드 파일은 수정하지 않는다. 이 경우 wrapper 인자가 없는 평소 명령까지 커버리지 빌드가 된다고 보장하지 않으며, 스킬이 남긴 검증된 명령을 사용한다. 상세 절차는 [GCC CMake·Make 문서](references/gcc-cmake-make.md)를 참고한다.

### STM32CubeIDE

1. 프로젝트 설정에서 STM32CubeIDE 관리 프로젝트와 빌드 구성을 확인한다.
2. IDE 실행 파일의 headless `-cleanBuild`로 원본 빌드를 수행한다. 생성된 Makefile을 독립 빌드 진입점으로 바꾸지 않는다.
3. 실제 타깃 도구를 Agent에 매핑한다. 호환 ToolChainInfo가 없으면 지정된 STM32 파서 템플릿으로 정의를 생성한다.
4. 글루 코드를 컴파일할 타깃 C++ 드라이버를 확인하고 Agent `gcc.config`의 작업용 복사본에 적용한다.
5. 같은 IDE 빌더에 `-Ep` 등으로 wrapper 환경을 전달한다. 도구 명령이 절대 경로로 고정되어 wrapper를 우회할 때만 `.cproject`의 해당 설정을 백업·최소 수정한다.
6. 성공하면 선택 구성의 `.settings/org.eclipse.cdt.core.prefs`에 wrapper 선행 PATH와 `COVER_BR_CONFIG=gcc.config`를 유지하고 검증한 Agent 설정을 적용한다.
7. 새 workspace에서 임시 환경 주입 없이 같은 구성을 다시 빌드해 IDE 재사용을 확인한다.

wrapper 대상은 C/C++, 링크, 아카이브 역할이다. ASM이나 objcopy 같은 SDK 후처리 도구를 새로 치환하지 않는다. 글루 코드용 C++ 드라이버는 원본 빌드에서 관찰되지 않았더라도 별도 규칙으로 확인할 수 있지만, 이를 Agent wrapper 매핑에 추가하지는 않는다.

이후 사용자는 IDE에서 같은 구성으로 Clean/Rebuild한다. 펌웨어 다운로드와 측정은 별도 작업이다. 상세 절차와 파서 본문은 [STM32CubeIDE 문서](references/stm32cubeide.md)에 있다.

새 workspace 검증은 저장된 프로젝트 설정만으로 headless 재빌드가 되는지 확인하는 절차다. 사용자가 열어 둔 기존 workspace의 캐시나 화면 상태까지 직접 확인한 결과는 아니다.

### C#

1. Windows에서 솔루션·프로젝트, 구성, 대상 프레임워크와 빌드 도구를 확정한다.
2. 기존 COVER 설정은 Toolbox CLI의 `disable`로 해제한다.
3. 필요한 NuGet restore를 수행하고 원본 빌드에서 실제 C# 컴파일러를 확인한다.
4. Agent를 준비하고 Toolbox 입력 설정에 확인된 컴파일러를 반영한다.
5. Toolbox CLI의 `enable`로 고객 프로젝트에 wrapper 연결을 적용한 뒤 같은 조건으로 빌드한다.
6. 선택 프로젝트의 CscHooker 처리와 산출물을 확인한다. 정상적인 소스 제외만으로 계측 실패라고 판단하지 않는다.
7. 성공한 프로젝트 연결은 유지한다. 실패 시 Toolbox로 연결을 해제한다. 작업 중 바꾼 Toolbox 입력 설정은 성공·실패와 관계없이 복구한다.

Toolbox CLI는 직접 계측하는 도구가 아니라 CscHooker가 호출되도록 빌드 연결을 설정한다. 고객 `.csproj`의 COVER 설정도 Toolbox로 변경하며 AI가 XML을 수동 편집하지 않는다. 상세 절차는 [C# 문서](references/csharp.md)를 참고한다.

### Java Maven·Gradle·직접 빌드

Java는 원본 빌드에서 JDK와 산출물을 확인한 뒤 설치된 JACOV 도구의 메타데이터를 읽어 계측 방법을 구성한다.

- **Maven**: 고객 goal·profile·property를 유지한다. JACOV plugin을 Maven 로컬 저장소에 먼저 설치하고 별도 빌드 호출에서 `setup` goal을 적용한다. 필요한 lower JAR의 로컬 등록과 애플리케이션 런타임 의존성 주입은 구분한다. POM은 변경하지 않고 재실행 명령을 제공한다.
- **Gradle**: 원본 빌드의 관찰용 init script로 실제 JDK와 archive 경로를 확인한다. 계측에는 설치된 JACOV JAR의 경로가 반영된 별도 init script를 사용한다. 성공 후 JAR와 init script를 프로젝트 루트에 두고 `--init-script` 명령을 제공한다. 고객 Gradle 빌드 파일은 변경하지 않는다.
- **직접 빌드**: 검증된 `JAVA_CLI` 원본 계획과 계측 인자가 필요하다. 자동으로 정할 수 없으면 모델 결정으로 받는다. 계측 소스의 임시 위치와 최종 산출물 위치를 구분하며, 최종 산출물은 원본 경로에 만들어야 한다.

커버리지 clean 전에 설정 파일, plugin과 실행 환경, JACOV 서버 TCP 연결을 확인한다. TCP 연결 성공은 제품 인증 성공을 의미하지 않으며, 실제 연결·계측 성공은 빌드 결과에서 확인한다.

Maven·Gradle은 JACOV 집계 또는 파일별 결과로 계측 성공을 확인한다. 정상적인 `[IGNORE]`와 계측 자체를 건너뛴 경우를 구분한다. 계측 실패가 있거나 실제 성공을 확인할 수 없으면 빌드 종료 코드가 정상이어도 성공 처리하지 않는다.

Maven·Gradle 빌드 단계에서는 `addDependencyJavaLib=false`로 런타임 선택을 미룬다. 성공 보고에서 다음 두 실행 방식을 안내하되 동시에 적용하지 않는다.

- **LOWER**: `tm-javalib-lower.jar`를 실행 classpath에 추가해 Local Agent와 연동한다.
- **JavaLib Agent**: `cover-javalib-agent.jar`를 실행 JVM의 `-javaagent`로 연결한다.

실제 적용 방식은 측정 실행 환경을 확인해 선택한다. 런타임 안내와 프로그램 실행·측정 검증은 별개다. 상세 절차는 [Java 문서](references/java.md)를 참고한다.

## 4. AI와 스크립트의 역할

AI는 사용자 요청을 해석하고 작업을 진행하지만, 명령 실행과 상태 판정은 Python 실행기에 맡긴다.

| 담당 | 수행하는 일 |
| --- | --- |
| AI | 누락 입력 요청, 여러 후보의 의미 해석, 근거가 있는 선택값 전달, 실패 설명과 최종 보고 |
| 조사·계획 코드 | 파일·설정·로그 수집, 단일 후보 확정, 명령 구조화와 결정 검증 |
| 환경별 파이프라인 | 원본 빌드, 해당 환경의 COVER 준비, 계측 빌드와 검증 |
| 공통 실행·상태 코드 | 프로세스 실행, 로그, 체크포인트, 백업·복구, 재사용 설정과 결과 기록 |

결정이 필요하면 실행기는 `WAITING_MODEL` 또는 `WAITING_USER`와 필요한 항목을 남긴다. AI는 검증된 결정 파일을 만들어 같은 `job.json`으로 재개한다. 성공 기록을 직접 고치거나 검증을 생략해 다음 단계로 넘기지 않는다.

## 5. 코드와 문서 구조

```text
cover-ee-coverage-build/
├─ SKILL.md                     AI의 작업 지침과 참조 문서 안내
├─ README.md                    동작·시나리오·구조 안내
├─ VERSION                      버전 파일
├─ agents/openai.yaml           스킬 표시·호출 메타데이터
├─ assets/parser-config/
│  └─ stm32cubeide.conf         STM32 파서 템플릿
├─ references/                  공통 및 환경별 상세 문서
│  └─ cover-open-api-v1.json    Open API 요청에 사용하는 명세
├─ scripts/
│  ├─ cover_workflow_runner.py  공개 실행 진입점
│  ├─ cover_auth.py             메모리 전용 인증 진입점
│  ├─ cover_build_executor.py   저수준 하위 명령 CLI(파서와 main)
│  ├─ project_index.py          프로젝트 파일 목록과 입력 식별
│  ├─ project_discovery.py      프로젝트 환경 탐지
│  ├─ managed_tools.py          Toolbox·JACOV 설치 도구 조사
│  ├─ cover_build/
│  │  ├─ workflow.py            전체 실행과 환경별 분기
│  │  ├─ common/               경로·파일·서버·Agent·명령 실행 등 공용 기능
│  │  ├─ pipelines/            환경별 패키지: 수행 순서와 그 환경의 선택·실행·재사용 설정
│  │  ├─ planning/             후보·계획·결정 처리
│  │  ├─ services/             여러 환경이 함께 쓰는 Agent·API·글루 컴파일러·백업
│  │  └─ core/                 상태·증거·설정 유지·보고
└─ tests/                       기능·실패·복구 테스트
```

### 실행 코드는 어디부터 읽는가

1. [workflow.py](scripts/cover_build/workflow.py)에서 작업 생성, 조사, 분기, 설정 적용과 결과 기록을 읽는다.
2. [registry.py](scripts/cover_build/pipelines/registry.py)에서 관심 환경의 파이프라인을 찾는다. CMake와 Make는 `gcc/`, Java의 세 방식은 `java/` 패키지를 사용한다.
3. 해당 `pipelines/<환경>/__init__.py`의 `run` 흐름을 따라 원본 빌드와 계측 빌드의 경계를 확인한다.
4. 같은 패키지의 모듈(`tools.py`, `selection.py`, `original.py`, `msbuild.py`, `planning.py`, `persistent.py`)과 `services/`, `common/`을 읽어 실제 명령과 검증 방법을 확인한다.
5. 작업 재개는 `core/state_store.py`, 성공 후 설정 유지와 복구는 `core/persistent_setup.py`와 각 환경 패키지의 `persistent.py`를 읽는다.

모델이 사용하는 공개 진입점은 `cover_workflow_runner.py`와 `cover_auth.py`다. 실행기 전체를 우회해 내부 서비스를 개별 호출하는 방식으로 작업하지 않는다.

### 실행 진입점 예시

다음은 유지보수자가 실행 구조를 확인하기 위한 예시다. 고객은 자연어로 요청하고 AI가 필요한 입력과 결정을 처리한다. `${SKILL_ROOT}`는 이 README와 SKILL.md가 있는 실제 디렉터리의 절대경로로 바꾼다. 작업 디렉터리를 스킬 설치 폴더로 바꿀 필요는 없다. 꺾쇠괄호 항목도 실제 값으로 바꾸며 Access Key는 인자로 넣지 않는다.

```text
# 새 작업의 계획 확인: 실제 빌드와 설정 적용은 수행하지 않는다.
python "${SKILL_ROOT}/scripts/cover_workflow_runner.py" --project-root "<프로젝트 절대 경로>" --open-api-server-url <메인 웹 서버 URL>

# 새 작업 실행: 첫 서버 작업 직전에 현재 터미널에서 Access Key를 입력한다.
python "${SKILL_ROOT}/scripts/cover_workflow_runner.py" --project-root "<프로젝트 절대 경로>" --open-api-server-url <메인 웹 서버 URL> --execute --prompt-credentials

# 결정 대기 작업 재개: 실행기가 생성·검증한 결정 파일을 사용한다.
python "${SKILL_ROOT}/scripts/cover_workflow_runner.py" --job-context "<동일 작업의 job.json>" --decision "<검증된 결정 파일>" --execute --prompt-credentials
```

`--open-api-server-url`을 생략하면 `${CODESCROLL_TM_INSTALL_PATH}/agent_kit/open-api-credentials.json`에 캐시된 값을 사용한다. 캐시도 없으면 새 작업 생성이 `OPEN_API_MAIN_WEB_SERVER_URL_REQUIRED`로 중단된다.

이 명령만으로 후보 선택이 생략되지는 않는다. 결정 파일 작성은 [결정 파일 문서](references/decision-contract-v2.md)를 따른다. AI 도구의 실행 권한과 Visual Studio 호스트 실행 조건은 [SKILL.md](SKILL.md)를 따르며, 실행기 인자만으로 권한이 부여되는 것은 아니다.

### 기능별 코드 위치

| 확인할 내용 | 주요 파일 |
| --- | --- |
| 빌드 시스템·진입점·구성 선택 | `planning/selection.py` |
| 원본 빌드 계획과 근거 | `planning/build_plan.py` |
| 사전 환경조사 JSON | `planning/survey.py` |
| 결정 파일 생성과 재개 | `planning/decision_file.py`, `planning/resolution.py` |
| MSBuild 실행 | `pipelines/visual_studio/msbuild.py` |
| Visual Studio 도구 탐색과 커버리지 계획 | `pipelines/visual_studio/tools.py`, `pipelines/visual_studio/planning.py` |
| GCC 원본 관찰과 계측 실행 | `pipelines/gcc/original.py`, `services/gcc_coverage.py` |
| GCC 래퍼 준비와 CMake/Make 계획 | `common/gcc_tools.py` |
| CMake 원본 선택값 저장·재개 검증 | `pipelines/gcc/selection.py`, `pipelines/gcc/__init__.py` |
| STM32CubeIDE 도구·구성 확인 | `pipelines/stm32cubeide/tools.py` |
| 글루 코드용 C++ 컴파일러 | `services/glue_compiler.py` |
| Agent 조회·생성·매핑 | `services/agent_open_api.py` |
| Local Agent 확인 | `services/agent_local.py` |
| 홈 JACOV 설정 생성 | `pipelines/java/jacov_config.py` |
| 프로젝트 파일 백업 | `services/project_backup.py` |
| 원본 도구·산출물 증거 | `core/original_build_evidence.py`, `core/job_evidence.py` |
| 동일 작업 재개·시간 기록 | `core/state_store.py` |
| 다른 작업의 환경조사 재사용 | `core/environment_profile.py` |
| 성공 설정 적용·해제·복구 | `core/persistent_setup.py`, `pipelines/<환경>/persistent.py`, `common/project_edit.py` |
| 명령 실행·빌드 로그 판독·산출물 스냅샷 | `common/processes.py`, `common/native_logs.py` |
| Open API 요청과 서버 원점 | `common/open_api.py`, `common/servers.py` |
| 작업 컨텍스트·무결성·경로 | `common/jobs.py`, `common/fileio.py`, `common/pathutil.py` |
| 현재 PC의 Agent 식별·시작 | `common/agent_host.py` |
| 최종 보고 | `core/build_reporting.py` |

`cover_build_executor.py`는 하위 명령 파서와 `main`만 가진 CLI다. 각 하위 명령의 구현은 `common/`과 환경 패키지의 함수이며, 파이프라인도 같은 함수를 직접 호출한다. 환경별 코드는 `pipelines/<환경>/` 패키지 하나에 모여 있고, 여러 환경이 함께 쓰는 Agent·API·글루 컴파일러·백업·GCC 커버리지 실행만 `services/`에 둔다. 세부 책임은 [구현 구조 문서](references/architecture.md)에서 확인한다.

문서와 코드의 역할도 구분한다. `SKILL.md`는 작업 지침과 필수 규칙, `references/`는 조건별 상세 절차, README는 전체 구조와 변경 검토 기준을 담당한다. 실제 옵션과 JSON의 필드·자료형은 이를 생성하고 읽는 Python 코드를 함께 확인한다. Open API 필드는 `references/cover-open-api-v1.json`과 API 호출 구현을 대조한다. 문서에 적힌 예제만 바꾸고 실행기의 입력·출력을 그대로 두면 이전과 같은 형식 불일치가 다시 발생할 수 있다.

### 상황별 참조 문서

| 필요한 설명 | 문서 |
| --- | --- |
| 공통 순서와 완료 기준 | [workflow.md](references/workflow.md) |
| 빌드 후보 수집과 선택 | [discovery-and-planning.md](references/discovery-and-planning.md) |
| 원본 명령 작성·검증 | [original-build-commands.md](references/original-build-commands.md) |
| 환경조사 기록 | [environment-survey.md](references/environment-survey.md) |
| 결정 파일 형식 | [decision-contract-v2.md](references/decision-contract-v2.md) |
| 인증·서버·Agent 처리 | [security-and-api.md](references/security-and-api.md) |
| 재개·캐시·시간 측정 | [performance.md](references/performance.md) |
| 최종 보고 형식 | [build-reporting.md](references/build-reporting.md) |

환경별 문서는 앞의 시나리오에서 연결한다. 모든 문서를 매 작업마다 읽는 대신 공통 흐름과 선택한 환경에 필요한 문서를 읽는다.

## 6. 서버 주소와 인증의 구분

Open API, Local Agent, JACOV는 서로 다른 연결이다. 같은 제품을 사용하더라도 주소와 포트가 같아야 하는 것은 아니다.

| 연결 | 주소의 출처 | 용도 |
| --- | --- | --- |
| Open API | 사용자가 알려준(또는 `agent_kit/open-api-credentials.json` 캐시의) 메인 웹 서버 URL, HTTPS | Agent·ToolChainInfo 조회와 설정 |
| Local Agent | `CODESCROLL_TM_SERVER_URL` | Local Agent와 `tm-remote-service` 연결 |
| Java JACOV | 선택한 `cover_jacov.json`의 `url` | Java 계측과 분석정보 전송 |

실행기의 `--server-url`은 Local Agent의 remote-service 주소, `--open-api-server-url`은 사용자가 입력한 메인 웹 서버 주소다. 후자가 없고 유효한 캐시도 없으면 서버 연결 전에 `WAITING_USER`(종료 코드 3)로 입력을 기다린다. 환경변수 주소의 scheme·port·path를 바꿔 메인 웹 서버 주소를 추측하지 않는다.

Open API는 Access Key로 v1 API를 호출한다. 기본 `curl -k` 정책은 Open API에만 적용한다. 엄격한 인증서 검증을 요청하면 새 작업에 `--verify-tls`를 사용한다. 이 옵션을 JACOV JVM의 인증서 검증 예외로 전파하지 않는다. Local Agent의 HTTP/HTTPS 처리도 별도이며 [인증과 API 문서](references/security-and-api.md)를 따른다.

네이티브·C#의 Agent 등록에는 제품 하드웨어 ID를 사용한다. Agent의 원본 도구 매핑은 `toolChains`, 컴파일러 파서 정의는 ToolChainInfo로 구분한다. 실제 원본 도구와 역할이 일치하는 기존 매핑은 변경하지 않는다. 매핑이 없는 도구에만 호환 정의를 선택하며, STM32CubeIDE에서 정의가 없을 때만 지정된 템플릿으로 자동 생성한다. 그 밖의 환경에서 정의가 없으면 사용자 작업을 기다린다. 공유 정의는 자동 수정하지 않는다.

일반 Agent 시작에는 Windows의 `bin/run_agent_controller.cmd`, Linux의 `bin/cvctl.sh start`를 사용한다. 이미 실행 중인 현재 Agent가 준비 조건을 만족하면 새 프로세스를 추가하지 않는다. 포트를 사용 중이라는 사실만으로 종료하거나, 프로세스가 있다는 사실만으로 준비됐다고 판단하지 않는다.

Linux 실행 스크립트가 설치 환경 파일인 `cvrg_env.sh`를 사용하는 경우에는 실제 설치본의 파일과 실행 로그를 확인한다. 이 파일의 Agent 서버 포트와 사용자가 입력한 Open API 메인 웹 포트는 역할이 다르므로, 숫자가 다르다는 이유만으로 잘못된 설정으로 판정하지 않는다.

### `cover_jacov.json` 처리

1. `COVER_JACOV_CONFIG`가 있으면 지정한 파일을 사용한다. 파일이 없거나 잘못됐다고 홈 파일로 조용히 바꾸지 않는다.
2. 별도 지정이 없으면 실행 계정 홈의 `cover_jacov.json`을 사용한다.
3. 홈 파일도 없으면 `.cstm_HardwareAddress`와 Open API Agent 목록을 대조해 현재 PC의 Agent를 찾는다.
4. 정확히 하나의 Agent가 확인되면 그 ID를 `key`, `CODESCROLL_TM_SERVER_URL`을 `url`로 홈 파일을 생성한다. 필요한 식별 정보나 Agent가 없으면 사용자 입력 대기로 중단한다.
5. 기존 파일은 덮어쓰지 않는다. 설정 경로만 JACOV 프로세스의 `COVER_JACOV_CONFIG`로 전달한다.

Java key를 제품 설정 파일에 저장하는 것은 Access Key를 작업 JSON이나 로그에 저장하는 것과 다른 절차다. 두 비밀값 모두 명령 인수와 보고에 노출하지 않는다. 기존 JACOV URL을 Open API 주소로 재작성하거나 포트 일치를 요구하지 않는다.

## 7. 작업 기록과 고객에게 남는 파일

작업 기록과 재사용 설정은 수명과 목적이 다르다. 아래 `.cover-build`와 `.cover-ee`는 기본적으로 고객 프로젝트 루트 아래 경로다. 별도 `--jobs-root`를 지정한 작업은 `job.json`에 기록된 작업 디렉터리를 기준으로 로그와 단계를 찾는다.

| 위치 | 내용 |
| --- | --- |
| `.cover-build/jobs/<jobId>/job.json` | 작업 ID, 경로, 서버 설정 등 작업 기준 |
| 작업의 `temporary/` | 프로젝트 인덱스, 사전 조사, 결정 파일과 임시 설정 |
| 작업의 `temporary/original-cmake-selection.json` | 같은 CMake 작업에서 원본 빌드의 진입점·build 디렉터리·generator·구성을 유지하는 기록 |
| 작업의 `stages/` | 단계별 최신 결과 |
| 작업의 `events/` | 단계 실행·재사용 이력 |
| 작업의 `logs/` | 원본·계측 빌드 및 시도별 로그 |
| 작업의 `workflow-state.json` | 현재 상태와 `result.buildSummary` |
| `.cover-build/cache/environment-profile.json` | 다음 작업의 조사를 줄이는 환경 프로필 |
| `.cover-ee/applied-state.json` | 현재 유지 중인 COVER 설정과 파일 해시 |
| `.cover-ee/config/`, `.cover-ee/backup/` | 재사용 설정과 원본 백업 |
| `.cover-ee/build-coverage.cmd`, `.cover-ee/build-coverage.sh`, `.cover-ee/run-coverage.py` | 수동 재실행 진입점 |

`cover-environment-survey.json`은 원본 빌드 방법을 결정하기 위한 사전 조사다. 실제 원본 도구와 산출물은 별도 `original-build-evidence.json`에 기록하고, 계측 결과는 단계 JSON에 기록한다. 사전 조사서를 계속 확장해 전체 작업 보고서로 사용하지 않는다.

이후 수동 빌드가 `.cover-build/jobs/<jobId>`를 참조하면 해당 작업 디렉터리가 사라졌을 때 실패한다. 따라서 재사용 설정을 적용할 때 작업용 경로를 제거한다. `.cover-ee` 외에 환경별로 유지하는 파일은 3절의 시나리오를 따른다.

## 8. 대기·실패·재실행

| 상태 | 의미 | 다음 처리 |
| --- | --- | --- |
| `WAITING_MODEL` | 파일 관계나 명령에 해석이 필요함 | AI가 근거를 확인하고 결정 파일로 같은 작업 재개 |
| `WAITING_USER` | 인증 입력·고객 선택·환경 준비가 필요함 | 필요한 입력이나 조치 후 같은 작업 재개 |
| `FAILED` | 빌드·연결·계측·검증·설정 적용 중 실패 | 실패 단계와 로그를 확인하고 수정 범위 결정 |
| `SUCCEEDED` | 전체 실행 성공을 나타내는 `status` 값 | 결과와 수동 빌드 방법 안내 |

성공한 `workflow-state.json`에는 `status=SUCCEEDED`, `state=COVERAGE_BUILD_VERIFIED`, `result.persistentSetup.state=APPLIED`가 함께 기록된다. `COVERAGE_BUILD_VERIFIED`는 이름만 보면 계측 단계 결과처럼 보이지만, 현재 공통 실행기는 재사용 설정 적용까지 끝난 뒤 이 최종 상태를 기록한다. 계측만 성공하고 이후 설정 적용이 실패한 경우 전체 결과는 `FAILED`이며 단계 기록에서 계측 성공을 별도로 확인한다.

실행기 종료 코드는 각각 `WAITING_MODEL=2`, `WAITING_USER=3`, `FAILED=1`, `SUCCEEDED=0`이다. `--execute`가 없는 계획 확인 실행은 실제 빌드나 재사용 설정 적용을 수행하지 않는다.

### 같은 작업 재개와 새 작업의 조사 재사용

같은 작업의 재개와 새 작업의 조사 재사용을 구분한다. 성공 후 사용자가 실행하는 `.cover-ee` 수동 빌드도 이 둘과는 별개다.

| 구분 | 재사용하는 내용 | 다시 확인할 내용 |
| --- | --- | --- |
| 같은 `job.json` 재개 | 입력과 증거가 유효한 성공 단계 | 관련 파일·명령·도구·산출물 변경 여부 |
| 새 작업의 환경 프로필 사용 | 현재 프로젝트와 일치하는 조사 결과와 원본 명령 후보 | 새 원본 빌드, 실제 도구·산출물, 계측 결과 |
| `.cover-ee` 수동 빌드 | 적용된 COVER 설정과 검증된 명령 | 이번 수동 빌드의 실행 결과. 과거 스킬 성공이 새 빌드의 성공을 대신하지 않음 |

후속 선택을 보완했다는 이유만으로 무관한 원본 빌드를 반복하지 않는다. 반대로 소스·빌드 정의·명령·구성·도구가 바뀌면 관련 단계를 다시 수행한다. 다른 작업의 기록 사용을 금지한 요청에는 새 작업과 `--no-environment-profile`을 사용한다. 이 옵션이 현재 작업 안의 안전한 재개까지 금지하는 것은 아니다.

인증 성공 기록은 Access Key를 대신하지 않는다. 새 프로세스로 재개하면 키를 다시 메모리 전용 방식으로 전달한다. 채팅에서 이미 받은 키를 사용할 수 있다면 사용자에게 같은 값을 반복 요청하지 않는다.

### CMake 원본 선택값 보존

원본 빌드가 preset의 `out/build/linux-gcc-release`에서 성공했는데, 재개 명령에 `--gcc-build-directory`가 빠졌다는 이유로 계측 단계가 `build`를 선택하면 다른 산출물을 만들게 된다. 이를 막기 위해 원본 빌드와 계측 계획은 같은 선택값을 사용한다.

1. 원본 계획에 `originalBuildDirectory`가 있으면 CMake 선택에도 그 값을 반영한다. 명시적으로 받은 디렉터리·generator·구성이 계획과 충돌하면 중단한다.
2. 원본 빌드 성공 후 실제 빌드 디렉터리와 선택값을 대조하고 `temporary/original-cmake-selection.json`에 작업 ID와 무결성 정보를 포함해 저장한다.
3. 같은 작업을 재개할 때에는 이 파일을 검증하고 저장한 진입점·build 디렉터리·generator·구성을 사용한다. 커버리지 configure로 달라진 cache에서 원본 환경을 다시 추정하지 않는다.
4. 새 입력이 저장값과 충돌하면 중단한다. build 디렉터리 충돌은 `COVERAGE_OUTPUT_PATH_CHANGED`, 진입점·generator·구성 충돌은 `CMAKE_ORIGINAL_SELECTION_CONFLICT`로 구분한다. 실제 빌드 환경을 바꾸려면 새 작업을 시작한다.

이 기록은 원본 빌드의 선택값을 보존하기 위한 것이지, 소스가 변경됐는데도 예전 빌드를 성공으로 간주하기 위한 것이 아니다. 공통 체크포인트의 입력 검증은 계속 적용한다.

### 실패 복구와 성능 진단

실패한 계측 빌드가 clean으로 원본 산출물을 지운 경우처럼 환경별 복구가 필요한 상황도 있다. 복구 빌드의 성공을 계측 성공으로 보고하지 않는다. 파일 복구 중 동시 변경이 확인되면 무조건 원본을 덮어쓰는 대신 중단한다.

재시도 로그는 `coverage-java.attempt-001.log`처럼 시도별로 남긴다. 단계 JSON에서 실제 로그를 찾고, 마지막 시도만 남는 대표 로그를 전체 실패 이력으로 오해하지 않는다. 시간 분석에서는 외부 빌드 명령, 실행기 활성 시간, 사용자·모델 결정 및 재개 대기를 구분한다. 단계 사이의 공백 전체를 컴파일 시간이나 순수 모델 추론 시간으로 설명하지 않는다.

성능 개선은 기존 기록과 시간 측정을 활용한다. 보고서를 만들려고 추가 빌드나 서버 요청을 수행하지 않는다. 원본 빌드 관찰, 실제 계측 확인, 동일 경로 산출물 검증을 삭제해 실행 시간을 줄이지 않는다. 상세 재사용 조건과 측정 항목은 [성능 문서](references/performance.md)를 따른다.

## 9. 코드 리뷰 기준과 테스트

새 환경은 `pipelines/registry.py`에 등록하고 별도 파이프라인에 수행 순서를 둔다. 공통 실행기에 환경별 예외를 계속 추가하거나 서로 다른 계측 방식을 같은 단계 수로 맞추지 않는다.

리뷰어와 코드 리뷰 봇은 변경된 경로에 해당하는 기준을 확인한다. 파일 이동이나 함수 이름 변경 자체보다 아래 동작이 보존됐는지가 중요하다.

| 변경 영역 | 유지해야 하는 동작 | 검토할 실패 사례 |
| --- | --- | --- |
| 조사·결정 | 생산자와 소비자가 같은 필드·자료형·대기 조건을 사용한다 | 프레임워크 목록을 한쪽에서 문자열로 비교해 같은 질문이 반복됨 |
| 원본 계획 | 고객 진입점·타깃·구성·환경 초기화를 유지한다 | 고객 명령을 근거 없이 기본 `build`로 교체함 |
| 실제 도구 | 이번 원본 상세 빌드에서 관찰된 실행 경로와 역할을 매핑한다 | `cc` 별칭을 다른 바이너리 이름으로 바꾸거나 wrapper 자체를 원본으로 등록함 |
| 글루 코드 | 타깃 C++ 드라이버를 별도로 확인한다 | 빌드 PC용 컴파일러를 쓰거나 보조 도구를 관찰된 wrapper 역할로 추가함 |
| 출력 경로 | 현재 계측 산출물을 원본과 같은 절대 경로에서 확인한다 | 임시 디렉터리에서 성공한 결과를 원본 빌드와 같다고 보고함 |
| CMake 재개 | 저장한 원본 선택값을 검증·사용한다 | 재개 인자가 빠져 preset 경로 대신 기본 `build`가 선택됨 |
| 단계 재사용 | 유효한 성공 단계만 재사용하고 관련 입력 변경은 감지한다 | 인증 입력 방식이 바뀔 때마다 원본을 반복하거나 소스 변경을 놓침 |
| Agent/Open API | 기존 매핑을 변경하지 않고 누락된 항목만 추가한 뒤 재조회한다 | 링커의 정상 `toolChainInfo=null`을 오류로 보거나 공유 정의를 자동 수정함 |
| 서버 연결 | Open API·Local Agent·JACOV 주소와 TLS 정책을 분리한다 | JACOV에 Open API 포트·HTTPS·인증서 예외를 강제함 |
| 비밀값 | Access Key와 JACOV key를 명령·작업 기록·로그에 남기지 않는다 | 제품의 `Key:` 출력을 그대로 저장함. 누락 홈 JACOV 제품 설정 생성은 별도 허용 절차를 따름 |
| CMake·Make 주입 | 명령행 실패 증거가 있을 때만 프로젝트 파일을 최소 수정한다 | 처음부터 파일을 수정하거나 원본 툴체인 바이너리를 교체함 |
| C# 설정 | 고객 프로젝트는 Toolbox로 변경하고 실제 CscHooker 처리를 확인한다 | Toolbox 종료 코드만으로 성공하거나 정상 제외만으로 실패 처리함 |
| Java 계측 | JACOV 실제 결과와 빌드 산출물을 확인한다 | 집계 문구가 없다는 이유만으로 유효한 파일별 결과를 버림 |
| IDE·수동 재사용 | 작업 임시 경로에 의존하지 않고 환경별 연결과 검증을 완료한다 | 보조 스크립트만 생성하고 `APPLIED`로 기록함 |
| 복구 | 현재 바이트와 기존 사용자 변경을 보존한다 | 동시 변경을 덮어쓰거나 전역 설정·공유 SDK를 무단 변경함 |
| 결과 보고 | 첫 줄의 최종 판정과 단계별 결과를 일치시킨다 | 계측 성공·설정 적용 실패를 전체 성공으로 보고함 |

기준 자체를 바꾸는 요구가 있으면 코드만 고치지 않는다. 변경 이유, 영향받는 환경, 복구 방식과 테스트를 함께 제시하고 문서도 맞춘다. 단순 리팩토링에서는 기능과 수행 순서를 그대로 유지한다.

테스트는 `tests/`에 있으며 환경별 실행, 계획과 결정, Agent/API, 재사용 설정, 보고를 나누어 확인한다. `SKILL.md`가 있는 스킬 설치 폴더에서 `python -m unittest discover -s tests`로 실행한다. 단위 테스트 통과는 실제 서버·IDE·고객 프로젝트 검증을 대신하지 않으므로 결과 보고에서 둘을 구분한다.

예를 들어 CMake 선택과 재개를 변경하면 `tests/test_cmake_resume_selection.py`에서 원본 계획 우선 적용, 인자 없이 재개, 충돌한 경로·구성 거부, 기록 무결성을 확인한다. 공통 기능 변경은 전체 테스트로 다른 환경에 영향이 없는지도 확인한다. 문서만 수정하는 경우에는 링크·파일 경로와 코드 설명을 대조하고 실행 코드가 변경되지 않았는지 확인한다.

README는 동작을 이해하기 위한 출발점이다. 정확한 실행 인자와 JSON 처리 방식은 Python 구현에서, AI의 판단·운영 규칙은 `SKILL.md`와 해당 참조 문서에서 확인한다.
