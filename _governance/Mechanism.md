---
id: 260802-114u-iter
author: user
drafter: agent:fable-5
created_at: 2026-08-02 13:22 (KST)
revised_at: 2026-08-02 15:00 (KST)
summary: "물리 최소 사양 — 배치 선언표, id·rid 형식, 대장 규약, 위임 절, 링크 문법, 비밀값 필터"
supported-by: "[[2026-07-28-space-structure-deliberation-record]]"
replaces: "[[PLAN-2026-07-25]]"
---


# osk-system Mechanism — 최소 사양

> 헌법 제7조 6~7항이 맡긴 범위에서 물리 형식과 계산 방법을 정한다. 이
> 문서는 부트스트랩에 필요한 최소만 담으며, 세부는 개정으로 자란다. 용어는
> 헌법·시행령의 정의를 따른다. (노드 계약 필드는 부트스트랩 단계에서 통치
> 문서 4종에 일괄 부여한다.)

## §1 물리 배치

1. 저장소는 사설 repo 하나로 자기 완결한다. 공개 repo는 system이 참조하지
   않는 발행 미러이며, 발행 범위는 allowlist가 정한다.
2. 경로와 소속:

   | 경로 | 소속 | 내용 |
   |---|---|---|
   | `Scope/<이름>/` | Scope Space의 scope | 노드 + `_raw/` |
   | `Scope/Workbench/` | Workbench scope (헌법 4조 5항) | 아래 3항 |
   | `Domain/<이름>/` | Domain Space의 Domain | 노드 |
   | `Person/<Facet>/` | Person Space의 Facet | 노드 |
   | `Person/Governance/` | 통치 Facet (pin 고정) | 통치 문서 + `records/`·`archive/` |
   | `_sources/` | 공용 원자료 구획 | 비노드 (이미지·pdf 등) |
   | `_engine/` | 엔진 | 코드·색인·캐시·동기화 도구 |

3. Workbench scope의 내부: 루트의 파일은 작업 상태(비노드)다. 노드는
   `transit/`(경유 노드)에만 둔다. `_ledger/`는 대장 구획으로
   `signatures.jsonl`(서명 기록부)·`case/`(사건부)·`migration/`(이행 기록)·
   `pins.jsonl`(pin 기록)을 담는다. `_raw/`는 운영 세션 기록이다.
4. **`_` 규칙**: 밑줄 접두 구획에는 노드를 두지 않는다. 노드 군집은 2항의
   표에 선언된 무접두 경로(`Scope/`·`Domain/`·`Person/`과 그 하위 군집,
   `transit/`)뿐이다. 그 밖의 루트 디렉토리는 엔진·저장소 지원 구획이며
   노드를 두지 않는다.
5. 콜드 티어는 규칙만 둔다(시행령 §2 6항) — 물리 구획은 필요해질 때 이 절의
   개정으로 신설한다.

## §2 노드 식별과 시간

1. `id`는 생성 시 `YYMMDD-ssss-rrrr`로 자동 부여한다 — 생성일 6자 +
   자정 기준 경과 초를 base36으로 인코딩한 4자 + 소문자 base36 무작위 4자
   (예: `260802-e3k1-k7f2`). 부여 시 기존 `id` 전수와 대조하여 중복이면
   무작위부를 재생성한다 — 유일성의 담보는 길이가 아니라 이 검증이다.
2. `created_at`·`revised_at`은 `YYYY-MM-DD hh:mm (KST)` 표기로 분 단위까지
   쓴다(예: `2026-08-02 15:30 (KST)`). 대장 기록의 `at`은 ISO 8601을
   유지한다.
3. 파일명은 제목이며 개명·이동할 수 있다. 동일성의 정본은 `id`다.
4. `author`·`drafter`의 agent 표기는 하네스명이 아니라 모델명으로 한다
   (예: `agent:fable-5`, `agent:gpt-5.6-sol`). 모델 미상의 이관 노드는
   `agent`로 표기한다. `drafter`는 대표 기초자 하나만 적는다.
5. frontmatter의 필드 순서는 `id`·`author`·`drafter`·`created_at`·
   `revised_at`·`summary`·Predicate Edge(상호 순서 무관)로 쓴다.

## §3 서명 기록부 (`_ledger/signatures.jsonl`)

