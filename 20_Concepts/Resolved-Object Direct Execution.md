---
id: concept_resolved_object_direct_execution
title: Resolved-Object Direct Execution
aliases:
  - 리졸브된 객체 직접 실행
  - Resolved Result Passthrough
  - 재질의 회피
type: Concept
moc: "[[Architecture MOC]]"
parent_moc: "[[Architecture MOC]]"
tags:
  - AI/Architecture
  - Agent
  - Engineering-Pattern
status: evergreen
created: 2026-06-20
updated: 2026-06-20
version: 1.0
node_id: 7e3a1b9c-2d4f-4a6b-8c0d-1e2f3a4b5c6d
embedding_model: null
embedding_hash: null
last_indexed: null
predicate_count: 1
related_nodes:
  - "[[Tool Use]]"
  - "[[discord_bots related encoded playback]]"
---

# Resolved-Object Direct Execution

이미 해석(resolve)되어 **그 자체로 실행 가능한 구조화 객체**(핸들·토큰·디코딩된 레코드)를 손에 쥐고 있다면, 그것을 다시 사람이 읽는 질의 문자열로 직렬화해 **재질의(re-query)** 하지 말고 **객체를 그대로 실행 경로에 넘기는** 설계 원칙. 재질의는 (1) 직렬화에서 정보를 잃고 (2) 같은 작업을 두 번 하며 (3) 원본과 다른 결과로 매칭될 위험이 있다.

## 핵심 메커니즘

1. 파이프라인의 해석 단계가 실행 가능한 객체(예: 디코딩된 트랙, 파일 핸들, 파싱된 레코드)를 산출한다.
2. 그 객체를 문자열/질의로 되돌리지 않고 다음 단계(실행기)에 직접 전달한다.
3. 재질의가 불가피한 경계(객체가 없는 폴백 경로)에서만 문자열 질의로 내려간다.

## 핵심 엣지

- `[[Resolved-Object Direct Execution]] implemented_by [[discord_bots related encoded playback]]` — 추상 원칙이 디스코드 뮤직봇 /related 의 'Mix resolve → encoded 트랙 직접 재생'으로 구체화됨

> [!NOTE] Tool Use 와의 관계
> 같은 층위의 도구/리소스 실행 패턴이지만 9-술어 중 단정 가능한 술어가 없어, 헌법 §4 *"불확실하면 비워라"* 에 따라 그래프 엣지 대신 `related_nodes` + 임베딩 근접성으로만 [[Tool Use]] 와 연결한다.

## Sources

- 도출 사례: discord_bots 뮤직봇 `/related` (2026-06-20). 상세: [[discord_bots related encoded playback]]
