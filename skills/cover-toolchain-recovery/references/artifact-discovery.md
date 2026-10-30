# 오류 ZIP 없이 자료 찾기

오류 소스나 빌드 폴더만 받아도 ZIP이나 Issue ID를 요구하지 않는다. **소스 전체 경로 + HardwareID**로 메인 서버에서 최신 오류 자료를 받고, 로컬 자료는 진단 후보로만 기록한다. 이 단계에서 빌드는 다시 실행하지 않는다.

## 시작과 소스 경로

스킬 폴더에서 실행하는 예시. `D:/build/app`은 실제 오류 명령을 실행한 작업 폴더로 바꾼다.

```powershell
python "${SKILL_ROOT}/scripts/cover_recovery.py" discover --source src/main.cpp --project-root D:/build/app
python "${SKILL_ROOT}/scripts/cover_recovery.py" start --source src/main.cpp --project-root D:/build/app --run-dir D:/recovery/case-01
```

- `discover`: 설정과 로컬 자료를 읽어 후보·누락 조건을 반환한다. 서버 요청은 없다.
- `start`: `--artifacts`가 없으면 소스·HardwareID에 맞는 최신 오류를 서버에서 수집한다. `--artifacts`를 지정하면 서버 없이 제공 자료의 복사본을 만든다.
- 상대 소스 경로는 `--project-root` 기준이다. 저장소 루트나 스킬 폴더를 빌드 작업 폴더로 가정하지 않으며, 작업 폴더가 불명확하면 그 정보만 확인한다.
- 다른 OS의 절대 경로를 현재 PC 경로로 바꾸지 않고 서버 자료의 전체 소스 경로와 대조한다.

## 읽는 설정과 탐색 범위

환경 전체를 출력하지 않고 아래 키만 사용한다. 사용자명·암호 값은 결과와 로그에 남기지 않는다.

| 키·파일 | 용도와 주의점 |
|---|---|
| `CODESCROLL_TM_INSTALL_PATH` | Agent 설치 루트. `--agent-root`로 지정 가능 |
| `CODESCROLL_TM_SERVER_URL` | Agent가 remote-service와 통신할 때 사용하는 주소. 오류 자료 API 주소로 자동 사용하지 않음 |
| `<agent>/bin/agent-config.yaml`의 `usingFixedServerUrl`, `fixedServerUrl` | 분석 서버 설정 힌트. **Open API 주소를 덮어쓰지 않는다** |
| `CATALINA_HOME` | 로컬 서버 설치 후보. 현재 접속 서버라는 증거는 아님 |
| `COVER_ACCESS_KEY` | 인증 환경변수의 존재 여부만 보고. 다른 이름은 `--access-key-env`로 지정 |
| `COVER_WORKSPACE`, `COVER_STANDALONE_INSTALL_PATH`, `COVER_INSTALL_PATH` | Agent 문맥이 없을 때의 보조 탐색 힌트. 제품의 확정 오류 저장 경로로 취급하지 않음 |
| 빌드 계정의 `.cstm_HardwareAddress` | 기존 HardwareID 확인용. 다른 계정이면 `--hardware-file` 등으로 대상 확인 |

기본 로컬 범위는 빌드 폴더·소스 부모, 확인된 Agent 루트의 `tmp`·`logs`, 제한된 서버 설치 후보와 다운로드 ZIP이다. Agent와 standalone 설치의 작업 폴더는 섞지 않는다. 추가 범위는 `--search-root`, 서버 위치는 `--server-root`로 지정한다.

보조 코드는 활성 프로세스나 로그 본문으로 현재 빌드 환경을 증명하지 않는다. `server.xml`의 같은 포트도 실행 중 서버의 증거가 아니다. 필요하면 에이전트가 프로세스·관련 로그를 읽기 전용으로 확인하고 위치를 다시 입력한다.

## 서버의 실제 저장 위치

서버 설정 파일은 `<catalina.home>/properties/` 아래에 있다. `EXTENAL`은 제품 코드의 실제 철자다.

| 값 | 설정과 기본값 |
|---|---|
| `external` | `remote-service.properties`의 `Configuration.EXTENAL_PATH`; 없으면 `parent(catalina.home)/external` |
| `workBase` | 같은 파일의 `Configuration.WORK_PATH`; 없으면 `<external>/workspace` |
| `issuePath` | `issue-manager.properties`의 `Configuration.ISSUE_PATH`; 없으면 `<external>/issue` |

