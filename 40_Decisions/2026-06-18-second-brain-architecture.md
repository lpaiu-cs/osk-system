---
type: decision
date: 2026-06-18
status: active
project: LLM Second Brain
confidence: medium
sources: []
related:
  - "[[LLM Second Brain]]"
  - "[[Second Brain Operating Model]]"
---

# Evolve llm-vault into an LLM-native second brain

## Decision

기존 `llm-vault` 저장소를 **LLM-native second brain으로 진화**시킨다. 별도의
`llm-wiki` 저장소를 새로 만들지 않는다. 기존 아키텍처(00_System / 10_MOC /
20_Concepts / 90_Engine + DuckDB + Ollama + MCP + 9-predicate 온톨로지)를 버리지
않고, 그 위에 아카이브·출처·결정·검토 계층을 **추가(additive)**한다.

## Context

- `llm-vault`는 이미 MCP 서버, DuckDB 인덱싱, Ollama 임베딩, graph retrieval,
  엄격한 9-predicate 온톨로지를 갖춘 **동작하는 LLM 장기 기억 런타임**이다.
- 그러나 second brain / LLM Wiki 아카이브 시스템으로서는 다음 계층이 비어 있었다:
  불변 raw 원본, inbox 인입, source 요약, 프로젝트 대시보드, 결정 기록, 열린 질문,
  모순 보존, 사람 검토 큐.
- 별도로 검토되던 "LLM Wiki" 계획은 아카이브 규율은 좋았으나 런타임/인덱싱이 없었다.

## Alternatives Considered

1. **별도 `llm-wiki` 저장소 신설** — 아카이브 규율은 깔끔하나 런타임/MCP를
   처음부터 다시 구축해야 하고, 지식이 두 저장소로 분절된다. 기각.
2. **`llm-vault`를 현행 유지(개념 그래프 전용)** — 출처·불확실성·모순·검토가
   없어 "근거 없는 LLM 기억 그래프"로 퇴화할 위험. 기각.
3. **`llm-vault`를 second brain으로 확장 (채택)** — 동작하는 런타임 위에 아카이브
   규율을 더해 단일 저장소로 통합.

## Rationale

- `llm-vault`는 이미 MCP/런타임/인덱싱 인프라를 갖추고 있다 → 재사용이 최선.
- 이전 LLM Wiki 계획은 아카이브 규율은 우수했으나 런타임이 없었다.
- 채택안은 둘을 결합한다:
  `llm-vault 런타임/인덱싱/MCP` + `LLM Wiki 아카이브 규율` + `second brain
  프로젝트/결정/검토 워크플로우`.
- raw / source / decision / review 계층을 반드시 추가해야, 시스템이 **근거 없는 LLM
  메모리 그래프로 퇴화하는 것**을 막을 수 있다. LLM은 틀릴 수 있으므로([[Hallucination
  as Default]]) 원본·인용·불확실성·모순·검토는 선택이 아니라 필수다.

## Consequences

- 새 계층 추가: `05_Inbox/`, `06_Raw/`, `30_Projects/`, `40_Decisions/`,
  `50_Source_Summaries/`, `60_Open_Questions/`, `70_Contradictions/`, `80_Reviews/`.
- 새 시스템 정책: [[Source Policy]], [[Ingest Policy]], [[Review Policy]],
  [[Naming Convention]], [[Second Brain Operating Model]], 그리고 [../AGENTS.md](../AGENTS.md).
- 엔진 변경(최소): `90_Engine/indexer.py`·`mcp_server.py`가 `05_Inbox/`·`06_Raw/`를
  인덱싱에서 제외 → raw가 false edge/노이즈 node로 그래프를 오염시키지 않음.
- 온톨로지: 9개 술어는 불변, §0 "그래프 적용 범위"만 추가([[Ontology Specification]]).

## Risks

- **계층 과잉으로 인입 마찰 증가** — 매번 11단계를 다 밟으면 피로. 완화: [[Ingest
  Policy]]에 자료 유형별 최소 경로 제공.
- **개념 과적합** — 모든 source를 concept node로 승격하면 그래프 오염. 완화:
  Anti-Bloat 경고 + 기본 도착지를 `50_Source_Summaries/`로 고정.
- **엔진과 디렉터리 정책의 드리프트** — 폴더 정책이 코드와 어긋날 수 있음. 완화:
  indexer/mcp_server에 주석 명시 + `per-folder index policy` TODO.
- **검토 큐 방치** — `80_Reviews/`가 쌓이기만 하고 비워지지 않을 위험. 완화:
  상태(status) 추적 + 주기적 검토 습관(추후 자동화 여지).

## Review Triggers

이 결정을 재고해야 하는 신호:

- 단일 저장소가 너무 커져 인덱싱/검색 성능이 실사용 기준 미달일 때.
- 인입 마찰이 너무 커서 실제로 자료가 `05_Inbox/`에만 쌓이고 흐르지 않을 때.
- raw 보존 정책이 저장 용량/프라이버시 측면에서 비현실적이 될 때.
- 다중 사용자/공유 요구가 생겨 아카이브와 런타임의 분리가 필요해질 때.

위 트리거가 발생하면 [[Review Policy]] §4에 따라 `decision-needs-reconsideration`로
올리고, 결정이 바뀌면 [../AGENTS.md](../AGENTS.md) §4의 supersede 절차를 따른다(본
기록을 `status: superseded`로 두고 새 결정에서 `replaces`로 링크).

## Sources

- 본 결정은 이 작업 세션의 설계 논의에서 도출됨(외부 출처 없음).
- 관련 정책/모델: [[Second Brain Operating Model]], [[Source Policy]], [[Ingest Policy]],
  [[Review Policy]], [[Naming Convention]]
- 관련 개념: [[Hallucination as Default]]
