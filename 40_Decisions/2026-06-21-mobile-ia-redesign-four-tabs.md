---
id: concept_2026_06_21_mobile_ia_redesign_four_tabs
title: 2026-06-21-mobile-ia-redesign-four-tabs
aliases: []
type: decision
moc: "[[Development MOC]]"
tags: [mobile, information-architecture, ux, expo-router, project-mgmt]
status: draft
created: 2026-06-21
version: 1.0
node_id: 5ce962ce-8b94-42c2-85ac-f9157bb81327
---

# 2026-06-21-mobile-ia-redesign-four-tabs

모바일 앱 정보구조(IA)를 **하단 탭 4개(홈/관리/계획/실적)** 로 재편한 결정. 사용자 원성("ui 의도 모르겠다 / 탭마다 다 다르다")의 근본 원인이 비주얼·배포가 아니라 **상호작용/IA 계층** — 같은 `SegmentTabs` 위젯이 탭마다 다른 의미(필터 vs 페이지이동 vs 설정)를 가진 '가짜 일관성'이라는 진단에 따른 것. PR #19(`598eb06`)가 스킨만 통일하고 건너뛴 '두 번째 반'을 수행. JS-only 변경이라 **모바일 1.1.3 OTA**로 배포(런타임 1.1, PC와 x.y 호환 라인 정책 준수).

## 확정 구조
- **하단 탭 4개**: `홈 / 관리 / 계획 / 실적` (`ios-app/src/constants/tab-meta.ts`).
- **설정**: 홈 좌상단 톱니바퀴(⚙️) → `SettingsModal`(계정/로그아웃/버전). 라우터가 아닌 Modal — expo-router `unstable-native-tabs`의 비탭 라우트 불확실성 회피, 양 플랫폼 안전.
- **관리** `[수주 | 회사 | 인력]`: 마스터 데이터. 기존 standalone 수주 탭 + 회사 + 개발자(인력)를 `manage.tsx`로 통합. projects.tsx·developers.tsx 삭제.
- **계획** `[투입 | 정산 | 하도사]`: **조회 전용**(`plan.tsx`). 계획 행은 단가만 보유, 월별 MM은 `*_plan_month` 자식 테이블 → JS로 합산·기간 표시. 그리드 편집은 PC.
- **실적** `[투입 | 정산 | 하도사]`: 단건 입력(`record.tsx` = 구 plans.tsx + sub_settlement). 하도사 폼 세부는 추후 재검토(사용자 보류).
- **고객요구사항**: 홈 미리보기 + **전용 풀스크린 메모장**(`CustomerRequirementsModal`, 편집 가능). customer_requirement 7개 자유 텍스트 컬럼. "규격화된 메모장" — 제목(프로젝트)+본문(요청사항, 여러 줄)+상태(진도) 기본, 담당·인원·투입·비고는 '상세 항목' 접기. 프로젝트는 자유 텍스트(PC 동일). PC는 편집 안 함 → 모바일도 가능해야 한다는 사용자 요구 반영.

## 핵심 원리 — 세그먼트 의미 고정
계획·실적이 **동일한 `[투입|정산|하도사]`** 공유 → "세그먼트 = 투입/정산/하도사 전환" 한 가지 의미로 학습·전이. "탭마다 다 다르다"의 근본 해소. '관리'는 다른 도메인(마스터)이라 `[수주|회사|인력]` 별도 어휘 허용.

## 채운 기능 갭
PC 11탭 대비: 계획 3종(조회), 하도사비용정산등록, 고객요구사항(편집). 백엔드 테이블·시퀀스는 라이브 준비됨. "데이터 0건"은 이 갭이 아니라 스키마 드리프트가 원인이었음(→ [[Project Mgmt App]]).

## 상태
**구현 완료** — `npx tsc --noEmit`·`npx expo lint` 통과(2026-06-21). 변경: tab-meta·screen-header·form(multiline)·supabaseIds(customer_requirement 추가)·index 수정 + manage/plan/record/SettingsModal/CustomerRequirementsModal 신규, projects/developers/plans 삭제. 실기 구동 검증은 OTA 후 기기에서 필요. 네이밍 통일(#3)·하도사 폼 세부는 별도 트랙.

## 핵심 엣지

- `[[2026-06-21-mobile-ia-redesign-four-tabs]] extends [[Project Mgmt App]]` — PR #19가 건너뛴 상호작용/IA 재설계를 수행해 모바일 앱을 확장

## Sources

