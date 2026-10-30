# Java / JACOV

## 1. 환경과 빌드 방식 확정

1. `CODESCROLL_TM_SERVER_URL`, `CODESCROLL_TM_INSTALL_PATH`, 프로젝트 절대 경로를 공통 시작 조건으로 확인한다. 최초 요청에 Access Key가 없으면 채팅으로 먼저 받고 메모리에만 보관하되, Java 원본 빌드가 끝난 뒤 첫 COVER 서버 작업 직전에만 사용한다.
2. `${SKILL_ROOT}/scripts/project_discovery.py managed --project-root <경로>`의 Java 증거와 고객 스크립트·CI를 읽고 다음 중 실제 진입점을 선택한다. `buildSystemCandidates`는 파일 기반 후보이고 `confirmedBuildSystems`는 수집기가 제한된 고객 빌드/CI 진입 파일에서 실제 실행 토큰을 찾은 결과다.
   - Maven 후보: 선택 프로젝트의 `pom.xml`, `mvnw`, `mvnw.cmd`
   - Maven 확인: 고객 빌드 스크립트, CI, 기존 빌드 로그 또는 사용자 원본 명령에서 `mvn`, `mvnw`, `mvnw.cmd` 호출이 확인되고 해당 명령의 goal, 프로필, 모듈, `-f` 대상, 작업 디렉터리를 수집한 경우
   - Gradle 후보: `build.gradle[.kts]`, `settings.gradle[.kts]`, `gradlew`, `gradlew.bat`
   - Gradle 확인: 고객 빌드 스크립트, CI, 기존 빌드 로그 또는 사용자 원본 명령에서 `gradle`, `gradlew`, `gradlew.bat` 호출과 task가 확인된 경우
   - 직접 빌드(Command Line): 명시적인 `javac`/패키징 스크립트와 소스 루트
3. Maven/Gradle이 설치돼 있거나 PATH에서 발견된 사실, `mvn --version`/`gradle --version` 성공, `pom.xml`/Gradle 파일 존재만으로 실제 빌드 방식을 확정하지 않는다. Maven과 Gradle이 함께 있거나 명령 증거가 없거나 여러 모듈의 빌드 진입점·소스 묶음·goal·task가 모호하면 정형 자동 판정을 중단하고 모델이 프로젝트 관계를 해석한다.

   저장소에도 고객의 실제 선택 근거가 없을 때만 사용자에게 원본 빌드 명령을 요청한다.

   Gradle의 `settings.gradle[.kts]`가 하나이고 다른 Gradle 빌드 파일이 그 디렉터리 아래에만 있으면, 실행기는 해당 settings를 루트 진입점 후보 하나로 정리한다. 여러 settings가 있으면 합성 빌드나 독립 프로젝트 관계를 모델이 확인한다. task는 진입점 파일의 존재만으로 추측하지 않는다. 아직 선택하지 않은 task와 다른 미결정 항목은 원본 빌드 전 결정 파일에 함께 담는다.

   Maven POM이 여러 개 있으면 각 POM의 `<modules>` 관계를 확인한다. 하나의 POM이 발견된 모든 하위 POM을 직접 또는 간접으로 포함하면 해당 POM을 reactor 루트로 사용한다. 서로 독립된 POM이 있거나 XML을 읽을 수 없으면 진입점을 자동으로 고르지 않는다. reactor 루트를 확인했더라도 goal은 POM만 보고 추측하지 않으며, 고객 명령이나 모델이 확정한 값을 사용한다.
4. Dockerfile, Compose, Kubernetes, Helm, 배포 스크립트는 프로그램 배치와 연결 구조를 보여주는 증거일 뿐 MSA 또는 JavaLib Agent 사용을 확정하는 근거가 아니다.

## 2. 원본 빌드와 실제 Java 툴체인

