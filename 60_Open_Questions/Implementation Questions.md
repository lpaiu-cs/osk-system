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

_(현재 열린 구현 질문 없음)_

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
