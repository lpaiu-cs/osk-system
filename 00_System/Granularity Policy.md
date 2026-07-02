---
id: system_granularity_policy
title: Granularity Policy
aliases:
  - 입자 정책
  - 노드 입자 결정
  - Split-vs-Fold Policy
type: System-Specification
moc: "[[Second Brain MOC]]"
parent_moc: "[[Second Brain MOC]]"
tags:
  - System
  - SecondBrain
  - Granularity
status: evergreen
created: 2026-07-02
updated: 2026-07-02
version: 1.0
related:
  - "[[Ingest Policy]]"
  - "[[Ontology Specification]]"
  - "[[Review Policy]]"
  - "[[Naming Convention]]"
---

> [!IMPORTANT] 목적
> 에이전트가 노드 입자(granularity) 결정 — 어떤 내용을 **독립 노드로 쪼갤지(split)**,
> 어떤 내용을 **기존 노트 안에 접어둘지(fold)** — 를 **사람에게 묻지 않고 자율로**
> 내리기 위한 절차를 규정합니다. 목표는 bloat(과분할)와 under-capture(과병합)를
> 동시에 회피하는 것입니다. 기존의 막연한 "재사용될 때만 승격" 휴리스틱
> ([[Ingest Policy]] §2 구판)을 본 절차가 대체합니다.

---

## §1. 쓰기 시점 — 3분기 결정 (Write-time Decision)

새 내용을 기록할 때 **span 단위**(문단 또는 자기완결적 텍스트 블록)로 판정한다.

| 판정 | 조건 |
|------|------|
| **Split** | G1 ∧ G2 동시 충족 |
| **Fold + latent 표식** | G1·G2 중 하나만 충족, 그리고 span이 자기완결적일 때 |
| **Fold (표식 없음)** | 둘 다 미달, 또는 불확실 |

**불확실하면 fold가 기본값이다.**

### G1 — 연결성 (Connectivity)

*증거 있고 강제 아닌* 9술어 엣지가 **구별되는 기존 노드**로 ≥1 실제 성립한다.
기존 엣지 게이트([[Ontology Specification]])를 그대로 적용한다:

- `evidence_quote`: 본문에 실재하는 문장 인용 (§5.2)
- `confidence ≥ 0.7` (§5.2)
- `utilizes` 남용 등 안티패턴 금지 (§6)
- 모호하면 엣지를 선언하지 않는다 — "불확실하면 비워라" (§4)

게이트를 통과하는 엣지를 세울 수 **있는지**가 기준이며, 억지로 엣지를 만들어
G1을 통과시키는 것은 그 자체가 §6 안티패턴이다.

### G2 — 독립 검토단위 (Independent Reviewability)

부모 노트가 폐기되어도 해당 span을 **단독으로 참/거짓 검토**할 수 있다.
조작적 검사는 **자기완결성**이다: span이 부모 문맥 참조 없이 읽히는가.

- 대명사·"위 방식"·"이 접근"류 문맥 의존 참조가 없다.
- 전제는 span 내부에 명시되거나 `[[위키링크]]`로 명명되어 있다.

### 판정 예시

- 디버깅 로그 요약 안의 한 단락이 일반화 가능한 설계 원칙을 담고 있고, 그 원칙이
  기존 개념 노드와 `contradicts` 엣지(증거 인용 가능)로 연결된다 → **G1 ∧ G2 → Split**.
- 같은 원칙이지만 아직 어떤 기존 노드와도 게이트 통과 엣지가 성립하지 않는다
  → G2만 충족 → **Fold + latent 표식**.
- 프로젝트 진행 상황 서술처럼 부모 맥락 없이는 의미가 없는 내용 → **Fold**.

---

## §2. Latent 표식 (Fissure Marker)

split 자격이 절반만 충족된 span은 부모 노트 frontmatter에 **분할 후보**로 표식한다.

### §2.1 형식 — frontmatter YAML만 허용

```yaml
latent_split_candidate:
  - id: staged-cognition-gating        # 후보 슬러그 (파일 내 유일)
    candidate_title: "Gating Threshold Reuse"   # 승격 시 새 노드 제목(제안)
    reason: "G2 충족·G1 미충족 — 독립 검토 가능하나 게이트 통과 엣지 부재"
    evidence: "쪼갠다면 세울 엣지의 근거가 될, 본문에 실재하는 인용 문장"
    promote_condition: "distinct-context retrieval >= 2"
```

