# Java / JACOV

## 빌드 결과 판정 정책

커버리지 빌드는 실행 명령의 종료 코드가 0이면 성공, 0이 아니면 실패로 판정한다. 탐침 여부를 확인하기 위한 래퍼 호출·우회 로그 검사, 심볼 검사, C#/Java 처리 건수 집계와 빌드 후 산출물 갱신·경로 비교는 수행하지 않는다. 재사용 설정으로 재빌드할 때도 종료 코드로 판정한다. 설정 적용 결과는 빌드 결과와 별도로 보고하며, 설정 적용 실패로 이미 성공한 빌드 결과를 변경하지 않는다. 원본 빌드의 도구·산출물 조사, 실행 전 설정·출력 경로 검증, 백업·복구는 유지한다.


## 1. 환경과 빌드 방식 확정

1. `CODESCROLL_TM_SERVER_URL`, `CODESCROLL_TM_INSTALL_PATH`, 프로젝트 절대 경로를 공통 시작 조건으로 확인한다. 최초 요청에 Access Key가 없으면 채팅으로 먼저 받고 메모리에만 보관하되, Java 원본 빌드가 끝난 뒤 첫 COVER 서버 작업 직전에만 사용한다.
2. `${SKILL_ROOT}/scripts/project_discovery.py managed --project-root <경로>`의 Java 증거와 고객 스크립트·CI를 읽고 다음 중 실제 진입점을 선택한다. `buildSystemCandidates`는 파일 기반 후보이고 `confirmedBuildSystems`는 수집기가 제한된 고객 빌드/CI 진입 파일에서 실제 실행 토큰을 찾은 결과다.
   - Maven 후보: 선택 프로젝트의 `pom.xml`, `mvnw`, `mvnw.cmd`
   - Maven 확인: 고객 빌드 스크립트, CI, 기존 빌드 로그 또는 사용자 원본 명령에서 `mvn`, `mvnw`, `mvnw.cmd` 호출이 확인되고 해당 명령의 goal, 프로필, 모듈, `-f` 대상, 작업 디렉터리를 수집한 경우
   - Gradle 후보: `build.gradle[.kts]`, `settings.gradle[.kts]`, `gradlew`, `gradlew.bat`
   - Gradle 확인: 고객 빌드 스크립트, CI, 기존 빌드 로그 또는 사용자 원본 명령에서 `gradle`, `gradlew`, `gradlew.bat` 호출과 task가 확인된 경우
   - 빌드 스크립트 후보(`BUILD_SCRIPT`): 프로젝트의 `.bat`, `.cmd`, `.ps1`, `.sh`에서 Java 도구 실행, 다른 스크립트 호출 또는 변수로 지정한 실행 파일 호출이 발견된 경우다. `compile.cmd`, `make-release.sh`처럼 파일 이름은 제한하지 않는다. 후보 경로는 `buildScriptFiles`에 남긴다. `java` 호출은 애플리케이션 실행일 수도 있으므로 후보만으로 빌드라고 확정하지 않는다.
   - 빌드 스크립트 확인: 모델이 스크립트와 프로젝트 문서를 읽어 실제 빌드 진입점, 인자, 작업 디렉터리와 내부에서 사용하는 빌드 도구를 확인한 경우다. 스크립트에서 Maven·Gradle을 호출하면 해당 빌드 방식으로 처리한다.
   - 직접 빌드 후보(`COMMAND_LINE_CANDIDATE`): Java 소스가 있지만 수집한 파일에서 Maven·Gradle·빌드 스크립트 후보를 찾지 못한 경우다. 이 상태만으로 직접 `javac`를 실행한다고 확정하지 않는다.
   - 직접 빌드 확인: 사용자 명령이나 프로젝트 자료에서 직접 실행하는 `javac`, Ant 등의 빌드 명령과 인자, 작업 디렉터리를 확인한 경우다. 자동 수집에서 놓친 스크립트가 발견되면 그 스크립트를 진입점으로 사용한다.

   조사한 스크립트 목록은 `scriptFiles`에 별도로 남긴다. 생성물·작업 기록 디렉터리와 `node_modules`, `vendor`, `third_party`, `third-party`, 가상환경 디렉터리는 스크립트 조사에서 제외한다. 후보 탐지로 호출을 해석하지 못했더라도 모델은 이 목록과 프로젝트 문서·CI를 읽어 진입점과 스크립트 호출 관계를 확인한다. 스크립트를 실행해서 내용을 알아내지는 않는다.

   별도의 Java 빌드 스크립트가 있으면 CI의 Maven·Gradle 호출만으로 그 스크립트 후보를 제외하지 않는다. 문서와 스크립트 내용을 읽어 실제 진입점을 선택한다. 빌드 스크립트와 직접 빌드의 확인은 모델이 수행하며, `confirmedBuildSystems`에 자동 기록됐다는 의미가 아니다.
3. Maven/Gradle이 설치돼 있거나 PATH에서 발견된 사실, `mvn --version`/`gradle --version` 성공, `pom.xml`/Gradle 파일 존재만으로 실제 빌드 방식을 확정하지 않는다. Maven과 Gradle이 함께 있거나 명령 증거가 없거나 여러 모듈의 빌드 진입점·소스 묶음·goal·task가 모호하면 정형 자동 판정을 중단하고 모델이 프로젝트 관계를 해석한다.

   저장소에도 고객의 실제 선택 근거가 없을 때만 사용자에게 원본 빌드 명령을 요청한다.

   Gradle의 `settings.gradle[.kts]`가 하나이고 다른 Gradle 빌드 파일이 그 디렉터리 아래에만 있으면, 실행기는 해당 settings를 루트 진입점 후보 하나로 정리한다. 여러 settings가 있으면 합성 빌드나 독립 프로젝트 관계를 모델이 확인한다. task는 진입점 파일의 존재만으로 추측하지 않는다. 아직 선택하지 않은 task와 다른 미결정 항목은 원본 빌드 전 결정 파일에 함께 담는다.

   Maven POM이 여러 개 있으면 각 POM의 `<modules>` 관계를 확인한다. 하나의 POM이 발견된 모든 하위 POM을 직접 또는 간접으로 포함하면 해당 POM을 reactor 루트로 사용한다. 서로 독립된 POM이 있거나 XML을 읽을 수 없으면 진입점을 자동으로 고르지 않는다. reactor 루트를 확인했더라도 goal은 POM만 보고 추측하지 않으며, 고객 명령이나 모델이 확정한 값을 사용한다.
4. Dockerfile, Compose, Kubernetes, Helm, 배포 스크립트는 프로그램 배치와 연결 구조를 보여주는 증거일 뿐 MSA 또는 JavaLib Agent 사용을 확정하는 근거가 아니다.

빌드 방식을 확정한 뒤 계측 도구를 선택한다. Maven·Gradle 빌드는 해당 JACOV 플러그인을 사용하고, 그 외의 Ant, 직접 `javac`, 기타 빌드 시스템·고객 스크립트는 JACOV CLI를 연결한다.

스크립트의 확장자로 계측 방식을 정하지 않는다. `build.sh`가 Maven을 호출하면 Maven 플러그인, `build.cmd`가 Gradle을 호출하면 Gradle 플러그인을 사용한다. 이때 고객 스크립트의 환경 초기화·소스 생성·후처리를 생략하지 않는다. 빌드 파일이 있다는 사실만으로 CLI 연결까지 자동 지원된다고 판단하지 않으며, 모델이 실제 컴파일 입력과 실행 순서를 확인한다.

`BUILD_SCRIPT` 또는 `COMMAND_LINE_CANDIDATE`를 선택하면 `managedTasks` 대신 `originalBuildPlan`을 작성한다. 실행 드라이버는 기존 `JAVA_CLI`를 사용하고, 고객 스크립트의 실행 파일·인자·작업 디렉터리·환경 초기화를 기록한다. 예를 들어 하위 디렉터리의 `build.bat jar`가 공식 빌드 방법이면 그 명령을 유지한다. POM이 함께 있다는 이유로 Maven 명령으로 바꾸지 않는다. 자동 수집 범위 밖의 스크립트는 모델이 내용을 확인한 뒤 같은 원본 계획으로 전달한다.

