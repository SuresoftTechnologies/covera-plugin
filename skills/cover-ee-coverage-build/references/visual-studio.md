# Visual Studio / MSBuild

공통 COVER 환경, Agent 등록·실행과 자격 증명 처리는 `SKILL.md`와 `workflow.md`를 따른다. 여기서는 Visual Studio 환경의 판정, 원본 빌드, 커버리지 빌드만 다룬다.

## 판정 기준

솔루션 파일, 빌드 구성, MSBuild 설치본을 순서대로 확인한다. 솔루션 안에 프로젝트가 여러 개 있다는 사실과, 독립적인 빌드 진입점이 여러 개라는 사실은 구분한다.

- `.sln`이 하나이고 그 안에 `.vcxproj`가 여러 개인 경우
  - 정상적인 솔루션 구조로 판정한다.
  - `.vcxproj` 개수만으로 `WAITING_MODEL`을 만들지 않는다.
  - 선택한 솔루션을 빌드 진입점으로 사용한다.
- 독립적인 빌드 진입점 후보가 여러 개인 경우
  - `.sln`이 여러 개 있다.
  - `.sln` 없이 서로 관계가 불명확한 `.vcxproj`가 여러 개 있다.
  - 고객 스크립트나 CI가 서로 다른 솔루션·프로젝트를 선택한다.
  - 이 경우 정형 자동 판정을 중단하고 모델이 프로젝트 간 관계와 스크립트 내용을 해석한다.
  - 저장소에도 선택 근거가 없으면 사용자에게 실제 빌드 대상을 요청한다.
- `.sln`의 `ProjectConfigurationPlatforms` 확인
  - `ActiveCfg`: 선택한 솔루션 `Configuration|Platform`이 프로젝트의 어떤 구성으로 연결되는지 나타낸다.
  - `Build.0`: 해당 솔루션 구성에서 프로젝트가 빌드 대상에 포함되는지 나타낸다.
  - `Build.0`은 빌드 순서가 아니다.
  - 솔루션에 포함된 모든 프로젝트를 무조건 빌드 대상으로 보지 않는다.
- `Configuration|Platform` 판정
  - 솔루션에는 여러 구성 후보가 있을 수 있으므로 파일만 보고 하나가 선택됐다고 판단하지 않는다.
  - 결정 파일에 `configurationPlatform`이 있으면 수집된 후보에 포함되는지 확인한 뒤 사용한다. 재사용 조건을 통과한 환경 프로필에서 불러온 선택값도 같은 방식으로 확인한다.
  - 선택값이 없고 선택한 빌드 진입점의 구성 후보가 하나뿐이면 그 구성을 자동 선택한다.
  - 선택값 없이 여러 후보가 남으면 `WAITING_MODEL`로 넘긴다. 모델은 고객 빌드 스크립트·CI 설정을 우선 확인하고, 프로젝트와 산출물의 관계를 해석한다.
  - 기존 산출물과 프로젝트 기본 구성을 보조 근거로 사용한다.
  - 산출물 경로와 최신 수정 시각만으로 이번 빌드 구성을 확정하지 않는다. 예를 들어 `x64/Debug/app.exe`만 남아 있거나 가장 최근에 생성됐더라도, 사용자가 이번에도 `Debug|x64`를 원한다는 뜻은 아니다.
  - 저장소에도 실제 선택 근거가 없으면 사용자에게 요청한다.
  - 실행 중인 Visual Studio의 DTE/EnvDTE `ActiveConfiguration`은 조회하지 않는다.
  - 구성을 알아내기 위해 Visual Studio를 실행하지 않는다.
  - 후보가 여러 개이면 구성 이름이 `COVER`, `Debug`, `Release`라는 이유만으로 자동 선택하지 않는다.
- COVER 전용 구성이 이미 있는 경우
  - 커버리지 구성과 원본 구성을 같은 것으로 취급하지 않는다.
  - 고객 매핑이 있으면 그 매핑을 사용한다.
  - 매핑이 없으면 빌드 스크립트·CI·기존 원본 진단 로그에서 실제 원본 구성을 확인한다.
  - 그래도 확정할 수 없으면 원본 구성을 임의 선택하지 않고 사용자에게 요청한다.

## MSBuild 선택

- 선택 우선순위
  1. 고객 빌드 스크립트나 CI에 절대 경로로 지정된 `MSBuild.exe`
  2. 사용자가 지정한 `MSBuild.exe`
  3. `vswhere`로 확인한 설치본
- `vswhere` 기본 위치
  - `%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe`
  - 없으면 `%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe`
- 설치본 수집 명령

