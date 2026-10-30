# 공통 빌드 환경 탐색과 계획

프로젝트 전체를 무차별 수집하지 말고 최초 탐색과 선택 후 탐색을 분리한다. 이 문서는 빌드 시스템별 참조 문서를 읽기 전에 적용한다.

## 1. 최초 탐색

프로젝트 경로를 절대 경로로 정규화한 뒤 다음 항목만 후보로 수집한다.

- Visual Studio 및 C#: `.sln`, `.slnx`, `.vcxproj`, `.vcproj`, `.csproj`
- CMake: `CMakeLists.txt`, `CMakePresets.json`, `CMakeUserPresets.json`
- 직접 Make: `Makefile`, `makefile`, `GNUmakefile`
- STM32CubeIDE: 같은 디렉터리의 `.project`, `.cproject`, 선택적으로 `.ioc`
- Java: `pom.xml`, `build.gradle`, `build.gradle.kts`, `settings.gradle`, `settings.gradle.kts`, Maven/Gradle 래퍼
- 프로젝트 루트의 `build.bat`, `build.cmd`, `build.ps1`, `build.sh`, `configure`, `bootstrap`
- `.github/workflows/*.yml`, `.github/workflows/*.yaml`, 루트 `.gitlab-ci.yml`, `Jenkinsfile`, `azure-pipelines.yml`, `azure-pipelines.yaml`
- C, C++, C#, Java 소스의 확장자별 개수와 경로

임의의 하위 디렉터리에 있는 모든 스크립트, `*.props`, `*.targets`, `*.cmake`, `*.mk`, 캐시와 생성 파일을 최초 단계에서 일괄 수집하지 않는다.

## 2. 선택 후 탐색

빌드 진입점 후보가 정해지면 그 파일이 직접 참조하는 설정을 따라간다. 처음부터 모든 설정 파일을 읽으면 이번 빌드와 관계없는 예제나 다른 타깃의 설정까지 조사하게 되므로, 선택한 빌드에 필요한 범위만 확장한다.

C/C++ 통합 실행기는 이 단계의 결과를 `selected-build-references.json`에 경로·해시와 함께 저장한다. 탐색은 프로젝트 루트 안, 최대 8단계·256개 파일로 제한하고 리터럴 경로만 자동 해석한다. 변수, glob, 조건부 식, 프로젝트 밖 경로는 `unresolved`에 원문과 이유를 남겨 모델이 필요한 참조만 이어서 확인하게 한다.

- MSBuild
  - 선택한 솔루션에 포함된 프로젝트
  - 프로젝트의 `Import`가 참조하는 `.props`, `.targets`
  - 해당 프로젝트에 적용되는 가장 가까운 `Directory.Build.props`, `Directory.Build.targets`
- CMake
  - 선택한 최상위 `CMakeLists.txt`의 `add_subdirectory()`와 `include()` 대상
  - 선택 프리셋의 `include`, 툴체인
  - 선택한 빌드 디렉터리의 `CMakeCache.txt`, `compile_commands.json`
- Make
  - 선택한 Makefile의 `include`, `-include`, `sinclude` 대상
  - 고객 명령의 `-f`/`--file` 대상과 Makefile이 직접 호출하는 스크립트
- STM32CubeIDE
  - 선택한 `.project`에서 프로젝트명을 확인한다.
  - 같은 디렉터리의 `.cproject`에서 빌드 구성명, ST/STM32 식별 문자열과 `arm-none-eabi-*` 도구 명령을 수집한다. 이 명령은 프로젝트에 선언된 설정이며, 실제 사용 도구는 이후 원본 빌드 로그로 확인한다.
  - 같은 디렉터리의 `.ioc`는 존재 여부와 경로를 기록해 STM32CubeIDE 판정 근거로 사용한다. 현재 탐지기는 `.ioc` 내부의 MCU·핀 설정을 해석하지 않는다.
  - 선택한 `.project`, `.cproject`, `.ioc`를 참조 파일 기록에 포함한다. SDK·sysroot·타깃 도구 설정을 추가로 확인할 필요가 있으면, 모델은 선택한 프로젝트나 고객 스크립트에 명시된 관련 참조만 확인한다. SDK 설치 디렉터리 전체를 자동 수집하지 않는다.
- Java
  - Maven `<modules>`와 Gradle `include`/`includeBuild` 대상
  - 선택 래퍼 설정과 직접 참조하는 툴체인 파일
- CI
  - GitHub Actions의 로컬 `uses`, GitLab `include: local`, Azure 로컬 템플릿, Jenkinsfile이 직접 호출하는 프로젝트 내부 스크립트

