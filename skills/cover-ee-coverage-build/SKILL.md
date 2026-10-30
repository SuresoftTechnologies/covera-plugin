---
name: cover-ee-coverage-build
description: 자연어 요청을 받아 C/C++, C#, Java 프로젝트의 COVER EE 커버리지 빌드를 준비·수행·검증하고, 성공한 설정을
  이후 IDE·일반 빌드에서 재사용할 수 있게 프로젝트에 적용한다. CMake, Visual Studio/MSBuild, 직접 Make, STM32CubeIDE,
  C# COVER Toolbox, Java JACOV CLI/Maven/Gradle 환경에서 원본 빌드와 실제 도구를 식별하고 동일 경로의 계측
  산출물을 확인할 때 사용한다.
user-invocable: true
disable-model-invocation: false
---

## 설치 경로와 호스트 연결

PLUGIN_ROOT는 이 진입점이 속한 설치 플러그인 루트다. skills/<id>/SKILL.md에서는 ../..이다. 공용 scripts/assets/tmpa는 플러그인 루트에 한 번만 배치한다. 스킬의 references와 보조 문서는 해당 스킬 파일 기준이다.

`${PLUGIN_ROOT}`와 `${PLUGIN_DATA}`가 셸에 자동 설정됐다고 가정하지 않는다. 로드한 파일의 실제 위치로 루트를 확인하고, 실행할 때 실제 절대경로로 대체하거나 동일 셸 호출에서 변수를 설정한다. PLUGIN_DATA는 호스트가 제공하는 영속 데이터 경로를 우선 사용하고, 없으면 사용자 데이터 디렉터리 아래 이 플러그인 전용 경로를 사용한다. 사용자의 분석 대상 작업 디렉터리는 바꾸지 않는다. bash 예제는 bash에서 실행한다.

SKILL_ROOT는 지금 읽은 SKILL.md의 부모 디렉터리다. 이 스킬 전용 scripts/assets는 SKILL_ROOT 아래에 있다. `${SKILL_ROOT}`는 설명용 표기이며 자동 설정된 환경변수가 아니다. 스크립트 실행 시 로드한 SKILL.md의 실제 위치로 절대경로를 구성한다. 프로젝트 작업 디렉터리를 스킬 폴더로 바꾸지 않는다.

# COVER EE 커버리지 빌드

## 스킬 파일 경로

이 스킬의 실행 코드와 파서 설정은 같은 디렉터리의 `scripts/`, `assets/`에 있다.
`${SKILL_ROOT}`는 이 `SKILL.md`가 있는 실제 디렉터리다. 자동 설정되는 환경변수가 아니므로 실행 예제의 경로는 이 파일 위치를 기준으로 절대경로로 바꾼다. 분석 대상 프로젝트의 작업 디렉터리는 바꾸지 않는다.
주 진입점은 [scripts/cover_workflow_runner.py](scripts/cover_workflow_runner.py), 인증 진입점은 [scripts/cover_auth.py](scripts/cover_auth.py)다.

## 작업 범위와 완료 조건

C/C++, C#, Java의 원본 빌드 방법을 확인하고 COVER 계측 빌드를 수행한다. 실제 계측 처리와 원본과 동일한 절대 경로의 신규 산출물을 확인한 뒤, 검증된 설정을 스킬 없이 다시 사용할 수 있도록 남긴다.

