# 결정 파일 및 원본 빌드 계획 작성 규칙

## Java 빌드 중 실행 런타임

원본 빌드 후 `javaBuildRuntime`을 JDK·산출물 등의 결정과 함께 작성한다. 빌드 목표 이름만으로 실행 여부를 추측하지 않고 고객 설정과 원본 로그를 확인한다. 실행이 없으면 다음처럼 기록한다.

```json
{"javaBuildRuntime": {"mode": "NONE", "reason": "선택한 명령은 컴파일만 수행하며 계측 클래스를 실행하지 않는다."}}
```

Maven의 별도 테스트 JVM이 기본 `argLine`을 읽고 기존 값이 비어 있는 경우는 다음과 같다. `value`에는 확인한 기존 값을 그대로 넣는다. 기존 옵션이 있으면 빈 문자열로 바꾸지 않는다. 실제 설정이 이 속성을 소비하는지, 모듈별 옵션을 덮어쓰지 않는지 먼저 확인한다.

```json
{
  "javaBuildRuntime": {
    "mode": "JAVA_AGENT",
    "reason": "원본 package에서 Surefire의 별도 JVM이 테스트를 실행하고 기본 argLine을 사용한다.",
    "javaMajorVersion": 17,
    "maven": {"property": "argLine", "value": "", "forked": true}
  }
}
```

Gradle은 `maven` 대신 `"gradleJvmTasks": [":lib:test", ":app:run"]`처럼 원본에서 실행되는 Test/JavaExec 태스크를 지정한다. 기존 `gradleTestTasks`도 호환된다. 직접 Java·빌드 스크립트는 `"environmentVariable": "TEST_JVM_FLAGS", "value": "-Xmx512m"`처럼 대상 JVM에만 적용되는 고객 변수를 지정한다. 이 이름은 예시이며 고객 스크립트가 실제로 읽는 변수를 사용한다. 원본 실행에서 사용한 Java 버전을 `javaMajorVersion`에 기록한다.

생성 Agent JAR는 설치본의 `java_agent/cover-javalib-agent.jar`를 사용하므로 결정에 임의 JAR 경로를 넣지 않는다. 적용 위치를 확정할 수 없으면 결정을 보완하며 테스트 생략이나 전역 JVM 환경변수로 우회하지 않는다. 배포 후 별도 실행 안내의 `javaRuntimeMode`와는 구분한다.

### 제품 의존성 옵션과 기존 런타임

`mode: LOWER`, `application: PLUGIN_DEPENDENCY`는 Maven·Gradle의 제품 의존성 추가를 선택한다. `javaMajorVersion`, `reason`, `executionJvm`, `classpathEvidence`, `existingRuntime`, `configurationEvidence`, `pluginDependencyEvidence`를 기록한다. 마지막 항목에는 JACOV goal/task의 의존성 추가가 실제 실행 JVM까지 전달되는 근거와 scope·모듈을 적는다. Maven은 `maven.jacov.addDependencyJavaLib=true`, Gradle은 `jacov.addDependencyJavaLib=true`와 검증된 설치본 JAR 부모 경로의 `jacov.javaLibJarDir`를 실행기가 설정한다. 이 방식에는 files가 필요 없다.

`application: EXISTING`은 같은 제품 런타임이 실행 설정에 이미 연결된 경우다. mode와 위의 실행·클래스로더·기존 런타임·설정 근거를 기록하고 새 연결을 추가하지 않는다. JACOV의 자동 의존성 옵션으로 연결되는 기존 LOWER는 PLUGIN_DEPENDENCY를 사용한다. 단순히 JAR가 설치 또는 로컬 저장소에 존재하는 것은 EXISTING의 근거가 아니다.

### 직접 Java 실행 명령

