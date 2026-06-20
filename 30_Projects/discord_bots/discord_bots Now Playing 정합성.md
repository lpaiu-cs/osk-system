---
id: concept_discord_bots_now_playing
title: discord_bots Now Playing 정합성
aliases: []
type: Project-Note
moc:
tags: [Project/discord_bots, Bug, Engineering-Log]
status: draft
created: 2026-06-21
version: 1.0
node_id: b8e849b0-37c4-4a2c-95af-6718312eb52d
---

# discord_bots Now Playing 정합성

디스코드 뮤직봇 `/play` → `/related` 이후 Now Playing UI 정합성 버그와 그 디버깅 과정 기록.

## 증상
1. UI 표시 곡 ≠ 실제 재생 곡 (클래식이 나오는데 표시는 jpop "첫사랑 같은 제이팝").
2. 일시정지했는데 진행바가 계속 진행.
3. 정지를 눌러도 안 멈추고 두 곡 사이를 깜빡이며 자동 재생.

## 오진 (정적 분석으로 단정)
백엔드가 손으로 관리하는 `state.currentTrack`/`paused`가 실제 플레이어와 어긋나는 "매핑 오류"라고 **확정이라 단정**하고, `start` 이벤트 신뢰원화·정지 race 차단·진행바 추정 게이트로 수정 시도(commit a49f082). 정지 race(③)는 실제 버그였지만, ①②의 주원인은 아니었다.

## 실제 원인 (로그 계측 후 확정)
- **stale Now Playing 메시지**: 새 now-playing 메시지를 attach 할 때 이전 메시지를 비활성화하지 않아, 옛 메시지·컨트롤이 활성처럼 남아 상태가 어긋남.
- **`/related` DB 폴백**: Mix 결과가 부족하면 라이브러리(`searchSongs`/`getRandomSongs`)에서 **무관한 곡**(그 jpop)을 큐에 넣어 표시를 오염시킴.

## 최종 수정 (commits 49b8a2a / 7b22ef1 / d218a3e)
- `NowPlayingUpdater.attach`: 이전 메시지 컨트롤 비활성(`components: []`), per-entry 상태(`_lastRendered` 등) + `getEntry`/`refresh` + tick 재진입 가드.
- `handleMusicButton`: stale control(옛 메시지) 클릭 감지 → 컴포넌트 비활성 + 활성 메시지 `refresh` 즉시 동기화.
- `/related`: **DB 폴백 제거**(YouTube Mix 전용)로 무관 곡 유입 차단.

## 교훈
정적 분석의 그럴듯한 가설을 "확정"이라 선언하지 말 것 — UI render 로그 vs 백엔드 'Now playing' 로그를 대조해 **실측**한 뒤에야 진짜 원인(stale 메시지 + related 폴백)이 드러났다. 일반화: [[Empirical Confirmation Before Claiming]]. 환경 배경은 [[YouTube Datacenter IP Login Wall]], 상위 프로젝트는 [[Discord Bots]].

## 핵심 엣지

<!-- 아직 엣지 없음 -->

## Sources

- discord_bots PR #9 후속 핫픽스 (2026-06-21), commits 49b8a2a/7b22ef1/d218a3e
