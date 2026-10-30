# STM32CubeIDE 자동 수집

## 사용 흐름

사용자가 IDE에서 계측 펌웨어를 실행하고 테스트한 뒤 수집을 요청한다. 스크립트가 현재 Debug 세션을 확인하고, CDT 플러그인을 통해 타깃 정지와 메모리 읽기를 수행한다. 덤프를 제품 유틸리티로 검증하고 정상 파일만 Agent에 전달한다. IDE 콘솔 입력이나 Memory View Export는 필요하지 않다.

프로젝트 경로와 IDE 작업 공간은 구분한다. CubeMX 프로젝트 아래 `STM32CubeIDE` 디렉터리에 Eclipse 프로젝트가 있는 경우도 있으므로 실제 세션의 프로젝트 경로와 ELF를 확인한다. 빌드 스킬 기록은 필수 입력이 아니다.

## 최초 설치

버전별 Java·Eclipse·CDT 구성과 확인 수준은 [IDE 호환성](ide-compatibility.md)을 참고한다. `install_cdt_plugin.py --action status`로 설치본을 먼저 조사한다. 개발용 후보 JAR는 고객용 배포본이 아니며, 실제 설치·수집을 검증하기 전에는 수동 복사하지 않는다.

스크립트 실행에는 Python 3.10 이상이 필요하다. 플러그인은 IDE 자체의 Java 17 이상 런타임에서 실행하며, CVD 검증에는 COVER Agent에 포함된 Java를 사용한다. 고객은 배포 JAR를 설치하므로 별도 JDK로 빌드할 필요가 없다. AI가 아래 설치·상태 조회 명령을 실행하고 사용자는 최초 IDE 종료·재시작과 테스트를 담당한다.

1. 사용하는 IDE 설치본과 작업 공간을 확인한다.
2. 진행 중인 Debug 세션에 보존할 RAM 데이터가 있는지 확인한다. 필요한 데이터가 있으면 수동 수집 또는 사용자 판단을 먼저 받고 IDE를 종료한다. 설치 스크립트는 IDE를 종료하지 않는다.
3. 다음 명령으로 배포 JAR를 설치한다. `--ide-closed`는 실행자가 IDE 종료를 확인했다는 의미이며, 스크립트가 프로세스를 자동 종료한다는 의미가 아니다.

```text
python "${SKILL_ROOT}/scripts/install_cdt_plugin.py" --ide-home <IDE 설치 경로> --ide-closed
```

배포 JAR는 p2로 `<IDE>/plugins/`에 등록한다. 설치기는 JAR 해시와 manifest에서 생성한 번들 버전 범위를 확인한다. `compiledAgainst`는 빌드에 사용한 파일 기록이며, 설치를 그 파일명에만 제한하지 않는다. 허용 범위 안에서도 p2 설치 검증과 재시작 후 서비스 확인이 필요하다. 범위 밖 버전은 고객 PC에서 즉석 재빌드하지 않고 호환 배포본을 준비한다.

이전 설치기의 dropins 복사본은 알려진 배포 해시와 일치할 때만 별도 폴더에 백업한 뒤 제거한다. 남겨두면 제거한 플러그인이 다시 등록될 수 있기 때문이다. 고객이 수정했거나 출처를 알 수 없는 JAR는 변경하지 않는다. 읽기 전용 설치본이면 관리자 설치가 필요할 수 있다.

JAR 배치만으로 등록이 끝났다고 판단하지 않는다. 실제 STM32CubeIDE 1.19.0 설치본에서는 dropins 자동 시작이 꺼져 있어 JAR를 배치하고 재시작해도 수집 플러그인이 등록되지 않았다. 설치 스크립트는 IDE에 지정된 Java와 Eclipse p2 도구를 사용해 다음 순서로 등록한다.

- 배포 JAR 하나로 임시 로컬 p2 저장소를 만든다.
- 현재 IDE의 p2 profile을 읽고 `-verifyOnly`로 설치 가능 여부를 확인한다.
- 같은 profile에 수집 플러그인을 설치한다. 다른 플러그인을 업데이트하도록 요청하지 않는다.
- `bundles.info`의 등록 항목과 `<IDE>/plugins/`에 설치된 JAR 해시를 확인한다.

