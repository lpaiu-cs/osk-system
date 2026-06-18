---
type: project
status: active
updated: 2026-06-18
area: second-brain
related:
  - "[[Second Brain Operating Model]]"
  - "[[2026-06-18-second-brain-architecture]]"
---

# LLM Second Brain

> [!NOTE] 대시보드
> 이 페이지는 **대시보드**입니다. 세부사항을 쏟아붓지 않고, 정책·결정·개념·검토
> 항목으로 링크합니다. 현재 상태를 한눈에 보는 지도로 유지하세요.

## Purpose

`llm-vault`를 출처 기반(source-grounded)·감사 가능(auditable) **LLM-native second
brain**으로 운영한다. 사람과 LLM이 함께 읽고 쓰며, LLM이 틀릴 수 있다는 전제 아래
원본·인용·불확실성·모순·검토를 1급 시민으로 다룬다.

## Current Architecture

- 계층 모델·데이터 흐름: [[Second Brain Operating Model]]
- 런타임/인덱싱/MCP: `90_Engine/` ([../SETUP.md](../SETUP.md), [../docs/MCP_TOOLS.md](../docs/MCP_TOOLS.md))
- 온톨로지(9-predicate, 그래프 적용 범위 §0): [[Ontology Specification]]
- 에이전트 행동 규칙: [../AGENTS.md](../AGENTS.md)

## Active Decisions

- [[2026-06-18-second-brain-architecture]] — `llm-vault`를 second brain으로 진화 (status: active)
- [[2026-06-18-layer-and-confidence-aware-retrieval]] — 계층/신뢰도 인지 검색 + 검토 큐 도구 (status: active)

## Current Problems

- 인입 마찰 vs. 규율의 균형 (모든 단계를 매번 밟지 않도록 최소 경로 필요) → [[Ingest Policy]] §3
- 개념 과적합 위험 (Anti-Bloat) → [[Ingest Policy]] §2
- 검토 큐(`80_Reviews/`)가 쌓이고 비워지지 않을 위험 → [[Review Policy]] (점검: `review_queue()`)
- 검색 가중치(`LAYER_RANK_WEIGHT`/`CONFIDENCE_WEIGHT`)가 휴리스틱 임시값 — 실데이터 튜닝 필요
- 로컬에 `duckdb`/`rank_bm25` 미설치로 검색 파이프라인 통합 검증 미수행 (순수 로직만 검증)

## Ingest Workflow

요약: `05_Inbox/` → `06_Raw/`(불변) → `50_Source_Summaries/` → 해석 계층 갱신 →
검토/모순/질문 라우팅 → 재인덱싱. 전체 절차: [[Ingest Policy]].

## Review Workflow

불확실/의심 → `80_Reviews/`, 충돌 → `70_Contradictions/`. 카테고리·상태 정의:
[[Review Policy]]. 현재 큐: [[Needs Human Review]] · [[Low Confidence Claims]] ·
[[Possible Hallucinations]].

## Runtime Layer

`90_Engine/`: `indexer.py`(컴파일·9술어 검증·임베딩·계층 정책) · `retriever.py`(BM25 +
Dense + graph expansion, **계층/신뢰도 인지**) · `mcp_server.py`(MCP 도구, `review_queue`
포함). DuckDB 캐시는 `ltm_cache.db`. `05_Inbox/`는 인덱싱 제외, `06_Raw/`는 전문검색
전용. 정책: [[2026-06-18-layer-and-confidence-aware-retrieval]].

## Open Questions

- 구현 관련: [[Implementation Questions]]
- 연구 관련: [[Research Questions]]

## Next Actions

- [ ] `pip install -r requirements.txt` 후 `python3 90_Engine/indexer.py --force --embed --report`로 신규 계층/raw 인덱싱 통합 검증
- [ ] 첫 실제 source를 `06_Raw/`에 이관하고 `50_Source_Summaries/`에 요약 1건 작성(워크플로우 검증)
- [ ] 실데이터로 검색 가중치(`LAYER_RANK_WEIGHT`/`CONFIDENCE_WEIGHT`) 튜닝
- [ ] `review_queue()`로 검토 큐 주기적 비우기 습관 정립([[Review Policy]])
- [x] 폴더별 인덱싱 정책 + 신뢰도 인지 검색 → [[2026-06-18-layer-and-confidence-aware-retrieval]] (완료)

---

## Sources

- 설계 근거: [[2026-06-18-second-brain-architecture]]
- 정책: [[Second Brain Operating Model]], [[Source Policy]], [[Ingest Policy]], [[Review Policy]], [[Naming Convention]]
