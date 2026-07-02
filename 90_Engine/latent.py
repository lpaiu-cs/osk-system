#!/usr/bin/env python3
"""90_Engine/latent.py — latent split candidate(분할 후보) 파이프라인.

[[Granularity Policy]](00_System/Granularity Policy.md)의 엔진 측 구현:

  - **파싱**: frontmatter `latent_split_candidate`(리스트-오브-맵)를 전용 파서로 읽어
    DuckDB `latent_candidates`로 컴파일한다(파생 캐시 — 파일 재색인 시 재구성).
    indexer의 최소 YAML 파서는 중첩 맵을 못 읽으므로 이 모듈이 전담한다.
  - **hit 기록**: retrieve 경로 piggyback. 회수된 노트에 후보가 있고 쿼리가 후보의
    evidence/reason/candidate_title과 어휘 겹침이 있으면 `latent_hits`에 기록한다
    (span-편중 회수의 근사). "서로 다른 맥락" v1 정의 = 정규화 쿼리 해시의 구별.
    카운터는 Markdown이 아니라 여기(DB)에만 산다 — md = 사람이 읽는 선언,
    카운터 = 파생 런타임 상태. 캐시 재생성 시 카운터 초기화는 허용 손실이다.
  - **승격(promote)**: extraction-only. evidence_quote로 span(문단 블록)을 기계적으로
    위치 특정해 새 노드로 **이동**하고, 부모에는 `[[새 노드]]` 한 줄만 남긴다.
    본문 복제(distillation) 경로는 없다. 애매하면(인용 미발견/중복/펜스 안/문단 경계
    초과/제목 충돌) 자동 실행하지 않고 80_Reviews 큐로 라우팅한다.
  - **동시성**: 모든 read-write 함수는 데몬의 write 락 하에서 호출되는 것을 전제로
    단명 연결(connect_db(read_only=False))을 열고 즉시 닫는다. DuckDB는 같은 파일에
    read-only/read-write 연결을 동시에 못 열기 때문이다(vault_daemon.py 참조).
    멱등성: 승격 로그(latent_promotions) + 노드 파일 존재로 재호출을 흡수한다.

설계 근거: 40_Decisions/2026-07-02-node-granularity-split-vs-fold.md
"""
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

LATENT_KEY = "latent_split_candidate"
DEFAULT_PROMOTE_THRESHOLD = 2
PROMOTE_FOLDER_DEFAULT = "20_Concepts"
REVIEW_QUEUE_REL = Path("80_Reviews") / "Needs Human Review.md"

# frontmatter 구분 정규식의 정본(canonical). indexer.FRONTMATTER_REGEX는 이것의 alias다
# (동일 정규식 중복 정의 금지 — drift 방지).
FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<meta>.*?)\n---\s*\n", re.DOTALL)

# 리뷰 큐 항목 헤딩 포맷의 정본 — 쓰기(route_to_review)와 읽기(mcp_server.review_queue)가
# 이 상수를 공유한다(포맷 소유권 분산 방지).
REVIEW_ITEM_HEADING = "### [{status}] {title}"
REVIEW_ITEM_RE = re.compile(r"^###\s*\[(?P<status>[A-Za-z\-]+)\]\s*(?P<title>.+?)\s*$")

_ENTRY_FIELDS = ("id", "candidate_title", "reason", "evidence", "promote_condition")

# 미지원 인라인 flow 스타일(latent_split_candidate: [{...}]) 감지용 — 무음 실패 방지.
_INLINE_FLOW_RE = re.compile(rf"^{LATENT_KEY}\s*:\s*\[.*\S.*\]\s*$")


# ─────────────────────────────────────────────────────────────
# §1. 스키마
# ─────────────────────────────────────────────────────────────
def ensure_schema(conn):
    """latent 테이블 3종을 보장한다(멱등). indexer.init_database와 promote/record_hits
    양쪽에서 호출되어, 구버전 캐시 파일에서도 안전하게 동작한다."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS latent_candidates (
            candidate_key     VARCHAR PRIMARY KEY,   -- f"{node_id}::{slug}"
            node_id           UUID NOT NULL,
            file_path         VARCHAR NOT NULL,
            slug              VARCHAR NOT NULL,
            candidate_title   VARCHAR,
            reason            VARCHAR,
            evidence          VARCHAR,
            promote_condition VARCHAR,
            last_indexed      TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_latent_cand_node ON latent_candidates(node_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_latent_cand_path ON latent_candidates(file_path)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS latent_hits (
            candidate_key VARCHAR NOT NULL,
            query_hash    VARCHAR NOT NULL,
            query_text    VARCHAR,
            hit_at        TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_latent_hits_key ON latent_hits(candidate_key)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS latent_promotions (
            candidate_key                VARCHAR NOT NULL,
            parent_title                 VARCHAR NOT NULL,
            new_title                    VARCHAR NOT NULL,
            evidence_quote               VARCHAR NOT NULL,
            independent_review_condition VARCHAR NOT NULL,
            promoted_at                  TIMESTAMP
        )
    """)