원본 빌드 실행 파일 자체가 java/java.exe이면 `application: JAVA_COMMAND`를 사용할 수 있다. `mode`, `javaMajorVersion`, `reason`, `executionJvm`, `classpathEvidence`, `existingRuntime`을 기록한다. Agent는 JVM 인수 앞에 검증된 `-javaagent`를 추가한다. LOWER는 `configurationEvidence`와 `classpathArgumentIndex`를 추가하며, 이 인덱스는 원본 arguments 배열에서 -cp/-classpath/--class-path 바로 다음 값의 0 기반 인덱스다. 실행기가 기존 classpath에 JAR를 덧붙이고 나머지 명령은 유지한다. -jar의 LOWER 연결, module path, 응답 파일이나 사용자 launcher는 이 방식으로 추측하지 않고 BUILD_FILES 등 실제 적용 지점을 검토한다. 스크립트 내부 java 호출은 이 모드가 아니라 BUILD_FILES다.

### 런타임 연결을 위한 빌드 파일 변경

기존 JVM 옵션 변수로 연결할 수 없거나 LOWER를 클래스패스에 넣어야 하면 `application: BUILD_FILES`를 사용한다. Maven·Gradle·직접 스크립트 모두 지원하며, 모델이 해당 빌드의 실행 JVM과 클래스 로더를 먼저 확인한다. 다음 형식에서 경로·해시·변경 조각은 실제 파일을 읽어 작성한다.

```json
{
  "javaBuildRuntime": {
    "mode": "LOWER",
    "application": "BUILD_FILES",
    "javaMajorVersion": 21,
    "reason": "패키징 중 빌드 도구가 계측 클래스를 로드한다.",
    "executionJvm": "build.bat가 시작하는 빌드 도구 JVM",
    "classpathEvidence": "해당 java 명령의 -cp와 계측 클래스의 로더 경로를 확인했다.",
    "existingRuntime": "해당 JVM에 LOWER와 COVER Java Agent가 없다.",
    "configurationEvidence": "설치 LOWER의 jacoco-agent.properties와 명령의 시스템 속성을 확인했다. 연결·측정 성공은 별도 검증한다.",
    "files": [{
      "path": "build.bat",
      "sha256": "실제 원본 SHA256",
      "replacements": [{"before": "실제 기존 실행 구문", "after": "기존 옵션과 classpath를 유지하고 검증된 LOWER 절대 경로를 추가한 구문"}]
    }]
  }
}
```

`JAVA_AGENT`도 같은 BUILD_FILES 형식으로 대상 java 호출에 생성된 `java_agent/cover-javalib-agent.jar`를 `-javaagent`로 연결할 수 있다. `configurationEvidence`는 LOWER에서 필수다. LOWER 경로는 설치본 `bin/tm-javalib-lower.jar`로 고정하며 임의 대체 JAR을 선택하지 않는다. 기존 옵션 방식은 기존 형식을 유지한다.

실행기는 파일 해시·정확한 before/after·프로젝트 내부 경로·런타임 JAR 참조와 혼용을 검사한다. Java 소스·JAR·작업 기록은 수정할 수 없다. `javaCliIntegration`과 같은 파일을 수정하면 같은 원본 해시를 사용하고 소스 계측 연결 변경 다음에 런타임 변경을 적용하므로 before 조각을 그 순서에 맞춘다. 하나의 백업·복구 단위로 적용하고 성공한 변경만 유지한다. IN_PLACE도 빌드 스크립트 변경과 조합할 수 있으며 Java 원본 소스는 계속 복구한다.

`ProbeStore` 누락으로 실패하면 복구 뒤 `JAVA_BUILD_RUNTIME_REQUIRED` 모델 결정으로 돌아온다. 최신 로그에서 실제 실패 JVM과 클래스 로더를 확인하고 계획을 수정하여 같은 작업을 재개한다. 같은 실패 계획을 그대로 다시 제출하지 않는다. 이 처리로 연결·측정 성공을 선언하지 않는다.

## Java CLI 빌드 파일 변경