참조가 동적이거나 조건부여서 스크립트가 안전하게 따라갈 수 없으면 모델 해석 단계로 넘긴다. 저장소에도 실제 참조 대상을 선택할 근거가 없을 때만 사용자에게 요청한다.

## 3. 언어 판정

- 저장소에 소스가 존재한다는 사실만으로 언어를 확정하지 않는다.
- 선택한 솔루션·프로젝트·타깃에 포함되는 소스를 기준으로 후보 언어를 판정한다.
- 테스트, 예제, 외부 라이브러리와 생성 코드는 실제 타깃과 관계를 확인해 분리한다.
- C/C++은 원본 빌드의 실제 C/C++ 컴파일러 호출로 최종 확인한다.
- C#은 선택 프로젝트의 Compile 대상과 원본 C# 컴파일 작업으로 최종 확인한다.
- Java는 선택 소스 묶음과 원본 Java 컴파일 작업으로 최종 확인한다.
- 여러 언어가 섞이면 사용자가 요청한 타깃과 의존 관계를 모델이 해석하고, 범위를 확정할 수 없을 때 사용자에게 대상 프로젝트를 요청한다.

## 4. 빌드 시스템 판정

고객 스크립트와 CI의 실제 실행 명령을 파일 존재보다 우선한다.

- CMake가 생성한 Makefile, Ninja 파일과 Visual Studio 솔루션은 `CMAKE` 내부 빌드 도구로 기록하고 별도 빌드 시스템으로 선택하지 않는다.
- 고객이 관리하는 Makefile을 `make` 계열 도구로 직접 실행할 때만 `MAKE`로 선택한다.
- `.csproj` 존재만으로 `MSBUILD`와 `DOTNET` 중 하나를 선택하지 않는다.
- `pom.xml`이나 Gradle 파일 존재는 후보일 뿐이며 Maven/Gradle 실행 명령으로 확인한다.
- STM32CubeIDE는 같은 디렉터리에 `.project`와 `.cproject`가 있고, `.cproject`의 ST/STM32 또는 `arm-none-eabi-*` 식별 문자열이나 같은 디렉터리의 `.ioc`가 확인되어 `CONFIRMED`일 때만 자동 선택한다. 일반 Eclipse CDT만 있으면 `CANDIDATE`로 남겨 CMake/Make 증거와의 관계를 해석한다. 세부 기준은 [STM32CubeIDE 문서](stm32cubeide.md#판정-기준)를 따른다.
- 직접 Ninja는 지원하지 않는다. CMake 소유 진입점 또는 직접 Make 진입점을 찾는다.

## 5. 원본 빌드 명령 판정

사용자 제공 명령의 우선 검토는 [원본 빌드 명령 문서](original-build-commands.md)를 따른다. 그 다음 프로젝트에서 명령을 찾을 때에는 아래 순서로 확인한다.

1. 고객 빌드 스크립트
2. CI에서 실제 실행하는 명령
3. 선택된 프리셋, 캐시, 프로젝트 설정과 기존 빌드 로그
4. 단일 표준 빌드 규칙
5. 모델이 여러 증거의 내용과 관계를 해석한 결과
6. 저장소·스크립트·로그에 고객의 실제 선택 근거가 없을 때 사용자 입력

스크립트가 있다는 이유만으로 바로 실행하지 말고 다음 항목으로 나누어 작성한다.

- 실행 파일 절대 경로와 문자열 인자 배열
- 작업 디렉터리와 필요한 환경 초기화
- 빌드 구성, 플랫폼, 프레임워크, 프로필, 모듈, 타깃
- clean/rebuild 방식과 예상 산출물
- 진단 또는 verbose 로그 생성 방식

실행 전 계획 검증기는 경로 범위, 실행 파일 존재, 비밀값 부재와 빌드 시스템별 주입 방식을 확인한다. 증거가 부족하면 임의의 Debug, Release, 프레임워크, 타깃, 프로필 또는 SDK를 선택하지 않는다.

## 6. 증거 기록

- 최초 후보에는 절대 경로와 파일 종류만 기록한다.
- 실제 판단에 사용한 파일에는 판단 목적과 해시를 기록한다.
- 환경변수는 이름만 기록하고 값은 저장하지 않는다.
- Access Key, Java key, 저장소 인증 정보와 스크립트의 비밀값을 기록하지 않는다.
- 원본 빌드 성공 뒤 진단·상세 로그의 실제 도구와 산출물을 최종 증거로 사용한다.
