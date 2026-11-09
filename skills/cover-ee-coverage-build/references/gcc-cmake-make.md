# Linux GCC / CMake / Makefile

## 빌드 결과 판정 정책

커버리지 빌드는 실행 명령의 종료 코드가 0이면 성공, 0이 아니면 실패로 판정한다. 탐침 여부를 확인하기 위한 래퍼 호출·우회 로그 검사, 심볼 검사, C#/Java 처리 건수 집계와 빌드 후 산출물 갱신·경로 비교는 수행하지 않는다. 재사용 설정으로 재빌드할 때도 종료 코드로 판정한다. 설정 적용 결과는 빌드 결과와 별도로 보고하며, 설정 적용 실패로 이미 성공한 빌드 결과를 변경하지 않는다. 원본 빌드의 도구·산출물 조사, 실행 전 설정·출력 경로 검증, 백업·복구는 유지한다.


공통 COVER 환경, Agent 등록·실행과 자격 증명 처리는 `SKILL.md`와 `workflow.md`를 따른다. 여기서는 Linux GCC 환경의 판정, 원본 빌드, 커버리지 빌드만 다룬다.

## 판정 기준

- CMake 후보
  - `CMakeLists.txt`가 있으면 후보로 수집한다.
  - `CMakePresets.json`, `CMakeUserPresets.json`, 유효한 `CMakeCache.txt`도 함께 수집한다.
  - 고객 스크립트·CI에서 `cmake -S`, `cmake --preset`, `cmake --build`가 확인되면 CMake 진입점으로 판정한다.
  - `CMakeLists.txt`만 있고 실제 진입 증거가 없으면 바로 확정하지 않고 모델이 프로젝트 관계를 확인한다.
- CMake 내부 빌드 도구
  - generator가 Unix Makefiles, Ninja, Visual Studio 중 무엇이든 `CMAKE`로 처리한다.
  - 생성된 Makefile, `build.ninja`, 솔루션은 CMake 내부 구현으로 취급한다.
  - configure와 빌드는 CMake CLI로만 수행한다.
  - `make`, `ninja`, `MSBuild.exe`를 내부 빌드 도구로 직접 호출하지 않는다.
- 직접 Make 후보
  - 소스 디렉터리의 `Makefile`, `makefile`, `GNUmakefile`을 수집한다.
  - 고객 스크립트·CI의 실제 `make`, `gmake`, `mingw32-make` 명령과 작업 경로를 확인한다.
  - CMake가 생성한 Makefile은 직접 Make로 판정하지 않는다.
- 직접 Ninja
  - CMake 소유 관계가 없는 `build.ninja`는 현재 지원 대상에서 제외한다.
  - 원래 CMake 진입점 또는 직접 Make 진입점을 요청한다.
- STM32CubeIDE 탐지가 `CONFIRMED`이면 생성 Makefile을 직접 Make로 처리하지 않고 `stm32cubeide.md`를 적용한다.
- STM32CubeIDE 탐지가 `CANDIDATE`이면 CMake·직접 Make 후보와 함께 실제 빌드 소유 관계를 판정한다.
- `.project` 하나나 `arm-none-eabi-*` 문자열 하나만으로 Linux GCC 절차를 중단하지 않는다.
- 후보가 여러 개면 고객 스크립트·CI, 유효 캐시·프리셋, 기존 산출물 순으로 확인한다.
- 스크립트가 수집한 파일·설정·로그만으로 하나가 되지 않으면 모델이 파일 간 관계와 고객의 빌드 절차를 해석하고, 저장소에도 선택 근거가 없을 때만 사용자에게 진입점·타깃·작업 경로를 요청한다.
- 선택한 진입점의 직접 참조 수집
  - CMake는 `add_subdirectory()`와 리터럴 `include()` 대상만 프로젝트 범위 안에서 따라간다.
  - Make는 `include`, `-include`, `sinclude`의 리터럴 대상만 따라간다.
  - 변수·조건문으로 결정되는 참조와 프로젝트 밖 참조는 `unresolved`로 남기고 해당 부분만 모델이 해석한다.

## 원본 빌드

먼저 고객이 사용하는 빌드 경로와 옵션으로 원본 빌드를 수행한다. CMake와 직접 Make의 호출 방식은 다르지만, 실제 로그에서 도구와 산출물을 확인하는 기준은 공통이다.

### CMake

