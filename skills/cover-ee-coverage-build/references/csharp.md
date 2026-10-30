# C# / MSBuild

## 1. 환경과 대상 확정

1. `CODESCROLL_TM_SERVER_URL`, `CODESCROLL_TM_INSTALL_PATH`, 프로젝트 절대 경로를 공통 시작 조건으로 확인한다. 최초 요청에 Access Key가 없으면 채팅으로 먼저 받고 메모리에만 보관하되, C# 원본 빌드가 끝난 뒤 Agent·COVER 서버 작업 직전에만 사용한다.
2. `${SKILL_ROOT}/scripts/project_discovery.py managed --project-root <경로>`의 `csharp` 증거와 `.sln`, `.slnx`, `.csproj`, 고객 빌드 스크립트, CI 파일을 읽어 실제 빌드 진입점을 고른다.
3. `.sln`에 `.vcxproj`가 섞였거나 후보 솔루션·구성·플랫폼이 여러 개면 모델이 프로젝트 관계를 판단한다. 모델도 선택할 수 없을 때만 사용자에게 실제 빌드 진입점과 `Configuration|Platform`을 요청한다.
4. 고객의 원본 빌드 명령, 작업 디렉터리, 환경 초기화, 산출물을 정해진 항목으로 나누어 작업별 계획에 기록한다. 임의로 `dotnet build`와 `MSBuild.exe`를 바꾸지 않는다.

### Target Framework 판정

1. `project_discovery.py managed`가 프로젝트별로 수집한 다음 선언을 확인한다.
   - SDK 형식: `TargetFramework`, `TargetFrameworks`
   - 기존 형식: `TargetFrameworkVersion`
   - 적용되는 가장 가까운 `Directory.Build.props`
2. 고객 빌드 스크립트·CI에서 다음 선택을 확인한다.
   - `dotnet build -f/--framework <framework>`
   - MSBuild `/p:TargetFramework=<framework>` 또는 동등한 속성 인자
3. 단일 무조건부 프레임워크면 `AUTO_SINGLE_DECLARATION`으로 원본 계획 후보를 만든다.
4. 하나의 무조건부 `TargetFrameworks` 선언이고 별도 프레임워크 인자가 없으면 기본 빌드 범위를 선언된 프레임워크 전체로 기록한다.
5. 조건부 PropertyGroup, MSBuild 속성 표현식, 서로 다른 props/project 선언, 일부 프레임워크만 선택하는 고객 절차는 모델이 해석한다.
6. 저장소·스크립트·기존 로그에도 일부 프레임워크를 선택한 근거가 없으면 사용자에게 Target Framework를 요청한다.
7. 선언과 명령은 계획 증거일 뿐이다. 원본 진단 로그/binlog에서 프로젝트별로 평가된 `TargetFramework`, 실제 컴파일 작업과 프레임워크별 산출물 경로를 확인해 최종 확정한다.
8. 원본 빌드가 여러 프레임워크를 빌드했다면 커버리지 빌드도 동일한 전체 범위를 유지한다. 임의로 최신 프레임워크 하나만 선택하지 않는다.

### 빌드 방식과 SDK 판정

1. 고객 스크립트·CI의 `MSBuild.exe` 또는 `dotnet build` 명령을 가장 먼저 사용한다.
2. SDK 형식이라는 이유만으로 `dotnet build`를 자동 선택하지 않는다. 두 방식이 모두 가능하면 고객 명령과 기존 빌드 로그를 모델이 해석한다.
3. MSBuild는 고객 지정 경로, 사용자 지정 경로, `vswhere -all` 설치본 순으로 찾고 프로젝트 프레임워크와 필요한 SDK를 평가할 수 있는 설치본만 유지한다.
4. `dotnet`은 프로젝트에 적용되는 `global.json`의 version, rollForward, allowPrerelease와 설치된 SDK를 대조한다.
5. `global.json`이 없으면 Target Framework만으로 SDK를 단정하지 말고 고객 로그와 실제 원본 빌드가 선택한 SDK를 확인한다.
6. 원본과 커버리지 빌드에서 같은 실행 파일, SDK, 빌드 구성, Platform과 Target Framework 범위를 사용한다.

## 2. 원본 빌드와 실제 C# 컴파일러

