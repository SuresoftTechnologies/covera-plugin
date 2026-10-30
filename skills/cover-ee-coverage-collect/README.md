# COVER EE 커버리지 수집

`cover-ee-coverage-collect`는 사용자가 STM32CubeIDE에서 테스트한 펌웨어의 커버리지 데이터를 수집하는 스킬이다. **기존 Debug 세션에서 메모리를 읽고, CVD 형식을 검증한 뒤 COVER Local Agent에 전달한다.**

사용자가 Memory View를 열거나 Export 길이를 계산할 필요는 없다. AI는 대상과 경로를 확인하고 스크립트를 실행한다. 타깃 정지, 배열 크기 확인, 메모리 읽기, 파일 검증과 전달은 Python 스크립트와 IDE 플러그인이 처리한다.

이 문서는 전체 구조와 동작을 설명한다. AI의 수행 지침은 [SKILL.md](SKILL.md), 설치·운영 세부 절차는 [CDT 연동](references/stm32-cdt.md), Java 플러그인 개발 방법은 [플러그인 README](plugin/README.md)를 따른다.

## 1. 사용자가 수행하는 일

최초 한 번은 STM32CubeIDE에 수집 플러그인을 설치하고 IDE를 재시작한다. 이후에는 다음 흐름으로 사용한다.

1. 사용자가 COVER로 계측한 펌웨어를 보드에 올리고 IDE에서 Debug를 시작한다.
2. 버튼 입력, 통신, 기능 실행 등 원하는 테스트를 수행한다.
3. Debug 세션을 종료하지 않은 상태에서 “이 프로젝트 커버리지 데이터 수집해줘”라고 요청한다.
4. 스킬이 해당 세션에서 데이터를 수집하고 결과를 보고한다.

프로젝트와 작업 공간을 알 수 없거나 대상 세션이 여러 개이면 필요한 정보만 사용자에게 확인한다. 테스트 완료 의사가 명확하면 다시 테스트가 끝났는지 묻지 않는다.

수집 후에는 타깃을 **정지 상태로 유지한다.** 추가 테스트가 필요하면 사용자가 IDE에서 실행을 재개한다. 스킬은 테스트 시나리오를 추측하거나 프로그램을 자동으로 재실행하지 않는다.

### 커버리지 빌드와의 구분

| 구분 | 커버리지 빌드 스킬 | 이 수집 스킬 |
|---|---|---|
| 목적 | 계측된 실행 파일 생성 | 실행 중 쌓인 측정 데이터 수집 |
| 주요 작업 | 원본 빌드, wrapper 설정, 계측 빌드 | 타깃 정지, 메모리 덤프, CVD 검증, Agent 전달 |
| 프로젝트 변경 | 빌드 설정을 적용할 수 있음 | 소스·빌드 설정을 변경하지 않음 |
| 사용자 테스트 | 수행하지 않음 | 이미 끝난 테스트의 데이터를 읽음 |

두 스킬은 독립적이다. 수동으로 커버리지 빌드한 펌웨어에도 사용할 수 있으며, 이전 빌드 작업 기록이나 빌드 스킬 설치를 요구하지 않는다.

## 2. 전체 구조

```text
사용자: IDE에서 테스트 완료 → 수집 요청
                         │
                         ▼
AI Agent: 프로젝트·작업 공간·Agent 경로 확인
                         │ 스크립트 실행
                         ▼
Python 수집 클라이언트
  cover_cdt_collect.py
                         │ 작업 공간의 요청/응답 파일
                         ▼
STM32CubeIDE 내부의 수집 플러그인
  Bridge.java → DsfCapture.java
                         │ CDT/DSF 서비스 호출
                         ▼
기존 Debug 세션 → 기존 디버거 연결 → 보드의 sdump_buf
                         │ Raw Binary 반환
                         ▼
Python 공통 검증·전달
  cover_stm32_dump.py
                         │ cover-cvd-utility.jar -m VIEW
                         ▼
검증 성공 → <Agent>/coverage/*.cvd 전달
```

