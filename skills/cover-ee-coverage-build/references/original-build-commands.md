# 원본 빌드 명령 확인과 상세 로그 실행

## 원본 명령 보존과 실패 후 보완

커버리지 빌드는 고객의 원본 빌드에 COVER 설정을 연결하는 작업이다. 실행 파일, 작업 디렉터리, 프리셋, 옵션, 타깃과 환경을 보존한다. 예상되는 오류를 피하려고 configure 검사 방식을 미리 바꾸거나 빌드·테스트 범위를 줄이지 않는다. 원본 명령에 이미 있던 옵션은 그대로 사용한다.

환경별 정상 연결은 다음과 같다. 이 설정은 커버리지 빌드에 필요한 절차이므로 실패할 때까지 미루지 않는다.

- CMake: 원본 configure·build 명령에 확인한 래퍼 경로를 연결한다. 추가 configure 우회 옵션은 첫 시도에 넣지 않는다.
- 직접 Make: 원본 명령을 유지하고 확인한 도구 변수만 래퍼로 바꾼다. 기존 clean/rebuild 방식과 고객 타깃을 유지한다.
- Visual Studio: 원본 MSBuild 명령에 래퍼 속성을 연결한다. 별도로 선택한 커버리지 구성, 확인된 SDK 보조 설정과 기존 clean/rebuild 절차를 유지한다.
- STM32CubeIDE: 원본 프로젝트·구성과 headless 빌드 절차를 유지한다. 기존 CDT 래퍼 연결 및 필요한 도구 명령 변경만 수행한다.
- C#: 원본 명령·프레임워크·구성과 restore 절차를 유지한다. Toolbox로 COVER 설정을 연결하며 컴파일 오류를 피하려고 경고·테스트 정책을 임의로 바꾸지 않는다.
- Java: 원본 Maven goal, Gradle task 또는 빌드 스크립트를 유지한다. JACOV와 필요한 Java 런타임을 연결하며 새 테스트 생략 옵션이나 임의의 대체 task를 추가하지 않는다.