1. 선택한 프로젝트에서 `CscToolPath`, `CscToolExe`, `OrgCscPath`, `CoverCSharpLibrary`, `.cs_wrapper`를 확인한다. `.sln` 또는 `.slnx`가 진입점이면 솔루션의 C# 프로젝트 전체를, `.csproj`가 진입점이면 해당 프로젝트를 확인한다.

   `.cs_wrapper`를 가리키는 `CscToolPath`, `OrgCscPath`, `CoverCSharpLibrary`, `.cs_wrapper` 중 하나라도 남아 있으면 COVER Toolbox 설정으로 보고 원본 빌드 전에 `cover-toolbox-cli.exe disable <solution-or-project>`을 실행한다.

   `CscToolExe`만 있는 일반 사용자 설정은 COVER 설정으로 단정하지 않는다.
2. `disable` 뒤 선택한 모든 `.csproj`에서 위 설정과 `.cs_wrapper`가 제거됐는지 확인한다. 일부라도 남아 있으면 원본 빌드를 시작하지 않는다.
3. Toolbox 설정이 없는 원본 상태에서 먼저 restore를 수행한 뒤 고객과 같은 clean/rebuild를 수행한다. restore와 빌드는 같은 실행 파일·작업 디렉터리·환경 초기화를 사용하되, restore는 모든 선언 프레임워크의 package assets를 준비하도록 빌드의 임의 `TargetFramework` 제한을 복사하지 않는다.
   - 자동 MSBuild 계획: `MSBuild.exe <sln-or-csproj> /t:Restore /p:Configuration=<구성> /v:minimal` 뒤 `MSBuild.exe <sln-or-csproj> /t:Rebuild ... /v:diag`
   - 자동 dotnet 계획: `dotnet restore <sln-or-csproj> --verbosity minimal` 뒤 `dotnet build <sln-or-csproj> --no-restore --no-incremental --verbosity diagnostic`
   - 고객이 구조화된 restore 명령을 제공하면 이를 우선한다. 이전 작업의 restore 없는 계획은 같은 실행 파일과 환경으로 표준 restore 명령을 보완해 재개한다.
4. SDK 형식 또는 `PackageReference`를 쓰는 선택 프로젝트에서는 restore 종료 코드뿐 아니라 `obj/project.assets.json` 생성도 확인한다. `packages.config` 등 기존 형식 프로젝트는 restore 성공 로그를 증거로 남기되 assets 파일 부재만으로 실패 처리하지 않는다.
5. MSBuild면 현재 작업의 진단 로그 또는 binlog를 새로 만들고, `dotnet`/고객 스크립트면 해당 빌드가 선택한 SDK, 평가된 Target Framework와 컴파일러 실행을 진단 출력에서 수집한다.
6. 실제 C# 컴파일 명령, 응답 파일(RSP) 또는 MSBuild 컴파일 작업에서 `csc.exe`의 절대 경로를 추출한다. SDK가 컴파일러 서버나 프로세스 내부 컴파일러를 사용해 실행 파일이 직접 나타나지 않으면 MSBuild의 `CscToolPath`, SDK/Roslyn 도구 디렉터리와 현재 SDK를 함께 기록하고, 이 원본 빌드 증거의 관계를 모델이 해석해 경로를 확정한다.
   - 관찰 결과는 `PROCESS_EXE`, `DOTNET_CSC_DLL`, `COMPILER_SERVER`, `IN_PROCESS` 중 하나로 구분한다.
   - Toolbox 입력에 필요한 `csc.exe`가 로그에서 하나로 확정되지 않으면 후보와 컴파일러 실행 방식을 근거로 모델이 선택하며, 근거 없이 PATH의 `csc.exe`를 사용하지 않는다.
7. `where csc`, PATH, 기존 `.csproj`의 `OrgCscPath`, Toolbox 기본값만으로 실제 컴파일러를 확정하지 않는다.
8. restore 또는 원본 빌드가 실패하거나 실제 컴파일러를 확정할 수 없으면 Toolbox 설정을 적용하지 않는다.

### 원본 빌드 중 예외 처리

- C# MSBuild 선택은 `managedBuildTool`을 기준으로 유지한다. 공유 Visual Studio 선택 단계가 남긴 `msbuildPath`는 동일한 MSBuild 경로일 때만 호환 입력으로 받아 원본 계획의 `buildTool`로 확정하고, `dotnet` 빌드로 바꾸거나 새 작업을 만들지 않는다. 두 값이 다르면 실패 처리한다.
- `NuGet.Config` 접근 거부는 프로젝트·restore 실패가 아니라 실행 호스트 권한 문제로 기록한다. 권한을 확보한 뒤 같은 작업과 같은 원본 계획으로 재개하며, checkout·출력 경로·빌드 도구를 바꾸지 않는다.
- `CS2012`와 다른 프로세스의 출력 파일 점유가 함께 확인되면 clean/delete 없이 같은 명령을 한 번만 재시도한다. 첫 번째와 재시도 로그를 모두 남기고, 재시도도 실패하면 외부 파일 잠금으로 종료한다.