커버리지 빌드 성공과 스킬 작업 완료는 구분한다. 빌드 오류 없이 실제 계측 처리와 동일 경로의 신규 산출물까지 확인하면 커버리지 빌드는 성공한 것이다. 스킬 작업은 여기서 끝내지 않고, 재사용 설정을 적용하고 이후 IDE 또는 수동 명령으로 빌드하는 방법까지 안내해야 완료한다. 자세한 기준은 [워크플로의 범위와 완료 기준](references/workflow.md#범위와-완료-기준)을 따른다.

환경별로 이후 빌드에 사용할 설정은 다음과 같다.

- Visual Studio와 C#은 IDE의 다음 Rebuild에 사용할 연결을 유지한다.
- STM32CubeIDE는 검증한 설정을 Agent `gcc.config`에 적용한다. 선택한 빌드 구성의 CDT 프로젝트 환경에는 래퍼 디렉터리를 맨 앞에 둔 `PATH`와 `COVER_BR_CONFIG=gcc.config`를 유지한다. 사용자는 별도 Environment 설정 없이 IDE에서 같은 구성을 Clean/Rebuild한다.
- CMake·Make·Maven·Gradle은 명령행 주입이 성공하면 프로젝트 빌드 파일을 수정하지 않고 검증된 재실행 명령을 제공한다.

프로그램 측정 데이터 확인과 서버 Module 조회는 사용자가 별도로 요청했을 때만 수행한다. 이 두 항목은 커버리지 빌드의 성공 조건에 포함하지 않는다.

STM32CubeIDE의 측정 데이터 수집은 별도 `cover-ee-coverage-collect` 스킬에서 담당한다. 수집만 요청받은 경우 원본 빌드나 계측 빌드를 다시 시작하지 않는다.

## 시작 조건과 보안

- `CODESCROLL_TM_SERVER_URL`은 Local Agent가 `tm-remote-service`에 연결할 때 쓰는 주소다. COVER 메인 웹 서버/Open API 주소로 사용하거나, scheme·port·path를 바꿔 메인 서버 주소를 추측하지 않는다. 사용자가 제공한 메인 웹 서버 URL이나 유효한 접속 정보 캐시가 없으면 환경변수 값이 있어도 사용자에게 주소를 요청하고 입력을 기다린다. 입력 전에는 서버 연결을 시도하지 않는다.
- AI Agent는 먼저 `${CODESCROLL_TM_INSTALL_PATH}/agent_kit/open-api-credentials.json`에 유효한 Open API 메인 웹 서버 URL·Access Key가 캐시돼 있는지 확인한다. 있으면 그 값을 사용하고 다시 묻지 않는다. 없으면 최초 사용자 요청에 프로젝트 경로, COVER Access Key 또는 COVER Open API가 제공되는 메인 웹 서버 URL이 없을 때 누락된 값을 한 번에 채팅으로 요청하고 입력을 기다린다. URL을 요청할 때에는 “COVER Open API가 제공되는 메인 웹 서버 URL을 알려주세요. (예: https://cover.example.com:4009)”라고 안내한다. 입력을 받은 뒤 프로젝트 조사와 원본 빌드를 시작하되, 키는 첫 서버 작업 전까지 인증에 사용하지 않는다.
- “평소 사용하는 원본 빌드 명령이나 스크립트가 있으면 알려주세요. 없다면 프로젝트 파일을 분석해 자동으로 찾겠습니다.”라고 선택적으로 안내한다. 받은 문자열을 그대로 실행하지 말고 구성 항목을 나눈 계획으로 검증한다.
- Local Agent의 remote-service URL은 환경 변수 `CODESCROLL_TM_SERVER_URL`, Local Agent 경로는 `CODESCROLL_TM_INSTALL_PATH`에서 읽고, 값이 없거나 유효하지 않을 때만 요청한다. 실행기의 `--server-url`은 remote-service 주소이며, 사용자가 입력한 메인 웹 서버 주소는 `--open-api-server-url`로 전달한다. Open API는 이 주소를 정규화한 HTTPS 원점으로 호출하며, remote-service와 같은 host·port일 필요는 없다. 알려진 host나 포트를 순서대로 탐색하지 않는다. Local Agent에는 환경변수에 설정된 scheme·host·port·path를 유지하며, [WSL HTTP loopback 예외](references/security-and-api.md#wsl에서-windows-서버에-연결하는-경우) 외에는 바꾸지 않는다. 환경변수 자체도 수정하지 않는다.
- Java JACOV 서버 주소는 `cover_jacov.json.url`에서 읽는다. JACOV 주소에는 위 HTTPS 변환을 적용하지 않으며 Open API 서버와 동일한 protocol·host·port도 요구하지 않는다.
- 프로젝트 조사와 원본 빌드는 인증 없이 수행한다. 최초 대화에서 받은(또는 캐시 파일에서 읽은) COVER Access Key는 현재 대화·인증 프로세스의 메모리에만 보관하고, 원본 빌드 뒤 첫 서버 작업 직전에만 `${SKILL_ROOT}/scripts/cover_auth.py -- <실행기 인자>`의 메모리 전용 인증 세션으로 전달한다. 새 호출에는 이전 인터페이스의 `terminal`과 `--force-login`을 사용하지 않는다. 이 첫 인증이 성공하면 Open API URL과 Access Key를 `${CODESCROLL_TM_INSTALL_PATH}/agent_kit/open-api-credentials.json`에 캐시해 다음 실행부터 재사용한다.
- 사용자의 커버리지 빌드 요청은 Open API에는 사용자가 알려준(또는 캐시 파일의) 메인 웹 서버 URL, Java 계측에는 제품이 생성했거나 사용자가 선택한 `cover_jacov.json.url`을 사용하여 해당 작업의 빌드 경로·파일명·도구 정보·계측 분석 정보를 전송하는 것까지 허용한다. 원본 빌드 뒤 서버 연결 계측 단계에서 별도 전송 승인을 다시 묻지 말고 계속 진행한다. 입력으로 정하지 않은 다른 서버로 작업 범위를 확대하지 않는다.
- Access Key와 Java key를 명령 인수·작업 파일·로그·응답에 남기지 않는다. 예외적으로 Java 설정이 없을 때는 [Java 설정 생성 절차](references/java.md)에 따라 실행 계정 홈의 `cover_jacov.json`에 Java key와 URL만 저장하고, 첫 Open API 인증이 성공했을 때는 위와 같이 `agent_kit/open-api-credentials.json`에 Access Key와 Open API URL만 저장한다. 두 파일 모두 한 번만 생성하며 이후 실행에서 자동으로 덮어쓰지 않는다. 입력을 위한 새 콘솔을 열지 않는다.
- Open API는 `curl`로 호출한다. 기본 실행에서는 `-k`를 사용하여 인증서 체인과 hostname을 검사하지 않는다. 이 정책은 사용자가 제공한(또는 캐시 파일의) 메인 웹 서버 URL로 생성한 무결성 보호된 작업의 Open API 원점에만 적용한다. 엄격한 검증을 요청받으면 새 작업에 `--verify-tls`를 사용한다.

  Local Agent는 별도의 Java 프로세스이므로 `curl -k`의 영향을 받지 않는다. Agent 원점이 HTTP이면 인증서 조회와 truststore 생성을 하지 않는다. HTTPS이면 작업에서 처음 제시된 미검증 인증서를 임시 truststore에 넣어 신뢰할 수 있지만, 인증서의 hostname 검사는 Agent가 그대로 수행한다. 인증서가 서버 주소를 포함하지 않으면 즉시 중단하고 인증서 또는 서버 주소를 바로잡도록 안내한다. 스킬에 별도 Java 보조 프로그램을 넣거나 시스템 truststore·hosts·Agent 설치 설정을 바꾸지 않는다.
- 인증과 API 사용 범위와 상세 규칙은 [references/security-and-api.md](references/security-and-api.md)를 읽고 따른다. 스킬 코드를 수정하거나 환경 지원을 확장할 때만 [references/architecture.md](references/architecture.md)를 추가로 읽는다.

## 반드시 지켜야 할 규칙

아래 규칙은 빌드 환경이 달라도 유지한다. 도구 경로, 출력 경로, 적용 상태를 구분해서 기록해야 이후 빌드를 재현하고 실패한 변경을 복구할 수 있다.

### 원본 빌드와 실제 도구 확인

- 상세 로그(verbose/diagnostic)를 남기는 원본 빌드를 실제 수행해 컴파일러, 링커, 아카이버와 최종 산출물을 확인한다. PATH·프로젝트 속성·기존 Agent 설정만으로 실제 도구를 확정하지 않는다.
- COVER 커버리지 빌드 산출물 절대 경로는 원본 산출물 절대 경로와 같아야 한다. 작업 기록 디렉터리는 로그·계획·JSON에만 사용하고 다른 복제 저장소, 경로 별칭, 임시 커버리지 출력 경로를 만들지 않는다.

### C/C++ 래퍼와 글루 코드 컴파일러

- Linux/Unix 네이티브 래퍼는 `gcc.config`를 읽지 않는다. Linux CMake/Make에서는 해당 파일을 요구·생성·복사·해시 검증하지 않으며 `COVER_BR_CONFIG`를 주입하거나 재사용 명령에 저장하지 않는다. 글루 코드 컴파일러는 `COVER_CC`로 전달한다. Windows GNU·STM32CubeIDE의 설정 파일 절차를 Linux에 적용하지 않는다. 실행기가 누락 파일 오류를 내더라도 임의 설정 파일 생성으로 우회하지 않는다.

- C/C++ 래퍼 설정은 C 컴파일러, C++ 컴파일러, 최종 링크 드라이버 또는 링커, 아카이버에만 적용한다. ASM 및 objcopy 등 SDK 후처리 도구를 새로 설정하지 않는다.
- `realTools`는 원본 상세 로그에 나타난 도구 경로를 보존한다. CMake·Make에서 `gcc`처럼 이름만 호출했다면 해당 원본 빌드의 PATH로 확인한 절대 경로를 기록한다. 심볼릭 링크 별칭은 유지하고, canonical 경로는 동일 파일 비교와 GCC 계열·타깃 prefix 내부 판정에만 사용한다. Visual Studio·CMake·Make·STM32CubeIDE의 래퍼 이름은 아래 기존 매핑 재사용 절차를 따른다.
- 상세 로그(verbose/diagnostic)를 남기는 원본 빌드에서 실제 호출이 확인된 역할만 Agent에 매핑하고 커버리지 계획에 전달한다. 같은 설치 디렉터리에 있다는 이유로 관찰되지 않은 C++ 컴파일러, 링커, 아카이버를 자동 보완하지 말며, Agent의 `wrapper` 또는 `toolchaininfo` 경로를 원본 도구로 등록하지 않는다.
- COVER 래퍼가 생성한 C++ 글루 코드는 실제 타깃 C++ 컴파일러 드라이버로 컴파일한다. 원본 빌드에서 관찰된 C++ 컴파일러를 우선하고, C 전용 MSVC는 관찰된 `cl.exe`, GCC·Clang 계열은 같은 디렉터리·툴체인 이름의 C++ 대응 도구를 사용한다.

  그 밖의 C 전용 툴체인은 중단한 뒤, 관찰된 C 컴파일러와 같은 디렉터리에서 검증한 C++ 컴파일러 드라이버를 `glueCodeCompiler` 결정으로 받아 재개한다.
- 글루 코드 컴파일러는 Agent 래퍼 매핑에 추가하지 않는다. Linux에서는 커버리지 프로세스의 `COVER_CC`로 전달한다. Windows GNU에서는 래퍼 형식과 호환되는 설정의 작업별 복사본 `GlueCodeCompiler`로 전달한다. STM32CubeIDE에서는 Agent의 `gcc.config`를 작업별로 복사하고, 위 공통 선택 절차로 확인한 타깃 C++ 컴파일러를 `GlueCodeCompiler`로 설정하며, 복사본의 `LinkAdditionalFlags`에서 Windows 전용 `-lwsock32`, `-lshlwapi`만 제거한다.

  검증 빌드는 Agent `conf`에 둔 작업별 basename 복사본을 `COVER_BR_CONFIG`로 사용한다. 성공한 뒤에만 원본 `gcc.config.cover-ee-backup`을 만들고 검증본을 `gcc.config`에 한 번에 교체해 적용하며 작업별 복사본은 삭제한다.

  실패하면 작업별 복사본을 삭제하고 기본 `gcc.config`는 변경하지 않는다.
- 공용 Agent 설정과 래퍼 자체를 글루 코드 컴파일러로 사용하지 않는다. Visual Studio `cl.exe` 래퍼처럼 설정 형식과 동작 규칙이 다른 래퍼에 GNU `base.config`를 강제로 적용하지 않는다.

### Agent 매핑과 호출 확인

CMake·Make는 원본 도구의 경로와 역할이 일치하는 기존 등록을 재사용한다. 없으면 새 등록을 만들며, 이때 래퍼 이름은 `cs + 원본 파일명`으로 정한다. 선택한 래퍼는 명령행으로 주입한다. 경로 확인, 이름 충돌과 복수 후보 처리는 [래퍼 매핑](references/gcc-cmake-make.md#래퍼-매핑)을 따른다.

Visual Studio·STM32CubeIDE는 원본 도구와 같은 이름의 래퍼를 사용한다. 해당 이름의 매핑이 없으면 생성하고 다른 이름의 등록은 보존한다. Visual Studio는 `ExecutablePath`, STM32CubeIDE는 PATH로 주입한다. STM32의 절대 경로 도구 명령은 백업 후 같은 이름의 래퍼 경로로 바꾼다. 별칭 지원을 위한 MSBuild 속성이나 CDT 명령 변경은 추가하지 않는다.

모든 네이티브 빌드는 기존 매핑을 보존하고, 새 매핑에 사용할 ToolChainInfo의 호환성, 실제 래퍼 호출과 성공 후 재사용 설정을 검증한다. C# Toolbox와 Java JACOV에는 이 래퍼 명명 규칙을 적용하지 않는다.

- 일반 네이티브 빌드의 ToolChainInfo는 `getToolChainInfos` 전체 응답과 실제 컴파일러 경로·`--version`·`-dumpmachine` 증거를 모델이 비교해 선택한다. 이 선택은 매핑이 없는 컴파일러에만 수행한다. 실제 원본 도구와 역할이 일치하는 기존 매핑은 ToolChainInfo, 원본 경로 표기, 래퍼 이름, 측정 기준과 인코딩을 변경하지 않고 사용한다. 더 적합해 보이는 정의가 있어도 재선택하지 않는다. `WAITING_MODEL`의 `TOOLCHAIN_INFO` 결정을 같은 작업에서 재개하며, OS/ABI·계열·버전·아키텍처가 맞는 정의가 없거나 정보가 부족하면 사용자에게 넘긴다. Linux와 MinGW를 GCC 버전·비트 수만 같다는 이유로 대체하지 않는다. 결정 형식은 [ToolChainInfo 선택](references/decision-contract-v2.md#toolchaininfo-선택)을 따른다. STM32CubeIDE의 지정 템플릿 기반 정의 선택·생성 절차는 유지한다.

- 일반 Agent 시작은 Windows의 `bin/run_agent_controller.cmd`, Linux의 `bin/cvctl.sh start`를 사용한다. 이미 실행 중인 Agent의 재사용과 준비 상태 검증은 유지한다. 구체적인 실행 방식과 하드웨어 ID 생성용 임시 실행의 차이는 [Agent 시작 절차](references/security-and-api.md#일반-agent-시작)를 따른다.

- Agent가 없으면 Open API `createAgent`로 생성하고, 원본 빌드에서 관찰한 컴파일러·링커·아카이버 매핑은 기존 항목을 그대로 유지하고 없는 항목만 `updateAgent.toolChains`로 추가한 뒤 `getAgent`로 재검증한다. 새 컴파일러 매핑에는 `GET /v1/toolchain-infos`에서 선택한 호환 `toolChainInfoId`를 넣는다. 링커·아카이버는 컴파일러 정의를 참조하지 않으므로 이 필드를 보내지 않으며, 조회 응답의 `toolChainInfo=null`을 정상으로 처리한다.

  여기서 Agent의 `toolChains` 매핑과 서버의 ToolChainInfo 정의를 구분한다. Open API는 정의 자체의 생성·수정도 지원한다. 기존 호환 정의를 우선 재사용한다. STM32CubeIDE에서 호환 정의가 없으면 [지정된 파서 템플릿](references/stm32cubeide.md#toolchaininfo-파서-본문)으로 새 정의를 생성한다. 그 밖의 환경은 `USER` 소유 `WAITING_USER`로 중단하여 정의를 준비하도록 안내한다. 공유 정의는 자동 수정하지 않으며 상세 제한은 [Open API 요청과 Agent 설정](references/security-and-api.md#open-api-요청과-agent-설정)을 따른다.
- 원본 명령의 컴파일러·링커 옵션이 `@response-file`에 들어 있으면 제한된 로컬 파일 내용을 읽어 역할과 산출물을 판정하고, 읽은 파일의 경로·크기·SHA-256을 원본 도구 증거에 남긴다. RSP 내용을 셸 명령으로 실행하지 않는다.
- Windows 네이티브 로그의 bare 실행 이름은 `.exe` 생략을 허용해 래퍼 basename과 비교하되, 절대·상대 경로가 포함된 실행 토큰은 basename만으로 통과시키지 말고 선택된 래퍼의 실제 경로와 일치하는지 계속 검증한다.

### 기존 설정 해제, 재사용 설정 적용과 복구

- 원본 빌드 전에는 기존에 적용한 COVER 연결을 일시 해제하고, 원본 빌드가 COVER 래퍼를 통과하지 않았음을 확인한다. 갱신 작업이 실패·대기·중단되면 이전 적용 상태를 다시 복구한다.
- 최초 계측 시도는 기존처럼 작업 범위가 좁은 명령행·Toolbox·JACOV 주입으로 검증한다. 성공한 뒤에는 프로젝트 루트의 `.cover-ee`에 설정·적용 상태·원본 백업·수동 실행 진입점을 만든다. Visual Studio는 선택한 각 `.vcxproj`의 검증된 `Configuration|Platform`에 `.cover-ee/config/cover-msbuild.props`를 `ImportGroup Label="PropertySheets"`로 연결하여, 속성 관리자와 `VC++ 디렉터리 > 실행 파일 디렉터리`에서 `$(CoverWrapperDirectory)`가 적용된 것을 확인할 수 있게 한다. 같은 구성의 기존 `PropertySheets` 그룹을 우선 사용하고 그룹을 중복 생성하지 않는다. 기존 COVER import가 있으면 경로·위치·구성을 재검증하여 stale import를 현재 props로 교체한다. 적용 뒤에는 `ForceImportBeforeCppTargets`와 `CoverWrapperDirectory` 명령행 인자 없이 같은 Rebuild를 다시 수행하고, 래퍼 호출과 동일 경로 신규 산출물이 확인돼야 `APPLIED`로 기록한다.

  사용자 전역 `Microsoft.Cpp.*.user.props`는 수정하지 않는다. STM32CubeIDE는 `.cproject`의 선택 빌드 구성 ID를 기준으로 `.settings/org.eclipse.cdt.core.prefs`에 IDE 재사용에 필요한 `PATH`와 `COVER_BR_CONFIG`만 최소 반영한다. prefs 파일이 없으면 최소 파일을 생성하고, 실패하면 원래 존재 여부까지 복구한다.

  성공 시에는 적용 상태를 유지하고, 실패 시에는 이번 작업이 변경한 연결만 복구한다.
- STM32CubeIDE 재사용 설정을 적용한 뒤에는 명령행 `-Ep`와 프로세스 `PATH`/`COVER_BR_CONFIG` 주입을 제거한 새 workspace에서 같은 빌드 구성을 다시 `-cleanBuild`한다. 이 빌드에서도 래퍼 호출과 동일 경로의 신규 산출물이 검증돼야 `APPLIED`로 기록한다.
- dry-run에서는 `.cover-ee` 생성·갱신, 프로젝트 파일 변경, Toolbox enable, Maven `install-file`, Gradle JAR·init script 복사를 수행하지 않는다. 실제 계측 빌드와 검증이 끝난 실행만 재사용할 수 있도록 설정을 남긴다.
- CMake/Make는 `명령행 주입 → 실패 증거 확인 → 프로젝트 파일 patch` 순서를 유지한다. 명령행으로 성공하면 파일을 수정하지 않고 실제 성공 명령을 남긴다. 수정으로만 성공하면 각 원본과 같은 폴더에 `<원본파일명>.cover-ee-backup`을 만들고 수정본을 유지한다.
- `.cover-ee/applied-state.json`이 `APPLIED`가 되려면 작업별 `.cover-build/jobs/<jobId>` 경로를 참조하지 않아야 한다. Visual Studio import, CMake/Make 재실행 명령 또는 대체 처리에 필요한 파일 수정본, STM32CubeIDE 선택 빌드 구성, C# Toolbox, Maven JACOV goal 명령, Gradle 프로젝트 루트의 JACOV JAR·init script와 `--init-script` 명령처럼 해당 빌드 시스템에 맞는 재사용 경로를 확인한다.

### 언어별 준비와 성공 확인

- C/C++·STM32CubeIDE·C#은 현재 PC의 Agent를 재사용하거나 등록하고 실행 준비를 별도 검증한다. Java는 Local Agent 설치본과 유효한 `cover_jacov.json`을 요구하지만, 커버리지 빌드만으로 Local Agent를 시작하지 않는다.

  Java JACOV 계측과 실행 런타임을 분리한다. 커버리지 빌드에서는 런타임을 선택하지 않고 `addDependencyJavaLib=false`, `javaLibJarDir=`로 계측한다. 실제 측정 실행을 요청받았을 때만 실행 환경을 판별하여 Local Agent 방식이면 lower를 실행 classpath/dependency에 추가하고, 직접 Java Agent 방식이면 `-javaagent:...cover-javalib-agent.jar`를 실행 JVM에 추가한다.

  이때 Agent online·Java listener 또는 Java Agent 배포·연결 준비를 각각 검증한다.
- 설정 파일이나 사전 확인만으로 성공을 선언하지 않는다. 실제 wrapper/CscHooker/JACOV 처리와 동일 경로의 신규 산출물을 확인한다.
- 공통 실행기는 상태·체크포인트·인증·증거·복구·보고를 담당한다. 모든 환경에 동일한 고정 단계를 강제하지 말고 네이티브 래퍼, C# Toolbox, Java JACOV 각각에 필요한 실행 단계와 순서를 따른다.
- `original-build-evidence.json`의 실제 관찰은 새 원본 빌드를 추가하는 별도 단계가 아니다. 이미 수행한 원본 빌드 한 번에서 네이티브 도구의 역할, C# 컴파일러 실행 방식 또는 Java의 JDK와 컴파일러 실행 방식을 구분하여 기록한다.

## 공통 실행 흐름

### C/C++ 분석 오류가 발생한 경우

Visual Studio, CMake, Make, STM32CubeIDE 커버리지 빌드 중 AI Agent가 이번 실행의 콘솔·빌드 로그에서 `excluded from coverage build(analysis error)` 또는 `excluded from coverage build(analysis error - multi)`를 발견하면, 같은 플러그인의 [cover-toolchain-recovery](../cover-toolchain-recovery/SKILL.md)를 읽고 복구 절차를 수행한다. 실행기가 실패를 반환했다는 이유만으로 최종 실패 보고를 하고 종료하지 않는다. 원본 컴파일로 대체되어 종료 코드가 0인 경우에도 이 절차를 따른다. `excluded from coverage build(option)`은 정상 제외이며 복구 대상으로 삼지 않는다. 일반 컴파일·링크·Agent 연결 오류나 C#/Java 오류에 이 절차를 적용하지 않는다.

분석 오류 문구는 복구 자료를 확인하는 시작 조건이다. 통신 예외에도 같은 문구가 나올 수 있으므로, 문구만 보고 파서 설정을 변경하지 않는다. 소스 경로와 해당 컴파일의 작업 디렉터리를 확인한 뒤 복구 스킬의 `start`로 자료를 확보하고 원인을 판단한다. 상대 소스 경로의 기준을 확인할 수 없으면 임의 경로로 조회하지 않는다.

- 현재 빌드 작업의 `job.json`, 결정 파일, 이번 오류 로그와 소스 경로를 보존한다. 복구에는 이미 확인한 메인 Open API URL, HardwareID, 실제 Wrapper 이름을 사용하고 확인된 값을 다시 묻지 않는다. 인증 정보는 복구 스킬이 정한 프로세스 환경변수로 전달하며 명령 인수·작업 기록에 남기지 않는다.
- 복구용 새 작업 폴더를 현재 빌드 작업과 연결해 기록하고, 자료 확보·복사본 PA 검증·서버 반영·Wrapper 연결까지 복구 스킬의 절차를 따른다. 이 경우 검증된 새 툴체인 생성은 복구 스킬이 담당하며, 일반 빌드의 초기 ToolChainInfo 선택 제한을 이유로 복구를 생략하지 않는다. 별도 복구·재빌드 승인을 다시 요청하지 않는다.
- `ReadyForRebuild`와 `handoff.json`을 확인한 뒤 같은 `job.json`으로 빌드 작업을 재개한다. 서버의 현재 Wrapper 연결과 복구된 ToolChainInfo를 확인하고, 기존 결정값이 이전 정의를 가리키면 지원되는 결정 작성 절차로 갱신한다. 이전 선택값으로 복구 결과를 덮어쓰지 않는다. 작업 상태·증거 JSON은 직접 고치지 않는다.
- Agent의 변경 설정 동기화를 확인한 뒤 커버리지 빌드를 실제로 다시 수행한다. 이전 성공 체크포인트만 재사용한 결과를 재빌드 성공으로 인정하지 않는다. 실행기가 새 설정을 반영하거나 재실행하지 못하면 그 제약을 보고하고 중단한다.
- 새 로그에서 해당 분석 오류 해소를 확인하고, 기존의 계측 처리·동일 경로 신규 산출물·재사용 설정 검증을 계속한다. 복구 성공만으로 빌드 성공을 선언하지 않는다.
- 복구 실패, 자료 부족, 통신 문제 또는 동봉 분석기 누락이면 복구 상태와 필요한 조치를 보고한다. 복구 스킬의 시도 한도를 유지하며 동일 오류가 재발해도 한도 우회용 새 case를 만들거나 변경 없이 반복 빌드하지 않는다.

### 기본 순서

1. `${SKILL_ROOT}/scripts/cover_workflow_runner.py --project-root <경로> --open-api-server-url <메인 웹 서버 URL> --execute`로 프로젝트 인덱스, 환경조사, 원본 계획과 상세 로그를 남기는 원본 빌드를 시작한다. `${CODESCROLL_TM_INSTALL_PATH}/agent_kit/open-api-credentials.json`에서 읽은 값을 쓸 때는 `--open-api-server-url`을 생략한다.
2. 언어·빌드 시스템·엔트리·구성·타깃 후보는 스크립트가 수집한 파일·설정·로그로 단일 값만 자동 확정한다. 혼합 저장소는 파일 존재만으로 한 언어를 우선하지 않는다. 선택한 빌드 진입점과 타깃 의존 관계를 기준으로 `NATIVE`, `CSHARP`, `JAVA` 중 이번 작업의 처리 대상과 빌드 시스템 하나를 정한다.

   여러 실행 계획이 남으면 `WAITING_MODEL`에 제시된 증거의 내용과 관계를 해석하고, 프로젝트 근거로도 정할 수 없을 때만 사용자에게 후보 차이를 제시한다.

   한 작업에서는 선택한 환경의 파이프라인만 실행한다. 같은 환경에 속한 여러 프로젝트·모듈을 함께 빌드하는 경우와 서로 다른 환경을 순회하는 경우는 구분한다. 자세한 범위는 [혼합 저장소에서 한 작업이 처리하는 범위](references/workflow.md#혼합-저장소에서-한-작업이-처리하는-범위)를 따른다.
3. 네이티브, C#, Java 모두 `cover-original-build-plan/v1` 데이터 형식과 무결성 검증 규칙을 사용한다. 사용자 제공 계획은 가장 먼저 검증해 실행하고, 고객의 goal·task·프로필·속성, 작업 디렉터리와 환경 초기화를 임의의 표준 명령으로 바꾸지 않는다.

   결정 파일과 원본 계획을 [references/decision-contract-v2.md](references/decision-contract-v2.md)에 따라 작성하고, 같은 `job.json`으로 작업을 재개한다.

   현재 확인 가능한 원본 빌드 전 선택은 한 결정 묶음으로 답한다. Java 원본 빌드 후 자동 확정하지 못한 JDK·산출물·JACOV 항목도 후속 묶음으로 답한다. 실행기가 확정한 값에 다시 모델 결정을 만들거나, 선택을 추가할 때마다 새 작업을 시작하지 않는다. 과거 작업을 사용하지 않는 요청은 `--no-environment-profile`을 지정한다.
   - Windows에서 Visual Studio/MSBuild 경로가 확정된 뒤 `VsDevCmd`, `TargetPath` 평가 또는 실제 MSBuild에 도달하는 첫 실행기 호출은 도구 호출 자체를 호스트 권한(`require_escalated`)으로 시작한다.

     샌드박스 MSBuild를 먼저 시험하지 않는다. COVER Access Key를 이미 받았다면 PTY를 유지한 같은 실행에 `--prompt-credentials`도 포함해 원본 빌드 뒤 인증과 커버리지 빌드까지 한 프로세스에서 이어 간다. 호스트 권한은 실행기 인자로 대체하거나 우회할 수 없다.

     승인이 거부되면 재시도하지 말고 사용자 대기로 끝내며, 호스트 실행에서도 접근 거부가 나면 일반 ACL·환경 오류로 진단한다.
4. 원본 빌드 방법이 확정되면 `cover-environment-survey.json`을 더 이상 Agent·래퍼·산출물 결과로 확장하지 않는다. 실제 원본 도구와 산출물은 `original-build-evidence.json`에 기록한다.
5. 최초 대화에서 받은 Access Key는 원본 빌드 후 첫 서버 작업 직전에 인증 프로세스의 표준입력으로 전달해 사용한다. REST 통신은 `<서버 원점>/cover-open-api` 아래 Open API v1만 사용한다. 실행기를 직접 호출하거나 같은 작업을 재개하면서 키 전달이 누락되면, 실행기는 서버 작업 직전에 `AUTHENTICATION_INPUT_REQUIRED`를 기록하고 `WAITING_USER`로 중단한다. 채팅에서 이미 받은 키가 현재 대화에 있으면 다시 요청하지 않고 안전하게 전달한다. 받은 키가 없을 때만 사용자에게 입력을 요청한다.
6. 각 환경에 필요한 준비를 마친 뒤 계측 수행 여부를 확인한다. 네이티브는 실제 래퍼 역할 호출, C#은 선택 프로젝트 전체의 Toolbox 구조와 CscHooker 호출, Java는 JACOV 소스 처리의 집계 결과 또는 JACOV 실행 문맥에 속한 파일별 success/failure/ignore 결과를 기록한다.

   Java는 JACOV 설정·플러그인·실행 환경과 서버 TCP 연결을 커버리지 `clean` 전에 확인한다. 현재 도구의 네트워크 정책상 연결이 제한되는 경우에는 처음부터 승인된 실행 권한을 사용한다. 승인 거부를 우회하거나 모든 환경에 호스트 실행을 강제하지 않는다. TCP 연결 성공을 제품 인증 성공으로 보고하지 않는다.

   단순 제품명 문자열이나 출처가 불명확한 success 한 줄로 계측 성공을 선언하지 않는다.
7. 원본 산출물 목록과 커버리지 빌드에서 독립 관찰하거나 현재 빌드 구간에 새로 생성된 산출물 목록을 비교한다. 같은 목록을 자기 자신과 비교하지 않는다.
8. 계측 빌드가 성공하면 검증에 사용한 설정 파일과 구조화된 명령을 재사용 위치인 `.cover-ee`로 옮기고 빌드 시스템에 맞게 연결한다. 적용 상태는 `.cover-ee/applied-state.json`, 보조 수동 진입점은 `.cover-ee/build-coverage.cmd`와 `.cover-ee/build-coverage.sh`에 둔다.

   보조 스크립트만 존재하면 `APPLIED`로 판정하지 않는다.
9. `workflow-state.json.result.buildSummary`와 최종 채팅에 빌드 시스템, 대상·구성·타깃, 실제 커버리지 명령, 작업 경로와 산출물, 설정을 유지하고 재사용하는 방법과 이후 수동 빌드 방법을 같은 형식으로 보고한다. Java 성공 보고에는 런타임을 빌드에 주입하지 않았음을 밝히고, Local Agent 연동(LOWER)과 생성된 `cover-javalib-agent.jar`를 쓰는 JavaLib Agent 방식의 실행 방법을 둘 다 표시한다.

공통 실행에서는 [references/workflow.md](references/workflow.md)만 먼저 읽는다. 다음 참조 문서는 해당 조건에서만 읽는다.

- 후보가 여러 개이거나 명령 추론이 필요함: [references/discovery-and-planning.md](references/discovery-and-planning.md)
- 사용자 명령 구조화 또는 원본 계획 검증이 필요함: [references/original-build-commands.md](references/original-build-commands.md)
- 환경조사 JSON을 생성·진단함: [references/environment-survey.md](references/environment-survey.md)
- 중단 작업 재개, 반복 실행 또는 성능 진단이 필요함: [references/performance.md](references/performance.md)
- 최종 요약 또는 보고 형식과 작성 규칙을 진단함: [references/build-reporting.md](references/build-reporting.md)

## 환경별 분기

- Visual Studio/MSBuild: [references/visual-studio.md](references/visual-studio.md)
- CMake 또는 직접 Make: [references/gcc-cmake-make.md](references/gcc-cmake-make.md)
- STM32CubeIDE: [references/stm32cubeide.md](references/stm32cubeide.md)
- C# COVER Toolbox: [references/csharp.md](references/csharp.md)
- Java JACOV Maven/Gradle: [references/java.md](references/java.md)

선택한 환경의 참조 문서만 추가로 읽는다. Visual Studio의 현재 빌드 구성(Active Configuration)을 DTE/EnvDTE로 조회하거나 IDE를 실행하지 않는다. CMake generator가 Ninja·Makefiles·Visual Studio여도 CMake 진입점이면 `CMAKE`로 처리하며, CMake 소유 관계가 없는 직접 Ninja는 지원하지 않는다.

## 스크립트 역할

모델은 빌드 작업에 `cover_workflow_runner.py`와 `cover_auth.py`를 진입점으로 사용한다. 위 C/C++ 분석 오류 복구로 전환한 동안에는 복구 스킬의 진입점과 절차를 사용하고, 복구 후 빌드 실행기로 돌아온다. 전체 단계는 `cover_build/workflow.py`, 환경별 흐름은 `cover_build/pipelines/`에 있다. 서버 연동, 상태·증거, 저수준 호환 계층의 책임과 확장 규칙은 [references/architecture.md](references/architecture.md)를 따른다.

정확한 명령과 JSON은 스크립트 구현을 기준으로 하고 MD에는 판정 정책과 반드시 유지할 조건만 유지한다.

## 성공 보고

최종 답변의 첫 줄에는 반드시 `# 최종 결과: <판정>`을 표시한다. 판정은 `성공`, `실패`, `사용자 입력 대기`, `모델 판단 대기` 중 하나만 사용한다. 인사말이나 작업 설명을 이 제목보다 먼저 쓰지 않는다.

제목 바로 아래에는 상세 내용을 읽지 않아도 현재 상태를 알 수 있도록 다음 항목을 짧게 표시한다.

- 커버리지 빌드 성공 여부와 상태 코드
- 재사용 설정 적용 여부
- 선택한 빌드 시스템과 빌드 구성
- 실패하거나 대기 중이면 중단 단계, 오류 코드와 다음에 필요한 조치

계측 빌드는 성공했지만 재사용 설정 적용이나 적용 후 검증이 실패했다면 최종 판정은 `성공`으로 쓰지 않는다. 이 경우 `최종 결과: 실패`로 표시한 뒤, 요약에서 `커버리지 빌드: 성공`, `재사용 설정: 실패`를 구분한다.

판정 요약 다음에는 아래 세 항목을 각각 독립된 제목으로 표시한다. 상세 근거를 첫 요약에 모두 넣거나 링크만으로 대체하지 않는다.

1. **프로젝트 환경 및 빌드 파악**: 언어, 빌드 시스템과 판정 근거, 확인한 프로젝트·빌드 파일, 진입점·프로젝트·모듈·타깃·구성, 원본 빌드 명령·작업 경로, 상세 로그를 남기는 원본 빌드에서 관찰한 실제 컴파일러·링커·아카이버 또는 JDK를 설명한다.
2. **커버리지 빌드 적용**: wrapper/Toolbox/JACOV 주입 방식, 실제 실행한 커버리지 명령·작업 경로, Agent·ToolChainInfo·글루 코드 컴파일러 등 적용한 설정, 계측 처리 증거와 설정을 유지하고 재사용하는 방법을 설명한다.
3. **파일 변경**: 수정한 프로젝트·Agent 파일, 생성하거나 갱신한 `.cover-ee` 파일과 재사용 파일, 각 백업 경로를 구분해 절대 경로로 표시한다. 고객 빌드 파일을 수정하지 않았다면 이 항목을 생략하지 말고 `고객 빌드 파일 수정 없음`이라고 명시한다.

그 뒤 산출물의 신규 생성·원본 경로 일치 여부, 이후 IDE·수동 빌드 방법, 작업 로그 경로를 표시한다. Java 성공이면 **실행 방법** 항목을 추가하여 다음 두 선택지를 모두 보여준다. Local Agent 연동(LOWER)은 `tm-javalib-lower.jar`의 실제 경로·Maven 좌표와 기존 애플리케이션 실행 시 클래스패스에 정확히 한 번 추가하는 명령 형식을, JavaLib Agent는 생성본 `java_agent/cover-javalib-agent.jar`의 실제 경로·현재 존재 여부와 기존 JVM 명령에 붙이는 `-javaagent` 형식을 표시한다.

프로젝트의 메인 클래스나 `-jar` 실행 진입점을 확인하지 못했다면 추측하지 말고 자리표시자를 사용한다. 두 방식을 동시에 적용하도록 안내하지 않는다. 실패 보고에는 현재까지 파악한 환경과 실제 적용 시도·복구 결과를 설명한다. 기존 산출물, Agent 프로세스 존재, 사전 점검 또는 보조 실행 스크립트 생성만으로 성공이라 말하지 않는다.
