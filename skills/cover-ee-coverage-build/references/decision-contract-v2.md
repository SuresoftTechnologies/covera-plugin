# 결정 파일 및 원본 빌드 계획 작성 규칙

## ToolChainInfo 선택

일반 네이티브 Agent 준비 단계는 매핑이 없는 컴파일러에 대해서만 `getToolChainInfos` 전체 응답을 조회해 `temporary/toolchain-info-candidates.json`에 실제 컴파일러 경로, 버전 출력, 타깃 triple과 함께 제공한다. 새 매핑의 후보가 하나여도 모델이 호환성을 확인한다. 기존 매핑은 연결된 ToolChainInfo와 모든 설정을 그대로 사용하며 이 선택 단계에 포함하지 않는다. API 목록에는 파서 본문이 없으므로 이름만으로 ABI를 확정할 수 없으면 사용자에게 필요한 정의·설정 확인을 요청한다. 목록 밖 ID나 가장 가까운 비호환 정의를 선택하지 않는다.

모델은 OS/ABI(Linux LP64와 MinGW LLP64 등), 컴파일러 계열, 아키텍처, 버전을 비교하고 모든 관찰 컴파일러의 선택을 하나의 `toolChainInfoSelection`으로 작성한다. 예시의 digest와 ID는 현재 증거 값으로 대체한다.

```json
{"toolChainInfoSelection":{"evidenceDigest":"현재 증거의 evidenceDigest","mappings":{"gcc":{"id":"조회된 ID","reason":"실제 도구와 정의의 OS/ABI·계열·버전·아키텍처 비교 근거"},"g++":{"id":"조회된 ID","reason":"C++ 파서 타입 모델을 포함한 호환 근거"}}}}
```

현재 작업 아래 choices 파일을 만들고 `decision --choices-file`로 무결성 보호된 결정을 생성한다. 재개 시 전체 API 목록을 새로 조회하고 작업·도구·목록 digest가 달라지면 다시 선택한다. 실행기는 ID의 유일한 존재, 전체 컴파일러 선택과 근거, 명백한 OS/계열/주 버전/아키텍처 충돌을 검증한다. 적용 후 선택한 ID까지 재조회로 확인하며, 검증된 현재 매핑과 같으면 PATCH하지 않는다. 판단 불가능하면 `--escalate-to-user`를 사용한다. C#에는 적용하지 않고 STM32CubeIDE의 지정 템플릿 절차는 유지한다.

`cover_workflow_runner.py`가 `WAITING_MODEL` 또는 `WAITING_USER`를 반환하면 해당 작업의 `decision.template.json`을 기준으로 결정 파일을 만든다. 다른 작업의 결정 파일을 재사용하지 않는다.

같은 작업에서 모델의 빌드 계획 결정 뒤 사용자 실행 승인이 이어지면, 실행기는 무결성이 확인된 선행 결정을 `decision-history.json`에 작업 범위로 보존하고 현재 결정과 병합한다. 새 결정이 이전 선택을 명시적으로 바꾸면 현재 결정이 우선한다. 다른 `jobId`의 이력, 무결성이 깨진 이력, 자격 증명이 포함된 값은 사용하지 않는다.

## 결정 주체

- `MODEL`: 정형 규칙이 하나의 답을 만들지 못해, 모델이 수집된 프로젝트 파일·캐시·프리셋·빌드 로그의 의미와 관계를 해석한다.
- `USER`: 고객 설정, 실행 권한, 모델로도 확정할 수 없는 원본 빌드 명령을 사용자가 정한다.

원본 빌드 명령은 먼저 사용자에게 선택적으로 받는다. 사용자가 제공한 명령·스크립트 또는 Visual Studio의 `.sln`과 `Configuration|Platform`은 `USER_PROVIDED` 최우선 근거다. 입력이 없을 때만 `SCRIPT/CI -> CACHE/PRESET/기존 로그 -> 프로젝트 표준 명령 -> MODEL -> USER` 순으로 해결한다.

이는 Agent를 지휘하는 주체의 순서가 아니라 증거 확정 수준이다. 정형 규칙에서 답이 하나면 별도의 `WAITING_MODEL` 결정을 만들지 않는다.

`SCRIPT`는 의미를 추측하는 주체가 아니다. 빌드 파일, 프리셋, 캐시, 스크립트, 실행 로그와 프로세스 증거를 수집하고 명백한 표준 계획만 만든다. 고객별 엔트리, 작업 경로, 환경 초기화 스크립트, 타깃, 호스트/타깃 도구 또는 Make 변수 선택이 필요하면 `MODEL`이 `build-evidence.json`을 읽고 `originalBuildPlan`을 작성한다.

## 원본 빌드 계획 작성

`originalBuildPlan`은 실행할 명령과 그 명령을 선택한 이유를 함께 보관한다. 실행기가 임의의 셸 문자열을 해석하지 않도록 항목별 자료형도 검증한다.

