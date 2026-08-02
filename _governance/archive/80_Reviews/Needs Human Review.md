---
type: review
status: open
created: 2026-06-18
reason: needs-human-review
moc: "[[Second Brain MOC]]"
related: []
---

# Needs Human Review

> 자동 판단이 불가능하여 **사람의 결정/확인**이 필요한 항목 큐입니다. 중복 개념 후보
> (`duplicate-concept-candidate`)와 결정 재고(`decision-needs-reconsideration`)도 여기로
> 모입니다. 카테고리·상태 정의는 [[Review Policy]].

## 항목 형식

```markdown
### [open] 한 줄 요약
- reason: needs-human-review | duplicate-concept-candidate | decision-needs-reconsideration
- created: YYYY-MM-DD
- detail: 무엇을, 왜 사람이 봐야 하는가
- related: [[관련 노트/결정]]
- resolution: (reviewed/resolved/rejected/superseded + 처리 내용)
```

## Open

### [open] 입자 토론 실측 "06_Raw 7 files"는 플레이스홀더 오계수 — 백필 대상 원본이 실재하는가
- reason: needs-human-review
- created: 2026-07-02
- detail: 2026-07-02 입자 토론 핸드오프의 실측 표는 06_Raw를 "7 files"로 보고했으나,
  검증 결과 `find 06_Raw -type f` = README.md 1 + `.gitkeep` 6 = **7 (실원본 0건)**.
  private 저장소의 전체 git 히스토리·원격 refs·워크트리 어디에도 raw 콘텐츠가 커밋된
  이력이 없다. 이에 따라 핸드오프 액션 1("06_Raw 7건 백필")은 대상 부재로 수행 불가 —
  대신 핸드오프 원문 자체를 첫 raw로 인입했다
  ([[Source Summary — 2026-07-02-granularity-debate-handoff]]). 같은 표의 다른 수치
  (Concepts ~60 · Decisions 19 · MOC 8)는 스팟체크로 일치.
  **사람 확인 필요**: 다른 기기(예: Windows 작업 머신)에 아직 커밋/동기화되지 않은
  raw 원본 7건이 실재하는지. 실재하면 이관 후 [[Ingest Policy]] 표준 절차로 백필.
- related: [[2026-07-02-node-granularity-split-vs-fold]], [[LLM Second Brain]]
- resolution:

### [open] gh-hint: GitHub 대량 색인의 ToS/AUP 적합성
- reason: needs-human-review
- created: 2026-07-16
- detail: [[gh-hint]] M1은 GitHub API로 MCP corpus(10³–10⁴ repo)의 메타데이터·README를
  수집·색인한다. GitHub ToS/AUP상 대량 수집·색인의 허용 범위(스크래핑 vs API 사용,
  연구/개인 도구 목적)를 사람이 확인해야 live 수집을 시작할 수 있다.
  잠정 정책: DATA_POLICY.md(문서화된 rate-limit 준수, 아카이브 데이터셋 우선).
- related: [[gh-hint]]
- resolution:

### [open] gh-hint: 제3자 README·코드 blob 원문 재배포 범위
- reason: needs-human-review
- created: 2026-07-16
- detail: 수집한 제3자 원문(README, 코드 발췌)을 공개 산출물·릴리스에 포함할 수 있는
  범위. 잠정 정책은 기본 비활성(prohibited, 파생 데이터만 재배포 —
  gh-hint repo DATA_POLICY.md). repo별 라이선스 기반 상향 기준을 사람이 승인해야 함.
- related: [[gh-hint]]
- resolution:

### [open] gh-hint: 공식 MCP Registry snapshot 이용·재배포 조건
- reason: needs-human-review
- created: 2026-07-16
- detail: Phase 0 corpus의 positive seed이자 baseline(B3)인 공식 MCP Registry의
  snapshot 이용 조건과 재배포 범위(우리 frozen snapshot에 Registry 항목
  metadata를 포함·공개해도 되는지, aggregator 정책과의 정합)를 사람이
  확인해야 한다. B3 baseline 구축(M1a.1) 전 필수.
- related: [[gh-hint]]
- resolution:

### [open] gh-hint: ecosyste.ms 최신 dump 접근성·라이선스·상업 이용조건
- reason: needs-human-review
- created: 2026-07-16
- detail: Phase 1(범용 census)의 부트스트랩 후보인 ecosyste.ms는 색인 규모 표기와
  공개 dump 최신성이 불일치(최신 full dump 2023 관측). 최신 export 접근성, 데이터
  라이선스, 상업 이용조건을 Phase 1 착수 전에 확인해야 한다. Phase 0(MCP 니치)에는
  불필요하므로 차단 요소는 아님.
- related: [[gh-hint]]
- resolution:

## Closed (reviewed / resolved / rejected / superseded)

_(없음)_

---

## Sources

- 큐 정의 근거: [[Review Policy]]