동일 버전과 해시가 이미 등록되어 있으면 등록 작업을 반복하지 않는다. `bundles.info`를 직접 편집하거나 다른 플러그인의 자동 시작 설정을 바꾸지 않는다. p2가 관리하는 설치 정보는 p2를 통해 갱신한다. 등록 로그는 결과의 `registrationLog`에 남긴다. 등록 실패나 시간 초과 시 IDE를 바로 열지 말고 로그를 먼저 확인한다. 설치기는 실행 프로세스도 확인하며, 조회할 수 없으면 IDE가 닫혔다고 추측하지 않는다.

상태 조회, 업데이트, 제거, 이전 버전 복구는 다음 명령을 사용한다. 업데이트·제거 전에는 사용자 테스트 데이터를 보존하고 IDE를 종료한다.

```text
python "${SKILL_ROOT}/scripts/install_cdt_plugin.py" --ide-home <IDE> --action status
python "${SKILL_ROOT}/scripts/install_cdt_plugin.py" --ide-home <IDE> --action update --ide-closed
python "${SKILL_ROOT}/scripts/install_cdt_plugin.py" --ide-home <IDE> --action remove --ide-closed
python "${SKILL_ROOT}/scripts/install_cdt_plugin.py" --ide-home <IDE> --action rollback --restore-from <backupDirectory> --ide-closed
```

백업에는 기존 수집 JAR, 등록 정보 사본, 대상 IDE를 기록한다. rollback은 동일 IDE의 알려진 배포본으로만 수행한다. IDE 전체 p2 profile을 되돌리거나 다른 플러그인을 복구하지 않는다. 실패 후 p2 상태가 불확실하면 로그부터 확인하며 무조건 자동 rollback하지 않는다. 최초 설치처럼 이전 버전이 없는 백업에는 rollback을 적용하지 않는다.

4. 등록에 성공하면 IDE를 열고 해당 작업 공간에서 상태를 조회한다.

```text
python "${SKILL_ROOT}/scripts/cover_cdt_collect.py" --workspace <작업 공간> status
```

프로토콜과 최근 heartbeat를 확인한다. `CDT_PLUGIN_NOT_READY` 또는 `CDT_PLUGIN_STALE`이면 설치·시작이 확인되지 않은 상태다. IDE의 `.metadata/.log`에서 플러그인 오류를 확인한다. JAR 복사만 성공했다고 자동 수집 준비 완료로 안내하지 않는다.

5. 설치 확인 후 사용자가 새 Debug 세션에서 원하는 테스트를 수행한다.

## 실제 수집

먼저 다음 명령으로 환경을 조사한다. Windows 실행 프로세스와 Linux `/proc`의 IDE 실행 인자를 확인하며, 전체 디스크를 검색하지 않는다. `-data`가 생략되어 작업 공간을 찾을 수 없거나 프로세스 조회가 제한되면 프로젝트 상위 `.metadata` 후보를 참고하고 필요한 값을 사용자에게 요청한다. 명시한 작업 공간은 다른 실행 IDE의 값으로 대체하지 않는다.

```text
python "${SKILL_ROOT}/scripts/cover_cdt_collect.py" doctor --project <프로젝트 경로>
```

`doctor`는 세션 조회만 요청하고 타깃을 정지하지 않는다. 여러 작업 공간 후보, 플러그인 버전·프로토콜 오류, 접근 거부, Agent 도구 경로를 구분해 보고한다. 쓰기 가능 여부는 참고값이며 실제 수집에서는 정지 전에 감시 폴더 쓰기도 확인한다. 사용자 전체 프로세스 명령행은 보고서에 남기지 않는다.

먼저 세션을 조회한다. 이 명령은 타깃을 정지하거나 프로그램을 실행하지 않는다.

```text
python "${SKILL_ROOT}/scripts/cover_cdt_collect.py" --workspace <작업 공간> sessions
```

대상 프로젝트에 해당하는 세션이 하나라면 다음 명령으로 수집한다.

```text
python "${SKILL_ROOT}/scripts/cover_cdt_collect.py" --workspace "<작업 공간>" collect --project "<프로젝트 경로>" --agent-dir "<COVER Agent 설치 경로>" --work-dir "<덤프 작업 디렉터리>"
```

