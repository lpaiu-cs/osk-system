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
| `mcp_server.py` | MCP 노출 — 조회·검증·노드 쓰기·충돌 후보 기록(보호영역 권위·pin 미노출) |

## 사용

```bash
PYTHONPATH=_engine .venv/bin/python -m osk.cli validate   # 검증기 전체
PYTHONPATH=_engine .venv/bin/python -m osk.cli status
PYTHONPATH=_engine .venv/bin/python -m osk.cli search "질의"
PYTHONPATH=_engine .venv/bin/python -m osk.cli protect "= Person/Delegation" --reason "..."  # 사용자 전속
PYTHONPATH=_engine .venv/bin/python -m osk.cli approve "= Person/Delegation" --reason "..."  # 사용자 전속
# 동기화 데몬: nohup .venv/bin/python _engine/sync_daemon.py &
```

## 미구현 (후속 개정 대상)

- 근거 상태 변경 **주기 스캔**과 `_ledger/rechecks.jsonl` 완료 기록·재검토 브리핑
  (대장 형식·판정은 있으나, 상태 변경을 훑어 재검토 후보를 올리는 스캐너는 미구현)
- 정합성 주기 스캔·충돌 후보 감지(사건부 자동 채널) — 근거 키 계산기만 예약
- 브리핑 4채널 생성기 / 중심성 기반 랭킹 통합 / 임베딩 검색
- `_raw` 세션 포착 파이프라인(라운드 제목·접두부 보존 포함) / 정돈 실행기
  (위임 성립됨)
- 자동 집행 활성화 제도(시행령 §11 3항) — 활성화된 자동 집행 현재 0건