## 3. Local Agent 준비

1. 공통 하드웨어 ID·서버 Agent 재사용/생성 절차를 따른다. C#에는 C/C++ ToolChainInfo를 임의로 만들지 않는다.
2. Local Agent를 시작하고 다음을 모두 확인한다.
   - 서버 Agent `online=true`
   - C# 빌드 서비스 포트 `CODESCROLL_TM_CS_PORT`(기본 9092) 준비
   - 측정 실행까지 요청됐으면 C# 런타임 포트 `CODESCROLL_TM_CS_EXECUTION_PORT`(기본 9093) 준비
   - 상태 보고 서비스 초기화와 Agent 로그의 오류 부재
3. 포트만 열렸다는 이유로 준비 완료라고 판단하지 않는다.

## 4. COVER Toolbox CLI 설정

Toolbox는 프로젝트가 CscHooker를 거쳐 원본 컴파일러를 호출하도록 설정한다. 따라서 Toolbox 설정 성공과 실제 커버리지 빌드 성공은 각각 확인한다.

고객용 진입점으로 `${CODESCROLL_TM_INSTALL_PATH}/bin/cover-toolbox/cover-toolbox-cli.exe`만 사용한다. `CoverCSharpManager-cli.exe`는 Toolbox 내부 구현이므로 직접 호출하지 않는다.

```text
cover-toolbox-cli.exe list <solution-or-project>
cover-toolbox-cli.exe enable <solution-or-project>
cover-toolbox-cli.exe disable <solution-or-project>
```

### 설정 파일별 변경 범위

`csharp-config.json`은 Toolbox 설치본이 함께 사용하는 C# 설정 파일이다. Toolbox는 이 파일의 컴파일러 경로를 읽어 프로젝트에 래퍼를 설정한다. 기존에 저장된 경로가 이번 원본 빌드의 컴파일러와 다를 수 있으므로, 실행기가 확인된 경로를 작업 중에만 반영한다.

- 실행기는 `csharp-config.json`을 백업한 뒤 원본 빌드에서 확인한 컴파일러를 반영하고, 작업이 끝나면 원본으로 복구한다. 복구 전에 다른 변경이 감지되면 덮어쓰지 않는다.
- AI Agent는 컴파일러 경로나 계측 옵션을 추측해 이 파일을 직접 수정하지 않는다. 실행기의 백업·검증·복구 절차를 거치는 임시 변경은 이 금지에 포함하지 않는다.
- 고객 `.csproj`의 커버리지 설정 적용과 해제는 Toolbox CLI로만 수행한다. AI Agent나 일반 파일 복원기가 `.csproj`를 직접 고쳐 Toolbox 처리를 대신하지 않는다.

### 적용과 복구 절차

1. `disable`과 `enable` 전후에 대상 `.csproj`, 존재하는 `*_backup`, `.cs_wrapper`의 유무·크기·수정 시각·해시를 작업 증거로 기록한다. 변경 전후 파일 상태는 복구 수단이 아니라 Toolbox 동작과 동시 변경을 확인하는 증거다.
2. 실행기는 `${CODESCROLL_TM_INSTALL_PATH}/bin/cover-toolbox/csharp-config.json`의 원본 바이트를 작업 임시 경로에 백업하고 SHA-256·수정 시각을 기록한다. 백업을 만든 뒤 원본 진단 로그에서 관찰한 실제 `csc.exe`를 `vsVersion.cscPath`에 작업 중에만 반영한다. Visual Studio 설치 경로에서 확인할 수 있으면 `vsVersion.version`도 `vs2022`처럼 맞추고, 알 수 없으면 `custom`으로 둔다.
3. 기존 `csharpConfig` 옵션과 다른 전역 필드는 보존한다. 복구할 때는 현재 파일이 이번 작업에서 적용한 상태와 일치하는지 확인하고, 일치할 때만 원본 바이트로 되돌린다. 외부 변경이 감지되면 덮어쓰지 않고 실패로 기록한다.
4. C# `enable`에는 세 번째 `wrapper-info` 인자를 주지 않는다. 현재 Toolbox CLI는 그 인자를 JSON으로 읽으며, C# 계측에는 사용하지 않는다. C/C++의 래퍼 설정 파일이나 Agent 래퍼 경로를 C# Toolbox 입력으로 변환하지 않는다.
5. C# Toolbox 처리는 Windows에서만 지원한다. enable/disable만 사용자에게 보이지 않는 네이티브 Windows 콘솔에서 직접 실행하고, Python `stdout=PIPE`나 AI Agent 통합 터미널의 UTF-8 스트림을 상속하지 않는다.

   실행 파일·인자·작업 디렉터리는 구조화된 형태로 그대로 전달하며, CMake·Make·Java 등 다른 환경에는 이 Windows 전용 실행 방식을 적용하지 않는다.
