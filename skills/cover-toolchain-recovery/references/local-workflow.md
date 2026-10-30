# 로컬 오류 자료로 시작하기

PA 실행 파일은 `${SKILL_ROOT}/assets/tmpa/`에 배포한다. Windows는 `tmpa.exe`, Linux는 `tmpa`를 쓴다. 오류 자료만 확보하면 별도 PA 경로 없이 검증할 수 있으며, 서버 다운로드는 자료 수집에만 사용한다. 동봉 PA를 쓸 수 없으면 `GuidanceOnly`로 기록하고 배포 파일을 점검한다.

Python 3.11 이상과 표준 라이브러리만 사용한다. 아래 경로와 소스 이름은 사용 환경에 맞게 바꾼다. `--source`는 DB의 원래 `SourceFile.path` 값이다. 상대 소스명을 사용하면 `--project-root`에 실제 빌드 작업 폴더를 지정한다.

ZIP·폴더가 없으면 [오류 자료 탐색](artifact-discovery.md)을 따른다. `start`의 `--artifacts`를 생략하면 소스 전체 경로와 HardwareID로 서버에서 최신 오류를 찾는다. 자동 발견 자료는 서버 확인을 대신하지 않으며 Issue ID는 서버 조회 결과로만 보관·전달한다.

## 1. 오류 자료와 동봉 PA 확인

```powershell
$skill = 'D:\04_AI_Workspace\cover-toolchain-recovery'
python "$skill\scripts\cover_recovery.py" start `
  --artifacts 'D:\cases\error.zip' `
  --source 'C:\build\src\main.cpp' `
  --run-dir 'D:\cases\runs\case-001'
```

`--artifacts`에는 폴더도 지정할 수 있다. `--run-dir`은 없는 폴더여야 하며 원본 자료 폴더 안에 만들 수 없다. 기존 결과는 덮어쓰지 않는다.

생성 결과:

| 위치 | 내용 |
| --- | --- |
| `inputs/` | 원본 자료의 보존용 복사본. 이후 수정 금지. |
| `case.json` | 원본·복사본 해시, 소스와 컴파일러 연결, 오류 근거, 상태. |
| `report.md` | 자동 관측·시도 기록. PA·서버·Agent·재빌드 인계 상태를 구분. |
| `diagnosis.md` | LLM이 별도로 작성하는 판단 근거·가설·추가 자료 요청. |

ZIP의 상위 폴더 이동, 절대 경로, Windows 장치명·ADS, 링크, 중복 이름은 거부한다. 한 번에 최대 100,000개 항목·압축 해제 크기 합계 2GiB까지 처리한다. ZIP 안의 ZIP을 자동으로 다시 풀지는 않는다.

`project.db`, `global.db`, `pa.ini`와 대상 컴파일러의 `.conf`를 확인한다. 필요한 자료가 없거나 대상이 여러 개면 `blockers`에 이유를 남기며, 추측으로 하나를 고르지 않는다. 미반영 WAL/저널이 있는 SQLite DB는 먼저 정상적으로 체크포인트한 자료가 필요하다.

`evidence.missing`은 보조 코드가 점검하는 파일·필드의 누락만 나타낸다. 빈 배열이어도 컴파일러 버전·전체 빌드 명령·오류와 코드의 일치가 확인됐다는 뜻은 아니다. LLM은 이를 대조해 근거와 추가 자료를 `diagnosis.md`에 기록한다.

## 2. 진단 근거를 후보 JSON으로 정리

[분석 설정 진단 참고서](analysis-guide.md)에 따라 현재 오류·컴파일 명령·버전·활성 설정을 대조한다. 보조 코드는 관측한 값을 추출하며, 특정 오류 문자열을 고정된 해결 설정으로 바꾸지 않는다.

다음은 JSON 형식 예시다. GCC 12.3.0 사용을 확인한 경우에만 이 버전 변경을 후보로 삼는다. 경로·행 번호·숫자는 실제 근거로 바꾼다.

```json
{
  "reason": "실제 GCC 12.3.0과 분석 설정의 버전 값이 다름을 확인",
  "evidence": [
    "compiler-version.txt:1: 실제 실행 파일의 GCC 12.3.0 출력",
    "active.conf:3: gnu_version=100300"
  ],
  "replacements": [
    {"old": "gnu_version=100300", "new": "gnu_version=120300"}
  ],
  "append_lines": []
}
```

- `reason`: 비어 있지 않은 변경 이유. `evidence`: 비어 있지 않은 근거 문자열 배열.
- `old`: 원본 `.conf`의 정확한 한 줄. 공백을 포함해 한 번만 일치해야 한다.
- `new`: 교체할 한 줄. 빈 문자열이면 해당 줄 전체 삭제. 줄바꿈 문자는 넣지 않는다.
- `append_lines`: 마지막에 추가할 줄 배열. 추가할 내용이 없으면 빈 배열.
- 각 시도는 `inputs/` 원본 설정에서 다시 시작한다. 이전 시도의 변경도 필요하면 현재 JSON에 함께 포함한다.

UTF-8 BOM, UTF-16 LE/BE BOM, CP949와 기존 개행을 보존한다. BOM이 없으면 UTF-8 우선·CP949 순으로 손실 없이 읽을 수 있는지 확인하며, `encoding_is_inferred=true`로 표시한다. 두 인코딩으로 모두 읽히는 바이트는 이 방법만으로 원래 인코딩을 확정할 수 없다. 글자가 이상하면 원본 인코딩 근거를 먼저 확인한다. 혼합 개행이나 원래 인코딩으로 표현할 수 없는 변경은 자동 적용하지 않는다.

## 3. 동봉된 tmpa로 복사본 검증

기본 PA 경로는 현재 작업 폴더와 관계없이 `$skill\assets\tmpa`다. `start`·`collect`에서 선택한 경로는 case에 저장해 `verify`에서 다시 사용한다. PA 경로가 없는 이전 case도 동봉 PA를 확인한다. 다른 PA의 경로·배포 구조는 [PA 실행 계약](pa-runtime.md)을 따른다.

```powershell
python "$skill\scripts\cover_recovery.py" runtime