```text
vswhere.exe -all -products * -requires Microsoft.Component.MSBuild -format json -utf8
```

- 각 설치본에서 확인할 파일
  - `MSBuild\Current\Bin\amd64\MSBuild.exe`
  - 없으면 `MSBuild\Current\Bin\MSBuild.exe`
  - 같은 설치 루트의 `Common7\Tools\VsDevCmd.bat`
- `MSBuild.exe`와 `VsDevCmd.bat`는 같은 Visual Studio 설치본의 파일만 함께 사용한다.
- 후보가 여러 개 남으면 고객 스크립트, 프로젝트 `PlatformToolset`, Windows SDK 요구사항을 모델이 분석한다.
- 그래도 하나로 정할 수 없으면 사용자에게 선택을 요청한다.
- `MSBuild.exe` 버전만으로 실제 `cl.exe` 버전을 확정하지 않는다.

## 원본 빌드

- 빌드 진입점, `Configuration|Platform`, MSBuild가 확정되면 `VsDevCmd` 환경 캡처나 `TargetPath` 평가에 도달하는 첫 실행기 호출부터 도구 호출의 호스트 권한(`require_escalated`)과 PTY를 사용한다.

  MSBuild는 사용자별 확장·SDK 설정 경로도 평가할 수 있으므로 실패 확인용 샌드박스 MSBuild를 먼저 실행하지 않는다. 권한 승인이 거부되면 재시도하지 않고 사용자 대기로 종료하며, 호스트 실행에서도 접근 거부가 나면 일반 ACL·환경 오류로 진단한다.
- COVER Access Key를 이미 받았다면 같은 호스트 실행기 호출에 `--prompt-credentials`를 넣고 표준입력으로만 전달한다. 원본 빌드 뒤 Open API Agent 준비·커버리지 빌드까지 같은 프로세스를 유지하며 비밀값을 명령행이나 파일에 넣지 않는다.
- 고객 빌드 스크립트가 있으면
  - 스크립트가 선택하는 `.sln` 또는 `.vcxproj`, 구성, 플랫폼, MSBuild, 작업 경로를 추출한다.
  - 스크립트를 그대로 실행해야 할 때도 실행 파일과 인자 배열을 계획의 해당 항목에 기록하고, 검증한 뒤 실행한다.
- 고객 빌드 스크립트가 없으면 다음 형태로 명령을 구성한다.

```text
<absolute-MSBuild.exe> <selected.sln-or-vcxproj>
  /nologo
  /p:Configuration=<configuration>
  /p:Platform=<platform>
  /m:<N>
  /t:Rebuild
  /verbosity:diagnostic
```

- 선택한 MSBuild와 짝이 맞는 `VsDevCmd.bat` 환경에서 실행한다.
- COVER 래퍼와 overlay는 적용하지 않는다.
- 원본 빌드 전에 `where cl`, `where link`, `where lib`를 필수 사전 점검으로 실행하지 않는다.
- 원본 빌드가 실패하면 Agent 등록·변경과 커버리지 빌드를 진행하지 않는다.
- 이번 실행의 진단 로그에서 다음 실제 명령을 확인한다.
  - `/c`가 있는 컴파일 명령의 `cl.exe` 또는 호환 컴파일러 절대 경로
  - `/OUT:`이 있는 링크 명령의 `link.exe` 또는 호환 링커 절대 경로
  - 정적 라이브러리를 생성한 경우 `lib.exe` 절대 경로
  - `/c`, `/OUT:`과 입력 파일이 `@response-file` 안에 있으면 검증된 작업 경로에서 RSP를 제한적으로 펼쳐 같은 기준으로 판정한다.
- `PlatformToolset`, `VCToolsVersion`, PATH, `where` 결과는 후보·설명 근거로만 사용한다.
- 실제 명령에 나타난 절대 경로를 최종 툴체인 증거로 사용한다.
- 같은 역할에서 실제 경로가 여러 개 확인되면 하나로 임의 축약하지 않는다.
  - 프로젝트를 툴체인별로 안전하게 분리할 수 있으면 별도 빌드로 처리한다.
  - 분리할 수 없으면 사용자에게 대상 프로젝트를 요청한다.
- 선택한 솔루션 구성에서 `Build.0`으로 활성화된 프로젝트별 `TargetPath`를 MSBuild로 평가한다.
  - 실행 파일 하나만 대표 산출물로 잡지 않는다.
  - 활성 프로젝트의 실행 파일·DLL·정적 라이브러리를 각각 예상 산출물로 기록한다.
  - `ConfigurationType=Utility` 프로젝트는 빌드에는 포함하되 최종 산출물 검증 대상에서는 제외한다.
  - 평가가 동적이거나 산출물 경로가 확정되지 않으면 `--artifact`를 반복 지정하도록 사용자에게 필요한 경로만 요청한다.

