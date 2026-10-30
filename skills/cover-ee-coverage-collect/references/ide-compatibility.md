# STM32CubeIDE 버전 호환성

## 배포 범위와 조사 범위를 구분한다

여러 IDE 버전을 하나의 JAR로 지원하는 것이 목표다. IDE 버전 문자열만으로 설치 가능 여부를 판단하지 않고, 실제 등록된 Eclipse·CDT 번들과 IDE가 사용하는 Java를 확인한다. 같은 IDE 버전도 플러그인 업데이트나 Java 변경에 따라 구성이 달라질 수 있다.

현재 고객용 배포본은 `assets/plugin/release.json`이 가리키는 0.0.1 JAR다. 이 파일과 manifest의 설치 제한은 이번 호환성 조사에서 변경하지 않았다. Java 11 호환 소스와 검증용 빌드를 준비했지만, 구버전 IDE에서 실행한 결과를 확보하기 전에는 고객용 JAR를 교체하지 않는다.

## 공식 자료에서 확인한 구성

2026-09-15에 조사했다. 아래 표는 제품 구성 조사이며 수집 기능의 지원 완료 목록이 아니다. 확인하지 못한 값은 주변 버전에서 추측해 채우지 않는다. 특히 `org.eclipse.cdt.core` 번들 버전은 CDT 전체 제품 버전과 다르다.

