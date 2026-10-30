# 인증과 서버 API 사용 규칙

## 환경 값

- 제품에 설정된 Local Agent의 remote-service URL: `CODESCROLL_TM_SERVER_URL` (`--server-url`로 명시 가능)
- 사용자가 알려주는(또는 캐시 파일에서 읽는) COVER Open API 메인 웹 서버 URL: `--open-api-server-url`
- Local Agent 설치 루트: `CODESCROLL_TM_INSTALL_PATH`
- Open API 접속 정보 캐시 파일: `${CODESCROLL_TM_INSTALL_PATH}/agent_kit/open-api-credentials.json`
- 실행 작업의 Open API 서버 원점: `--open-api-server-url`로 받았거나 캐시 파일에서 읽은 메인 웹 서버 URL을 정규화한 HTTPS 주소
- 실행 작업의 Local Agent 서버 원점: `CODESCROLL_TM_SERVER_URL`의 scheme·host·port·path를 유지하되 아래 WSL HTTP loopback 규칙을 적용한 주소
- Open API 기준 경로: `<실행 작업의 COVER 서버 원점>/cover-open-api`
- 메모리 전용 환경변수: `COVER_ACCESS_KEY`

여기서 서버 원점은 프로토콜, 호스트, 포트와 선택적인 기준 path를 뜻한다. `CODESCROLL_TM_SERVER_URL`은 Local Agent가 `tm-remote-service`에 연결할 주소이므로 Open API 주소와 같은 host·port라고 가정하지 않는다. Open API 서버 원점은 사용자가 알려준(또는 캐시 파일의) 메인 웹 서버 URL로 만들고, Local Agent는 환경변수의 scheme·host·port·path를 그대로 사용한다. 아래 WSL HTTP loopback 예외 외에는 Local Agent 원점의 host를 바꾸지 않으며 환경변수 자체도 수정하지 않는다.

메인 웹 서버 URL은 사용자 입력이나 유효한 접속 정보 캐시에서만 받는다. `CODESCROLL_TM_SERVER_URL`이 있어도 이를 사용하거나 scheme·port·path를 바꿔 추측하지 않는다. URL이 없으면 서버 연결 전에 `OPEN_API_MAIN_WEB_SERVER_URL_REQUIRED`로 중단하고 실행기는 `WAITING_USER`(종료 코드 3)를 반환한다. 사용자에게 메인 웹 서버 URL을 요청한 뒤 `--open-api-server-url`로 새 작업을 시작한다. 형식이 잘못되면 `INVALID_SERVER_URL`, HTTPS가 아니면 `INVALID_OPEN_API_MAIN_WEB_SERVER_URL`로 중단한다. 알려진 host나 포트를 추측하거나 차례로 접속해 찾지 않는다. REST 요청은 정규화한 Open API 원점의 `/cover-open-api/v1/...`에만 보내며 다른 내부 REST API로 우회하지 않는다.

작업에는 사용자가 설정한 `configuredServerUrl`, Open API에 사용하는 `serverOrigin`, Local Agent에 사용하는 `agentServerOrigin`, 입력받은 그대로의 `configuredOpenApiServerUrl`과 `openApiServerUrlSource`(`USER_PROVIDED` 또는 `PERSISTED_CONFIG`)를 구분해 기록한다. `serverOrigin`은 항상 `https://<host>:<port>` 형식이고 `serverBaseUrl`은 여기에 `/cover-open-api`를 붙인 값이다. 기존 작업을 재개할 때는 보호된 원점을 다시 계산하거나 바꾸지 않는다. 이전 형식의 작업이거나 Open API URL을 바꾸어야 하면 새 작업을 생성한다.

### Open API 접속 정보 캐시

