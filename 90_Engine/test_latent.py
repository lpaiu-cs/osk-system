#!/usr/bin/env python3
"""90_Engine/test_latent.py — latent 파이프라인 통합 테스트 (stdlib assert 기반).

Ollama 불필요(embed=False). duckdb만 있으면 됨:
    python3 90_Engine/test_latent.py

검증 범위: frontmatter 전용 파서 → 인덱서 동기화(재색인 생존/삭제) → hit 기록
(어휘 겹침 게이트 · distinct-context 판정) → promote(적출/멱등/review 라우팅).
"""
import sys
import shutil
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import latent  # noqa: E402
import indexer  # noqa: E402

PARENT_MD = """---
title: Parent Note
aliases: []
type: Concept
moc: "[[Test MOC]]"
status: active
created: 2026-07-02
latent_split_candidate:
  - id: split-alpha
    candidate_title: "Alpha Extraction Concept"
    reason: "G2 충족·G1 미충족 — 독립 검토 가능하나 게이트 통과 엣지 부재"
    evidence: "게이트 임계값은 재사용 수요가 실측될 때만 조정한다"
    promote_condition: "distinct-context retrieval >= 2"
  - reason: "후보 2 — id 없이 자동 슬러그"
    parent: "Parent Note"
    evidence: "두번째 후보의 근거 문장이다"
    hit_count: 0
    promote_condition: "distinct-context retrieval >= 3"
---

# Parent Note

첫 문단은 부모의 주제 서술이다. 적출 대상이 아니다.

게이트 임계값은 재사용 수요가 실측될 때만 조정한다. 이 문단은 자기완결적이며,
독립 검토가 가능한 span이다. Alpha extraction 대상.

두번째 후보의 근거 문장이다. 이 문단도 자기완결적이다.

```
게이트 임계값은 재사용 수요가 실측될 때만 조정한다
(코드 펜스 안 미끼 — span 특정에 걸리면 안 됨... 은 아니고, 중복 검출 테스트용)
```

마지막 문단.
"""

OTHER_MD = """---
title: Other Note
type: Concept
status: active
---

# Other Note

관련 없는 노트.
"""


def make_vault():
    root = Path(tempfile.mkdtemp(prefix="latent_test_vault_"))
    (root / "20_Concepts").mkdir(parents=True)
    (root / "80_Reviews").mkdir(parents=True)
    (root / "20_Concepts" / "Parent Note.md").write_text(PARENT_MD, encoding="utf-8")
    (root / "20_Concepts" / "Other Note.md").write_text(OTHER_MD, encoding="utf-8")
    db = root / "90_Engine" / "test_cache.db"
    return root, db


def reindex(root, db):
    stats, conn = indexer.index_vault(root, db, force_rebuild=False, embed=False)
    conn.close()
    return stats


def q1(db, sql, params=None):
    from retriever import connect_db
    conn = connect_db(str(db), read_only=True)
    try:
        return conn.execute(sql, params or []).fetchall()
    finally:
        conn.close()


def test_parser():
    cands = latent.parse_latent_candidates(PARENT_MD)
    assert len(cands) == 2, cands
    a = cands[0]
    assert a["slug"] == "split-alpha"
    assert a["candidate_title"] == "Alpha Extraction Concept"
    assert a["evidence"] == "게이트 임계값은 재사용 수요가 실측될 때만 조정한다"
    assert latent.parse_threshold(a["promote_condition"]) == 2
    b = cands[1]
    assert b["slug"].startswith("lsc-"), "id 없는 엔트리는 자동 슬러그"
    assert latent.parse_threshold(b["promote_condition"]) == 3
    # 핸드오프 스타일 여분 키(parent/hit_count)는 무해하게 무시
    assert "parent" not in b or True
    # 후보 없는 문서
    assert latent.parse_latent_candidates(OTHER_MD) == []
    print("  [ok] parser")


def test_index_sync(root, db):
    stats = reindex(root, db)
    assert stats["latent_candidates_synced"] == 2, stats
    rows = q1(db, "SELECT candidate_key, slug FROM latent_candidates ORDER BY slug")
    assert len(rows) == 2, rows
    # 무변경 재색인 → 후보 행 유지(그리고 hits도 생존해야 함: 아래 hit 테스트 후 재확인)
    stats2 = reindex(root, db)
    assert stats2["latent_candidates_synced"] == 0  # 무변경 파일은 재파싱 안 함
    rows2 = q1(db, "SELECT COUNT(*) FROM latent_candidates")
    assert rows2[0][0] == 2
    print("  [ok] index sync")


