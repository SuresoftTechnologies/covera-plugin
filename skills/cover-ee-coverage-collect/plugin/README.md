# CDT 수집 플러그인 개발

이 디렉터리는 IDE 안에서 실행되는 Java 플러그인의 원본이다. Python 수집 클라이언트와 같은 저장소에서 관리한다. 고객에게는 `assets/plugin`의 빌드된 JAR를 배포하므로 고객 PC에 JDK나 빌드 도구를 설치할 필요는 없다.

## 역할

- `Startup.java`: Eclipse 시작 확장으로 수집 기능을 활성화한다.
- `Activator.java`: 플러그인 시작·종료 시 연동 작업의 수명을 관리한다.
- `Bridge.java`: 현재 작업 공간의 비공개 요청 폴더에서 세션 조회와 수집 요청을 처리한다. 네트워크 서버나 임의 GDB 명령 실행 기능은 제공하지 않는다.
- `DsfCapture.java`: 기존 DSF 세션을 정지하고 `::sdump_buf` 타입·크기를 확인한 뒤 메모리를 읽는다. 모든 DSF 호출은 해당 세션 executor에서 수행한다. 기다리는 작업은 별도 worker에서 수행하므로 IDE UI나 DSF executor를 막지 않는다.
- `BufferRules.java`: CDT가 보고한 배열 여부와 실제 메모리 크기를 검사한다. CDT child의 청크 표시 타입이나 `typeId`에는 의존하지 않으며 독립 Java 테스트로 검증한다.
- `build_plugin.py`: 지정한 IDE의 번들로 컴파일하고 배포 JAR 및 `release.json`을 생성한다.

여러 IDE 버전을 위한 Java 11 호환 소스도 같은 디렉터리에서 관리한다. 현재 고객용 JAR는 기존 검증본을 유지한다. 별도 후보 빌드 방법과 구버전 API 컴파일 결과는 [IDE 호환성](../references/ide-compatibility.md)에 정리한다. 이 변경 소스로 새 배포본을 확정하기 전에는 고객용 JAR를 다시 빌드해 덮어쓰지 않는다.

## 빌드

```text
python "${SKILL_ROOT}/plugin/build_plugin.py" --ide-home <STM32CubeIDE 설치 경로> --jdk-home <JDK 17 이상 경로>
```

외부 Maven 저장소를 사용하지 않고 대상 IDE의 `plugins`에 있는 API로 컴파일한다. 결과는 Java 17 바이트코드다. `plugin.xml`의 startup 확장이 있어야 IDE 시작 후 요청을 받을 수 있다. JAR를 복사했다는 사실만으로 서비스 시작을 증명하지 않는다.

`assets/plugin/release.json`에는 JAR 해시, 소스별 해시, 빌드 시점 Git HEAD(저장소가 없으면 null), 컴파일 대상 번들을 기록한다. 미커밋 변경이 있으면 Git HEAD만으로 빌드 소스를 설명할 수 없으므로 소스별 해시를 함께 사용한다. 동일 소스를 빌드하면 JAR ZIP 항목 시각을 고정해 같은 바이트를 생성한다.

## 검증과 호환성

다음 테스트는 통합 저장소 루트에서 실행한다. 테스트는 배포 ZIP이 아니라 `plugins/cover-build-measure-agent/tests/coverage_collect/`에 있다.

```text
python -m unittest discover -s plugins/cover-build-measure-agent/tests/coverage_collect -v
```

Python 테스트는 프로토콜·검증·전달·실패 경로를 확인한다. 보드와 연결된 DSF 서비스는 모의 테스트로 대체해 성공 판정하지 않는다. 실제 검증은 [CDT 연동](../references/stm32-cdt.md)의 절차를 따른다.

초기 컴파일 대상은 STM32CubeIDE 1.19.0의 DSF 2.12.100, DSF GDB 7.1.300이다. 허용 버전 범위는 manifest에서 관리하고 빌드 스크립트가 `release.json.compatibleBundles`로 추출한다. `compiledAgainst`는 빌드 출처 기록으로만 사용한다. 제한된 범위 안에서도 p2 검증과 시작 후 DSF 서비스 확인이 필요하며, 다른 IDE의 실물 검증을 대체하지 않는다. 특정 디버거의 별도 연결을 만들지는 않지만 ST-LINK 외 디버거까지 검증됐다는 의미는 아니다.

현재 자동 수집은 단일 프로세스/코어·단일 실행 컨텍스트와 1바이트 정수/문자 요소 배열을 대상으로 한다. 프로세스·실행 컨텍스트 수는 정지 전에 검사한다. 다중 코어, 여러 RTOS 스레드, 포인터 버퍼, 별도 memory space는 추측해서 처리하지 않는다. 타깃 정지로 CPU 실행을 멈춰도 DMA·주변장치까지 모두 정지하는 것은 아니므로 테스트가 끝나 버퍼를 변경하는 주체가 없는 상태에서 수집한다.

최초 배포본은 버전 0.0.1과 protocol 1을 사용한다. Python은 플러그인 버전과 프로토콜을 함께 확인한다. STM32CubeIDE 1.19.0·ST-LINK에서 이 배포본으로 실제 수집·CVD 검증·Agent 전달을 확인했다. 검증에 사용한 고객 프로젝트명이나 버퍼 크기는 스킬의 전제 조건으로 기록하지 않는다. 이후 배포본은 이전 버전의 실제 보드 성공 결과를 그대로 재사용하지 않는다. 설치 스킬 갱신과 IDE 플러그인 교체를 구분하고, 교체 전 진행 중인 사용자 테스트 데이터를 보존한다.

## 형상관리

Java 원본, manifest, startup 확장, 빌드 스크립트, Python, 문서, 테스트, 배포 JAR와 release 정보를 함께 관리한다. 클래스 중간 파일·실행 중 요청·측정 데이터는 커밋하지 않는다. 프로토콜 변경 시 플러그인과 Python을 함께 수정하고 `protocol` 버전 호환 여부를 확인한다. `known-releases.json`에는 고객에게 배포한 버전과 JAR 해시를 등록한다. 최초 배포 시에는 0.0.1만 기록하며, 이후 배포본을 추가할 때 이전 고객 배포본의 교체·복구에 필요한 해시를 보존한다. 교체·복구 테스트는 임시 디렉터리에 가상 배포 자료를 만들고 p2 실행을 모의 처리한다. 이 테스트를 실제 IDE 설치 검증으로 간주하지 않는다.