6. `enable`의 종료 코드만 보지 말고 선택한 모든 `.csproj`에 `CscToolPath`, `CscToolExe`, `OrgCscPath`, `.cs_wrapper/*.cmd`가 생성됐으며 각 명령 파일이 이번 원본 `csc.exe`를 가리키는지 프로젝트별로 확인한다. CLI가 오류를 출력하면서 종료 코드 0으로 끝나는 경우도 이 검증에서 실패 처리한다.
   - 생성된 `.cs_wrapper`의 `.cmd`/`.bat` 파일 첫 3바이트가 UTF-8 BOM(`EF-BB-BF`)이면 `CSHARP_TOOLBOX_WRAPPER_BOM`으로 중단한다. 네이티브 Windows 콘솔 실행으로 한 번 전환한 뒤에도 BOM이면 같은 방식으로 재시도하지 않는다.
   - BOM을 임의로 제거하거나 고객 프로젝트 파일을 수동 수정하지 않는다. 현재 Toolbox CLI에는 래퍼 인코딩 지정 옵션이 없으므로, 공통 `CsprojModifier.MakeCmdFile()`가 `Console.OutputEncoding` 대신 BOM 없는 명시적 batch 인코딩을 사용하도록 제품을 수정·배포한 뒤 재시도한다.
7. 기존 설정 여부와 관계없이 원본 빌드는 `disable`된 상태에서 수행한다. 원본 빌드에서 확인한 `csc.exe`를 위 절차로 준비한 뒤 Toolbox `enable`로 프로젝트에 적용한다.
8. `enable` 도중 또는 이후 커버리지 빌드가 실패·중단되면 `cover-toolbox-cli.exe disable <solution-or-project>`로 원본 빌드 설정을 복구하고 제거 결과를 확인한다. 성공하면 Toolbox가 만든 `.csproj`·`.cs_wrapper` 설정을 유지하여 Visual Studio의 다음 Rebuild에 사용한다.

   성공·실패와 관계없이 Toolbox 전역 C# 설정 파일은 원본으로 복구한다. 다음 스킬 실행의 원본 빌드 직전에만 Toolbox `disable`을 다시 수행하며, 일반 파일 복원기로 `.csproj`만 되돌리지 않는다.

## 5. 설정 결과 검증

각 선택 `.csproj`에서 Toolbox가 만든 다음 항목을 확인한다.

- `<CscToolPath>$(MSBuildProjectDirectory)\.cs_wrapper</CscToolPath>`
- GUID 기반 `<CscToolExe>...cmd</CscToolExe>`
- `CoverCSharpLibrary.dll` 참조와 설치 루트 아래 HintPath
- `ProjectExtensions/Cover/OrgCscPath`가 이번 원본 `csc.exe`와 일치
- 프로젝트별 `.cs_wrapper` 명령 파일
- 원본 `*.csproj_backup`

