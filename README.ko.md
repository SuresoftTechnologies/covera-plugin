# COVERA — COVER 코드 커버리지를 위한 AI 에이전트

> English: [README.md](README.md)

COVERA(COVER AI Agent)는 Claude Code에서 COVER 코드 커버리지를 사용할 수 있게 해 주는 AI 에이전트입니다. "이 프로젝트 커버리지 빌드해줘"처럼 요청하면 COVERA가 C/C++, C#, Java 프로젝트의 원본 빌드를 파악합니다. CMake, Make, Visual Studio/MSBuild, STM32CubeIDE, C#(COVER Toolbox), Java(JACOV CLI, Maven, Gradle)를 지원합니다. 이어서 COVER 계측 빌드를 수행하고, 계측 산출물이 원본과 같은 경로에 만들어졌는지 확인한 뒤, 검증된 설정을 남겨 두어 이후에도 IDE나 명령행에서 다시 빌드할 수 있게 합니다. 또한 STM32CubeIDE Debug 세션에서 커버리지 메모리 덤프를 수집하고, C/C++ 분석 오류의 원인을 진단해 툴체인 설정 수정안을 제시합니다.

COVER는 시나리오 테스트 수행 결과를 정량적으로 분석하여 테스트 충분성을 검증하는 코드 커버리지 측정 도구입니다.

COVERA가 COVER의 결과를 대신하지는 않습니다. 계측과 측정을 수행하고 커버리지 결과를 만드는 것은 COVER이고, COVERA는 코딩 세션에서 그 작업을 준비하고 실행하며, 결과를 검증해 보고합니다.

## COVER와 COVERA로 할 수 있는 것

| 상황 | COVER와 COVERA가 하는 일 |
|---|---|
| 툴체인별 설정을 몰라도 커버리지 빌드를 하고 싶을 때 | CMake, Make, Visual Studio/MSBuild, STM32CubeIDE, C#(COVER Toolbox), Java(JACOV CLI, Maven, Gradle)의 원본 빌드와 실제 도구를 찾고, 계측 빌드를 수행해 검증합니다. |
| 설정을 다음에도 쓰고 싶을 때 | 검증된 설정을 IDE의 다음 Rebuild에서도 쓸 수 있게 유지하거나, 다시 실행할 수 있는 검증된 명령을 알려 줍니다. |
| 임베디드 테스트의 커버리지를 수집하고 싶을 때 | STM32CubeIDE Debug 세션에서 계측 펌웨어를 테스트한 뒤 커버리지 메모리를 덤프하고, CVD 파일을 검증해 Local Agent에 전달합니다. |
| 분석 오류로 빠진 파일을 복구하고 싶을 때 | 오류 자료를 확보해 원인을 찾고 파서 설정 수정안을 제시합니다. 수정안을 검증하고 반영하려면 이 플러그인에 포함되지 않은 COVER PA가 필요합니다. |

## 사용 흐름

1. 프로젝트의 커버리지 빌드를 요청합니다. 처음 요청할 때는 Claude가 COVER Access Key와 COVER Open API 메인 웹 서버 URL을 묻습니다.
2. Claude가 프로젝트와 환경을 조사하고 원본 빌드를 실행한 뒤, 계측 빌드를 수행하고 산출물을 확인합니다.
3. Claude가 검증된 설정을 적용하고, IDE나 명령행에서 다시 빌드하는 방법을 알려 줍니다.
4. STM32 타깃이라면 STM32CubeIDE에서 테스트를 수행한 뒤, Claude에게 커버리지 데이터 수집을 요청합니다.

이 저장소의 스킬은 설치된 COVER EE Local Agent와 함께 동작하며, COVER를 대신하지 않습니다.

## 설치

claude.ai의 Anthropic 플러그인 디렉터리(**Customize > Plugins**)에서 COVERA를 추가합니다. 추가한 플러그인은 계정 동기화를 통해 Claude Code에서도 사용할 수 있습니다. 설치하지 않고 로컬 사본으로 써 보려면, 저장소를 받은 폴더의 상위 폴더에서 다음 명령을 실행합니다. 이 경우 플러그인은 해당 세션에서만 로드됩니다.

```text
claude --plugin-dir ./covera-plugin
```

COVERA는 Claude Code 전용입니다. 스크립트는 사용자 PC에서 실행되며, 그 PC에 설치된 COVER EE Local Agent와 빌드 도구를 사용합니다. COVER가 설치되어 있지 않거나 설정이 빠져 있으면 스킬은 설정 확인 단계에서 멈추고 무엇이 빠졌는지 알려 줍니다.

## 요청 예시

응답은 AI 모델과 대화 맥락에 따라 달라질 수 있습니다. 원하는 결과가 나오지 않으면 요청을 더 구체적으로 바꿔 다시 시도해 보세요.

| 요청 | 스킬 |
|---|---|
| `이 프로젝트 COVER 커버리지 빌드해줘` | `cover-ee-coverage-build` |
| `이 STM32CubeIDE 프로젝트의 Debug 구성에 커버리지 설정해줘` | `cover-ee-coverage-build` |
| `이 Maven 프로젝트를 JACOV로 커버리지 빌드해줘` | `cover-ee-coverage-build` |
| `STM32CubeIDE에서 테스트 끝났어. 커버리지 데이터 수집해줘` | `cover-ee-coverage-collect` |
| `분석 오류로 커버리지 빌드에서 빠진 파일이 있어. 원인 찾아줘` | `cover-toolchain-recovery` |

## 스킬

모든 스킬은 COVER와 연동되는 작업을 하려면 설치된 COVER EE Local Agent가 필요합니다. Claude가 요청에 맞는 스킬을 고르며, `/covera:cover-ee-coverage-build`처럼 직접 실행할 수도 있습니다.