python "$skill\scripts\cover_recovery.py" verify `
  --case 'D:\cases\runs\case-001\case.json' `
  --candidate 'D:\cases\candidate.json'
```

`runtime`은 배포 파일 점검이지 PA 실행 성공이 아니다. 동봉 PA가 없거나 사용할 수 없으면 `verify`도 `GuidanceOnly`로 끝나며 후보 적용·서버 변경을 하지 않는다. 보고서의 확인 경로와 `runtime.blockers`에서 누락·OS 불일치를 확인한다.

검증 폴더는 매번 `attempts/<번호>/workspace`로 새로 만든다. case당 최대 3회이며 `VerificationBlocked`도 포함한다. 회당 기본 600초, `--timeout`은 1~600초다. 같은 변경을 반복하지 않고, 차단된 후보는 조건 해결 후 남은 시도에서 재검증한다. 조건이 같으면 반복하지 않으며 새 case로 한도를 우회하지 않는다. 무시·치환·문법 완화 옵션은 자동 후보와 PA 검증에서 제외한다.

| 시도별 파일 | 용도 |
| --- | --- |
| `proposed.conf` | 로컬 검증 경로로 바꾸기 전의 논리적 변경안. |
| `workspace/…conf` | PA 검증용 설정. 필요한 참조 경로를 해당 검증 폴더로 바꾼 파일. 서버에 그대로 전송하지 않음. |
| `proposal.diff` | 원본과 실제 검증용 설정의 차이. |
| `workspace/verification.json` | 대상 컴파일러·전체 TU, 설정 경로, 파일 해시, 경로 점검 결과. |

TU는 한 번에 분석하는 소스 단위다. 같은 컴파일러의 가용 TU 전체를 선택하며 제외·비활성 단위가 섞이면 자동으로 범위를 바꾸지 않고 중단한다. 다른 자식 case에도 같은 컴파일러가 있으면 일부만 성공으로 반영하지 않도록 중단한다. 현재 버전은 여러 case의 PA 결과를 합쳐 검증하지 않는다.

복사한 DB의 정확한 설정 키는 `Compiler.confFile`이다. 해당 컴파일러 행만 변경하고 다른 컴파일러 행은 유지한다. `PREPROCESS_PATH`, `ImportInfo`, `logPath` 등은 확인된 원래 경로 접두어를 이용해 복사본 안으로 바꾼다. 제품 DB의 뷰·트리거·가상 테이블 존재 자체는 차단 사유가 아니다. 복사본 수정 중에는 트리거를 잠시 내렸다가 원문 그대로 복원하고 가상 테이블·shadow table은 수정하지 않는다. 알 수 없는 절대 경로·명령 안의 경로처럼 안전하게 옮길 수 없는 실제 값만 `path_audit.safe=false`로 막는다. 이것은 경로 점검이며 실제 PA 호환성 검증을 대신하지 않는다.

원본 소스가 없는 오류 자료는 DB의 소스 경로만 검증 폴더 안으로 바꿀 수 있다. 가짜 소스 파일은 만들지 않는다. 실제 복사된 소스 또는 해당 TU의 전처리 파일이 있어야 PA 실행으로 넘어갈 수 있다.

## 4. 결과 읽기

```powershell
python "$skill\scripts\cover_recovery.py" inspect `
  --case 'D:\cases\runs\case-001\case.json'
```

새 실행에서 대상 TU가 실제 처리됐고, 변경 설정이 적용됐고, 오류가 해소됐는지 확인해야 한다. 종료 코드 0이나 예전 `.elog`·산출물만으로 성공 처리하지 않는다.

PA 검증 성공 상태는 `Recovered`다. 로컬 검증 명령은 서버·Agent를 바꾸지 않는다. 복구 작업은 [서버 반영과 되돌리기](server-publication.md)의 읽기 전용 계획과 실제 반영까지 추가 승인 없이 이어간다. 진단·설명·로컬 검증만 요청했으면 종료한다. 이 흐름은 원래 빌드나 `.cvd` 수집 성공을 뜻하지 않는다.