# ─────────────────────────────────────────────────────────────
# §2. frontmatter 전용 파서 (latent_split_candidate 블록만)
# ─────────────────────────────────────────────────────────────
def _clean_value(val):
    """스칼라 값 정리. 인용 값은 **닫는 따옴표까지**를 값으로 취하고 그 뒤(트레일링
    주석 포함)는 버린다 — `"값"   # 주석` 형태(정책 문서의 정본 예시)에서 따옴표가
    값에 남는 것을 방지. 비인용 값은 트레일링 주석만 제거한다."""
    val = val.strip()
    if val[:1] in ("'", '"'):
        end = val.find(val[0], 1)
        if end != -1:
            return val[1:end]
    return re.split(r"\s+#", val, 1)[0].strip()


def _parse_block(meta_lines):
    """meta 라인들에서 latent_split_candidate 블록을 찾아 엔트리와 라인 범위를 반환.

    반환: (key_idx, entries)
      key_idx : 키 라인 인덱스 (없으면 -1)
      entries : [{"fields": dict, "start": i, "end": j}]  (meta_lines 기준, 양끝 포함)
    """
    key_idx = -1
    for i, raw in enumerate(meta_lines):
        if re.match(rf"^{LATENT_KEY}\s*:\s*(\[\s*\]\s*)?$", raw.strip()) and not raw[:1].isspace():
            key_idx = i
            break
    if key_idx == -1:
        return -1, []

    entries = []
    cur = None  # {"fields": {}, "start": i, "end": i}
    i = key_idx + 1
    while i < len(meta_lines):
        raw = meta_lines[i]
        stripped = raw.strip()
        if stripped and not raw[:1].isspace():
            break  # 다음 top-level 키 → 블록 종료
        if not stripped:
            i += 1
            continue  # 블록 내 빈 줄 허용(종료로 치지 않음)
        if stripped.startswith("- "):
            if cur:
                entries.append(cur)
            cur = {"fields": {}, "start": i, "end": i}
            rest = stripped[2:].strip()
            if ":" in rest:
                k, _, v = rest.partition(":")
                if k.strip() in _ENTRY_FIELDS:
                    cur["fields"][k.strip()] = _clean_value(v)
        elif cur is not None and ":" in stripped:
            cur["end"] = i
            k, _, v = stripped.partition(":")
            if k.strip() in _ENTRY_FIELDS:
                cur["fields"][k.strip()] = _clean_value(v)
        elif cur is not None:
            cur["end"] = i  # 알 수 없는 연속 라인 — 엔트리에 귀속
        i += 1
    if cur:
        entries.append(cur)
    return key_idx, entries


def _slug_of(fields):
    explicit = (fields.get("id") or "").strip()
    if explicit:
        return explicit
    basis = (fields.get("reason") or "") + (fields.get("evidence") or "")
    return "lsc-" + hashlib.md5(basis.encode("utf-8")).hexdigest()[:8]


def parse_latent_candidates(content):
    """노트 전문에서 latent 후보 목록을 파싱한다(블록 스타일 리스트만 지원).
    반환: [{"slug","candidate_title","reason","evidence","promote_condition"}]"""
    m = FRONTMATTER_RE.search(content)
    if not m:
        return []
    meta_lines = m.group("meta").splitlines()
    # 미지원 인라인 flow 스타일은 조용히 0건이 되지 않도록 경고한다(표식은 무시됨).
    for raw in meta_lines:
        if not raw[:1].isspace() and _INLINE_FLOW_RE.match(raw.strip()):
            print(f"[latent][WARN] '{LATENT_KEY}' 인라인 flow 스타일([{{...}}])은 미지원 — "
                  f"블록 스타일 리스트로 작성하세요(Granularity Policy §2.1). 이 표식은 무시됩니다.",
                  file=sys.stderr)
            break
    _, entries = _parse_block(meta_lines)
    out = []
    for e in entries:
        f = e["fields"]
        out.append({
            "slug": _slug_of(f),
            "candidate_title": f.get("candidate_title") or None,
            "reason": f.get("reason") or None,
            "evidence": f.get("evidence") or None,
            "promote_condition": f.get("promote_condition") or None,
        })
    return out


