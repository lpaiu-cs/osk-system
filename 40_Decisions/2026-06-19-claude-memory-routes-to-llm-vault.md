---
type: decision
date: 2026-06-19
status: active
project: LLM Second Brain
confidence: high
sources: []
related:
  - "[[LLM Second Brain]]"
  - "[[Second Brain Operating Model]]"
---

# Claude(에이전트)의 장기 기억을 llm-vault로 라우팅한다

## Decision

에이전트(Claude Code 등)의 지속 기억을 **두 층으로 분리**한다:

1. **코드·레포 종속 디테일 → 프로젝트 레포의 `.claude` 메모리** (`~/.claude/projects/<repo>/memory/`). 파일 경로, 줄 단위 동작, 배포 구성, 패치 내용 등은 **반드시 해당 레포별로** 저장한다. 중앙 vault에 넣으면 버전/레포가 달라 빠르게 낡아 망가진다(사용자 지시 2026-06-19).
2. **증류된 공통·일반 지식 → `llm-vault`** (`/Users/lpaiu/vs/llm-vault`). 프로젝트를 가로지르는 재사용 가능한 원리/개념/결정만 vault의 [AGENTS.md](../AGENTS.md) 계층 규약대로 저장한다.

llm-vault MCP는 전역(user scope, `~/.claude.json` top-level `mcpServers`)으로 등록되어 모든 프로젝트 세션에서 사용 가능하다(2026-06-19 등록, 재시작 후 활성).

## Context

- 사용자가 "너의 메모리는 이제 llm-vault를 사용해야 돼"라고 지시(2026-06-19).
- `llm-vault`는 이미 MCP 서버·DuckDB 인덱싱·Ollama 임베딩·9-predicate 온톨로지를 갖춘 동작하는 LLM 장기기억 런타임이다([[2026-06-18-second-brain-architecture]]).
- 단, `llm-vault` MCP는 vault 디렉터리 기준 `.mcp.json`에만 정의되어 있어, **다른 프로젝트(예: discord_bots) 세션에는 자동 연결되지 않는다.** 그 경우 에이전트는 vault에 마크다운으로 직접 기록한다.

## Consequences

- 증류된 공통 지식만 vault 계층에 기록: 일반 원리/개념→`20_Concepts/`, 프로젝트 고수준 지도→`30_Projects/`(코드 디테일 금지, 레포로 포인터), 중요선택→`40_Decisions/`, 불확실→`80_Reviews/`.
- 코드 디테일(파일 경로, 패치, 배포 구성, 줄 단위 동작)은 vault에 넣지 않는다 → 프로젝트 레포 `.claude` 메모리에만.
- per-project `.claude` 메모리는 (a) 코드 디테일의 1차 저장소이자 (b) vault로 가는 부트스트랩 포인터를 함께 유지한다.
- 비밀값(password/refreshToken/arl/poToken/visitorData/masterDecryptionKey)은 vault·레포 어디에도 평문 저장 금지, `***MASKED***` 처리.

## Risks

- vault MCP 미연결 세션에서 에이전트가 vault 존재를 모르고 `.claude` 메모리에만 쓸 위험 → 완화: `.claude` 부트스트랩 포인터 유지.
- 이중 기록(vault + .claude) 드리프트 → 완화: `.claude`는 포인터/인덱스 역할로만 한정.

## Review Triggers

- vault MCP가 모든 세션에 전역 연결되면(`.claude` 부트스트랩 불필요) 본 결정 갱신.
- 메모리 라우팅 정책이 바뀌면 [AGENTS.md](../AGENTS.md) §4 supersede 절차.

## Sources

- 본 결정은 2026-06-19 작업 세션의 사용자 지시에서 도출(외부 출처 없음).
- 관련: [[Naming Convention]], [[Ingest Policy]]
