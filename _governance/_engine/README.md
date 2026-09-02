# _engine — osk-system 엔진 (v2 체제 네이티브)

구체제 엔진은 삭제됨(git 이력이 보존). 동기화는 `sync_daemon.py`(얇은 재작성,
`vault_sync.py` 재사용)가 담당하며 검색·색인은 서빙하지 않는다.

## 조문 → 모듈 매핑

| 모듈 | 구현 조문 |
|---|---|
| `osk/core.py` | Mechanism §1~§3 공통 — 루트·시각·id·rid·대장 append 규율 |
| `osk/contract.py` | 시행령 §1·Mechanism §2 — 노드 계약 파싱·검증(2술어·순서·형식·정합) |
| `osk/graph.py` | Mechanism §1·헌법 8조·4조 5항·11조 2항 — 배치·참조 위상(id 해석)·중심성 |
| `osk/approvals.py` | 헌법 10조·시행령 §6·Mechanism §3 — 보호영역·승인본·승인 기록부 |
| `osk/signatures.py` | 헌법 14조 6항 — 구체제 서명 기록부(사료 판독) + id·사건 헤더 헬퍼 |
| `osk/authority.py` | 헌법 7조·시행령 §5·Mechanism §7 — 위임 전수·승인본 반영·3값·fail-closed |
| `osk/secrets.py` | 시행령 §2 3항·Mechanism §9 — 비밀값 필터 + fixture |
| `osk/search.py` | 헌법 11조 3~4항·시행령 §7 4항 — 작업/열람 검색·summary 확장 |
| `osk/validate.py` | 시행령 §11 — 검증기 수트(보고 전용)·보호영역 생애 fixture |
| `osk/cli.py` | 사용자 명령 — protect·unprotect·approve·revert는 대화형 확인 강제(사용자 전속) |
| `osk/write.py` | Mechanism §6-2 3항 — 노드 쓰기의 단일 통로(계약·위상·세션 라우팅) |
| `osk/raw.py` | 헌법 4조 3~4항·Mechanism §9 4~8항 — `_raw/` append와 좌표 회상 |
| `osk/scope_memory.py` | Mechanism §9-2 — scope 기억(상한·앵커 일괄·거부 계약) |
| `osk/epoch.py` | 판본 관문 — 이 프로세스가 적재한 엔진과 디스크의 대조 |
| `osk/update.py`·`osk/release.py` | Mechanism §1-2 — 정본 릴리스와 인스턴스 갱신(크래시-안전 트랜잭션) |
| `osk/publish.py` | Mechanism §1 1항 — 공개 미러 발행(allowlist) |
| `mcp_server.py` | MCP 노출 — 조회·검증·노드 쓰기·충돌 후보 기록(보호영역 권위·pin 미노출) |

## 사용

엔진의 실경로는 `<vault>/_governance/_engine`이다.

```bash
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli validate   # 검증기 전체
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli status
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli search "질의"
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli protect "= Person/Delegation" --reason "..."  # 사용자 전속
PYTHONPATH=_governance/_engine .venv/bin/python -m osk.cli approve "= Person/Delegation" --reason "..."  # 사용자 전속
# 동기화 데몬: SYNC_ENABLED=1 nohup .venv/bin/python _governance/_engine/sync_daemon.py &
```

## 미구현 (후속 개정 대상)

- 근거 상태 변경 **주기 스캔**과 `_ledger/rechecks.jsonl` 완료 기록·재검토 브리핑
  (시행령 §7 2항·Mechanism §4-1이 정한 완료 기록·`node_state` 결속·판정이 모두
  미구현이다 — 검증기가 그 파일의 JSON 무결성만 본다)
- 정합성 주기 스캔·충돌 후보 감지(사건부 자동 채널) — 근거 키 계산기만 예약
- 브리핑 4채널 생성기 / 중심성 기반 랭킹 통합 / 임베딩 검색
- 정돈 실행기(위임은 성립됨) — 경유 노드의 주기 정돈
- 활성화된 자동 집행 현재 0건 — 제도(`osk validators`)와 첫 규칙
  (`cluster-overview`)은 구현돼 있고 검사도 돌지만 보고 전용이다

## 알려진 한계

**보호는 선의의 실수를 되돌리는 장치다**(시행령 §6 7항). 이 엔진은 vault에 임의로
쓸 수 있는 상대를 위협 모델에 두지 않는다 — 그런 상대는 작업본·승인본 객체·승인
기록부를 직접 고칠 수 있으므로 보호영역은 애초에 그에 대한 권한 경계가 아니다.
그래서 이 층에는 권한 경계를 두지 않는다. 신뢰 밖 입력 방어는 동기화 충돌·디스크
손상·부분 기록처럼 **사고로 생기는 것**까지다. (갱신·대장 경로의 봉쇄와 symlink
재지정 거부는 별개다 — 그쪽은 Mechanism §1-2 5항이 명하고 `core.resolve_in_root`·
`update`가 구현한다. 다기기 병합으로 임의의 경로가 대장에 실려 오기 때문이다.)

**영역 경로가 디렉터리가 아닌 것으로 바뀌면 수동 정리가 먼저다.** 영역이 통째로
사라진 사고는 반려가 자동 복구한다(승인본에서 디렉터리와 파일을 되살린다). 그러나
같은 경로에 일반 파일·심볼릭 링크가 놓이면 상태는 `pending`으로 정확히 드러나되
반려는 거부한다 — 그 객체는 어떤 승인본에도 없던 사용자의 물건이고, 엔진이 승인본
밖 물건을 대신 지우는 것은 반려의 범위가 아니다(영역 **안**에서만 하는 일이다).
삭제 사고와 달리 여기서는 사용자가 치울 것이 눈앞에 있다 — `rm` 뒤에 반려하면
복원된다. 영역 경로가 심볼릭 링크가 되면 대장의 region key와 실제 경로가 갈리므로
판정도 그 링크를 먼저 걷어낸 뒤에 한다.

**대소문자만 다른 경로가 한 영역에 있으면 그 영역이 고착될 수 있다.** 대소문자를
구분하는 기기(Linux 등)에서 `Note.md`와 `note.md`를 같은 보호영역에 두고 승인하면,
구분하지 않는 기기(macOS·Windows)에서는 파일이 하나만 실재하므로 작업본 tree가
승인본과 영원히 같아지지 않는다 — 영역은 계속 `pending`, `revert`는 매번 실패,
`unprotect`는 pending이라 거부된다. `integrity()`는 이 상태를 평범한 pending과
구별하지 못한다(탐지기를 두면 오탐이 더 크다). 회복은 Mechanism §3의 수동 복구다.
예방은 한 영역 안에 대소문자만 다른 이름을 두지 않는 것.
