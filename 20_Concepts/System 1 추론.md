---
id: concept_system_1
title: System 1 추론
aliases: [System 1, System 1 Thinking, 시스템 1]
type: Concept
moc: "[[Architecture MOC]]"
tags: [reasoning, cognition, karpathy]
status: draft
created: 2026-06-21
version: 1.0
node_id: 96fc63d3-36d2-4e23-a726-faaf032b526c
---

# System 1 추론

Kahneman 이중과정 이론에서 온 개념으로, 빠르고 자동적이며 직관적인 추론 양식. Karpathy는 LLM의 단일 forward pass를 System 1에 비유한다 — 자기검증 단계 없이 학습된 패턴에 따라 즉시 토큰을 생성하는 방식.

## 핵심 메커니즘
- **무검증 즉시성**: 단일 forward pass는 중간 비판·재고 단계가 없어, 가장 그럴듯한 다음 토큰을 곧바로 출력 평면으로 흘려보낸다.
- **환각의 근원**: 자기검증 부재가 곧 [[Hallucination as Default]]의 구조적 원인 — 틀린 패턴도 검열 없이 그대로 출력된다.
- **대비 축**: 분기 탐색·자기비판을 포함하는 [[System 2 추론]]과 대비되며, Reflection Loop 같은 설계로 System 2를 덧대 보완한다.

## 핵심 엣지

- `[[System 1 추론]] causes [[Hallucination as Default]]` — 자기검증 없는 직관적 추론이 환각의 근본 원인

## Sources

- Intro to Large Language Models — Karpathy (https://www.youtube.com/watch?v=zjkBMFhNj_g)