## 2. 원본 빌드와 실제 Java 툴체인

1. JACOV 설정이 없는 상태에서 고객의 원본 clean/build를 먼저 수행하고 성공 여부를 확인한다.
2. Maven/Gradle 래퍼, `JAVA_HOME`, 툴체인 설정, CI 이미지, verbose/debug 로그를 함께 분석해 실제 JDK 설치 경로, Java 실행 파일, 컴파일러 source·target·release를 기록한다.
3. 별도 프로세스로 실행된 `javac`가 있으면 실제 절대 경로와 명령을 수집한다. 컴파일러 API 또는 데몬 내부 컴파일러를 사용하면 존재하지 않는 `javac` 프로세스를 요구하지 말고 선택된 JDK·툴체인과 컴파일러 실행 방식을 증거로 기록한다.
4. 원본 빌드 goal·task, 작업 디렉터리, 프로필, 로컬 저장소, daemon, 산출물(JAR/WAR/classes)을 보존한다. 모델이 임의로 Maven과 Gradle을 바꾸거나 task를 추가하지 않는다.
5. 원본 빌드가 실패하면 JACOV 빌드를 진행하지 않는다.
6. Maven 원본 빌드에는 비밀값과 전체 시스템 속성을 노출할 수 있는 `-X`를 기본 추가하지 않는다. 필요한 JDK 식별은 `-V`, 툴체인 출력과 실제 컴파일 증거로 수집하고, 고객이 이미 사용한 상세 옵션은 그대로 보존한다.

Maven `-V` 출력의 `runtime:` 경로는 실제 실행에 사용된 JDK 후보로 수집한다. 해당 경로에 `bin/java`와 `bin/javac`가 모두 있을 때만 JDK 홈으로 인정한다. 따라서 Maven이 이미 출력한 경로를 다시 사용자에게 묻지 않으며, JRE 경로나 우연히 같은 이름을 가진 디렉터리를 JDK로 잘못 선택하지 않는다.

Gradle 원본 빌드에는 작업별 `observe-gradle.init`을 추가한다. 이 스크립트는 `JavaCompile` 실행 시 실제 compiler metadata의 JDK 경로와 버전을, archive 작업 완료 시 실제 산출물 경로를 로그로 남긴다. 고객 빌드 파일이나 task의 컴파일 옵션을 바꾸지 않는다. 실행기는 `COVER_OBS_JAVA_HOME`, `COVER_OBS_JAVA_VERSION`, `COVER_OBS_ARTIFACT`를 읽어 후속 항목을 함께 확정한다. 서로 다른 JDK가 관찰되면 임의로 하나를 고르지 않고 모델 결정 묶음에 포함한다.

이 기록은 원본 빌드 한 번에서 수집한다. JDK 확인이나 산출물 목록 작성을 위해 같은 원본 빌드를 추가하지 않는다. 재개 시 입력이 같으면 원본 로그와 해석 결과를 이어 사용한다.

## 3. Java 키와 Local Agent 경계

1. Java 커버리지 빌드에는 설치된 COVER Local Agent와 JACOV 도구가 필요하다. `CODESCROLL_TM_INSTALL_PATH` 아래의 Local Agent JAR와 `agent-config.yaml`, 선택한 JACOV 도구 파일을 확인하되, 이 단계에서 Local Agent를 시작하지 않는다.
2. JACOV 입력은 다음 순서로 확인한다.
   1. 사용자가 명시한 `COVER_JACOV_CONFIG` 키 파일
   2. 실행 계정의 `${user.home}/cover_jacov.json`
3. 파일은 JSON `url`, `key`가 모두 비어 있지 않고 `url`이 올바른 서버 주소 형식일 때 사용한다. `CODESCROLL_TM_SERVER_URL`은 Local Agent의 `tm-remote-service` 주소이고, Open API는 사용자가 알려준(또는 캐시 파일의) 메인 웹 서버 URL로 별도 구성한다. `cover_jacov.json.url`도 JACOV가 사용할 별도 주소이므로 세 주소의 protocol·host·port가 같아야 한다고 판단하지 않는다.

   예를 들어 Open API가 `https://cover.example:4009`에 있고 JACOV 서비스가 `http://cover.example:9089`에 있을 수 있다. 이 차이는 잘못된 설정이 아니다. 실행기는 JACOV 주소를 Open API 주소로 바꾸거나 `cover_jacov.json`을 수정하지 않는다.
4. 검증한 `cover_jacov.json` 절대 경로를 Maven·Gradle·직접 빌드 프로세스의 `COVER_JACOV_CONFIG`로 전달한다. 파일 경로만 전달하고 key 값은 환경변수·명령행·작업 기록에 남기지 않는다. Open API에 적용한 인증서 검증 생략 설정도 JACOV 프로세스에 전달하지 않는다.
5. `COVER_JACOV_CONFIG`를 지정하지 않았고 홈의 파일이 없으면 원본 빌드 후 Access Key 인증을 준비하고 아래 절차로 생성한다. 기존 파일이 잘못된 JSON이거나 url·key가 유효하지 않으면 `JACOV_CONFIG_INVALID`로 실패하며 덮어쓰지 않는다. 명시한 `COVER_JACOV_CONFIG` 파일이 없으면 `JACOV_CONFIG_MISSING`으로 대기한다. 명시한 경로를 무시하고 홈 파일로 대체하지 않는다.
   - 실행 계정 홈의 `.cstm_HardwareAddress`에서 제품이 만든 MAC을 읽는다. 파일이 없으면 `AGENT_IDENTITY_MISSING`으로 대기하고 Agent를 자동 실행하지 않는다.
   - Open API `getAgents`에서 같은 MAC의 항목을 찾는다. 없거나 여러 개면 `JACOV_AGENT_NOT_UNIQUE`로 대기하며 새 Agent를 자동 등록하지 않는다.
   - 일치한 항목에 `id`가 있으면 사용한다. 현재 목록 명세에는 ID가 없으므로 `getAgent?macAddress=...` 상세 조회로 받는다. 상세 응답도 같은 MAC인지 확인한다. 상세 조회에는 관리자 권한이 필요할 수 있으며 권한 오류를 내부 API로 우회하지 않는다.
   - 받은 Agent ID를 `key`, `CODESCROLL_TM_SERVER_URL`의 값을 `url`로 하여 홈의 `cover_jacov.json`을 생성한다. Open API용 HTTPS 주소나 메인 웹 서버 URL로 바꾸지 않는다. 예시의 ID·주소를 하드코딩하지 않는다.
   - 기존 파일과 동시 생성된 파일을 덮어쓰지 않는다. 새 파일은 UTF-8이며 POSIX에서는 생성 권한을 `0600`으로 제한한다. Windows에서는 홈 디렉터리의 ACL을 상속한다.
   - 생성 중 쓰기나 파일 닫기에 실패하면 `JACOV_CONFIG_WRITE_FAILED`로 중단하고 해당 파일 경로를 안내한다. 파일을 확인한 직후에도 다른 프로세스가 수정할 수 있으므로 실패한 파일을 자동 삭제하거나 덮어쓰지 않는다. 파일에는 불완전한 내용이 남을 수 있으며 사용자가 파일 상태와 저장 권한을 확인하고, 필요하면 불완전한 파일을 수정하거나 제거한 뒤 재개한다. 다음 실행에서 JSON 형식이 잘못된 것으로 확인되면 `JACOV_CONFIG_INVALID`로 중단하며, 불완전한 파일을 제거하는 작업도 사용자 확인 없이 수행하지 않는다. 오류 메시지에는 파일 내용이나 key를 넣지 않는다.
   - 동시 생성된 파일을 발견하면 그 파일을 기존 파일과 같은 기준으로 검증해 사용한다. 기존 파일의 key를 현재 Agent ID와 비교하거나 url을 환경변수 값에 맞춰 변경하지 않는다. MAC은 제품 파일의 하이픈 형식을 사용하고 대소문자를 구분하지 않는다.
   - Agent ID가 들어 있는 API 응답은 메모리에서만 처리한다. Java key를 기록하는 예외는 홈의 제품 설정 파일뿐이며, API 응답 임시 파일·단계 JSON·로그·채팅에는 넣지 않는다. 생성 후에는 기존 파일과 동일한 형식 검사를 수행한다.