고객 `.csproj`의 커버리지 설정 변경과 복구는 Toolbox CLI만 수행한다. 실패하면 `disable`로 원본 빌드 설정을 복구한다. Toolbox 공통 설정인 `csharp-config.json`은 커버리지 빌드 성공 여부와 관계없이 실행기가 복구하며, [적용과 복구 절차](#적용과-복구-절차)의 외부 변경 확인 규칙을 따른다.

## 6. 커버리지 빌드와 검증

1. Toolbox 설정 후 원본과 같은 작업 디렉터리·환경·구성·Target Framework 범위로 clean/build를 수행한다. COVER Toolbox의 래퍼 설정은 package restore 입력을 추가하지 않으므로 원본 restore 결과를 재사용하며 매번 restore를 반복하지 않는다.

   원본 `.sln`/`.csproj`의 절대 경로를 그대로 사용하고, 솔루션을 `D:\cover-csharp-*` 같은 별칭 경로로 복제하거나 `MSBuildProjectDirectory`가 달라지는 작업별 복제 저장소를 만들지 않는다.
2. MSBuild 진단 로그, `${CODESCROLL_TM_INSTALL_PATH}/logs/CoverVcxTagger.log`, `${CODESCROLL_TM_INSTALL_PATH}/logs/csharp`에서 다음을 확인한다.
   - `.cs_wrapper/<guid>.cmd` 실행
   - `QualityScroll.Cover.CscHooker.exe` 실행
   - `OrgCscPath`의 실제 `csc.exe` 실행
   - CscHooker의 실제 계측 처리
   - 선택 프로젝트마다 해당 `.cs_wrapper/<guid>.cmd` 호출과 `OrgCscPath`의 원본 `csc.exe` 설정이 확인된다.
   - `includedfile: 0` 또는 `excludedfile` 자체는 실패로 처리하지 않는다. 제외 사유와 별개로 CscHooker 호출, 원본 컴파일 성공, 동일 경로 산출물 갱신이 확인되면 커버리지 빌드는 성공으로 판정한다.
3. 원본 `csc.exe`가 CscHooker를 우회해 직접 실행된 프로젝트가 있으면 실패로 처리한다.
4. 원본 진단 로그에서 관찰한 어셈블리의 절대 경로를 `originalOutputPaths`로 기록하고, `BaseOutputPath`, `OutputPath`, `OutDir`, `IntermediateOutputPath` 및 Target Framework별 `TargetPath`를 커버리지 빌드에서도 그대로 유지한다.

   Toolbox 설정은 컴파일러 래퍼만 바꿔야 하며 출력 경로를 바꾸는 속성을 추가하지 않는다.
5. `${SKILL_ROOT}/scripts/cover_build_executor.py verify-output-paths --original-artifact <원본 경로> --coverage-artifact <커버리지 경로>`를 사용해 경로를 비교한다. `sameAsOriginal=true`가 아니면 `COVERAGE_OUTPUT_PATH_CHANGED`로 중단한다.

   비교 결과(`originalOutputPaths`, `coverageOutputPaths`, `missingOriginalPaths`, `unexpectedCoveragePaths`)를 커버리지 단계 JSON에 남긴다.

## 7. 측정 실행

사용자가 실행 또는 측정 데이터 확인까지 요청했을 때만 검증된 실행/테스트 명령을 수행한다. 프로세스 실행 후 C# 런타임 연결, 해당 Module의 새 측정 시각과 커버리지 변경을 확인한다. 테스트 명령이나 배포 대상을 추측하지 않는다.

## 성공 판정

다음이 모두 참일 때 C# 커버리지 빌드 성공으로 판정한다.

- 원본 빌드와 실제 `csc.exe`를 확인했다.
- Toolbox CLI가 선택한 모든 `.csproj`에 설정을 적용했다.
- CscHooker가 실제 컴파일을 처리하고 원본 `csc.exe`를 호출했다.
- 어셈블리 산출물이 이번 작업에서 새로 생성 또는 변경됐다.
- 어셈블리 산출물의 절대 경로가 원본과 완전히 같다(`sameAsOriginal=true`).

측정 데이터 성공은 별도 판정으로 두고 실제 실행과 데이터 갱신까지 확인한다.

## 실행 시간 최적화

- 이전 성공 작업의 환경 프로필이 현재 빌드 설정 파일·요청 조건·도구 식별 정보와 일치하면 솔루션·프로젝트, 빌드 방식, 빌드 구성, Target Framework와 구조화된 원본 명령을 재사용한다. 현재 작업의 진단 로그를 남기는 원본 빌드와 실제 `csc.exe` 확인은 생략하지 않는다.
- C#/Java 프로젝트 조사에서 솔루션·프로젝트, Target Framework 후보, `global.json`, 고객 빌드 명령을 한 번에 수집하고 한 번의 계획으로 확정한다.
- 원본 진단 로그/binlog 한 번에서 평가된 Target Framework, 실제 `csc.exe`, 어셈블리 출력 경로를 함께 추출한다. 프레임워크 확인과 컴파일러 확인을 위해 원본 빌드를 반복하지 않는다.
- 고객 MSBuild 병렬 설정이 없으면 CPU 기반 `/m:<N>`(최대 8)을 사용한다. 고객 명령의 명시값은 보존한다.
- Access Key와 현재 Agent의 macAddress 조회 결과, C# 빌드 서비스 준비 결과는 같은 실행기 프로세스와 작업 안에서 재사용한다. Toolbox 적용 전후에 Agent 전체 목록을 조회하지 않는다.
- Toolbox enable 결과와 CscHooker 실제 처리 로그는 해당 작업의 단계 JSON에 한 번 기록하고 후속 산출물 검증에서 재사용한다.
