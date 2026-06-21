---
id: concept_rag
title: RAG
aliases: [Retrieval-Augmented Generation, 검색 증강 생성]
type: Concept
moc: "[[Architecture MOC]]"
tags: [retrieval, rag, architecture]
status: draft
created: 2026-06-21
version: 1.0
node_id: 01994e93-ea09-472e-9b0f-2a526b320a53
---

# RAG

Retrieval-Augmented Generation. 생성 시점에 외부 지식 저장소를 검색해 그 결과를 모델 컨텍스트에 주입하는 패턴. 파라미터 내부 지식만으로는 최신성·정확성·검증가능성이 부족하다는 한계를 보완한다.

## 핵심 메커니즘
- **검색 후 주입**: 질의를 임베딩/BM25로 매칭해 관련 문서를 끌어오고, 이를 프롬프트 컨텍스트에 합류시켜 생성을 조건화한다.
- **환각 고정의 양날**: 외부 근거 주입은 환각을 "사실"로 굳혀 신뢰도를 높일 수 있으나([[Hallucination as Default]]), 주입된 근거 자체가 틀리면 오히려 그럴듯한 오류를 강화한다 → 출처 신뢰도가 관건.
- **본 Vault 연관**: retrieve_knowledge의 하이브리드 검색+그래프 확장이 RAG의 한 구현이며, 계층/신뢰도 인지로 낮은 신뢰 출처를 강등한다.

> ⚠️ 출처 미연결 — 에이전트 일반지식으로 작성. 1차 출처 보강 시 [[Source Policy]] 따라 인용.

## 핵심 엣지

- `[[RAG]] utilizes [[Hallucination as Default]]` — 외부 지식 주입으로 환각을 사실로 '고정'하는 전략

## Sources

