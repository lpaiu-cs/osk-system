---
id: concept_scaling_laws
title: Scaling Laws
aliases: [Scaling Law, Neural Scaling Laws, 스케일링 법칙]
type: Concept
moc: "[[AI Research MOC]]"
tags: [scaling, compute, ai-research]
status: draft
created: 2026-06-21
version: 1.0
node_id: 5aab4fb4-d129-4dda-8f9d-1f6e23402829
---

# Scaling Laws

모델 성능이 파라미터 수·학습 데이터·연산량(compute)에 대해 멱법칙(power law)으로 예측가능하게 향상된다는 경험 법칙. [[The Bitter Lesson]]의 "컴퓨테이션 우위" 원리가 정량적 법칙으로 구체화된 형태다.

## 핵심 메커니즘
- **예측가능한 멱법칙**: 손실(loss)이 규모의 로그축에서 직선적으로 감소 — 더 큰 모델 + 더 많은 데이터 + 더 많은 연산이 일관되게 더 나은 성능을 낸다.
- **Bitter Lesson의 정량화**: 정교한 사람 휴리스틱보다 규모 확장이 이긴다는 철학적 주장을, 투자 대비 성능을 예측하는 공학 법칙으로 환원한다.
- **함의**: 아키텍처 미세조정보다 규모·데이터·연산의 확보가 우선이라는 자원 배분 논리를 제공한다.

> ⚠️ 출처 미연결 — 에이전트 일반지식으로 작성(Kaplan 2020 등). 1차 출처 보강 필요.

## 핵심 엣지

<!-- 아직 엣지 없음 -->

## Sources

