# 로컬 COVER PA 실행·판정 계약

`Recovered`는 이번 복사본에서 대상 PA 분석이 완료됐다는 뜻이다. 원본 빌드·커버리지 측정·`.cvd`·서버 반영 성공은 포함하지 않는다.

## 배포 구조와 런타임 확인

PA 실행 파일은 스킬과 함께 다음 위치에 배포한다. 실행할 PC의 OS에 맞는 파일을 넣는다.

```text
cover-toolchain-recovery/
├── SKILL.md
├── scripts/
├── references/
└── assets/
    └── tmpa/
        └── tmpa.exe  (Windows) 또는 tmpa (Linux)
```

`${SKILL_ROOT}/assets/tmpa/`를 포함해 스킬 폴더 전체를 배포한다. Linux의 `tmpa`는 실행 권한을 유지한다. 필요한 라이브러리·설정·probe 자원은 같은 버전으로 원래 상대 위치에 넣는다. 단독 실행 파일이면 추가 자원은 필요 없다. `.gitkeep`은 배포 전 폴더 보존용이며 실제 PA 파일은 배포 시 포함한다.

`inspect_runtime(pa_root=None, executable=None)`는 경로를 생략하면 이 모듈이 속한 스킬의 `assets/tmpa/`를 읽기만 한다. 현재 작업 폴더나 PATH, 서버에서 PA를 찾지 않는다. `start`, `collect`, `runtime` 모두 같은 기본 경로를 사용한다.