- 구조화된 원본 계획이 있으면 `originalBuildDirectory`를 CMake 빌드 디렉터리로 사용한다. 예를 들어 프리셋이 `out/build/linux-gcc-release`를 사용하면 별도로 존재하는 `build` 폴더를 선택하지 않는다.
- 원본 빌드가 성공하면 소스·빌드 경로, generator, 구성과 선택에 사용한 정보를 작업의 `temporary/original-cmake-selection.json`에 무결성 값과 함께 저장한다. 같은 작업을 재개할 때는 이 정보를 복원하므로 `--gcc-build-directory`를 반복해서 전달할 필요가 없다. 계측 configure가 변경한 캐시를 기준으로 원본 선택을 다시 계산하지 않는다.
- 재개할 때 다른 빌드 경로를 지정하면 `COVERAGE_OUTPUT_PATH_CHANGED`, 다른 소스·generator·구성을 지정하면 `CMAKE_ORIGINAL_SELECTION_CONFLICT`로 중단한다. 빌드 대상을 변경하려면 새 작업에서 원본 빌드부터 확인한다.

- 기존 `CMakeCache.txt`는 다음 조건을 모두 만족할 때만 유효한 빌드 디렉터리로 취급한다.
  - `CMAKE_HOME_DIRECTORY`가 현재 프로젝트 안의 실제 소스 디렉터리이고 해당 위치에 `CMakeLists.txt`가 있다.
  - `CMAKE_CACHEFILE_DIR`이 있으면 현재 캐시가 놓인 빌드 디렉터리와 일치한다.
  - `CMAKE_COMMAND`가 실제 CMake 실행 파일을 가리킨다.
  - `CMAKE_GENERATOR`가 기록되어 있다.
  - 빌드 디렉터리에 `CMakeFiles`가 있고, generator에 맞는 `Makefile`, `build.ninja` 또는 Visual Studio 솔루션이 존재한다.
- 위 조건이 하나라도 맞지 않으면 다른 프로젝트의 캐시나 중간 생성물로 보고 자동 재사용하지 않는다.
- 유효한 기존 `CMakeCache.txt`가 있으면
  - 기존 빌드 디렉터리와 generator를 보존한다.
  - `CMakeCache.txt`, `CMakeFiles`와 generator 내부 빌드 도구 메타데이터만 초기화하고 빌드 디렉터리의 고객 파일은 보존한다.
  - 원본 검증은 항상 configure부터 시작한 뒤 다음 형태로 수행한다.

```text
cmake -S <source-dir> -B <native-build-dir>
      [-G <generator>] [-DCMAKE_BUILD_TYPE=<configuration>] <preserved-arguments>
cmake --build <native-build-dir> --clean-first --verbose --parallel <N>
      [--config <configuration>] [--target <targets>]
```

- 유효한 캐시가 없을 때도 동일하게 configure부터 시작한다.
  - 프로젝트의 고객 표준 빌드 경로가 증거로 확인된 경우 그 경로를 사용한다.
  - 표준 빌드 경로를 결정할 수 없으면 작업마다 다른 대체 빌드 경로를 만들지 말고 원본 `-B`/작업 경로를 모델 또는 사용자에게 확인한다.
  - 프리셋·툴체인·sysroot·flags 등 확인된 인수를 보존한다.

```text
cmake -S <source-dir> -B <normal-build-dir>
      [-G <generator>] [-DCMAKE_BUILD_TYPE=<configuration>] <preserved-arguments>
cmake --build <normal-build-dir> --clean-first --verbose --parallel <N>
      [--config <configuration>] [--target <targets>]
```

사용자가 제공한 COVER configure 예시는 다음처럼 정해진 항목으로 나누어 작성한다. `.`은 실제 소스 경로로, `build`는 검증된 정상 빌드 경로로 정규화하며 래퍼 파일은 실행 시 현재 Agent에서 검증한 경로인지 확인한다.

```text
cmake -S <source-dir> -B <normal-build-dir> -G "Unix Makefiles" \
  -DCMAKE_C_COMPILER=<c-wrapper> \
  -DCMAKE_CXX_COMPILER=<cxx-wrapper> \
  -DCMAKE_LINKER=<link-wrapper> \
  -DCMAKE_AR=<ar-wrapper>
```