6. JACOV CLI/Maven/Gradle 계측은 `cover_jacov.json.url`과 빌드 키로 COVER 서버의 Java 빌드 서비스에 직접 연결한다. Local Agent `online=true`를 Java 빌드 성공 조건으로 사용하지 않는다. 실행 전 설정·연결 준비와 실제 빌드 종료 코드를 구분해 기록한다.
7. 표준 `tm-javalib` 런타임으로 측정 실행까지 요청됐을 때만 `CODESCROLL_TM_JAVA_PORT`(기본 9091)의 Local Agent 수집 listener를 확인한다. Java 빌드 키 파일만 존재한다는 이유로 런타임 준비 완료라고 판단하지 않는다.

## 4. JACOV 도구 선택

설치된 `${CODESCROLL_TM_INSTALL_PATH}/bin`의 실제 파일을 열거하고 현재 JDK/소스 호환 수준에 맞는 도구를 선택한다. 이름을 추측하거나 존재하지 않는 버전을 만들지 않는다.

실행기는 `${SKILL_ROOT}/scripts/managed_tools.py`의 설치 조사를 호출한다. 별도 진단이 필요하면 `managed_tools.py jacov --install-root <CODESCROLL_TM_INSTALL_PATH> --output <작업 경로>/jacov-install-evidence.json`을 사용한다. 수집기는 설치 파일의 존재·크기·SHA-256, Maven 플러그인 JAR 내부 좌표·내장 POM 경로, Gradle 플러그인 등록 정보와 진입 클래스의 최소 Java 버전을 기록한다. 수집기 자체는 도구를 선택하지 않으므로 `selection=MODEL_REQUIRED`를 유지한다.

Java 파이프라인은 실제 원본 빌드의 Java 버전과 설치 정보를 함께 판단한다. 같은 JRE 변형의 후보가 하나이고, Maven은 플러그인 메타데이터가 확인되거나 Gradle은 진입 클래스의 최소 Java 버전이 맞으면 자동 선택한다. 파일명만 확인한 후보나 호환 후보가 여러 개인 경우에는 모델이 설치본과 소스 호환 수준을 확인한다. `selection=MODEL_REQUIRED`라는 수집 결과만 보고 매번 같은 JAR 선택을 다시 요청하지 않는다. 명시적으로 선택한 도구도 확인된 최소 Java 버전이 실제 JDK보다 높으면 `JACOV_JDK_INCOMPATIBLE`로 중단한다.

- 직접 빌드(Command Line): `cover-jacov-cli-jre8.jar`, `cover-jacov-cli-jre17.jar`
- Maven: `jacov-maven-plugin-jre8.jar`, `jacov-maven-plugin-jre17.jar`
- Gradle: `jacov-gradle-plugin-jre11.jar`, `jacov-gradle-plugin-jre17.jar`, `jacov-gradle-init.gradle`

CLI는 `--help`, Maven 플러그인은 JAR 내부 `pom.properties`/`plugin.xml`, Gradle은 설치된 init script를 읽어 현재 설치본의 옵션·goal·version을 확인한다. 문서의 SNAPSHOT 버전을 고정값으로 복사하지 않는다.

## 5. 커버리지 빌드

빌드 방식에 맞는 JACOV 도구를 연결한 뒤 원본과 같은 조건으로 다시 빌드한다. 여기서 만드는 산출물과, 이후 프로그램을 실행할 때 필요한 런타임 설정을 구분한다. 실행 런타임 선택은 뒤의 별도 절차를 따른다.

### clean 이전 확인

기존 산출물을 삭제하기 전에 다음을 확인한다.

1. 선택한 JDK 경로에 컴파일러가 있고, 빌드 실행 파일과 JACOV JAR가 존재하는지 확인한다. 고객이 사용한 실행 환경과 환경 초기화는 원본 명령에서 유지한다.
2. 위 절차로 기존 `cover_jacov.json`을 확인하거나 누락된 홈 파일을 생성한다. JSON 형식, 비어 있지 않은 url·key와 URL 형식을 확인하고 검증한 절대 경로만 `COVER_JACOV_CONFIG`로 전달한다. 기존 파일 내용은 변경하지 않는다.
3. Gradle init script를 만들 때 선택한 JAR의 절대 경로를 넣고 Windows 경로의 구분자와 인용부호를 처리한다. 이미 준비된 작업별 script를 받았을 때도 참조를 확인한다. 상대 경로로 빌드를 실패시킨 다음 수정하는 방식은 사용하지 않는다.
4. 원본 빌드 후 Open API 인증을 확인하고, JACOV 설정에 기록된 호스트와 포트에 TCP 연결이 가능한지 짧게 확인한다. TCP 단계가 실패하면 `JACOV_NETWORK_UNAVAILABLE`로 중단하며 커버리지 `clean`은 시작하지 않는다.
5. Maven은 필요한 JAR의 로컬 저장소 설치를, Gradle은 최종 init script와 런타임 속성 설정을 완료한 뒤 계측 빌드를 시작한다.