현재 소스의 `assets/tmpa`에는 `.gitkeep`만 있고 실제 PA는 없다. `.gitkeep`과 빈 폴더는 ZIP에서 생략되므로 배포본에서는 `RuntimeNotFound`, 소스의 빈 폴더에서는 `ExecutableNotFound`가 나올 수 있다. 둘 다 실제 PA가 없다는 뜻이며 성공으로 처리하지 않는다. 배포 자산은 이 경로에 넣고 기존 case의 PA 절대경로는 자동으로 바꾸지 않는다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" runtime
```

`verify`는 case에 저장된 PA 경로를 다시 확인한다. 경로가 없는 이전 case는 `${SKILL_ROOT}/assets/tmpa/`를 사용한다. `--pa-root` 또는 `--pa-executable`은 저장된 선택보다 우선하며 다른 PA를 선택할 때만 쓴다. `--pa-root`는 PA 폴더 또는 단독 실행 파일이고, 둘을 함께 쓰면 실행 파일의 상대 경로는 PA 폴더 기준이다. `--pa-executable`만 쓰면 현재 작업 폴더 기준이다.

`tmpa`가 모든 런타임을 내장한 단독 실행 파일이면 `--pa-root tmpa.exe` 또는 `--pa-executable tmpa.exe`로 지정할 수 있다. 이 모드에서는 주변 DLL·설정·probe 파일을 검사하지 않고 실행 파일만 fingerprint한다.
`runtime`은 다운로드·설치·PA 실행을 하지 않는다. 파일 누락이나 OS 불일치면 `available=false`, `blockers`, 종료 코드 2를 반환한다. `start`·`collect`·`verify`는 `GuidanceOnly`로 안내하고 확인 경로·사유를 기록한다. 서버 다운로드나 별도 PA 경로로 전환하지 않는다.

- `tmpa` 또는 `tmpa.exe` 후보가 여러 개면 실행 파일을 직접 지정해야 한다.
- PE/ELF 형식, 호스트 OS, 실행 파일과 주요 리소스의 SHA-256을 기록한다.
- `VERSION`·README는 참고 자료다. 폴더명이나 문서만으로 바이너리 버전을 확정하지 않는다.
- 버전 근거가 없으면 `Unknown`으로 둔다. 이 사유만으로 실행 불가 처리하지 않는다.
- 실행 직전에 기록한 실행 파일·리소스가 사라지거나 바뀌면 중단한다.
- 이 확인은 동적 라이브러리 로더·라이선스·모든 배포 의존성을 검증하지 않는다.

반환값의 `available=true`는 로컬 후보를 찾았다는 뜻이며, PA 실행 성공 보증이 아니다.

## 실행 전 조건

공개 함수:

```python
run_pa(workspace, runtime, expected_conf, compiler_id, timeout_seconds=600)
```

호출자는 회차마다 새 검증 폴더와 `verification.json`을 준비한다. manifest에는
`schema_version=1`, `compiler_id`, `selected_tu_ids`, `expected_conf`,
`original_hashes`, `workspace_hashes`, `path_audit`가 들어간다.

- `path_audit.safe=true`이고 미해결 경로가 없어야 한다.
- `project.db`, `global.db`, `pa.ini`, 선택한 conf의 경로와 해시를 다시 확인한다.
- 실행 대상 TU(분석 파일 단위)는 manifest 목록과 정확히 같아야 한다.
- 대상 TU의 Compiler 행은 선택한 복사본 conf를 가리켜야 한다.
- SourceFile 경로는 복사본 안에 있어야 한다. 원본 `.c/.cpp`가 없는 오류 묶음은
  같은 TU의 `src/preprocessed/<stem>_<id>.i.c` 또는 `.i.cpp`가 있으면 사용할 수 있다.
- 지원하지 않는 DB 구조·입력 배치, 외부 경로, 링크, 변경된 입력은 실행 전에 중단한다.
- `.pa-run`이 이미 있는 폴더는 재사용하지 않는다.

실행 인자는 별도 문자열 배열이다. 셸 문자열로 조립하지 않는다.

```text
tmpa <복사본/project.db> <복사본/global.db> win|linux <복사본/pa.ini>
```

작업 디렉터리와 임시 파일 경로는 검증 폴더 안으로 지정한다. stdout·stderr는
`.pa-run/stdout.log`, `.pa-run/stderr.log`에 새로 저장한다. 기본 제한은 600초이며
timeout 후 같은 회차를 자동 재실행하지 않는다. 프로세스 트리 종료 결과도 기록한다.

경로 확인은 OS sandbox가 아니다. 준비기는 DB·INI·conf와 런타임 로그 설정의
실제 입출력 경로를 확인하며, 설명할 수 없는 경로는 `path_audit.safe=false`로 남긴다.
원본 해시의 사후 검증은 상위 워크플로가 수행한다.

## 성공 판정

판정기는 이번 PA 실행의 콘솔·로그, DB 변화, 계측 산출물을 대조한다. 종료 코드 0만으로는 부족하며 다음 증거가 모두 필요하다.

1. 이번 실행의 콘솔 정상 완료 순서:
   `VALUE1 → VALUE34 → PHASE21002 → VALUE67 → VALUE101 → VALUE100`.
2. 대상마다 새 `PHASE21001 ... (Parsing)` 표시가 있음. `end parsing`은 프로젝트 로그가 생성된 경우의 보조 증거임.
3. 대상마다 DB의 `isAnalyzed`가 이번 실행에서 0에서 1로 바뀜.
4. 대상마다 이번 실행의 `src/instrumented/<stem>_<id><suffix>` 출력이 있음. DB에 기록된 소스 이름과 TU ID로 정확한 경로를 계산하며, `.cxx`, `.c++`, 대문자 확장자, 확장자 없는 소스도 같은 규칙을 적용함. 파일의 크기·수정 시각·해시를 실행 전후 비교하므로 이전 결과가 그대로 남았거나 다른 TU·확장자의 파일만 생성된 경우 성공 근거가 아님.
5. 프로젝트 로그에 오류 출력 경로가 있으면 검증 폴더 안이어야 함. 프로젝트 로그가 생성되지 않았으면 실행 후 내용 있는 `.elog`가 하나도 없어야 함. `pa.main: end time:`은 참고 정보이며 필수 조건이 아님.
6. 새 파싱·Fatal·Internal 오류가 없고, 대상 오류 파일에 남은 내용도 없음.
7. 인자로 넘긴 DB가 선택한 conf 경로를 계속 참조하고 conf·INI가 바뀌지 않음.

`.elog`가 실행 후 없어졌거나 생성되지 않았고, 새 Parsing 진행·DB 분석 전환·계측 소스·PA 완료 마커가 확인되면 `Recovered`다. 이때 프로젝트 로그의 `end parsing`과 `--error_output`은 필수로 요구하지 않는다. 빈 `.elog`도 오류 없음으로 본다. 내용 있는 `.elog`가 남아 있으면 새 파일이든 이전부터 있던 파일이든 회복 실패다.

프로젝트 로그가 이전 실행을 포함하면 이번에 추가된 바이트만 오류 판정에 사용한다. 오래된 완료 표시는 재사용하지 않는다.

`active_config.database_reference_verified`는 전달한 DB의 참조·해시 확인 결과다.
실제 런타임의 conf 읽기 추적을 뜻하지 않으므로 `runtime_load_trace=false`로 별도 표시한다.

## 상태와 한계

| 상태 | 의미 |
| --- | --- |
| `Recovered` | 모든 대상의 새 PA 분석 증거를 확인함 |
| `RecoveryFailed` | 새 분석 오류 또는 대상 `.elog`의 미해결 오류를 확인함 |
| `VerificationBlocked` | 환경·timeout·경로·DB·버전 충돌 또는 검증 근거 부족 |

알 수 없는 콘솔 형식, 일부 TU만의 증거, 0으로 종료했지만 분석하지 않은 실행은 원인을 추측하지 않고 `VerificationBlocked`로 보고한다.

## 테스트

테스트는 임시 폴더의 합성 DB·소스와 Python 모의 자식 프로세스만 사용하며 고객 자료나 실행 파일을 fixture로 복사하지 않는다.

```text
python -B -m unittest discover -s tests -p test_pa.py
```

모의 테스트 통과와 실제 Windows/Linux PA 실행·서버 반영·native 빌드 검증은 별도 단계다. 추가 출력 형식은 근거를 확보한 뒤 판정기에 반영한다.
