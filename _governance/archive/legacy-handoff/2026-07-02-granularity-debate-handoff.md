# 📦 핸드오프 — llm-vault 노드 입자(split-vs-fold) 자율 결정 규칙

> 출처: 5단계 다자 구조화 토론(발상가/확장가/검토자/비판가) + 사후 정제 세션 + vault 실측 (2026-07-02)
> 목적: 이 문서만 읽고 실작업 에이전트가 AGENTS.md 갱신·백필·도구 구현을 진행할 수 있게 함

---

## 1. 풀려던 문제

에이전트가 노드 입자 결정(split-vs-fold)을 **사람에게 묻지 않고 자율로** 내리게 하는 규칙 코드화.
제약: (a) 쓰기 시점 LLM 실행 가능 (b) bloat와 under-capture 동시 회피 (c) AGENTS.md/00_System 탑재 (d) 단일 사용자 · 다중 에이전트.

---

## 2. 합의된 최종 규칙 (토론 수렴안)

### 2.1 쓰기 시점 — 3분기 결정

| 판정 | 조건 |
|---|---|
| **Split** | G1 ∧ G2 동시 충족 |
| **Fold + latent 표식** | G1·G2 중 하나만 충족 (표식 자격은 §2.2 전제조건 필요) |
| **Fold (표식 없음)** | 둘 다 미달, 또는 불확실 |

- **G1 — 연결성**: *증거 있고 강제 아닌* 9술어 엣지가 **구별되는 기존 노드**로 ≥1 실제 성립.
  기존 엣지 게이트 그대로 적용: evidence_quote(본문 실재 문장) 필수 · confidence≥0.7 · §6 안티패턴(utilizes 남용 등) 금지 · "불확실하면 비워라".
- **G2 — 독립 검토단위**: 부모 노트가 폐기되어도 단독으로 참/거짓 검토 가능.
  조작적 검사 형태 = **자기완결성**: 해당 span이 부모 문맥 참조 없이 읽히는가 (대명사·"위 방식"류 참조 없음, 전제는 span 내 명시 또는 위키링크로 명명).

### 2.2 Latent 표식 (fissure marker)

- **본문 인라인 텍스트 금지** (`[Latent: ...]`류 → 검색 오염 · 비공식 반노드 계층). **frontmatter YAML만** 허용:
  ```yaml
  latent_split_candidate:
    - reason: "..."          # 왜 후보인지
      parent: "..."           # 부모 노트
      evidence: "..."         # 쪼갠다면 세울 엣지의 증거 인용
      hit_count: 0
      promote_condition: "distinct-context retrieval ≥ 2"
  ```
- **자격 전제조건**: 해당 span이 자기완결적일 것. 미달이면 표식을 남기려는 그 시점에만 다듬는다 (별도 재작성 지시 아님 — 자격 요건에서 자연히 따라 나옴).
- 구현 선택지: `hit_count` 같은 가변 상태는 frontmatter 대신 DuckDB 테이블로 분리 권장 (md = 사람이 읽는 진실, 카운터 = DB 파생 상태).

### 2.3 승격 (latent → node)

- **트리거**: 읽기 시점 piggyback — `retrieve_knowledge`가 latent 표식이 있는 노트를 회수할 때 카운터 +1. **서로 다른 맥락에서 2회** 돌파 시 발화. (1회 회수 ≠ 재사용. 별도 스윕 데몬 없음 — 트래픽이 곧 트리거.)
- **신호 근거 (부트스트랩 해결)**: fold된 단락은 그래프 신호(피인용)는 0이지만 BM25+dense **텍스트 인덱스에는 살아 있음**. 활용 신호 2종:
  - span-편중 회수: 노트가 특정 묻힌 단락에만 매칭되는 질의로 반복 회수 → split 압력 (예측 아닌 로그 사실)
  - 부모-횡단 근접중복: 서로 다른 부모의 청크가 임베딩 근접 → 공유 개념 묻힘 신호 (승격보다는 감사/병합 큐용)