실제 실행이 실패하면 로그에서 원인을 확인한 뒤 필요한 부분만 보완한다. CMake·Java의 추가 옵션과 현재 작업의 실패 근거를 전달하는 형식은 [결정 문서](decision-contract.md#실패-후-추가-옵션-보완)를 따른다. 다른 환경의 래퍼 적용 실패·프로젝트 파일 수정·복구는 각 환경의 기존 절차를 유지한다. 빌드 성공 판정 기준은 변경하지 않는다.

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

## 재개 시 생성물과 원본 입력 구분

모든 빌드 시스템의 원본 계획에 `outputTracking`을 지정한다.
`directories`는 생성물 전용 디렉터리의 절대 경로 배열, `files`는 소스와 출력이 섞인
경로에서 제외할 생성 파일의 절대 경로 배열, `reason`은 해당 경로가 생성물이라는 근거다.
빈 배열도 가능하지만 입력 감지에 영향을 주는 생성물이 없다는 근거를 적는다.
CMake의 별도 바이너리 디렉터리만 계획에서 자동 유도한다. 나머지는 작업 디렉터리를 출력
디렉터리로 간주하지 않는다. 미지정 또는 소스와 겹치는 선언은 모델 판단으로 돌아간다.

- Make: 실제 recipe의 출력 및 생성 규칙을 읽는다. in-source 빌드는 files를 사용한다.
- Maven/Gradle: 선택된 하위 프로젝트와 적용된 build directory/생성 소스 경로를 확인한다.
- MSBuild/C#: 선택한 구성의 OutputPath/IntermediateOutputPath 및 사용자 지정 경로를 확인한다.
- STM32CubeIDE: 선택한 구성의 생성 디렉터리를 확인한다.
- Java CLI: 실제 스크립트의 출력/생성 경로를 확인한다.

`build`, `bin`, `obj`, `target`이라는 이름만으로 소스를 제외하지 않는다.
프로젝트 루트/상위 경로, 빌드 진입점, 근거 파일, 이미 색인된 소스 입력은 제외할 수 없다.
생성 소스가 이미 존재해 원본과 구분되지 않으면 근거를 추가 조사하며 무조건 숨기지 않는다.
생성 경로는 원본 빌드 전에 작업 색인에 저장하며 재개 시 탐색·서명·지문에 같은 범위를 적용한다.

## C/C++ 아카이브 처리 방식 검토

모드 선택은 모델이 수행한다. 스크립트는 링크 형태나 아카이브 분류만으로 모드를 추론·변경하지 않고, 증거 형식과 모델이 명시한 필요 모드·선택 모드·현재 설정·실행 모드의 일치 여부를 검증한다.

### 모델의 판단 순서

1. 원본 상세 로그의 모든 최종 EXE/DLL 링크 명령과 RSP를 읽는다. 직접 객체와 아카이브를 구분하고, 객체의 컴파일 입력 소스 및 아카이브의 생성 명령에서 최종 소비 링크까지 연결한다. 파일명, `.a` 확장자, `--whole-archive` 하나만으로 결정하지 않는다.
2. 이번 측정 대상 소스에서 생성되는 객체가 최종 링크에 직접 전달되는지, 모두 아카이브를 거치는지 판단한다. 시작 코드·외부 비계측 객체가 있다는 이유만으로 직접 측정 대상 객체가 있다고 판단하지 않는다. 원본 빌드 조사로 예상하는 구성과 실제 COVER 등록 사실은 구분한다.
3. 최종 출력의 예상 COVER 모듈 구성을 검토해 `requiredMode`를 결정한다.
   - 측정 대상 객체가 직접 들어오고 아카이브는 독립 라이브러리 모듈로 유지하려는 구조이면 `module`을 선택할 수 있다.
   - 측정 대상 객체가 모두 아카이브로 전달되고 최종 EXE/DLL 자체를 COVER 모듈로 만들어야 하면 `tu`를 선택한다. xxHash의 객체 → 중간 `objects.a` → 최종 DLL/EXE 구조가 이 경우다.
   - 직접 객체와 아카이브가 섞여도 아카이브 내부 TU까지 최종 출력 모듈에 포함해야 한다면 `tu`를 검토한다. 직접 객체가 있다는 이유만으로 `module`을 강제하지 않는다.
   - 아카이브만 링크하더라도 기존 독립 라이브러리 모듈만 유지하려는 의도이면 `tu`를 강제하지 않는다.
4. 근거가 부족하면 원본 명령·RSP·빌드 파일을 추가 조사한다. 여러 최종 타깃에서 요구 모드가 충돌하면 Agent 전역 설정 하나로 충족 가능한지 검토한다. 프로젝트 근거로 해결할 수 없는 사용자 의도·필수 정보가 있을 때만 구체적인 차이를 질문한다. 판단 대기만으로 작업을 끝내지 않는다.

위 판단은 COVER EE의 다음 처리에 근거한다. `module`은 아카이브의 모듈 ID를, `tu`는 내부 TU ID를 읽는다. 서버는 등록된 객체 입력이 하나 이상일 때만 최종 출력의 새 모듈 큐를 생성하며 그 객체 TU들을 포함한다. `module` 처리된 아카이브만으로는 최종 출력의 새 모듈 큐가 생기지 않는다. 단순히 링크 명령에 `.o`가 있다는 것과 서버가 측정 대상 객체로 인식한다는 것은 다르다. 이 옵션은 아카이브만 있는 경우에 자동 전환되지 않으며, `tu`도 최종 링커가 채택한 멤버만 선별하는 기능으로 가정하지 않는다.

### 판단 기록과 설정 적용

`originalToolchainDecision.archiveReview`에 `mode`(KEEP/tu/module), `reason`, `moduleIntent`, `links`를 기록한다. 아카이브 입력이 있으면 모델이 판단한 `requiredMode`(tu/module)를 별도로 기록한다. `reason`과 `moduleIntent`에는 직접 객체의 출처, 측정 대상 객체의 전달 경로, 아카이브 생성·소비 관계, 최종 출력의 예상 모듈 구성과 선택 이유를 설명한다. 판별되지 않은 사항을 확인된 사실처럼 기록하지 않는다.

각 link에는 절대 output, kind(EXECUTABLE/SHARED_LIBRARY/STATIC_LIBRARY), inputKind(OBJECT/ARCHIVE/MIXED), archiveRole(NONE/INTERMEDIATE/INDEPENDENT/UNKNOWN), 원본 로그의 line/commandText, responseFiles(path/sha256), archiveCommands를 넣는다. archiveCommands도 line/commandText/responseFiles를 가진다. 응답 파일을 실제로 읽고 생성 명령과 소비 링크를 연결한다. 이 분류 값들은 증거 설명이며 스크립트의 모드 자동 선택 규칙이 아니다.

`requiredMode`는 필요한 심볼 처리 방식이며 `mode`는 설정 변경 여부를 포함한 선택이다. `KEEP`은 현재 설정과 실행 중인 Agent 모드가 모두 `requiredMode`와 일치할 때만 허용한다. 불일치 또는 필요 모드 누락은 모델 재판단 요청으로 처리하고 같은 작업에서 결정을 보완한다. 아카이브가 없으면 KEEP과 그 근거를 기록한다.

위 판단을 위해 빌드 후 계측 심볼 검사나 서버 Module 조회를 추가하지 않는다. 원본 빌드 구조의 조사와 설정 검증 범위에서 수행하며, 빌드 성공은 실행 종료 코드로만 판정한다.

Agent 설정과 권장 모드가 다르면 `archivePolicyApproval` USER 결정으로 변경을 승인받는다.
제시된 jobId/agentRoot/configSha256/from/runtime/to/reviewDigest를 그대로 포함하고
approved=true를 넣는다. 기존 사용자 승인이 정확히 같은 변경을 포함하면 재질문 없이
그 승인을 USER 결정으로 기록한다. 이 옵션은 Agent 전체에 적용되며 재시작이 필요함을 알린다.
다른 빌드가 있거나 프로세스를 확인할 수 없으면 적용을 보류한다.
백업/적용/복구 결과는 archive-policy-state.json에 남는다. 설정 실패와 빌드 실패를 구분하며
빌드 성공은 종료 코드로만 판단한다. 모듈 조회는 별도 요청 시에만 수행한다.
빌드에 사용한 모드, Agent 범위, 백업 위치, 복원 결과와 재사용 요구사항을 보고한다.

### 임시 아카이브 설정의 종료 처리

아카이브 모드 변경은 기본적으로 이번 실행에만 적용한다. 승인에는 `lifetime: WORKFLOW_INVOCATION`을 포함한다. 성공·실패·결정 대기·정상적인 인터럽트 모두 종료 처리에서 변경 전 설정을 복원하고 Agent를 재시작하여 실행값을 확인한다. 강제 종료로 복원이 누락되면 같은 작업 재개 시 다른 단계보다 먼저 복구한다. 외부에서 설정을 바꿨거나 다른 빌드가 실행 중이면 덮어쓰거나 재시작하지 않고 복원 실패를 별도로 보고한다. 빌드 종료 코드는 복원 결과로 바꾸지 않는다. 재사용 설정에는 필요한 모드를 남기며, 일반 IDE·수동 빌드에서 자동 적용되는 것으로 안내하지 않는다. 해당 빌드를 다시 수행하려면 플러그인을 통해 필요한 모드를 적용하고 종료 시 복원한다.
