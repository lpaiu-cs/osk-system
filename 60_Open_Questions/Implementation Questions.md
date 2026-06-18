---
type: open-questions
category: implementation
status: active
moc: "[[Development MOC]]"
created: 2026-06-18
updated: 2026-06-18
related:
  - "[[LLM Second Brain]]"
---

# Implementation Questions

> 구현/엔진/도구 관련 미해결 질문 큐입니다. 형식·라우팅은 [[Review Policy]] 참조.

## Open

### [open] retrieval 가중치(provisional prior)를 실측 튜닝
- created: 2026-06-18
- context: `00_System/Retrieval Policy.yaml`의 layer/confidence/status 가중치는 경험적
  최적값이 아니라 출발점(provisional prior)이다. `90_Engine/eval_retrieval.py`로
  MRR@5/Recall@5/review_leakage_rate/raw_overexposure_rate를 측정해 조정해야 한다.
- todo: 실제 도메인 쿼리로 `eval_queries.sample.json`을 확장하고, 가중치를 조정하며
  지표를 비교. raw가 과다노출되면 `06_Raw` weight↓, 유효 원본이 과강등되면 ↑.
- related: [[2026-06-18-layer-and-confidence-aware-retrieval]], [[LLM Second Brain]]
- answer: _(측정 후 기록)_

### [open] 06_Raw 하위 폴더별 임베딩 정책
- created: 2026-06-18
- context: 현재 `raw_policy.embed: true`로 raw 전체를 임베딩한다. 스크린샷 OCR 등은
  BM25만으로 충분할 수 있어 임베딩 비용을 줄일 여지가 있다. config 구조(`raw_policy.
  subfolders`)와 `indexer.py` TODO가 이미 열려 있다.
- related: [[2026-06-18-layer-and-confidence-aware-retrieval]]
- answer: _(미정)_

## Resolved

### [resolved] 폴더별 인덱싱 정책(per-folder index policy)을 어떻게 둘 것인가?
- created: 2026-06-18
- context: 초기 구현은 `05_Inbox/`·`06_Raw/`를 디렉터리 이름으로 단순 제외만 했다.
  해석 계층(`30`/`40`/`50`/`60`/`70`/`80`)은 모두 node+edge로 동일 취급되었다.
- answer (2026-06-18): `LAYER_POLICY`/`policy_for()` 도입.
  `05_Inbox` 제외 / `06_Raw` **전문검색 전용**(node+embed, edge·링크 타깃 제외) /
  그 외 해석 계층 node+edge 유지. 검색 랭킹은 계층 가중치로 차등.
  결정: [[2026-06-18-layer-and-confidence-aware-retrieval]].
- related: [[LLM Second Brain]], [[Ontology Specification]]

### [resolved] 검토 큐(`80_Reviews/`)·결정 status를 retrieval에 반영할 것인가?
- created: 2026-06-18
- context: 낮은 신뢰도/환각 의심 항목이 일반 지식과 동급으로 검색되면 오염 위험.
- answer (2026-06-18): confidence/status 인지 검색 도입. 낮은 신뢰도·폐기 상태는
  **강등 + 표기**(숨기지 않음). `60/70/80`은 기본 검색 제외(`include_reviews=True`로
  포함). 검토 큐 위생용 `review_queue()` MCP 도구 추가.
  결정: [[2026-06-18-layer-and-confidence-aware-retrieval]].
- related: [[Review Policy]], [[LLM Second Brain]]

---

## Sources

- 큐 정의 근거: [[Second Brain Operating Model]]
- 엔진 변경 맥락: [[2026-06-18-second-brain-architecture]]