- 프리셋이나 고객 명령에서 빌드 구성·빌드 유형이 확인되지 않으면 `CMAKE_BUILD_TYPE=Release`를 임의로 추가하지 않는다.
- single-config generator는 고객 근거가 있을 때만 `CMAKE_BUILD_TYPE`을 전달하고, multi-config generator는 고객이 선택한 경우에만 `--config`를 전달한다.
- 고객 스크립트나 프리셋이 별도 절차를 정하면 그 절차를 정해진 항목으로 나누어 작성한 뒤 사용한다.
- `--clean-first`를 지원하지 않는 고객 절차에서만 검증된 clean 타깃을 수행한 뒤 빌드한다.

### 직접 Make

- 고객 스크립트에서 실행 파일, `-f`, `-C`, 변수, 타깃, 작업 경로를 추출한다.
- 선택한 Makefile의 실제 빌드 규칙(recipe)에서 컴파일러 변수를 확인한다.
  - `$(CC)`, `$(CXX)`, `$(LD)`, `$(AR)` 같은 표준 변수를 사용하면 해당 변수에 래퍼 경로를 전달할 수 있다.
  - `$(TARGET_CC)`, `$(CROSS_COMPILE)gcc` 같은 비표준 변수는 이름과 사용 위치를 증거로 남기고 모델이 `toolVariables`를 작성한다.
  - recipe에 `/opt/toolchain/bin/gcc`처럼 컴파일러 경로가 하드코딩되어 있으면 명령행 변수 주입이 안전하다고 가정하지 않는다.
- 실행 예정 명령 목록은 보조 확인에 사용한다.

```text
make -f <Makefile> -n -B [variables] [targets]
```

- clean 타깃이 확인되면 clean 후 빌드한다.

```text
make -f <Makefile> [variables] <clean-target>
make -f <Makefile> -j<N> [variables] [targets]
```

- clean 타깃이 없으면 임의의 `clean`을 호출하지 않는다.
- 이 경우 고객 절차를 우선하고, 증거가 없으면 `-B` 강제 재빌드를 모델이 검토한다.
- 안전하게 확정되지 않으면 사용자에게 clean/rebuild 절차를 요청한다.

### 실제 툴체인 확인

CMake·Make에서는 SKILL.md의 원본 툴체인 모델 판단 절차가 최종 선택을 담당한다. 아래 도구·역할 기준은 모델의 판단 기준이며, 로그 정규식으로 자동 확정하지 않는다. 검증기는 현재 작업·근거 해시·로그 인용·실행 경로 일치를 확인한다.


CMake와 Make 모두 아래 기준을 적용한다. 설치된 도구의 존재 여부와 이번 빌드에서 실제로 호출한 도구를 구분한다.

- COVER 래퍼가 없는 상태에서 원본 빌드를 먼저 수행하고 성공 여부를 확인한다.
- 상세 로그의 실제 명령에서 다음 경로를 추출한다.
  - C 컴파일러
  - C++ 컴파일러
  - 실제 링크를 수행한 컴파일러 드라이버 또는 독립 링커. `gcc`/`g++`가 최종 링크를 수행하면 그 실제 컴파일러 드라이버 경로를 논리적 링커 역할로 기록하고, PATH/cache에만 있는 `ld`를 보충하지 않는다.
  - 정적 라이브러리를 만들 때 아카이버
- `which gcc`, `which g++`, `CMakeCache.txt`, `compile_commands.json`, 현재 PATH는 후보 정보로만 사용한다.
- 최종 판정은 이번 원본 빌드 명령에 실제 나타난 실행 파일을 기준으로 한다.
- 원본 명령에 `@response-file`이 있으면 명령 작업 경로를 기준으로 제한적으로 펼쳐 `-c`, 입력 소스, `-o`, `/OUT:`을 판정한다. 중첩·순환·크기 제한을 적용하고 읽은 RSP의 경로·크기·SHA-256을 도구 증거에 기록한다.
- 명령을 출력하지 않는 recipe 때문에 역할이 보이지 않을 때만 Make의 `-n -B` 결과를 보조 근거로 사용한다.
- GCC 계열은 `--version`, `-dumpmachine`으로 계열·메이저 버전·타깃 식별 문자열(target triple)을 확인한다.
- 호스트 도구와 타깃 도구가 함께 있으면 타깃 역할만 래퍼 대상으로 선택한다.
- `HOSTCC`, 코드 생성기 등 호스트 도구를 타깃 래퍼에 매핑하지 않는다.
- 원본 빌드 실패, 필수 도구 불명확, 최종 산출물 불명확 중 하나라도 있으면 Agent 변경과 커버리지 빌드를 진행하지 않는다.
### 래퍼 매핑