1. JACOV 설정이 없는 상태에서 고객의 원본 clean/build를 먼저 수행하고 성공 여부를 확인한다.
2. Maven/Gradle 래퍼, `JAVA_HOME`, 툴체인 설정, CI 이미지, verbose/debug 로그를 함께 분석해 실제 JDK 설치 경로, Java 실행 파일, 컴파일러 source·target·release를 기록한다.
3. 별도 프로세스로 실행된 `javac`가 있으면 실제 절대 경로와 명령을 수집한다. 컴파일러 API 또는 데몬 내부 컴파일러를 사용하면 존재하지 않는 `javac` 프로세스를 요구하지 말고 선택된 JDK·툴체인과 컴파일러 실행 방식을 증거로 기록한다.
4. 원본 빌드 goal·task, 작업 디렉터리, 프로필, 로컬 저장소, daemon, 산출물(JAR/WAR/classes)을 보존한다. 모델이 임의로 Maven과 Gradle을 바꾸거나 task를 추가하지 않는다.
5. 원본 빌드가 실패하면 JACOV 빌드를 진행하지 않는다.
6. Maven 원본 빌드에는 비밀값과 전체 시스템 속성을 노출할 수 있는 `-X`를 기본 추가하지 않는다. 필요한 JDK 식별은 `-V`, 툴체인 출력과 실제 컴파일 증거로 수집하고, 고객이 이미 사용한 상세 옵션은 그대로 보존한다.

Maven `-V` 출력의 `runtime:` 경로는 실제 실행에 사용된 JDK 후보로 수집한다. 해당 경로에 `bin/java`와 `bin/javac`가 모두 있을 때만 JDK 홈으로 인정한다. 따라서 Maven이 이미 출력한 경로를 다시 사용자에게 묻지 않으며, JRE 경로나 우연히 같은 이름을 가진 디렉터리를 JDK로 잘못 선택하지 않는다.

Gradle 원본 빌드에는 작업별 `observe-gradle.gradle`을 추가한다. 이 스크립트는 `JavaCompile` 실행 시 실제 compiler metadata의 JDK 경로와 버전을, archive 작업 완료 시 실제 산출물 경로를 로그로 남긴다. 고객 빌드 파일이나 task의 컴파일 옵션을 바꾸지 않는다. 실행기는 `COVER_OBS_JAVA_HOME`, `COVER_OBS_JAVA_VERSION`, `COVER_OBS_ARTIFACT`를 읽어 후속 항목을 함께 확정한다. 서로 다른 JDK가 관찰되면 임의로 하나를 고르지 않고 모델 결정 묶음에 포함한다.

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
6. JACOV CLI/Maven/Gradle 계측은 `cover_jacov.json.url`과 빌드 키로 COVER 서버의 Java 빌드 서비스에 직접 연결한다. 계측 로그의 서버 연결과 분석정보 업로드로 준비 상태를 확인하고 Local Agent `online=true`를 성공 조건으로 사용하지 않는다.
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
- 빌드 후 `${SKILL_ROOT}/scripts/cover_build_executor.py verify-output-paths --original-artifact <원본 경로> --coverage-artifact <커버리지 경로>`를 실행하고 `sameAsOriginal=true`를 확인한다. 비교 결과를 커버리지 단계 JSON에 기록한다.
- 커버리지 산출물은 커버리지 로그 또는 현재 커버리지 빌드 시간 구간에 실제 갱신된 파일에서 다시 수집한다. 원본 산출물 배열을 커버리지 배열로 재사용해 자기 비교하지 않는다.

### 직접 빌드(Command Line)

1. 고객 소스 루트마다 계측 소스를 준비하되, 작업별 임시 출력 디렉터리를 최종 class/JAR/WAR 산출물 위치로 사용하지 않는다. 다음 형태는 계측 소스 임시 위치를 나타내며 최종 패키징은 원본 출력 경로로 수행해야 한다.

```text
java -jar <cover-jacov-cli> <source-root> --output=<instrumented-source-root> [include/exclude/replaceSrcDir]
```