- **실행**: `promote_latent(target_id, evidence_quote, independent_review_condition)` — **원자적 MCP 도구**. 서버 트랜잭션 락 + 노드 유니크 제약 → 다중 에이전트 동시 호출 멱등 처리. 객관 판정 증거를 API 필수 인자로 강제.

### 2.4 Split 실행 방식 — extraction only (사후 확정)

- **Split = 반드시 extraction(적출)**: span을 새 노드로 이동, 부모엔 위키링크 한 줄만 남김.
- **Distillation(부모 본문 유지 + 별도 원자 노트 복제) 금지**. 이유: 두 살아있는 사본 → drift 내장. Raw/Source_Summaries 패턴이 안전한 건 06_Raw가 **불변**이라 한쪽이 절대 안 움직이기 때문 — 해석 계층엔 그 전제가 없음.
- 따라서 자동 승격은 **extraction-ready(자기완결) span만**. 재구성이 필요한 건 전부 review queue로. → 시스템 내 텍스트 자동 재작성 경로 0개, drift 원천 설계 봉쇄.
- 표식 없이 fold된 단락에 나중에 회수 신호가 잡히는 경우: 자동 경로 불가 → review queue.

### 2.5 작성 원칙 (규칙의 전제를 떠받침)

- **단락 자기완결성**: 자기완결의 단위 = 그 계층의 회수 원자. **20_Concepts = 문단, 40_Decisions = 노트 전체**(ADR식 맥락→대안→결정→귀결 서사 허용, 노트 경계에서만 자기완결).
- **위키링크 ≠ 9술어 엣지**: 링크는 무타입·무비용 참조 명시 수단(자유롭게), 엣지는 evidence 게이트되는 DB 시민(엄격하게). 링크는 자기완결성을 복제 없이 싸게 사는 수단 — "위 방식" 대신 `[[노드명]]`.

---

## 3. 끝내 미해결 (기록 필수 — 60_Open_Questions 후보)

1. **G1∧G2 통과 = 즉시 split인가, latent 자격까지만인가.**
   - 즉시 split파(발상가·검토자): 쓰기 에이전트가 가장 많은 의미 맥락 보유, 게이트는 이미 보정된 판단 재사용.
   - latent 자격파(비판가): confidence≥0.7 잔여 주관이 남는 한 게이트는 1차 필터, split은 회수 수요가 증명할 때.
   - **결론: 논리로 판가름 불가 — 실운영 오분류율 측정으로 결정할 경험 문제.** 초기값은 즉시-split로 시작하고 과분할률 모니터링 권장.
2. "서로 다른 맥락 2회"의 조작적 정의 (질의 임베딩 거리? 세션 구분? 호출 에이전트 구분?).
3. 그래프 확장 검색이 위키링크를 타는가 — 00_System 검색 정책에서 확인 필요 (§5 액션 4와 연동).

---

## 4. Vault 실측 결과 (2026-07-02, ltm-vault-private)

| 항목 | 값 | 함의 |
|---|---|---|
| 링크 수/파일 수 | MOC 229/8 · Concepts 591/60 · Projects 96/10 · Decisions 126/19 | 링크 문화 정착 (Concepts 평균 ~10/파일) |
| 헤딩 링크 `[[노트#섹션]]` | **0** | 인바운드 헤딩 링크 승격 보조 신호 **기각** — 승격은 텍스트 회수 신호 단독 |
| 06_Raw | **7 files** | ↓ |
| 50_Source_Summaries | **0 files** | **원본 계층 전체가 그래프에서 고립** — 최대 under-capture 구멍 |
| distinct-file 인바운드 | Staged Cognition 16 · Hallucination as Default 15 · arel_wars_2_render… **2** (원시 19링크는 2개 파일의 반복 언급) | 원시 링크 수 아닌 **distinct 소스 수**가 올바른 수요 프록시 |
| 철학 노드 링크 출처 | Software 2.0: Concepts 6 / Decisions **0** · Bitter Lesson: Concepts 5 / Decisions **0** | 결정-근거 추적(provenance) 아닌 개념 간 **vibe-linking 징후** |

