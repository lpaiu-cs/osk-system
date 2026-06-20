---
title: YouTube Datacenter IP Login Wall
aliases:
  - This video requires login
  - YouTube 로그인 월
  - datacenter IP login wall
type: Concept
moc: "[[Development MOC]]"
tags:
  - Development
  - Debugging
  - YouTube
status: evergreen
created: 2026-06-19
updated: 2026-06-19
node_id: null
related:
  - "[[Discord Bots]]"
---

> [!NOTE] 증류된 일반 지식 (코드/경로 디테일은 해당 레포 메모리에)

# YouTube Datacenter IP Login Wall

클라우드/VPS 같은 **데이터센터 IP**에서 Lavalink + youtube-source로 YouTube를 다룰 때, 일부 요청이 **playability(재생 가능 여부) 단계에서 `"This video requires login"`** 으로 막힌다. OAuth·서명(cipher)이 정상이어도 발생한다. 데이터센터 ASN이 YouTube에 흔히 차단되기 때문이다(youtube-source #107, 메인테이너 확인).

## 확정된 핵심: 영상 제한이 아니라 "경로" 문제

2026-06-19 통제 실험으로 확정(discord music-bot, Oracle IP, youtube-source 1.18.1):

- **검색(`ytsearch:`)→재생 경로는 정상 동작.** 음악 공식 MV 포함 대량 재생 성공.
- **직접 video-ID/watch-URL 로드(`routeFromVideoId`)는 음악 콘텐츠에서 광범위하게 실패** ("requires login").
- **같은 영상**이 검색-재생은 성공하고 직접-로드는 실패함(IU LILAC 공식 MV, Aimer Kataomoi MV로 확인). 비음악 영상(RickRoll) 직접-로드는 통과.

→ 따라서 이 "requires login"은 **연령·지역 제한 같은 영상 속성이 아니라**, 직접-URL 로드 경로가 데이터센터에서 거부되는 **경로/클라이언트 라우팅 문제**다. (초기 "연령제한 영상" 해석은 틀렸음 — 사용자가 incognito로 열림을 지적, 통제 실험으로 반증.)

### 확정된 메커니즘 (youtube-source 클라이언트 능력 매트릭스)

README의 per-client 표에서 **TV 클라이언트(=OAuth로 로그인월 뚫는 유일 클라이언트)의 "Metadata Support = None"** 이 결정적이다:
- **직접 video-ID 로드**(`routeFromVideoId`→`loadVideo`)는 *메타데이터* 단계라, 메타데이터 능력 있는 **비-OAuth 클라이언트(WEB·ANDROID_VR·WEBEMBEDDED)만** 시도 → 데이터센터서 로그인월. **TV+OAuth는 메타데이터 능력이 없어 이 단계에 구조적으로 안 낀다** → 재생 단계 가보지도 못하고 실패.
- **검색→재생**은 검색(playability 게이트 없음)으로 트랙을 얻고 *재생/format* 단계에서 **TV+OAuth가 참여**해 같은 영상도 통과.

이게 "같은 영상인데 URL은 실패, 검색은 성공"의 정확한 원인이다.

## 진단 구분 (여전히 유효)

- **playability 단계 실패**(`getPlayabilityStatus`, "requires login") ← 위 경로/IP 문제.
- **cipher·signature 단계 실패**(`scriptExtractionFailed`, signature) ← youtube-source/remoteCipher 문제. remoteCipher가 푸는 건 이쪽이며 **login wall과는 다른 층**이다.

## 알려진 우회

- **검색 경로로 우회(확정·실측)**: 직접 watch-URL을 ID 직접로드하지 말고, **oembed(인증 불필요, 데이터센터서 통과)로 제목을 얻어 `ytsearch`** 로 돌리면 재생 단계에서 TV+OAuth가 끼어 통과. 검색 결과에 동일 video id가 있으면 그걸 골라 정확도 유지. 정식 MV는 보통 정확 일치.
- **poToken + visitorData(미검증)**: README상 **WEB·WEBEMBEDDED에만** 적용. 직접-URL 메타데이터 로드 자체를 고칠 후보지만, 데이터센터 맥락 생성 필요·실증 안 됨. 검색 우회로 충분하면 불필요.
- **OAuth + TV**: 재생(format) 단계에서 로그인월 통과를 담당(메타데이터는 못 함).

## Open

- poToken을 데이터센터서 생성해 **직접-URL 로드 자체**를 고칠 수 있는지는 미검증(검색 우회로 실사용은 해결). → [[Implementation Questions]] 큐.

## Source

- 근거: youtube-source [#107](https://github.com/lavalink-devs/youtube-source/issues/107)·[#161](https://github.com/lavalink-devs/youtube-source/issues/161)·README(클라이언트/poToken); 2026-06-19 discord music-bot 라이브 통제 실험. 구체 코드/서버 디테일은 `discord_bots` 레포 메모리. 관련: [[Discord Bots]].
