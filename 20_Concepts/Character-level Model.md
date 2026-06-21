---
id: concept_character_level_model
title: Character-level Model
aliases: [Character-level Language Model, 문자 단위 모델]
type: Concept
moc: "[[Architecture MOC]]"
tags: [tokenization, nlp]
status: draft
created: 2026-06-21
version: 1.0
node_id: ee38ef78-cfeb-4f87-81df-7f7add27202f
---

# Character-level Model

텍스트를 개별 문자(character) 단위로 토큰화해 처리하는 언어모델. 어휘집이 작고 미등록어(OOV)가 원천적으로 없지만, 시퀀스가 길어지고 의미 단위 학습이 비효율적이라는 한계가 있다.

## 핵심 메커니즘
- **작은 어휘 · OOV 없음**: 모든 텍스트를 문자 집합만으로 표현 → 어휘 폭발이 없고 희귀어도 처리 가능.
- **긴 시퀀스 비용**: 같은 문장이 훨씬 많은 토큰으로 쪼개져 컨텍스트·연산 비용이 커지고, 단어/형태소 수준 의미 학습이 느리다.
- **대체 관계**: [[Byte Pair Encoding]] 같은 서브워드 토크나이저가 문자 단위 방식을 "대체"해, 어휘 크기와 시퀀스 길이 사이의 균형을 잡는다.

> ⚠️ 출처 미연결 — 에이전트 일반지식으로 작성. 1차 출처 보강 필요.

## 핵심 엣지

<!-- 아직 엣지 없음 -->

## Sources