| 스킬 | 하는 일 |
|---|---|
| `cover-ee-coverage-build` | COVER EE 커버리지 빌드를 준비하고 수행해 검증한 뒤, 검증된 설정을 이후 빌드에 쓸 수 있게 유지합니다. |
| `cover-ee-coverage-collect` | STM32CubeIDE Debug 세션에서 커버리지 메모리 덤프를 수집하고, CVD 파일을 검증해 Local Agent에 전달합니다. |
| `cover-toolchain-recovery` | 분석 오류로 빠진 C/C++ 파일을 진단합니다. 오류 자료를 확보하고 파서 설정 수정안을 제시합니다. 수정안을 검증하는 COVER PA(`tmpa`)는 이 플러그인과 함께 배포되지 않으므로, 기본적으로는 진단과 수정안 제시까지만 합니다(`GuidanceOnly`). 가지고 있는 PA의 경로를 `--pa-root`로 지정하면 복사본에서 수정안을 검증한 뒤 COVER 서버에 등록하고 wrapper를 연결합니다. |

## COVER 연동 방식

COVERA는 COVER EE Local Agent가 설치된 환경에서 사용합니다. 이 저장소에는 스킬과 스킬이 실행하는 스크립트, STM32CubeIDE용 수집 플러그인이 들어 있으며, COVER 자체는 포함되어 있지 않습니다. COVER 없이 계측이나 측정이 수행됐다고 보고하지 않습니다.

작업 중 사용자 PC에서 바뀌는 것은 다음과 같습니다.

- **빌드.** 프로젝트의 빌드 도구로 원본 빌드와 COVER 계측 빌드를 실행합니다.
- **프로젝트 파일.** 검증된 빌드가 끝나면 프로젝트 루트에 `.cover-ee/` 폴더를 만들어 설정과 원본 파일 백업을 보관합니다. Visual Studio, C#, STM32CubeIDE 프로젝트는 이후 빌드에도 커버리지가 적용되도록 프로젝트 설정을 바꾸며, 필요하면 Local Agent 설정도 갱신합니다. CMake, Make, Maven, Gradle 프로젝트의 빌드 파일은 수정하지 않습니다.
- **IDE 플러그인.** 커버리지 수집이 처음 필요할 때 동봉된 플러그인을 STM32CubeIDE에 설치합니다. 설치한 뒤에는 IDE를 재시작해야 합니다.

## 요구 사항

- COVER EE Local Agent 설치 및 라이선스. `CODESCROLL_TM_INSTALL_PATH`가 설치 경로를, `CODESCROLL_TM_SERVER_URL`이 remote service 주소를 가리켜야 합니다.
- COVER Access Key와 COVER Open API 메인 웹 서버 URL(HTTPS)
- `PATH`에서 실행할 수 있는 Python 3.11 이상과 curl
- 프로젝트의 빌드 도구: CMake/Make와 GCC, Visual Studio/MSBuild, STM32CubeIDE, .NET 툴체인, 또는 Java와 Maven/Gradle
- 커버리지 수집에는 Debug 세션이 실행 중인 STM32CubeIDE
- Windows 또는 Linux. Local Agent가 Windows에 설치되어 있으면 WSL에서도 사용할 수 있습니다.

제품 데모, 구매, 도입 상담은 [bizcenter@suresofttech.com](mailto:bizcenter@suresofttech.com)으로 문의하세요.

## 개인정보 처리방침

COVERA의 스크립트는 사용자 PC에서 실행됩니다.

- Suresoft Technologies로 어떤 데이터도 보내지 않으며, 텔레메트리도 수집하지 않습니다.
- 사용자가 알려 준 COVER 서버에만 Access Key로 접속하며, 필요하면 서버에 이 PC의 Local Agent와 툴체인을 등록합니다. 사내 인증서를 쓰는 환경이 많아 기본적으로 인증서를 검증하지 않습니다. 검증이 필요하면 `--verify-tls`를 지정합니다.
- 첫 인증에 성공하면 서버 주소와 Access Key를 `<CODESCROLL_TM_INSTALL_PATH>/agent_kit/open-api-credentials.json`에 평문으로 저장해 다음부터 다시 묻지 않습니다. 저장된 값을 쓰지 않으려면 이 파일을 삭제하세요. Access Key는 로그에 남기지 않습니다.
- Java 프로젝트에서는 `~/cover_jacov.json`이 없으면 새로 만듭니다.
- 파일 경로, 빌드 로그, 커버리지 결과 같은 출력은 Claude 세션에 표시되며, Claude 클라이언트를 통해 AI 모델 제공자에게 전송됩니다.
- COVER가 직접 남기는 데이터는 COVER 제품의 데이터 처리 방침을 따릅니다.

전체 내용은 [PRIVACY.md](PRIVACY.md)를 참고하세요. 지원 및 문의는 [bizcenter@suresofttech.com](mailto:bizcenter@suresofttech.com)으로 연락하세요.

## 라이선스와 상표

이 저장소는 [MIT 라이선스](LICENSE)로 배포됩니다. 라이선스는 문서, 스킬 파일, 스크립트, 동봉된 IDE 수집 플러그인을 포함한 이 저장소의 모든 내용에 적용되며, COVER EE와 Local Agent에는 적용되지 않습니다. COVER와 COVERA는 Suresoft Technologies의 제품이며, 이 저장소는 어떠한 제품 기능이나 제품 라이선스도 제공하지 않습니다.

STM32CubeIDE는 STMicroelectronics의 상표입니다. Claude와 Claude Code는 Anthropic의 상표입니다. 그 밖의 제품명은 각 소유자의 상표일 수 있습니다.