`--agent-dir`는 `CODESCROLL_TM_INSTALL_PATH`로 대체할 수 있다. 기본 감시 경로는 `<Agent>/coverage`지만, 고객 설정이 다르면 `--monitoring-path <확인한 절대 경로>`를 전달한다. Java가 설치 기본 경로와 다르면 `--java-path <Java 실행 파일>`을 지정한다. 세 경로를 서로 추측해 만들지 않으며 Agent 설정은 변경하지 않는다. Access Key나 Open API 포트는 사용하지 않는다.

덤프 작업 디렉터리는 `--project`가 있으면 `<프로젝트>/.cover-ee/measurements`를 기본으로 사용하며 `--work-dir`로 바꿀 수 있다. 성공 결과는 명령의 JSON 출력과 `<작업 디렉터리>/<수집ID>/result.json`에서 확인한다. `destination`은 Agent에 전달한 `.cvd` 경로이며, 같은 수집 폴더의 `memory.bin`, `cdt-response.json`, `cvd-view.log`는 증거로 보관한다. Agent가 `.cvd`를 즉시 이동하거나 삭제할 수 있으므로 파일이 사라졌다는 사실만으로 업로드 성공 또는 실패를 판정하지 않는다.

프로젝트 조건에 맞는 세션이 여러 개면 스크립트가 멈춘다. 조회 결과를 보여주고 사용자에게 대상을 확인한 뒤 `--session-id` 또는 `--elf`로 구분한다. 첫 세션을 임의 선택하지 않는다. 세션에서 읽은 ELF 경로와 파일 해시를 플러그인에 전달하고 수집 전후에도 대조한다. 이 검사는 디버거 심볼 파일의 일관성 검사이며 보드 플래시 전체와 ELF의 동일성을 증명하는 검사는 아니다.

## 크기와 정지 상태 처리

- 플러그인은 전역 `::sdump_buf`를 실행 컨텍스트에서 먼저 조회한다. DSF가 프레임을 요구하는 경우에만 최상위 프레임을 사용하며, 시간 초과 시에는 다른 방식으로 재요청하지 않는다.
- 배열 여부와 요소의 기본 타입·1바이트 크기·요소 개수·전체 바이트 크기를 확인한다. typedef 이름만으로 거부하지 않는다. 포인터, 구조체 배열, 크기를 확인할 수 없는 타입은 임의 처리하지 않는다.
- 배열 주소와 전체 바이트 크기를 CDT에서 읽는다. 크기를 사용자에게 입력받거나 고정값으로 넣지 않는다. 0바이트 또는 64 MiB 초과는 지원 범위 밖으로 중단한다.
- DSF 메모리 캐시를 비운 뒤 최대 4096바이트씩 읽고, 읽기 불가능한 바이트가 하나라도 있으면 실패 처리한다.
- 정지 완료 후 배열을 읽는다. 읽는 동안 타깃이 재개되면 파일을 전달하지 않는다. 수집 후에도 정지 상태를 유지하며 reset, load, resume을 수행하지 않는다.
- 프로세스·실행 컨텍스트 수는 정지 전에 확인한다. 실행 중 정보를 제공하지 않는 디버거도 추측해 정지하지 않고 중단한다. 여러 RTOS 스레드·다중 코어의 수집은 별도 보드 검증 전까지 허용하지 않는다. 별도 memory space도 지원하지 않는다. 타깃 정지만으로 DMA나 주변장치의 메모리 변경까지 막는 것은 아니므로 테스트가 끝난 안정된 버퍼를 수집한다.

## 파일과 통신

플러그인은 작업 공간의 `.metadata/.plugins/com.qualityscroll.cover.collect/`에 현재 OS 사용자만 접근할 수 있는 요청 폴더를 만든다. POSIX 권한 또는 Windows ACL로 제한하며 권한 설정이 실패하면 시작하지 않는다. 네트워크 포트를 열지 않으며 다른 GDB를 연결하지 않는다.

AI 실행 환경의 샌드박스 계정은 같은 PC에서도 이 폴더에 접근하지 못할 수 있다. 접근 거부가 발생하면 권한을 넓히지 않고, IDE를 실행한 OS 사용자 권한으로 클라이언트를 실행한다. 샌드박스 밖 실행에 승인이 필요한 환경에서는 해당 승인을 받은 뒤 진행한다.

