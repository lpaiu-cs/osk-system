---
id: concept_project_mgmt_app
title: Project Mgmt App
aliases: [project-mgmt, 프로젝트 투입정산 관리, 투입정산 관리앱]
type: project
moc:
tags: [electron, expo, react-native, supabase, sync, korean-business-app]
status: draft
created: 2026-06-21
version: 1.0
node_id: 3c390f25-5d7e-4cdf-b76d-a07643079175
---

# Project Mgmt App

프로젝트 투입·정산을 관리하는 1인용 업무 앱. **Electron 데스크톱(주력)** 과 **Expo/React Native 모바일(보조)** 이 공유 **Supabase** 백엔드에 동기화한다. 데스크톱은 로컬 SQLite를 SSOT로 두고 `id_map` 기반 양방향 동기화하며, 모바일은 "이동 중 조회 + 단건 실적 입력" 보조 역할이다. 버전은 PC와 모바일이 **x.y 호환 라인을 공유**하고 z(patch)는 플랫폼별 독립 카운터다(`db/app_compat.sql`이 SSOT).

## 현황 스냅샷 (2026-06-21)
- **데스크톱 v1.1.20 배포 완료** — 입력 포커스 desync 수정, 자동 업데이트 차등 다운로드 비활성화(간헐 재설치 실패 완화), 업데이트 패치노트 뷰어(1.1.19 도입). 인프라: electron-updater + Cloudflare R2(`publish: generic`), `v*` 태그 → release.yml.
- **모바일 v1.1.2 OTA 완료** — EAS Update, runtimeVersion `"1.1"` 고정. PostgREST 임베드 조인 제거→JS 룩업 조인(견고성), 폼 입력 UX(KeyboardAvoidingView·인라인 에러·터치타깃), 스코프 온보딩, YmField/DateField carry-over 수정.

## "데이터 0건" 근본 원인 — 해소됨
증상의 진짜 원인은 기능 갭이 아니라 **라이브 Supabase 스키마 드리프트**였다: `project.settle_category` 컬럼 + `*_record_id_seq` 시퀀스 11개가 운영 DB에 누락(선언 스키마엔 존재). `project`는 모든 테이블의 FK 부모라 그 업로드 실패가 `next_user_record_id` RPC를 막아 **업로드 큐 전체가 잠김** → 0건. SQL 마이그레이션 2건(`db/supabase_migration_project_settle_category_20260620.sql`, `db/supabase_migration_record_id_sequences_20260620.sql`)으로 교정, 데이터 흐름 확인.

## 호환 토대 (마음 놓고 기능 추가 가능한 이유)
- **x.y 호환 라인 정책**(`app_compat.sql`, 커밋 `d22995a`): 같은 x.y면 데이터 구조 호환 보장 → 모바일이 PC 탭 일부를 안 가져도(기능 갭) 동기화가 안 깨진다. 빠진 탭은 "안 쓰는 테이블"일 뿐.
- 모바일 누락 기능 테이블(`deployment_plan`·`settlement_plan`·`sub_settlement_plan`·`sub_settlement`·`customer_requirement`)은 **라이브에 이미 존재하고 시퀀스도 준비됨** → UI만 붙이면 됨.

## 진행 중 / 로드맵
- **진행 중: 모바일 IA 재설계** → [[2026-06-21-mobile-ia-redesign-four-tabs]]. 근본 원인은 비주얼·배포 아닌 상호작용/IA 계층.
- 잔여: #3 네이밍/용어 통일(PC 용어가 길어 분리), #4 디자인 시스템 단일화+다크모드, 인프라/CI 자동화(모바일 빌드·OTA가 전부 수동), web.output `static`→`single`(web export SSR 크래시 제거).

## 반복 버그 영역
정산/MM 정밀도(절사·중복합산), task_id, 동기화(LWW·금액 0 덮어쓰기·FK 순서).

## 핵심 엣지

<!-- 아직 엣지 없음 -->

## Sources

