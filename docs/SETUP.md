# osk-system 설치·운용

이 인스턴스의 실행 방법. 규범이 아니라 **운용 문서**이므로 서명 대상이 아니고,
구현이 바뀌면 여기부터 고친다.

체계 자체의 규범은 `_governance/`(헌법·시행령·Mechanism·Workbench 계약)에
있고, 공개 미러 <https://github.com/lpaiu-cs/osk-system> 로 발행된다.

## 구성

```
_governance/
  Constitution.md 등   통치 문서 4종 + records/ (사료) — Space 밖 통치 구획
  _engine/
    osk/               엔진 — 계약·서명·인과 DAG·검색·검증기·발행
    mcp_server.py      외부 표면(MCP, stdio) — 도구 8종
    sync_daemon.py     동기화 데몬(git만; 검색·색인은 서빙하지 않는다)
    vault_sync.py      순수 git 헬퍼
    tests/             회귀 수트
    scripts/           발행 매니페스트, launchd/systemd 예시
= Scope/ = Domain/ = Person/   지식 공간
= Scope/Workbench/_ledger/     대장 — 서명·pin·세션 라우팅 (append-only)
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

## 공개 미러 발행

공개 미러는 사설 트리에서 **빌드**한다. v2.1부터 통치 구획 경로가 사설·공개
동일해(`_governance/`) 사상은 항등이지만, 무엇이 나가는지의 정본은 여전히
코드가 아니라 `_governance/_engine/scripts/publish-manifest.txt`다.

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.publish --public <PUBLIC_REPO>
```

기본은 **보고**다. `--apply`가 있어야 공개 트리에 쓰고, `--push`가 있어야 올린다.
보고에는 add·change·remove와 함께 `stray`(매니페스트가 통제하지 않는데 디스크에
있는 파일)가 나온다 — 발행은 stray를 커밋하지도 지우지도 않는다.

네 가드가 전부 fail-closed다. 하나라도 걸리면 아무것도 쓰지 않는다.

- 지식 노드 유출 금지 — `_governance/` 밖의 노드형 파일은 나가지 않는다
- 비밀값 스캔 — 값 자체는 보고에 싣지 않는다
- **미서명 통치 문서 발행 금지** — 사용자가 확인하지 않은 규범은 세상에 내놓지 않는다
- 검증기 PASS — 깨진 vault에서 발행하지 않는다

## 배경 기록

구 데몬(검색 서빙 + 동기화 혼성)과 DuckDB 색인은 v2에서 폐기됐다. 그 판단과 경위는
결정 노드에 있다 — `= Person/Decisions/2026-07-02-lavalink-swap-lock-and-daemon-demotion.md`.