`bridge.json`에는 프로토콜, 플러그인 버전, IDE 프로세스 ID, 인스턴스 ID, heartbeat를 기록한다. 인스턴스 ID는 IDE 재시작을 구분하는 값이며 사용자 인증 키가 아니다. 현재 OS 계정 내부의 실행 환경을 신뢰하며, 다른 사용자와 IDE 계정을 공유하는 구성은 지원하지 않는다.

각 요청은 `requests/<요청ID>/request.json`으로 제출하고 플러그인은 이를 `working.json`으로 이름을 바꿔 한 번만 처리한다. 작업은 `sessions` 또는 `capture`로 제한하며 임의 GDB 명령, 메모리 쓰기, 실행 재개 요청을 받지 않는다. 메모리는 요청 폴더의 `memory.bin`으로 저장한다. 응답에는 세션·ELF·주소·타입·크기·해시와 정지 상태를 기록한다.

클라이언트는 응답과 파일을 대조한 뒤 지정된 덤프 작업 디렉터리에 증거를 보관한다. `cover_stm32_dump.py`의 공통 검증·전달 기능을 호출하므로 수동 방식과 같은 `cover-cvd-utility.jar -m VIEW` 검증을 사용한다. 검증 종료 코드가 0일 때만 `.partial` 작성 후 `.cvd`로 이름을 바꿔 Agent에 전달한다.

덤프와 관련 정보를 작업 폴더에 기록한 뒤 클라이언트가 `ack.json`을 작성한다. 플러그인은 응답이 있는 확인 완료 요청만 `completed/`로 이동하고 통신 폴더의 중복 `memory.bin`을 제거한다. 작업 폴더의 원본 덤프는 제거하지 않는다. 완료 요청을 매번 순회하지 않지만, 완료 메타데이터의 장기 보관 기간은 현재 자동 관리하지 않는다.

## 시간 초과와 실패

요청 만료는 60초이며 수집 작업 기한 기본값도 60초다. 큰 버퍼나 느린 디버거에는 `--capture-timeout <초>`로 10~600초 범위에서 지정한다. 개별 DSF 대기는 최대 10초이며 클라이언트는 수집 기한보다 15초 더 기다린다. `progress.json`에 읽은 바이트와 전체 크기를 남긴다. 시간 초과가 발생한 요청은 나중에 타깃을 정지시킬 수 있으므로 자동 재요청하지 않는다. 미완료 요청의 `client-lock.json`도 유지한다.

덤프가 완료되고 검증 또는 전달만 실패했다면 보드를 다시 읽지 않는다.

```text
python "${SKILL_ROOT}/scripts/cover_cdt_collect.py" resume --capture-dir <수집 폴더>
```

`capture-stage.json`, `validation.json`, `delivery.json`에 완료 단계를 기록한다. 같은 덤프와 검증 도구이면 성공한 검증을 재사용한다. 이미 전달 완료로 기록된 파일은 Agent가 가져갔더라도 다시 보내지 않는다. 전달 직전 기록만 있고 `.cvd`도 없으면 처리 여부가 불확실하므로 `DELIVERY_UNCERTAIN`으로 중단한다. 이 기록을 삭제해 강제로 재전달하지 않는다.

재개 결과의 `targetStopped`는 덤프 당시 기록이다. `resume`은 IDE에 접속하지 않으므로 재개 명령을 실행한 현재 시점에도 타깃이 정지해 있다고 보고하지 않는다.

`publish.lock`은 OS 파일 잠금을 사용하며 프로세스 종료 시 잠금이 해제된다. 파일이 남아 있다는 이유만으로 삭제하지 않는다. 실제 게시 작업이 잠금을 보유하고 있으면 `PUBLISH_PENDING`으로 중단한다. `resume`은 잠금을 얻은 뒤 전달 기록을 대조한다. `capture-error.json`의 `targetStopped=null`은 실패 시 정지 여부를 확정하지 못했다는 의미다.

IDE 상태와 요청 폴더의 응답을 확인한 뒤에만 다음 명령을 사용한다.

```text
python "${SKILL_ROOT}/scripts/cover_cdt_collect.py" --workspace <작업 공간> recover-lock
```

