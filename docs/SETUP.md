# osk-system 설치·운용

인스턴스의 실행 방법. 규범이 아니라 **운용 문서**다.

체계 자체의 규범은 `_governance/`(헌법·시행령·Mechanism·Workbench 계약)에
있다. 그 **정본은 정본 저장소** <https://github.com/lpaiu-cs/osk-system> 이고,
각 인스턴스는 릴리스를 갱신으로 받는다(아래 '정본 릴리스와 갱신').

## 구성

```
_governance/
  Constitution.md 등   통치 문서 4종 + records/ (사료) — Space 밖 통치 구획의 특수 노드
  _engine/
    osk/               엔진 — 계약·서명·인과 DAG·검색·검증기·릴리스·갱신
    mcp_server.py      외부 표면(MCP, stdio) — 도구 10종
    sync_daemon.py     동기화 데몬(git만; 검색·색인은 서빙하지 않는다)
    vault_sync.py      순수 git 헬퍼
    tests/             회귀 수트
    scripts/           발행 매니페스트, launchd/systemd 예시
= Scope/ = Domain/ = Person/   지식 공간
= Scope/Workbench/_ledger/     대장 — 서명·pin·세션 라우팅·갱신 저널 (append-only)
```

## 준비

Python 3.12. 실의존성은 네 가지뿐이다 — `mcp`, `pydantic`, `PyYAML`, `rank-bm25`.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r _governance/_engine/requirements.txt
```

`mcp`는 major가 묶여 있다(`>=1.28,<2`). 외부 표면이 1.x의 `FastMCP`로 쓰였는데
2.0이 그 모듈을 없앴다 — **`requirements.txt`를 갱신으로 받았으면 pip을 다시
돌려야** 이미 만든 venv에 반영된다.

## Windows

엔진은 Windows에서도 돈다(잠금은 `msvcrt`, tz는 `tzdata` 패키지로 보충한다).
이 문서의 명령은 POSIX 표기이니 아래 셋만 바꿔 읽는다.

| | POSIX | Windows (PowerShell) |
|---|---|---|
| 인터프리터 | `python3.12` | `py -3.12` |
| venv 실행 파일 | `.venv/bin/python` | `.venv\Scripts\python.exe` |
| 환경변수 + 명령 | `VAR=값 명령` | `$env:VAR="값"; 명령` |

준비와 CLI는 이렇게 된다:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r _governance\_engine\requirements.txt
$env:PYTHONPATH="_governance\_engine"; .venv\Scripts\python.exe -m osk.cli validate
```

환경변수는 그 세션에만 남는다. 여러 명령을 이어 쓸 것이면 `$env:PYTHONPATH`를
한 번만 두고 이후 명령에서는 생략한다. `cmd.exe`라면 `set VAR=값`을 별도 줄에
쓴다(`set` 뒤 값에 따옴표를 붙이면 따옴표까지 값이 된다).

데몬의 상시 실행은 `scripts/`의 launchd·systemd 예시에 해당하는 것이 없다 —
**작업 스케줄러**에 `.venv\Scripts\python.exe _governance\_engine\sync_daemon.py`를
등록하고 환경변수 `SYNC_ENABLED=1`을 준다.

## MCP 서버

에이전트가 이 체계를 다루는 **유일한 외부 표면**이다. 도구는 열이다 —
`overview` `search` `read_node` `run_validators` `create_node` `update_node`
`move_node` `record_candidate` `append_raw` `read_raw`.

`_raw/` 세션 기록은 작업 검색에서 빠지므로(헌법 11조 3항) `read_raw`는 질의가
아니라 **좌표**를 받는다. 노드의 `derived-from`에 든 `[[경로#N]]`을 그대로 넣으면
그 라운드가 열린다 — 근거에서 증거로 가는 데 번역이 끼지 않는다. 좌표를 모르면
`space`로 기록 목록부터, 경로만으로 라운드 목차부터 본다.

서명과 pin은 **표면에 영구히 노출하지 않는다**(Mechanism §6-2). 권위의 발의는
사용자 전속이므로 아래 CLI에만 있다.

Claude Code에 user scope로 등록:

```bash
claude mcp add --scope user osk-system -- <REPO>/.venv/bin/python <REPO>/_governance/_engine/mcp_server.py
```

설정 파일을 직접 쓰는 클라이언트(Antigravity의 `~/.gemini/config/mcp_config.json`,
Codex 등)는 `.mcp.json.example`을 그대로 베끼고 `<REPO>`만 바꾼다. 전송은 stdio다.

