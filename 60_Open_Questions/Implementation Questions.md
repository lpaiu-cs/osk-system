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
  지표를 측정해 조정한다.
- 1차 측정 (2026-06-18, bge-m3 dense+BM25, 55 nodes, 7쿼리): baseline Recall@5 0.857 >
  flat 0.762 > adversarial 0.0. review_leakage 0(전 설정). → **현재 가중치 유지**가 타당.
  상세: [[2026-06-18-layer-and-confidence-aware-retrieval]] §Tuning Log.
- 남은 todo: corpus가 작고 동질적이라 민감도 제한적. raw/summary/저신뢰 데이터가
  쌓이면 재튜닝(특히 raw_overexposure는 raw 콘텐츠가 있어야 의미). 도메인 쿼리로
  `eval_queries.sample.json` 확장 필요.
- related: [[2026-06-18-layer-and-confidence-aware-retrieval]], [[LLM Second Brain]]
- status: 1차 완료, corpus 확장 후 재튜닝 대기

### [open] strawberry류 쿼리 콘텐츠 갭 (검색 미스)
- created: 2026-06-18
- context: "왜 LLM은 strawberry의 r 개수를 못 세나?"가 BPE/Tokenizer/Glitch Tokens를
  못 끌어온다. 해당 원자 노트에 strawberry 예시 텍스트가 없고 설명이 MOC에만 있어
  dense/BM25 모두 약하게 매칭. **가중치가 아니라 콘텐츠 문제.**
- todo: BPE/Tokenizer 노트에 실제 strawberry 토큰화 예시를 (사실 기반으로) 보강하거나,
  MOC 본문 청크가 검색되도록 개선. 콘텐츠 보강 시 eval로 재확인.
- related: [[Byte Pair Encoding]], [[Tokenizer]], [[Glitch Tokens]]

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
