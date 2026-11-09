# 서버 TMPA 실행 계약

클라이언트는 Python 표준 라이브러리로 COVER 서버에 검증을 요청하고 실행 상태와 오류 자료를 조회한다.

## 서버 요청

[서버 API 계약](server-api.md)의 세 `/ai-agent-kit/tmpa-run*` API를 사용한다. 인증과 TLS 정책은 자료 수집·툴체인 등록과 같다. `/v1` 접두어는 붙이지 않는다.

서버에서 수집한 `case.json.collection`의 서버·HardwareID와 소스 전체 경로를 사용한다. 로컬 자료만 있는 case는 `NeedsInput`이다. 서버에서 `start`/`collect`로 새 case를 수집한 뒤 그 자료로 후보를 다시 작성한다.

`proposed.conf`의 전체 줄을 `toolChainContent`로 보내고 DB에서 확인한 `compilerId`를 보낸다. COVER가 만든 일반 이슈의 번호는 1이지만 코드는 DB 값으로 결정한다. 이슈의 원래 경로를 유지하며 서버가 DB·INI·경로를 처리한다.

`platform`은 빌드 PC의 OS(win/linux)다. 서버 실행 환경·컴파일 대상 `target`과 구분한다. 생략하면 서버가 이슈 경로로 추론한다. 드라이브·UNC 경로는 win, 나머지는 linux다.

## 실행과 재개

POST 직전 요청 내용·digest·시작 시각을 case에 저장한다. 200 응답의 32자리 16진수 `runId`도 즉시 저장한다. 약 2초마다 상태를 읽는다. 한 CLI 호출은 최대 60초간 조회하고, 아직 RUNNING이면 `VerificationBlocked`/`PollingPending`으로 반환한다. 같은 후보와 `verify` 명령으로 재개하면 기존 runId만 조회한다. 서버 실행은 30분 제한이며 로컬 조회 시간 만료가 취소를 뜻하지 않는다.

진행 중인 실행의 후보·platform을 바꾸지 않는다. 전송 결과가 불명확하거나 프로세스가 POST 도중 종료됐다면 새 POST를 자동 재전송하지 않는다. 이미 발급된 runId는 네트워크 오류 뒤에도 보존한다. 완료된 HTTP 4xx·5xx는 명세상 실행이 생성되지 않은 거절이다. 원인을 해결한 뒤 남은 시도 안에서 새 요청할 수 있다.

결과는 종료 후 1시간 보관하며 서버 재시작이나 만료 후 GET은 404다. 코드가 자동으로 다시 실행하지 않는다. 만료된 실행은 성공으로 간주하지 않고 서버 기록과 추가 조치를 확인한다. 새 실행 한도는 case당 3회이며 조회 재개·오류 ZIP 재다운로드는 같은 시도다.

## 판정과 오류 자료

| 서버 상태 | CLI 판정 | 처리 |
| --- | --- | --- |
| RUNNING | VerificationBlocked / PollingPending | 같은 runId로 계속 조회 |
| SUCCEEDED | Recovered | 정상 종료·계측 결과 생성이라는 서버 판정. 성공 ZIP 없음 |
| FAILED | RecoveryFailed | 오류 ZIP 다운로드·안전한 압축 해제 후 진단 |
| TIMEOUT | Timeout | 오류 ZIP 수집. DB는 진단용으로만 사용 |
| ERROR | VerificationBlocked | 서버 로그에서 runId 확인. 오류 ZIP 없음 |

`attempts/<번호>/server-result.json`과 case에 서버 상태·runId·tmpaRevision·전송 설정 해시를 저장한다. 실패 ZIP은 같은 시도의 `error-archive.zip`, 해제 결과는 `error-artifacts/`다. 다운로드 실패는 원래 FAILED/TIMEOUT 판정을 보존하며 같은 명령으로 다운로드를 재개한다. 보존 입력을 덮어쓰지 않는다.

원래 오류 ZIP의 `console.log` REV와 응답 `tmpaRevision`을 비교한다. null은 버전 미확인이다. 서버가 최신 이슈를 선택하므로 수집한 ZIP과 실행 사이에 이슈가 바뀔 수 있고 Issue ID를 고정할 수 없다. 동시 빌드를 피하고 다른 이슈가 생겼으면 자료와 후보를 갱신한다.

툴체인 등록은 별도 단계다. 동일 서버·Agent, 요청 digest, 설정 해시, compilerId, 소스 경로와 SUCCEEDED 기록이 일치해야 `publish-plan`으로 이어진다. PA 성공은 프로젝트 재빌드·커버리지 수집 성공이 아니다.