---

## 5. 액션 아이템 (우선순위순)

1. **[긴급] 06_Raw 백필**: 7건 각각 → 50_Source_Summaries 요약 노트 생성 + 기존 20/40 노드로 엣지 연결. evidence_quote 게이트 못 넘는 애매한 엣지는 확정하지 말고 **review queue**로.
2. **AGENTS.md / 00_System 갱신**: §6 삽입 초안을 반영. 기존 "재사용될 때만 승격" 휴리스틱을 본 절차로 대체.
3. **`promote_latent` MCP 도구 구현**: 원자 트랜잭션 + 멱등 + 필수 인자(target_id, evidence_quote, independent_review_condition). hit counting은 `retrieve_knowledge` 경로에 piggyback. hit_count 저장 위치(frontmatter vs DuckDB) 결정.
4. **링크 위생**: (a) 00_System에서 그래프 확장이 위키링크를 타는지 확인. 탄다면 read-time 차수 역가중(`1/log(degree)`) 또는 MOC 타입 확장 제외 — 쓰기 규칙은 불변. (b) 작성 지침 1줄 추가: "철학 노드 링크는 그 원칙이 이 노트의 주장을 실제로 지지할 때만."
5. **미해결 쟁점 3건**을 60_Open_Questions에 등록 (§3).
6. (선택) 승격 규칙 가동 후 첫 실전 검증: distinct 인바운드 상위 노드들 대상 과분할/미분할 스팟체크.

---

## 6. AGENTS.md 삽입용 초안 (그대로 붙여넣기 가능)

```markdown
## 노드 입자 결정 (split-vs-fold) — 자율 절차. 사람에게 묻지 않는다.

새 내용을 기록할 때 span 단위로 판정한다:

**Split** ⟺ G1 ∧ G2:
- G1 연결성: 증거 있고 강제 아닌 9술어 엣지를 *구별되는 기존 노드*로 ≥1 실제로 세울 수 있다
  (evidence_quote 본문 실재 · confidence ≥ 0.7 · utilizes 남용 금지 · 불확실하면 비워라).
- G2 독립 검토단위: 부모가 폐기돼도 단독 참/거짓 검토 가능 = span이 자기완결적이다
  (부모 문맥 참조 없음, 전제는 span 내 명시 또는 [[위키링크]]로 명명).
- Split 실행은 extraction만: span을 새 노드로 이동, 부모엔 [[링크]] 한 줄. 본문 복제 금지.

**Fold + latent 표식** ⟺ G1·G2 중 하나만 충족, 그리고 span이 자기완결적일 때:
- frontmatter에만 기록: latent_split_candidate: {reason, parent, evidence, hit_count, promote_condition}
- 본문 인라인 표식 금지. span이 자기완결 미달이면 표식을 남기는 지금 다듬는다.

**Fold (표식 없음)** ⟺ 둘 다 미달 또는 불확실. 불확실하면 fold가 기본값이다.

**승격**: latent 표식은 서로 다른 맥락에서 2회 회수되면 promote_latent 도구로만 승격한다
(직접 파일을 쪼개지 않는다). 재구성이 필요하거나 애매하면 review queue로 보낸다.

## 작성 원칙 — 자기완결성
- 자기완결 단위: 20_Concepts는 문단, 40_Decisions는 노트 전체.
- "위 방식"류 문맥 의존 참조 대신 [[노드명]]으로 대상을 명시한다. 링크는 자유롭게 걸어도 된다.
- 위키링크는 참조 명시 수단이고, 9술어 엣지가 아니다. 지식 관계 주장은 엣지로만 한다.
- 철학/원칙 노드 링크는 그 원칙이 이 노트의 주장을 실제로 지지할 때만 건다.
```