| STM32CubeIDE | Java | Eclipse | CDT | 확인 근거 |
|---|---|---|---|---|
| 1.6.0 | Java 11 도입 | 미확인 | 미확인 | [RN0114의 1.6.0 변경 이력](https://www.st.com/resource/en/release_note/rn0114-stm32cubeide-release-v1101-stmicroelectronics.pdf) |
| 1.7~1.13 | 설치본 확인 필요 | 설치본 확인 필요 | 설치본 확인 필요 | 정확한 버전 조합은 아직 확보하지 않음 |
| 1.14.0 | 미확인 | 2023-09 | 미확인 | [RN0114의 1.14.0 변경 이력](https://www.st.com/resource/en/release_note/rn0114-stm32cubeide-release-v1101-stmicroelectronics.pdf) |
| 1.15.x | 미확인 | 2023-12 | 11.4.0 | [ST 1.15.x 구성표](https://wiki.st.com/stm32mcu/wiki/STM32CubeIDE:STM32CubeIDE_errata_1.15.x) |
| 1.16.x | 미확인 | 2023-12 | 11.4.0 | [ST 1.16.x 구성표](https://wiki.st.com/stm32mcu/wiki/STM32CubeIDE:STM32CubeIDE_errata_1.16.x) |
| 1.17.x | 21.0.3_9 | 2023-12 | 11.4.0 | [ST 1.17.x 구성표](https://wiki.st.com/stm32mcu/wiki/STM32CubeIDE:STM32CubeIDE_errata_1.17.x) |
| 1.18.x | 21.0.3_9 | 2024-09 | 11.6.1 | [ST 1.18.x 구성표](https://wiki.st.com/stm32mcu/wiki/STM32CubeIDE:STM32CubeIDE_errata_1.18.x) |
| 1.19.0 | 21.0.3 | 4.33 / 2024-09 | 11.6.1 | 설치본과 [ST 구성표](https://wiki.st.com/stm32mcu/wiki/STM32CubeIDE:STM32CubeIDE_errata_1.19.x)를 대조함 |
| 2.0.x | 21.0.8_9 | 2024-09 | 11.6.1 | [ST 2.0.x 구성표](https://wiki.st.com/stm32mcu/wiki/STM32CubeIDE:STM32CubeIDE_errata_2.0.x) |
| 2.1.x | 설치본 확인 필요 | 설치본 확인 필요 | 설치본 확인 필요 | 정확한 버전 조합은 아직 확보하지 않음 |
| 2.2.0 | 21.0.10+7 | 2025-12 | 12.3.0 | [RN0114 Rev 39의 현재 구성](https://www.st.com/resource/en/release_note/rn0114-stm32cubeide-release-v1101-stmicroelectronics.pdf) |

ST 문서는 같은 URL의 내용이 새 릴리스로 갱신될 수 있다. 문서에 적힌 릴리스와 실제 설치본을 함께 확인한다. 현재 자료만으로 1.6.0부터 최신 버전까지 지원한다고 선언하지 않는다.

## 설치본 조사

다음 명령은 IDE를 실행하거나 보드에 접속하지 않는다. 여러 설치본은 `--ide-home`을 반복해서 지정한다.

```text
python "${SKILL_ROOT}/scripts/inspect_cdt_compatibility.py" --ide-home <IDE A> --ide-home <IDE B>
```

- `stm32cubeide.ini`에서 제품 build ID와 `-vm` 경로를 읽는다.
- 해당 Java의 `release` 파일에서 실제 Java 버전을 읽는다. ini의 `osgi.requiredJavaVersion=1.8`을 현재 Java 버전으로 해석하지 않는다.
- `bundles.info`의 등록 경로에서 번들 manifest를 읽는다. 폴더에 남아 있지만 등록되지 않은 오래된 JAR를 현재 구성으로 선택하지 않는다.
- JAR 형태와 디렉터리 형태의 번들을 모두 읽는다. 등록 정보와 manifest 버전이 다르면 `issues`에 기록한다.
- CDT·Eclipse feature 버전과 개별 번들 버전을 구분해 기록한다. feature 파일 목록은 등록 번들의 실제 로딩 상태를 증명하지 않는다.
- 설치·시작·세션 조회·메모리 수집 결과는 모두 `NOT_TESTED`로 출력한다. 파일을 조사한 것만으로 실행 검증을 통과시킬 수 없기 때문이다.

`install_cdt_plugin.py --action status`도 같은 구성 정보를 출력한다. `bundleRangeMatched=true`는 기존 배포본의 번들 버전 범위에 들어간다는 뜻이며, 보드 수집 성공을 의미하지 않는다. 설치는 기존 p2 검증과 JAR 해시 확인을 계속 사용한다.

## 공통 JAR를 위한 소스 변경

수집 순서와 프로토콜 1은 유지한다. Java 11에서 제공하지 않는 `record`, 패턴 변수 형태의 `instanceof`, `Stream.toList`, `HexFormat`은 이전 API로 같은 동작을 구현했다. Eclipse의 최신 `Status.error`와 클래스 기반 로그 접근도 이전부터 제공하던 생성자·Bundle 기반 접근으로 바꿨다. Gson은 구버전에서도 있는 파서 메서드를 사용한다. 최신 Gson에서 나오는 deprecated 경고는 이 호환 호출에 따른 것이다.

CDT 서비스 호출은 그대로 유지한다. 타깃 선택, 정지, `sdump_buf` 조회, 캐시 비우기, 메모리 읽기, 재개 감지, 시간 초과 후 재시도 금지 규칙은 바꾸지 않는다. Java 기준을 낮춘다는 이유로 안전 검사를 생략하거나 다른 GDB 연결을 만들지 않는다.

## 개발자 검증용 빌드

고객은 아래 빌드를 수행하지 않는다. 유지보수자가 검증할 설치본 또는 공식 API 번들 묶음을 준비한 뒤 사용한다. `<스킬>`은 `SKILL.md`가 있는 설치 스킬의 절대 경로다. 다음 명령은 작업 디렉터리에 관계없이 실행할 수 있다.

```text
python "${SKILL_ROOT}/plugin/build_plugin.py" --ide-home <검증 대상 IDE 또는 API 폴더> --jdk-home <JDK> --java-release 11 --output-directory <검증용 출력 폴더>
```

Java 11 검증 빌드는 별도 출력 폴더가 필수다. `assets/plugin`에 덮어쓰는 것도 거부한다. JAR의 버전 문자열이 현재 배포본과 같더라도 다른 바이너리이므로 고객 IDE의 설치기를 우회해 복사하지 않는다. 결과 JSON의 `validation=COMPILE_ONLY`, `compiledAgainst`, `javaRelease`, 소스·JAR·실제 manifest 해시를 함께 확인한다.

현재 manifest의 번들 제한은 검증 빌드에도 유지된다. 따라서 이 후보는 구버전 API 컴파일 확인용이며, 기존 설치기로 구버전 IDE에 바로 설치할 수 없다. 구버전 실물 설치본이 확보되면 유지보수자는 스킬의 독립 개발 사본에서 해당 설치본에 한정한 manifest와 새 후보 버전을 준비하고, 클라이언트·release·known-releases도 같은 후보로 맞춘 뒤 시험한다. 고객 설치 스킬의 제한을 먼저 넓혀 시험하지 않는다. manifest가 바뀌면 JAR 해시도 달라지므로 설치 시험부터 다시 확인한다. 실험용 배포본 생성과 버전별 실물 설치 검증은 아직 완료하지 않았다.

## 현재 검증 결과

| 대상 | 확인한 내용 | 아직 확인하지 못한 내용 |
|---|---|---|
| STM32CubeIDE 1.19.0 설치본 | Java 21.0.3, Eclipse 4.33, CDT 11.6.1, DSF 2.12.100, DSF GDB 7.1.300을 직접 조사함. 변경 소스를 Java 11 대상으로 컴파일함 | 변경된 JAR의 IDE 설치·시작·보드 수집 |
| Eclipse 4.17 + CDT 10.0.0 공식 API 번들 | Java 11 대상 컴파일 성공. Gson 2.8.2, core.runtime 3.19.0, DSF 2.9.0, DSF GDB 6.0.0 사용 | STM32CubeIDE 특정 버전의 전체 설치본·실행 환경 검증 |
| 두 API 묶음의 출력 JAR | SHA-256이 동일함. 같은 소스로 같은 바이너리를 만들 수 있음을 확인함 | 그 사이의 모든 버전에서 런타임 호환된다는 보장 |

구버전 API는 [Eclipse 4.17 공식 보관 저장소](https://archive.eclipse.org/eclipse/updates/4.17/R-4.17-202009021800/plugins/)와 [CDT 10.0.0 공식 보관 저장소](https://archive.eclipse.org/tools/cdt/releases/10.0/cdt-10.0.0/plugins/)에서 확보했다. 타사 바이너리를 스킬 배포물에 포함하지 않는다. JDK 17의 `--release 11` 컴파일이며 Java 11 VM에서 실행한 테스트는 아니다.

## 지원 범위를 넓히는 기준

1. 구버전과 신버전 설치본을 별도 검증 PC 또는 독립 설치 디렉터리에 준비한다. 고객의 사용 중인 IDE를 검증용으로 다운그레이드하지 않는다.
2. 구성 조사 결과와 후보 JAR 해시를 보관한다. JAR를 바꾸면 이전 후보의 성공 결과를 그대로 재사용하지 않는다.
3. 실험용 설치본에서 p2 설치, IDE 재시작, 현재 인스턴스의 heartbeat, Debug 세션 조회를 각각 확인한다.
4. 사용자가 테스트를 끝낸 계측 펌웨어에서 배열 크기와 덤프 크기 일치, CVD 검증, Agent 전달, 타깃 정지 유지까지 확인한다. 빌드·다운로드·테스트 자동 실행은 추가하지 않는다.
5. OS·IDE 구성·디버거별 결과를 보고 새 배포본의 허용 범위를 확정한다. 중간 버전을 검증하지 않았으면 연속 범위를 모두 지원한다고 기록하지 않는다.

설치·세션 조회·수집 명령과 증거 파일은 [CDT 연동 절차](stm32-cdt.md)의 최초 설치, 자동 수집, 파일과 통신 절을 따른다. 설치 성공은 `registrationVerified`, 서비스 시작은 현재 인스턴스의 `bridge.json`, 수집·검증·전달은 해당 수집 폴더의 `capture-stage.json`, `validation.json`, `delivery.json`으로 구분한다. 후보 버전과 JAR 해시, IDE 구성 조사 결과를 같은 검증 기록에 묶는다.

현재는 공통 소스가 두 API 묶음에서 같은 JAR로 컴파일되므로 JAR 분리가 필요하다는 근거가 없다. 실제 검증에서 공통 구현으로 해결할 수 없는 API 차이가 확인될 때만 배포본을 분리한다. 그때 설치기가 구성 조사 결과로 검증된 JAR를 선택하도록 추가하며, 고객에게 직접 빌드하거나 IDE를 올리도록 요구하지 않는다.