계획은 `cover-original-build-plan/v1` 스키마를 사용하고 다음을 포함한다.

- 현재 `jobId`, 계획 생성 방식인 origin(`SCRIPT_EXTRACTED`, `MODEL_INFERRED`, `USER_CONFIRMED_FALLBACK`)과 별도의 `evidenceSource` (`USER_PROVIDED`, `SCRIPT`, `CI`, `CACHE`, `PRESET`, `MODEL`)
- 선택한 `MSBUILD`, `CMAKE`, `MAKE`, `STM32CUBEIDE`, `CSHARP_MSBUILD`, `CSHARP_DOTNET`, `JAVA_MAVEN`, `JAVA_GRADLE`, `JAVA_CLI` driver와 커버리지 어댑터가 사용할 `buildTool`, `adapterEntry`
- 원본 빌드 산출물 기준 디렉터리와 필요한 도구 역할
- 원본 빌드와 커버리지 빌드가 순차적으로 사용할 검증된 동일 프로젝트 빌드 디렉터리. 작업 기록 디렉터리는 로그·계획·증거 전용이며 산출물 경로로 사용하지 않는다. 표준 빌드 경로를 결정할 수 없으면 임의의 대체 경로에 산출물을 만들지 말고 `WAITING_MODEL`/`WAITING_USER`로 중단한다.
- 상세 로그를 남기는 원본 빌드에서 관찰한 최종 산출물 절대 경로와 커버리지 예상 경로를 함께 기록하고 `sameAsOriginal`을 검증한다.
- 호스트 도구와 타깃 도구가 함께 관찰될 때 실제 관찰 후보 중 모델이 선택한 `targetTools`
- 전체 재빌드 명령 또는 `clean`과 `build` 명령을 항목별로 나누어 작성한 사양
- `environmentInitialization`: 원본 환경을 얻은 방법. 허용값은 `INHERITED_PROCESS`, `VSDEVCMD_CAPTURED`, `CUSTOMER_BUILD_SCRIPT`, `SDK_ENVIRONMENT_SCRIPT`, `STRUCTURED_ENVIRONMENT_DELTA`이며, 환경 초기화 스크립트가 필요할 때는 검증된 `scriptPath`만 기록한다.
- CMake/Make에 맞는 `coverageInjection`과 커버리지 빌드 타깃
- CMake 커버리지 configure는 기존 네이티브 캐시를 재사용하지 않도록 정상 빌드 디렉터리의 생성 상태를 초기화한 뒤 실행한다.
- 판단에 사용한 파일이나 증거 경로

네이티브 driver만 `requiredRoles`와 `targetTools`를 사용한다. C#/Java 계획은 두 필드를 빈 값으로 유지하고 각각 `targetFrameworks`, Maven goal·프로필·모듈 또는 Gradle task·속성을 원본 명령과 함께 보존한다. C#/Java를 별도 약식 계획 스키마로 우회하지 않는다.

각 명령은 셸 문자열이 아니라 `executable`, 문자열 배열 `arguments`, `workingDirectory`, 비밀값이 없는 `environmentDelta`, `timeoutSeconds`로 작성한다. `source sdk.sh && make` 같은 원문은 계획에 넣지 말고, 환경 초기화 방식과 비밀값을 제외한 필요한 환경 설정만 별도로 구조화한다.

실행기는 경로 범위, 파일 존재, 자격 증명으로 보이는 인자·근거·환경 변수 이름, 제한 시간과 주입 전략을 검증하고 하나라도 맞지 않으면 실행하지 않는다.

직접 Ninja driver는 지원하지 않는다. 다만 CMake 캐시 또는 소스·프리셋이 소유한 `build.ninja`는 별도 driver가 아니라 CMake generator 산출물이다. 이 경우 계획의 driver는 항상 `CMAKE`이고 generator 값만 원본 구성 정보로 보존한다.

CMake 소유 관계가 없는 Ninja 증거만 발견되면 원래 CMake 진입점 또는 직접 Make 진입점을 요청한다.

CMake 프리셋에서 커버리지 configure에도 보존해야 할 안전한 캐시 인수는 `coverageInjection.configureArguments`, 직접 Make의 clean 타깃은 `coverageInjection.cleanTarget`, 비표준 컴파일러 변수는 `coverageInjection.toolVariables`에 기록한다.

명령행으로 사용자가 명시한 타깃과 configure 인수는 모델 계획보다 우선한다.

MSBuild 계획은 `cleanBuild=true`, 현재 작업 디렉터리 아래 `diagnosticLog`, `MSBUILD_EXECUTABLE_PATH_OVERLAY` 전략을 포함한다. STM32CubeIDE 계획은 실제 headless 실행 파일, 프로젝트, 빌드 구성과 기본 `STM32CUBEIDE_HEADLESS_PATH_PREPEND` 전략을 포함한다.