1. 한 줄이 한 기록인 JSON Lines. 기록은 추가만 하며 수정·삭제하지 않는다.
   기기 안에서 추가는 단일 writer(파일 lock)로 직렬화하고, 한 행 단위로
   원자 append한 뒤 fsync한다. 부분 행은 손상으로 보고 수동 복구 절차로
   넘긴다. 기록 식별자 `rid`는 UUID 버전 7로 부여한다 — 다기기 병렬 기록의
   병합은 합집합 후 `rid`의 시간순 정렬로 하며, 이것이 대장의 정본 순서다.
   이 규율은 모든 `_ledger/` jsonl 대장에 공통이다.
2. 기록 형식:
   `{"rid": "<UUIDv7>", "kind": "sign"|"unsign"|"restore",
   "node": "<id>", "path": "<참고 경로>", "hash": "sha256:<hex>",
   "at": "<ISO 8601>", "reason": "<선택>", "case": "<선택 — 사건번호>"}`
3. `hash`의 의미 — `sign`: 사용자가 확인한 상태의 해시. `unsign`: 해제되는
   서명의 해시(그 `sign` 기록과 일치해야 한다). `restore`: 회복되는 서명의
   해시(입건 직전 유효 서명과 일치해야 하며 `case`가 필수다).
4. 해시 대상은 경로를 제외한 노드 파일의 정확한 바이트 상태다. `path`는
   조회 편의를 위한 참고 값이며 서명의 대상이 아니다.
5. 서명 상태 판정: 그 `node`의 최신 기록(`rid` 시간순의 마지막)이 `sign`
   또는 `restore`이고 현재 파일의 sha256이 그 `hash`와 일치하면 유효, 그
   밖에는 미서명이다.
6. 기각 회복의 순서: ①회복 3조건(시행령 §9 4항)을 검증하고 → ②`restore`
   기록을 먼저 append·fsync한 뒤 → ③노드 파일을 원자 교체(atomic rename)로
   복원한다. 어느 단계에서 실패해도 해시 불일치로 미서명에 머문다 — 실패는
   언제나 미서명 쪽으로 남는다.
7. 수동 복구: 이 파일은 엔진 없이 판독 가능해야 한다. 검증은
   `shasum -a 256 <파일>`과 마지막 해당 `node` 행의 대조로 충분하다.

## §4 사건부 (`_ledger/case/`)

1. 충돌 후보 대장 `candidates.jsonl`:
   `{"rid": "<UUIDv7>", "kind": "candidate"|"dismiss", "basis": "<근거 키>",
   "basis_version": 1, "type": "<충돌 유형>", "nodes": ["<id>", ...],
   "at": "<ISO>", "reason": "<선택>"}`
2. `basis`는 정렬된 전체 당사자 `id` 집합 + 충돌 유형 + 검사 당시 각
   당사자의 상태 해시로 만드는 정규화 키다. 당사자의 상태가 변하거나 새
   근거가 생기면 키가 달라져 재상정이 가능하다 — 중복 억제는 같은 근거에만
   작용한다(헌법 12조 2항). `basis_version`은 계산식의 판이며, 판이 다른
   키끼리는 대조하지 않는다.
3. 충돌 유형의 초기 목록 — 이 절의 개정으로 확장한다:
   - `contradiction` — 두 주장이 양립 불가.
   - `duplication` — 같은 주장·적용 범위의 독립 노드(헌법 3조 7항).
   - `competition` — 같은 물음에 다른 답. 존치 판결이 자연스러운 유형.
   - `lineage-fork` — 둘 이상의 노드가 같은 선행 노드를 `replaces`.
   - `delegation-overlap` — 위임 노드 사이의 적용 범위 중첩·상반. 심의는
     사용자 전속.
3. 사건 기록은 `CASE-<연도>-<일련>.md` 파일로 두고, 파일 머리에 기계 판정용
   고정 헤더를 둔다 — 이 헤더는 노드 frontmatter가 아니라 사건 필드다:
   `case_no` / `status`(docketed·adjudicated) / `parties`(`id` 목록) /
   `docketed_at` / `pre_sign`(당사자별 입건 직전 유효 서명의 `rid`, 없으면
   null) / `verdict`(기각·개정·존치·null) / `verdict_at` / `applied`(적용
   결과) / `schema_version`. 본문에는 근거·심의 경과·판결문을 산문으로
   쓰고 당사자 위키링크(판례의 역참조)를 둔다.
4. 비노드 파일 안의 위키링크는 그래프에 산입되지 않는 표시용 참조다.
   당사자에서 사건으로의 도달은 Obsidian backlink 표시가 제공한다(시행령
   §9 4항).