2. 원본 소스를 덮어쓰지 않는다. 생성된 소스 디렉터리와 원본 리소스를 사용하도록 원본 `javac`/패키징 계획을 모델이 정해진 항목으로 나누어 작성한다.
3. 원본 클래스패스, 어노테이션 프로세서, 생성 소스, 인코딩, 모듈 경로, 컴파일러 플래그, 작업 디렉터리와 원본 출력 디렉터리를 보존할 수 없으면 Command Line 자동화를 중단하고 사용자 빌드 명령을 요청한다.

### Maven

1. 선택한 JACOV Maven 플러그인 JAR의 내장 `pom.properties`와 `pom.xml`에서 실제 `groupId:artifactId:version`을 읽는다. Maven은 실행 안에서 플러그인을 등록하기 전에 플러그인 POM을 해석하므로, 계측 Maven 실행 전에 `maven-install-plugin:install-file`로 선택한 JACOV 플러그인을 로컬 저장소에 설치한다.
2. 고객 명령이 `-Dmaven.repo.local`을 지정했다면 그 저장소를 그대로 사용한다. 지정하지 않았다면 Maven의 일반 사용자 로컬 저장소(`~/.m2/repository`)를 사용하고 계측 명령에 별도 `-Dmaven.repo.local`을 강제하지 않는다.

   제품의 `utils/install_maven_plugin.cmd|sh`와 같이 `install-file`에 플러그인 좌표를 직접 전달해 Maven이 최소 POM을 생성하게 하며, 플러그인 JAR에 내장된 POM을 `-DpomFile`로 설치하지 않는다.
3. 제품의 `install_maven_plugin.cmd|sh` 흐름과 같이 설치본 `${CODESCROLL_TM_INSTALL_PATH}/bin/tm-javalib-lower.jar`도 같은 로컬 저장소에 등록하여 이후 실행에서 재사용할 수 있게 한다.

   런타임 JAR의 내장 좌표와 현재 플러그인이 조회하는 기존 `com.codescroll.tm` 좌표를 모두 등록하되 version은 JAR 메타데이터에서 읽고 고정하지 않는다.
4. 커버리지 빌드에는 런타임을 주입하지 말고 항상 `-Dmaven.jacov.addDependencyJavaLib=false`를 적용한다. install 명령, 플러그인 JAR SHA-256, 런타임 JAR SHA-256·좌표와 런타임 선택이 실행 단계로 연기됐다는 사실을 해당 단계의 증거로 남긴다.
5. 원본 래퍼, settings, profiles, properties와 goal을 보존하고 JACOV `setup` goal을 원본 compile/package보다 앞에 추가한다.
6. `project.build.directory`, outputDirectory, finalName과 작업 디렉터리를 변경하지 않는다. 테스트 실행 여부를 원본 명령에서 바꾸지 않는다. `source:jar`, `skipTests`, lifecycle goal을 프로젝트 증거 없이 임의로 추가하지 않는다.
7. 성공 후 POM에 프로필이나 플러그인을 삽입하지 않는다. 사용자가 수동으로 반복할 수 있도록 플러그인이 설치된 로컬 저장소와 `com.qualityscroll.cover:jacov-maven-plugin:<실제-version>:setup`이 포함된 검증 명령을 `.cover-ee/coverage-commands.json`과 최종 요약에 남긴다.

같은 작업을 재개할 때 설치 명령·원본 JAR 해시가 같고 로컬 저장소의 JAR도 일치하면 `install-file`을 반복하지 않는다. 저장소 파일이 삭제되거나 변경됐으면 다시 설치한다. 커버리지 인자를 따로 결정할 이유가 없는 표준 경로에서는 실제 플러그인 좌표의 `setup` goal과 원본 인자 배열을 실행기가 결합한다.

### Gradle