def _remove_candidate_from_meta(meta_text, slug):
    """meta 텍스트에서 slug에 해당하는 엔트리를 제거. 엔트리가 다 없어지면 키 라인도
    제거한다. 반환: (new_meta_text, removed: bool)"""
    lines = meta_text.splitlines()
    key_idx, entries = _parse_block(lines)
    if key_idx == -1:
        return meta_text, False
    target = None
    for e in entries:
        if _slug_of(e["fields"]) == slug:
            target = e
            break
    if target is None:
        return meta_text, False
    drop = set(range(target["start"], target["end"] + 1))
    if len(entries) == 1:
        drop.add(key_idx)
    new_lines = [ln for i, ln in enumerate(lines) if i not in drop]
    return "\n".join(new_lines), True


# ─────────────────────────────────────────────────────────────
# §3. 인덱스 동기화 (indexer 2차 패스에서 호출)
# ─────────────────────────────────────────────────────────────
# promote가 새 노드 frontmatter에 남기는 감사 필드 — update_node류 재조립에서 보존 대상.
PROMOTION_AUDIT_FIELDS = ("promoted_from", "promotion_evidence", "independent_review_condition")


def extract_passthrough_meta(meta_text):
    """frontmatter 재조립(update_node 등) 시 보존해야 할 엔진 소유 라인들의 원문을 반환:
    latent_split_candidate 블록(엔트리가 있을 때) + 승격 감사 필드. 없으면 None.
    일반 노드 편집이 승격 신호(마커→hit 카운터)와 감사 추적을 지우는 것을 막는다."""
    if not meta_text:
        return None
    lines = meta_text.splitlines()
    keep = []
    for ln in lines:
        if (":" in ln and not ln[:1].isspace()
                and ln.split(":", 1)[0].strip() in PROMOTION_AUDIT_FIELDS):
            keep.append(ln)
    key_idx, entries = _parse_block(lines)
    if key_idx != -1 and entries:
        end = max(e["end"] for e in entries)
        keep.extend(lines[key_idx:end + 1])
    return "\n".join(keep) if keep else None


def candidate_key(node_id, slug, evidence=""):
    """후보의 안정 식별자: node_id::slug::evidence지문.

    evidence 지문을 포함시키는 이유 — 카운터의 정체성은 **표식 내용**이다. 같은 slug가
    삭제 후 재활용되거나 evidence가 다른 span으로 고쳐지면(repurpose) 새 키가 되어,
    옛 hit이 새 후보의 승격 조건에 계승 집계되는 조기 발화를 막는다. evidence가 그대로면
    재색인을 넘어 키가 안정적이라 hit이 생존한다(설계 의도)."""
    fp = hashlib.md5((evidence or "").encode("utf-8")).hexdigest()[:8]
    return f"{node_id}::{slug}::{fp}"