## §5 이행 기록 (`_ledger/migration/`)

1. 체제 이행의 감사 대장이다(헌법 14조 9~10항). 이행의 모든 변경은 실행
   전에 이 대장에 기록한다 — 형식 확정 전의 이동·변환은 하지 않는다.
2. 판정 로그 `events.jsonl`:
   `{"rid": "<UUIDv7>", "kind": "archive"|"move"|"transform"|"hold"|"drop",
   "source": "<구 경로>", "dest": "<신 경로|null>",
   "before": "sha256:<hex>", "after": "sha256:<hex>|null",
   "rule": "<적용 규칙>", "at": "<ISO>", "note": "<선택>"}`
3. manifest는 이행 착수 시 전수 목록(대상·분류·계획)을 먼저 작성하고, 완료
   후 결과 요약과 보류 목록으로 닫는다. 이행 완료 후에는 새 기입이 없는
   닫힌 대장으로 보존한다.

## §6 pin 기록 (`_ledger/pins.jsonl`)

1. `{"rid": "<UUIDv7>", "kind": "pin"|"unpin",
   "target": "<군집 경로 또는 노드 id>", "at": "<ISO>", "reason": "<선택>"}`
2. 군집의 형성·분화·재배정 판정은 실행 전에 이 파일을 대조한다(시행령 §3
   4항). pin은 노드가 아니며 서명·위임의 효력을 갖지 않는다.

## §7 위임 절 형식

1. 지속 위임 노드는 본문에 `## 위임` 절을 두고 다음 네 항목을 갖춘다.
   ```
   ## 위임
   - 대상: <위임하는 행위>
   - 범위: <적용 경계>
   - 조건: <조건. 없으면 "없음">
   - 종료: <종료 조건. 없으면 "없음">
   ```
2. 절이 없거나 항목이 빠진 위임 절은 형식 미충족으로, 권한의 근거가 되지
   않는다(시행령 §5 1항).

## §8 링크·임베드 문법

1. Link는 위키링크 `[[대상]]`으로 쓴다. 대상 내부 위치는 `[[대상#앵커]]`.
2. Predicate Edge는 frontmatter에 YAML 값으로 쓴다 — 위키링크는 따옴표로
   감싼다: `supported-by: "[[대상]]"`, 여럿이면 목록.
3. 원자료의 표시는 임베드 `![[파일명]]`을 쓴다(`_sources/`의 이미지·pdf가
   Obsidian에서 렌더링된다). 임베드는 Link의 표시형이다.
4. 산입 세칙: 중심성은 노드 사이의 참조만 산입한다. 비노드를 향한 Link·
   임베드·Predicate Edge는 중심성에 산입하지 않는다. 비노드 파일 안의
   위키링크는 그래프 밖 표시용이다.

## §9 비밀값 정규식 필터 (시행령 §2 3항)

1. `_raw/` 기록 시 다음 고위험 정형 패턴을 `[FILTERED:<이름>]`으로
   치환한다. 목록은 이 절의 개정으로 확장한다.

   | 이름 | 패턴 |
   |---|---|
   | pem-private-key | `-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----` |
   | aws-access-key | `\bAKIA[0-9A-Z]{16}\b` |
   | github-token | `\bgh[pousr]_[A-Za-z0-9]{36,}\b` |
   | openai-style-key | `\bsk-[A-Za-z0-9_\-]{20,}\b` |
   | slack-token | `\bxox[baprs]-[A-Za-z0-9\-]{10,}\b` |
   | google-api-key | `\bAIza[0-9A-Za-z_\-]{35}\b` |
   | bearer-header | `Authorization:\s*Bearer\s+[A-Za-z0-9_\-.~+/]+=*` |

2. 정규식은 Python `re` 문법이며 추가 flags 없이 해석한다(pem-private-key의
   줄바꿈 매칭은 패턴 안의 `[\s\S]`가 담당한다). 각 패턴은 양성·음성
   fixture와 함께 검증기에 등재한 뒤 활성화한다.
3. 그 밖의 마스킹은 적용하지 않는다.

## §10 해석 각서

1. 헌법 6조 11항의 충돌 우선순위는 사용자에 관한 서술의 충돌 규칙이다.
   통치 규범과의 충돌은 이 사다리가 아니라 헌법 14조의 개정 절차로 다룬다.
