---
id: concept_pytorch
title: PyTorch
aliases: [torch, 파이토치]
type: Concept
moc: "[[Implementation MOC]]"
tags: [framework, deep-learning, pytorch]
status: draft
created: 2026-06-21
version: 1.0
node_id: fa9cb52f-c391-407d-b9b1-966e5e7ece65
---

# PyTorch

동적 계산 그래프(define-by-run)와 autograd를 핵심으로 하는 딥러닝 프레임워크. 연구·프로토타이핑의 사실상 표준 스택으로, 텐서 연산·자동미분·GPU 가속을 파이썬 친화적 API로 제공한다.

## 핵심 메커니즘
- **동적 그래프 + autograd**: 실행 흐름에 따라 계산 그래프를 그때그때 구성해 디버깅·유연성이 높다.
- **추상화 vs 제어 트레이드오프**: [[llm.c]]가 의존성 없는 순수 C/CUDA로 PyTorch 스택을 "대체"하려는 대상 — 편의·범용성(PyTorch) 대 직접 제어·최소 의존·성능(llm.c)의 대비를 보여준다.
- **본 프레임워크 위치**: [[nanoGPT]]·[[minGPT]] 등 교육용 구현의 기반 런타임.

> ⚠️ 출처 미연결 — 에이전트 일반지식으로 작성. 1차 출처 보강 필요.

## 핵심 엣지

<!-- 아직 엣지 없음 -->

## Sources