Windows에서는 두 경로의 실행 파일 부분이 `.venv\Scripts\python.exe`가 되고,
JSON 안의 역슬래시는 `\\`로 이스케이프한다 —
`"command": "C:\\vault\\.venv\\Scripts\\python.exe"`.

대상 인스턴스는 `OSK_VAULT_ROOT`로 바꿀 수 있다. 지정하지 않으면
`_governance/_engine/`에서 두 단계 위(= `_governance`의 부모)를 vault 루트로 본다.

## CLI

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli --help
```

| 명령 | 하는 일 |
|---|---|
| `validate` | 검증기 수트 전체 |
| `status` | 체계 현황 |
| `search` / `view` | 작업 검색 / 열람 검색 |
| `check` | 권한 사전 검사 |
| `raw append` / `raw status` | `_raw/` 세션 기록 — 훅 경로(아래) |
| `protect` / `unprotect` | **사용자 전속** — 보호영역 지정·해제 |
| `approve` / `revert` | **사용자 전속** — 변경집합 승인·반려 |
| `update` / `release` | 갱신 / 릴리스 선언 — 인자를 그대로 위임한다 |

보호영역 권위(`protect`·`unprotect`·`approve`·`revert`)는 MCP로 노출되지 않는다.
헌법 10조 1~2항이 지정·해제·승인·반려를 사용자에게 전속시키고, 표면은 그 경계를
물리적으로 지킨다(Mechanism §6-2 2항). 이 명령들은 대화형 단말을 요구하며,
표준입력이 단말이 아니면 묻지 않고 중단한다 — 파이프로 무인 승인이 성립하지
않게 한다.

`check`는 적용 봉투를 기계로 평가할 수 있기 전까지 **언제나 보류를 낸다**. 강제할
수 없는 것을 강제한 척하지 않는다.

### 세션 기록 훅 (`raw append`)

표면의 `append_raw`는 에이전트가 **서술한** 라운드를 받는다. 헌법 4조 3항이 명하는
것은 전량 포착이므로, 전사를 그대로 나르는 기계 경로를 따로 둔다 — 통로와 계약은
같고 입력만 하네스에서 온다. 이 명령은 사용자 전속이 아니므로 대화형 확인을 걸지
않는다(걸면 훅에서 쓸 수 없다).

대화 바이트는 **stdin의 JSON 봉투**로 받는다. argv는 임의 바이트를 안전히 나르지
못한다 — 따옴표·길이 상한·콘솔 인코딩이 전부 전사를 훼손한다.

```bash
printf '%s' '{"rounds":[{"user":"…","agent":"…"}]}' \
  | .venv/bin/python -m osk.cli raw append \
      --session <세션 키> --record <대화 이름> --space "= Scope/<이름>"
