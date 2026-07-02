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


def check_parser():
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
    # 핸드오프 스타일 여분 키(parent/hit_count)는 파서 출력에 새지 않는다
    assert set(b) == {"slug", "candidate_title", "reason", "evidence", "promote_condition"}, b
    # 중첩 latent 필드가 indexer 미니 파서에서 최상위 키로 누출되지 않는다 (회귀 방지)
    meta = indexer.parse_yaml_frontmatter(PARENT_MD)
    for leaked in ("reason", "evidence", "promote_condition", "candidate_title"):
        assert leaked not in meta, f"'{leaked}' 최상위 누출: {meta}"
    assert meta.get("status") == "active" and meta.get("title") == "Parent Note", meta
    # retriever 파서도 동일: 엔트리 안의 status/confidence류 필드가 노드 메타를 덮지 않는다
    import retriever
    nested_status_md = ("---\ntitle: N\nstatus: active\nlatent_split_candidate:\n"
                        "  - id: x\n    status: pending\n    confidence: low\n"
                        "    evidence: \"e\"\n---\n\nbody\n")
    fm = retriever.parse_frontmatter_fields(nested_status_md)
    assert fm.get("status") == "active" and "confidence" not in fm, fm
    # 후보 없는 문서
    assert latent.parse_latent_candidates(OTHER_MD) == []
    # 인용 스칼라 + 트레일링 주석(정책 문서 §2.1 정본 예시 형태) → 따옴표·주석 모두 제거
    doc_comment = ("---\nlatent_split_candidate:\n"
                   "  - id: slug-x        # 후보 슬러그\n"
                   "    candidate_title: \"Quoted Title\"   # 승격 시 새 노드 제목(제안)\n"
                   "    evidence: 'single quoted'  # c\n"
                   "    reason: unquoted value  # c\n"
                   "---\n\nb\n")
    c = latent.parse_latent_candidates(doc_comment)[0]
    assert c["slug"] == "slug-x", c
    assert c["candidate_title"] == "Quoted Title", c
    assert c["evidence"] == "single quoted" and c["reason"] == "unquoted value", c
    # update_node류 frontmatter 재조립에서 보존할 엔진 소유 메타 추출(마커+감사 필드)
    pm = latent.extract_passthrough_meta(
        "title: X\npromoted_from: \"[[P]]\"\nlatent_split_candidate:\n"
        "  - id: keep-1\n    evidence: \"e\"\nstatus: active")
    assert "promoted_from" in pm and "- id: keep-1" in pm, pm
    assert "title: X" not in pm and "status: active" not in pm, pm
    reparsed = latent.parse_latent_candidates(f"---\ntitle: Y\n{pm}\n---\n\nb\n")
    assert len(reparsed) == 1 and reparsed[0]["slug"] == "keep-1", reparsed
    assert latent.extract_passthrough_meta("title: X\nstatus: active") is None
    # 미지원 인라인 flow 스타일 → 후보 0건 + 경고(무음 실패 아님)
    import io
    import contextlib
    inline_md = ('---\ntitle: I\nlatent_split_candidate: [{id: x, evidence: "e"}]\n---\n\nb\n')
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        assert latent.parse_latent_candidates(inline_md) == []
    assert "인라인 flow" in buf.getvalue(), buf.getvalue()
    print("  [ok] parser")


def check_index_sync(root, db):
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


def check_hits(root, db):
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


def check_promote(root, db):
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
    # 유효한 candidate_id + 무관 문단의 인용 → 마커 evidence 불일치로 review 라우팅
    r = latent.promote(db, root, "Parent Note", "split-alpha",
                       "첫 문단은 부모의 주제 서술이다. 적출 대상이 아니다.", "G2 확인함")
    assert r["status"] == "routed_to_review" and r["reason"] == "evidence-mismatch", r
    assert "첫 문단은 부모의 주제 서술이다" in (root / "20_Concepts" / "Parent Note.md").read_text(
        encoding="utf-8"), "무관 문단이 적출되면 안 됨"
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
    # 승격으로 마커가 제거되면 그 후보의 hit도 정리된다(옛 수요 잔류 금지)
    n = q1(db, "SELECT COUNT(*) FROM latent_hits WHERE candidate_key LIKE '%::split-alpha::%'")[0][0]
    assert n == 0, n
    print("  [ok] promote (적출/멱등/review 라우팅)")