`JAVA_CLI` 원본 빌드 후 `javaCliIntegration`이 요청되면 모델이 고객 스크립트와 Ant 등의 빌드 파일을 읽고 다음 객체를 결정에 넣는다. 임의의 셸 구문을 자동 탐색·치환하는 대신, 모델이 실행 흐름을 해석하고 실행기는 정확한 변경 범위와 백업·복구를 검증한다.

```json
{
  "javaCliIntegration": {
    "reason": "소스 생성 target 이후 계측하고 compile target의 입력을 계측 소스로 변경한다.",
    "files": [{
      "path": "build.xml",
      "sha256": "검토한 현재 파일 바이트의 SHA-256",
      "replacements": [{"before": "파일 안에서 정확히 한 번 나타나는 원문", "after": "검토한 변경문"}]
    }]
  }
}
```

`path`는 프로젝트 상대 경로이며 기존 빌드 파일만 허용한다. `before`와 `after`는 줄바꿈을 포함한 실제 문자열이다. 원래 인코딩/BOM을 유지하며 각 `before`는 순서대로 정확히 한 번 일치해야 한다. 변경문에는 확인한 JACOV CLI 절대 경로를 넣고 일회용 job 경로나 인증 key를 넣지 않는다. CLI 오류가 컴파일로 넘어가지 않도록 종료 코드를 전달하고 계측 출력이 실제 컴파일 입력인지 모델이 확인한다. 기존 `clean`이 계측 소스를 삭제하는 순서가 되지 않도록 주의한다.

위 형식은 기본 `SEPARATE_OUTPUT` 방식이며 `mode`를 생략해도 동일하다. 빌드 파일을 미리 고치지 않는다. 실행기가 원본 빌드 성공 후 검증된 변경을 백업·적용한다. `javaCoverageArguments`는 Maven·Gradle 인자에만 사용하며 CLI는 원본 명령을 그대로 재실행한다.

소스 경로가 고정되어 별도 계측 디렉터리를 컴파일 입력으로 연결하기 어려운 경우에만 다음 형식을 사용한다. 두 방식의 필드를 혼합하지 않는다.

```json
{
  "javaCliIntegration": {
    "mode": "IN_PLACE",
    "reason": "원본 빌드 프로그램이 src/main을 직접 컴파일한다.",
    "separateOutputUnavailableReason": "소스 루트 변경 옵션이 없으며 변경하려면 빌드 프로그램의 컴파일 로직 수정이 필요하다.",
    "sourcePreparationComplete": true,
    "sourceRoots": ["src/main"]
  }
}
```

`sourceRoots`는 프로젝트 루트 아래의 서로 겹치지 않는 기존 소스 디렉터리다. `sourcePreparationComplete=true`는 계측 대상 생성이 끝났고 뒤의 빌드가 그 소스를 재생성하지 않는 것을 모델이 확인했다는 뜻이다. 단지 원본 빌드가 한 번 성공했다는 이유로 이 값을 넣지 않는다. 빌드 중 소스가 다시 생성된다면 생성 이후에 계측하는 기본 방식을 사용한다. 실행기는 별도 위치에서 계측한 결과를 원본 위치에 임시 적용하고 원본 명령으로 빌드한 뒤 소스만 복구한다. key나 설정 파일 내용은 결정에 넣지 않으며 검증한 `COVER_JACOV_CONFIG` 경로는 공통 실행 환경으로 전달한다.

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

### 복합 빌드 경로의 모델 결정

