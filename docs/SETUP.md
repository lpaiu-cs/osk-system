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
    mcp_server.py      외부 표면(MCP, stdio) — 도구 8종
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

## MCP 서버

에이전트가 이 체계를 다루는 **유일한 외부 표면**이다. 도구는 여덟이다 —
`overview` `search` `read_node` `run_validators` `create_node` `update_node`
`move_node` `record_candidate`.

서명과 pin은 **표면에 영구히 노출하지 않는다**(Mechanism §6-2). 권위의 발의는
사용자 전속이므로 아래 CLI에만 있다.

Claude Code에 user scope로 등록:

```bash
claude mcp add --scope user osk-system -- <REPO>/.venv/bin/python <REPO>/_governance/_engine/mcp_server.py
```

설정 파일을 직접 쓰는 클라이언트(Antigravity의 `~/.gemini/config/mcp_config.json`,
Codex 등)는 `.mcp.json.example`을 그대로 베끼고 `<REPO>`만 바꾼다. 전송은 stdio다.

대상 인스턴스는 `OSK_VAULT_ROOT`로 바꿀 수 있다. 지정하지 않으면
`_governance/_engine/`에서 두 단계 위(= `_governance`의 부모)를 vault 루트로 본다.

## CLI

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli --help
```

| 명령 | 하는 일 |
|---|---|
| `validate` | 검증기 수트 전체 (17세그먼트) |
| `status` | 체계 현황 |
| `search` / `view` | 작업 검색 / 열람 검색(미서명 후보 표시) |
| `check` | 권한 사전 검사 |
| `sign` / `unsign` | **사용자 전속** — 서명·해제 |

`sign`/`unsign`은 MCP로 노출되지 않는다. 헌법 10조 3항이 서명의 발의를 사용자에게
전속시키고, 표면은 그 경계를 물리적으로 지킨다.

`check`는 적용 봉투를 기계로 평가할 수 있기 전까지 **언제나 보류를 낸다**. 강제할
수 없는 것을 강제한 척하지 않는다.

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
해시 목록인 **비준증빙**(`release.json`)을 만들어 커밋·태그한다. 버전은
불변이다 — 같은 태그의 재선언은 거부된다. 태그 push는 git으로 직접 한다.

**인스턴스에서 — 갱신**:

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.update            # 보고
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.update --apply    # 적용
```

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