def check_hit_reset(root, db):
    # 마커 evidence 변경(repurpose) → 키의 evidence 지문이 바뀌어 카운터가 0에서 재시작
    p = root / "20_Concepts" / "Parent Note.md"
    r = latent.record_hits(db, ["Parent Note"], "두번째 후보의 근거 문장 관련 질의")
    assert r["recorded"] == 1, r
    live_sql = ("SELECT COUNT(*) FROM latent_hits h JOIN latent_candidates c "
                "ON c.candidate_key = h.candidate_key WHERE c.slug LIKE 'lsc-%'")
    assert q1(db, live_sql)[0][0] == 1
    txt = p.read_text(encoding="utf-8").replace(
        "두번째 후보의 근거 문장이다", "두번째 후보의 수정된 근거 문장이다")
    p.write_text(txt, encoding="utf-8")
    reindex(root, db)
    assert q1(db, live_sql)[0][0] == 0, "repurpose된 마커가 옛 hit을 계승하면 안 됨"
    # alpha 후보(evidence 불변)의 distinct 2회는 재색인을 넘어 생존
    alpha = q1(db, "SELECT COUNT(DISTINCT h.query_hash) FROM latent_hits h "
                   "JOIN latent_candidates c ON c.candidate_key = h.candidate_key "
                   "WHERE c.slug = 'split-alpha'")[0][0]
    assert alpha == 2, alpha
    print("  [ok] hit reset on marker repurpose (evidence fingerprint)")


def check_ambiguous_title(root, db):
    # 동일 candidate_title 후보 2개 → 제목으로 promote 시도하면 명시적 거부 (오적출 방지)
    p = root / "20_Concepts" / "Ambig Note.md"
    p.write_text("""---
title: Ambig Note
type: Concept
latent_split_candidate:
  - id: amb-1
    candidate_title: "Same Title"
    reason: "entry one"
    evidence: "엔트리 하나의 근거 문장이다"
    promote_condition: "distinct-context retrieval >= 2"
  - id: amb-2
    candidate_title: "Same Title"
    reason: "entry two"
    evidence: "엔트리 둘의 근거 문장이다"
    promote_condition: "distinct-context retrieval >= 2"
---

# Ambig Note

엔트리 하나의 근거 문장이다. 첫째 스팬.

엔트리 둘의 근거 문장이다. 둘째 스팬.
""", encoding="utf-8")
    reindex(root, db)
    try:
        latent.promote(db, root, "Ambig Note", "Same Title",
                       "엔트리 둘의 근거 문장이다. 둘째 스팬.", "검증")
        raise AssertionError("동명 후보 2개인데 제목 매칭이 통과됨")
    except ValueError as e:
        assert "amb-1" in str(e) and "amb-2" in str(e), e
    # 유일 매칭이면 제목 폴백 허용: id로 지정해 하나 제거 후 제목으로 승격 성공해야 함
    r = latent.promote(db, root, "Ambig Note", "amb-1",
                       "엔트리 하나의 근거 문장이다. 첫째 스팬.", "검증",
                       new_title="Entry One Node")
    assert r["status"] == "promoted", r
    r2 = latent.promote(db, root, "Ambig Note", "Same Title",
                        "엔트리 둘의 근거 문장이다. 둘째 스팬.", "검증")
    assert r2["status"] == "promoted" and r2["new_title"] == "Same Title", r2
    print("  [ok] ambiguous candidate_title guard")