```text
분석 중: <workBase>/workspace/<tuHash>_<sessionId>/
전체 자료: <issuePath>/<issueId>/issue_<issueId>.zip
소스 제외: <issuePath>/<issueId>/issue_NoSrc_<issueId>.zip
```

작업 폴더에는 `project.db`, `global.db`, `pa.ini`, `console.log`, `src/preprocessed` 등이 들어간다. PA 오류 저장은 전체 ZIP 생성 → `src` 제거 → NoSrc ZIP 생성 → 원 작업 폴더 삭제 순서다. 따라서 작업 폴더가 사라졌어도 issue 저장소를 확인한다. `<workBase>/error`는 구형 자료 탐색 힌트일 뿐, 현재 PA 오류 보관 위치를 대신하지 않는다.

저장 용량 정리로 자료가 영구 보존되지 않을 수 있다. NoSrc ZIP에는 PA 재검증에 필요한 전처리 소스가 없을 수 있다.

## 후보 선택과 서버 확인

`project.db`의 `SourceFile.path`로 전체 소스 경로를 확인한다. 파일명·ZIP 수정 시각·소스 basename만으로 선택하지 않는다.

| 발견 결과 | 처리 |
|---|---|
| 자동 발견한 로컬 자료가 하나이고 완전함 | HardwareID를 증명하지 못하므로 서버 API에서 확인·수집 |
| 불완전 자료, 여러 후보, 후보 없음 또는 탐색 한도 초과 | 서버 API에서 전체 소스 경로·HardwareID가 일치하는 최신 오류 수집 |
| `--hardware-id` 명시 | 빌드 계정 파일 대신 지정한 HardwareID로 서버 조회 |
| 사용자가 직접 제공한 오류 ZIP·폴더를 `--artifacts`로 지정 | 서버 접속 없이 제공된 자료로 시작 |

대상 TU에 연결된 `.elog`가 없다는 사실은 진단 종료 조건이 아니다. 자동 수집은 항상 서버 Issue API에서 소스 경로와 HardwareID가 일치하는 오류를 조회하고 전체 ZIP을 다운로드한다. 이 단계에서 사용자에게 ZIP, `.elog`, Issue ID 제공을 요구하지 않는다.

현재 Open API는 `GET /v1/issue/download`에 `macAddress`, `sourceFilePath`, `withSource=true`, `issueWorkType=PA_ERROR_RESULT`를 받아 최신 오류 ZIP을 바로 반환한다. 서버의 경로 매칭은 구분자·대소문자를 무시한 끝부분 일치이므로 소스 전체 경로를 전달하고 ZIP 내부 DB의 경로를 다시 확인한다. 오류 목록, Issue ID, 발생 시각은 응답에 없으며 클라이언트에서 추정하지 않는다.

**ZIP DB만으로 HardwareID를 증명하지 않는다.** ZIP 이름은 Issue ID, 내부 폴더는 TU 해시와 세션 ID를 사용한다. 소스 경로와 HardwareID만으로 파일명을 계산할 수 없다. ZIP 이름에서 읽은 Issue ID는 파일명 힌트일 뿐 조회 입력이나 다운로드 ID로 사용하지 않는다. 다운로드에는 MAC 주소와 소스 전체 경로, `PA_ERROR_RESULT`를 전달한다. `case.json.collection`에는 요청 조건과 API 경로를 기록하며, 응답에 없는 Issue ID·발생 시각을 추정하지 않는다.

## 중단 규칙

- 서버 확인이 불가능하면 후보, 탐색한 루트, 한도 초과 여부와 누락된 접근 조건을 남긴다. 확인되지 않은 자료로 조용히 진행하거나 곧바로 ZIP 제공을 요구하지 않는다.
- 필요한 정보만 확인한다: 실제 빌드 작업 폴더, 메인 서버 주소, 빌드 Agent 식별, 인증 환경변수 또는 좁힌 탐색 위치. 여러 오류의 Issue ID는 사용자에게 묻지 않는다.
- HTTP는 Access key와 자료가 평문으로 전송되므로 사용하지 않는다. HTTPS 메인 서버 주소가 없으면 `https_server_url`을 누락 조건으로 남긴다.
- 드라이브 전체·사용자 홈 전체의 재귀 탐색으로 범위를 넓히지 않는다. 탐색 결과가 잘렸으면 전체를 확인했다고 보고하지 않는다.
- 원본 ZIP·소스·DB·설정을 수정하지 않는다. 탐색·수집 단계에서 Wrapper·컴파일러·PA를 재실행하지 않는다. 서버 변경은 탐색·수집 단계의 대상이 아니다.