- 호스트 플랫폼, 사용자 지정 컴파일러·타깃, 선택한 빌드 진입점을 먼저 기준으로 삼는다. `selected-build-references.json`에 `selectedBuild`가 있으면 후속 계획의 출발점으로 사용한다. 도구가 확인되지 않았으면 계획 실행 전에 확인한다.
- CI·configure·배치 파일은 해당 경로의 준비 단계와 환경을 확인하는 증거다. 파일의 존재만으로 명령 순서를 정하거나, 다른 플랫폼의 CI 경로로 바꾸지 않는다. 선택한 Makefile을 생성하는 단계인지, 별도의 대안 빌드인지 내용을 읽어 구분한다.
- 선택과 증거가 충돌하면 충돌 근거를 남기고 지원되는 결정 절차로 선택을 명시적으로 갱신한다. 계획의 adapterEntry만 바꿔 기존 선택과 불일치한 상태로 진행하지 않는다.
- `decisionOwner: MODEL` 요청에는 작업의 최신 결정 양식으로 계획과 근거를 제출하고 같은 `job.json`을 재개한다. 실행기는 제출된 계획을 검증하며 모델 대신 임의 명령을 만들지 않는다. 후속 모델 결정도 같은 방식으로 처리하고, 실제 명령 실행 전 대기를 빌드 실패로 집계하지 않는다.
- 증거로 경로를 결정할 수 없거나 필수 도구가 없으면 필요한 선택·환경을 보고한다. 동일한 요청에 새 근거나 수정 없이 반복 제출하지 않는다.

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

CMake 원본 옵션은 `commands.configure.arguments`와 해당 명령이 참조하는 프리셋에 보존한다. 캐시에서 원본 명령을 구성할 때에도 이 위치에 기록하며, `coverageInjection.configureArguments`에 중복 기록하지 않는다. 직접 Make의 clean 타깃은 `coverageInjection.cleanTarget`, 비표준 컴파일러 변수는 `coverageInjection.toolVariables`에 기록한다.

명령행으로 사용자가 명시한 타깃은 기존 선택 규칙을 따른다. `--gcc-configure-arg`는 커버리지 전용 보완 인수이며, 명령행으로 전달했다고 실패 근거 확인을 생략하지 않는다.

MSBuild 계획은 `cleanBuild=true`, 현재 작업 디렉터리 아래 `diagnosticLog`, `MSBUILD_EXECUTABLE_PATH_OVERLAY` 전략을 포함한다. STM32CubeIDE 계획은 실제 headless 실행 파일, 프로젝트, 빌드 구성과 기본 `STM32CUBEIDE_HEADLESS_PATH_PREPEND` 전략을 포함한다.

선택 빌드 구성이 네이티브 도구 실행 명령을 절대 경로로 고정하면 실행 단계가 작업 manifest에 `TEMPORARY_CPROJECT_TOOL_COMMAND_OVERRIDE`를 기록한다. 이 기록은 설정 변경 근거이며 빌드 성공 자체를 뜻하지 않는다. 원본 로그에서 실제 ARM 도구를 확인하고 커버리지 빌드 결과는 종료 코드로 판정한다.

C# 계획은 `CSHARP_TOOLBOX`, Java Maven/Gradle 계획은 `JACOV_PLUGIN`, Java CLI 계획은 `JACOV_CLI` 전략을 사용한다. 사용자가 제공한 managed 계획이 있으면 해당 명령의 goal·task·프로필·속성과 작업 디렉터리를 그대로 검증·실행한다.

단지 표준 프로젝트 파일이 있다는 이유로 새 `dotnet build`, Maven 또는 Gradle 명령을 합성해 사용자 계획을 덮어쓰지 않는다.

## 실패 후 추가 옵션 보완

원본 빌드가 성공했다면 먼저 그 명령에 환경별 COVER 설정만 연결한다. 실패할 것이라는 예상만으로 configure 검사 방식을 바꾸거나 테스트를 생략하지 않는다. 기존 명령에 이미 있던 옵션은 이 제한의 대상이 아니다.

CMake configure 또는 Java 커버리지 명령에 추가 보완 옵션이 필요하면 모델이 현재 작업의 실제 실패를 진단한다. CMake 옵션은 `originalBuildPlan.coverageInjection.configureArguments`, Java 옵션은 원본 인수를 보존한 `javaCoverageArguments`에 넣는다. 공통 근거는 결정 파일의 최상위 `coverageOptionRecovery` 객체에 기록한다.

