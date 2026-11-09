# COVER 툴체인 복구

C/C++ 분석 오류의 근거를 모아 설정 변경안을 만들고 **COVER 서버에서 tmpa를 실행**해 검증한다.

흐름: 서버 Issue 수집 → LLM 진단 → 후보 설정 작성 → tmpa-run 요청·조회 → 검증한 설정으로 새 툴체인 등록·Wrapper 연결 → 재빌드 인계.

## 명령

Python 3.11 이상, 표준 라이브러리만 사용한다. `${SKILL_ROOT}`를 실제 스킬 경로로 바꾼다. Access key는 `COVER_ACCESS_KEY` 환경변수에서 읽으며 명령 인수나 보고서에 쓰지 않는다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" start --project-root <빌드폴더> --source <소스> --run-dir <새폴더> --server <HTTPS서버>
python "${SKILL_ROOT}/scripts/cover_recovery.py" verify --case <case.json> --candidate <후보.json>
python "${SKILL_ROOT}/scripts/cover_recovery.py" publish-plan --case <case.json> --server <HTTPS서버> --hardware-id <MAC> --wrapper <Wrapper>
python "${SKILL_ROOT}/scripts/cover_recovery.py" publish --case <case.json> --server <HTTPS서버> --plan-digest <digest>
```

`start --artifacts <ZIP/폴더>`는 로컬 진단을 지원한다. 서버 API는 로컬 ZIP 업로드를 받지 않으므로 검증에는 서버에서 수집한 case가 필요하다. `discover`는 자료 탐색, `collect`는 서버 오류 ZIP 수집, `inspect`는 case 조회다.

## 서버 검증

- POST `/cover-open-api/ai-agent-kit/tmpa-run`: MAC·소스 전체 경로·compilerId·전체 설정 줄을 전송한다.
- GET `/cover-open-api/ai-agent-kit/tmpa-run/{runId}`: 약 2초 간격으로 조회한다. RUNNING이면 같은 명령으로 조회를 이어간다. POST는 반복하지 않는다.
- GET `/cover-open-api/ai-agent-kit/tmpa-run/{runId}/error-archive`: FAILED/TIMEOUT의 이번 오류 ZIP을 받는다.

`platform`은 소스를 빌드한 PC의 win/linux다. 생략하면 서버가 이슈 경로로 정한다. 서버 OS나 컴파일 대상이 아니다. `--timeout`은 한 CLI 호출의 조회 대기(1~60초), 서버 실행은 최대 30분이다. 결과는 종료 후 1시간 보관된다.

SUCCEEDED는 서버가 정상 종료·계측 결과 생성을 확인했다는 뜻이다. 성공 ZIP은 제공하지 않는다. tmpaRevision이 없으면 버전은 미확인이다. 실패 ZIP은 `attempts/<번호>/error-archive.zip`, 로그는 `error-artifacts/`에 저장한다.

## 보존과 반영

원본 소스·오류 자료는 바꾸지 않는다. 후보는 근거가 있는 최소 설정 변경이며 의미를 제거하는 무시·치환 옵션은 자동 적용하지 않는다. 새 실행은 최대 3회다. 검증한 설정 해시·요청 digest·서버·Agent·소스·compilerId가 일치해야 등록한다. PA 검증 성공과 서버 반영, 재빌드, 커버리지 수집은 별도 결과다.

실행 API는 최신 서버 Issue를 선택하며 Issue ID 고정 기능은 없다. 수집 후 동시 빌드를 피한다. 소스를 수정했다면 최신 결과를 먼저 확인한다. 서버 기록과 로컬 ZIP이 항상 동일하다고 보장하지 않는다.

[스킬 절차](SKILL.md), [후보 작성](references/local-workflow.md), [서버 실행](references/server-execution.md), [API 계약](references/server-api.md), [반영·되돌리기](references/server-publication.md)를 참고한다.

테스트는 모의 HTTP 서버와 합성 오류 자료로 실행한다. 실제 서버 tmpa·툴체인 변경·재빌드를 수행하는 통합 검증과 구분한다.
