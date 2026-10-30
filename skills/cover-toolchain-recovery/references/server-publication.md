# 서버 반영과 되돌리기

로컬 PA 검증과 서버 변경은 별도 명령이다. `verify`의 성공은 `Recovered`이며 이 명령 자체는 서버 설정·Agent를 바꾸지 않는다. 복구 작업에서는 성공 직후 계획과 서버 반영을 추가 승인 없이 이어간다. 진단·로컬 검증만 요청한 경우에는 적용하지 않는다. 기존 툴체인 수정·삭제와 재빌드는 제공하지 않는다.

## 1. 읽기 전용 반영 계획

[서버 API 계약](server-api.md)을 읽는다. 본문은 PA 검증을 통과한 배포용 설정을 사용하며 기존 서버 본문은 조회·비교하지 않는다. 서버에서는 연결 대상과 툴체인의 식별·표시 정보를 확인한다. 입력은 `case.json`, 메인 서버 환경변수, HardwareID 파일, 실제 빌드 명령에서 찾는다. Wrapper는 서버에 등록된 정확한 이름을 쓴다. 실행 경로와 `wrapperName`이 다르면 조회로 대응을 확인하고 하나로 정해지지 않으면 중단한다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" publish-plan --case <case.json> --server <HTTPS서버URL> --hardware-id <HardwareID> --wrapper <등록된Wrapper이름>
```

`publish-plan`은 현재 서버와 PA 검증 설정을 비교해 `publish-plan.json`과 `rollback.json`을 기록한다. 서버 쓰기는 없다. 계획에는 다음이 고정된다.

- 서버 URL, HardwareID, Agent DB ID, Wrapper, 이전 툴체인 ID와 표시 정보, 전체 Agent 스냅샷
- 검증 설정·PA 결과의 해시, 새 툴체인 내용·이름·버전, 표시 정보의 근거
- 변경 계획의 `plan_digest`

GCC는 검증된 `gnu_version`에서 표시 버전을 맞춘다. 예를 들어 `gnu_version=120300`은 major `12`, minor `3`, 이름은 `12.3.0`이다. 확인된 target만 이름에 넣고 배포판은 추측하지 않는다. 기존 툴체인은 보존하고 새 항목을 만든다.

## 2. 검증 후 서버 반영

서버 URL, HardwareID/Agent, Wrapper, 기존·새 툴체인 이름·버전과 설정 diff가 검증된 계획과 일치하면 바로 진행한다. 변경 내용은 진행 상황으로 알리고 사용자 승인을 기다리지 않는다. 스킬 코드·문서만 수정하는 작업에서는 실제 서버를 변경하지 않는다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" publish --case <case.json> --server <HTTPS서버URL> --plan-digest <생성한계획의digest>
```

- 별도 승인 옵션 없이 실행한다. Access key를 읽기 전에 PA 검증 상태, 계획 digest와 대상 서버를 확인한다.
- `publication_execution`에 실행한 계획 digest와 시작 시각을 기록한다. Access key 인증은 기존 Open API 계약을 따른다.
- `InvalidPublishPlan`이면 `publish-plan`으로 계획을 다시 만든다. 대상·설정·표시 정보가 바뀌면 변경된 조건을 다시 검증한다.
- 반영 직전에 전체 Agent 설정·Wrapper 연결과 이전 툴체인의 식별·표시 정보를 다시 대조한다. 변경됐으면 `StaleTarget`/`StaleBaseline`으로 중단한다. ETag 조건부 갱신이 없어 마지막 조회 이후의 동시 변경은 막지 못하므로 대상 설정의 동시 편집·빌드를 피한다.
- 새 툴체인 생성 → 지정 Wrapper 연결만 변경 → GET으로 저장된 연결 확인 순서다. 다른 Wrapper·설정·기존 툴체인을 보존한다.
- 성공해도 `ReadyForRebuild`까지만 기록한다. 이후 재빌드와 측정 검증은 빌드 요청의 호출자가 맡는다.

## 3. 실패한 반영의 확인

쓰기 응답이 모호하거나 중단되면 재전송하지 않는다. `case.json`의 체크포인트와 `rollback.json`을 보고 서버를 읽기 전용으로 재조회한다.

- `Unknown`: 새 툴체인·연결이 생겼을 수 있다. 기록한 새 ID 또는 고유 이름·표시 정보로 생성 여부를 확인한다. 응답에 없는 저장 본문까지 확인했다고 기록하지 않으며, 후보가 확정되기 전에는 생성·되돌리기를 반복하지 않는다.
- `ToolchainCreated`: 새 항목은 확인됐지만 Wrapper 반영은 미확인 또는 실패다. 현재 연결을 확인한다.
- `ServerApplied` + `NeedsAttention`: 서버 연결은 확인됐지만 인계 기록 등이 실패했다. `last_error`와 인계 파일을 확인하며 재빌드 준비 완료로 표시하지 않는다.

## 4. 현재 상태 확인 후 되돌리기

자동 rollback 명령은 없다. `rollback.json`은 실행 지시가 아닌 복구 인계 기록이며 상태는 `NotExecuted`로 남긴다. 기존 툴체인을 삭제하지 않으므로 이전 연결을 복원할 수 있다. 새 툴체인 ID가 없으면 생성 결과가 미확인일 수 있다.

1. `rollback.json`의 `plan_digest`와 `publish-plan.json`의 계획 해시를 대조한다. 서버 URL·HardwareID·Agent DB ID·Wrapper·새/이전 툴체인 ID를 확인한다.
2. 현재 전체 Agent와 양쪽 툴체인을 GET으로 읽는다. 이전 툴체인의 ID·표시 정보는 계획의 `previous_toolchain`과 같아야 한다. 본문은 조회하지 않는다. 현재 연결이 이번 새 ID인지 확인한다. 제3의 툴체인으로 바뀌었거나 조회가 불명확하면 중단한다. 이미 이전 ID라면 연결 PATCH를 생략하고 현재 연결을 기록한다.
3. 실패 복구에 필요한 대상과 연결 변경 내용이 확인되면 별도 승인 없이 진행한다. 상태가 모호하면 추가 조회로 확인하며 추측해서 되돌리지 않는다.
4. 최신 `toolChains` 목록에서 지정 Wrapper의 `toolChainInfoId`만 복원하고 전체 목록을 PATCH한다. `aliasName`은 서버가 참조에서 반환한다. 저장해 둔 과거 Agent 스냅샷을 그대로 PATCH하지 않는다. 관련 없는 후속 변경을 덮어쓸 수 있기 때문이다.
5. 기존 클라이언트의 `update_agent_mapping`을 쓴다면 `new_toolchain_id=restore_toolchain_id`, `expected_old_toolchain_id=이번 새 ID`, `expected_snapshot=직전 GET 결과`로 전달한다. 이전 연결이 저장됐는지 GET으로 확인한다. 모호한 쓰기는 재전송하지 않는다.
6. 되돌리기 요청·실제 결과·확인 시각을 별도 기록한다. 새 항목과 기존 항목은 모두 보존한다. 사용 중일 수 있는 툴체인 삭제는 이 절차에 포함하지 않는다. 이전 설정 복원은 PA 오류 복구 성공을 의미하지 않는다.

서버 URL·Access key 등 보안 처리는 Open API 계약을 따른다. 이 문서는 인증이나 TLS 정책을 바꾸지 않는다.