같은 IDE 인스턴스에 미완료 요청이 남아 있으면 잠금을 해제하지 않는다. 플러그인 내부 DSF timeout 후에는 추가 수집을 차단하므로 현재 데이터 보존 방법을 사용자와 결정하고 IDE를 재시작해야 한다. 실패했다고 프로그램을 자동 실행하거나 원본 빌드를 수행하지 않는다.

복구 순서는 다음과 같다.

1. `status`에서 `pendingRequest`를 확인한다. 같은 IDE 인스턴스이고 `response.json`이 없으면 `recover-lock`을 실행하지 않는다.
2. DSF timeout이면 사용자와 현재 데이터 보존 방법을 결정한 뒤 IDE를 재시작한다. 잠금 해제가 플러그인의 timeout 차단을 해제하지는 않는다.
3. 재시작 후 `status`를 다시 확인한다. `pendingRequest`가 없으면 잠금 복구가 필요 없다. 이전 인스턴스의 요청 잠금이 남아 있으면 `recover-lock`을 한 번 실행한다.
4. 재시작하지 않은 일반 오류라도 완료 응답이 있고 타깃 상태를 확인했다면 남은 잠금을 복구할 수 있다. 새 테스트 실행은 사용자가 결정한다.

`status` 출력의 `pendingRequest.requestId`로 요청 폴더를 찾는다. 같은 인스턴스에서 `working.json`만 있고 `response.json`이 없으면 완료 여부를 확인할 수 없으므로 재요청하지 않는다. 실패 후 타깃이 정지했는지는 IDE의 Debug 뷰에서 확인한다. 성공 결과가 반환된 뒤에는 사용자가 IDE에서 실행을 재개할 수 있지만 스킬이 자동으로 재개하지는 않는다.

## 검증 상태와 결과 의미

현재 배포 JAR는 STM32CubeIDE 1.19.0 설치본 API를 기준으로 빌드했다. ST-LINK Debug 세션에서 세션 조회 → 정지 → 배열 덤프 → 제품 CVD 검증 → Agent 전달을 실제 확인했다. 버퍼 크기는 테스트 프로젝트의 고정값을 사용하지 않고 CDT에서 읽는다. 제품 검증 종료 코드는 0이었으며 수집 후 타깃은 정지 상태로 유지됐다. 새 호환성 후보의 실제 실행 검증은 이 기존 배포본의 결과와 구분한다.

위 환경에서 최초 배포본 0.0.1/protocol 1의 IDE 등록과 실제 보드 수집까지 확인했다. 클라이언트는 버전 0.0.1과 프로토콜 1을 함께 확인한다. 새 JAR 빌드·회귀 테스트와 실제 IDE 설치·보드 수집 검증은 별도로 구분하며, 호환되지 않는 플러그인에는 수집을 요청하지 않는다.

이 결과를 모든 IDE 버전·보드·디버거에서 검증된 것으로 확대하지 않는다. 다른 설치본은 주요 번들과 플러그인 호환성을 확인하고, 실제 보드 수집 결과를 별도로 검증한다. 프로토콜·파일 검증 테스트 통과와 실물 보드 검증은 구분한다.

수집 결과 `CVD_PUBLISHED`는 검증한 파일을 Agent에 전달했다는 뜻이다. 서버 반영과 커버리지 수치는 확인하지 않으며 `serverMeasurementVerified=false`를 유지한다. 테스트 충분성이나 서버 업로드 성공으로 확대해서 보고하지 않는다.

자동 연동이 준비되지 않았고 사용자가 수동 방식을 선택했을 때만 [수동 GDB 절차](stm32-gdb-manual.md)를 사용한다.

## 개발 참고

- [Eclipse dropins 설치 구조](https://help.eclipse.org/latest/topic/org.eclipse.platform.doc.isv/reference/misc/p2_dropins_format.html): 플러그인 설치 위치와 시작 시 인식 방식.
- [Eclipse p2 director](https://help.eclipse.org/latest/topic/org.eclipse.platform.doc.isv/guide/p2_director.html): 로컬 저장소를 이용한 설치 검증과 플러그인 등록 방식.
- [DSF 프로그래밍 안내](https://help.eclipse.org/latest/topic/org.eclipse.cdt.doc.isv/guide/dsf/intro/dsf_programming_intro.html): 서비스 executor와 비동기 RequestMonitor 사용 방식.