TCP 확인은 JACOV 인증·TLS·분석정보 업로드를 검증하지 않는다. 해당 오류는 실제 계측 단계에서 확인하며 사전 검증용 비공개 API를 새로 만들지 않는다. Open API와 JACOV의 주소 및 인증서 정책도 결합하지 않는다. 실행 권한 선택과 실패 후 산출물 복구는 [실행 시간 최적화 원칙](performance.md#java의-clean-이전-확인과-복구)을 따른다.

### 산출물 경로 불변 규칙

- 상세 로그를 남긴 원본 Maven/Gradle/`javac` 빌드에서 확인한 JAR/WAR/classes의 절대 경로를 `originalOutputPaths`로 고정한다.
- `project.build.directory`, Maven outputDirectory, Gradle `buildDir`, Java classes/resources 출력 위치와 작업 디렉터리는 원본과 동일하게 유지한다. 별도로 복제한 저장소, 작업 기록 디렉터리의 build/output 디렉터리, `-Dproject.build.directory`, `-PbuildDir` 같은 경로 재지정은 사용하지 않는다.
- 계측 소스를 임시 위치에 생성해야 하는 CLI 방식은 최종 컴파일·패키징 산출물을 원본 출력 경로에 만들 수 있을 때만 허용한다. 그 보장이 없으면 임시 산출물을 만들지 말고 중단한다.
- 커버리지 빌드를 시작하기 전에 계획의 출력 경로가 원본 조사에서 확인한 경로를 유지하는지 검증한다. 경로가 바뀌는 계획은 실행하지 않는다.
- 빌드 후에는 `verify-output-paths`를 실행하거나 `sameAsOriginal=true`를 요구하지 않는다. 커버리지 빌드의 성공 여부는 실행 명령의 종료 코드로 판정한다.
- 빌드 후 파일을 다시 수집하거나 갱신 여부를 비교하지 않는다. 보고서의 산출물 경로는 원본 조사에서 확인한 경로임을 밝히고, 현재 빌드에서 새로 확인한 결과로 표현하지 않는다.

### 직접 빌드(Command Line)

먼저 컴파일 입력을 별도 계측 디렉터리로 바꿀 수 있는지 확인한다. 가능하면 아래 기본 방식을 사용한다. 빌드 스크립트라는 이유만으로 원본 소스를 수정하지 않는다. 빌드 프로그램 내부에 소스 경로가 고정되어 있고 입력 변경에 큰 수정이 필요한 경우에만 뒤의 원본 위치 임시 적용 방식을 사용한다.

#### 기본: 별도 소스 디렉터리 사용 (`SEPARATE_OUTPUT`)

1. 고객 소스 루트마다 계측 소스를 준비하되, 작업별 임시 출력 디렉터리를 최종 class/JAR/WAR 산출물 위치로 사용하지 않는다. 다음 형태는 계측 소스 임시 위치를 나타내며 최종 패키징은 원본 출력 경로로 수행해야 한다.

```text
java -jar <cover-jacov-cli> <source-root> --output=<instrumented-source-root> [include/exclude/replaceSrcDir]
```

2. 고객 빌드 스크립트와 직접 참조하는 빌드 파일을 분석한다. 환경 초기화와 소스 생성이 끝난 뒤, Java 컴파일 전에 JACOV CLI를 실행하도록 변경안을 만든다. Ant는 필요하면 `build.xml`의 target 의존 관계와 `javac` 입력도 변경한다. CLI 호출만 추가하고 원본 소스를 컴파일하는 구성은 허용하지 않는다.
3. 원본 빌드는 파일을 바꾸기 전에 수행한다. 이후 모델은 [CLI 변경 결정](decision-contract.md#java-cli-빌드-파일-변경)의 `javaCliIntegration`으로 수정 대상·현재 해시·정확한 변경 구간·연결 근거를 전달한다. 고객 파일을 먼저 직접 수정하거나 작업 증거를 우회하지 않는다.
4. 실행기는 모든 변경안을 검증한 뒤 원본 바이트를 작업 백업과 같은 폴더의 `<원본파일명>.cover-ee-backup`에 보관한다. 기존 백업이 원본과 다르면 덮어쓰지 않고 중단한다. 고객의 미커밋 변경도 백업에 포함한다. 프로젝트 밖의 공유 빌드 파일은 자동 수정하지 않는다.
5. 원본 Java 소스를 덮어쓰지 않는다. 컴파일 입력만 계측 소스 디렉터리로 연결하고 원본 리소스, 클래스패스, 어노테이션 프로세서, 생성 소스, 인코딩, 모듈 경로, 컴파일러 플래그와 최종 산출물 경로를 유지한다. 계측 소스는 재실행 가능한 프로젝트 내 전용 경로에 생성하며, 일회용 job 경로를 스크립트에 남기지 않는다. 매 빌드마다 소스 생성 이후 계측이 수행되고 계측 명령의 비정상 종료가 전체 빌드 실패로 전파되도록 한다.
6. 실행 파일·인자·작업 디렉터리는 원본 빌드와 동일하게 유지한다. CLI 경로에서는 `javaCoverageArguments`로 다른 명령을 우회 실행하지 않는다. 스크립트 자체에 검증된 `COVER_JACOV_CONFIG` 파일 경로 사용도 연결하되 key나 파일 내용은 넣지 않는다. JACOV 도구는 확인한 절대 경로로 참조한다.
7. 커버리지 빌드 명령이 종료 코드 0으로 끝나면 수정본을 유지하고 공통 `.cover-ee` 적용 상태에 변경 파일과 백업을 등록한다. 사용자는 기존 빌드 명령으로 다시 커버리지 빌드한다. 다음 원본 빌드에서는 공통 설정 해제 절차로 변경을 일시 해제한다.
8. 빌드나 검증이 실패하면 먼저 스크립트를 복구한 뒤 필요한 원본 산출물 복구를 수행한다. 작업 중 다른 수정이 발견되면 덮어쓰지 않고 중단한다. 원본 옵션이나 계측 소스 연결을 보존할 수 없으면 실행 전에 필요한 정보를 사용자에게 요청한다.

#### 예외: 빌드 동안만 원본 위치에 계측본 적용 (`IN_PLACE`)

예를 들어 스크립트가 자체 Java 빌드 프로그램을 실행하고 그 프로그램이 `src/main`을 직접 열어 컴파일한다면, CLI의 출력 경로만 바꿔서는 계측 소스가 빌드에 쓰이지 않는다. 소스 경로를 바꾸는 옵션이나 작은 빌드 파일 수정으로 해결할 수 없는 근거를 확인한 경우에 이 방식을 선택한다.

1. [CLI 변경 결정](decision-contract.md#java-cli-빌드-파일-변경)에 별도 출력 방식을 사용할 수 없는 이유와 계측할 소스 루트를 기록한다. 계측 대상 소스 생성이 이미 끝났고 이후 원본 빌드가 해당 소스를 다시 생성하거나 삭제하지 않는지도 확인한다. 원본 빌드 중에 생성되는 소스라면 이 방식으로 먼저 계측하지 않는다. 생성 이후에 계측을 연결할 수 있도록 기본 방식의 빌드 흐름 수정안을 마련하거나 사용자에게 필요한 정보를 요청한다.
2. 실행기는 `pipelines/java/inplace.py`로 해당 소스의 현재 바이트와 SHA-256, 파일 모드, 수정 시각을 백업한다. 미커밋 변경도 그대로 보관한다. 백업과 진행 기록은 프로젝트의 `.cover-build/java-inplace-active`에 남긴다. 프로젝트 밖 파일과 링크로 연결된 소스 트리는 자동 수정하지 않는다.
3. JACOV CLI는 우선 새 임시 디렉터리에 계측 소스를 만든다. 계측 결과의 Java 파일 목록과 원본이 그대로인지 확인한 다음 계측 바이트를 원본 위치에 적용한다. Java 파일이 추가되거나 빠졌다면 적용하지 않고 중단한다. 제품 CLI가 원본을 직접 덮어쓰게 하지 않는 이유는, 실행기가 적용한 계측 바이트의 해시를 알아야 복구 시 고객의 동시 수정과 구분할 수 있기 때문이다.
4. 원래 실행 파일·인자·작업 디렉터리로 빌드한다. 컴파일 옵션, 리소스, 패키징과 최종 산출물 경로는 바꾸지 않는다. 고객 빌드 스크립트도 수정하지 않는다.
5. 성공·실패·처리 가능한 중단 모두 소스를 복구하고 원본 해시를 확인한다. 성공한 계측 class/JAR는 그대로 둔다. 실패한 빌드의 부분 산출물은 성공 결과로 취급하지 않으며, 소스 복구 후 기존 원본 산출물 복구 절차를 따른다. 복구 전에 다른 수정이 발견되면 그 파일은 덮어쓰지 않고 백업을 남긴다. 충돌하지 않은 파일은 복구하되 전체 작업은 실패로 끝낸다. 소스 복구가 확인되기 전에는 실행기가 원본 산출물 복구용 빌드도 실행하지 않는다.
6. 프로세스 강제 종료나 전원 차단 때는 즉시 복구를 보장할 수 없다. 잠금과 진행 기록이 남으면 실행기와 `.cover-ee` 진입점의 다음 빌드를 차단한다. 사용자가 외부에서 직접 실행하는 빌드까지 막는 것은 아니므로 복구 전 일반 빌드를 실행하지 않도록 안내한다. 잠금은 `.cover-build/java-inplace-active.lock`에 PID를 기록하고, `java-inplace-active/manifest.json`은 파일별 `path`, `backup`, `sha256`, `stagedSha256`, `installed`와 `restoreConflicts`를 기록한다. 해당 프로세스가 종료됐는지 확인하고 백업·원본·계측본 해시를 비교하여 소스를 복구한 뒤에만 진행 기록과 잠금을 정리한다. 확인 없이 잠금을 지우거나 빌드를 재시도하지 않는다.
7. 성공한 설정에는 독립 실행 가능한 보조 스크립트와 계획을 `.cover-ee`에 포함한다. 이후 `build-coverage.cmd` 또는 `build-coverage.sh`를 실행하면 매번 백업·계측·빌드·복구를 수행한다. 원본 소스가 복구되어 있으므로 기존 빌드 명령만 실행하면 일반 빌드이며, 커버리지 빌드로 안내하지 않는다.

Maven·Gradle 플러그인 방식에는 이 예외를 적용하지 않는다. CLI의 include/exclude 등 별도 계측 옵션이나 빌드 중 소스 생성 순서 변경이 필요한 경우에는 현재 단순 `IN_PLACE` 계획에 임의 옵션을 넣지 않고 기본 방식으로 명시적으로 연결한다.

### Maven

1. 선택한 JACOV Maven 플러그인 JAR의 내장 `pom.properties`와 `pom.xml`에서 실제 `groupId:artifactId:version`을 읽는다. Maven은 실행 안에서 플러그인을 등록하기 전에 플러그인 POM을 해석하므로, 계측 Maven 실행 전에 `maven-install-plugin:install-file`로 선택한 JACOV 플러그인을 로컬 저장소에 설치한다.
2. 고객 명령이 `-Dmaven.repo.local`을 지정했다면 그 저장소를 그대로 사용한다. 지정하지 않았다면 Maven의 일반 사용자 로컬 저장소(`~/.m2/repository`)를 사용하고 계측 명령에 별도 `-Dmaven.repo.local`을 강제하지 않는다.

   제품의 `utils/install_maven_plugin.cmd|sh`와 같이 `install-file`에 플러그인 좌표를 직접 전달해 Maven이 최소 POM을 생성하게 하며, 플러그인 JAR에 내장된 POM을 `-DpomFile`로 설치하지 않는다.
3. 제품의 `install_maven_plugin.cmd|sh` 흐름과 같이 설치본 `${CODESCROLL_TM_INSTALL_PATH}/bin/tm-javalib-lower.jar`도 같은 로컬 저장소에 등록하여 이후 실행에서 재사용할 수 있게 한다.

   런타임 JAR의 내장 좌표와 현재 플러그인이 조회하는 기존 `com.codescroll.tm` 좌표를 모두 등록하되 version은 JAR 메타데이터에서 읽고 고정하지 않는다.
4. `javaBuildRuntime.application=PLUGIN_DEPENDENCY`이고 `mode=LOWER`이면 `-Dmaven.jacov.addDependencyJavaLib=true`로 연결한다. 그 외에는 false로 중복 자동 추가를 끈다. install 명령, 플러그인 JAR SHA-256, 런타임 JAR SHA-256·좌표를 기록한다. 빌드 중 계측 클래스를 실행하면 아래의 `javaBuildRuntime` 절차로 해당 JVM에 LOWER 또는 Java Agent를 연결하고 결정을 기록한다. 로컬 저장소에 LOWER를 등록한 것만으로 테스트 JVM에서 사용할 수 있는 것은 아니다.
5. 원본 래퍼, settings, profiles, properties와 goal을 보존하고 JACOV `setup` goal을 원본 compile/package보다 앞에 추가한다. 원본 명령에 `clean`이 있으면 `clean → JACOV setup → package` 순서로 실행한다. `setup` 뒤에 `clean`을 실행하면 방금 생성한 `target/jacov`의 계측 소스가 삭제되어 컴파일할 소스가 사라진다. 원본에 없는 `clean`은 추가하지 않으며, 옵션 값으로 쓰인 `clean`을 goal로 취급하지 않는다.
6. `project.build.directory`, outputDirectory, finalName과 작업 디렉터리를 변경하지 않는다. 테스트 실행 여부를 원본 명령에서 바꾸지 않는다. `source:jar`, `skipTests`, lifecycle goal을 프로젝트 증거 없이 임의로 추가하지 않는다.
7. 성공 후 POM에 프로필이나 플러그인을 삽입하지 않는다. 사용자가 수동으로 반복할 수 있도록 플러그인이 설치된 로컬 저장소와 `com.qualityscroll.cover:jacov-maven-plugin:<실제-version>:setup`이 포함된 검증 명령을 `.cover-ee/coverage-commands.json`과 최종 요약에 남긴다.

같은 작업을 재개할 때 설치 명령·원본 JAR 해시가 같고 로컬 저장소의 JAR도 일치하면 `install-file`을 반복하지 않는다. 저장소 파일이 삭제되거나 변경됐으면 다시 설치한다. 커버리지 인자를 따로 결정할 이유가 없는 표준 경로에서는 실제 플러그인 좌표의 `setup` goal과 원본 인자 배열을 실행기가 결합한다.

### Gradle

1. 고객 빌드 스크립트를 수정하지 말고 작업 중에는 작업별 설치본 `jacov-gradle-init.gradle` 사본을 주입해 커버리지 빌드를 수행한다.
2. 사본의 플러그인 경로를 선택한 설치본 JAR의 절대 경로로 설정한다. LOWER의 PLUGIN_DEPENDENCY 선택이면 `jacov.addDependencyJavaLib=true`, `jacov.javaLibJarDir=<검증한 LOWER JAR의 부모 디렉터리>`를 설정한다. 다른 방식이면 false와 빈 경로로 자동 추가를 끈다. Java Agent의 기존 옵션 경로는 확인한 Test/JavaExec 태스크에 적용하며 그 외 실행은 실제 적용 지점을 확인해 BUILD_FILES로 연결한다.
3. 원본 Gradle 래퍼와 task에 `--init-script <job-init-script>`만 추가한다. `buildDir`, `destinationDirectory`, `archiveFileName` 등 산출물 경로를 바꾸는 속성은 추가하지 말고 원본 daemon/toolchain/task 선택을 보존한다.
4. 고객 빌드 스크립트나 설치본 init script는 수정하지 않는다. 적용된 두 속성과 빌드 중 실행 JVM의 런타임 계획을 해당 단계에 기록한다.
5. 성공 후 제품 수동 절차와 같이 선택한 `jacov-gradle-plugin-jre*.jar`와 검증된 `jacov-gradle-init.gradle`을 프로젝트 루트에 둔다. init script의 플러그인 경로는 함께 둔 JAR를 가리켜야 한다.
6. `settings.gradle[.kts]`나 `build.gradle[.kts]`에 init script를 강제 연결하지 않는다. 사용자가 기존 Gradle 명령에 `--init-script <프로젝트 루트>/jacov-gradle-init.gradle`을 추가해 반복 실행할 수 있도록 정확한 명령을 `.cover-ee/coverage-commands.json`과 최종 요약에 남긴다.

원본 로그 관찰용 init script는 커버리지 인자에서 제거하고, 같은 관찰 코드를 계측용 script에 넣는다. 원본 빌드에서 파악한 JDK와 산출물 경로는 보존하되 커버리지 로그의 관찰 출력을 후속 탐침·산출물 검증에 사용하지 않는다. 동일 관찰을 중복 주입하지 않는다. 기본 계측 인자는 원본 task·옵션과 생성한 init script로 자동 구성한다.

Gradle에는 Maven의 `setup` goal을 추가하지 않는다. 현재 JACOV Gradle 플러그인은 시스템 임시 디렉터리에 계측 소스를 만들고 `compileJava`가 계측 task를 먼저 실행하도록 연결한다. 따라서 일반적인 `clean build`는 Maven의 `setup clean package`처럼 `target/jacov`를 지우는 구조가 아니다. 고객이 `clean`의 삭제 범위를 별도로 확장한 경우에는 실제 task와 삭제 경로를 확인한다.

프로젝트 루트에 이전 스킬 실행이 만든 JAR나 init script가 있으면 현재 `.cover-ee/applied-state.json`의 소유 기록과 해시를 먼저 확인한다. 기록과 일치하는 파일만 새 검증본으로 갱신한다. 고객이 수정했거나 소유 기록이 없는 파일은 덮어쓰지 않는다. 적용 중 오류가 발생하면 이번에 쓴 내용의 해시를 확인한 뒤 이전 바이트로 복구한다.

### 빌드 결과 확인

1. 선택한 JACOV 방식으로 커버리지 빌드를 실제 수행하고 명령의 종료 코드를 기록한다.
2. Maven·Gradle 또는 JACOV CLI가 연결된 고객 빌드의 종료 코드가 0이면 성공으로 판정한다. CLI 실패가 후속 컴파일 성공에 가려지지 않도록 명령 구성에서 종료 코드를 전달한다.
3. JACOV 집계 marker, 파일별 success/failure/ignore, 업로드 로그를 파싱해 탐침 성공을 별도로 판정하지 않는다. 탐침 건수나 전체 소스 처리 완료를 보고하지 않는다.
4. 원본 출력 경로를 유지하도록 실행 전 계획을 검증한다. 빌드 후 JAR/WAR/classes의 갱신 여부나 `sameAsOriginal` 비교는 추가 성공 조건으로 사용하지 않는다.
5. 실패 로그와 소스·설정 복구 결과를 보존한다. 빌드 실패 후 원본 산출물 복구가 필요한 경우의 절차는 유지한다.

## 성공 판정

커버리지 빌드 명령의 종료 코드만으로 성공·실패를 판정한다. 재사용 설정 적용 결과는 별도로 보고한다. 프로그램 실행과 측정 데이터 확인은 별도 요청에 따라 수행하며, 빌드 성공을 측정 성공으로 표현하지 않는다.

## 빌드 중 테스트·프로그램 실행

`package`나 `build`는 컴파일·패키징뿐 아니라 테스트도 수행할 수 있다. JACOV 계측이 성공했더라도 이 테스트 JVM에 `ProbeStore`가 없으면 `ClassNotFoundException`으로 빌드가 실패한다. 이때 테스트를 생략하거나 빌드 목표를 `compile`로 바꾸지 않는다. 원본 명령은 유지하고 실행 JVM에 필요한 런타임을 제공한다.

원본 빌드 후 모델은 명령, 빌드 설정과 로그를 함께 읽고 `javaBuildRuntime`을 다른 결정 항목과 한 번에 작성한다. 형식은 [결정 문서](decision-contract.md#java-빌드-중-실행-런타임)를 따른다.

- 계측 코드를 실행하지 않는 빌드는 `NONE`과 그 근거를 기록한다. 이 경우 JavaLib Agent 파일은 필요하지 않다.
- 실행하는 빌드는 `JAVA_AGENT` 또는 `LOWER`, 실행 JVM의 Java 주 버전과 적용 위치를 기록한다. 빌드 도구를 구동한 Java 버전과 테스트 JVM 버전이 다를 수 있으므로 구분한다.
- 실행기는 `${CODESCROLL_TM_INSTALL_PATH}/java_agent/cover-javalib-agent.jar`의 존재, premain·`ProbeStore` 클래스, 해당 클래스의 바이트코드 버전과 제품 서버 URL 형식을 확인한다. 파일이 없으면 준비를 요청하며 설치본 `bin`의 미생성 JAR로 대체하지 않는다. 이 확인은 서버 연결이나 전체 의존성 호환성 검증을 대신하지 않는다.
- 생성 JAR의 서버 설정을 Open API 주소로 덮어쓰지 않는다. 모델은 제품이 생성한 JAR가 이번 실행 환경용인지 확인한다. 런타임이 실제 동작하면 측정 데이터를 전송할 수 있지만, 별도의 측정 결과 조회를 커버리지 빌드 성공 조건으로 추가하지 않는다.

적용 위치는 빌드 방식별로 구분한다.

- Maven: Surefire/Failsafe의 별도 JVM이 실제로 읽는 속성과 기존 값을 확인한다. 기본 설정이라면 `argLine`에 기존 옵션을 보존하고 `-javaagent`를 덧붙인다. POM이 `argLine`을 직접 고정하거나 모듈별 옵션이 다르면, 공통으로 추가할 수 있는 사용자 속성을 확인한다. 확인할 수 없거나 `forkCount=0`이면 적용하지 않고 결정을 요청한다. JaCoCo의 지연 치환과 다른 Java Agent 옵션을 보존하며 자기 자신을 참조하는 속성은 허용하지 않는다.
- Gradle: 확인한 `Test` 또는 `JavaExec` 태스크의 전체 경로를 `gradleJvmTasks`에 지정한다. 기존 `gradleTestTasks` 결정도 읽는다. 작업별 init script에서 해당 태스크의 기존 `jvmArgs`에 Agent를 추가한다. Gradle daemon에는 일괄 추가하지 않는다. 선택한 태스크가 Test/JavaExec인지 실행 시 검사한다. 다른 worker·launcher는 모델이 실제 JVM 옵션·classpath 적용 지점을 확인하고 BUILD_FILES로 연결한다.
- 직접 Java·빌드 스크립트: 고객 스크립트가 대상 Java 실행에만 전달하는 JVM 옵션 환경변수가 확인된 경우 그 변수에 추가한다. 해당 지점이 없으면 BUILD_FILES 결정으로 대상 java 호출의 JVM 옵션 또는 classpath를 수정한다. `JAVA_TOOL_OPTIONS`, `JDK_JAVA_OPTIONS`, `_JAVA_OPTIONS`, `MAVEN_OPTS`, `GRADLE_OPTS`로 전체 JVM에 적용하지 않는다.

기존 LOWER와 JavaLib Agent가 함께 적용되지 않는지 실행 설정에서 확인한다. 실행기는 전달받은 옵션의 LOWER 표기와 Gradle 테스트 classpath도 검사하지만, 임의 스크립트나 Maven 의존성 전체를 자동 판별하는 것은 아니다. 같은 JavaLib Agent 옵션은 중복 추가하지 않으며 다른 파일을 가리키면 중단한다.

빌드의 테스트 범위와 실패 처리 기준은 바꾸지 않는다. 원본 명령에 포함된 테스트가 실패하면 빌드 실패로 처리하고, 요청하지 않은 별도 테스트를 추가하지 않는다. 검증한 실행 인자·환경·init script는 이후 `.cover-ee` 재사용 명령에도 유지한다. 생성 Agent를 이동하거나 변경하면 재검증한다.

### 빌드·실행 방식별 적용 후보

한 가지 연결 방식으로 모든 Java 프로세스를 처리하지 않는다. 원본 명령, 실제 적용 JACOV goal/task, 프로젝트 의존성, JVM 옵션, 클래스로더 관계를 확인한 뒤 다음 후보를 비교한다.

| 실행 형태 | 모델이 확인할 사항 | 선택할 연결 방식 |
| --- | --- | --- |
| Maven 프로젝트 클래스패스를 쓰는 실행 | JACOV goal이 의존성을 추가하는지, plugin 의존성의 LOWER 좌표·버전, `maven.jacov.scope`, 실행 플러그인의 dependency scope | LOWER + PLUGIN_DEPENDENCY |
| Gradle 프로젝트 runtimeClasspath를 쓰는 Test/JavaExec | JACOV 적용 모듈과 실행 태스크, implementation 상속 여부, 별도 classpath 덮어쓰기 | LOWER + PLUGIN_DEPENDENCY |
| Maven Surefire/Failsafe 별도 JVM | 실제 argLine 속성, fork 여부, 기존 agent와 옵션 | JAVA_AGENT + EXISTING_OPTIONS |
| Gradle Test/JavaExec | 전체 태스크 경로와 기존 JVM 옵션·런타임 | JAVA_AGENT + EXISTING_OPTIONS |
| 이미 적절한 런타임이 연결된 실행 | 실제 JAR·버전·옵션·로드 경로가 선택한 제품 런타임과 일치하는지 | EXISTING; 자동 의존성 추가로 연결된 경우 PLUGIN_DEPENDENCY |
| 직접 java 명령 | 기존 명령의 실행 파일, JVM 옵션, classpath 값 위치 | JAVA_COMMAND; Agent 옵션 추가 또는 기존 classpath 확장 |
| Ant·배치·셸·직접 실행을 호출하는 빌드 파일 | 실행 java 명령, -cp/classpath·JVM 옵션·작업 디렉터리·인수 전달 | BUILD_FILES로 정확한 실행 지점 수정 |
| Maven 플러그인 realm, exec:java, Gradle buildscript/worker/daemon, 사용자 클래스 로더 | 프로젝트 의존성으로 전달되지 않는 경계와 계측 클래스를 로드하는 실제 로더 | 해당 로더의 dependency 또는 대상 JVM 시작 옵션을 BUILD_FILES로 연결; 근거가 없으면 모델 결정 보완 |
| java -jar, 모듈 실행, fat JAR·컨테이너 launcher | manifest·module path·번들링·클래스로더 격리 | 일반 -cp 추가를 가정하지 않고 실제 launcher 설정을 BUILD_FILES로 연결 |

Maven 제품 구현은 `maven.jacov.addDependencyJavaLib=true`일 때 플러그인 의존성에서 LOWER artifact를 찾아 프로젝트 dependency artifacts에 추가한다. scope는 기존 `maven.jacov.scope`를 유지하고 없으면 compile이다. 플러그인 자체 JVM의 realm까지 바뀐다고 해석하지 않는다. Gradle은 `jacov.javaLibJarDir`를 먼저 보고 설치 경로를 fallback으로 사용해 implementation에 JAR를 추가한다. JavaExec나 worker가 별도 classpath를 만들면 자동 의존성만으로 충분하지 않다. Android는 다른 런타임 artifact를 사용하므로 Java LOWER를 자동 대입하지 않는다.

LOWER 추가는 패키징 플러그인에 따라 배포 의존성이나 fat JAR 내용에도 영향을 줄 수 있다. 원래 패키징 계약과 실행 범위를 확인하고 필요하면 특정 실행만 수정한다. 여러 실행 JVM은 각각의 로더·옵션을 조사하고 선택한 연결이 모두에 유효한지 확인한다. 현재 실행기의 한 결정은 하나의 런타임 방식이며, 서로 다른 JVM에 서로 다른 런타임이 필수인 경우 이를 동일 JVM 혼용으로 처리하거나 한 가지로 강제하지 말고 적용 범위를 나눌 계획을 요청한다. 모델이 해석할 수 없는 launcher까지 지원 완료라고 보고하지 않는다.

### LOWER와 빌드 도구 JVM 판단

테스트뿐 아니라 버전 조회, 코드 생성, manifest 생성, 패키징에서 계측 클래스를 로드할 수 있다. `jar`나 `package`라는 목표 이름 또는 테스트 생략만으로 `NONE`을 선택하지 않는다. 별도 프로세스인지 빌드 도구 자체 JVM인지 구분하고 클래스 로딩 경로를 확인한다. 옵션을 직접 제어할 수 있으면 Java Agent, 제품 LOWER를 해당 로더에서 볼 수 있게 classpath/dependency를 연결할 수 있으면 LOWER를 검토한다. 둘 중 하나가 무조건 우선인 고정 규칙은 두지 않는다.

제품 소스에서 확인한 연결 규칙:

- JACOV의 `CoverageBufferInitializerType` 등은 계측 클래스의 로더를 통해 `com.qualityscroll.cover.runtime.ProbeStore`를 찾는다. JAR가 디스크나 Maven 저장소에 존재하는 것만으로는 충분하지 않다.
- LOWER의 `ProbeStore`는 `Offline.getProbes`로 연결된다. `Offline`은 `/jacoco-agent.properties`와 JVM 시스템 속성을 읽는다. `ConfigLoader`가 실제로 처리하는 접두사는 `jacoco-agent.`와 `CODESCROLL_TM.`이며 시스템 속성이 리소스보다 우선한다. 이 초기화 경로는 환경변수 map을 넘기지 않는다. 환경변수 이름을 그대로 전달하는 것으로 런타임 설정을 대신하지 않는다.
- 소스에 포함된 LOWER 기본 설정은 `output=dual`, `destfile=tm.offline.exec`, `port=9091`이다. 설치된 JAR나 실행 JVM의 설정은 다를 수 있으므로 모델이 확인한다. 실행만으로 파일 저장·전송이 발생할 수 있으며, 대상·출력 정책을 임의로 다른 서버로 바꾸지 않는다. Local Agent 전송을 요청한 경우 실제 endpoint와 listener 준비를 확인한다. JAR 구조 검사는 연결 성공을 증명하지 않는다.
- Java Agent의 `PreMain.premain`은 비어 있으며 단순히 임의 클래스 변환을 수행하는 구조로 설명하지 않는다. 제품이 생성한 Agent JAR의 `ProbeStore`와 내부 Sandbox·제품 설정을 사용하는 경로다. 검증된 생성 JAR를 대상 JVM 시작 옵션으로 전달한다.

BUILD_FILES 형식은 [결정 문서](decision-contract.md#런타임-연결을-위한-빌드-파일-변경)를 따른다. 기존 실행 옵션·의존성·타깃을 유지하고 원본 파일 해시와 정확한 변경을 제출한다. `java -jar`는 일반 `-cp` 추가만으로 클래스패스가 확장되지 않으므로 manifest·launcher·지원되는 JVM 옵션을 검토한다. 실제 로더가 LOWER를 볼 수 없는 경우 Agent 연결 가능성을 검토하고, 둘 다 확인할 수 없으면 필요한 설정을 보고한다. 빌드 설정의 의미와 런타임 가시성은 모델이 판단하고 실행기는 파일 범위와 변경·복구를 검증한다.

## 6. 실행 런타임 선택

커버리지 빌드와 런타임 선택을 분리한다. `cover-javalib-agent.jar`로 빌드한다고 표현하지 않는다.

빌드 중 실행은 위의 `javaBuildRuntime`으로 처리한다. 배포·수동 실행의 `javaRuntimeMode`까지 이 결정으로 확정하지 않는다. 계측 산출물을 별도로 실행할 때 실행 인자에 `-javaagent:...cover-javalib-agent.jar`가 있으면 `JAVA_AGENT`, lower dependency/classpath 증거가 있으면 `LOWER`로 판별한다.

둘 다 없으면 실행 환경과 사용자의 실행 방법을 확인하고, 두 방식의 증거가 동시에 있으면 충돌로 중단한다.

- `LOWER` 실행은 같은 호스트의 Local Agent가 현재 COVER 서버에 등록되어 있고 `online=true`이며 Java listener까지 준비됐는지 확인한 뒤 lower JAR을 실행 classpath/dependency에 추가한다.
- 실행 호스트가 다르거나 실행 위치마다 Local Agent를 둘 수 없고, JVM 명령에 `-javaagent`를 추가하며 생성된 agent JAR을 배포할 수 있으면 `JAVA_AGENT`를 추천한다.
- Agent가 등록되지 않았다는 이유만으로 `JAVA_AGENT`를 확정하지 않는다. 생성된 `${CODESCROLL_TM_INSTALL_PATH}/java_agent/cover-javalib-agent.jar`와 실행 명령 변경 가능성을 함께 확인한다.
- lower dependency와 `-javaagent:...cover-javalib-agent.jar`가 동시에 발견되면 `JAVA_RUNTIME_MODE_CONFLICT`로 중단한다.

별도 실행 안내에서는 생성 Agent JAR의 존재 여부를 기록한다. 빌드 중 계측 코드 실행이 없는 경우에는 파일 부재만으로 계측 빌드를 실패시키지 않는다. 반대로 `javaBuildRuntime=JAVA_AGENT` 계획이라면 빌드 전에 해당 파일이 필요하다. 사용자가 측정 실행까지 요청하면 생성 JAR 존재, 배포 위치, `-javaagent` 적용과 서버 데이터 갱신을 런타임 성공 조건으로 검증한다.

커버리지 빌드 성공 결과의 `executionGuidance`와 최종 채팅에는 선택을 강제하지 않고 두 실행 방법을 모두 제공한다.

- `localAgentLower`: `readiness=REQUIRES_RUNTIME_VALIDATION`, 설치본 `tm-javalib-lower.jar`의 절대 경로, 성공한 설치 기록에 남은 실제 Maven 좌표 배열, 실행 시 클래스패스 또는 실행 의존성에 정확히 한 번 추가한다는 설명과 main-class 명령 형식. 로컬 저장소 등록만으로 애플리케이션 런타임에 포함된 것은 아니라고 명시한다.

  일반 `java -jar`에는 추가 classpath를 적용할 수 있는 것처럼 안내하지 말고 launcher·manifest 또는 main-class 실행 방식 확인이 필요하다고 표시한다.
- `javaLibAgent`: 생성본 `${CODESCROLL_TM_INSTALL_PATH}/java_agent/cover-javalib-agent.jar`의 절대 경로·현재 존재 여부, 기존 JVM 실행 명령에 적용할 `-javaagent:<생성본>` 옵션과 main-class/`-jar` 명령 형식. 생성본이 없으면 `readiness=REQUIRES_PREPARATION`, 있더라도 서버 일치·배포·JVM 옵션 적용을 아직 검증하지 않았으면 `REQUIRES_RUNTIME_VALIDATION`으로 표시한다.
- 공통: `coverageBuildRuntimeInjected`에 이번 빌드의 적용 여부를 기록하고 `buildRuntime`에 대상·근거·Agent 해시를 남긴다. 별도 실행 안내는 `selection=CHOOSE_ONE_AT_EXECUTION`으로 제공하며 두 런타임을 동시에 적용하지 않는다고 명시한다.

프로젝트의 실제 메인 클래스, 실행 가능한 JAR, application 플러그인 task 또는 서비스 실행 명령가 빌드 증거에서 확인되지 않았다면 임의 실행 대상을 만들지 말고 `kind=NON_EXECUTABLE_TEMPLATE`과 `<MAIN_CLASS>`, `<APPLICATION_JAR>`, `<existing-runtime-classpath>` 자리표시자를 사용한다.

기존 Java 실행 파일, JVM 옵션과 애플리케이션 인자를 보존하는 형식으로 표시하고 현재 OS의 클래스패스 구분자를 함께 기록한다.

### 표준 `tm-javalib` 런타임

측정 대상 JVM이 연결할 수 있는 COVER Local Agent가 실행 환경에 있고 `CODESCROLL_TM_JAVA_PORT`로 연결할 수 있을 때 사용한다. JACOV 플러그인의 `addDependencyJavaLib=true` 또는 동등한 고객 dependency로 `tm-javalib-lower.jar`가 실행 시 클래스패스에 정확히 한 번 포함됐는지 확인한다.

### `cover-javalib-agent.jar` 직접 전송 런타임

MSA 등 분산 실행 환경에서 각 JVM 옆에 Local Agent를 배치하기 어렵고 Java 인스턴스가 COVER 서버로 직접 전송해야 할 때 사용한다.

1. Local Agent 시작 후 생성된 `${CODESCROLL_TM_INSTALL_PATH}/java_agent/cover-javalib-agent.jar`를 사용한다. 원본 `${CODESCROLL_TM_INSTALL_PATH}/bin/cover-javalib-agent.jar`를 직접 배포하지 않는다.
2. 생성 JAR 내부 `cover-config.properties`의 서버 URL이 현재 서버와 일치하는지 비밀값을 출력하지 않고 확인한다.
3. 검증된 고객 JVM 명령의 main class 또는 `-jar` 앞에 다음 옵션을 추가한다.

```text
-javaagent:<배포된-cover-javalib-agent.jar>
```

4. 컨테이너·원격 호스트에 JAR를 복사/마운트하거나 배포 manifest를 바꾸는 일은 사용자가 측정 실행을 요청하고 대상·배포 방식이 확정된 경우에만 수행한다.
5. `tm-javalib-lower.jar`와 `cover-javalib-agent.jar`를 근거 없이 동시에 로드하지 않는다. 런타임 방식을 하나로 확정한다.

Docker/원격 실행이라는 사실만으로 직접 전송 런타임을 선택하지 않는다. 다음 질문으로 판정한다.

> 측정 대상 Java 프로세스가 연결할 수 있는 COVER Local Agent가 실행 환경마다 존재하는가?

불명확하면 `WAITING_USER`로 두고 Agent 배치와 네트워크 경로를 요청한다.

## 7. 측정 데이터 검증

사용자가 실행 또는 데이터 확인까지 요청했을 때만 검증된 테스트/실행 명령을 수행한다.

- 표준 런타임: Local Agent Java 연결과 해당 Module의 새 측정 데이터 업로드를 확인한다.
- 직접 전송 런타임: COVER 모니터링의 연결된 Java Agent, 상태 보고, 코드 소스, 실행 경로, 실행 클래스 수를 확인한다.
- 두 방식 모두 계측 클래스를 실제로 실행한 뒤 해당 Module의 측정 시각과 커버리지 값이 현재 실행으로 갱신됐는지 확인한다.

`cover-javalib-agent.jar`의 JVM `premain` 진입만으로는 성공이 아니다. 계측 클래스가 로드돼 런타임이 초기화되고 서버 데이터가 갱신돼야 한다.

## 실행 시간 최적화

원본 goal·task·프로필·테스트 옵션은 커버리지 명령에서도 보존한다. 모델이 `javaCoverageArguments`를 지정해도 원본 인수의 제거·재정렬은 허용하지 않는다. JACOV 연결 외의 옵션을 새로 추가하려면 현재 작업의 `coverage-java` 실패 로그와 변경 이유를 `coverageOptionRecovery`로 제출한다. 기존 성공 프로필이 재사용 검사를 통과한 경우에는 동일한 보완 옵션을 그대로 사용한다. `--max-workers 2`처럼 옵션과 값을 나눠 전달하는 형식도 지원한다. 실패를 피하려고 테스트를 새로 생략하거나 모듈을 제외하지 않는다. 원본에 이미 있던 생략 옵션은 유지한다. 상세 형식은 [실패 후 추가 옵션 보완](decision-contract.md#실패-후-추가-옵션-보완)을 따른다. JACOV 플러그인 연결과 선택한 Java 런타임 설정은 정상 절차이므로 실패 근거를 요구하지 않는다.

- C#/Java 프로젝트 조사에서 Maven/Gradle 진입점, JDK/toolchain, goal·task, 모듈, 프로필, 소스 묶음 후보를 묶어서 확정한다. 같은 `pom.xml`이나 Gradle 파일을 항목별로 반복 해석하지 않는다.
- 유효한 `cover_jacov.json`이 있으면 Java 커버리지 빌드 중 Local Agent 조회·등록·시작을 수행하지 않는다. Java 런타임 실행을 요청한 경우에만 선택한 런타임 방식에 필요한 Agent 준비를 수행한다.
- 고객 Maven 명령의 `-T`, Gradle 명령의 `--parallel`·worker 설정은 그대로 보존한다. 프로젝트의 task 안전성이 확인되지 않은 상태에서 속도를 위해 새 병렬 옵션을 자동 추가하지 않는다.
- 원본 빌드 한 번에서 실제 JDK·컴파일러 방식, source·target·release, JAR/WAR/classes 출력 경로를 함께 기록하고 JACOV 계획에서 재사용한다.
- Open API 요청에는 같은 실행기 프로세스의 메모리에 보관한 Access Key를 재사용한다. Java 빌드 키는 이 Access Key와 별개의 값이며, 두 키를 포함한 비밀값은 단계 JSON이나 최종 보고에 남기지 않는다. 입력·사용·보관 방식은 [인증 문서](security-and-api.md#현재-인증)를 따른다.