| 필드 | 내용 |
| --- | --- |
| `jobId` | 현재 작업 ID |
| `arguments` | 이번에 추가하는 옵션 배열. CMake는 명령행 보완 인수까지 합친 배열, Java는 JACOV 연결 인수를 제외한 추가 옵션 |
| `log` | 현재 작업의 `logs` 아래에 있는 실패 시도 로그의 절대 경로 |
| `sha256` | 진단한 로그의 SHA-256 |
| `line`, `text` | 원인을 확인한 줄 번호와 해당 줄 원문 |
| `reason` | 확인한 실패 원인과 추가 옵션이 필요한 이유 |

실행기는 로그 옆의 `.metrics.json`에서 실제 종료 코드가 0이 아니고 기록이 완료됐는지 확인한다. CMake는 `configure`, `build`, `rebuild`, Java는 `coverage-java` 단계의 실패를 인정한다. configure가 성공한 뒤 컴파일·링크에서 드러난 문제도 configure 옵션 보완이 필요할 수 있기 때문이다. 시간 초과만으로 보완 옵션을 허용하지 않는다. 로그의 작업 범위, 해시와 인용도 확인한다. 근거가 없거나 맞지 않으면 `BUILD_OPTION_RECOVERY_EVIDENCE_REQUIRED`로 모델 판단을 요청한다. 정상 첫 실행에서 추가 옵션이 없으면 이 검사는 파일을 읽지 않고 통과한다.

실패 기록이 있다는 사실만으로 원인이 입증되지는 않는다. 모델은 컴파일러·SDK 문제, 권한 문제, 네트워크 문제 등을 구분하고 해당 옵션이 필요한 이유를 설명한다. 근거를 맞추기 위해 로그나 실행 결과 파일을 고치지 않는다. 원본 명령의 옵션·타깃·순서를 제거하거나 바꾸지 않으며, Java에서 테스트·모듈 제외 또는 실패 무시 옵션을 새로 넣지 않는다. Java 보완 옵션은 `--max-workers=2`와 `--max-workers 2`처럼 결합형과 분리형 모두 사용할 수 있다. 분리형의 값은 실행기가 인식하는 옵션의 값으로만 처리하며, 새 goal·task를 섞지 않는다.

기존 성공 프로필의 보완 옵션을 재사용할 때는 같은 실패를 다시 만들지 않는다. 실행기는 기존 프로필의 무결성, 프로젝트·도구·요청 일치 검사를 통과한 경우에만 이번 실행에 재사용 정보를 전달한다. 보완 인수 배열까지 동일해야 하며, 이전 로그의 `jobId`를 현재 작업 값으로 바꾸지 않는다. 재개할 때 원본 계획의 작업 ID를 현재 작업에 맞춰 연결한 것은 요청 변경으로 취급하지 않는다. 프로필 검사가 실패하거나 `--no-environment-profile`을 지정한 경우, 또는 보완 인수를 변경한 경우에는 현재 작업의 실패 근거를 요구한다.

`coverageOptionRecovery` 도입 전에 저장한 성공 프로필도 같은 재사용 검사를 거친다. CMake는 저장된 `configureArguments`와 동일한 옵션만, Maven·Gradle은 저장된 `javaCoverageArguments` 전체와 동일한 명령 인수만 재사용한다. 이전 성공 기록을 근거로 새 옵션까지 허용하지 않으며, 이전 프로필에 실패 근거를 만들어 넣지 않는다.

변경 전 코드로 원본 빌드까지 완료한 GCC 작업을 재개할 때는 현재 체크포인트 비교값을 먼저 확인하고, 없으면 이전 형식의 비교값도 확인한다. 이전 자동 계획이 원본 옵션을 `coverageInjection.configureArguments`에도 중복 저장한 경우에는 해당 필드와 계획 무결성 값만 비교에서 제외한다. 원본 명령의 옵션은 제외하지 않는다. 나머지 계획과 소스·구성·참조 파일의 비교값이 기존 성공 기록에 정확히 맞는 경우에만 재사용한다. 이전 이벤트는 수정하지 않고, 재사용 출처를 포함한 새 이벤트를 현재 형식으로 남긴다. 실제 실행 조건이 달라지면 원본 빌드를 다시 수행한다.