CMake의 Visual Studio generator는 예외로 `cl.exe`·`link.exe`·`lib.exe` 동일 이름 매핑을 사용한다. 같은 이름과 원본 경로·역할이 일치하는 등록만 재사용하고, 별칭 등록은 보존한다. 동일 이름 충돌은 Visual Studio와 같이 사용자 결정으로 처리한다. 그 외 CMake/Make는 아래 규칙을 따른다.

원본 빌드에서 관찰한 도구마다 다음 순서로 매핑한다.

1. 원본 도구의 절대 경로와 역할이 일치하는 기존 등록을 찾는다.
2. 있으면 래퍼 이름과 측정 옵션을 보존해 재사용한다.
3. 없으면 `cs + 원본 파일명`으로 새 등록을 만든다. 예를 들어 Windows의 `g++.exe`는 `csg++.exe`, Linux의 `gcc`는 `csgcc`가 된다. Windows 확장자는 `.exe`로 통일한다.

기존 등록이 여러 개 일치하면 `AGENT_WRAPPER_MAPPING_AMBIGUOUS`로 후보를 제시한다. 새로 만들 `cs + 원본 파일명`이 다른 도구·역할에 사용 중이면 `cs2-<원본 파일명>`, `cs3-<원본 파일명>` 순서로 비어 있는 이름을 선택한다. 예를 들어 `csgcc.exe`가 다른 GCC를 가리키면 `cs2-gcc.exe`를 새로 등록하고 명령행에 해당 래퍼를 전달한다. 다음 실행에서는 이름을 새로 만들기 전에 원본 도구와 역할이 같은 기존 등록을 찾아 재사용한다. 기존 등록과 관련 없는 등록은 변경하지 않는다.

원본 경로는 상세 로그의 실행 명령을 기준으로 확정한다. 이름만 호출했으면 원본 빌드 환경의 PATH로, 상대 경로이면 해당 명령의 작업 디렉터리로 해석한다. `realTools`에는 이 절대 경로를 기록하고 `/usr/bin/gcc-ar` 같은 심볼릭 링크 별칭도 보존한다. canonical 경로는 동일 파일 비교와 GCC 계열·타깃 prefix 판정에만 사용한다.

서버의 기존 원본 경로가 `gcc`처럼 이름뿐이면 원본 빌드의 PATH로 같은 도구인지 확인한다. 이 확인은 비교에만 사용하며 서버에 등록된 경로 표기는 변경하지 않는다. 서버 설정의 상대 디렉터리는 기준 작업 경로를 알 수 없으므로 해석하지 않는다. Agent의 `wrapper`나 `toolchaininfo` 아래를 원본으로 지정하면 서버 변경 전에 `WRAPPER_SELF_REFERENCE`로 중단한다. 적용 후 `toolchaininfo/<wrapper-name>`이 확인된 원본 경로와 일치하는지 검증한다.

매핑 대상은 실제 호출된 역할로 한정한다. 같은 설치 디렉터리에 있다는 이유로 다른 도구를 추가하지 않으며, `ar`와 `gcc-ar`, `ld`와 컴파일러 드라이버를 서로 대체하지 않는다. `gcc`/`g++`가 링크까지 수행했다면 해당 컴파일러 래퍼를 링크에도 사용한다.

### 글루 코드 컴파일러

Linux/Unix 래퍼는 `gcc.config`와 `COVER_BR_CONFIG`를 사용하지 않는다. Linux CMake/Make 계획은 설정 파일 경로와 해시를 `null`로 기록하고, 실행·재사용 설정 게시에서도 파일 검증이나 복사를 수행하지 않는다. Agent의 `conf`가 없어도 이 이유로 빌드를 중단하지 않는다. 이 규칙은 설정 파일이 필요한 Windows GNU·STM32CubeIDE에는 적용하지 않는다.

원본 도구의 래퍼 매핑과 글루 코드 컴파일러는 역할이 다르다. C 소스만 있는 프로젝트라도 COVER가 생성한 C++ 글루 코드를 컴파일할 도구가 필요하다. 아래에서 찾는 C++ 대응 도구는 그 용도로만 사용한다.

- COVER 래퍼가 생성하는 글루 코드는 C++ 소스이므로 타깃 C++ 컴파일러를 별도로 확정한다.
- 상세 로그를 남기는 원본 빌드에 C++ 컴파일러가 있으면 그 실제 절대 경로를 사용한다.
- C 전용 빌드라 C++ 호출이 없으면 관찰된 C 컴파일러와 같은 디렉터리에서 이름만 대응되는 C++ 대응 도구를 확인한다.
  - `gcc` → `g++`
  - `<target>-gcc` → `<target>-g++`
  - `gcc-<version>` → `g++-<version>`
  - `clang` → `clang++`
  - `cc` → `c++`