선택 빌드 구성이 네이티브 도구 실행 명령을 절대 경로로 고정하면 실행 단계가 작업 manifest에 `TEMPORARY_CPROJECT_TOOL_COMMAND_OVERRIDE`를 기록한다. 원본 로그에서 실제 ARM 도구가 관찰되고 커버리지 로그에서 래퍼 호출이 확인되기 전에는 성공 근거가 아니다.

C# 계획은 `CSHARP_TOOLBOX`, Java Maven/Gradle 계획은 `JACOV_PLUGIN`, Java CLI 계획은 `JACOV_CLI` 전략을 사용한다. 사용자가 제공한 managed 계획이 있으면 해당 명령의 goal·task·프로필·속성과 작업 디렉터리를 그대로 검증·실행한다.

단지 표준 프로젝트 파일이 있다는 이유로 새 `dotnet build`, Maven 또는 Gradle 명령을 합성해 사용자 계획을 덮어쓰지 않는다.

## 작성 방법

다음 명령으로 같은 작업에 묶인 `decision.json`을 생성한다.

```text
python "${SKILL_ROOT}/scripts/cover_workflow_runner.py" decision --job-context <job.json> --template <decision.template.json> --owner MODEL --choice 'buildSystem="CMAKE"' --reason "활성 CMakeCache가 프로젝트 루트를 가리킴"
```

`originalBuildPlan`처럼 큰 구조화 값은 현재 작업 디렉터리 아래 JSON 파일에 `{"originalBuildPlan": {...}, "originalBuildCommandSource": "MODEL_INFERRED"}` 형태로 저장하고 `--choices-file <파일>`로 전달한다.

사용자가 제공한 계획은 `originalBuildCommandSource=USER_CONFIRMED_FALLBACK`, `originalBuildPlan.evidenceSource=USER_PROVIDED`으로 기록한다. 셸 인자에 긴 JSON이나 사용자가 준 원문 명령을 직접 넣지 않는다.

저장소·스크립트·로그에도 고객의 실제 선택 근거가 없으면 다음 형태로 사용자에게 넘긴다.

```text
python "${SKILL_ROOT}/scripts/cover_workflow_runner.py" decision --job-context <job.json> --template <decision.template.json> --owner MODEL --escalate-to-user --reason "두 빌드 엔트리가 동일한 증거를 가짐"
```

사용자 답을 기록할 때 `originalBuildCommandSource=USER_CONFIRMED_FALLBACK`을 함께 남긴다. 모델의 파일 내용·관계 해석은 `MODEL_INFERRED`, 스크립트 자동 확정은 `SCRIPT_EXTRACTED`로 구분하되, 이 origin 값은 계획 생성 경로이고 조사서의 근거 출처는 `evidenceSource`다.

결정 파일에는 비밀번호, 세션 토큰, API 키를 절대 넣지 않는다. 실행기는 무결성 해시, `jobId`, 관찰된 후보 범위를 검증하며, 범위 밖의 값을 거부해야 한다.

## 선택 값의 형식과 검증 규칙

단일 선택은 문자열, 여러 항목을 고르는 선택은 배열로 기록한다. 예를 들어 Target Framework가 하나여도 `targetFrameworks`는 배열 형식을 유지한다. 후보가 하나인지와 결과의 자료형은 별개다.

`decision.template.json`의 `choices`는 후보 배열 자체가 아니라 각 항목의 값 형식을 포함한 사양이다. 결정 생성기는 사양 밖의 값과 잘못된 JSON 형식을 즉시 거부한다.

- `ONE_OF`: 후보 중 하나인 비어 있지 않은 문자열 하나. 빌드 시스템, 진입점, 구성처럼 단일 선택에 사용한다.
- `STRING_ARRAY_SUBSET`: 후보의 부분집합인 비어 있지 않은 문자열 배열. `targetFrameworks`, 여러 Visual Studio 프로젝트처럼 다중 선택에 사용한다.
- `STRING_ARRAY`: 후보가 없어서 사용자가 지정해야 하는 비어 있지 않은 문자열 배열. Java task와 기대 산출물에 사용한다.
- `NONEMPTY_STRING`: 후보를 신뢰할 수 없거나 없어도 필요한 비어 있지 않은 문자열 하나에 사용한다.
- `EXACT_VALUE`: 실행 승인처럼 템플릿이 허용하는 값이 하나로 고정된 경우에 사용한다.

후보 목록이 빈 배열이라는 이유만으로 임의의 배열 값이나 빈 값을 허용하지 않는다. `targetFrameworks`의 후보 목록과 선택 결과 배열은 서로 다른 역할이며, 결과는 항상 배열로 기록한다.

이미 생성된 작업의 구형 템플릿은 작업 재개를 위해 읽기 호환으로만 해석한다. 이 경우에도 `targetFrameworks`와 `visualStudioProjects`는 후보 부분집합 배열로, 단일 선택 후보는 문자열 하나로 검증한다. 새 템플릿을 구형 배열 형식으로 다시 만들지 않는다.
