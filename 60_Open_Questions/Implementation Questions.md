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

### [open] 폴더별 인덱싱 정책(per-folder index policy)을 어떻게 둘 것인가?
- created: 2026-06-18
- context: 현재 `90_Engine/indexer.py`는 `05_Inbox/`·`06_Raw/`를 디렉터리 이름으로
  단순 제외만 한다(코드 내 TODO 참조). 해석 계층(`30`/`40`/`50`/`60`/`70`/`80`)은
  모두 node+edge로 동일 취급된다. 향후 계층별로 다르게 다뤄야 할 수 있다.
- 후보: `06_Raw`는 전문(full-text) 검색만 / `80_Reviews`는 검색 우선순위 강등 /
  `40_Decisions`는 node+edge 유지 / `50_Source_Summaries`는 raw 대리물로 가중치 부여.
- related: [[LLM Second Brain]], [[Ontology Specification]]
- answer: _(미정)_

### [open] 검토 큐(`80_Reviews/`)·결정 status를 retrieval에 반영할 것인가?
- created: 2026-06-18
- context: 낮은 신뢰도/환각 의심 항목이 일반 지식과 동급으로 검색되면 오염 위험.
  confidence-aware retrieval 또는 status 필터가 필요할 수 있다.
- related: [[Review Policy]], [[LLM Second Brain]]
- answer: _(미정)_

## Resolved

_(없음)_

---

## Sources

- 큐 정의 근거: [[Second Brain Operating Model]]
- 엔진 변경 맥락: [[2026-06-18-second-brain-architecture]]