## 커버리지 빌드 설정 및 빌드

MSBuild overlay는 기존 프로젝트에 추가로 읽히는 설정 파일이다. 최초 빌드에서는 래퍼 검색 순서를 이 파일로 검증하고, 성공한 설정은 프로젝트에 연결해 IDE에서도 재사용한다. 임시 적용과 성공 후 유지할 연결을 구분한다.


### Agent 툴체인 매핑

- 원본 빌드에서 확인한 실제 경로를 사용한다.
- `CL.exe` → 실제 컴파일러
- `link.exe` → 실제 링커
- 필요한 경우 `lib.exe` → 실제 아카이버
- 진단 로그를 남긴 원본 빌드에서 관찰되지 않은 `link.exe`·`lib.exe`는 설치 디렉터리에서 찾아 자동 추가하지 않는다.
- 기존의 관련 없는 툴체인 설정은 보존한다.
- 서버 Agent DTO와 로컬 `toolchaininfo`를 다시 읽어 반영 결과를 확인한다.

### MSBuild overlay

- 최초 검증은 작업 디렉터리의 `.targets`를 `ForceImportBeforeCppTargets`로 주입한다.
- 성공하면 같은 내용을 안정된 `.cover-ee/config/cover-msbuild.props`로 옮기고 선택한 각 `.vcxproj`의 검증된 `Configuration|Platform`에 `ImportGroup Label="PropertySheets"`로 연결한다. 같은 구성의 기존 `PropertySheets` 그룹이 있으면 그 안에 COVER import를 추가하고, 그룹이 없을 때만 새로 만든다. 별도 그룹을 중복 생성하면 MSBuild 빌드는 성공해도 Visual Studio 속성 관리자에 COVER 속성 시트가 표시되지 않을 수 있다.
- 속성 시트 import는 `Microsoft.Cpp.targets`보다 앞에 두고 내부 `Import`에는 `COVER EE persistent integration` label을 기록한다.
- 같은 label의 기존 import가 있더라도 그대로 통과시키지 않는다. 경로·구성·위치를 다시 검사하고 누락되거나 오래된 대상이면 현재 props를 가리키는 import 하나로 교체한다.
- Visual Studio `속성 관리자`에서 선택 구성 아래의 COVER 속성 시트를 열면 `VC++ 디렉터리 > 실행 파일 디렉터리`에 래퍼가 맨 앞에 적용된 값을 확인할 수 있어야 한다.
- 재사용 props에는 Agent 래퍼 절대 경로를 `CoverWrapperDirectory`로 기록하므로 IDE Rebuild가 별도 MSBuild 인자 없이 같은 `ExecutablePath`를 사용한다.
- 영속 적용 후에는 `/p:ForceImportBeforeCppTargets`와 `/p:CoverWrapperDirectory`를 제거한 같은 Rebuild를 실행한다. 이 빌드 로그에서 필요한 래퍼가 모두 관찰되고 동일 경로 산출물이 새로 생성된 경우에만 `APPLIED`로 기록한다.
- `.sln`은 수정하지 않고 실제 빌드 대상 `.vcxproj`만 연결한다.
- 사용자 전역 `Microsoft.Cpp.*.user.props`는 수정하지 않는다.
- 다음 원본 빌드 전에는 기록된 속성 시트 import를 일시 제거하고 실패 시 되살린다.

```xml
<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup Condition="'$(CoverWrapperDirectory)' != ''">
    <ExecutablePath>$(CoverWrapperDirectory);$(ExecutablePath)</ExecutablePath>
  </PropertyGroup>
</Project>
```

- overlay 적용 인자

```text
/p:ForceImportBeforeCppTargets=<overlay.targets>
/p:CoverWrapperDirectory=<agent-wrapper-directory>
```

- overlay에는 `ExecutablePath` 앞에 래퍼 디렉터리를 추가하는 속성만 둔다.
- 재사용 `.cover-ee/config` 사본은 IDE가 `/p:CoverWrapperDirectory`를 전달하지 않아도 되도록 `CoverWrapperDirectory` 절대 경로를 해당 조건과 `ExecutablePath` 평가보다 먼저 정의한다.
- `CoverPreflight`, `where.exe` 실행, 툴셋 출력용 타깃은 기본 overlay에 넣지 않는다.

### 커버리지 빌드 명령