하위 실행기의 `plan-cmake`를 직접 호출할 때도 같은 검사를 수행한다. 전역 `--job-context <job.json>`과 `plan-cmake`의 `--option-recovery <recovery.json>`을 함께 지정한다. 이 파일에는 위 표의 `coverageOptionRecovery` 객체 자체를 저장한다. `--configure-arg`는 추가할 인수이고, `--option-recovery`는 그 인수가 필요한 실패 근거다. 근거가 부족하면 traceback 대신 `WAITING_MODEL` 응답을 반환하므로, 같은 작업의 실제 실패 기록을 보완하여 재개한다. 일반 워크플로에서는 기존처럼 결정 파일을 사용한다.

CMake 보완 옵션과 그 근거만 변경된 경우에는 성공한 원본 빌드 체크포인트를 무효화하지 않는다. 원본 명령이나 프로젝트 입력이 바뀌면 기존 무효화 기준을 그대로 적용한다. 성공 후 저장하는 수동 재실행 명령에도 확인된 보완 옵션을 포함한다.

## 작성 방법

다음 명령으로 같은 작업에 묶인 `decision.json`을 생성한다.

```text
python "${SKILL_ROOT}/scripts/cover_workflow_runner.py" decision --job-context <job.json> --template <decision.template.json> --owner MODEL --choice 'buildSystem="CMAKE"' --reason "활성 CMakeCache가 프로젝트 루트를 가리킴"
```

`originalBuildPlan`처럼 큰 구조화 값은 현재 작업 디렉터리 아래 JSON 파일에 `{"originalBuildPlan": {...}, "originalBuildCommandSource": "MODEL_INFERRED"}` 형태로 저장하고 `--choices-file <파일>`로 전달한다.

빌드 환경에 관계없이 `BUILD_OUTPUT_TRACKING_REQUIRED`로 대기하면 `outputTracking`만 제출한다. `directories`와 `files`에는 절대 경로를, `reason`에는 해당 경로를 선택한 근거를 적는다. 이미 선택한 원본 명령과 출처는 다시 제출하지 않는다.

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


## 실행 환경 판단

계획 작성 시 공통 판단 요청의 `executionHost`를 기준으로 한다. 원본 빌드 직전 `EXECUTION_ENVIRONMENT_REVIEW_REQUIRED`는 `MODEL` 소유의 `BUILD_ENVIRONMENT` 요청이며 사용자 승인이 아니다. `temporary/execution-environment-context.json`의 계획과 근거를 확인하여 `choices.executionEnvironmentReview`를 제출한다.

- `jobId`, `contextDigest`: 현재 작업 ID와 context의 integrity.
- `compatible`: 현재 계획을 해당 실행 환경에서 사용할 수 있다고 판단한 경우 true.
- `executionMode`: HOST / WSL / CONTAINER / REMOTE. HOST의 `executionOs`는 context.host.os와 같아야 한다.
- `executionOs`, `shellAssessment`, `toolchainAssessment`, `prerequisitesAssessment`: 실제 실행 OS 및 셸·도구·사전 준비를 확인한 근거.
- `candidateAssessment`, `scopeAssessment`: 후보 선택 근거와 요청한 타깃·구성·옵션의 의미 보존 여부.
- `evidencePaths`: context.files에 기록된 존재하는 근거 파일 경로 배열.
- `launcherAssessment`: HOST 외 환경의 진입 방법과 준비 상태를 설명하며 해당 환경에서 필수.

실행기는 필수 항목, 호스트 OS 일관성, 근거 파일 존재와 현재 계획·호스트·파일 해시 연결을 검사한다. 의미상 호환성은 모델이 판단한다. 계획을 바꿨다면 갱신된 context로 판단을 다시 제출한다. 원본 실패가 환경 선택 문제인지도 로그로 판단하며 실행기가 임의로 다른 명령을 선택하지 않는다.