1. 고객 빌드 스크립트를 수정하지 말고 작업 중에는 작업별 설치본 `jacov-gradle-init.gradle` 사본으로 계측을 검증한다.
2. 사본의 플러그인 경로를 선택한 설치본 JAR의 절대 경로로 설정한다. 커버리지 빌드에서는 런타임을 선택하지 않고 `jacov.addDependencyJavaLib=false`, `jacov.javaLibJarDir=`로 유지한다.
3. 원본 Gradle 래퍼와 task에 `--init-script <job-init-script>`만 추가한다. `buildDir`, `destinationDirectory`, `archiveFileName` 등 산출물 경로를 바꾸는 속성은 추가하지 말고 원본 daemon/toolchain/task 선택을 보존한다.
4. 고객 빌드 스크립트나 설치본 init script는 수정하지 말고 적용된 두 속성과 런타임 선택이 실행 단계로 연기됐다는 사실을 해당 단계의 증거에 기록한다.
5. 성공 후 제품 수동 절차와 같이 선택한 `jacov-gradle-plugin-jre*.jar`와 검증된 `jacov-gradle-init.gradle`을 프로젝트 루트에 둔다. init script의 플러그인 경로는 함께 둔 JAR를 가리켜야 한다.
6. `settings.gradle[.kts]`나 `build.gradle[.kts]`에 init script를 강제 연결하지 않는다. 사용자가 기존 Gradle 명령에 `--init-script <프로젝트 루트>/jacov-gradle-init.gradle`을 추가해 반복 실행할 수 있도록 정확한 명령을 `.cover-ee/coverage-commands.json`과 최종 요약에 남긴다.

원본 로그 관찰용 init script는 커버리지 인자에서 제거하고, 같은 관찰 코드를 계측용 script에 넣는다. 원본·계측 로그에서 JDK와 산출물을 각각 확인하되 동일 관찰을 중복 주입하지 않는다. 기본 계측 인자는 원본 task·옵션과 생성한 init script로 자동 구성한다.

프로젝트 루트에 이전 스킬 실행이 만든 JAR나 init script가 있으면 현재 `.cover-ee/applied-state.json`의 소유 기록과 해시를 먼저 확인한다. 기록과 일치하는 파일만 새 검증본으로 갱신한다. 고객이 수정했거나 소유 기록이 없는 파일은 덮어쓰지 않는다. 적용 중 오류가 발생하면 이번에 쓴 내용의 해시를 확인한 뒤 이전 바이트로 복구한다.

### 공통 검증

1. JACOV 로그에서 선택 소스 수, success/failure/ignore 수, 업로드 결과를 확인한다. 일부 소스 실패를 전체 성공으로 보고하지 않는다.
2. 커버리지 빌드 후 원본과 같은 JAR/WAR/classes 산출물 집합이 이번 작업에서 새로 생성 또는 변경됐는지 확인한다.
3. 원본·커버리지 출력 절대 경로 비교 결과가 `sameAsOriginal=true`인지 확인한다. 하나라도 다르면 `COVERAGE_OUTPUT_PATH_CHANGED`로 중단한다.
4. 선택한 계측 대상 소스마다 JACOV 처리 결과를 기록하고 exclude/ignore 대상은 별도로 기록한다.

### JACOV 처리 결과 확인

파일이 계측 대상에서 제외된 경우와 JACOV 계측을 수행하지 못한 경우를 구분한다. 제외된 파일 수만으로 성공이나 실패를 판정하지 않는다.

JACOV 설치본이 집계 결과를 출력하면 `JAVA SOURCE COVERAGE BUILD RESULTS`의 success/failure/ignore를 사용한다. 집계가 없으면 `[JACOV]`, `[COVER] Instrumenting`, `uploadJavaAnalyzeInfo` 등 관련 로그에서 JACOV 실행 문맥을 확인한 뒤, 그 문맥에 속한 `.java` 파일별 `[SUCCESS]`/`[FAILURE]`/`[IGNORE]` 결과를 사용한다. 제품명이나 출처가 불명확한 success 한 줄만으로 계측을 확인했다고 하지 않는다.