- **본문 인라인 표식은 금지한다** (`[Latent: ...]`류). 인라인 표식은 전문검색을
  오염시키고 비공식 반(半)노드 계층을 만든다.
- **위 블록 스타일만 지원한다.** YAML 인라인 flow 맵(`latent_split_candidate: [{...}]`)은
  엔진의 미니 파서가 읽지 못한다(`Retrieval Policy.yaml`과 동일한 블록-전용 제약) —
  엔진이 감지 시 경고를 내고 해당 표식을 무시한다.
- `evidence`는 해당 span 본문에 실재하는 문장이어야 한다. 승격 시 이 인용으로
  span을 기계적으로 위치 특정한다(§3.3).
- **`hit_count`는 frontmatter에 두지 않는다.** 회수 카운터는 가변 런타임 상태이므로
  엔진 DB(`latent_hits` 테이블)가 관리한다 — Markdown은 사람이 읽는 선언,
  카운터는 DB 파생 상태. (캐시 재생성 시 카운터가 초기화될 수 있으며, 이는
  "승격이 다시 수요를 증명해야 한다"는 뜻이므로 허용 가능한 손실이다.)

### §2.2 자격 전제조건

표식 대상 span은 **자기완결적이어야 한다**(G2 검사와 동일). 미달이면 표식을
남기려는 **그 시점에** span을 다듬어 자기완결로 만든다. 이는 별도의 재작성
지시가 아니라 표식 자격 요건에서 자연히 따라 나오는 것이다.

---

## §3. 승격 (Promotion: latent → node)

### §3.1 트리거 — 읽기 시점 piggyback

- `retrieve_knowledge`가 latent 표식이 있는 노트를 회수하면 엔진이 후보별 hit을
  기록한다(쿼리가 후보의 evidence/reason과 어휘 겹침이 있을 때만 — span-편중
  회수의 근사).
- **서로 다른 맥락에서 2회** 회수되면 승격 조건 발화 — 응답에 승격 안내가
  포함된다. 1회 회수 ≠ 재사용이다. 별도 스윕 데몬은 없다 — **트래픽이 곧
  트리거**다.
- "서로 다른 맥락"의 v1 조작적 정의는 *정규화된 쿼리 해시의 구별*이다. 이 정의는
  잠정적이며 미해결 쟁점이다(§6, [[Implementation Questions]]).

신호가 존재하는 근거: fold된 단락은 그래프 신호(피인용)는 0이지만 BM25+dense
**텍스트 인덱스에는 살아 있다**. 활용 신호는 예측이 아니라 로그 사실이다.

### §3.2 실행 — `promote_latent` 도구로만

승격은 MCP 도구 `promote_latent(parent_title, candidate_id, evidence_quote,
independent_review_condition, new_title=None)`로만 수행한다. **에이전트가 직접
파일을 쪼개지 않는다.**

- 서버(데몬)의 쓰기 락 + 노드 유니크 제약으로 **원자적·멱등** 처리된다.
  다중 에이전트가 동시에 호출해도 한 번만 실행된다.
- `evidence_quote`(부모 본문 실재 문장)와 `independent_review_condition`(G2를
  어떻게 확인했는지)은 **필수 인자**다 — 객관 판정 증거를 API가 강제한다.

### §3.3 Split 실행 방식 — extraction only

- **Split = 반드시 extraction(적출)이다.** span을 새 노드로 **이동**하고, 부모에는
  `[[새 노드]]` 위키링크 한 줄만 남긴다.
- **Distillation(부모 본문 유지 + 별도 원자 노트 복제)은 금지한다.** 살아있는
  사본 두 개는 drift를 내장한다. `06_Raw` ↔ `50_Source_Summaries` 패턴이 안전한
  것은 `06_Raw`가 **불변**이라 한쪽이 절대 움직이지 않기 때문이며, 해석 계층에는
  그 전제가 없다.
- 적출되는 span은 **후보 마커에 기록된 `evidence`를 포함해야 한다** — 호출 인자
  `evidence_quote`는 span을 *위치 특정*하고, 마커의 `evidence`는 그 span이 정말
  표식된 그 span인지 *검증*한다. 불일치는 자동 실행 없이 review로 회송된다.
- 따라서 자동 승격은 **extraction-ready(자기완결) span만** 대상으로 한다.
  재구성이 필요한 span은 전부 `80_Reviews/` 큐로 보낸다([[Review Policy]]).
  → 시스템 안에 텍스트 자동 재작성 경로는 0개이며, drift가 설계 차원에서
  봉쇄된다.