def check_vault_under_claude_dir():
    # vault 자체가 .claude/worktrees/<id>/ 밑에 체크아웃된 경우에도 색인돼야 한다
    # (제외 판정은 vault-상대 경로 기준). vault 내부의 .claude 사본만 걸러진다.
    base = Path(tempfile.mkdtemp(prefix="latent_wt_"))
    vault = base / ".claude" / "worktrees" / "wt1"
    (vault / "20_Concepts").mkdir(parents=True)
    (vault / "20_Concepts" / "Solo Note.md").write_text(OTHER_MD.replace("Other Note", "Solo Note"),
                                                        encoding="utf-8")
    db = vault / "90_Engine" / "cache.db"
    try:
        stats = None
        stats, conn = indexer.index_vault(vault, db, embed=False)
        conn.close()
        assert stats["nodes_total"] == 1 and stats["nodes_new"] == 1, stats
        # vault 내부 워크트리 사본은 제외
        copy = vault / ".claude" / "worktrees" / "copy" / "20_Concepts"
        copy.mkdir(parents=True)
        copy_file = copy / "Solo Note.md"
        copy_file.write_text("# dup", encoding="utf-8")
        stats2, conn = indexer.index_vault(vault, db, embed=False)
        conn.close()
        assert stats2["nodes_total"] == 1, stats2
        # 업그레이드 경로: 제외 규칙 도입 '전'에 색인된 사본 행(파일은 실존)은
        # 존재 검사만으로는 안 지워진다 — 정책 기반 prune이 제거해야 한다.
        import uuid as uuid_mod
        from datetime import datetime as dt
        conn = indexer.init_database(db)
        conn.execute(
            "INSERT INTO nodes (node_id, file_path, title, aliases, md5_hash, last_indexed)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [uuid_mod.uuid4(), str(copy_file), "Solo Note", [], "stale", dt.now()])
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2
        conn.close()
        stats3, conn = indexer.index_vault(vault, db, embed=False)
        n_rows = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()
        assert stats3["nodes_pruned"] >= 1 and n_rows == 1, (stats3, n_rows)
        print("  [ok] vault-under-.claude indexing + stale excluded-row prune")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def check_cross_folder_title_collision(root, db):
    # 새 노드 제목은 vault 전역 링크 네임스페이스(title + 파일명 stem + aliases)와
    # 충돌하면 안 된다 — 어느 쪽과 겹쳐도 자동 적출 대신 review로 라우팅된다.
    (root / "50_Source_Summaries").mkdir(exist_ok=True)
    (root / "50_Source_Summaries" / "Existing Elsewhere.md").write_text(
        "---\ntitle: Existing Elsewhere\ntype: source-summary\n---\n\n# Existing Elsewhere\n\n요약.\n",
        encoding="utf-8")
    # stem ≠ title 인 노드: 링크 네임스페이스는 stem("Stem Foo")과 alias("Alias Target")로 키잉됨
    (root / "50_Source_Summaries" / "Stem Foo.md").write_text(
        "---\ntitle: Different Title\naliases: [Alias Target]\ntype: source-summary\n---\n\n# x\n\n요약.\n",
        encoding="utf-8")
    p = root / "20_Concepts" / "Collide Note.md"
    p.write_text("""---
title: Collide Note
type: Concept
latent_split_candidate:
  - id: col-1
    candidate_title: "Existing Elsewhere"
    reason: "title 충돌"
    evidence: "다른 계층 동명 노드와 충돌하는 근거 문장"
    promote_condition: "distinct-context retrieval >= 2"
  - id: col-2
    candidate_title: "Stem Foo"
    reason: "stem 충돌"
    evidence: "파일명 stem과 충돌하는 근거 문장"
    promote_condition: "distinct-context retrieval >= 2"
  - id: col-3
    candidate_title: "Alias Target"
    reason: "alias 충돌"
    evidence: "alias와 충돌하는 근거 문장"
    promote_condition: "distinct-context retrieval >= 2"
---

# Collide Note

본문 문단.

다른 계층 동명 노드와 충돌하는 근거 문장. 자기완결 span이다.

파일명 stem과 충돌하는 근거 문장. 자기완결 span이다.

alias와 충돌하는 근거 문장. 자기완결 span이다.
""", encoding="utf-8")
    reindex(root, db)
    for cid, quote in [
        ("col-1", "다른 계층 동명 노드와 충돌하는 근거 문장. 자기완결 span이다."),
        ("col-2", "파일명 stem과 충돌하는 근거 문장. 자기완결 span이다."),
        ("col-3", "alias와 충돌하는 근거 문장. 자기완결 span이다."),
    ]:
        r = latent.promote(db, root, "Collide Note", cid, quote, "검증")
        assert r["status"] == "routed_to_review" and r["reason"] == "title-collision", (cid, r)
    assert not (root / "20_Concepts" / "Existing Elsewhere.md").exists()
    assert not (root / "20_Concepts" / "Alias Target.md").exists()
    print("  [ok] vault-wide collision (title/stem/alias) → review")


def check_fence_quote(root, db):
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
        check_parser()
        check_index_sync(root, db)
        check_hits(root, db)
        check_hit_reset(root, db)
        check_promote(root, db)
        check_ambiguous_title(root, db)
        check_cross_folder_title_collision(root, db)
        check_fence_quote(root, db)
        check_vault_under_claude_dir()
        print("\nALL LATENT TESTS PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_all():
    """pytest 진입점 — pytest가 이 파일을 수집해도 전체 스위트가 한 테스트로 돈다.
    (check_* 헬퍼는 인자를 받아 pytest가 fixture로 오인하지 않도록 test_ 접두를 피했다.)"""
    main()


if __name__ == "__main__":
    main()