- `[SUCCESS]`는 계측 성공 건수로 기록한다.
- `[IGNORE]`는 파일 제외 건수로 기록한다. 제외된 파일이 있다는 이유만으로 실패 처리하지 않는다.
- `[FAILURE]`가 있으면 계측 실패로 처리한다. 다른 파일의 성공 건수가 있더라도 이 실패를 무시하지 않는다.
- `Skipping jacov instrumentation`, `Instrumentation skipped`, `No valid source directories`는 현재 파서가 계측 생략으로 판정하는 문구다. 이 문구가 있으면 파일별 제외와 구분해 계측 검증을 실패 처리한다.
  - 일반적인 테스트 생략이나 다른 작업의 skip 문구까지 JACOV 실패로 해석하지 않는다.
- 계측 오류도 별도로 확인한다. 현재 파서는 `[COVER]` 또는 `[COVER(Jacov)]` 표시 뒤에 error·invalid가 나오거나, `COVER(Jacov)` 뒤에 failed가 나오는 로그를 오류로 판정한다.

집계와 파일별 결과 모두 계측 성공이 1건 이상이고 계측 실패·생략·오류가 없어야 JACOV 처리 검증을 통과한다. 계측 성공을 확인하지 못하거나 실패·생략·오류가 있으면 `JACOV_PROCESSING_NOT_PROVED`로 중단한다.

예를 들어 실패·생략·오류가 없는 상태에서 성공 3건과 제외 2건이 확인되면 처리 검증을 통과한다. 성공 0건과 제외 5건만 확인되면 통과하지 못한다. 후자는 제외가 오류이기 때문이 아니라 실제 계측 성공을 확인하지 못했기 때문이다.

이 검증의 통과만으로 커버리지 빌드 전체를 성공 처리하지 않는다. 빌드 성공과 원본 경로의 신규 산출물도 아래 기준에 따라 확인한다.

## 성공 판정

Java 커버리지 빌드는 다음이 모두 참일 때 성공이다.