Eclipse CDT는 IDE의 C/C++ 개발·디버깅 기능을 제공한다. 이 플러그인은 그 안의 [DSF(Debugger Services Framework)](https://help.eclipse.org/latest/topic/org.eclipse.cdt.doc.isv/guide/dsf/intro/dsf_programming_intro.html) 서비스를 호출해 현재 Debug 세션의 상태, 심볼, 메모리에 접근한다. Python이 별도의 GDB를 실행하거나 보드에 두 번째 연결을 만드는 방식이 아니다.

따라서 GUI 버튼 위치나 별도 OpenOCD 포트에 의존하지 않는다. 다만 기존 세션을 사용한다는 구조와 모든 디버거에서 검증되었다는 주장은 다르다. 현재 검증 범위는 아래 지원 범위를 참고한다.

## 3. 최초 설치는 어떻게 처리하는가

고객에게는 빌드된 플러그인 JAR를 스킬과 함께 제공한다. 설치할 때마다 Java 소스를 빌드하지 않는다.

1. 사용하는 IDE 설치 경로를 확인한다.
2. 보존할 RAM 데이터가 있는지 확인한 뒤 사용자에게 IDE 종료를 요청한다. 설치 스크립트 자체는 IDE를 종료하지 않는다.
3. `install_cdt_plugin.py`가 실제 IDE 종료 여부, 배포 JAR 해시와 주요 번들 버전 범위를 확인한다.
4. 배포 JAR로 임시 로컬 p2 저장소를 만든다. 기존 설치가 있으면 알려진 배포본인지 확인하고 백업한다.
5. IDE의 Java와 Eclipse p2 설치 도구로 설치 가능 여부를 확인한 뒤 `<IDE>/plugins/`에 등록한다.
6. `bundles.info`의 등록 항목과 `<IDE>/plugins/`의 JAR 해시를 확인한다.
7. IDE를 다시 열고 플러그인의 최근 heartbeat를 확인한다. 확인이 끝나면 사용자가 Debug와 테스트를 시작한다.

JAR를 복사하는 것만으로 설치 성공을 판단하지 않는다. 실제 검증한 STM32CubeIDE 설치본은 dropins 자동 시작이 꺼져 있어 명시적인 p2 등록이 필요했다. 동일 버전과 해시가 이미 등록되어 있으면 등록 작업을 반복하지 않는다. 구버전은 `--action update`로 갱신하며 제거·백업 복구에는 `remove`, `rollback`을 사용한다. 기존 dropins 사본은 백업하고 p2 작업이 성공한 뒤 정리한다. 사용자가 변경한 JAR는 임의로 덮어쓰지 않는다.

플러그인 설치에는 IDE 설정 변경과 재시작이 필요하지만, **일반적인 수집 요청마다 재설치하거나 재시작하지는 않는다.**

## 4. 수집 요청을 받으면 처리하는 순서

### 4.1 대상과 실행 환경 확인

AI는 `cover_cdt_collect.py doctor --project <프로젝트>`로 프로젝트 경로, 실행 중인 IDE와 작업 공간 후보, Local Agent 경로를 확인한다. 작업 공간은 Eclipse의 `.metadata`가 있는 폴더이며 프로젝트 폴더와 다를 수 있다. 실행 인자에 작업 공간이 없거나 권한 때문에 조회할 수 없으면 필요한 경로를 질문한다. 여러 후보를 첫 번째 값으로 임의 결정하지 않는다.

Agent 설치 경로는 `CODESCROLL_TM_INSTALL_PATH` 또는 명시한 `--agent-dir`에서 가져온다. 감시 폴더 기본값은 `<Agent>/coverage`이며 고객 설정이 다르면 `--monitoring-path`로 분리해 지정한다. Java도 `--java-path`로 지정할 수 있다. 감시 경로 확인은 AI의 운영 절차이며 클라이언트가 서버에 조회하는 것은 아니다. 고객의 Agent 설정 자체는 변경하지 않는다.

클라이언트는 감시 폴더, Agent Java, `cover-cvd-utility.jar`가 있는지 확인한다. 이후 플러그인을 통해 세션 목록을 조회하고 프로젝트·ELF·세션 ID 조건으로 대상을 하나로 정한다. 첫 번째 세션을 임의로 선택하지 않는다.

### 4.2 기존 세션을 정지하고 배열 크기 확인

플러그인은 선택한 세션의 실제 ELF 경로와 해시를 확인한다. 프로세스·실행 컨텍스트 수를 정지 전에 확인해 지원하지 않는 다중 대상을 걸러낸다. 지원하는 타깃이 실행 중이면 정지시키고, 이미 정지해 있으면 그 상태에서 진행한다. 실행 중 구성 정보를 제공하지 않는 디버거는 추측해 정지하지 않는다.

CDT 표현식 서비스에 `::sdump_buf`를 조회해 다음 정보를 얻는다.

- 버퍼 타입: 배열인지 확인하고 첫 요소의 정수/문자 타입과 1바이트 크기를 확인한다. typedef의 표시 이름만으로 거부하지 않는다.
- 시작 주소: 디버거가 해석한 배열 주소를 사용한다.
- 전체 바이트 크기: CDT가 반환한 배열 크기를 사용한다.

예를 들어 `unsigned char [3000]`이면 3,000바이트를 읽는다. 코드에 3,000을 고정하거나 사용자에게 Export 길이를 입력받는 방식이 아니다. 포인터이면 배열 크기를 알 수 없으므로 추측하지 않고 중단한다.

전역 심볼 조회는 실행 컨텍스트에서 먼저 시도하고, DSF가 프레임을 요구할 때만 최상위 프레임을 사용한다. 배열 요소 개수와 전체 바이트 크기도 일치해야 한다. 시간 초과가 발생한 요청은 다른 방식으로 재시도하지 않는다.

### 4.3 메모리 읽기

이전 Memory View 조회 결과가 남아 있지 않도록 DSF 메모리 캐시를 비우고, 최대 4,096바이트 단위로 메모리를 읽는다. 배열 전체를 Raw Binary로 저장하며 뒤쪽의 빈 공간을 잘라내지 않는다.

읽는 도중 타깃이 재개되거나 읽을 수 없는 바이트가 있으면 실패 처리한다. 수집 전후 ELF도 대조한다. 단, 이 검사는 디버거가 사용하는 ELF 파일의 일관성을 확인하는 것이며 실제 보드 플래시 전체와 ELF가 같은지를 증명하지는 않는다.

읽은 바이트 수는 `progress.json`에 기록한다. 전체 수집 기한은 기본 60초이며 `--capture-timeout`으로 10~600초 사이에서 지정한다. 개별 DSF 호출의 10초 제한은 유지한다.

### 4.4 CVD 검증과 Agent 전달

Python 클라이언트는 응답의 세션·ELF·바이트 수·SHA-256과 실제 덤프를 대조한다. 정상 덤프만 작업 폴더에 보관하고, Agent에 포함된 Java로 다음 제품 검증기를 실행한다.

```text
<Agent Java> -jar <Agent>/bin/cover-cvd-utility.jar -m VIEW -i <memory.bin>
```

검증 종료 코드가 0이고 검증 중 파일이 바뀌지 않았을 때만 전달한다. 메모리 바이트를 변환하거나 임의로 보정하지 않는다.

Agent 감시 폴더에는 먼저 `.partial`로 완전히 기록한 뒤 `.cvd`로 이름을 바꾼다. 작성 중인 파일을 Agent가 읽지 않도록 하기 위한 처리다. 이후 파일 감시와 서버 전송은 Local Agent의 역할이며, 수집 스크립트가 Agent를 시작하거나 서버 업로드 완료를 확인하지는 않는다.

## 5. AI와 스크립트의 역할

| 구성 요소 | 담당하는 일 |
|---|---|
| AI Agent | 사용자 요청 해석, 누락 경로 확인, 대상 선택이 필요한 경우 질문, 스크립트 실행, 결과 설명 |
| Python 클라이언트 | 요청 제출, 응답 대기, 세션 선택 조건 적용, 파일·해시 대조, 검증·전달 호출 |
| IDE 플러그인 | 기존 Debug 세션 조회, 타깃 정지, 심볼 해석, 배열 메모리 읽기 |
| 제품 CVD 유틸리티 | 읽어 온 데이터의 CVD 형식 검증 |
| Local Agent | 감시 폴더에 전달된 측정 파일 처리 |

AI가 매번 GDB 명령을 작성하거나 화면을 보고 주소와 크기를 옮겨 적지 않는다. 같은 작업은 같은 스크립트로 처리한다. 지원하지 않는 세션이나 오류가 발생하면 자동 우회보다 현재 상태를 확인하는 쪽으로 멈춘다.

## 6. 파일 구조와 읽는 순서

```text
cover-ee-coverage-collect/
├─ README.md                       전체 구조와 동작 설명
├─ SKILL.md                        AI 실행 지침과 범위
├─ references/
│  ├─ stm32-cdt.md                  자동 설치·수집 명령과 오류 대응
│  └─ stm32-gdb-manual.md           사용자가 선택한 경우의 수동 대안
├─ scripts/
│  ├─ install_cdt_plugin.py         배포 JAR 확인과 p2 등록
│  ├─ collect_environment.py        실행 중 IDE·작업 공간 후보 조사
│  ├─ cover_cdt_collect.py          자동 수집 진입점과 파일 기반 통신
│  └─ cover_stm32_dump.py           덤프 작업 준비·CVD 검증·전달 공통 처리
├─ plugin/
│  ├─ README.md                    Java 개발·빌드·호환성 안내
│  ├─ build_plugin.py              대상 IDE API로 플러그인 빌드
│  ├─ META-INF/MANIFEST.MF          번들 이름·버전·의존성
│  ├─ plugin.xml                   IDE 시작 확장 등록
│  ├─ build.properties             플러그인 구성 정보
│  └─ src/com/qualityscroll/cover/collect/
│     ├─ Startup.java              IDE 시작 시 수집 기능 활성화
│     ├─ Activator.java            플러그인 시작·종료 처리
│     ├─ Bridge.java               요청 폴더 감시와 세션·수집 요청 처리
│     └─ DsfCapture.java           CDT/DSF 호출과 실제 메모리 읽기
├─ assets/plugin/
│  ├─ com.qualityscroll.cover.collect_0.0.1.jar
│  ├─ known-releases.json          기존 배포본 확인용 해시
│  └─ release.json                 배포 JAR·소스 해시와 호환 범위
└─ tests/
   ├─ test_cdt_collect.py          통신·세션 선택·자동 수집·설치 테스트
   ├─ test_stm32_dump.py           공통 준비·검증·전달 테스트
   ├─ test_customer_environments.py 경로·설치·재개·프로세스 종료 테스트
   ├─ test_java_rules.py           실제 Java 버퍼 판정 테스트 실행
   └─ java/BufferRulesTest.java    버퍼 타입·크기 판정 사례
```

처음 인수받는 경우 이 README → `SKILL.md` → `references/stm32-cdt.md` 순서로 읽는다. Python 흐름은 `cover_cdt_collect.py`의 `collect()`부터, IDE 내부 처리는 `Bridge.java`와 `DsfCapture.java` 순서로 확인한다.

`cover_stm32_dump.py`는 이름과 달리 수동 방식 전용이 아니다. 자동 수집에서도 `prepare(..., manual=False)`와 `publish()`를 사용해 검증·전달을 공유한다. 수동 대안에서만 `dump.gdb`를 생성한다.

`plugin/src/.../BufferRules.java`는 디버거와 독립된 배열 크기 판정이며 Java 테스트에서도 실제로 컴파일해 실행한다.

## 7. 통신 파일과 작업 결과

Python과 플러그인은 네트워크 서버 대신 작업 공간의 파일로 요청과 응답을 주고받는다.

```text
<작업 공간>/.metadata/.plugins/com.qualityscroll.cover.collect/
├─ bridge.json                     프로토콜·IDE 인스턴스·heartbeat
├─ client-lock.json                미완료 요청의 중복 실행 방지
├─ completed/                      확인 완료된 요청 메타데이터
└─ requests/<요청ID>/
   ├─ request.json → working.json  요청 제출 → 처리 시작
   ├─ response.json                처리 결과
   ├─ progress.json                메모리 읽기 진행 상황
   ├─ ack.json                     클라이언트의 보관 완료 확인
   └─ memory.bin                   플러그인이 읽은 메모리
```

요청 폴더는 IDE를 실행한 OS 사용자만 접근할 수 있도록 제한한다. AI 샌드박스가 접근하지 못하면 권한을 넓히지 않고 같은 OS 사용자 권한으로 실행하는 절차를 따른다. 필요한 실행 승인은 별도로 받는다.

수집 결과는 보통 다음 위치에 보관한다. `--project`가 있으면 이 경로가 기본값이며, `--work-dir`로 별도 지정할 수 있다.

```text
<프로젝트>/.cover-ee/measurements/<수집ID>/
├─ capture.json                    ELF·크기·해시 등 수집 정보
├─ cdt-response.json               플러그인의 수집 응답
├─ memory.bin                      검증 전후 보존하는 원본 덤프
├─ cvd-view.log                    제품 검증기 출력
├─ capture-stage.json              덤프 보관 완료 상태
├─ validation.json                 검증 완료와 재사용 조건
├─ delivery.json                   전달 전후 기록
├─ publish.lock                    중복 게시를 막는 OS 파일 잠금
└─ result.json                     검증·전달 결과
```

`result.json`은 성공 시 작성한다. 실패 시 모든 파일이나 최종 JSON이 존재한다고 가정하지 않고 CLI 오류와 생성된 중간 파일을 확인한다. Agent가 전달된 `.cvd`를 이동하거나 삭제해도 작업 폴더의 원본 덤프는 남는다.

클라이언트가 덤프와 응답을 작업 폴더에 보관하고 확인을 보내면, 플러그인은 통신 폴더의 중복 덤프를 제거하고 요청을 `completed/`로 옮긴다. 완료 요청은 반복 검색하지 않는다. 완료 메타데이터의 장기 보관 기간은 아직 자동 관리하지 않는다.

## 8. 성공 기준과 중단 기준

`CVD_PUBLISHED`는 **메모리 덤프·CVD 검증·Agent 폴더 전달이 완료되었다**는 뜻이다. 서버 반영, 커버리지 백분율, 테스트 충분성까지 확인했다는 뜻은 아니다. 현재 `serverMeasurementVerified`는 `false`다. 수집에는 COVER Access Key나 Open API 포트를 요청하지 않는다.

다음 경우에는 정상 파일을 전달하지 않고 중단한다.

- 플러그인이 준비되지 않았거나 heartbeat가 오래된 경우
- 대상 세션을 하나로 정할 수 없는 경우
- 배열 타입이나 크기를 확인할 수 없는 경우
- 수집 중 실행 재개, ELF 변경, 메모리 읽기 오류가 발생한 경우
- 응답과 파일의 크기·해시가 다른 경우
- 제품 CVD 검증이 실패한 경우

시간 초과는 특히 자동 재시도하지 않는다. 클라이언트가 기다리기를 끝내도 IDE 안에서 요청이 처리 중일 수 있기 때문이다. 미완료 요청 잠금을 유지하고 IDE와 응답 파일을 확인한 뒤 복구한다. 세부 복구 명령은 [CDT 연동](references/stm32-cdt.md)을 따른다.

덤프 완료 후 검증·전달 실패는 `resume --capture-dir <수집 폴더>`로 재개한다. IDE나 보드를 다시 읽지 않으며 같은 덤프와 도구로 성공한 검증을 재사용한다. 전달 완료 기록이 있으면 재전달하지 않는다. `.cvd` 전달 여부가 불확실한 경우는 `DELIVERY_UNCERTAIN`으로 멈춘다. OS 게시 잠금은 프로세스 종료 시 해제되지만, IDE 미완료 요청 잠금은 별도이므로 둘을 혼동하지 않는다.

## 9. 현재 지원·검증 범위

버전별 공식 구성, 설치본 조사 명령, 공통 Java 11 후보의 컴파일 결과와 남은 실물 검증은 [IDE 호환성](references/ide-compatibility.md)에 정리한다. `scripts/inspect_cdt_compatibility.py`는 IDE에 등록된 번들과 내장 Java를 읽는 조사 도구다. 조사만으로 설치·수집 성공을 표시하지 않는다. 현재 고객용 JAR와 설치 허용 범위는 유지한다.

- Python 3.10 이상을 사용한다.
- 배포 플러그인은 Java 17 바이트코드이며 IDE 내부 Java에서 실행한다. 설치는 IDE 자체 Java, CVD 검증은 Agent 자체 Java를 사용한다.
- 현재 배포본은 STM32CubeIDE 1.19.0의 주요 번들을 기준으로 빌드했다. manifest의 제한된 버전 범위에서 설치 후보를 허용하며 범위 밖은 중단한다. 호환 범위는 실물 검증 완료 목록과 다르다.
- 단일 프로세스/코어, 단일 실행 컨텍스트, 요소 크기를 확인할 수 있는 1바이트 정수/문자 배열을 대상으로 한다.
- 포인터 버퍼, 여러 RTOS 스레드, 별도 memory space, 0바이트 또는 64 MiB 초과 배열은 현재 지원하지 않는다.
- 타깃 정지가 DMA나 주변장치의 메모리 변경까지 막는다고 가정하지 않는다. 테스트를 끝내고 버퍼가 안정된 상태에서 수집한다.

DMA나 주변장치가 수집 버퍼를 직접 갱신하는 환경이라면 사용자가 해당 쓰기를 끝낸 뒤 수집을 요청해야 한다. 현재 스크립트는 이런 쓰기의 종료 여부를 검사하지 않는다. 이를 확인할 수 없으면 CVD 형식 검증을 통과하더라도 덤프가 한 시점의 일관된 데이터인지 보장할 수 없다.

Windows·STM32CubeIDE 1.19.0·ST-LINK의 Debug 세션에서 실제 수집을 검증했다. CDT가 보고한 배열 크기만큼 읽었고, COVER EE 4sp9 CVD 유틸리티 검증 종료 코드 0과 Agent 전달을 확인했다. 고객 프로젝트명과 특정 버퍼 크기는 지원 조건이 아니다. 이 결과를 다른 OS·IDE 버전·디버거까지 검증된 것으로 확대하지 않는다.

위 환경에서는 최초 배포본 0.0.1/protocol 1의 IDE 등록과 실제 수집까지 확인했다. 클라이언트는 배포 버전과 프로토콜을 함께 검사한다. 이후 버전에서도 JAR 빌드·자동 테스트와 실제 IDE 설치·보드 수집을 구분해 검증한다. 새 스킬을 복사해도 실행 중인 IDE의 플러그인이 자동으로 교체되지는 않는다.

## 10. 유지보수와 검증

Java 소스, Python, 문서, 테스트, 배포 JAR와 `release.json`을 함께 형상관리한다. 측정 파일과 실행 중 요청 폴더는 소스 저장소에 넣지 않는다.

- 수집 순서·성공 기준을 바꾸면 `SKILL.md`와 운영 문서도 함께 대조한다.
- 요청·응답 형식을 바꾸면 Python과 Java 양쪽을 수정하고 프로토콜 호환 여부를 확인한다.
- Java 소스를 바꾸면 JAR와 `release.json`을 다시 생성한다. 고객에게 전달하는 것은 배포 JAR이므로 소스 수정만으로 설치본이 바뀌지는 않는다.
- Python 모의 테스트 통과와 실제 보드 검증을 구분한다.

통합 저장소 루트에서 다음 명령으로 테스트한다. 테스트는 배포 ZIP에 포함하지 않는다.

```text
python -m unittest discover -s plugins/cover-build-measure-agent/tests/coverage_collect -v
```

플러그인 빌드 명령과 호환성 확인 방법은 [플러그인 README](plugin/README.md)를 참고한다. 새 IDE나 디버거를 지원할 때는 설치·세션 조회뿐 아니라 실제 정지·덤프·CVD 검증·전달까지 확인한다.
