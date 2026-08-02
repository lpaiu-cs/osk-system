---
type: open-questions
category: implementation
status: active
moc: "[[Development MOC]]"
created: 2026-06-18
updated: 2026-07-02
related:
  - "[[LLM Second Brain]]"
---

# Implementation Questions

> 구현/엔진/도구 관련 미해결 질문 큐입니다. 형식·라우팅은 [[Review Policy]] 참조.

## Open

### [open] retrieval 가중치(provisional prior)를 실측 튜닝
- created: 2026-06-18
- context: `00_System/Retrieval Policy.yaml`의 layer/confidence/status 가중치는 경험적
  최적값이 아니라 출발점(provisional prior)이다. `90_Engine/eval_retrieval.py`로
  지표를 측정해 조정한다.
- 1차 측정 (2026-06-18, bge-m3 dense+BM25, 55 nodes, 7쿼리): baseline Recall@5 0.857 >
  flat 0.762 > adversarial 0.0. review_leakage 0(전 설정). → **현재 가중치 유지**가 타당.
  상세: [[2026-06-18-layer-and-confidence-aware-retrieval]] §Tuning Log.
- 남은 todo: corpus가 작고 동질적이라 민감도 제한적. raw/summary/저신뢰 데이터가
  쌓이면 재튜닝(특히 raw_overexposure는 raw 콘텐츠가 있어야 의미). 도메인 쿼리로
  `eval_queries.sample.json` 확장 필요.
- related: [[2026-06-18-layer-and-confidence-aware-retrieval]], [[LLM Second Brain]]
- status: 1차 완료, corpus 확장 후 재튜닝 대기

### [open] 20_Concepts allowlist를 디렉터리 전체→개별 파일로 좁히기
- created: 2026-06-18
- context: `scripts/template-allowlist.txt`가 현재 `20_Concepts/`·`00_System/`을 디렉터리
  전체로 공개 허용한다. 공개 LLM 개념 코퍼스라면 안전하지만, **개인 연구 개념이나
  개인 운영 규칙이 이 폴더에 쌓이면 silent하게 공개로 새어나갈 수 있다.** 검토자(GPT)와
  Claude 모두 지적. Claude는 "나중"이 아니라 지금 좁히거나 최소한 unpinned 파일 경고를
  넣자는 입장(GPT보다 강경).
- todo: 사용자 결정 대기 — 20_Concepts를 (a) 공개 지식 전용으로 유지(개인 개념은
  30/40 등 다른 계층에), (b) 개별 파일 allowlist로 전환, (c) 디렉터리 허용 유지하되
  sync 시 unpinned 파일 경고. 결정 후 `template-allowlist.txt` 반영.
- related: [[LLM Second Brain]]

### [mostly-resolved] music-bot: YouTube 직접-URL 로드만 실패하는 메커니즘/대응
- created: 2026-06-19
- **메커니즘 확정**: youtube-source README 능력표상 **TV(OAuth) 클라이언트는 Metadata Support=None** → 직접 video-ID 로드(`routeFromVideoId`/`loadVideo`, 메타데이터 단계)엔 비-OAuth 클라이언트(WEB/ANDROID_VR/WEBEMBEDDED)만 참여 → 데이터센터서 로그인월. 검색→재생은 재생(format) 단계에서 TV+OAuth가 끼어 통과. ⇒ 영상 제한 아닌 **로드 경로** 문제. 통제 실험으로 확정(같은 영상 검색OK/직접FAIL). 상세: [[YouTube Datacenter IP Login Wall]].
- **대응 구현(확정)**: 봇 `_resolveQuery`에 oembed→ytsearch 폴백(동일 id 우선). 정식 MV는 정확 일치 resolve 실측. discord_bots 레포 메모리 참조.
- **남은 open**: poToken(WEB/WEBEMBEDDED 전용)을 데이터센터서 생성해 **직접-URL 로드 자체**를 고칠 수 있는지 미검증(검색 우회로 실사용은 해결돼 우선순위 낮음).
- related: [[Discord Bots]], [[YouTube Datacenter IP Login Wall]]

### [open] 입자 규칙: G1∧G2 통과를 즉시 split할 것인가, latent 자격까지만 인정할 것인가
- created: 2026-07-02
- context: 즉시-split파(쓰기 에이전트가 가장 많은 의미 맥락을 보유, 게이트는 이미
  보정된 판단의 재사용) vs latent-자격파(confidence≥0.7의 잔여 주관이 남는 한 게이트는
  1차 필터일 뿐, split은 회수 수요가 증명할 때). 토론 결론: **논리로 판가름 불가 —
  실운영 오분류율 측정으로 결정할 경험 문제.** 초기 운영값은 **즉시-split** +
  과분할률 모니터링 ([[2026-07-02-node-granularity-split-vs-fold]]).
- 측정 아이디어: 승격 규칙 가동 후 distinct-file 인바운드 상위 노드 대상
  과분할/미분할 스팟체크(핸드오프 §5 액션 6).
- related: [[2026-07-02-node-granularity-split-vs-fold]], [[Granularity Policy]]

### [open] latent 승격 트리거 "서로 다른 맥락 2회"의 조작적 정의
- created: 2026-07-02
- context: 후보 정의들 — 질의 임베딩 거리, 세션 구분, 호출 에이전트 구분. v1 구현은
  **정규화 쿼리 해시의 구별**(+ 쿼리와 후보 evidence/reason의 어휘 겹침 게이트로
  span-편중 회수를 근사)로 시작한다(90_Engine latent hit tracking). 카운터는 DuckDB
  캐시에 있어 캐시 재생성 시 초기화됨 — "승격이 다시 수요를 증명해야 한다"로
  해석하고 허용. 실운영에서 오발화/미발화가 관찰되면 정의를 교체한다.
