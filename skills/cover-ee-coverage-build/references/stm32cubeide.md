# STM32CubeIDE

## 판정 기준

- 프로젝트 루트에서 `.project`, `.cproject`, `.ioc`를 수집한다.
- `.project`와 `.cproject`가 같은 디렉터리에 있고 다음 중 하나가 확인되면 STM32CubeIDE 프로젝트로 확정한다.
  - `.cproject`에 ST/STM32 managed-build 식별자가 존재한다.
  - 도구 실행 명령에 `arm-none-eabi-*`가 존재한다.
  - 같은 디렉터리에 `.ioc`가 존재한다.
- 일반 Eclipse CDT 파일만 있고 STM32 증거가 없으면 후보로만 기록한다.
- STM32CubeIDE 프로젝트 안의 생성 Makefile은 IDE의 내부 산출물이므로 직접 Make 빌드로 분류하지 않는다.
- 프로젝트나 빌드 구성이 여러 개면 고객 스크립트·CI·기존 로그를 먼저 확인하고, 하나로 좁혀지지 않을 때만 사용자에게 실제 대상을 요청한다.

## 원본 빌드

STM32CubeIDE가 내부적으로 사용하는 빌더를 명령행에서 호출한다. IDE 화면을 열지 않아도 프로젝트의 선택한 빌드 구성을 기준으로 빌드할 수 있도록 같은 설치본을 사용한다.

- GUI를 열거나 버튼을 자동 조작하지 않는다.
- 선택한 STM32CubeIDE 설치본의 headless Eclipse CDT managed builder를 호출한다.
- 기본 호출 형태는 다음과 같다.

```text
stm32cubeidec.exe --launcher.suppressErrors -nosplash
  -application org.eclipse.cdt.managedbuilder.core.headlessbuild
  -data <작업별 Eclipse workspace>
  -import <프로젝트 디렉터리>
  -cleanBuild <프로젝트명>/<Configuration>
  -no-indexer -printErrorMarkers -verbose
```

- 작업별 Eclipse workspace에는 IDE 메타데이터만 둔다. ELF/AXF/OUT 등 빌드 산출물은 프로젝트가 원래 생성하는 경로에 유지한다.
- 상세 로그에서 실제 `arm-none-eabi-gcc`, `arm-none-eabi-g++`, 최종 링크 드라이버 또는 링커, 필요한 경우 아카이버와 산출물 경로를 추출한다.
- 컴파일러·링커 옵션이 `@response-file`로 분리된 경우 화면 없이 실행한 빌드의 작업 경로를 기준으로 RSP를 제한적으로 펼쳐 컴파일·링크 역할과 산출물을 판정한다.
- PATH, `.cproject` 선언, Agent 설정만으로 실제 사용 도구를 확정하지 않는다.

## 커버리지 빌드

원본에서 확인한 도구를 Agent 래퍼에 연결하고, IDE 내부 빌더가 그 래퍼를 선택하도록 설정한다. 명령행에서 검증한 뒤에는 선택한 IDE 빌드 구성에 설정을 남겨 다음 수동 빌드에서도 사용한다.

### Agent에 연결할 도구

