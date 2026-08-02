---
id: concept_working_memory_vault
title: Working Memory 층위를 vault에 둘 것인가
aliases: []
type: open_question
moc:
tags: []
status: draft
created: 2026-06-25
version: 1.0
node_id: 17c09f85-e6d1-4892-9063-e539a1452961
---

# Working Memory 층위를 vault에 둘 것인가

# Working Memory 층위를 vault에 둘 것인가

## 질문

긴 에이전트 세션의 **작업 기억(working memory, WM)**을 위해 vault에 전용 층위를 신설할
것인가, 아니면 현행(모델 컨텍스트=STM + `30_Projects`=durable 프로젝트 상태)으로 충분한가?

## 배경 — 3-tier 메모리 구도

- **STM** = 모델 컨텍스트 윈도우([[Context Window]]). 휘발성, 세션 종료 시 소멸.
- **WM** = STM↔LTM 완충지대. 가변·세션간 지속·아직 canonical 아님.
- **LTM** = vault 그래프(20_Concepts/40_Decisions 등). 큐레이션된 내구 지식.

`30_Projects`는 *역할상* WM을 겸하지만 *기질은 LTM*이다(임베딩 + git-sync + 9-predicate
온톨로지). 그래서 고churn 스크래치로 쓰면 (a) 임베딩 thrash, (b) 기본 검색 오염 비용을 치른다.
WM을 WM답게 만드는 본질은 **값싼 쓰기 + 기본 검색 격리**인데 `30_Projects`는 둘 다 없다.

## 판별 기준

"세션/기기를 넘나드는, 가변적, non-canonical 작업 상태를, 검색은 되되 LTM과 분리해 두고
싶은가?"
- 아니오(단일 세션 스크래치) → STM + vault 밖 임시파일로 충분, 신설 불필요.
- 예 → 전용 층위 정당화(특히 Mac↔Windows 동시 작업의 세션간 연속성; 컨텍스트 윈도우는 기기를
  못 넘지만 git-sync 파일은 넘는다).

## 신설 시 설계 (4규칙)

1. 비임베딩(BM25-only/미인덱싱) — 값싼 쓰기. 2. 기본 검색 제외(reviews처럼 opt-in 플래그).
3. in-place 가변 + TTL/GC, 9-predicate 온톨로지 면제. 4. 명시적 consolidation WM→LTM(졸업).
엔진엔 이미 검토계층(60/70/80) 제외 메커니즘이 있어 나중에 추가하는 비용이 낮다.

## 잠정 입장

**지금은 보류(YAGNI).** `30_Projects`는 durable 체크포인트 전용으로 쓰고 고churn 스크래치는
넣지 않는다. "검색되되 non-canonical·가변·세션간" 선반이 반복적으로 아쉬워지면 위 4규칙으로
신설한다. (낮은 switching cost가 보류를 정당화.)

## 관련

- [[2026-06-19-claude-memory-routes-to-ltm-vault]] — 에이전트 메모리 2층 분리 규약(.claude vs vault)
- [[Context Window]] · [[LLM OS]] — STM=RAM 대응 개념

## 핵심 엣지

<!-- 아직 엣지 없음 -->

## Sources