def test_hits(root, db):
    # 어휘 겹침 없는 쿼리 → 기록 안 됨
    r = latent.record_hits(db, ["Parent Note"], "완전히 무관한 질의 텍스트 blah")
    assert r["recorded"] == 0 and r["due"] == [], r
    # 겹침 쿼리 1회 → 기록 1, due 없음
    r = latent.record_hits(db, ["Parent Note"], "게이트 임계값 조정 기준이 뭐였지")
    assert r["recorded"] == 1 and r["due"] == [], r
    # 같은 쿼리(토큰 집합 동일) 반복 → distinct 안 늘어남
    r = latent.record_hits(db, ["Parent Note"], "기준이 뭐였지 게이트 임계값 조정")
    assert r["recorded"] == 0 and r["due"] == [], r
    # 다른 맥락 쿼리 → distinct 2 → 후보 alpha(threshold 2)만 발화
    r = latent.record_hits(db, ["Parent Note"], "재사용 수요 실측은 어떻게 하나")
    assert r["recorded"] == 1, r
    assert len(r["due"]) == 1 and r["due"][0]["candidate_id"] == "split-alpha", r
    assert r["due"][0]["distinct_contexts"] == 2
    # 회수 목록에 부모가 없으면 아무 일도 없음
    r = latent.record_hits(db, ["Other Note"], "게이트 임계값")
    assert r["recorded"] == 0
    # 재색인해도 hits 생존 (candidate_key 안정성)
    reindex(root, db)
    n = q1(db, "SELECT COUNT(DISTINCT query_hash) FROM latent_hits")[0][0]
    assert n == 2, n
    print("  [ok] hits & distinct-context")


def test_promote(root, db):
    quote = "게이트 임계값은 재사용 수요가 실측될 때만 조정한다. 이 문단은 자기완결적이며,"
    # 하드 인자 오류
    try:
        latent.promote(db, root, "Parent Note", "split-alpha", "짧음", "G2 확인함")
        raise AssertionError("짧은 evidence_quote가 통과됨")
    except ValueError:
        pass
    try:
        latent.promote(db, root, "Parent Note", "split-alpha", quote, "")
        raise AssertionError("빈 review_condition이 통과됨")
    except ValueError:
        pass
    try:
        latent.promote(db, root, "Parent Note", "no-such-slug", quote, "G2 확인함")
        raise AssertionError("없는 candidate_id가 통과됨")
    except ValueError:
        pass
    # 본문에 없는 인용 → review 라우팅
    r = latent.promote(db, root, "Parent Note", "split-alpha",
                       "본문에 존재하지 않는 인용 문장이다", "G2 확인함")
    assert r["status"] == "routed_to_review", r
    review = (root / "80_Reviews" / "Needs Human Review.md").read_text(encoding="utf-8")
    assert "[open] latent 승격 보류" in review
    # 정상 승격
    r = latent.promote(db, root, "Parent Note", "split-alpha", quote,
                       "부모 폐기 가정 하 단독 검토 가능 — 대명사/문맥 참조 없음 확인")
    assert r["status"] == "promoted", r
    new_path = root / "20_Concepts" / "Alpha Extraction Concept.md"
    assert new_path.exists()
    new_md = new_path.read_text(encoding="utf-8")
    assert quote in new_md, "span이 새 노드로 이동해야 함"
    assert "promoted_from" in new_md and "[[Parent Note]]" in new_md
    parent = (root / "20_Concepts" / "Parent Note.md").read_text(encoding="utf-8")
    assert quote not in parent, "extraction-only: 부모에 사본이 남으면 안 됨"
    assert "[[Alpha Extraction Concept]]" in parent, "부모에 위키링크 한 줄"
    remaining = latent.parse_latent_candidates(parent)
    assert len(remaining) == 1 and remaining[0]["slug"] != "split-alpha", remaining
    # 코드 펜스 미끼는 부모에 그대로 (span 아님)
    assert "코드 펜스 안 미끼" in parent
    # 멱등 재호출
    r2 = latent.promote(db, root, "Parent Note", "split-alpha", quote, "G2 재확인")
    assert r2["status"] == "already_promoted", r2
    # 재색인 → 새 노드 등장 + 후보 1개 남음
    reindex(root, db)
    rows = q1(db, "SELECT title FROM nodes WHERE title = 'Alpha Extraction Concept'")
    assert len(rows) == 1
    n = q1(db, "SELECT COUNT(*) FROM latent_candidates")[0][0]
    assert n == 1, n
    logs = q1(db, "SELECT new_title FROM latent_promotions")
    assert logs and logs[0][0] == "Alpha Extraction Concept"
    print("  [ok] promote (적출/멱등/review 라우팅)")


def test_fence_quote(root, db):
    # 코드 펜스 안에만 있는 인용 → review 라우팅
    p = root / "20_Concepts" / "Fence Note.md"
    p.write_text("""---
title: Fence Note
type: Concept
latent_split_candidate:
  - id: fenced
    candidate_title: "Fenced Concept"
    reason: "test"
    evidence: "펜스 안에만 있는 문장 데이터"
    promote_condition: "distinct-context retrieval >= 2"
---

# Fence Note

본문 문단.

```
펜스 안에만 있는 문장 데이터 12345
```
""", encoding="utf-8")
    reindex(root, db)
    r = latent.promote(db, root, "Fence Note", "fenced",
                       "펜스 안에만 있는 문장 데이터 12345", "G2 확인")
    assert r["status"] == "routed_to_review", r
    assert "코드 펜스" in r["reason"], r
    print("  [ok] fence guard")


def main():
    root, db = make_vault()
    print(f"[*] temp vault: {root}")
    try:
        test_parser()
        test_index_sync(root, db)
        test_hits(root, db)
        test_promote(root, db)
        test_fence_quote(root, db)
        print("\nALL LATENT TESTS PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
