---
type: project
status: active
updated: 2026-06-19
area: development
related:
  - "[[Development MOC]]"
---

# Discord Bots

> 여러 Discord 봇 모음(`discord_bots` 저장소). music / casino / game / attendance 등.
> 고수준 지도만 둔다. **코드·배포·파일경로 등 레포 종속 디테일은 vault가 아니라
> 프로젝트 레포의 메모리에 보관한다**(`~/.claude/projects/-Users-lpaiu-vs-discord-bots/memory/`).
> 근거: [[2026-06-19-claude-memory-routes-to-llm-vault]].

## Purpose

Discord용 봇들을 운영·개선한다. 핵심 활성 작업은 **music-bot**(Lavalink 기반 음악 재생).

## 증류된 일반 사실 (cross-project)

- UX 철학: 슬래시 명령은 최소화, 버튼/셀렉트 피드백 UI에 투자(슬래시 단축 추가 지양).
- music-bot의 YouTube 재생은 데이터센터 IP login-wall 이슈를 만날 수 있음 — 일반 원리는 [[YouTube Datacenter IP Login Wall]]. (구체 진단/패치/서버 경로는 레포 메모리에.)

## 레포 종속 디테일 위치

- 배포 방식, Lavalink/yt-cipher 구성, 코드 패치 등은 `discord_bots` 레포 `.claude` 메모리 참조.

## Open Questions

- [[Implementation Questions]]

---

## Sources

- 일반 원리: [[YouTube Datacenter IP Login Wall]]
- 메모리 라우팅 정책: [[2026-06-19-claude-memory-routes-to-llm-vault]]
