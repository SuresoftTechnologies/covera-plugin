# 빌드 기록과 채팅 보고

Visual Studio, GCC/CMake, GCC/Make, STM32CubeIDE, C#, Java에서 같은 형식으로 빌드 선택과 실행 결과를 남긴다. 정보가 기존 단계 JSON에 분산되어 있더라도 작업이 끝나거나 중단될 때 공통 상태 파일인 `workflow-state.json`의 `result.buildSummary`로 모아 기록한다.

별도 요약 파일을 추가하지 않으며, 사전 조사서인 `cover-environment-survey.json`을 대체하거나 사후 정보로 갱신하지 않는다.

## 기록 시점

- 원본 빌드 방법이 확정되면 빌드 시스템과 타깃 부분을 작성한다.
- 실제 커버리지 명령을 실행할 때 명령과 실행 시간을 작성한다.
- 계측 처리와 산출물을 검증한 뒤 결과를 보완한다.
- 성공, 실패, 사용자 입력 대기와 관계없이 현재까지 확정된 내용을 기록한다.
- 재개한 작업은 같은 `jobId`의 파일을 갱신하되 실제로 다시 수행하지 않은 과거 명령을 현재 실행처럼 추가하지 않는다.

## 보고 시 추가 작업 제한

- 요약을 만들기 위해 빌드, configure, dry-run, Agent 조회 또는 모델 판단을 추가로 수행하지 않는다.
- 현재 작업에서 이미 생성된 조사서, 단계 JSON과 최종 결과만 로컬에서 한 번 조합한다.
- 빌드 시스템·타깃·명령·산출물을 단계가 끝날 때마다 별도로 재탐색하지 않고 성공·실패·대기 상태를 기록하기 직전에 한 번 구성한다.
- Access Key는 같은 실행기 프로세스의 메모리에서 재사용한다. 입력과 사용 시점은 [인증 문서](security-and-api.md#access-key-입력과-사용-시점)의 규칙을 따른다. 보고를 위해 키를 파일에 저장하거나 별도 인증을 수행하지 않는다.
- 기존 Agent ID·성공 단계 재사용, CPU 기반 병렬 빌드와 빠른 Agent 준비 판정 정책을 변경하지 않는다.

## 공통 형식

아래 JSON은 사람이 읽는 설명과 실제 실행 기록을 연결하는 기준이다. 명령은 실행 파일·인자·작업 디렉터리를 각각 보관하고, 채팅에 표시할 때만 읽기 좋은 한 줄로 조합한다. JSON 필드 이름은 설명의 한국어 용어와 별개로 유지한다.

```json
{
  "schema": "cover-build-summary/v1",
  "jobId": "<현재 job ID>",
  "status": "SUCCEEDED | FAILED | WAITING_USER | WAITING_MODEL",
  "language": "C | CXX | C_AND_CXX | CSHARP | JAVA",
  "transportSecurity": {
    "configuredServerUrl": "<제품에 설정된 URL>",
    "serverOrigin": "<Open API용 HTTPS 원점>",
    "agentServerOrigin": "<Local Agent용 설정 scheme 유지 원점>",
    "openApiServerUrlSource": "USER_PROVIDED | PERSISTED_CONFIG",
    "serverTransportAdaptation": "HTTPS_MAIN_WEB_SERVER_URL",
    "tlsCertificateVerification": "ENABLED | DISABLED_BY_SKILL_POLICY",
    "localAgentTransport": "PLAINTEXT_HTTP | HTTPS | UNKNOWN",
    "localAgentTlsPolicy": "NOT_APPLICABLE | DEFAULT_JAVA_TLS_VERIFICATION | JOB_SCOPED_PRESENTED_CERTIFICATE_TRUST | UNKNOWN"
  },
  "buildSystem": {
    "id": "VISUAL_STUDIO | CMAKE | MAKE | STM32CUBEIDE | CSHARP_MSBUILD | CSHARP_DOTNET | JAVA_MAVEN | JAVA_GRADLE | JAVA_CLI",
    "evidenceSource": "USER_PROVIDED | SCRIPT | CI | CACHE | PRESET | MODEL",
    "evidencePaths": []
  },
  "target": {
    "entry": "<빌드 진입점 절대 경로>",
    "projectsOrModules": [],
    "buildTargets": [],
    "targetSelection": "EXPLICIT | DEFAULT",
    "buildAction": "<Rebuild, build, package, task 등>",
    "configuration": null,
    "platform": null,
    "targetFrameworks": [],
    "profiles": [],
    "sourceSets": [],
    "targetOs": null
  },
  "coverageBuild": {
    "injectionMethod": "<WRAPPER, TOOLBOX, JACOV 방식>",
    "commands": [
      {
        "phase": "<configure, clean, rebuild, toolbox, instrument 등>",
        "executable": "<실행 파일 절대 경로>",
        "arguments": [],
        "workingDirectory": "<절대 경로>",
        "environmentVariableNames": [],
        "exitCode": null,
        "startedAt": null,
        "finishedAt": null,
        "durationSeconds": null
      }
    ]
  },
  "artifacts": [
    {
      "path": "<산출물 절대 경로>",
      "kind": "EXECUTABLE | STATIC_LIBRARY | SHARED_LIBRARY | ASSEMBLY | JAR | WAR | OTHER",
      "fresh": false,
      "sameAsOriginal": false,
      "size": null,
      "sha256": null
    }
  ],
  "persistentSetup": {
    "state": "APPLIED",
    "path": "<.cover-ee/applied-state.json 절대 경로>",
    "mode": "<빌드 시스템별 영속 적용 방식>",
    "entrypoints": {},
    "manualCommands": [],
    "fileChanges": [
      {
        "path": "<수정·생성·갱신한 파일 절대 경로>",
        "changeType": "MODIFIED | CREATED_OR_UPDATED",
        "purposes": ["<변경 종류>"],
        "backupPaths": []
      }
    ]
  },
  "executionGuidance": {
    "selection": "CHOOSE_ONE_AT_EXECUTION",
    "coverageBuildRuntimeInjected": false,
    "decisionRule": {
      "localAgentLower": "<검증된 Local Agent listener 도달 및 lower 1회 추가 조건>",
      "javaLibAgent": "<생성 agent의 서버 일치, 배포 및 JVM 명령 변경 조건>"
    },
    "localAgentLower": {
      "readiness": "REQUIRES_RUNTIME_VALIDATION",
      "runtimeJar": "<절대 경로>",
      "runtimeJarAvailable": false,
      "mavenCoordinates": [],
      "repositoryAvailabilityDoesNotEnableRuntime": true,
      "classpathSeparator": "; | :",
      "mainClassTemplate": {"kind": "NON_EXECUTABLE_TEMPLATE", "command": "<자리표시자 명령>"},
      "executableJar": {"readiness": "REQUIRES_LAUNCHER_SPECIFIC_CONFIGURATION", "reason": "<이유>"}
    },
    "javaLibAgent": {
      "readiness": "REQUIRES_PREPARATION | REQUIRES_RUNTIME_VALIDATION",
      "agentJar": "<생성본 절대 경로>",
      "agentJarAvailable": false,
      "vmOption": "-javaagent:<생성본>",
      "mainClassTemplate": {"kind": "NON_EXECUTABLE_TEMPLATE", "command": "<자리표시자 명령>"},
      "executableJarTemplate": {"kind": "NON_EXECUTABLE_TEMPLATE", "command": "<자리표시자 명령>"}
    }
  },
  "failure": null,
  "evidenceFiles": []
}
```

- 명령의 기준 정보는 `executable`, `arguments`, `workingDirectory`다. 사람이 읽는 한 줄 명령은 채팅 표시용으로만 조합한다.
- Access Key, Java key와 비밀 환경변수 값은 기록하지 않는다. 환경변수는 비밀값이 없는 이름만 기록한다.
- 명시적인 타깃이 없으면 임의 이름을 만들지 말고 `targetSelection`을 `DEFAULT`, `buildTargets`를 빈 배열로 기록한다.
- 여러 configure/build 명령을 하나로 합치지 않는다. 실제 실행 순서대로 모두 기록한다.
- 실패했다면 실행한 명령과 확인된 산출물을 삭제하지 말고 `failure.stage`, `failure.code`, 비밀값이 없는 `failure.message`를 기록한다.
- `persistentSetup.fileChanges`에는 프로젝트·Agent에 적용한 파일을 먼저 `MODIFIED`로 기록하고, `.cover-ee/applied-state.json`, 수동 명령·entrypoint, 재사용 파일을 `CREATED_OR_UPDATED`로 기록한다. 한 파일이 여러 역할을 가지면 `purposes`와 `backupPaths`를 합치며 모든 경로는 절대 경로로 정규화한다.

## 환경별 타깃 기록

- Visual Studio
  - `entry`: 선택한 `.sln` 또는 `.vcxproj`
  - `projectsOrModules`: 솔루션 구성에서 실제 `Build.0` 대상으로 평가된 프로젝트
  - `buildTargets`: MSBuild target. 기본 커버리지 빌드는 `Rebuild`
  - `configuration`, `platform`: 실제 사용한 솔루션·프로젝트 매핑 값
- GCC/CMake
  - `entry`: 최상위 `CMakeLists.txt`
  - `projectsOrModules`: 선택한 CMake project 또는 subdirectory 타깃
  - `buildTargets`: `cmake --build --target`에 실제 전달한 타깃
  - 타깃 인수가 없으면 `DEFAULT`로 기록하고 빌드 type 또는 multi-config 빌드 구성을 별도로 기록
- GCC/Make
  - `entry`: 실제 Makefile
  - `buildTargets`: make에 전달한 타깃
  - 타깃 인수가 없으면 Makefile 기본 타깃 사용으로 기록
- STM32CubeIDE
  - `buildSystem.id`: `STM32CUBEIDE`
  - `entry`: 선택한 `.project`
  - `projectsOrModules`: Eclipse 프로젝트명
  - `buildTargets`: `<프로젝트명>/<Configuration>`
  - `configuration`: 실제 headless `-cleanBuild`에 전달한 빌드 구성
  - `targetOs`: `STM32`
  - 타깃 실행·디버깅·메모리 수집 명령은 포함하지 않는다
- C#
  - `buildSystem.id`: 실제 진입점에 따라 `CSHARP_MSBUILD` 또는 `CSHARP_DOTNET`
  - `entry`: `.sln`, `.slnx` 또는 `.csproj`
  - `projectsOrModules`: 실제 빌드된 C# 프로젝트
  - `buildTargets`, `configuration`, `platform`, `targetFrameworks`: diagnostic/binlog에서 평가된 값
  - Toolbox 설정 명령과 실제 MSBuild/dotnet 빌드 명령을 별도 phase로 기록
- Java
  - `buildSystem.id`: 실제 실행 근거에 따라 `JAVA_MAVEN`, `JAVA_GRADLE`, `JAVA_CLI`
  - `entry`: root `pom.xml`, Gradle root 또는 CLI 작업 경로
  - `projectsOrModules`: Maven 모듈 또는 Gradle subproject
  - Maven은 goal·프로필, Gradle은 task·소스 묶음, CLI는 instrument/compile/package 대상을 각각 기록
  - JACOV 설정 명령과 실제 빌드 명령을 별도 phase로 기록

## 채팅 표시

[워크플로의 범위와 완료 기준](workflow.md#범위와-완료-기준)에 따라 커버리지 빌드 성공과 스킬 작업 완료를 구분해 보고한다. 계측·산출물 검증 뒤 재사용 설정 적용에 실패했다면, 확인된 빌드 결과를 설명하되 전체 작업이 완료됐다고 하지 않는다. 실패 원인, 복구 결과와 남은 작업을 함께 안내한다.

채팅의 첫 줄에는 다음 형식으로 전체 작업의 판정을 표시한다.

```text
# 최종 결과: 성공 | 실패 | 사용자 입력 대기 | 모델 판단 대기
```

판정 제목 앞에는 인사말, 작업 설명이나 로그 링크를 쓰지 않는다. 제목 바로 아래에는 다음 네 가지를 한 줄씩 표시한다.

- `커버리지 빌드`: 성공·실패·미실행과 확인된 상태 코드
- `재사용 설정`: 적용 완료·적용 실패·미적용
- `대상`: 프로젝트, 빌드 시스템과 선택한 구성
- `다음 조치`: 성공이면 이후 수동 빌드 방법, 실패·대기이면 중단 단계와 필요한 조치

예를 들어 계측 빌드는 성공했지만 IDE 재사용 설정 검증에 실패했다면 다음처럼 보고한다.

```text
# 최종 결과: 실패

- 커버리지 빌드: 성공 (`COVERAGE_BUILD_VERIFIED`)
- 재사용 설정: 실패
- 대상: GPIO_EXTI / STM32CubeIDE / Debug
- 다음 조치: 재사용 설정 실패 원인을 수정한 뒤 적용 검증을 다시 수행해야 함
```

첫 요약은 판정을 빠르게 확인하기 위한 것이며 상세 근거를 대신하지 않는다. 그 아래에는 다음 항목을 같은 순서로 반드시 표시한다. 첫 세 항목은 각각 독립된 제목이어야 하며 다른 항목에 합치거나 생략하지 않는다.

1. **프로젝트 환경 및 빌드 파악**: 판정한 언어·빌드 시스템과 핵심 근거, 확인한 프로젝트·빌드 파일, 진입점, 프로젝트·모듈, 타깃, 구성·플랫폼·프레임워크·프로필, 원본 명령과 작업 경로, 실제 관찰한 도구/JDK
2. **커버리지 빌드 적용**: 실제 실행한 단계별 명령과 작업 경로, 주입 방식, Agent·ToolChainInfo·글루 코드 컴파일러 등 설정, 계측 처리와 설정을 유지하고 재사용하는 방법
3. **파일 변경**: `persistentSetup.fileChanges`를 `MODIFIED`와 `CREATED_OR_UPDATED`로 나눠 절대 경로·목적·백업 경로와 함께 표시. 고객 빌드 파일 수정이 없으면 `고객 빌드 파일 수정 없음`을 명시
4. **산출물**: 절대 경로, 신규 생성·변경 여부, 원본과 동일 경로 여부
5. **다음 수동 빌드 방법**: `.cover-ee/applied-state.json`의 적용 방식, IDE에서 바로 Rebuild 가능한지 또는 어떤 Maven/Gradle/CMake/Make 명령을 실행해야 하는지
6. **Java 실행 방법**: Java 성공일 때만 Local Agent 연동(LOWER)과 JavaLib Agent 두 방법을 모두 표시. 실제 런타임 JAR/agent JAR 경로·존재 여부와 classpath 또는 `-javaagent` 적용 형식을 제공하되 둘 중 하나만 선택한다고 명시
7. **로그**: 원본 빌드, 커버리지 빌드, 적용 검증의 핵심 로그 절대 경로

실패한 작업도 프로젝트 환경 및 빌드 파악, 실제 커버리지 적용 시도와 확인된 복구 결과를 보여주고, 확정하지 못한 값은 `미확정`으로 표시한다. `DEFAULT` 타깃은 “기본 타깃(명시값 없음)”으로 풀어서 쓴다. 비밀값은 채팅에 다시 표시하지 않는다.
