# 로컬 오류 자료로 시작하기

로컬 ZIP·폴더는 보존 복사본에서 진단한다. tmpa 실행은 서버 API에서만 수행한다. 로컬 자료만 제공한 case에는 서버 Issue 출처가 없으므로 검증 전에 `start`/`collect`로 서버에서 새 case를 수집해야 한다.

Python 3.11 이상과 표준 라이브러리만 사용한다. 아래 경로와 소스 이름은 사용 환경에 맞게 바꾼다. `--source`는 DB의 원래 `SourceFile.path` 값이다. 상대 소스명을 사용하면 `--project-root`에 실제 빌드 작업 폴더를 지정한다.

ZIP·폴더가 없으면 [오류 자료 탐색](artifact-discovery.md)을 따른다. `start`의 `--artifacts`를 생략하면 소스 전체 경로와 HardwareID로 서버에서 최신 오류를 찾는다. 자동 발견 자료는 서버 확인을 대신하지 않으며 응답에 없는 Issue ID를 추측하지 않는다.

## 1. 오류 자료 확인

```powershell
$skill = '<cover-toolchain-recovery 스킬 폴더의 절대 경로>'
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

## 3. 서버에서 후보 검증

[서버 TMPA 실행 계약](server-execution.md)을 따른다. 서버 수집 case와 환경변수의 Access key가 필요하다.

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" verify --case <case.json> --candidate <후보.json>
```

각 시도는 `attempts/<번호>/`에 원본과 분리해 준비한다. `proposed.conf`는 서버에 보낼 설정이며 `workspace/`는 변경 비교·입력 보존용 복사본이다. DB·INI·소스·로그는 바이트 그대로 복사하고 후보 `.conf`만 수정한다. 경로 치환과 tmpa 실행은 서버가 맡는다. `proposal.diff`는 설정 변경, `workspace/verification.json`은 입력·컴파일러·TU(분석 소스 단위) 기록이다. 서버가 실행할 이슈의 실제 범위는 서버 API가 결정한다.

새 실행은 최대 3회다. RUNNING·조회 오류 뒤에는 동일 명령으로 기존 runId를 조회한다. `--timeout`은 1~60초 조회 대기이며 30분 서버 실행 제한과 다르다. 상세 판정·재개·오류 아카이브는 [PA 실행 계약](server-execution.md)에 있다.

## 4. 결과 읽기

```text
python "${SKILL_ROOT}/scripts/cover_recovery.py" inspect --case <case.json>
```

`Recovered`는 서버의 SUCCEEDED 응답을 확인했다는 뜻이다. 클라이언트에서 새 계측 파일·DB 전환을 관측한 결과가 아니다. 실패 시 `error-artifacts/`의 이번 로그로 진단을 갱신하고 원본 설정 기준의 새 후보를 작성한다.

복구 요청은 [서버 반영과 되돌리기](server-publication.md)까지 이어간다. 진단·검증만 요청했다면 툴체인과 Wrapper를 변경하지 않는다. 로컬 작업만 요청했으면 API 실행 전 진단에서 종료한다.