- 같은 디렉터리의 대응 파일이 하나로 확인되지 않으면 커버리지 빌드 전에 중단하고 모델 또는 사용자 결정으로 넘긴다. PATH 전체나 다른 SDK에서 임의 검색하지 않는다.
- 이 대응 도구는 글루 코드 컴파일에만 사용한다. 원본 빌드에서 호출되지 않은 C++ 컴파일러를 Agent ToolChainInfo, 래퍼 매핑, CMake `CMAKE_CXX_COMPILER`, Make `CXX`에 추가하지 않는다.
- Linux에서는 최초 커버리지 configure/build에 `COVER_CC=<실제 C++ compiler 절대 경로>`를 전달한다. 성공 후 수동 빌드에 필요한 값은 `.cover-ee`의 빌드 시스템 연결과 보조 진입점에 기록하고 사용자 전역 환경은 변경하지 않는다.
- Windows GCC에서는 선택한 Agent 설정을 작업 사본으로 검증한 뒤 성공하면 재사용 위치인 `.cover-ee/config`로 옮긴다. `COVER_BR_CONFIG`가 폐기되는 작업 경로를 가리킨 채 성공 처리하지 않는다.
- 선택 결과와 근거는 `glueCodeCompiler.path`, `provenance`, `configurationMethod`, `mappingPolicy=GLUE_ONLY_NOT_AGENT_WRAPPER_MAPPING`으로 단계 JSON과 최종 빌드 요약에 기록한다.

## 커버리지 빌드 설정 및 빌드

처음에는 명령행으로 래퍼를 지정한다. 실제로 이 지정이 무시된 근거가 있을 때만 프로젝트 파일을 백업하고 필요한 도구 경로를 수정한다. 첫 시도와 대체 처리의 조건을 아래에서 구분한다.

커버리지 계획은 성공한 원본 계획의 명령에서 만든다. 실행 파일과 작업 디렉터리, 환경 초기화 결과, 제한 시간, 고객 인수의 순서와 값은 유지한다. 타깃과 병렬도를 다시 조합하지 않으므로 `FEATURE=ON` 같은 고객 변수가 빠지거나 프리셋의 선택 범위가 달라지지 않는다. 이 명령은 파일 수정 후 재시도와 `.cover-ee`의 수동 재실행에도 사용한다.

원본 명령이 빌드 도구가 아니라 셸이나 스크립트를 실행한다면 그 뒤에 CMake 옵션이나 Make 변수를 무작정 붙이지 않는다. `NATIVE_WRAPPER_INJECTION_UNRESOLVED`로 모델 판단을 요청하고, 스크립트가 수행하는 준비 작업과 실제 빌드 호출을 확인한다. 원래 준비 작업을 생략한 표준 명령으로 바꾸지 않는다.

### 산출물 경로 불변 규칙

- 상세 로그를 남기는 원본 빌드에서 관찰한 최종 실행 파일·공유 라이브러리·정적 라이브러리의 절대 경로를 `originalOutputPaths`로 고정한다.
- CMake의 `-B`, Make의 `-C`/작업 디렉터리와 출력 변수는 원본과 동일하게 유지한다.
- 작업별 `temporary` 디렉터리, 별도로 복제한 저장소, 경로 별칭를 실제 컴파일·링크 산출물 위치로 사용하지 않는다.
- 경로가 하나라도 달라지면 `COVERAGE_OUTPUT_PATH_CHANGED`로 즉시 중단한다.
- 커버리지 단계 JSON에 `originalOutputPaths`, `coverageOutputPaths`, `sameAsOriginal`, `missingOriginalPaths`, `unexpectedCoveragePaths`를 기록한다.

### CMake

