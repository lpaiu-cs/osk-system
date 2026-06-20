---
id: concept_empirical_confirmation_before_claiming
title: Empirical Confirmation Before Claiming
aliases: [실측 후 단정, 근본원인 확정 전 증거 캐포, Evidence Before Verdict]
type: Concept
moc: "[[Development MOC]]"
tags: [Engineering-Practice, Agent, Debugging]
status: draft
created: 2026-06-21
version: 1.0
node_id: ab1c4027-c639-474a-acf7-a3dc900bc8cc
---

# Empirical Confirmation Before Claiming

근본 원인을 "확정"이라고 선언하기 전에, 정적 분석의 그럴듯한 가설이 아니라 **로그·관측으로 실측 증거를 먼저 캡처**해야 한다는 디버깅·추론 규율. 검증되지 않은 가설을 "맞다"고 단정하는 순간 그것은 사실처럼 보이는 환각이 된다.

## 핵심 메커니즘
1. 정적 코드 분석은 *가능한* 원인을 좁힐 뿐, *실제* 원인을 확정하지 못한다 — 코드 스멜은 가설이지 증거가 아니다.
2. 단정 전에 계측(타깃 로그·메트릭)을 심고 **재현**해 증거를 캡처한다(예: "UI가 그린 곡" vs "백엔드가 시작한 곡"을 각각 로깅해 대조).
3. "정말 확인했나?"라는 물음에 "확인했다"가 아니라 **증거(로그 라인)** 를 제시할 수 있어야 한다.
4. 첫 가설이 틀릴 수 있음을 전제하고, 대안 가설도 계측으로 동시에 가른다.

## 안티패턴
정적 분석만으로 "이게 원인으로 확정"이라 사용자에게 선언 → 사용자가 시간 들여 재현했더니 실제 원인은 다른 곳. 이는 자가검증 누락이며 과신이다.

상위 자가검증 개념은 [[Reflection Loop]], 발현 사례는 [[discord_bots Now Playing 정합성]].

## 핵심 엣지

- `[[Empirical Confirmation Before Claiming]] extends [[Reflection Loop]]` — 출력 전 자가검증 루프에 '근본원인 단정 전 실측 증거 캐포' 요건을 더한 지원 규율
- `[[Empirical Confirmation Before Claiming]] implemented_by [[discord_bots Now Playing 정합성]]` — 정적 분석으로 매핑오류를 '확정'이라 단정했다가 실측 후 실제 원인(stale 메시지+related 폴백)이 다름이 드러난 사례

## Sources

- discord_bots Now Playing 디버깅 세션 (2026-06-21)