- 원본과 동일한 MSBuild, VsDevCmd 환경, 솔루션 구성·플랫폼을 사용한다.
- 빌드 전에 예상 산출물의 크기·수정 시각·해시를 저장한다.
- overlay를 적용해 `/t:Rebuild` 한 번 수행한다.
- 진단 로그를 남긴다.
- 고객 절차상 `Rebuild`를 사용할 수 없을 때만 검증된 `Clean` 후 `Build`를 사용한다.

### 래퍼 적용 확인

- 실제 `/t:Rebuild` diagnostic 출력에서 원본 빌드로 관찰한 역할별 `CL.exe`, `link.exe`, 필요한 경우 `lib.exe` 래퍼 호출을 확인한다.
- Agent 로그에서 래퍼 요청과 매핑된 네이티브 도구 실행을 확인한다.
- 원본 빌드에서 관찰한 네이티브 도구 절대 경로가 컴파일·링크·아카이브 명령으로 직접 나타나면 우회로 판정한다.
- 필수 래퍼가 하나라도 실제 빌드 출력에 없으면 실패로 판정한다.
- overlay 적용이나 `where.exe` 결과만으로 성공 처리하지 않는다.
- 일부 프로젝트나 일부 소스만 네이티브 도구를 직접 호출하면 실패로 판정한다.
- 래퍼 선택 문제가 발생하면 해당 실패 로그를 기준으로 `ExecutablePath`, `UseEnv`, 사용자 정의 도구 경로를 진단한다.

### 산출물 경로 확인

- 원본 진단 로그에서 평가·관찰한 `TargetPath`, `OutDir`, `OutputPath`, `BaseIntermediateOutputPath`, `IntermediateOutputPath`와 최종 `.exe`/`.dll`/`.lib` 절대 경로를 보존한다.
- 별칭 솔루션이나 복제한 저장소, 작업마다 달라지는 프로젝트 경로를 만들지 않는다. overlay는 래퍼 탐색 순서만 바꾸고 출력 경로 속성은 건드리지 않는다.
- `${SKILL_ROOT}/scripts/cover_build_executor.py verify-output-paths`로 원본·커버리지 경로를 비교하고 `sameAsOriginal=true`가 아니면 `COVERAGE_OUTPUT_PATH_CHANGED`로 중단한다.

### 성공 판정

- 커버리지 `Rebuild` 종료 코드가 정상이어야 한다.
- 실제 래퍼 호출을 확인한다.
- 활성 프로젝트별 예상 산출물이 이번 커버리지 빌드에서 새로 생성되거나 갱신됐는지 확인한다.
- 모든 최종 산출물의 절대 경로가 원본과 동일해야 한다(`sameAsOriginal=true`).

STATIC 문서의 Workspace/Project Key, Analyze & Upload, `static.vs.props`는 COVER EE 커버리지 빌드에 적용하지 않는다. 참고 범위는 컴파일러 래퍼를 MSBuild 도구 탐색 순서 앞에 두는 방식뿐이다.

## 실행 시간 최적화

- 고객 스크립트·CI가 MSBuild를 지정하면 추가 설치본 선택 없이 그 설치본과 같은 `VsDevCmd.bat`를 사용한다.
- 구성 선택은 위 [판정 기준](#판정-기준)을 따른다. 질문 횟수를 줄이기 위해 기존 산출물의 경로·최신 수정 시각만으로 구성을 선택하지 않는다. 이미 확인한 선택값은 결정 파일과 환경 프로필의 기존 재사용 조건에 따라 이어 쓴다.
- 지정값이 없고 선택 프로젝트의 `PlatformToolset`이 한 세대로 모이면 `v143`→Visual Studio 2022, `v142`→2019처럼 정확히 대응하는 설치 세대를 사용한다. 대응 설치본이 여럿이거나 툴셋이 섞이면 모델 판단으로 남긴다.
- 원본·커버리지 MSBuild에 고객 병렬도 근거가 없으면 CPU 기반 `/m:<N>`(최대 8)을 사용한다. 고객 명령의 `/m` 값이 있으면 보존한다.
- 진단 로그를 남기는 원본 빌드, 실제 도구 추출, 산출물 검증은 한 번의 원본 `Rebuild` 결과를 함께 사용한다. 같은 구성을 툴체인 확인 목적으로 다시 빌드하지 않는다.
- 인증 입력을 보완하거나 예상하지 못한 권한 오류를 해결한 뒤 같은 작업을 재개할 때는 빌드 설정 내용·소스·원본 계획이 같으면 성공한 원본 빌드 체크포인트를 재사용한다. 재사용 import를 동일 내용으로 해제·복원해 바뀐 `.vcxproj` 수정 시각만으로 원본 `Rebuild`를 반복하지 않는다.
