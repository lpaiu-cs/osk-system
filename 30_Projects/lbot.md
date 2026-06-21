---
id: concept_lbot
title: lbot
aliases: []
type: project
moc:
tags: [kakaotalk, frida, bot, automation]
status: active
created: 2026-06-20
updated: 2026-06-20
version: 1.0
node_id: 39d198dd-02f0-4568-8f05-4c79ba37458a
---

# lbot

> KakaoTalk 오픈챗 운영 봇(`android_bots` 저장소). 에뮬레이터(redroid) 위 KakaoTalk을 frida로 계측해 메시지 수신·발신·운영(강퇴/가리기/제재)·게임·그룹 보이스룸을 자동화한다. **고수준 지도만 둔다 — 코드·배포·파일경로·역공학 시그니처 등 레포 종속 디테일은 vault가 아니라 레포 메모리**(`~/.claude/projects/-Users-lpaiu-vs-android-bots/memory/`)에 보관한다. 근거: [[2026-06-19-claude-memory-routes-to-llm-vault]].

## Purpose

오픈챗 운영 자동화: 규칙 기반 모더레이션(강퇴/언밴/메시지 가리기/결합 제재), 게임(숫자야구·오목 edit-frame 보드), 개발자 문의 라우팅, 그룹 보이스룸 호스팅 + 스피커 자동 승격.

## 아키텍처 (high-level)

- **main.py** — 메시지 루프(서버에선 screen `lbot`). DB 폴링 → 명령/모더레이션 처리 → outbox 발행. `--init` = 배포/콜드스타트(브리지 재시작 + 백로그 스킵).
- **bridge runtime** — 별도 프로세스(서버=systemd `lbot-frida-bridge`). outbox(sqlite) 드레인 → frida RPC로 KakaoTalk 조작. op 종류로 분기(text/edit/kick/hide/voiceroom…). 회로차단기·DLQ·재시도 내장.
- **frida agent (JS)** — KakaoTalk에 주입. anti-detect + LOCO/vox op 후킹. 송신은 fire-and-forget(클라이언트가 실권위, 와이어 포맷 동결).

## 증류된 일반 교훈 (cross-project)

- **장수 데몬은 배포 때 명시적으로 재시작해야 한다.** `git pull` + 앱(main) 재시작만으로는 별도 서비스(systemd 브리지)가 옛 코드로 *박제*되어, 신규 op 라우팅 누락 등 "켜지는데 종료는 안 되는" 부분 고장을 만든다. 배포 절차에 서비스 재시작을 포함하라(lbot: `main.py --init`이 owner 무관 브리지 재시작을 트리거).
- **fire-and-forget + 인메모리 상태는 재시작에 취약.** 활성 상태(예: 호스팅 중 보이스룸)는 영속화해야 재시작 후 복구 가능. 또한 **enqueue 성공 ≠ 전달 성공** — 정리(leave) 마커는 전달이 확인되기 전까지 보존해야 유실되지 않는다.
- **콜드 세션 자원 해석 지연.** 난독화 심볼을 런타임 점진 스캔으로 찾는 계측은, 갓 attach한 세션에서 첫 op이 "manager not ready"로 실패할 수 있다 → 세션이 살아있을 때 미리 warming.
- **재시작 시 프리다 세션은 graceful teardown(detach/unload)** 해야 다음 attach가 되돌려지지 않은 후크 위에 적층되어 불안정해지는 것을 막는다.

## 레포 종속 디테일 위치

보이스룸 시그니처·anti-frida 우회·브리지 생명주기 결함·프로덕션 배포 구조 등 구체 내용은 `android_bots` 레포 `.claude` 메모리 참조 — 특히 `voiceroom-antifrida-bypass`, `bridge-restart-frida-teardown-defect`, `lbot-prod-server-deploy`, `loco-op-discovery-campaign`.

## Open Questions

- [[Implementation Questions]]

---

## Sources

- 메모리 라우팅 정책: [[2026-06-19-claude-memory-routes-to-llm-vault]]
- 자매 프로젝트(봇 운영 UX 원리 공유): [[Discord Bots]]

## 핵심 엣지

<!-- 아직 엣지 없음 -->