- 원본과 동일한 검증된 프로젝트 빌드 디렉터리를 순차적으로 사용한다.
- 성공한 커버리지 configure의 래퍼 컴파일러 캐시를 같은 빌드 디렉터리에 유지하여 CMake IDE의 다음 Build/Rebuild가 그대로 사용하게 한다. `.cover-ee/applied-state.json`에는 빌드 디렉터리와 래퍼 설정을 기록한다.
- 원본 generator, 빌드 구성, preset/toolchain/sysroot/flags, 타깃을 보존한다.
- 원본 configure 인수에서 관찰된 도구에 해당하는 `CMAKE_C_COMPILER`, `CMAKE_CXX_COMPILER`, `CMAKE_AR`, `CMAKE_LINKER` 정의만 교체하고 없는 정의는 추가한다. `-D KEY:TYPE=value` 형식도 처리하며 다른 정의와 프리셋은 유지한다.
- 원본 configure 명령과 프리셋 옵션을 그대로 보존한다. 명령이 확정된 경우에는 캐시에서 수집한 값을 다시 뒤에 붙여 원본 옵션을 덮어쓰지 않는다. 실제 명령을 찾지 못해 캐시를 근거로 원본 명령을 구성하는 경우에만 보존 대상 캐시 값을 `commands.configure.arguments`에 넣는다.
- `coverageInjection.configureArguments`는 원본 옵션의 복사본이 아니라 실제 실패 후 configure에 보완할 인수 배열이다. 재사용할 성공 설정이 없는 첫 시도에는 빈 배열로 둔다. `--gcc-configure-arg`도 같은 기준을 적용하며, 원본 빌드에 필요한 고객 옵션은 원본 명령에 기록한다. 기존 성공 프로필이 재사용 검사를 통과하면 동일한 보완 인수를 유지한다.
- configure 또는 후속 컴파일·링크가 실제로 실패하면 모델이 해당 로그에서 원인을 확인하고 필요한 옵션만 제안한다. 예를 들어 컴파일러 확인 단계의 링크 실패를 확인하고 해당 타깃에서 정적 라이브러리 검사로 충분한 이유가 있을 때 `-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY`를 검토한다. 크로스 컴파일이라는 이유만으로 이 옵션을 미리 넣거나 컴파일러 검사를 무조건 성공 처리하지 않는다.
- 보완 인수와 현재 작업의 실패 로그·인용·변경 이유를 [결정 문서의 `coverageOptionRecovery`](decision-contract.md#실패-후-추가-옵션-보완)에 기록한다. 실행기는 실제 실패 기록과 인용 일치를 확인하며, 옵션이 실패를 해결하는지는 모델이 판단한다. 기존과 같이 소스·빌드 디렉터리, generator, 빌드 구성이나 래퍼 컴파일러 선택을 이 배열로 덮어쓰지 않는다. 승인된 보완 인수는 프로젝트 파일 수정 후 재시도에도 유지한다.
- 원본 빌드 인수의 `--` 뒤에 있는 내부 빌드 도구 옵션도 유지한다. 기존 `--clean-first`·`--verbose`는 중복 추가하지 않고, 별도 clean 명령이 있으면 이를 먼저 실행한다. 별도 clean이 없을 때만 누락된 `--clean-first`를 보완한다. CMake 생성 상태 초기화와 configure 수행 절차는 변경하지 않는다.
- 1차 시도는 configure 명령행의 래퍼 절대 경로만 사용한다.

```text
-DCMAKE_C_COMPILER=<c-wrapper>
-DCMAKE_CXX_COMPILER=<cxx-wrapper>
-DCMAKE_AR=<ar-wrapper>
-DCMAKE_LINKER=<link-wrapper>   # 별도 linker가 실제 확인된 경우. compiler-driver 링크면 CMake의 실제 link driver wrapper를 사용
```

- 위 옵션은 상세 로그를 남기는 원본 빌드에서 관찰된 역할에 대해서만 추가한다. 예를 들어 C 컴파일러와 컴파일러 드라이버의 링크 호출만 관찰된 C 프로젝트에는 C++ 컴파일러와 아카이버 옵션을 만들지 않는다.

- `CC`, `CXX`, `AR`, `LD` 환경변수와 PATH 선두 변경, 툴체인 overlay는 래퍼 주입 대체 처리로 사용하지 않는다.
- 원본 도구와 래퍼가 섞인 기존 캐시를 커버리지 빌드에 재사용하지 않는다.
- configure 후 새 `CMakeCache.txt` 또는 실제 빌드의 상세 로그에서 래퍼가 적용되지 않은 것이 확인되면 즉시 1차 시도를 종료한다.
- 이 경우에만 선택한 `CMakeLists.txt`와 실제 컴파일러를 직접 덮어쓰는 프로젝트 내부 `.cmake` 파일을 작업별 백업한다. 파일 전체를 바꾸지 않고 컴파일러·아카이버·링커 경로만 래퍼로 최소 수정한다.
- 백업 뒤에는 동일한 정상 빌드 디렉터리에서 다시 configure/build한다. 공유 SDK, STM32CubeIDE 설치본, 프로젝트 루트 밖 툴체인 파일은 자동 수정하지 않고 사용자 확인으로 남긴다.
- 별도 clean 절차가 없는 경우의 기본 명령 형태는 다음과 같다. 고객의 원본 명령이 프리셋이나 추가 옵션을 사용하면 그 인수를 유지하며 아래 예제로 다시 조립하지 않는다.

```text
cmake --build <normal-build-dir> --clean-first --verbose --parallel <N>
      [--config <configuration>] [--target <targets>]
```

- 생성된 Makefile, `build.ninja`, 솔루션을 수정하거나 직접 실행하지 않는다.
- 수정 검증이 실패·중단되면 원본 파일의 바이트를 복구하고 SHA-256이 백업 명세(manifest)와 일치해야 한다. 성공한 수정이 이후 수동 빌드에도 필요한 경우에는 원본 옆의 `<원본파일명>.cover-ee-backup`을 유지하고 `.cover-ee/backup`에도 복구용 사본과 적용 해시를 기록한다. 작업 중 고객 변경이 감지되면 덮어쓰지 않고 중단한다.

### 직접 Make

- 1차 시도는 현재 빌드 프로세스의 명령행에서 역할 변수를 주입한다.
- 원본 clean·build 또는 강제 rebuild 명령의 `-f`, `-C`, 병렬도, 타깃과 고객 변수를 유지한다. `toolVariables`로 선택한 도구 변수만 래퍼 값으로 바꾸고, 기존 네이티브 아카이버 대체 처리가 선택된 경우에는 그 검증된 값을 사용한다. 원본 계획의 준비용 configure 명령도 그대로 먼저 실행한다.

```text
CC=<c-wrapper> CXX=<cxx-wrapper> LD=<link-wrapper> AR=<ar-wrapper>
```

- 실제 Makefile이 다른 변수명을 사용하면 모델이 확인한 식별자만 `toolVariables`로 전달한다.
- `HOSTCC`, `RANLIB`, `OBJCOPY` 등 COVER 대상이 아닌 역할은 원본 값을 보존한다.
- 환경변수 컴파일러 주입, PATH 선두 변경, overlay는 대체 처리로 사용하지 않는다.
- 명령 구성에서 관찰된 필수 타깃 도구에 선택한 래퍼 경로를 전달한다. 빌드 후 호출 로그를 성공 조건으로 검사하지 않는다.
- 하드코딩·override·sub-make 등으로 래퍼 선택 실패가 확인된 경우에만 `make -n -B`를 진단 증거로 실행한다.
- clean 타깃이 있으면 clean 후 빌드한다.
- clean 타깃이 없으면 검증된 고객 절차 또는 모델이 승인한 `forceRebuild=true` 계획으로 `-B` 강제 재빌드를 수행한다.
- 하드코딩, override, sub-make 때문에 명령행 주입이 무시된 것이 실제 증거로 확인될 때만 대체 처리를 적용한다.
  - 선택한 Makefile, 포함된 `.mk`, 실제 컴파일러를 직접 호출하는 프로젝트 내부 빌드 스크립트를 작업별로 백업한다.
  - 확인된 표준/고객 변수 또는 직접 컴파일러 호출의 도구 경로만 래퍼 절대 경로로 최소 수정한다.
  - clean/rebuild를 수행하고 종료 코드로 결과를 판정한다.
- 프로젝트 루트 밖 파일, 공유 SDK/STM32CubeIDE 설치 경로는 자동 수정하지 않는다.
- 백업 명세(manifest)에는 원본 경로, SHA-256, 크기, 수정 시각, 인코딩/BOM, 줄바꿈을 기록한다. 커밋하지 않은 변경이 있는 파일(dirty 파일)도 현재 바이트를 그대로 복구하며 git checkout/reset을 사용하지 않는다. 동시 변경을 감지하면 덮어쓰지 않고 중단한다.
- 명령행 주입이 성공하면 프로젝트 파일을 수정하지 않는다. 검증된 도구 변수와 `COVER_CC`/`COVER_BR_CONFIG`를 포함한 재실행 명령을 `.cover-ee`와 최종 채팅에 남긴다.
- 명령행 주입이 실패하고 프로젝트 파일 수정으로 성공한 경우에만 각 원본 파일과 같은 디렉터리에 `<원본파일명>.cover-ee-backup`을 만들고 수정본을 유지한다. 다음 스킬 실행의 원본 빌드 동안만 백업본을 일시 적용하며, 기존 백업 내용이나 현재 수정본 해시가 다르면 덮어쓰지 않는다.

### 성공 판정

- CMake configure/cache와 Make 명령 구성의 실행 전 검증은 유지한다.
- 실제 커버리지 빌드 명령의 종료 코드가 0이면 성공으로 판정한다.
- 래퍼 호출·우회 로그, 정의된 `cstm*` 심볼과 산출물 갱신 여부를 후속 성공 조건으로 검사하지 않는다.
- 원본 출력 경로를 유지하도록 계획하며 재사용 설정 적용 결과는 별도로 보고한다.

프로그램 실행, 서버 Module 조회와 측정 데이터 생성·조회는 별도 요청으로 수행한다.

## 실행 시간 최적화

- CMake와 Make의 고객 병렬도 근거가 없으면 CPU 기반 병렬도(최대 8)를 사용한다. 고객 명령·프리셋·CI의 명시값이 있으면 그대로 보존한다.
- 직접 Make는 실제 커버리지 빌드 전에 `make -n -B`를 매번 실행하지 않는다. 실제 빌드에서 래퍼 선택 실패가 확인됐을 때만 대체 처리 대상 파일을 정하기 위한 진단으로 실행한다.
- 원본 Make의 명령을 출력하지 않는 recipe 때문에 실제 도구 역할이 보이지 않을 때만 원본 성공 후 실행 예정 명령 목록을 보조 실행한다.
- CMake는 속도 최적화 대상에서 예외다. 원본 네이티브 configure와 커버리지 configure를 각각 수행하고, 두 configure 사이에서 컴파일러 캐시를 재사용하지 않는다.

## CMake Visual Studio 생성기

원본 캐시에서 Visual Studio 생성기를 확인한 경우 CMake가 선택한 MSVC 컴파일러 식별은 유지한다. `CMAKE_C_COMPILER`·`CMAKE_LINKER`를 래퍼로 덮어쓰는 대신, `cmake --build`의 `--` 뒤에 MSBuild `ForceImportBeforeCppTargets` 속성을 전달한다. COVER 속성 파일은 관찰된 역할에 따라 CL·Link·Lib 도구 경로를 지정한다. 원본 캐시의 플랫폼·툴셋·Visual Studio 인스턴스도 계측 구성에 전달한다.

CMake 캐시 검사는 생성기 일치까지만 확인하며, 커버리지 빌드 결과는 실제 빌드 명령의 종료 코드로 판정한다. 캐시가 원본 컴파일러를 가리키는 것은 이 방식에서 정상이다.

명령행 주입이 래퍼 선택 검증에서 실패하면 선택한 `CMakeLists.txt`만 백업하고, 타깃 생성 이후 COVER 속성 파일을 연결하는 블록을 추가한다. 이 블록은 CMake 구성 때 빌드 디렉터리에 속성 파일을 생성하고 하위 디렉터리의 네이티브 타깃에도 `VS_USER_PROPS`로 연결한다. 기존 사용자 속성 파일은 먼저 import한다. 재시도에서는 명령행 속성 주입을 제거해 프로젝트 연결 자체를 검증한다. 실패하면 원본을 복구하고, 성공하면 기존 옆파일 백업 규칙으로 수정본을 유지한다. 생성된 `.vcxproj`는 직접 수정하지 않는다.

성공한 명령행 방식은 속성 파일을 `.cover-ee`의 재사용 자산으로 복사하고 검증된 재실행 명령을 제공한다. 파일 수정 방식은 CMake 재구성 때 연결을 다시 생성한다. 두 방식 모두 플러그인이 제공하는 `.cover-ee` 재실행 명령으로 COVER 환경을 유지하며, CMake의 기존 구성·타깃·출력 경로를 사용한다. 단순히 빌드 디렉터리에서 MSBuild를 직접 실행하는 것으로 재사용을 안내하지 않는다.

## GCC/Clang 결과 보고

GCC/Clang도 공통 종료 코드 기준을 사용한다. `nm`을 추가 실행하거나 바이너리 심볼을 검사하지 않는다. 빌드 로그는 보관하되 탐침 성공 건수나 바이너리 내 탐침 코드 포함 여부를 확인했다고 보고하지 않는다.
