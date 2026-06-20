---
id: concept_2026_06_21_cross_device_sync_via_git
title: 2026-06-21-cross-device-sync-via-git
aliases: []
type: decision
moc: "[[Development MOC]]"
tags: [sync, cross-device, git, portability, operations]
status: active
created: 2026-06-21
version: 1.0
node_id: 56103195-e1a1-4346-b6cf-814e414399b7
related:
  - "[[LLM Second Brain]]"
---

# 2026-06-21-cross-device-sync-via-git

**결정.** PC ↔ laptop 등 기기 간 vault 동기화는 **git 하나로 단일화**한다. Obsidian
Sync·iCloud·Dropbox·OneDrive 같은 별도 파일 동기화 도구를 vault 폴더 위에 겹쳐 쓰지
않는다. 기기 이동은 항상 `git pull` / `git push` 로만 한다.

**근거(오너 결정).** vault 폴더가 곧 git 저장소다. 그 위에 두 번째 파일 동기화 주체를
얹으면 `.git` 내부 객체와 DuckDB 캐시(`*.db`/`*.db.wal`)가 두 동기화기 사이에서
경쟁하며 손상·충돌한다. 동기화 경로를 하나로 두는 것이 정합성·복구가능성 면에서
가장 단순하고 안전하다.

**무엇을 동기화하고, 무엇을 안 하나.**
- **동기화(git):** 마크다운 노트 전체. node_id 등 그래프 정체성은 frontmatter에
  들어 있어 git으로 함께 옮겨지므로, 어느 기기에서든 재인덱싱하면 동일 그래프가
  재생성된다.
- **동기화 안 함(`.gitignore`, 기기마다 재생성):** `ltm_cache.db`(임베딩 포함)·
  `.venv`·`.mcp.json`(절대경로 포함 머신별 배선). 파생물이라 옮길 필요가 없고,
  옮기면 오히려 충돌·이식성 문제를 만든다.

**계층 귀속(연동성 분해).**
- ① 데이터(노트) 동기화 = **이 private 인스턴스의 운영 정책** → 이 결정 기록.
- ② 재현 가능한 환경 구성(any-machine bring-up 문서·`.mcp.json.example`) =
  **public 프레임워크 템플릿** → `SETUP.md` §4·§다중 기기 설정, `.mcp.json.example`.
- ③ 머신별 실제 배선(절대경로 `.mcp.json`·venv·ollama 실행·DB 캐시) =
  **시스템/로컬**, 어느 repo에도 커밋하지 않음.

**운영 습관.** 작업 시작 전 `git pull`, 종료 후 `git commit && git push`. 기기를
바꾸기 전에 반드시 push하고, 새 기기에서 pull 후 필요 시 재인덱싱한다. 첫 셋업
절차는 `SETUP.md` §다중 기기 설정 참조.

**관련.** [[LLM Second Brain]]
