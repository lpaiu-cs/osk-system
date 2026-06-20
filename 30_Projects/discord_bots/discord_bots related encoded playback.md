---
id: project_discord_related_encoded_playback
title: discord_bots related encoded playback
aliases:
  - 디스코드 뮤직봇 /related encoded 재생
  - discord music related
type: Project-Note
moc: null
tags:
  - Project/discord_bots
  - Engineering-Log
status: evergreen
created: 2026-06-20
updated: 2026-06-20
version: 1.0
node_id: 0a9b8c7d-6e5f-4a3b-9c8d-7e6f5a4b3c2d
embedding_model: null
embedding_hash: null
last_indexed: null
predicate_count: 0
related_nodes:
  - "[[Resolved-Object Direct Execution]]"
  - "[[Tool Use]]"
---

# discord_bots related encoded playback

디스코드 뮤직봇(`bots/music`)의 `/related`(YouTube Mix 연관곡 추천) 구현 교훈. **데이터센터 IP에서 YouTube 재생이 막히는 환경**에서 Mix 추천을 큐에 넣는 올바른 방법.

## 문제

처음엔 Mix 후보의 **제목을 파싱해 "아티스트 제목" 검색어를 재조립 → 재검색**했다. 큐레이션 채널(채널명=author, 제목=이모지·`[가사]`·콜론 수식어 범벅) 포맷마다 깨지는 정규식 두더지잡기가 됐고, mix 24곡 중 15곡만 매칭됐다.

## 해결

- Lavalink로 `playlist?list=RD<videoId>` 믹스를 resolve하면 **이미 재생 가능한 encoded 트랙**을 받는다. 이 트랙 객체를 `audioManager.enqueue(track)` 로 **그대로 재생**한다(제목 파싱 0).
- 로그인벽은 watch-URL **load** 단계(WEB 클라이언트)에만 걸린다. resolve로 받은 encoded는 load를 통과한 상태라, 재생은 OAuth/TV + remoteCipher 경로를 타 정상 동작한다.
- 검증(2026-06-20): 동일 입력에서 mix 15→24곡, 전부 실제 재생. encoded 가 없는 DB 폴백 경로에서만 최소 검색을 쓴다.

## 일반 원칙

이 사례는 [[Resolved-Object Direct Execution]] 의 구체적 구현이다 — 이미 resolve된 실행 가능한 객체를 문자열로 되돌려 재질의하지 말 것.

## Sources

- discord_bots PR #9 (2026-06-20), commit `4e09ac3`. 관련 개념: [[Tool Use]]