def sync_candidates_for_file(conn, node_id, file_path, content):
    """재색인된 파일의 후보 행을 재구성한다(delete+insert — 파생 캐시).
    무변경 마커의 hit은 candidate_key가 동일해 보존되고, 이번 재구성에서 사라진 키
    (마커 삭제/evidence 변경)의 hit은 함께 정리한다 — 옛 수요가 새 후보로 계승되지
    않는다. 스키마는 init_database(→ensure_schema)가 매 색인마다 보장하는 것을 전제.
    반환: 이 파일의 후보 수."""
    old_keys = {r[0] for r in conn.execute(
        "SELECT candidate_key FROM latent_candidates WHERE file_path = ?",
        [str(file_path)]).fetchall()}
    conn.execute("DELETE FROM latent_candidates WHERE file_path = ?", [str(file_path)])
    cands = parse_latent_candidates(content)
    now = datetime.now()
    new_keys = set()
    for c in cands:
        key = candidate_key(node_id, c["slug"], c["evidence"])
        new_keys.add(key)
        conn.execute("""
            INSERT OR REPLACE INTO latent_candidates
                (candidate_key, node_id, file_path, slug, candidate_title,
                 reason, evidence, promote_condition, last_indexed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [key, node_id, str(file_path), c["slug"],
              c["candidate_title"], c["reason"], c["evidence"], c["promote_condition"], now])
    stale = old_keys - new_keys
    if stale:
        ph = ", ".join("?" for _ in stale)
        conn.execute(f"DELETE FROM latent_hits WHERE candidate_key IN ({ph})", list(stale))
    return len(cands)


def prune_candidates_for_nodes(conn, node_ids):
    """orphan prune 경로: 사라진 노드들의 후보와 hit 기록을 일괄 제거한다(set-based —
    노드별 반복 호출로 N+1 쿼리를 만들지 않는다). 스키마는 init_database가 보장."""
    ids = list(node_ids)
    if not ids:
        return
    ph = ", ".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM latent_hits WHERE candidate_key IN "
        f"(SELECT candidate_key FROM latent_candidates WHERE node_id IN ({ph}))", ids)
    conn.execute(f"DELETE FROM latent_candidates WHERE node_id IN ({ph})", ids)


# ─────────────────────────────────────────────────────────────
# §4. hit 기록 (retrieve piggyback)
# ─────────────────────────────────────────────────────────────
def _tokenize(text):
    # 토큰 규칙의 단일 출처는 retriever다 — 여기 해시가 검색과 다른 규칙으로 어긋나면
    # distinct-context 카운트가 조용히 리셋/분기하므로 복제하지 않고 재사용한다.
    from retriever import tokenize_korean_english  # lazy (connect_db와 동일 패턴)
    return tokenize_korean_english(text or "")


def normalized_query_hash(query):
    """'서로 다른 맥락' v1 조작적 정의: 토큰 집합(순서·중복 무시) 기준 해시.
    잠정 정의 — 60_Open_Questions/Implementation Questions.md 참조."""
    toks = sorted(set(_tokenize(query)))
    return hashlib.md5(" ".join(toks).encode("utf-8")).hexdigest()[:16]


def parse_threshold(promote_condition):
    m = re.search(r"(\d+)", promote_condition or "")
    return int(m.group(1)) if m else DEFAULT_PROMOTE_THRESHOLD


def candidate_parent_titles(db_path):
    """latent 후보를 보유한 부모 노드 제목 집합(read-only). 데몬이 이 집합과 회수 결과의
    겹침을 먼저 보고, 겹칠 때만 write 락 + hit 기록으로 넘어간다(불필요한 락 회피).
    테이블 부재(구버전 캐시)나 연결 실패는 빈 집합으로 답한다."""
    from retriever import connect_db  # lazy: 락-재시도 헬퍼 재사용
    try:
        conn = connect_db(str(db_path), read_only=True)
    except Exception:
        return set()
    try:
        try:
            rows = conn.execute(
                "SELECT DISTINCT n.title FROM latent_candidates c "
                "JOIN nodes n ON n.node_id = c.node_id").fetchall()
            return {r[0] for r in rows}
        except Exception:
            return set()
    finally:
        conn.close()


def record_hits(db_path, retrieved_titles, query):
    """회수된 노트 제목들에 대해 latent hit을 기록하고 승격 조건 도달 후보를 반환한다.

    반드시 데몬 write 락 하에서 호출할 것(단명 read-write 연결을 열기 때문).
    쿼리와 후보(evidence/reason/candidate_title)의 어휘 겹침이 있을 때만 hit로 친다 —
    노트가 후보 span과 무관한 이유로 회수된 경우를 걸러내는 span-편중 회수의 근사.

    반환: {"recorded": int, "due": [{parent_title, candidate_id, candidate_title,
            distinct_contexts, promote_condition, action}]}
    """
    result = {"recorded": 0, "due": []}
    titles = [t for t in (retrieved_titles or []) if t]
    if not titles or not (query or "").strip():
        return result
    from retriever import connect_db  # lazy
    conn = connect_db(str(db_path), read_only=False)
    try:
        # 스키마는 호출 게이트(candidate_parent_titles가 비어있지 않음 = 테이블 실재)가 보장
        placeholders = ", ".join("?" for _ in titles)
        rows = conn.execute(f"""
            SELECT c.candidate_key, c.slug, c.candidate_title, c.reason, c.evidence,
                   c.promote_condition, n.title
            FROM latent_candidates c
            JOIN nodes n ON n.node_id = c.node_id
            WHERE n.title IN ({placeholders})
        """, titles).fetchall()
        if not rows:
            return result
        qtoks = set(_tokenize(query))
        qhash = normalized_query_hash(query)
        for key, slug, ctitle, reason, evidence, cond, parent_title in rows:
            ctoks = set(_tokenize(" ".join(filter(None, [ctitle, reason, evidence]))))
            if not (qtoks & ctoks):
                continue  # 후보 span과 무관한 회수 — hit 아님
            seen = conn.execute(
                "SELECT 1 FROM latent_hits WHERE candidate_key = ? AND query_hash = ?",
                [key, qhash]).fetchone()
            if not seen:
                conn.execute(
                    "INSERT INTO latent_hits (candidate_key, query_hash, query_text, hit_at)"
                    " VALUES (?, ?, ?, ?)",
                    [key, qhash, (query or "")[:500], datetime.now()])
                result["recorded"] += 1
            distinct = conn.execute(
                "SELECT COUNT(DISTINCT query_hash) FROM latent_hits WHERE candidate_key = ?",
                [key]).fetchone()[0]
            threshold = parse_threshold(cond)
            if distinct >= threshold:
                result["due"].append({
                    "parent_title": parent_title,
                    "candidate_id": slug,
                    "candidate_title": ctitle,
                    "distinct_contexts": distinct,
                    "promote_condition": cond or f"distinct-context retrieval >= {threshold}",
                    "action": ("승격 조건 도달 — promote_latent(parent_title, candidate_id, "
                               "evidence_quote, independent_review_condition)로 승격하거나, "
                               "span이 extraction-ready(자기완결)가 아니면 review queue로 "
                               "보내세요 (Granularity Policy §3)."),
                })
        conn.commit()
        return result
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# §5. 승격 (extraction-only)
# ─────────────────────────────────────────────────────────────
_FORBIDDEN_TITLE = re.compile(r'[\\/:*?"<>|]')


def _norm_ws(s):
    """공백 정규화 비교용 — 본문 줄바꿈 래핑과 frontmatter 한 줄 문자열의 차이를 흡수."""
    return re.sub(r"\s+", " ", s or "").strip()


def _is_graph_row(file_path, vault_root):
    """nodes 행이 링크/엣지 네임스페이스(graph_node=True 계층)에 속하는지 판정.
    06_Raw/05_Inbox 행은 검색용으로만 nodes에 있고 [[링크]] 타깃이 아니므로, 부모 해석과
    제목 충돌 검사에서 제외해야 한다. 계층 정책의 단일 출처인 indexer.policy_for를
    재사용한다(런타임 lazy import — indexer가 이 모듈을 import하므로 모듈 수준 상호
    import는 피하고, 호출 시점엔 indexer가 이미 적재돼 있다)."""
    import indexer  # lazy
    return indexer.policy_for(Path(file_path), Path(vault_root))["graph_node"]


def _split_doc(content):
    """(frontmatter meta 텍스트 | None, body 텍스트) 반환."""
    m = FRONTMATTER_RE.search(content)
    if not m:
        return None, content
    return m.group("meta"), content[m.end():]


def _find_span_block(body, quote):
    """quote가 속한 문단 블록(빈 줄 경계, 코드 펜스 밖)을 찾는다.
    반환: ({"start","end","text"} | None, error_reason | None) — 라인 인덱스는 양끝 포함."""
    idx = body.find(quote)
    if idx == -1:
        return None, "evidence_quote가 부모 본문에 그대로 존재하지 않음(verbatim 필수)"
    if body.find(quote, idx + 1) != -1:
        return None, "evidence_quote가 본문에 2회 이상 등장 — span 특정 불가"

    lines = body.splitlines()
    # 문자 오프셋 → 라인 인덱스
    starts, off = [], 0
    for ln in lines:
        starts.append(off)
        off += len(ln) + 1
    qline = 0
    for i, s in enumerate(starts):
        if s <= idx:
            qline = i
        else:
            break

    # 코드 펜스 안 여부
    fence = False
    for i in range(qline + 1):
        if lines[i].strip().startswith("```"):
            fence = not fence
    if fence:
        return None, "evidence_quote가 코드 펜스 내부에 있음 — 자동 적출 대상 아님"

    s = qline
    while s > 0 and lines[s - 1].strip() != "":
        s -= 1
    e = qline
    while e < len(lines) - 1 and lines[e + 1].strip() != "":
        e += 1
    # 헤딩은 부모에 남긴다(구조 보존) — 헤딩 단독 블록이면 span이 아님
    while s <= e and lines[s].lstrip().startswith("#"):
        s += 1
    if s > e:
        return None, "블록이 헤딩뿐 — 적출할 span 없음"
    span_text = "\n".join(lines[s:e + 1])
    if quote not in span_text:
        return None, "evidence_quote가 문단 경계를 넘음 — 문단 단위 span으로 특정 불가"
    if any(l.strip().startswith("```") for l in lines[s:e + 1]):
        return None, "span 블록에 코드 펜스가 걸침 — 자동 적출 대상 아님"
    nonblank = [i for i, l in enumerate(lines) if l.strip()]
    if nonblank and s <= nonblank[0] and e >= nonblank[-1]:
        return None, "span이 본문 전체 — extraction이 무의미(노트 이동은 수동으로)"
    return {"start": s, "end": e, "text": span_text}, None


def route_to_review(vault_root, summary, detail, related=None):
    """80_Reviews/Needs Human Review.md의 '## Open' 아래에 [open] 항목을 추가한다.
    반환: 리뷰 파일 경로(str)."""
    path = Path(vault_root) / REVIEW_QUEUE_REL
    today = datetime.now().strftime("%Y-%m-%d")
    item_lines = [
        REVIEW_ITEM_HEADING.format(status="open", title=summary),
        "- reason: needs-human-review",
        f"- created: {today}",
        f"- detail: {detail}",
        f"- related: {related or ''}".rstrip(),
        "- resolution:",
        "",
    ]
    if path.exists():
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        insert_at = None
        for i, ln in enumerate(lines):
            if ln.strip() == "## Open":
                insert_at = i + 1
                break
        if insert_at is None:
            lines += ["", "## Open", ""] + item_lines
        else:
            # 바로 아래의 '(없음)' 플레이스홀더는 제거
            j = insert_at
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip().startswith("_("):
                del lines[j]
            lines[insert_at:insert_at] = [""] + item_lines
        path.write_text("\n".join(lines) + ("\n" if not text.endswith("\n") else ""),
                        encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = ("---\ntype: review\nstatus: open\ncreated: " + today +
              "\nreason: needs-human-review\nrelated: []\n---\n\n# Needs Human Review\n\n## Open\n\n")
        path.write_text(fm + "\n".join(item_lines) + "\n", encoding="utf-8")
    return str(path)


def _yaml_str(v):
    return json.dumps(v or "", ensure_ascii=False)


def _build_promoted_node(new_title, span_text, parent_title, parent_moc_raw,
                         evidence_quote, independent_review_condition):
    today = datetime.now().strftime("%Y-%m-%d")
    fm = ["---", f"title: {new_title}", "aliases: []", "type: Concept"]
    if parent_moc_raw:
        fm.append(f"moc: {parent_moc_raw}")
    fm += [
        "status: active",
        f"created: {today}",
        "version: 1.0",
        f'promoted_from: "[[{parent_title}]]"',
        f"promotion_evidence: {_yaml_str(evidence_quote)}",
        f"independent_review_condition: {_yaml_str(independent_review_condition)}",
        "---",
    ]
    body = [
        "",
        f"# {new_title}",
        "",
        span_text,
        "",
        "## Sources",
        "",
        f"- 적출 출처: [[{parent_title}]] (promoted latent, {today}) — "
        f"부모 노트에는 [[{new_title}]] 링크가 남아 있다.",
        "",
    ]
    return "\n".join(fm + body)


def promote(db_path, vault_root, parent_title, candidate_id, evidence_quote,
            independent_review_condition, new_title=None, folder=PROMOTE_FOLDER_DEFAULT):
    """latent 후보를 독립 노드로 승격한다(extraction-only).

    데몬 write 락 하에서 호출할 것. 파일 2개(신규 노드, 부모)를 원자적 단위로 다루고,
    승격 로그(latent_promotions)로 멱등성을 보장한다. 재색인은 호출자(데몬)가 수행하며,
    전역 제목 충돌 검사가 인덱스 기준이므로 호출 직전 인덱스가 신선해야 한다(데몬
    라우트가 승격 직전 증분 재색인으로 보장).

    반환 dict의 status:
      promoted          — 적출 완료 (new_title/new_path/parent_path 포함)
      already_promoted  — 동일 후보가 이미 승격됨 (멱등 재호출)
      routed_to_review  — 자동 실행 부적합 → 80_Reviews 큐에 항목 추가 (reason 포함)
    하드 인자 오류는 ValueError를 던진다.
    """
    vault_root = Path(vault_root)
    evidence_quote = (evidence_quote or "").strip()
    independent_review_condition = (independent_review_condition or "").strip()
    candidate_id = (candidate_id or "").strip()
    parent_title = (parent_title or "").strip()
    if len(evidence_quote) < 10:
        raise ValueError("evidence_quote는 부모 본문에 실재하는 10자 이상 문장이어야 합니다")
    if not independent_review_condition:
        raise ValueError("independent_review_condition(자기완결/독립 검토 확인 근거)은 필수입니다")
    if not parent_title or not candidate_id:
        raise ValueError("parent_title과 candidate_id는 필수입니다")

    from retriever import connect_db  # lazy
    conn = connect_db(str(db_path), read_only=False)
    try:
        ensure_schema(conn)
        # 부모 해석은 그래프 행만 대상 — 동명 06_Raw 행이 먼저 잡히면 마커 없는 raw
        # 파일을 읽어 유효한 승격이 거부된다.
        row = None
        for cond in ("title = ?", "list_contains(aliases, ?)"):
            for cand_row in conn.execute(
                    f"SELECT node_id, file_path FROM nodes WHERE {cond}",
                    [parent_title]).fetchall():
                if _is_graph_row(cand_row[1], vault_root):
                    row = cand_row
                    break
            if row is not None:
                break
        if row is None:
            raise ValueError(f"부모 노드를 찾을 수 없습니다: '{parent_title}' (list_nodes로 제목 확인)")
        node_id, parent_path = str(row[0]), Path(row[1])
        if not parent_path.exists():
            raise ValueError(f"부모 노드 파일이 없습니다: {parent_path}")

        content = parent_path.read_text(encoding="utf-8")
        meta_text, body = _split_doc(content)
        entries = parse_latent_candidates(content)
        entry = next((c for c in entries if c["slug"] == candidate_id), None)
        if entry is None:
            # candidate_title 폴백은 유일 매칭일 때만 — 동명 후보가 2개 이상이면 첫 매칭이
            # 엉뚱한 엔트리를 제거하는 사고가 나므로 명시적으로 거부한다.
            title_matches = [c for c in entries
                             if c["candidate_title"] and c["candidate_title"] == candidate_id]
            if len(title_matches) > 1:
                raise ValueError(
                    f"candidate_title '{candidate_id}'가 후보 여러 개와 일치합니다 — id 슬러그로 "
                    f"지정하세요: {[c['slug'] for c in title_matches]}")
            entry = title_matches[0] if title_matches else None

        if entry is not None:
            key = candidate_key(node_id, entry["slug"], entry.get("evidence"))
            prior = conn.execute(
                "SELECT new_title FROM latent_promotions WHERE candidate_key = ? "
                "ORDER BY promoted_at DESC LIMIT 1", [key]).fetchone()
        else:
            # 멱등 재호출 경로: 승격으로 마커가 이미 제거돼 evidence 지문을 재계산할 수
            # 없으므로 node_id::slug 접두로 승격 로그를 조회한다.
            key = None
            prior = conn.execute(
                "SELECT new_title FROM latent_promotions WHERE candidate_key LIKE ? "
                "ORDER BY promoted_at DESC LIMIT 1",
                [f"{node_id}::{candidate_id}::%"]).fetchone()

        if entry is None:
            if prior:
                return {"status": "already_promoted", "new_title": prior[0],
                        "parent_title": parent_title,
                        "note": "동일 후보가 이미 승격되어 frontmatter에서 제거됨(멱등 재호출)"}
            available = [c["slug"] for c in entries]
            raise ValueError(
                f"'{parent_title}'의 frontmatter에 candidate_id '{candidate_id}'가 없습니다. "
                f"존재하는 후보: {available or '없음'}")

        resolved_title = (new_title or entry.get("candidate_title") or "").strip()
        if not resolved_title:
            raise ValueError("새 노드 제목이 없습니다 — new_title 인자 또는 후보의 candidate_title 필요")
        if _FORBIDDEN_TITLE.search(resolved_title):
            raise ValueError(f"제목에 파일명 금지문자가 있습니다: {resolved_title!r}")

        new_path = vault_root / folder / f"{resolved_title}.md"
        # 새 노드는 vault 전역 링크 네임스페이스에 들어간다. 인덱서의 title_to_id는
        # **파일명 stem + aliases**로 키잉하고(frontmatter title 아님), 검색/노출은
        # nodes.title을 쓴다 — 셋 중 무엇과 겹쳐도 [[링크]]/엣지 해석이 모호해지므로
        # 세 네임스페이스 전부에서 충돌을 검사한다(승격은 드물어 전행 스캔 비용 무시 가능).
        # 단, 링크 타깃이 아닌 계층(06_Raw 등 graph_node=False)의 행은 충돌이 아니다 —
        # 동명 raw가 있다고 유효한 승격을 review로 보내지 않는다(인덱서 정책과 동일 기준).
        dup = None
        for fp, t, aliases in conn.execute(
                "SELECT file_path, title, aliases FROM nodes").fetchall():
            if (t == resolved_title or Path(fp).stem == resolved_title
                    or (aliases and resolved_title in list(aliases))):
                if not _is_graph_row(fp, vault_root):
                    continue
                dup = (fp,)
                break
        if new_path.exists() or dup:
            if prior and prior[0] == resolved_title:
                return {"status": "already_promoted", "new_title": resolved_title,
                        "parent_title": parent_title, "note": "노드가 이미 존재(멱등 재호출)"}
            existing = dup[0] if dup else str(new_path)
            review = route_to_review(
                vault_root,
                f"latent 승격 충돌: '{resolved_title}' 노드가 이미 존재",
                f"[[{parent_title}]]의 후보 '{entry['slug']}' 승격 시도 — 동명 노드({existing})가 "
                f"이미 있어 자동 적출을 중단(제목은 vault 전역 링크 식별자). 병합/개명 판단 "
                f"필요. evidence: {evidence_quote[:120]}",
                f"[[{parent_title}]]")
            return {"status": "routed_to_review", "reason": "title-collision",
                    "review_file": review, "parent_title": parent_title}

        span, err = _find_span_block(body, evidence_quote)
        if err:
            review = route_to_review(
                vault_root,
                f"latent 승격 보류: [[{parent_title}]] 후보 '{entry['slug']}'",
                f"{err}. extraction-ready가 아니므로 자동 적출하지 않음(Granularity Policy §3.3). "
                f"재구성 필요 여부를 사람이 판단. evidence: {evidence_quote[:120]}",
                f"[[{parent_title}]]")
            return {"status": "routed_to_review", "reason": err,
                    "review_file": review, "parent_title": parent_title}

        # 감사 앵커 검증: 후보에 기록된 evidence가 지정된 span 안에 실재해야 한다 —
        # 유효한 candidate_id에 무관 문단의 인용을 조합하면 마커와 무관한 span이
        # 적출되는 사고를 차단하는, 파괴적 extraction의 최종 게이트다.
        recorded = _norm_ws(entry.get("evidence"))
        if not recorded or recorded not in _norm_ws(span["text"]):
            review = route_to_review(
                vault_root,
                f"latent 승격 보류: [[{parent_title}]] 후보 '{entry['slug']}' evidence 불일치",
                f"후보 마커에 기록된 evidence가 지정된 span에 없음(또는 미기록) — 마커와 "
                f"다른 문단을 적출하려는 호출일 수 있어 자동 실행하지 않음. "
                f"기록된 evidence: {(entry.get('evidence') or '(없음)')[:120]} / "
                f"지정 quote: {evidence_quote[:120]}",
                f"[[{parent_title}]]")
            return {"status": "routed_to_review", "reason": "evidence-mismatch",
                    "review_file": review, "parent_title": parent_title}

        # 부모 moc 원문(있으면 새 노드에 승계)
        parent_moc_raw = None
        if meta_text:
            for ln in meta_text.splitlines():
                if ln.startswith("moc:") and not ln[:1].isspace():
                    parent_moc_raw = ln.partition(":")[2].strip()
                    break

        # ── 쓰기 단계 ──
        new_md = _build_promoted_node(resolved_title, span["text"], parent_title,
                                      parent_moc_raw, evidence_quote,
                                      independent_review_condition)
        body_lines = body.splitlines()
        body_lines[span["start"]:span["end"] + 1] = [f"[[{resolved_title}]]"]
        new_body = "\n".join(body_lines)
        if meta_text is not None:
            new_meta, _removed = _remove_candidate_from_meta(meta_text, entry["slug"])
            new_parent = f"---\n{new_meta}\n---\n{new_body}"
        else:
            new_parent = new_body
        if not new_parent.endswith("\n"):
            new_parent += "\n"

        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(new_md if new_md.endswith("\n") else new_md + "\n",
                            encoding="utf-8")
        parent_path.write_text(new_parent, encoding="utf-8")

        conn.execute("""
            INSERT INTO latent_promotions
                (candidate_key, parent_title, new_title, evidence_quote,
                 independent_review_condition, promoted_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [key, parent_title, resolved_title, evidence_quote,
              independent_review_condition, datetime.now()])
        conn.commit()
        return {
            "status": "promoted",
            "new_title": resolved_title,
            "new_path": str(new_path),
            "parent_title": parent_title,
            "parent_path": str(parent_path),
            "span_lines": span["end"] - span["start"] + 1,
            "note": "부모 span은 [[링크]] 한 줄로 대체됨(extraction-only). 재색인은 데몬이 수행.",
        }
    finally:
        conn.close()
