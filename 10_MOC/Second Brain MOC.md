---
id: moc_second_brain
title: Second Brain MOC
aliases:
  - 세컨드 브레인 MOC
  - Second Brain Root
type: Framework-Root-MOC
moc: self
parent_moc: null
child_moc:
  - "[[AI Research MOC]]"
  - "[[Development MOC]]"
  - "[[Life Admin MOC]]"
  - "[[Karpathy LLM Framework MOC]]"
tags:
  - SecondBrain
  - PKM
  - Knowledge-Graph
  - LTM
status: evergreen
created: 2026-06-18
updated: 2026-06-18
version: 1.0
related:
  - "[[Second Brain Operating Model]]"
  - "[[LLM Second Brain]]"
---

> [!IMPORTANT] 문서 정체성
> 본 노트는 `llm-vault`가 LLM-native second brain으로 운영될 때의 **최상위 진입
> 지도(root MOC)**입니다. 시스템 정책, 계층, 영역별 MOC, 프로젝트, 검토 큐로 가는
> 허브 역할을 합니다.

전체 멘탈 모델은 [[Second Brain Operating Model]]을 보세요.

---

## 1. 시스템 정책 (00_System)

- [[Second Brain Operating Model]] — 계층 모델과 데이터 흐름
- [[Source Policy]] — 무엇이 source인가
- [[Ingest Policy]] — 인입 워크플로우
- [[Review Policy]] — 검토 카테고리와 상태
- [[Naming Convention]] — 명명 규칙과 frontmatter
- [[Ontology Specification]] — 9-predicate 헌법 (그래프 적용 범위 §0 포함)
- 에이전트 행동 규칙: [../AGENTS.md](../AGENTS.md)

## 2. 영역별 MOC (Domains)

- [[AI Research MOC]] — AI/LLM 이론·연구 노트
- [[Development MOC]] — 코드·디버깅·도구·환경
- [[Life Admin MOC]] — 행정·개인 워크플로우 기록
- [[Karpathy LLM Framework MOC]] — 초기 코퍼스(LLM 멘탈 모델)와 그 하위 MOC

## 3. 활성 프로젝트 (30_Projects)

- [[LLM Second Brain]] — 이 시스템 자체
- [[TFT RL Simulator]]
- [[Artificial Consciousness]]
- [[Discord Bots]]
- [[Local Dev Environment]]

## 4. 계층 바로가기 (Layers)

| 계층 | 경로 | 역할 |
|------|------|------|
| Inbox | `05_Inbox/` | 미처리 인입 |
| Raw | `06_Raw/` | 불변 원본 |
| Summaries | `50_Source_Summaries/` | 원본 압축 이해 |
| Decisions | `40_Decisions/` | 중요 선택 기록 |
| Questions | `60_Open_Questions/` | 미해결 질문 |
| Contradictions | `70_Contradictions/` | 모순 보존 |
| Reviews | `80_Reviews/` | 사람 검증 큐 |

## 5. 검토·충돌 큐 (품질 게이트)

- [[Needs Human Review]]
- [[Low Confidence Claims]]
- [[Possible Hallucinations]]
- [[Theory Conflicts]] · [[Source Conflicts]] · [[Stale Assumptions]]

## 6. 미해결 질문

- [[Research Questions]] · [[Implementation Questions]] · [[Admin Questions]]

---

## Sources

- 설계 근거: [[2026-06-18-second-brain-architecture]]