- 원본 빌드에서 확인한 도구만 Agent ToolChainInfo에 매핑한다.
- Agent가 없으면 `createAgent`로 생성하고, 원본 도구와 역할이 일치하는 기존 STM32 매핑은 연결된 정의와 설정을 그대로 사용한다. 매핑이 없는 도구만 `updateAgent.toolChains`로 추가한다. 새 컴파일러 매핑에는 호환 STM32 ToolChainInfo 정의를 먼저 재사용하고, 없으면 아래 파서 템플릿으로 `createToolChainInfo`를 호출한다. 생성 ID를 재조회해 확인한 뒤 매핑에 연결한다. `targetKind=general`인 정의를 STM32 정의 대신 선택하지 않는다. 공유 정의 수정과 오류 처리는 [Open API 요청과 Agent 설정](security-and-api.md#open-api-요청과-agent-설정)을 따른다.
- C 컴파일러, C++ 컴파일러, 실제 최종 링크 드라이버 또는 링커, 아카이버만 대상으로 한다.
- 설치된 GNU Arm 툴체인 디렉터리에서 `g++`, `ar`, `ld` 파일이 발견되더라도 상세 로그를 남기는 원본 빌드에 호출이 없으면 매핑하지 않는다. 프로젝트 결정값 하나로 실제 호출을 확인하지 못한 역할을 보완하지 않는다.
- 글루 코드 컴파일러는 Agent 매핑과 별도로 정한다. 원본 빌드에 `arm-none-eabi-g++`가 호출됐으면 그 절대 경로를 사용하고, C 전용 프로젝트이면 관찰된 `arm-none-eabi-gcc`와 같은 디렉터리의 `arm-none-eabi-g++`만 글루 코드 전용 대응 도구로 사용한다. 이 대응 도구는 글루 코드용이며, 원본 호출을 확인하지 못한 C++ 래퍼 역할로 추가하지 않는다.
- 실제 도구 경로가 Agent의 `wrapper` 또는 `toolchaininfo` 아래를 가리키면 래퍼 자기참조로 보고 Agent 설정을 변경하기 전에 중단한다.
- ASM 컴파일러, objcopy, size, nm 등 후처리 도구를 새로 매핑하거나 변경하지 않는다.

### ToolChainInfo 파서 본문

STM32CubeIDE 파이프라인의 GNU Arm 컴파일러에 대해서만 [stm32cubeide.conf](../assets/parser-config/stm32cubeide.conf)를 사용한다. 이 파일은 사용자가 지정한 파서 본문이며 UTF-8로 읽어 줄 단위 배열인 `toolChainContent`에 넣는다. 주석과 빈 줄도 보존한다. 파일 내용은 Make 명령이 아니므로 셸에서 실행하지 않는다.

`gnu_version=130300`을 비롯한 모든 설정은 그대로 사용한다. 실제 컴파일러 버전이 다르다는 이유로 파서 값을 자동 변경하거나 Linux GCC 설정을 덧붙이지 않는다. ToolChainInfo의 `majorVersion`·`minorVersion`은 원본 빌드에서 확인한 컴파일러 버전이고, 파서 본문의 설정값과 별도로 기록한다. 표시 이름은 기존 규칙인 `gcc <주 버전> STM32`, 타깃 분류는 `STM32`를 사용한다.

이 파서 본문은 Agent `gcc.config`의 글루 코드·링크 설정과 다른 데이터다. 해당 Agent 설정 파일을 이 내용으로 덮어쓰지 않는다. 템플릿을 등록했다는 사실만으로 계측 성공을 판단하지 않으며 이후 실제 래퍼 호출과 신규 산출물 검증을 계속 수행한다.

새 매핑에 연결할 기존 정의의 선택 기준은 `majorKind=gcc`, 실제 컴파일러와 같은 `majorVersion`, `targetKind=STM32`이다. 부 버전까지 같아야 한다는 조건은 추가하지 않는다. 생성 후에는 응답 ID로 재조회하여 이름·주/부 버전·컴파일러 계열·세부 계열·타깃이 요청과 같은지 확인한다. 본문은 목록 API에 포함되지 않으므로 별도로 기록한 요청과 템플릿 해시를 근거로 남긴다.

### 명령행으로 래퍼 적용

- 원본과 동일한 STM32CubeIDE 실행 파일·프로젝트·빌드 구성으로 한 번의 `-cleanBuild`를 수행한다.
- 검증 빌드에서는 수동 UI의 C/C++ Build Environment 설정 대신 화면 없이 실행하는 IDE 내부 빌더의 `-Ep`로 래퍼 PATH를 주입한다.

```text
stm32cubeidec.exe ...
  -cleanBuild <프로젝트명>/<Configuration>
  -Ep PATH=<CODESCROLL_TM_INSTALL_PATH>/wrapper
  -no-indexer -printErrorMarkers -verbose
```

- `-Ep`는 IDE 내부 managed 빌드가 참조하는 PATH에 래퍼 경로를 맨 앞에 추가한다. 프로세스 환경 PATH도 같은 순서로 고정한다.
- 작업 또는 재사용 STM32CubeIDE workspace가 선택한 Eclipse project 디렉터리 안에 들어가면 CDT가 workspace의 `.metadata/.../specs.c`를 고객 소스로 탐색할 수 있다. 이 경우 workspace는 project의 형제 `.cover-build` 또는 `.cover-ee` 아래로 이동하고, headless 명령의 `-data`에는 이동한 절대 경로를 사용한다.

### 글루 코드와 Agent 설정

- COVER builder/configuration 설정은 PATH 주입과 별개다. Agent의 `gcc.config`를 작업 사본으로 검증하고, `GlueCodeCompiler`를 원본 빌드에서 관찰한 타깃 C++ 컴파일러 절대 경로(또는 관찰한 C 컴파일러와 같은 디렉터리의 검증된 C++ 대응 도구)로 바꾼다.

  STM32 ARM 링커에 부적합한 Windows 라이브러리 `-lwsock32`, `-lshlwapi`는 복사본의 `LinkAdditionalFlags`에서 제거하되 다른 추가 링크 플래그는 보존한다. 성공한 뒤에만 원본을 Agent `conf/gcc.config.cover-ee-backup`과 프로젝트 `.cover-ee/backup`에 보관하고 검증본을 Agent `conf/gcc.config`에 한 번에 교체해 적용한다.

  검증에 사용한 `cover-glue-compiler-*.config`는 성공·실패 모두 삭제한다. `.cover-ee/applied-state.json`에는 기본 설정 경로, 원본·적용 해시와 백업 경로를 기록하며 다음 원본 빌드에서는 원본을 일시 복원한다.

### 도구 경로가 고정된 프로젝트의 처리

- 선택 빌드 구성의 `.cproject`가 C/C++ 컴파일러, 실제 링커·컴파일러 드라이버 또는 아카이버를 절대 경로로 고정하면 `-Ep`만으로는 래퍼를 선택할 수 없다. CDT의 `-T/-Ta/-Tp/-Tr`는 도구 옵션 값만 변경하므로 tool `command` 대체에 사용하지 않는다.
- 이 경우 프로젝트 루트 안의 `.cproject`를 인코딩과 줄바꿈을 포함해 원본 바이트 그대로 백업하고, 원본 빌드에서 관찰된 역할과 basename이 일치하는 `command` 속성만 래퍼 절대 경로로 변경한다. ASM, objcopy, size 등 다른 도구와 선택하지 않은 빌드 구성은 변경하지 않는다.

### 성공한 설정을 IDE에 유지

- 커버리지 빌드가 성공하면 `.cproject`에서 선택 빌드 구성 ID를 확인하고 `.settings/org.eclipse.cdt.core.prefs`의 해당 빌드 구성에 `PATH=<wrapper>;<검증된 빌드 PATH>`와 `COVER_BR_CONFIG=gcc.config`를 적용한다. prefs 파일이 없고 `.settings` 디렉터리가 유효하면 `eclipse.preferences.version=1`을 포함한 최소 파일을 생성한다. 게시 또는 재사용 검증이 실패하면 새 파일을 삭제해 원래의 파일 없음 상태로 복구한다.

  STM32CubeIDE가 수동 UI 설정과 동일하게 읽도록 PATH는 `operation=replace`로 저장한다. 기존 빌드 구성 PATH가 있으면 그 값을 우선 보존하고 래퍼 경로 중복은 제거하며, 다른 환경변수는 변경하지 않는다.

  Agent 설치 절대 경로인 `CODESCROLL_TM_INSTALL_PATH`는 프로젝트 환경에 추가하지 않는다.
- 절대 도구 실행 명령 때문에 `.cproject` 대체 처리를 사용한 경우에는 실패 시 즉시 원본으로 복구한다. 성공하면 도구 실행 명령과 CDT 프로젝트 환경 변경을 각각 복구할 수 있는 상태로 유지한다. 원본 파일의 바이트는 `.cover-ee/backup`에 보관하고 다음 스킬 실행의 원본 빌드 동안만 일시 복원하며, 동시 변경이 감지되면 덮어쓰지 않는다.
- 재사용 설정을 적용한 뒤 사용자는 같은 빌드 구성을 선택해 IDE에서 Clean/Rebuild만 수행하면 된다. 추가 Environment 편집이나 `.cover-ee` launcher 실행을 요구하지 않는다.
- 설정 유지와 재사용 검증은 새 빌드용 workspace에서 `-Ep` 없이 수행한다. 검증 프로세스의 PATH에서는 래퍼 디렉터리를 제거하고 `COVER_BR_CONFIG`도 비워, `.settings/org.eclipse.cdt.core.prefs`의 선택 빌드 구성 설정만으로 래퍼가 호출되는지 확인한다.

  이 단계도 반드시 `-cleanBuild`이며 실패하면 프로젝트 설정과 Agent 설정을 복구한다.

## 성공 판정

- 화면 없이 실행한 clean 빌드 종료 코드가 정상이어야 한다.
- 실제 상세 로그와 Agent 로그에서 필요한 래퍼 호출을 확인해야 한다.
- 네이티브 컴파일러·링커 직접 우회가 없어야 한다.
- 원본과 같은 절대 경로의 ELF/AXF/OUT 산출물이 이번 작업에서 변경되어야 한다.

## 측정 수집과의 경계

- 타깃 실행·디버깅·측정 데이터 추출은 커버리지 빌드와 별도 단계다.
- STM32 측정 수집을 요청하면 별도 `cover-ee-coverage-collect` 스킬을 사용한다. 빌드 스킬에서는 Debug 세션 제어, 메모리 덤프, CVD 검증·전달을 수행하지 않는다.
- 이 단계의 성공을 커버리지 빌드 성공과 혼합하지 않는다.