- 원본 빌드와 실제 JDK·컴파일러 방식을 확인했다.
- 선택한 JACOV CLI/plugin이 모든 대상 소스를 처리했다.
- Maven/Gradle 계측 빌드가 종료 코드 0으로 성공했다.
- [JACOV 처리 결과 확인](#jacov-처리-결과-확인)을 통과했다. 파일 제외는 별도로 기록하되, 실제 계측 성공과 계측 실패·생략·오류가 없음을 확인했다.
- 원본과 같은 최종 산출물이 이번 작업에서 새로 생성 또는 변경됐다.
- 최종 산출물의 절대 경로가 원본과 완전히 같다(`sameAsOriginal=true`).

측정 데이터 성공은 별도 판정으로 두고 런타임 연결, 계측 클래스 실행, Module 데이터 갱신까지 확인한다.

## 6. 실행 런타임 선택

커버리지 빌드와 런타임 선택을 분리한다. `cover-javalib-agent.jar`로 빌드한다고 표현하지 않는다.

커버리지 빌드에서는 `javaRuntimeMode`를 선택하지 않는다. 계측 산출물을 실제로 실행할 때 실행 인자에 `-javaagent:...cover-javalib-agent.jar`가 있으면 `JAVA_AGENT`, lower dependency/classpath 증거가 있으면 `LOWER`로 판별한다.

둘 다 없으면 실행 환경과 사용자의 실행 방법을 확인하고, 두 방식의 증거가 동시에 있으면 충돌로 중단한다.

- `LOWER` 실행은 같은 호스트의 Local Agent가 현재 COVER 서버에 등록되어 있고 `online=true`이며 Java listener까지 준비됐는지 확인한 뒤 lower JAR을 실행 classpath/dependency에 추가한다.
- 실행 호스트가 다르거나 실행 위치마다 Local Agent를 둘 수 없고, JVM 명령에 `-javaagent`를 추가하며 생성된 agent JAR을 배포할 수 있으면 `JAVA_AGENT`를 추천한다.
- Agent가 등록되지 않았다는 이유만으로 `JAVA_AGENT`를 확정하지 않는다. 생성된 `${CODESCROLL_TM_INSTALL_PATH}/java_agent/cover-javalib-agent.jar`와 실행 명령 변경 가능성을 함께 확인한다.
- lower dependency와 `-javaagent:...cover-javalib-agent.jar`가 동시에 발견되면 `JAVA_RUNTIME_MODE_CONFLICT`로 중단한다.

`JAVA_AGENT` 선택 시 생성 agent JAR의 현재 존재 여부를 확인 근거에 기록하되, 파일 부재만으로 계측 빌드를 실패시키지 않는다. 사용자가 측정 실행까지 요청했을 때만 생성 JAR 존재, 배포 위치, `-javaagent` 적용과 서버 데이터 갱신을 런타임 성공 조건으로 검증한다.

커버리지 빌드 성공 결과의 `executionGuidance`와 최종 채팅에는 선택을 강제하지 않고 두 실행 방법을 모두 제공한다.

- `localAgentLower`: `readiness=REQUIRES_RUNTIME_VALIDATION`, 설치본 `tm-javalib-lower.jar`의 절대 경로, 성공한 설치 기록에 남은 실제 Maven 좌표 배열, 실행 시 클래스패스 또는 실행 의존성에 정확히 한 번 추가한다는 설명과 main-class 명령 형식. 로컬 저장소 등록만으로 애플리케이션 런타임에 포함된 것은 아니라고 명시한다.

  일반 `java -jar`에는 추가 classpath를 적용할 수 있는 것처럼 안내하지 말고 launcher·manifest 또는 main-class 실행 방식 확인이 필요하다고 표시한다.
- `javaLibAgent`: 생성본 `${CODESCROLL_TM_INSTALL_PATH}/java_agent/cover-javalib-agent.jar`의 절대 경로·현재 존재 여부, 기존 JVM 실행 명령에 적용할 `-javaagent:<생성본>` 옵션과 main-class/`-jar` 명령 형식. 생성본이 없으면 `readiness=REQUIRES_PREPARATION`, 있더라도 서버 일치·배포·JVM 옵션 적용을 아직 검증하지 않았으면 `REQUIRES_RUNTIME_VALIDATION`으로 표시한다.
- 공통: `coverageBuildRuntimeInjected=false`, `selection=CHOOSE_ONE_AT_EXECUTION`을 기록하고 두 런타임을 동시에 적용하지 않는다고 명시한다.

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

- C#/Java 프로젝트 조사에서 Maven/Gradle 진입점, JDK/toolchain, goal·task, 모듈, 프로필, 소스 묶음 후보를 묶어서 확정한다. 같은 `pom.xml`이나 Gradle 파일을 항목별로 반복 해석하지 않는다.
- 유효한 `cover_jacov.json`이 있으면 Java 커버리지 빌드 중 Local Agent 조회·등록·시작을 수행하지 않는다. Java 런타임 실행을 요청한 경우에만 선택한 런타임 방식에 필요한 Agent 준비를 수행한다.
- 고객 Maven 명령의 `-T`, Gradle 명령의 `--parallel`·worker 설정은 그대로 보존한다. 프로젝트의 task 안전성이 확인되지 않은 상태에서 속도를 위해 새 병렬 옵션을 자동 추가하지 않는다.
- 원본 빌드 한 번에서 실제 JDK·컴파일러 방식, source·target·release, JAR/WAR/classes 출력 경로를 함께 기록하고 JACOV 계획에서 재사용한다.
- Open API 요청에는 같은 실행기 프로세스의 메모리에 보관한 Access Key를 재사용한다. Java 빌드 키는 이 Access Key와 별개의 값이며, 두 키를 포함한 비밀값은 단계 JSON이나 최종 보고에 남기지 않는다. 입력·사용·보관 방식은 [인증 문서](security-and-api.md#현재-인증)를 따른다.