`${CODESCROLL_TM_INSTALL_PATH}/agent_kit/open-api-credentials.json`이 있고 `openApiServerUrl`·`accessKey`가 모두 비어 있지 않으면 그 값을 사용하고 사용자에게 다시 묻지 않는다. 파일이 없으면 [Access Key 입력과 사용 시점](#access-key-입력과-사용-시점)에 따라 채팅으로 요청한 뒤, 원본 빌드 후 첫 서버 인증이 성공한 시점에 두 값을 이 파일에 저장해 다음 실행부터 재사용한다. 파일은 한 번만 생성하며 이후 실행에서 자동으로 덮어쓰지 않는다. 기존 파일이 잘못된 JSON이거나 두 필드가 비어 있으면 `OPEN_API_CREDENTIALS_INVALID`로 중단하며 덮어쓰지 않는다. 쓰기에 실패하면 `OPEN_API_CREDENTIALS_WRITE_FAILED`로 보고하되 커버리지 빌드 자체를 실패시키지 않는다(캐시는 이미 성공한 인증 위의 부가 기능이다). Access Key를 이 파일에 저장하는 것은 일반적인 비저장 정책의 명시적 예외이며, 그 범위와 권한은 [Access Key 입력과 사용 시점](#access-key-입력과-사용-시점)에서 정의한다.

### Java JACOV 서버 주소

Java 계측은 `COVER_JACOV_CONFIG` 또는 실행 계정의 `${user.home}/cover_jacov.json`에 기록된 `url`로 `tm-remote-service`에 연결한다. 이 주소는 사용자가 제공한(또는 캐시 파일의) Open API 원점이나 `CODESCROLL_TM_SERVER_URL`의 remote-service 주소와 다를 수 있다. protocol·host·port가 다르다는 이유만으로 오류 처리하거나 한쪽 주소를 다른 쪽에 맞춰 바꾸지 않는다.

명시한 설정 경로가 없고 홈 파일도 없으면 [Java 설정 생성 절차](java.md)에 따라 현재 PC의 Agent ID와 `CODESCROLL_TM_SERVER_URL`로 홈 파일을 생성한다. 이 경우에만 Java 빌드가 Open API Agent 목록·상세 조회를 수행한다. 기존 파일은 그대로 유지하며, 생성용 조회 응답은 임시 응답 파일에도 저장하지 않는다. 홈의 제품 설정 파일은 Java key 비기록 규칙의 명시적인 예외이고 작업 기록·로그·명령 인수에는 예외를 적용하지 않는다.

Open API의 `insecureSkipTlsVerify`와 HTTPS Local Agent의 작업용 truststore는 COVER 제어 통신에만 사용한다. Maven·Gradle·직접 Java JACOV 프로세스에는 전달하지 않는다. JACOV는 제품 설정 파일의 주소와 해당 Java 프로세스의 기본 TLS 정책을 사용한다.

### HTTPS 인증서 검증 정책

Open API는 `curl`로 호출한다. 사내 CA가 없는 COVER 배포에서도 작업을 계속할 수 있도록 기본 실행에는 `-k`를 붙여 인증서 체인과 hostname을 검사하지 않는다. Access Key는 curl 명령 인수에 넣지 않고 표준입력으로 전달한다. 엄격한 TLS 검증이 필요한 작업은 새 작업을 `cover_workflow_runner.py --verify-tls`로 생성한다.

선택한 정책은 무결성 보호된 작업의 서버 원점에 고정하며 기존 작업을 재개하면서 바꾸지 않는다. Open API의 검증 생략은 중간자 공격이 가능한 네트워크에서 Access Key와 응답을 노출·변조시킬 수 있다.

### Local Agent에 적용하는 인증서 설정

C/C++·STM32CubeIDE·C# Local Agent는 `agentServerOrigin`을 사용한다. 이 원점이 HTTP이면 인증서 조회, TLS handshake와 truststore 생성을 하지 않고 평문 HTTP로 통신한다. 따라서 HTTP 포트에 HTTPS handshake를 시도해서 발생하는 `WRONG_VERSION_NUMBER`도 만들지 않는다. 제품 설정의 명시적인 HTTP scheme을 Agent 전송 선택으로 취급하며 별도의 TLS 검증 생략 옵션은 요구하지 않는다. HTTP는 전송 암호화와 서버 인증을 제공하지 않으므로 설정된 신뢰 경계 안에서만 사용해야 한다.

Agent 원점이 HTTPS이면 Local Agent는 별도 Java 프로세스이므로 curl의 `-k` 설정을 물려받지 않는다. 작업에서 처음 제시된 미검증 서버 인증서는 `temporary/agent-tls` truststore에 저장해 같은 작업 동안만 재사용한다. 이 설정은 자체 서명 인증서의 신뢰 문제만 해결하며 hostname 검사를 끄지는 않는다.

인증서에 `agentServerOrigin`의 host가 포함되지 않으면 Agent 시작 로그에서 오류를 확인한 즉시 `AGENT_TLS_HOSTNAME_MISMATCH`로 중단한다. 이 경우 인증서 SAN에 실제 접속 주소를 넣거나 인증서에 포함된 서버 이름을 사용해야 한다. 스킬에 별도 Java 보조 프로그램을 포함하거나, 더 높은 Java 버전의 작업용 JRE로 Agent 설치본을 대체하지 않는다.

처음 제시된 인증서를 신뢰하는 것은 검증된 certificate pin이 아니다. 중간자 공격이 가능한 네트워크에서는 다른 인증서가 작업에 저장될 수 있으므로 적용 사실과 인증서 지문을 최종 보고에 남긴다.

### 적용 제한과 결과 기록

시스템 truststore·hosts, Agent 설치본의 `agent-config.yaml`, 운영체제 전역 TLS 설정은 바꾸지 않는다. Windows service 모드는 작업별 JVM 인수를 전달할 수 없으므로 이 모드와 함께 사용하지 않는다. 다른 원점이나 `--ca-file`과 함께 사용하지 않는다.

`workflow-state.json.result.buildSummary.transportSecurity`와 최종 보고에 Open API 검증 정책, `agentServerOrigin`, Local Agent의 HTTP 또는 HTTPS 전송 방식과 HTTPS일 때의 인증서 신뢰 방식을 명시한다.

### WSL에서 Windows 서버에 연결하는 경우

WSL에서 Windows의 `CODESCROLL_TM_SERVER_URL`이 HTTP loopback 주소이고 WSL loopback 접속은 실패하지만 기본 host gateway의 같은 포트는 접속되면, 현재 작업의 `agentServerOrigin` host만 gateway 주소로 변환하고 `serverNetworkAdaptation=WSL_HOST_GATEWAY`로 기록한다. Open API의 `serverOrigin`은 사용자가 알려준(또는 캐시 파일의) URL을 그대로 정규화한 값이므로 이 변환의 영향을 받지 않는다.

Windows 환경변수나 시스템 네트워크 설정은 바꾸지 않는다. HTTPS loopback은 인증서 이름이 달라질 수 있으므로 IP로 자동 변환하지 말고 WSL에서 유효한 서버 hostname 또는 네트워크 설정을 요청한다.

## 현재 인증

모든 REST 요청은 COVER Access Key를 `Authorization: Bearer <ACCESS_KEY>`로 전달한다. 키를 사용자에게 받는 시점과 서버 인증에 사용하는 시점은 구분한다.

### Access Key 입력과 사용 시점

1. AI Agent는 먼저 `${CODESCROLL_TM_INSTALL_PATH}/agent_kit/open-api-credentials.json`을 확인한다. 유효한 `openApiServerUrl`·`accessKey`가 있으면 그 값을 사용하고 채팅으로 다시 묻지 않는다. 파일이 없거나 유효하지 않으면, 최초 요청에 Access Key나 Open API 메인 웹 서버 URL이 없을 때 누락된 프로젝트 경로와 함께 채팅으로 한 번 요청하고 입력을 기다린다. URL 안내 문구는 “COVER Open API가 제공되는 메인 웹 서버 URL을 알려주세요. (예: https://cover.example.com:4009)”로 한다. 평소 사용하는 원본 빌드 명령은 알고 있을 때만 선택적으로 받는다.
2. 입력을 받으면 프로젝트 인덱스·환경조사·원본 계획 작성과 상세 로그를 남기는 원본 빌드를 진행한다. 키는 현재 대화와 인증 프로세스 메모리에만 보관하고 장기 메모리에는 저장하지 않는다. 이 단계에서는 키를 서버 인증에 사용하지 않는다.
3. 원본 빌드 후 첫 Open API 작업 직전에 받은 키를 해당 실행기 프로세스의 메모리로 전달해 사용한다. 사전 확인은 키가 존재하는지만 확인하고, 키의 유효성과 권한은 첫 Open API 응답으로 판정한다.
4. 실행기를 직접 호출하거나 작업 재개 중 키 전달이 누락되면, 실행기는 첫 서버 작업 직전의 확인에서 `AUTHENTICATION_INPUT_REQUIRED`를 기록하고 `WAITING_USER`로 중단한다. 같은 `job.json`으로 작업을 재개하되, 키는 파일에 넣지 않고 실행기 프로세스의 표준입력으로 전달한다. 현재 대화에서 이미 받은 키가 있으면 다시 요청하지 않는다. 받은 키가 없을 때만 사용자에게 입력을 요청한다.

마지막 확인은 키 없이 서버 작업이 실행되는 것을 막기 위한 방어 처리다. AI Agent가 최초 입력 요청을 생략하고 원본 빌드부터 진행해도 된다는 뜻은 아니다.

Visual Studio, GCC/CMake/Make, STM32CubeIDE, C#, Java에 같은 비밀값 정책을 적용한다. Access Key를 다음 위치에 남기지 않는다.

- 셸 명령 인수와 명령 이력
- JSON/YAML/환경 파일
- 빌드 로그와 단계 결과
- 최종 보고서

예외적으로 `${CODESCROLL_TM_INSTALL_PATH}/agent_kit/open-api-credentials.json`에는 재입력을 피하기 위해 Access Key와 Open API 메인 웹 서버 URL만 저장한다. 이 파일은 첫 서버 인증이 성공한 직후 한 번만 생성하며(`cover_jacov.json`과 같은 배타적 생성·`0600` 권한), 이후 실행에서 자동으로 덮어쓰지 않는다. Windows에서는 `CODESCROLL_TM_INSTALL_PATH`의 ACL을 그대로 상속하므로, 그 설치 경로를 다른 계정과 공유한다면 `cover_jacov.json`의 사용자 홈 디렉터리보다 노출 범위가 넓을 수 있다는 점을 유의한다. 이 예외는 이 파일에만 적용되며 다른 JSON/YAML/환경 파일, 로그, 작업 기록에는 여전히 Access Key를 남기지 않는다.

### 서버 전송 범위

커버리지 빌드를 요청받았으면 Open API에는 사용자가 알려준(또는 캐시 파일의) 메인 웹 서버 URL, Java 계측에는 사용자가 선택했거나 제품이 생성한 `cover_jacov.json.url`을 사용하여 해당 작업의 빌드 경로, 파일명, 도구 정보와 계측 분석 정보를 전송할 수 있다. 원본 빌드가 끝난 뒤 "서버 연결 계측" 또는 "프로젝트 빌드 메타데이터·분석 정보 전송"에 대한 별도 승인을 요청하지 않는다.

이 권한은 두 설정에 명시된 COVER 서비스와 현재 커버리지 빌드 작업에만 적용한다. 설정에 없는 다른 서버·프로그램 실행·측정 데이터 확인·Module 조회와 같은 별도 작업에는 적용하지 않는다.

### 인증 입력 전달


에이전트가 `cover_auth.py -- <실행기 인자>`를 통합 터미널에서 시작하고, 채팅으로 받은 Access Key를 해당 프로세스의 표준입력에 전달한다. 값은 자식 워크플로 프로세스 범위에만 설정하고 종료 시 제거한다. 새 호출에는 이전 인터페이스의 `terminal`과 `--force-login`을 사용하지 않는다. 사용자가 별도 콘솔에 다시 입력하게 하지 말고, 현재 프로세스에 유효한 `COVER_ACCESS_KEY`가 있으면 다시 요청하지 않는다.

Java 빌드 키는 COVER Access Key와 별개의 제품 식별 정보지만 외부에 노출하지 않는다. `-k`/`--key` 명령 인수 대신 기존 제품 설정 파일, 사용자가 지정한 `COVER_JACOV_CONFIG` 또는 위 절차로 생성한 홈 파일을 사용한다.

key 값을 작업 증거·로그 인용·최종 보고서에 복사하지 말고, key 파일을 스킬 저장소나 고객 저장소에 커밋하지 않는다.

사용자 입력을 받기 위해 운영체제의 새 콘솔 창이나 loopback 로그인 페이지를 열지 않는다. 채팅에서 받은 Access Key를 AI Agent 애플리케이션의 통합 터미널에 실행한 인증 프로세스의 표준입력으로 전달한다. 전달 과정에서 값을 출력하거나 응답에 다시 인용하지 않는다.

현재 실행 환경이 비밀값 비노출 표준입력을 지원하지 않으면 명령 인수·환경 파일·임시 파일·외부 콘솔·브라우저 폼으로 우회하지 말고 중단한다.

## Local Agent 식별 정보

### 일반 Agent 시작

이미 실행 중인 Agent는 기존 준비 상태 검증을 통과하면 재사용한다. 시작이 필요한 경우 Windows는 `{Agent 설치 경로}/bin/run_agent_controller.cmd`를 별도 콘솔에서 실행하고, Linux는 같은 `bin` 디렉터리의 `cvctl.sh start`를 Bash로 실행한다. 제품 스크립트가 없으면 중단하며 controller JAR 직접 실행으로 우회하지 않는다.

Linux의 제품 스크립트는 PID 파일 관리와 백그라운드 실행을 담당하며 로그를 계속 출력할 수 있다. 실행기는 스크립트 종료를 기다리는 대신 기존 reporting·listener·서버 online·모니터링 검증으로 준비 상태를 확인한다. 빌드가 끝났다는 이유로 Agent를 자동 종료하지 않는다.

Windows 설치본의 기존 CMD에서 Java·JAR 경로를 따옴표 없이 전달하고 마지막에 `PAUSE`로 대기하는 형식이 확인되면 작업 디렉터리에 호환 사본을 만든다. 사본에서는 Java 실행 파일·JAR 경로에 따옴표를 적용하고 변수 대입은 `set "이름=값"` 형식으로 바꾸며, 마지막 `PAUSE`는 종료 코드 반환으로 바꾼다. 공백이 있는 설치 경로에서 시작이 실패하거나 Java 오류가 입력 대기에 가려지는 일을 막기 위한 처리다. 제품의 JAR 이름과 실행 절차는 유지하고 설치 파일은 수정하지 않는다. 다른 형식이거나 `%~dp0` 등의 표현으로 스크립트 위치에 의존하는 파일은 자동 수정하지 않고 원본을 실행한다. 이 경우 경로 인용이나 입력 대기 문제가 남을 수 있으므로 시작 로그를 확인하며, 모든 CMD 형식을 자동 보정했다고 판단하지 않는다.

Windows 콘솔의 실행 방식은 유지하되 표준 출력과 오류는 작업의 `agent-start.stdout.log`, `agent-start.stderr.log`에 저장한다. 따라서 콘솔에 제품 출력이 표시되지 않을 수 있다. 재시도할 때는 이번 실행에서 추가된 로그만 오류 판정에 사용한다. 비정상 종료와 준비 상태 timeout을 보고할 때 두 로그 경로를 함께 제공한다.

HTTPS Agent에 필요한 작업별 JVM 옵션은 시작 프로세스의 `JAVA_TOOL_OPTIONS`에 전달한다. 기존 옵션을 보존하고 시스템 환경변수나 제품 스크립트는 수정하지 않는다. Open API 또는 JACOV 실행 환경에는 이 값을 추가하지 않는다.

아래 하드웨어 ID 생성용 임시 실행은 일반 Agent 시작과 다르다. 등록에 필요한 ID만 생성한 뒤 해당 임시 프로세스를 종료하는 기존 절차를 유지한다.

`~/.cstm_HardwareAddress`는 Local Agent를 실행하는 운영체제 계정의 홈에 COVER Java 코드가 저장하는 제품 소유 파일이다. 파일이 없을 때는 커버리지 빌드 요청에 포함된 Agent 준비 권한으로 검증된 `CODESCROLL_TM_INSTALL_PATH`의 Java와 Agent JAR를 실행한다.

파일이 생성되고 제품 형식으로 다시 읽히는 것을 확인한 다음 현재 작업이 시작한 부트스트랩 프로세스만 종료한다. 이후 Agent를 다시 탐색하고 그 하드웨어 ID로 서버를 조회한다. 정확한 실행 인자와 timeout은 실행기 구현을 기준으로 한다.

Agent 조회와 실행 준비를 같은 증거로 취급하지 않는다. 조회는 로컬 하드웨어 ID와 유일하게 일치하는 서버 Agent ID를 확인하는 단계다. `online`, 빌드 요청 수신 포트, 상태 보고 서비스, 래퍼 동기화, 모니터링 경로는 커버리지 빌드 직전의 별도 실행 준비 단계에서 확인한다.

동일 하드웨어 ID가 여러 Agent와 일치하거나 상세 조회로 Agent ID를 확인할 수 없으면 잘못된 Agent 선택을 막기 위해 중단한다.

`ipconfig`, `getmac` 또는 유사 명령의 MAC 주소를 그대로 서버 ID로 사용하지 않는다. 네트워크 인터페이스 선택 규칙과 직렬화 형식을 복제해 파일을 직접 만들지 않는다. 필요한 listener 포트가 사용 중이면 현재 하드웨어 ID의 Agent인지 준비 상태를 검증해 재사용한다. 소유 프로세스를 확인할 수 없거나 다른 프로세스이면 새 Agent를 시작하지 않고 충돌로 중단한다.

## Open API 요청과 Agent 설정

기존 매핑은 원본 도구와 역할이 일치하면 변경하지 않고 사용한다. ToolChainInfo 전체 목록 조회와 모델 선택은 매핑이 없는 컴파일러에만 수행하며, [ToolChainInfo 선택 계약](decision-contract-v2.md#toolchaininfo-선택)을 따른다. 기본 제공 여부나 ID 순서로 호환 정의를 자동 선정하지 않는다. 모델 선택도 서버 정의를 수정하는 권한은 아니며, 호환 후보가 없으면 사용자 확인으로 넘긴다.

`cover-open-api-v1.json`에 선언된 Open API v1 작업만 사용한다.

- `GET /v1/agent?macAddress=...`: 현재 PC Agent 단건 조회. 404는 미등록이며, 응답의 `online`은 이후 listener·reporting·monitoring 검증과 함께 준비 상태 판정에 사용
- `POST /v1/agent`: 일치하는 Agent가 없을 때 생성
- `PATCH /v1/agent?macAddress=...`: 기존 항목을 보존한 전체 `toolChains` 목록 적용
- `GET /v1/toolchain-infos`: Agent 매핑에 사용할 기존 ToolChainInfo 조회
- `GET /v1/testing-standards`: 매핑에 사용할 TestingStandard 조회
- `GET /v1/agents`: 접근 가능한 Agent의 이름·MAC 목록 조회. 현재 PC의 등록 여부와 구성 확인에는 기존 단건 조회를 유지한다. 목록 응답을 상세 구성이나 online 검증 대신 사용하지 않는다.
- `POST /v1/toolchain-infos`: 확인된 파서 설정으로 사용자 정의 ToolChainInfo 생성. 성공 응답은 `201`이다.
- `PATCH /v1/toolchain-infos/{id}`: 지정한 사용자 정의의 일부 필드 수정. `toolChainContent`를 보내면 본문 전체가 교체된다.
- `GET /v1/exclusion-policies`: 제외 정책 조회. 일반 커버리지 빌드 흐름에서는 호출하지 않는다.

Agent가 없으면 `createAgent`로 생성한다. 원본 빌드 도구의 Agent 툴체인 매핑은 `updateAgent`의 `toolChains` 전체 교체 필드로 기존 항목을 보존하면서 없는 매핑만 추가하고, 이후 `getAgent`로 재조회해 검증한다. 이 Agent 매핑과 서버의 ToolChainInfo 정의는 다른 데이터다.

ToolChainInfo 정의 생성·수정은 Open API v1에서 지원하고 삭제는 제공하지 않는다. 빌드 실행기는 기존 매핑이 없을 때만 새 매핑에 쓸 호환 정의를 찾는다. STM32CubeIDE 파이프라인에서 실제 GNU Arm 도구가 확인됐고 호환 STM32 정의가 없으면, 사용자가 지정한 [파서 템플릿](stm32cubeide.md#toolchaininfo-파서-본문)으로 새 정의를 생성한다. 일반 GCC·Visual Studio 등 다른 환경으로 이 자동 생성을 확대하지 않는다. 그 밖의 환경에서 정의가 없으면 `AGENT_TOOLCHAIN_UNPROVISIONED`로 사용자 입력을 기다린다. 공유 정의는 자동 수정하지 않는다.

STM32 정의 생성 요청 전에 작업별 시도 기록을 남긴다. 생성 ID를 받은 뒤 목록을 재조회해 분류를 확인하고 현재 Agent 매핑에 연결한다. 생성 API가 400·401·403으로 명확히 거절하면 `REJECTED`로 기록한다. 해당 시도는 실패로 끝내고, 입력이나 인증·권한 문제를 해결한 뒤 같은 작업을 재개하면 다시 생성할 수 있다. 재개 시에도 기존 호환 정의 조회를 먼저 수행한다.

timeout, 409 충돌, 서버 오류, 응답 누락처럼 처리 결과를 확정할 수 없는 경우에는 생성 POST를 자동 재전송하지 않는다. 같은 작업을 재개하면 기존 호환 정의를 다시 조회하고, 여전히 없으면 `TOOLCHAIN_CREATION_REVIEW_REQUIRED`로 중단한다. 서버에 이미 생성됐을 가능성이 있는 POST를 반복하지 않기 위한 처리다. 생성한 정의는 이후 빌드가 실패해도 삭제하지 않는다. 파서 템플릿 해시는 생성 요청 전에 기록하고, 생성 ID는 응답에서 확보한 경우에만 기록한다.

정의 생성·수정 API가 생겼다고 해서 빈 파서 설정이나 추측한 내용을 보내지 않는다. 서버는 `toolChainContent`의 줄 형식만 검사하며, 내용이 해당 컴파일러·타깃과 맞는지는 보장하지 않는다. 이 필드를 생략하면 빈 본문으로 생성될 수 있으므로 실제 사용에 필요한 설정을 먼저 확인한다. 줄 배열의 각 항목은 주석, 빈 줄, 단독 플래그 또는 `key=값` 형태이며 항목 내부에 줄바꿈을 넣지 않는다. `aliasName`은 파서 파일 이름이 되므로 경로 구분자·제어 문자·`..`·앞뒤 공백을 허용하지 않는다. `id`, `original`, `filePath`는 서버 관리 필드이므로 보내지 않는다.

기본 제공 정의(`original=true`)는 수정할 수 없다. 사용자 정의도 다른 Agent에서 참조할 수 있으므로, 수정 대상 ID와 변경 내용·공유 영향을 확인한 별도 요청이 있을 때만 수정한다. 수정 응답의 `referencedAgentCount`는 영향받는 Agent 수이며 요청 전에 승인을 대신하는 값은 아니다. 생성 후에는 반환된 ID로 목록을 다시 조회해 분류와 이름을 확인한다. 목록 응답에는 파서 본문이 없으므로 재조회만으로 본문이 동일하게 저장됐다고 주장하지 않는다. 403이면 관리자 권한의 Access Key가 필요한지 확인하고, 409 또는 응답이 불명확한 경우 생성 요청을 반복하지 말고 목록에서 처리 결과부터 확인한다.

생성 응답에서 ID를 얻지 못했다면 목록의 고유 `aliasName`과 요청한 컴파일러 계열·버전·타깃을 함께 비교한다. 같은 이름이 보인다는 사실만으로 이번 생성이 성공했다고 단정하지 않는다. 기존 항목인지 이번 요청으로 생긴 항목인지 확인할 수 없거나 본문 검증이 필요하면 담당자 확인을 요청한다.

개별 수정 요청은 실행기의 `api-call --operation updateToolChainInfo --path-param id=<정의 ID> --payload <요청 JSON> --allow-mutation`으로 전달한다. 공통 인증·서버 원점·TLS 규칙은 그대로 적용하며 Access Key는 명령 인수에 넣지 않는다. 내부 REST API로 우회하지 않는다.

Agent PATCH는 생략한 필드를 유지한다. 실행기는 필요한 `toolChains`만 보내며, 이 배열은 전체 교체이므로 유지할 기존 매핑도 함께 보낸다. `toolChainInfoId`는 `C_COMPILER`, `CPP_COMPILER`, `C_CPP_COMPILER`에만 넣는다. `C_LINKER`, `CPP_LINKER`, `ARCHIVER`에는 이 필드를 보내지 않으며, 상세 조회의 `toolChainInfo=null`도 정상으로 처리한다. 모든 도구에 ID를 요구하면 정상 링커·아카이버 요청이 서버에 도달하기 전에 로컬 검증에서 거부된다. 새 매핑의 `testingStandardId`는 기존처럼 조회한 값을 사용하되 실제 측정 기준으로 쓰이는 것은 컴파일러 항목이다. API가 다른 Agent 필드의 부분 수정도 지원하더라도 빌드에 무관한 경로·권한·제외 정책을 자동 변경하지 않는다.

최신 서버에는 측정 초기화·계산, 파일별 커버리지, 그룹·보고서 등의 API도 있다. 이 스킬은 커버리지 빌드를 담당하므로 해당 작업을 호출 목록에 추가하지 않는다. 특히 측정 초기화는 기존 데이터를 바꿀 수 있으며 빌드 성공 확인 과정에서 호출하지 않는다.

C#은 Agent 조회·생성·실행 준비만 수행하고 C/C++ 툴체인 매핑을 만들지 않는다. Java는 JACOV 설정을 사용하며 커버리지 빌드만으로 Local Agent를 시작하지 않는다. 요청·응답 필드는 [cover-open-api-v1.json](cover-open-api-v1.json)의 명세를 따른다.

제품 코드의 Open API `monitoringPath`는 Local Agent가 감시하는 디렉터리이고 `targetLogPath`는 타깃 실행 로그 경로다. 신규 Agent 생성 시 `monitoringPath`를 `CODESCROLL_TM_INSTALL_PATH/coverage`로 설정한다. 이미 등록된 Agent는 필요한 `toolChains`만 PATCH하고 기존 경로를 덮어쓰지 않는다.

신규 Agent의 `agentName`은 `AI-Agent-Kit-<환경>-<호스트>-<식별 suffix>-<작업 suffix>` 형식으로 생성해 서버 UI에서 AI Agent가 만든 Agent임을 구분할 수 있게 한다. 전체 하드웨어 ID나 비밀값은 이름에 넣지 말고, 기존 Agent의 이름은 변경하지 않는다.

Agent 생성(`POST /v1/agent`)의 `userName`은 선택 항목이다. 지정된 `coverUserName` 또는 `COVER_USER_NAME`이 있으면 전달하고, 없으면 필드를 생략해 Access Key 계정을 소유자로 사용한다. 내장 관리자 계정은 Agent 소유자가 될 수 없으므로 서버가 이 사유를 명시한 400 응답을 반환할 때만 `AGENT_OWNER_REQUIRED`로 소유자 이름을 요청한다. 그 밖의 400 응답을 사용자 이름 누락으로 해석하지 않는다. 소유자 이름은 비밀값으로 취급하지 않는다.

기존 Agent의 다른 속성이나 기존 래퍼를 삭제하지 않는다. 추가 전 DTO를 읽고 기존 `toolChains` 항목은 값 그대로 보존한다. 없는 항목만 추가한 뒤 다시 조회해 신규 등록과 기존 설정 보존을 확인한다. PATCH 직전 재조회에서 준비 중 서버 매핑 변경이 확인되면 덮어쓰지 않고 중단한다. 이 재조회는 서버의 원자적 조건부 갱신을 대신하지 않으므로, 조회와 PATCH 사이에 발생한 동시 변경까지 차단한다고 보장하지 않는다. 동일한 래퍼 이름이 다른 도구나 역할에 사용 중인 경우에도 자동 교체하지 않는다. 분석 오류 발생 후 복구 스킬이 검증한 정의로 연결을 변경하는 절차는 별도로 유지한다.

서버가 반환한 Agent와 ToolChainInfo는 매 요청의 확인 근거로 사용한다. 작업별 캐시를 현재 서버 상태보다 우선하지 않는다.