```

- 봉투는 세 모양을 받는다 — `{"rounds":[…]}` · 라운드 하나(`{"user":…,"agent":…}`) ·
  배열. `session`·`record`·`space`는 봉투에 넣어도 되고, **플래그가 봉투를 이긴다**.
- 배치는 **한 번의 쓰기**다. 중간 라운드가 거부되면 아무것도 쓰지 않는다 — 라운드마다
  따로 쓰면 거부 지점에서 "있었던 대화의 일부"가 남는다.
- 라운드 번호는 엔진이 매긴다. 응답의 `round_refs`가 그대로 `derived-from`의 근거
  표기이고, 거부 시 종료코드는 0이 아니며 `violations`가 이유를 싣는다.
- 출력은 콘솔 코드페이지와 무관하게 **UTF-8 바이트**다.

**중복이 유실보다 위험하다.** 훅은 같은 대화에 여러 번 깨어나는데, 그때마다 처음부터
이어 붙이면 같은 라운드가 다른 번호로 두 번 앉는다. `_raw/`는 append-only라 사후에
되돌릴 수 없다. 엔진은 **직전 꼬리와 내용이 같은 배치를 거부**하지만(Mechanism §9
7항) 그것은 마지막 방어선일 뿐이다 — 중간에 한 라운드만 새것이 섞여 오면 앞의
중복이 함께 들어간다. 어댑터는 붙이기 전에 센다.

```bash
.venv/bin/python -m osk.cli raw status --session <키> --record <이름>
# → {"rounds": 12, "next_index": 13, "damaged": false, …}
```

어댑터는 `rounds`를 읽고 **그 뒤부터만** 보낸다. `damaged`가 참이면 기록의 index 열이
손상된 것이며, 그 위에는 이어 쓰지 않는다(append도 같은 이유로 거부한다).

전사에서 라운드를 뽑는 어댑터 자체는 하네스마다 다르므로 이 저장소에 두지 않는다 —
프레임워크가 아는 것은 위의 봉투 계약까지다.

## 회귀 수트

```bash
.venv/bin/python _governance/_engine/tests/test_regression.py
```

수트는 자기 프로세스 안에서 임시 mini-vault를 `OSK_VAULT_ROOT`로 가리킨다 —
실 vault를 읽지도 쓰지도 않는다. 검토 세션의 적대 시나리오가 여기에 영속 고정돼
있으므로, 엔진을 고쳤으면 이걸 통과시킨 뒤에 커밋한다.

## 동기화 데몬

git 동기화만 한다. 검색·색인 서빙은 하지 않는다 — 그것은 엔진과 MCP 서버의 일이다.
**명시적 opt-in**이라 `SYNC_ENABLED`가 없으면 즉시 종료한다.

```bash
SYNC_ENABLED=1 .venv/bin/python _governance/_engine/sync_daemon.py --interval 900
```

잠금은 실제 git 디렉터리 안의 `osk-sync.lock`에 둔다(추적 트리로 폴백하지 않는다 —
데몬 자신의 `git add -A`가 잠금 파일을 커밋해 버리기 때문이다).

**동기화 대상은 `main` 고정이다**(`vault_sync.SYNC_BRANCH`). 데몬은 HEAD를 따라가지
않는다 — 어떤 세션이 다른 브랜치를 checkout해 둔 사이에 그 브랜치가 vault의 정본인
것처럼 커밋·push되면 정본이 조용히 갈라지기 때문이다. `pull`·`push`도 `origin main`을
명시하므로 upstream이 잘못 걸려 있어도 엉뚱한 곳으로 새지 않는다.

HEAD가 `main`이 아니면 매 주기 시작에 되돌린다. 되돌릴 수 없는 경우에는 **아무것도
하지 않고** 사유를 낸다.

| 상태 | 동작 |
|---|---|
| `main` | 그대로 동기화 |
| 다른 브랜치·detached, 추적 파일 수정 없음 | `main`으로 전환 후 동기화 (미추적 새 노드는 함께 넘어간다) |
| 다른 브랜치·detached, 추적 파일 수정 있음 | **거부** — 진행 중 작업일 수 있어 옮기지도 감추지도 않는다 |
| 로컬에 `main` 없음 | 거부 |

launchd/systemd 예시는 `_governance/_engine/scripts/`에 있다.

## 정본 릴리스와 갱신

프레임워크(통치 문서·엔진·운용 문서)의 **정본은 정본 저장소**
<https://github.com/lpaiu-cs/osk-system> 다(Mechanism §1-2). 엔진·통치 문서의
저작은 정본에서 하고, 모든 인스턴스는 — 기초자의 것을 포함해 — 릴리스를
**갱신**으로 받아들인다. 데이터 동기화 데몬(위)과는 다른 축이다 — 데몬은
인스턴스 자신의 원격만 다루고 정본에 닿지 않는다.

**정본에서 — 릴리스 선언** (사용자의 비준 행위, 대화형 단말 전속):

```bash
PYTHONPATH=_governance/_engine python3 -m osk.release --version vX.Y.Z --apply
```

깨끗한 작업 트리·검증기 PASS·비밀값 스캔을 전제로, 릴리스 전 파일의 내용
해시 목록인 **비준증빙**(`release.json`)을 만들어 커밋·태그한다. 전제 검사와
증빙은 모두 **그 커밋의 스냅샷**에서 수행되고, 커밋은 작업 트리를 거치지 않고
object로 만들어 **원자적 교체**로 설치된다 — 그 사이 다른 커밋이 들어오면
실패하고 아무것도 남지 않는다. 버전은 불변이다: 같은 태그의 재선언은 거부되고,
인스턴스도 같은 버전에 다른 증빙이 오면 거부한다(태그 force-move 방어).

선언은 **작업 트리를 건드리지 않는다**(외부 수정을 덮지 않기 위해서다). 그래서
선언 직후 작업 트리에는 새 `release.json`이 없다 — 보고가 안내하는 대로 맞춘다:

```bash
git checkout vX.Y.Z -- release.json
```

태그 push는 git으로 직접 한다.

**인스턴스에서 — 갱신**:

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.update            # 보고
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.update --apply    # 적용
```

`osk.cli`를 거쳐도 같다 — `... -m osk.cli update --apply`. 위임 명령은 인자를
해석하지 않고 그대로 넘긴다(`--help`도 위임 대상의 사용법이 나온다).