- related: [[2026-07-02-node-granularity-split-vs-fold]], [[Granularity Policy]]

## Resolved

### [resolved] 그래프 확장 검색이 위키링크를 타는가
- created: 2026-07-02
- context: 위키링크가 검색 그래프 확장에 쓰인다면 링크 다작성 문화가 검색을 오염시켜
  read-time 차수 역가중(1/log(degree))·MOC 타입 확장 제외 같은 보정이 필요했다
  (입자 토론 핸드오프 §3 쟁점 3 · §5 액션 4).
- answer (2026-07-02): **타지 않는다.** `retriever.py`의 `adaptive_hop_expansion`은
  DuckDB `edges` 테이블(9술어)만 순회하며, 위키링크는 DB에 저장되지 않는다
  (인덱서가 title/alias 해석에만 사용). 노드 차수(hub/authority)도 엣지 기준.
  → read-time 보정 불필요, "링크는 자유롭게" 쓰기 규칙 유지. 링크 텍스트는 BM25
  본문 매칭으로만 검색에 기여한다.
- related: [[2026-07-02-node-granularity-split-vs-fold]], [[Granularity Policy]]

### [resolved] 06_Raw 하위 폴더별 임베딩 정책
- created: 2026-06-18
- context: raw 전체를 임베딩하면 비용·노이즈가 커진다. 하위 폴더별 차등 필요.
- answer (2026-06-18): `indexer.py`의 `RAW_SUBFOLDER_EMBED` 상수로 하위 폴더별 embed
  차등(`policy_for`가 06_Raw에서 적용). chats/papers/project-logs/**admin-records**=embed
  true, code-logs/screenshots=false, 미분류=false. **06_Raw 전체는 index=true(BM25 유지)·
  parse_edges=false·graph_node=false 불변** — embed=false는 dense만 끄는 것이지 검색에서
  빠지는 게 아님. 검증: `policy_for` 단위 테스트로 전 케이스 확인.
  - **admin-records=embed true는 의식적 선택**: 행정/건강/복무/증빙/상담 기록은 민감하지만
    그렇기에 "나"를 잘 반영하는 중요한 기억이라 private 안에서 의미검색되게 둔다. 보안상
    안전하다는 뜻이 아니라 **private-only 운영 전제**(유출 방지는 public/private 분리·sync
    guard 담당). public 템플릿에는 실제 raw가 절대 포함되지 않음.
- 문서: `00_System/Retrieval Policy.yaml` raw_policy.subfolders(미러), indexer.py 주석.
- related: [[2026-06-18-layer-and-confidence-aware-retrieval]]

### [resolved] strawberry류 쿼리 콘텐츠 갭 (검색 미스)
- created: 2026-06-18
- context: "왜 LLM은 strawberry의 r 개수를 못 세나?"가 BPE/Tokenizer/Glitch Tokens를
  못 끌어온다. 해당 원자 노트에 strawberry 예시 텍스트가 없고 설명이 MOC에만 있어
  dense/BM25 모두 약하게 매칭. **가중치가 아니라 콘텐츠 문제.**
- answer (2026-06-18): `[[Byte Pair Encoding]]`·`[[Tokenizer]]`에 strawberry 글자 세기
  사례를 사실 기반(과장 금지)으로 추가. 재인덱싱+eval 결과 strawberry 쿼리 recall 0→1.0,
  전체 MRR@5 0.857→1.0 / Recall@5 0.857→0.952. 상세:
  [[2026-06-18-layer-and-confidence-aware-retrieval]] §Tuning Log 2차.
- related: [[Byte Pair Encoding]], [[Tokenizer]], [[Glitch Tokens]]

### [resolved] 폴더별 인덱싱 정책(per-folder index policy)을 어떻게 둘 것인가?
- created: 2026-06-18
- context: 초기 구현은 `05_Inbox/`·`06_Raw/`를 디렉터리 이름으로 단순 제외만 했다.
  해석 계층(`30`/`40`/`50`/`60`/`70`/`80`)은 모두 node+edge로 동일 취급되었다.
- answer (2026-06-18): `LAYER_POLICY`/`policy_for()` 도입.
  `05_Inbox` 제외 / `06_Raw` **전문검색 전용**(node+embed, edge·링크 타깃 제외) /
  그 외 해석 계층 node+edge 유지. 검색 랭킹은 계층 가중치로 차등.
  결정: [[2026-06-18-layer-and-confidence-aware-retrieval]].
- related: [[LLM Second Brain]], [[Ontology Specification]]

### [resolved] 검토 큐(`80_Reviews/`)·결정 status를 retrieval에 반영할 것인가?
- created: 2026-06-18
- context: 낮은 신뢰도/환각 의심 항목이 일반 지식과 동급으로 검색되면 오염 위험.
- answer (2026-06-18): confidence/status 인지 검색 도입. 낮은 신뢰도·폐기 상태는
  **강등 + 표기**(숨기지 않음). `60/70/80`은 기본 검색 제외(`include_reviews=True`로
  포함). 검토 큐 위생용 `review_queue()` MCP 도구 추가.
  결정: [[2026-06-18-layer-and-confidence-aware-retrieval]].
- related: [[Review Policy]], [[LLM Second Brain]]

---

## Sources

- 큐 정의 근거: [[Second Brain Operating Model]]
- 엔진 변경 맥락: [[2026-06-18-second-brain-architecture]]
