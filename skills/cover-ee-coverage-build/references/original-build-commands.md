# 원본 빌드 명령 확인과 상세 로그 실행

## 입력 우선순위

고객이 제공한 명령은 실제 빌드 방법을 알 수 있는 가장 직접적인 근거다. 실행 전에는 명령을 구성 항목으로 나누어 확인한다. 이렇게 해야 작업 디렉터리와 환경 초기화를 보존하면서 경로와 인자를 검증할 수 있다.

- 프로젝트 경로를 확인한 직후 다음을 한 번 안내한다.

  > 평소 사용하는 원본 빌드 명령이나 스크립트가 있으면 알려주세요. 없다면 프로젝트 파일을 분석해 자동으로 찾겠습니다.

- 사용자가 명령이나 스크립트를 알려주면 `USER_PROVIDED` 근거로 가장 먼저 검토한다. 단, `cmd /c`, `sh -c`, PowerShell 문자열, `source ... && ...`처럼 원문 셸 문자열을 저장하거나 그대로 실행하지 않는다.
- 모델은 해당 입력을 다음 항목으로 나눠 `cover-original-build-plan/v1`에 기록한다.
  - `executable`: 실제 실행 파일 절대 경로
  - `arguments`: 문자열 배열
  - `workingDirectory`: 프로젝트 또는 현재 작업 디렉터리 아래의 작업 경로
  - `environmentInitialization`: `INHERITED_PROCESS`, `VSDEVCMD_CAPTURED`, `CUSTOMER_BUILD_SCRIPT`, `SDK_ENVIRONMENT_SCRIPT`, `STRUCTURED_ENVIRONMENT_DELTA` 중 하나와 필요한 경우 검증된 환경 초기화 스크립트 경로
  - `environmentDelta`: 비밀값이 없는 필요한 환경 변수만
  - `evidenceSource`: `USER_PROVIDED`, `SCRIPT`, `CI`, `CACHE`, `PRESET`, `MODEL` 중 하나
- 인자·환경 변수 이름·근거 문자열에 password, token, secret, API key 등 인증성 값이 있으면 계획과 조사서에 넣지 않고 사용자에게 안전한 실행 환경에서 값을 제공하도록 안내한다.
- 통합 실행기는 원본 configure/build와 일반 고객 도구에는 `CODESCROLL_TM_INSTALL_PATH/lib`를 주입하지 않는다. COVER 래퍼·Agent·Toolbox·JACOV처럼 제품 라이브러리를 실제로 불러오는 명령에만 `LD_LIBRARY_PATH` 선두로 추가하고, `environmentDelta`에 이미 SDK·프로젝트 라이브러리 경로가 있으면 그 순서를 뒤에 보존한다.
- Visual Studio 사용자가 명령을 모르는 경우에는 `.sln` 또는 `.vcxproj`와 평소 사용하는 `Configuration|Platform`을 받아 MSBuild 계획으로 구조화한다. UI의 현재 빌드 구성(Active Configuration)을 조회하거나 Visual Studio를 새로 실행하지 않는다.

## 자동 추론 순서

사용자 입력이 없을 때만 아래 순서를 적용한다.

1. 고객 빌드 스크립트와 CI의 명시적 빌드 진입점 (`SCRIPT` 또는 `CI`)
2. 현재 프로젝트와 일치하는 CMake 캐시, 프리셋, 기존 빌드의 상세 로그 (`CACHE` 또는 `PRESET`)
3. 프로젝트 파일로 바로 만들 수 있는 표준 명령
4. 남은 후보의 관계를 해석하는 모델 계획 (`MODEL`)
5. 위 근거가 모두 부족한 항목만 사용자에게 질문

표준 명령은 추측한 고객 설정이 아니다. 구성·타깃·SDK 환경 초기화처럼 프로젝트 파일만으로 결정되지 않는 값은 모델이 증거를 해석하거나 사용자에게 확인한 뒤에만 넣는다.

## 원본 빌드(verbose) 호출 형태

아래는 원문 셸 명령이 아니라 계획으로 구조화할 때의 호출 형태다. 실제 경로·구성·타깃은 선택된 근거로 채운다.

| 환경 | 원본 빌드 호출 | 실제 도구 관찰 |
| --- | --- | --- |
| Visual Studio | `<MSBuild> <.sln 또는 .vcxproj> /p:Configuration=<구성> /p:Platform=<플랫폼> /t:Rebuild /fl /flp:LogFile=<작업 로그>;Verbosity=diagnostic` | 새 진단 로그에서 `cl.exe`, `link.exe`, 필요 시 `lib.exe` |
| Linux GCC / CMake | 매번 생성된 CMake 상태를 초기화한 뒤 `cmake -S <source> -B <build> ...`, 이어서 `cmake --build <build> --clean-first --verbose` | 상세 로그의 컴파일러, 링커, 아카이버 |
| Linux GCC / Makefile | 검증된 `make -f <Makefile> <clean target>`, 이어서 `make -f <Makefile> -j<N> <target>` | 실제 recipe 출력, 필요한 경우 빌드 후의 제한된 실행 예정 명령 목록 |
| STM32CubeIDE | Eclipse CDT managed builder의 headless `-cleanBuild <project>/<configuration> -verbose` | IDE 번들의 `arm-none-eabi-gcc`, `g++`, 실제 링크 드라이버, `ar` |
| C# | 고객이 사용하는 `dotnet build` 또는 `MSBuild`에 diagnostic/binlog 옵션을 구조화해 적용 | 원본 로그/binlog의 `csc.exe` 및 Target Framework 평가 결과 |
| Java / Maven | `mvn` 또는 `mvnw`의 고객 goal·프로필·모듈을 보존하고 필요 시 안전하게 debug 수준을 높인 빌드 | Maven의 실제 `javac`/plugin 실행과 산출물 |
| Java / Gradle | `gradle` 또는 `gradlew`의 고객 task·프로젝트·속성을 보존하고 `--info`를 적용한 빌드 | Gradle task 출력의 `javac`/plugin 실행과 산출물 |

Maven `-X`와 Gradle `--debug`는 설정 값에 인증 정보가 노출될 가능성이 있으므로 기본값으로 사용하지 않는다. 필요할 때에도 로그를 고객 비밀값 없이 보관할 수 있는지 먼저 확인한다.

## 원본 빌드 이후

- 어떤 근거로 명령을 골랐든 원본 빌드는 새 상세·진단 로그를 남기며 실제 수행한다.
- `cover-environment-survey.json`에는 원본 빌드 전에 계획의 `evidenceSource`, `workingDirectory`, `environmentInitialization`, 구조화된 명령만 확정한다.
- 로그에서 새로 관찰한 `actualObservedToolchain`, 실제 실행 명령과 산출물은 `original-build-evidence.json`에 기록한다.
- 계획에 들어 있던 컴파일러 경로나 Agent의 기존 툴체인 정보는 후보일 뿐이다. 실제 관찰 결과가 커버리지 래퍼 매핑의 기준이다.
