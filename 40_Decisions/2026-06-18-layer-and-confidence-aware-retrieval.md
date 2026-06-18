---
type: decision
date: 2026-06-18
status: active
project: LLM Second Brain
confidence: medium
sources:
  - 90_Engine/indexer.py
  - 90_Engine/retriever.py
  - 90_Engine/mcp_server.py
related:
  - "[[LLM Second Brain]]"
  - "[[2026-06-18-second-brain-architecture]]"
  - "[[Implementation Questions]]"
---

# Layer- and confidence-aware retrieval

## Decision

검색 런타임(`90_Engine`)을 **계층(layer)·신뢰도(confidence)·상태(status) 인지** 방식으로
확장한다. 구체적으로:

1. **계층별 인덱싱 정책(per-folder index policy)** 도입:
   - `05_Inbox/` → 인덱싱 안 함(휘발성).
   - `06_Raw/` → **전문검색 전용**으로 인덱싱(node+embed, 검색 가능)하되 edge를
     파싱하지 않고 wikilink/edge 타깃도 아님(`graph_node=False`).
   - 그 외 해석 계층 → node+edge 풀 인덱싱(현행 유지).
2. **신뢰도/상태 인지 검색**: 검색 결과 랭킹에 `계층 × confidence × status` 가중치를
   곱한다. 낮은 신뢰도·폐기(superseded/rejected/stale) 항목은 **강등**하되 숨기지
   않고, 결과에 `layer/confidence/status`를 함께 **표기**한다.
3. **검토/메타 계층 스코프**: `60/70/80`은 기본 검색에서 제외(`include_reviews=True`로
   포함). `06_Raw`는 기본 포함하되 강등(`include_raw=False`로 제외 가능).
4. **검토 큐 위생 도구**: MCP `review_queue(status, layer)` 추가 — 60/70/80의 열린
   항목을 모아 상태별로 반환.

## Context

[[2026-06-18-second-brain-architecture]] 직후 [[Implementation Questions]]에 두 개의
미해결 질문이 남아 있었다: (a) 폴더별 인덱싱 정책, (b) 신뢰도 인지 검색. 초기 구현은
`05_Inbox/06_Raw`를 디렉터리 이름으로 단순 제외하고, 모든 해석 계층을 동일 가중치로
검색했다. 그 결과 ① 원본 증거를 직접 검색할 수 없고(요약만 대리), ② 낮은 신뢰도·검토
대기 항목이 검증된 지식과 동급으로 노출되어 "근거 없는 LLM 메모리 그래프"로 퇴화할
위험이 있었다.

## Alternatives Considered

1. **06_Raw 완전 제외 유지 (요약만 검색)** — 가장 단순하나 원본 증거 직접 검색 불가.
   기각(사용자 선택: 전문검색 전용 포함).
2. **낮은 신뢰도 항목 하드 제외** — 오염은 막으나 맥락 누락·정보 은닉. 기각.
3. **채택: 06_Raw 전문검색 전용 + 강등, 낮은 신뢰도는 강등+표기** — 검색 가능성과
   품질 게이트를 동시에 확보.
4. **DuckDB 스키마에 layer/confidence 컬럼 추가** — 가능하나 마이그레이션 비용·위험.
   retriever가 이미 node 본문을 읽으므로 **로드 시 경로·frontmatter에서 파생**하는 쪽이
   더 가볍다. 채택(인덱서 스키마 불변).

## Rationale

- 원본(`06_Raw`)을 검색은 가능하되 그래프 node/링크 타깃이 아니게 하여, raw 채팅
  로그의 `[[A]] pred [[B]]` 문장이 false edge가 되는 것을 차단하면서도 증거 직접
  검색을 가능케 한다.
- "LLM은 틀릴 수 있다"([[Hallucination as Default]]) 전제에 따라, 불확실성을 숨기지
  않고 강등+표기로 노출하는 것이 [[Review Policy]]·[[Second Brain Operating Model]]의
  철학과 일치한다.
- 계층/신뢰도 메타를 retriever 로드 시 파생하므로 인덱서 DB 스키마를 바꾸지 않아
  "엔진은 꼭 필요할 때만 변경" 원칙([../AGENTS.md](../AGENTS.md))을 지킨다.

## Consequences

- `indexer.py`: `LAYER_POLICY`/`policy_for()` 도입. `06_Raw` 인덱싱(전문검색),
  edge 파싱·링크 네임스페이스에서 제외. `05_Inbox` 제외 유지.
- `retriever.py`: `layer_from_path`/`parse_frontmatter_fields`/`compute_rank_weight`
  추가. seed 검색·그래프 확장에 가중치 적용, 스코프 필터, 결과에 layer/confidence/status
  표기. `retrieve(..., include_raw, include_reviews, confidence_weighting, include_layers,
  exclude_layers)` 파라미터 추가.
- `mcp_server.py`: `retrieve_knowledge`에 `include_raw/include_reviews/confidence_weighting`
  추가. `review_queue` 도구 신설.
- 문서 갱신: README, [[Second Brain Operating Model]], [[Ontology Specification]] §0,
  [../AGENTS.md](../AGENTS.md), `06_Raw/README`, `docs/MCP_TOOLS.md` — "raw 인덱싱 제외"
  → "raw 전문검색 전용(그래프 제외)"로 일관화.

## Risks

- **가중치 수치는 휴리스틱이다.** `LAYER_RANK_WEIGHT`/`CONFIDENCE_WEIGHT` 값은 실제
  사용 데이터로 튜닝 전까지 임시값. 검색 품질이 기대와 다르면 조정 필요.
- **raw 임베딩 비용·노이즈.** raw가 커지면 Ollama 임베딩 호출·BM25 코퍼스가 커진다.
  필요 시 raw를 BM25-only(임베딩 제외)로 더 낮출 수 있음(향후 정책 옵션).
- **로컬 검증 한계.** 작성 환경에 `duckdb`/`rank_bm25` 미설치로 전체 파이프라인 통합
  테스트는 미수행. 순수 로직(정책/가중치/파서/그래프 확장)은 stub로 단위 검증함.
  → 사용자 머신에서 `pip install -r requirements.txt && indexer --force --embed` 후
  실검증 필요.

## Review Triggers

- 검색 결과가 체감상 raw/검토 항목에 치우치거나, 반대로 유효한 원본이 과도하게
  강등될 때 → 가중치 재조정.
- raw 임베딩 비용이 부담될 때 → raw BM25-only 옵션 도입 검토.
- confidence/status 표기가 실제 의사결정에 안 쓰이면 → 출력 포맷 간소화.

위 트리거 발생 시 [[Review Policy]] §4의 `decision-needs-reconsideration`로 올리고,
정책이 바뀌면 [../AGENTS.md](../AGENTS.md) §4 supersede 절차를 따른다.

## Sources

- 구현: `90_Engine/indexer.py`, `90_Engine/retriever.py`, `90_Engine/mcp_server.py`
- 선행 결정: [[2026-06-18-second-brain-architecture]]
- 관련 정책: [[Review Policy]], [[Second Brain Operating Model]], [[Ontology Specification]]