- 출처는 둘이다: `git`(기본 — 정본을 태그로 얕게 받는다. `--to vX.Y.Z`로
  버전 고정) · `bundle`(`--from <경로>` — 디렉터리·tar 오프라인 반입).
  로컬 설정은 `.osk/config.json`의 `{"upstream": {"source", "url", "pin"}}`.
- 릴리스는 **비준증빙과 전수 대조**한 뒤에만 적용된다 — 증빙 파일의 누락·해시
  불일치·증빙 부재는 중단이고, 증빙 밖의 미추적 파일은 적용하지 않는다(막지도
  않는다). 적용은 오직 증빙이 모는 파일만 하고 하나하나를 해시로 검증한다.
- 적용 범위는 릴리스 안의 발행 매니페스트가 정하고(별도 갱신 매니페스트
  없음), **인스턴스 소유 바닥**(`= ` Space·`_ledger/`·`_raw/`·`_sources/`·
  `.osk/`)에는 무엇이 와도 쓰지 않는다.
- 로컬 수정이 있는 문서는 덮지 않고 `<이름>.upstream-<버전>` 사본을 옆에
  둔다(병합은 수동). **엔진 파일의 로컬 수정은 갱신 전체를 중단한다** —
  엔진을 고치는 자리는 정본이다. 기존 인스턴스의 최초 편입은 `--adopt`.
- 갱신이 통치 문서를 덮으면 그 인스턴스의 서명이 자동으로 풀린다 — diff를
  확인하고 `osk sign`으로 재서명하는 것이 **수용의 기록**이다(효력 요건은
  아니다; 미비준은 status에 상시 표시된다).
- 갱신 이력은 `_ledger/update.jsonl`(운영 저널)에 남고, 엔진이 갱신됐으면
  실행 중인 MCP 서버·데몬을 재시작한다.
- **갱신은 인스턴스당 한 번이다.** 한 인스턴스를 여러 기기에서 쓰더라도
  `osk.update`는 정본→인스턴스 경계를 넘을 때만 돌린다 — 프레임워크 파일도,
  현재 판본을 정하는 갱신 저널도 인스턴스 자신의 저장소가 추적하므로, 나머지
  기기는 **평소의 동기화(pull)만으로 같은 판본이 된다**. 판본 판정이 로컬
  상태 파일이 아니라 union 병합되는 저널의 인과 극대이기 때문이다. 그 기기들
  에서 할 일은 둘뿐이다 — 돌고 있는 서버·데몬 재시작(구 코드가 메모리에 남아
  있다), 그리고 `requirements.txt`가 바뀌었으면 pip 재실행.
- 적용은 **크래시-안전 트랜잭션**이다. 갱신이 도중에 죽으면 다음 `--apply`가
  자동 복구한다. 엔진이 반쯤 교체돼 `osk.update` 자체가 안 돌면 엔진과 독립된
  복구 부트스트랩을 쓴다(표준 라이브러리만 사용):

```bash
python3 _governance/_engine/scripts/recover.py --apply
```

  기본은 보고다. 커밋된 트랜잭션은 파일을 두고 표식만 정리하고(roll-forward),
  미커밋이면 pre-image로 되돌린다(rollback). 백업이 없거나 손상되면 아무것도
  지우지 않고 중단한다. 복구가 끝나기 전에는 동기화 데몬도 tick을 거부한다.

## 적대적 하네스

회귀 수트가 **알려진** 결함을 고정한다면, 이 하네스는 아직 모르는 결함을
찾는다 — 갱신 프로세스를 실제로 SIGKILL로 죽이고, 악의 릴리스와 동시 데몬을
조합해 무작위로 돌린 뒤 불변식을 검사한다.

```bash
.venv/bin/python _governance/_engine/tests/test_adversarial.py --trials 12 --seed 7
```

보고 끝의 **커버리지** 줄이 중요하다 — `pending_txn`·`half_applied`가 0이면
위험 구간을 한 번도 때리지 못한 것이므로 "통과"에 의미가 없다(타이밍·규모를
조정해야 한다). 실측에서 `half_applied`를 만든 뒤 복구·재적용이 수렴함을
확인했다.

신규 설치는 버전 0에서의 첫 갱신이다 — 빈 디렉터리에서 `--apply --adopt`로
시작하거나, 정본을 clone한 뒤 자기 원격으로 갈아탄다.

## 배경 기록

구 데몬(검색 서빙 + 동기화 혼성)과 DuckDB 색인은 v2에서 폐기됐다. 그 판단과 경위는
결정 노드에 있다 — `= Person/Decisions/2026-07-02-lavalink-swap-lock-and-daemon-demotion.md`.