- 표식 없이 fold된 단락에 나중에 회수 신호가 잡히는 경우: 자동 경로가 없으므로
  review queue로 보낸다.

---

## §4. 작성 원칙 — 자기완결성 (Self-Containment)

본 절차의 전제를 떠받치는 작성 규율.

- **자기완결의 단위 = 그 계층의 회수 원자.**

| 계층 | 자기완결 단위 |
|------|---------------|
| `20_Concepts/` | **문단** — 각 문단이 부모 문맥 없이 읽혀야 한다 |
| `40_Decisions/` | **노트 전체** — ADR식 맥락→대안→결정→귀결 서사 허용, 노트 경계에서만 자기완결 |

- "위 방식"류 문맥 의존 참조 대신 `[[노드명]]`으로 대상을 명시한다. 위키링크는
  자기완결성을 **복제 없이 싸게 사는 수단**이다.
- **위키링크 ≠ 9술어 엣지.** 링크는 무타입·무비용 참조 명시 수단이고(자유롭게),
  엣지는 evidence 게이트되는 DB 시민이다(엄격하게). 지식 관계 주장은 엣지로만
  한다. (엔진 사실: 그래프 확장은 엣지만 탐색하며 위키링크는 DB에 저장되지
  않는다 — 링크를 많이 걸어도 검색 그래프는 오염되지 않는다.)
- **철학/원칙 노드 링크는 그 원칙이 이 노트의 주장을 실제로 지지할 때만 건다.**
  (vibe-linking 방지 — 2026-07-02 실측에서 철학 노드 링크가 결정 기록 아닌
  개념 노트에서만 나오는 징후가 확인됨.)

---

## §5. 신호와 감사 (Signals & Audit)

- **distinct 소스 수가 올바른 수요 프록시다.** 원시 링크 수는 한 파일의 반복
  언급으로 부풀 수 있다(2026-07-02 실측: 원시 19링크가 실제로는 2개 파일의
  반복 언급인 사례).
- **부모-횡단 근접중복**(서로 다른 부모의 청크가 임베딩 근접)은 공유 개념이
  묻혀 있다는 신호다. 이는 자동 승격이 아니라 **감사/병합 큐**(`80_Reviews/`)
  용도로 쓴다.
- 인바운드 헤딩 링크(`[[노트#섹션]]`)는 승격 보조 신호로 **기각**되었다
  (2026-07-02 실측 0건). 승격은 텍스트 회수 신호 단독으로 판정한다.

---

## §6. 미해결 쟁점

다음은 논리로 판가름 나지 않아 실운영 측정으로 결정할 경험 문제다.
등록: `60_Open_Questions/Implementation Questions.md`.

1. **G1∧G2 통과 = 즉시 split인가, latent 자격까지만인가.** 초기 운영값은
   **즉시-split**로 시작하고 과분할률을 모니터링한다.
2. **"서로 다른 맥락 2회"의 조작적 정의.** v1은 정규화 쿼리 해시 구별(§3.1).
   세션/에이전트 구별, 쿼리 임베딩 거리 등 대안은 측정 후 결정.

(구 쟁점 3 — "그래프 확장이 위키링크를 타는가" — 는 2026-07-02 엔진 코드
확인으로 **해결**: 확장은 9술어 엣지만 탐색한다. read-time 차수 역가중은 불필요.)

---

## 변경 이력

| 버전 | 일자 | 변경 내용 |
|------|------|-----------|
| 1.0 | 2026-07-02 | 최초 제정. 5단계 다자 구조화 토론 + vault 실측에서 도출. |

---

## Sources

- 설계 근거(결정 기록): [[2026-07-02-node-granularity-split-vs-fold]]
- 토론·실측 원본: `06_Raw/project-logs/2026-07-02-granularity-debate-handoff.md`
- 관련 정책: [[Ingest Policy]], [[Ontology Specification]], [[Review Policy]]

> 결정 기록과 raw 원본은 **private 인스턴스**의 지식 계층에 있다. 이 저장소는 지식
> 계층이 비어 있는 프레임워크 템플릿이므로 위 두 참조는 여기서는 의도적 dangling이며,
> 기존 정책들이 [[2026-06-18-second-brain-architecture]]를 인용하는 방식과 같다.
