---
id: concept_system_2
title: System 2 추론
aliases: [System 2, System 2 Thinking, 시스템 2]
type: Concept
moc: "[[Architecture MOC]]"
tags: [reasoning, cognition, karpathy]
status: draft
created: 2026-06-21
version: 1.0
node_id: 06963625-ce2e-4f9f-ba65-1fc0f4ce6f83
---

# System 2 추론

Kahneman 이중과정 이론에서 온 개념으로, 느리고 신중하며 의식적인 추론 양식. 분기 탐색·자기비판·검증을 포함한다. LLM에서는 단일 forward pass([[System 1 추론]])를 넘어, 사고의 분기를 스스로 검토하는 메커니즘으로 구현된다.

## 핵심 메커니즘
- **고정 연산 초월**: 토큰당 고정된 forward 연산만으로는 부족한 고차 추론을, 반복·재고로 추가 연산을 투입해 달성한다.
- **코드적 구체화**: [[Reflection Loop]] 같은 자가 수정 루프 아키텍처가 System 2를 실제 코드로 구현한 형태다(초안 → 비판 스캔 → 재작성).
- **대비 축**: 즉시적·무검증인 [[System 1 추론]]과 대비되며, 그 한계(환각)를 방어하기 위한 상위 계층이다.

## 핵심 엣지

- `[[System 2 추론]] implemented_by [[Reflection Loop]]` — 사고의 분기를 검토하는 고차 추론이 자가 수정 루프 아키텍처로 구체화됨

## Sources

- Intro to Large Language Models — Karpathy (https://www.youtube.com/watch?v=zjkBMFhNj_g)
