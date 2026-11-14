"""회귀 수트의 한 갈래: 퇴출과 정돈 — 덜어 낸 줄의 기록과 처분.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
from unittest import mock

from regression.support import check, ENGINE, _hook_context, node_text, rmtree_force, ROOT, VAULT, _vault_at, _w
from osk import core, validate, write, nodefile  # noqa: E402


# ── 19c. 퇴출 기록부와 정돈 (Mechanism §9-2 12항 · §9-3) ─────────────────
def test_eviction_preservation_mcp():
    """거부→evict→MCP 노드 보존→settle. 기록 선행·no-op·거짓 실패를 잡는다.

    노드 저장 전에 처분하거나 실패한 쓰기를 닫으면 원문이 사라진다.
    ledger fsync의 응답이 불확실할 때 노드를 되돌려도 같은 손실이 생긴다.
    """
    import mcp_server as M
    from osk import evictions as ev, scope_memory as wm, approvals as A, approval_acts
    session = "regr-preservation"
    fact = "분모를 고정해야 전후 비교가 가능하다."
    initial = wm.read(VAULT, session, "00_Scope/W1")
    mem = _w(wm.replace, VAULT, session, fact + "\n- 남길 요약", initial["hash"], "00_Scope/W1")
    write.set_primary(VAULT, session, "W1", "시험 지정")
    assert mem.get("ok"), mem
    rejected = _w(wm.replace, VAULT, session, "가" * 1600, mem["hash"])
    check("보존 실험은 실제 scope 초과에서 시작한다",
          not rejected.get("ok") and rejected.get("rejected_chars") == 1600, rejected)
    trimmed = _w(wm.replace, VAULT, session, edits=[{"old_text": fact + "\n", "new_text": ""}])
    assert trimmed.get("ok") and trimmed.get("evicted"), trimmed
    rid = trimmed["evicted"]
    check("잘린 지식이 evict에 있고 scope에는 없다",
          ev.records(VAULT)[-1]["text"] == fact and fact not in wm.read(VAULT, session)["text"])
    M.search("비교의 전제")                      # 새 노드 앞의 검색(Mechanism §6-2 10항)
    created = M.create_node("regr-preserved-node", "비교의 전제", fact, "fable-5",
                            session=session, settle=rid)
    check("생성과 처분이 한 MCP 호출에서 이어진다",
          created.get("ok") and created.get("settlement", {}).get("state") == "recorded", created)
    check("성장 증거는 실제 노드 본문이다", fact in M.read_node("regr-preserved-node").get("body", ""))
    check("처분은 새 노드 제목과 원 evict를 잇는다",
          ev.records(VAULT)[-1].get("of") == rid and ev.records(VAULT)[-1].get("outcome") == "node"
          and ev.records(VAULT)[-1].get("target") == "regr-preserved-node")
    p = ROOT / created["path"]
    next_fact = "0을 해석하기 전에 양성 대조로 계측의 작동을 확인한다."
    source = ev.record_evict(VAULT, "W1", session, next_fact)["rid"]
    before, ledger = p.read_bytes(), VAULT.evictions.read_bytes()
    bad_create = M.create_node("regr-no-evict", "없는 원본", next_fact, "fable-5",
                               session=session, settle="ffffffff-ffff-7fff-8fff-ffffffffffff")
    check("없는 evict로 새 노드를 쓰지 않는다",
          not bad_create.get("ok") and not (p.parent / "regr-no-evict.md").exists()
          and VAULT.evictions.read_bytes() == ledger, bad_create)
    empty_create = M.create_node("regr-empty-evict", "빈 본문", "\n ", "fable-5",
                                 session=session, settle=source)
    check("빈 새 노드로 보존했다고 처분하지 않는다",
          not empty_create.get("ok") and not (p.parent / "regr-empty-evict.md").exists()
          and VAULT.evictions.read_bytes() == ledger, empty_create)
    for invalid_rid in ("missing", "ffffffff-ffff-7fff-8fff-ffffffffffff"):
        invalid = M.update_node(created["id"], old_text=fact, new_text=fact + next_fact, settle=invalid_rid)
        check("잘못된 형식·없는 evict는 노드와 대장 모두 쓰기 전에 거부",
              not invalid.get("ok") and p.read_bytes() == before and VAULT.evictions.read_bytes() == ledger, invalid)
    bad_anchor = M.update_node(created["id"], old_text="없는 앵커", new_text=next_fact, settle=source)
    check("노드 갱신 실패는 처분하지 않는다",
          not bad_anchor.get("ok") and VAULT.evictions.read_bytes() == ledger, bad_anchor)
    noop = M.update_node(created["id"], body=M.read_node(created["id"])["body"],
                         expect_hash=created["new_hash"], settle=source)
    check("동일 내용 재저장은 보존 완료로 위장하지 않는다",
          not noop.get("ok") and p.read_bytes() == before and VAULT.evictions.read_bytes() == ledger, noop)
    for change in ({"summary": "요약만 갱신"}, {"add_edges": {"derived-from": "W1"}},
                   {"body": "", "expect_hash": created["new_hash"]},
                   {"body": "\n" + fact + "\n ", "expect_hash": created["new_hash"]}):
        metadata_only = M.update_node(created["id"], settle=source, **change)
        check("요약·엣지·공백·본문 삭제만으로 보존 완료로 세지 않는다",
              not metadata_only.get("ok") and p.read_bytes() == before
              and VAULT.evictions.read_bytes() == ledger, metadata_only)
    with mock.patch.object(nodefile, "write_file", side_effect=OSError("node write failed")):
        failed = M.update_node(created["id"], old_text=fact, new_text=fact + next_fact, settle=source)
    check("디스크 쓰기 실패도 미처분을 유지한다",
          not failed.get("ok") and p.read_bytes() == before and VAULT.evictions.read_bytes() == ledger, failed)
    dup = ROOT / "00_Scope/W2" / p.name
    dup.parent.mkdir(parents=True, exist_ok=True)
    dup.write_bytes(before.replace(created["id"].encode(), core.new_node_id().encode()))
    try:
        ambiguous = M.update_node(created["id"], old_text=fact, new_text=fact + next_fact, settle=source)
        check("id가 유일해도 처분 target 제목이 모호하면 갱신 전에 거부한다",
              not ambiguous.get("ok") and p.read_bytes() == before
              and VAULT.evictions.read_bytes() == ledger, ambiguous)
    finally:
        dup.unlink()
    merged = M.update_node(created["id"], old_text=fact, new_text=fact + "\n" + next_fact, settle=source)
    check("기존 노드 갱신과 처분이 한 호출에서 이어진다",
          merged.get("ok") and merged.get("settlement", {}).get("state") == "recorded", merged)
    check("id로 갱신해도 target에는 실제 제목을 기록한다",
          next_fact in M.read_node(created["id"]).get("body", "")
          and ev.records(VAULT)[-1].get("target") == p.stem and ev.records(VAULT)[-1].get("outcome") == "merged")

    # append 전 실패와 append 후 fsync/응답 실패는 호출자에게 구별되지 않는다.
    append = ev.ledger_append
    for after_append in (False, True):
        claim = f"계측의 한계도 기록한다 {after_append}."
        source = ev.record_evict(VAULT, "W1", session, claim)["rid"]

        def interrupted(*args, **kwargs):
            if after_append:
                append(*args, **kwargs)
            raise OSError("settlement acknowledgement lost")

        with mock.patch.object(ev, "ledger_append", side_effect=interrupted):
            result = M.update_node(p.stem, old_text=next_fact, new_text=next_fact + "\n" + claim, settle=source)
        check("처분 응답 실패에도 노드 보존 성공을 정확히 보고한다",
              result.get("ok") and result.get("settlement", {}).get("state") == "unconfirmed"
              and claim in M.read_node(p.stem).get("body", ""), result)
        check("기록 여부를 추측하거나 보존된 노드를 되돌리지 않는다",
              any(r.get("of") == source for r in ev.records(VAULT)) == after_append)

    source = ev.record_evict(VAULT, "W1", session, "손상 대장 시험")["rid"]
    before, ledger = p.read_bytes(), VAULT.evictions.read_bytes()
    VAULT.evictions.write_bytes(ledger + b"{broken\n")
    try:
        result = M.update_node(p.stem, old_text=next_fact, new_text="새 내용", settle=source)
        check("손상 대장은 보존 요청의 검증 단계에서 거부한다",
              not result.get("ok") and p.read_bytes() == before, result)
    finally:
        VAULT.evictions.write_bytes(ledger)
    approval_acts.protect(VAULT, "00_Scope/W1", "보존 경로는 승인을 대신하지 않는다")
    approvals_before = A.ledger_path(VAULT).read_bytes()
    before = p.read_bytes()
    result = M.update_node(p.stem, old_text=next_fact, new_text=next_fact + "\n보호영역의 새 지식", settle=source)
    check("보호영역은 작업본만 바뀌고 승인본은 그대로다",
          result.get("ok") and A.state(VAULT, "00_Scope/W1") == "pending"
          and A.ledger_path(VAULT).read_bytes() == approvals_before, result)
    p.write_bytes(before)  # mini-vault 보호 시험 작업본을 원복한 뒤 해제한다.
    approval_acts.unprotect(VAULT, "00_Scope/W1", "회귀 시험 종료")
    check("훅은 기존 노드 검색과 같은 호출의 처분을 안내한다",
          "settle" in ev._exits(VAULT, "W1", "python", "engine")
          and ev._exits(VAULT, "W1", "python", "engine").index("update_node") < ev._exits(VAULT, "W1", "python", "engine").index("create_node"))


def test_evictions():
    """상한 초과 거부 **직후의 첫 성공한 쓰기**가 덜어 낸 줄을 `evict`로 남기고,
    처분은 `settle`로 남긴다. 세션 시작 훅이 오래된 것부터 K개를 싣는다.

    무엇을 망가뜨리면 실패하는가: 거부 없는 정리를 기록하면 · 거부 뒤 읽기가
    대기 표식을 지우면 · 덜어 낸 줄 대신 전문을 적거나 남은 줄을 적으면 ·
    덜어 낸 것이 없는데 적으면 · 대장 손상에도 파일을 쓰면 · settle이 없는
    evict·없는 노드·폐기의 target을 받으면 · 미처분이 오래된 것부터가 아니면 ·
    나이를 rid가 아니라 at에서 읽으면 · K를 넘겨 싣거나 밀림 경고를 뒤에 두면 ·
    스키마 검사가 settle의 of·target을 안 보면."""
    import io, importlib, re, types
    from osk import scope_memory as wm, evictions as ev, cli
    (ROOT / "00_Scope/WEvi").mkdir(exist_ok=True)
    S = "regr-evi"                        # 훅 시험: 디렉터리 이름이 곧 세션 키
    VAULT.evictions.unlink(missing_ok=True)
    now_ms = int(time.time() * 1000)

    # 오래된 미처분 넷을 대장의 **첫 구간**에 심는다 — 이후의 append가 이 위에
    # 이어지므로 스키마(parents)도 rid 단조도 그대로 선다. `at`은 일부러 오늘로
    # 두어 나이가 rid에서 나오는지 본다(계기와 같은 판정).
    old, prev = [], []
    for d in (20, 19, 18, 17):
        rid = core._make_rid(now_ms - d * 86_400_000, 0)
        old.append({"rid": rid, "parents": prev, "kind": "evict", "scope": "WEvi",
                    "session": S, "text": f"- 옛 조각 {d}", "at": core.now_iso()})
        prev = [rid]
    VAULT.evictions.parent.mkdir(parents=True, exist_ok=True)
    VAULT.evictions.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in old), encoding="utf-8")
    check("나이는 rid에서 — at이 오늘이어도 20일", ev.age_days(old[0], now_ms) == 20)
    check("미처분은 오래된 것부터",
          [r["text"] for r in ev.unsettled(VAULT, "WEvi")] == [f"- 옛 조각 {d}" for d in (20, 19, 18, 17)])
    st = ev.status(VAULT, now_ms)
    check("status: 미처분 4·가장 오래된 20일·밀림",
          st.get("WEvi") == {"unsettled": 4, "oldest_days": 20, "overdue": True}, st)

    # 거부 없는 정리는 기록하지 않는다 (Mechanism §9-2 12항)
    r = _w(wm.replace, VAULT, S, "# 머리\n- 갑 첫 줄\n- 을 둘째 줄\n- 병 셋째 줄", None, "00_Scope/WEvi")
    check("첫 쓰기", r.get("ok"), r)
    write.set_primary(VAULT, S, "WEvi", "시험 지정")
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "\n- 병 셋째 줄", "new_text": ""}])
    check("거부 없는 정리는 기록하지 않는다",
          r.get("ok") and "evicted" not in r and len(ev.records(VAULT)) == 4, r)

    # 거부 → 읽기 → 잘라서 통과 → evict. 읽기는 대기 표식을 지우지 않는다 —
    # 무엇을 자를지 보는 일이 그 사이에 있다.
    big = [{"old_text": "# 머리", "new_text": "# 머리\n- 정 " + "가" * 1600}]
    r = _w(wm.replace, VAULT, S, edits=big)
    check("상한 초과 거부", r.get("ok") is False, r)
    check("거부문이 '사라지지 않는다'를 말한다 — (2)는 지금의 의무가 아니다",
          "사라지지 않는다" in " ".join(r["violations"]), r)
    _w(wm.read, VAULT, S)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "- 갑 첫 줄\n", "new_text": ""},
                                 {"old_text": "# 머리", "new_text": "# 머리\n- 정 " + "가" * 100}])
    check("잘라서 통과", r.get("ok"), r)
    check("evict rid를 낸다", re.match(core.RID_RE, str(r.get("evicted"))), r)
    rows = ev.records(VAULT)
    check("evict 한 행이 늘었다", len(rows) == 5 and rows[-1]["kind"] == "evict", len(rows))
    check("덜어 낸 줄만 적는다 — 전문도 남은 줄도 아니다",
          rows[-1]["text"] == "- 갑 첫 줄", rows[-1]["text"])
    check("scope·session·at", rows[-1]["scope"] == "WEvi" and rows[-1]["session"] == S
          and "at" in rows[-1], rows[-1])
    check("덜어 낸 자수", r.get("evicted_chars") == len("- 갑 첫 줄"), r)
    mem = _w(wm.read, VAULT, S)["text"]
    check("저장본은 보낸 그대로", "- 갑 첫 줄" not in mem and "- 정 가" in mem, mem)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "\n- 을 둘째 줄", "new_text": ""}])
    check("성공이 표식을 지운다 — 다음 정리는 기록 없음",
          r.get("ok") and "evicted" not in r and len(ev.records(VAULT)) == 5, r)

    # 전체 치환 — 두 정본 형태의 차이. 사이의 해시 불일치 거부는 표식을 지우지 않는다
    cur = _w(wm.read, VAULT, S)
    r = _w(wm.replace, VAULT, S, cur["text"] + "\n- 무 " + "나" * 1600, cur["hash"])
    check("전체 치환 초과 거부", r.get("ok") is False, r)
    check("해시 불일치 거부", _w(wm.replace, VAULT, S, "# 머리\n- 정 새로", "sha256:00").get("ok") is False)
    r = _w(wm.replace, VAULT, S, "# 머리\n- 정 새로", cur["hash"])
    check("전체 치환의 차이가 기록된다", r.get("ok") and "evicted" in r, r)
    rows = ev.records(VAULT)
    check("치환 전 줄 중 남지 않은 줄만 — 머리는 남았으니 없다",
          rows[-1]["text"] == "- 정 " + "가" * 100, rows[-1]["text"][:30])
    # 덜어 낸 것이 없는 성공 — 기록 없음, 표식은 끝
    check("다시 거부", _w(wm.replace, VAULT, S, edits=big).get("ok") is False)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "- 정 새로", "new_text": "- 정 새로\n- 기 한 줄"}])
    check("덜어 낸 것이 없으면 기록하지 않는다",
          r.get("ok") and "evicted" not in r and len(ev.records(VAULT)) == 6, r)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "\n- 기 한 줄", "new_text": ""}])
    check("그 성공이 표식도 끝냈다", r.get("ok") and "evicted" not in r and len(ev.records(VAULT)) == 6, r)

    # 대장 손상 — 잘린 것이 기록 없이 사라질 수 없으므로 파일도 쓰지 않는다
    backup = VAULT.evictions.read_bytes()
    with open(VAULT.evictions, "a", encoding="utf-8") as f:
        f.write("{broken\n")
    check("다시 거부(손상 위)", _w(wm.replace, VAULT, S, edits=big).get("ok") is False)
    before = _w(wm.read, VAULT, S)["text"]
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "\n- 정 새로", "new_text": ""}])
    check("손상된 대장 위에서는 잘라도 쓰지 않는다",
          r.get("ok") is False and "퇴출 기록부" in " ".join(r["violations"]), r)
    check("파일이 그대로다", _w(wm.read, VAULT, S)["text"] == before)
    VAULT.evictions.write_bytes(backup)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "\n- 정 새로", "new_text": ""}])
    check("복구 뒤 같은 쓰기가 통과하고 기록된다",
          r.get("ok") and "evicted" in r and ev.records(VAULT)[-1]["text"] == "- 정 새로", r)
    live = r["evicted"]

    # 표식은 프로세스 밖에 산다(리뷰 P1) — 3회째 거부는 "접고 다음 통합으로"를
    # 지시하고 그 다음 통합은 흔히 새 프로세스에서 온다. 거부는 여기서, 잘라서
    # 통과는 **다른 프로세스**(CLI `sm write`)에서.
    r = _w(wm.replace, VAULT, S, "# 머리\n- 재시작 줄", _w(wm.read, VAULT, S)["hash"])
    check("재시작 시험 기준선", r.get("ok") and "evicted" not in r, r)
    pend = wm._pending_path(VAULT, S)
    check("다시 거부(표식 파일)", _w(wm.replace, VAULT, S, edits=big).get("ok") is False)
    check("거부가 표식 파일을 세운다 — 추적 트리 밖", pend.is_file() and ROOT not in pend.parents, pend)
    # 표식은 **이 작업 트리**의 것이다(리뷰 2차 P1) — linked worktree는 잠금
    # 자리와 세션 키를 공유하므로, 트리가 키에 없으면 다른 트리의 성공이 소비한다.
    # git 없는 mini-vault는 루트 해시가 든 임시 경로로 떨어져 저절로 갈리므로,
    # 실제 모양 — 본 저장소 A와 그 `.git`을 `commondir`로 공유하는 linked
    # worktree B — 를 파일로 세운다(git 실행 없음). 둘의 잠금 자리는 같다.
    # 운영의 ROOT는 `Vault.open()`이 정규화한 경로다 — 시험 루트도 그렇게 세운다.
    # 날것(RUNNER~1·/var)이면 A는 그 철자, B는 commondir resolve로 실경로가 된다.
    wt = Path(tempfile.mkdtemp(prefix="osk-wt-")).resolve()
    A, B = wt / "A", wt / "B"
    (A / ".git" / "worktrees" / "B").mkdir(parents=True)
    B.mkdir()
    (B / ".git").write_text(f"gitdir: {A / '.git' / 'worktrees' / 'B'}", encoding="utf-8")
    (A / ".git" / "worktrees" / "B" / "commondir").write_text("../..", encoding="utf-8")
    check("두 트리의 잠금 자리는 같다",
          core.local_lock_path("x", A) == core.local_lock_path("x", B), (A, B))
    wm._pending_set(_vault_at(A), S)
    check("A 트리에 표식", wm._pending(_vault_at(A), S))
    check("같은 git을 쓰는 B 트리에서는 A의 표식이 보이지 않는다", not wm._pending(_vault_at(B), S),
          wm._pending_path(_vault_at(B), S))
    wm._pending_clear(_vault_at(A), S)
    rmtree_force(wt)
    cur = _w(wm.read, VAULT, S)
    sub = subprocess.run(
        [sys.executable, "-m", "osk.cli", "sm", "write", "--session", S,
         "--expect-hash", cur["hash"]],
        input="# 머리".encode("utf-8"), capture_output=True, cwd=str(ENGINE),
        env={**os.environ, "PYTHONPATH": str(ENGINE), "OSK_VAULT_ROOT": str(ROOT)},
        timeout=120)
    check("새 프로세스의 첫 성공이 통과", sub.returncode == 0,
          (sub.stdout[-300:] + sub.stderr[-300:]).decode("utf-8", "replace"))
    check("새 프로세스가 덜어 낸 줄을 적는다 — 표식이 재시작을 넘었다",
          ev.records(VAULT)[-1]["kind"] == "evict" and ev.records(VAULT)[-1]["text"] == "- 재시작 줄",
          ev.records(VAULT)[-1])
    check("성공이 표식 파일을 지운다", not pend.is_file())

    # 저장본의 전후로 잰다(리뷰 P1) — 앵커 일괄의 중간 문자열은 저장된 적이 없고
    # 필터도 지나지 않았다. 옮긴 줄은 남은 것이고, 비밀값은 대장에 없어야 한다.
    r = _w(wm.replace, VAULT, S, "# 머리\n- 엑스\n- 옮길 줄\n- 남는 줄", _w(wm.read, VAULT, S)["hash"])
    check("연쇄 시험 기준선", r.get("ok"), r)
    check("다시 거부(연쇄)", _w(wm.replace, VAULT, S, edits=big).get("ok") is False)
    tok = "ghp_" + "b" * 36
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "- 엑스", "new_text": "- " + tok},
                                 {"old_text": "- " + tok, "new_text": "- 와이"},
                                 {"old_text": "\n- 옮길 줄", "new_text": ""},
                                 {"old_text": "- 남는 줄", "new_text": "- 남는 줄\n- 옮길 줄"}])
    check("연쇄 편집 통과", r.get("ok") and "evicted" in r, r)
    last = ev.records(VAULT)[-1]["text"]
    check("덜어 낸 것은 저장본의 전후 차이뿐 — 옮긴 줄은 남았고 중간 문자열은 없다",
          last == "- 엑스", last)
    check("비밀값이 대장에 없다", tok not in VAULT.evictions.read_text(encoding="utf-8"))
    # 손으로 고친 저장본에 비밀값이 있어도 대장으로 새지 않는다 — 대장은 동기화된다
    p_mem = ROOT / "00_Scope/Workbench/_scope_memory/WEvi.md"
    # `write_bytes` — `write_text`는 Windows에서 CRLF를 넣어 앵커가 어긋난다
    p_mem.write_bytes(("# 머리\n- 손편집 ghp_" + "c" * 36 + "\n- 와이\n").encode("utf-8"))
    check("다시 거부(손편집)", _w(wm.replace, VAULT, S, edits=big).get("ok") is False)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "- 손편집 ghp_" + "c" * 36 + "\n", "new_text": ""}])
    check("손편집 줄 제거 통과", r.get("ok") and "evicted" in r, r)
    check("대장의 퇴출문은 필터를 지난다", "ghp_" not in ev.records(VAULT)[-1]["text"],
          ev.records(VAULT)[-1]["text"])

    # settle — 처분은 한 일의 기록이지 하겠다는 약속이 아니다
    def bad(fn):
        try:
            fn()
            return False
        except ValueError:
            return True
    check("모르는 outcome 거부", bad(lambda: ev.settle(VAULT, live, "kept")))
    check("폐기에 target 거부", bad(lambda: ev.settle(VAULT, live, "discarded", "x")))
    check("node에 target 없음 거부", bad(lambda: ev.settle(VAULT, live, "node")))
    check("없는 노드 거부", bad(lambda: ev.settle(VAULT, live, "node", "없는-노드-제목")))
    check("없는 evict 거부", bad(lambda: ev.settle(VAULT, core._make_rid(now_ms, 99), "discarded")))
    # 파손 파일·동명은 노드가 아니다(리뷰 P2) — 후보 파일이 아니라 판독되는
    # 비모호 노드여야 한다. 아니면 증류된 적 없는 조각이 정돈 큐에서 영구히 빠진다.
    broken = ROOT / "00_Scope/W1/regr-evi-broken.md"
    broken.write_text("---\nid: [\n---\n본문\n", encoding="utf-8")
    check("파손 파일은 노드가 아니다 — 거부", bad(lambda: ev.settle(VAULT, live, "node", "regr-evi-broken")))
    # 경로형·id형은 계약 밖이다(리뷰 2차 P2) — 경로는 파싱 없이 존재만 보고 동명을
    # 우회하므로, 파손 파일을 경로로 지목하면 통과해 버린다.
    check("경로형 target 거부", bad(lambda: ev.settle(VAULT, live, "node", "00_Scope/W1/regr-evi-broken")))
    check("id형 target 거부", bad(lambda: ev.settle(VAULT, live, "node", "260801-zzzz-w1ix")))
    broken.unlink()
    # 불완전한 색인에서는 유일성을 확정하지 않는다(리뷰 3차 P2) — 다른 군집이
    # 안 읽히는 동안 이 군집의 동명 하나만 보이면 단일 node로 오판한다.
    real_scandir = os.scandir

    def blind(path):
        if str(path) == str(ROOT / "00_Scope/WEvi"):
            raise PermissionError(13, "권한 없음", str(path))
        return real_scandir(path)

    os.scandir = blind
    try:
        n_seen = len(ev.unsettled(VAULT, "WEvi"))
        try:
            ev.settle(VAULT, live, "node", "W1")
            blind_ok = False
        except ValueError as e:
            blind_ok = "관측하지 못했다" in str(e)
        check("불완전 관측에서는 settle을 적지 않는다", blind_ok)
        check("불완전 관측 거부는 아무것도 처분하지 않았다", len(ev.unsettled(VAULT, "WEvi")) == n_seen)
    finally:
        os.scandir = real_scandir
    n_before = len(ev.unsettled(VAULT, "WEvi"))
    check("거부된 settle은 아무것도 처분하지 않았다", n_before == 10, n_before)
    node = _w(write.create_node, VAULT, "regr-evi-distilled", "퇴출 조각의 증류", "본문",
              "fable-5", space="00_Scope/W1")
    check("증류 노드", node.get("ok"), node)
    dup = ROOT / "00_Scope/WEvi/regr-evi-distilled.md"
    dup.write_text(node_text("260903-zzzz-evdp", "동명"), encoding="utf-8")
    check("동명이 둘이면 거부", bad(lambda: ev.settle(VAULT, live, "node", "regr-evi-distilled")))
    dup.unlink()
    s1 = ev.settle(VAULT, live, "node", "regr-evi-distilled")
    check("settle 행", s1["kind"] == "settle" and s1["of"] == live
          and s1["target"] == "regr-evi-distilled", s1)
    par = core.effective_parents(ev.records(VAULT))
    check("settle은 그 evict를 조상으로 갖는다 (§9-3 4항)", live in core.ancestors(s1["rid"], par))
    check("처분된 것은 미처분에서 빠진다", live not in {r["rid"] for r in ev.unsettled(VAULT, "WEvi")})
    s2 = ev.settle(VAULT, live, "discarded")
    check("두 번째 settle도 산다 — 다기기 동시 처분은 손상이 아니다",
          s2["of"] == live and len([r for r in ev.records(VAULT) if r.get("of") == live]) == 2)
    s3 = ev.settle(VAULT, old[3]["rid"], "merged", "regr-evi-distilled")
    check("merged도 target을 받는다", s3["outcome"] == "merged"
          and old[3]["rid"] not in {r["rid"] for r in ev.unsettled(VAULT)})

    # 검증기 — 스키마 세그먼트
    def names(lst):
        return [x if isinstance(x, str) else list(x)[0] for x in lst]
    rep = validate.run(VAULT, cli.published_tools)
    check("퇴출 기록부 스키마가 통과에 있다",
          any("퇴출 기록부 스키마" in n for n in names(rep["pass"])), names(rep["fail"]))
    backup = VAULT.evictions.read_bytes()
    b0, b1, b2 = (core._make_rid(now_ms + 1000, i) for i in range(3))
    bad_rows = [
        {"rid": b0, "parents": core.heads(ev.records(VAULT)), "kind": "settle",
         "of": "ghost", "outcome": "burned", "at": "t"},
        {"rid": b1, "parents": [b0], "kind": "settle", "of": live,
         "outcome": "discarded", "target": "x", "at": "t"},
        {"rid": b2, "parents": [b1], "kind": "evict", "scope": "WEvi", "at": "t"},
    ]
    with open(VAULT.evictions, "a", encoding="utf-8") as f:
        for row in bad_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    rep = validate.run(VAULT, cli.published_tools)
    seg = next((list(f.values())[0] for f in rep["fail"]
                if "퇴출 기록부 스키마" in list(f)[0]), None)
    check("스키마 세그먼트가 위반을 적발", seg is not None, names(rep["fail"]))
    if seg:
        joined = " | ".join(seg)
        for want in ("미지의 evict", "미정의 outcome", "target은 폐기에는 없고",
                     "필수 필드 누락 session"):
            check(f"스키마 적발: {want}", want in joined, joined)
    VAULT.evictions.write_bytes(backup)

    # 훅 문안 — K개·밀림 경고·경유 노드·출구. 미처분은 옛 20·19·18 + 갑·정가·
    # 재시작·엑스·손편집 = 8
    transit = ROOT / "00_Scope/Workbench/transit/regr-evi-transit.md"
    transit.write_text(node_text("260903-zzzz-evtr", "경유 노드"), encoding="utf-8")
    banner, block = ev.hook_block(VAULT, "WEvi", "PY", "ENG", now_ms)
    check("밀림 경고가 있다", banner.startswith("[osk 정돈이 밀렸다") and "20일" in banner, banner)
    check("오래된 것부터 K=3건만", block.count("\n- `") == 3 and "- 옛 조각 20" in block
          and "- 옛 조각 18" in block and "- 옛 조각 17" not in block, block)
    check("나머지 건수를 알린다", "외 5건" in block, block)
    check("경유 노드를 싣는다", "[[regr-evi-transit]]" in block, block)
    check("settle 명령·출구·벽 아님", "tidy settle" in block and "PYTHONPATH=ENG PY" in block
          and "벽이 아니다" in block and "00_Scope/WEvi" in block, block)
    # 경유 노드의 출구는 넷이다 — 착지만 말하면 내용을 옮겨 둔 노드를 다시 착지시킨다(10-04)
    check("경유 노드의 네 출구와 폐기 명령을 싣는다",
          "출구 넷" in block and "tidy discard" in block and "git 이력" in block, block)

    # 훅 실행 — 경고가 맨 앞, 기억, 정돈 블록 순. 결속은 위 첫 쓰기가 세웠고,
    # git 밖의 cwd는 디렉터리 이름으로 접히므로 이름이 곧 세션 키다.
    hooks = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
    sys.path.insert(0, str(hooks))
    hook = importlib.import_module("claude_session_start")
    cwd = Path(tempfile.mkdtemp(prefix="osk-")) / S
    cwd.mkdir()

    def hook_run():
        buf = io.BytesIO()
        real_in, real_out = sys.stdin, sys.stdout
        try:
            # 호출마다 다른 세션 시작이다 — 같은 입력의 되풀이는 이중 등록으로 가려진다.
            sys.stdin = io.StringIO(json.dumps({"session_id": "regr-evi-hook", "cwd": str(cwd),
                                                "turn_id": os.urandom(8).hex()}))
            sys.stdout = types.SimpleNamespace(buffer=buf)
            hook.main()
        finally:
            sys.stdin, sys.stdout = real_in, real_out
        return _hook_context(buf.getvalue(), "SessionStart")

    out = hook_run()
    mem = _w(wm.read, VAULT, S)["text"].strip()
    check("훅: 밀림 경고가 맨 앞", out.startswith("[osk 정돈이 밀렸다"), out[:80])
    check("훅: 기억 전문이 있다", mem and mem in out, out[:200])
    check("훅: 정돈 블록이 기억 뒤에", out.index("[osk scope 기억") < out.index("[osk 정돈 —"), out[:200])
    check("훅: 처분 명령에 실제 인터프리터", sys.executable in out and "tidy settle" in out, out[-300:])
    # 호스트 한도(Kiro 3천 자·Claude Code 1만 자) — 넘치면 블록째 접는다(§9-3 1항). 밀림 경고는
    # 접지 않고, 접은 정돈은 건수와 overview include 한 줄로 남는다.
    real_budget = hook._budget
    try:
        hook._budget = lambda env, host=None: 1200
        small = hook_run()
    finally:
        hook._budget = real_budget
    check("한도: 예산 안에 싣는다", len(small) <= 1200, len(small))
    check("한도: 밀림 경고는 접지 않고 맨 앞", small.startswith("[osk 정돈이 밀렸다"), small[:80])
    check("한도: 정돈은 블록째 접고 건수·조회 경로를 남긴다",
          "[osk 정돈 —" not in small and "[osk 접음" in small and "퇴출 8건(가장 오래된" in small
          and "경유 노드" in small and '"tidy"' in small and f'overview(session="{S}"' in small, small[-500:])
    r = _w(wm.replace, VAULT, S, "", _w(wm.read, VAULT, S)["hash"])
    check("비움", r.get("ok"), r)
    out2 = hook_run()
    check("훅: 기억이 비어도 정돈을 싣는다",
          "[osk 정돈 —" in out2 and "[osk scope 기억" not in out2, out2[:120])

    # CLI — list · prompt · settle · status
    def run(argv):
        out, buf = {}, io.BytesIO()
        real_emit, real_out = cli._emit, sys.stdout
        try:
            cli._emit = out.update
            sys.stdout = types.SimpleNamespace(buffer=buf)
            try:
                cli.main(argv)
            except SystemExit as e:
                out["exit"] = e.code
        finally:
            cli._emit, sys.stdout = real_emit, real_out
        return out, buf.getvalue().decode("utf-8")

    o, _ = run(["tidy", "list", "--scope", "WEvi"])
    check("tidy list: 미처분 8건·현황", len(o.get("items", [])) == 8
          and o["status"]["WEvi"]["unsettled"] == 8, o.get("status"))
    _, p = run(["tidy", "prompt"])
    # 전용 세션은 키를 흉내 내지 않고 착지를 명시한다(Mechanism §9-3 3항)
    check("tidy prompt: 전용 세션·착지 명시·전 항목", "전용 정돈 세션" in p
          and 'space="00_Scope/WEvi"' in p and f'session="{S}"' not in p
          and p.count("\n- `") == 8, p[:200])
    _, p2 = run(["tidy", "prompt", "--scope", "NoSuch"])
    check("tidy prompt: 없는 scope는 있는 scope를 알린다", "WEvi" in p2 and "없다" in p2, p2)
    o, _ = run(["tidy", "settle", "no-such-rid", "discarded"])
    check("tidy settle: 없는 evict는 중단", str(o.get("exit", "")).startswith("[중단]"), o)
    target = ev.unsettled(VAULT, "WEvi")[0]["rid"]
    o, _ = run(["tidy", "settle", target, "discarded"])
    check("tidy settle: 처분 등재", o.get("ok") and re.match(core.RID_RE, str(o.get("rid")))
          and o["of"] == target, o)
    check("처분 뒤 미처분 7건", len(ev.unsettled(VAULT, "WEvi")) == 7)
    sio, real_out = io.StringIO(), sys.stdout
    try:
        sys.stdout = sio
        cli.main(["status"])
    finally:
        sys.stdout = real_out
    stj = json.loads(sio.getvalue())
    check("status에 scope별 미처분·나이", stj.get("evictions", {}).get("WEvi", {}).get("unsettled") == 7
          and stj["evictions"]["WEvi"]["overdue"] is True, stj.get("evictions"))
    # 한 조각이 여러 노드로 갔으면 대상마다 settle을 하나씩, 사유와 함께 적는다
    second = ev.unsettled(VAULT, "WEvi")[0]["rid"]
    o, _ = run(["tidy", "settle", second, "merged", "--target", "W1",
                "--target", "regr-evi-transit", "--reason", "두 노드로 나뉘어 옮겨졌다"])
    rows = [r for r in core.ledger_read(VAULT.evictions)
            if r.get("kind") == "settle" and r.get("of") == second]
    check("tidy settle: 대상마다 settle 하나와 사유",
          o.get("ok") and len(o.get("records", [])) == 2
          and {r.get("target") for r in rows} == {"W1", "regr-evi-transit"}
          and all(r.get("reason") == "두 노드로 나뉘어 옮겨졌다" for r in rows), o)
    third = ev.unsettled(VAULT, "WEvi")[0]["rid"]
    o, _ = run(["tidy", "settle", third, "merged", "--target", "W1", "--target", "regr-없는-노드"])
    check("tidy settle: 대상 하나라도 없으면 아무것도 적지 않는다",
          str(o.get("exit", "")).startswith("[중단]")
          and not [r for r in core.ledger_read(VAULT.evictions)
                   if r.get("kind") == "settle" and r.get("of") == third], o)

    # 퇴출이 0이어도 경유 노드는 독립적으로 정돈한다.
    for r in ev.unsettled(VAULT):
        ev.settle(VAULT, r["rid"], "discarded")
    check("퇴출 0에도 경유 정돈 프롬프트", "[[regr-evi-transit]]" in ev.tidy_prompt(VAULT, None, "PY", "ENG"))
    out3 = hook_run()
    check("훅: 퇴출 0에도 경유를 싣고 밀림 경고는 없다",
          "[[regr-evi-transit]]" in out3 and "[osk 정돈이 밀렸다" not in out3, out3[:120])
    check("status는 미처분 없는 scope를 싣지 않는다", ev.status(VAULT) == {}, ev.status(VAULT))

    # 폐기 — 경유 노드만, 들어오는 참조·scope 기억 언급이 없을 때, 확인받은 판만 지운다
    o, _ = run(["tidy", "discard", "W1"])
    check("tidy discard: 경유 노드가 아니면 중단", str(o.get("exit", "")).startswith("[중단]")
          and (ROOT / "00_Scope/W1/W1.md").exists(), o)
    ref = _w(write.create_node, VAULT, "regr-evi-cites-transit", "s", "[[regr-evi-transit]]를 본다.",
             "fable-5", space="00_Scope/W1")
    # id로 불러도 실제 제목으로 적힌 참조·기억을 찾는다(PR #135 리뷰 P1)
    for handle in ("regr-evi-transit", "260903-zzzz-evtr"):
        o, _ = run(["tidy", "discard", handle])
        check(f"tidy discard: 들어오는 참조가 있으면 중단({handle})",
              "regr-evi-cites-transit" in str(o.get("exit", "")) and transit.exists(), o)
    (ROOT / ref["path"]).unlink()
    from osk import scope_memory as sm
    mention = sm.sm_dir(VAULT) / "regr-evi-mention.md"
    mention.write_text("경유 regr-evi-transit 참고\n", encoding="utf-8")
    for handle in ("regr-evi-transit", "260903-zzzz-evtr"):
        o, _ = run(["tidy", "discard", handle])
        check(f"tidy discard: scope 기억이 가리키면 중단({handle})",
              "regr-evi-mention" in str(o.get("exit", "")) and transit.exists(), o)
    mention.unlink()
    # 제목에 `.md`가 든 노드(`X.md.md`)의 `[[X.md]]` 참조도 그래프의 해석으로 잡는다
    dot = ROOT / "00_Scope/Workbench/transit/regr-evi-dot.md.md"
    dot.write_text(node_text("260903-zzzz-evdt", "제목에 .md가 든 경유 노드"), encoding="utf-8")
    ref2 = _w(write.create_node, VAULT, "regr-evi-cites-dot", "s", "[[regr-evi-dot.md]]를 본다.",
              "fable-5", space="00_Scope/W1")
    o, _ = run(["tidy", "discard", "regr-evi-dot.md"])
    check("tidy discard: 제목에 .md가 든 노드의 참조도 잡는다",
          "regr-evi-cites-dot" in str(o.get("exit", "")) and dot.exists(), o)
    (ROOT / ref2["path"]).unlink()
    dot.unlink()
    o, _ = run(["tidy", "discard", "regr-evi-transit"])
    plan = o.get("plan", {})
    check("tidy discard: 확인 없이는 지울 판만 알린다",
          o.get("ok") is False and plan.get("hash") == core.sha256_file(transit)
          and transit.exists(), o)
    o, _ = run(["tidy", "discard", "regr-evi-transit", "--confirm", "sha256:" + "0" * 64])
    check("tidy discard: 확인한 판이 아니면 중단",
          str(o.get("exit", "")).startswith("[중단]") and transit.exists(), o)
    before = transit.read_bytes()
    o, _ = run(["tidy", "discard", "260903-zzzz-evtr", "--confirm", plan.get("hash", "")])
    copy = Path(o["copy"]) if o.get("copy") else None
    check("tidy discard: 확인받은 판을 지우고 이 기기에 사본을 남긴다",
          o.get("ok") and o.get("discarded", {}).get("title") == "regr-evi-transit"
          and not transit.exists() and copy is not None and copy.read_bytes() == before, o)
    if copy is not None:
        copy.unlink(missing_ok=True)
    # 사본 이름은 확인받은 판의 해시다(#138 리뷰 P1). 손편집한 id가 경로 문자를 품어도
    # vault 밖을 쓰지 않고, 같은 id의 다른 판이 앞 사본을 덮지 않는다.
    escape = Path(os.path.normpath(core.local_lock_path("osk-discarded-x/../../../regr-evi-outside.md", VAULT.root)))
    escape.write_text("관련 없는 파일\n", encoding="utf-8")
    copies = []
    for n, body in enumerate(("첫 판", "둘째 판"), 1):
        transit.write_text(node_text("x/../../../regr-evi-outside", "경로 문자를 품은 id", body),
                           encoding="utf-8")
        h = run(["tidy", "discard", "regr-evi-transit"])[0].get("plan", {}).get("hash", "")
        o, _ = run(["tidy", "discard", "regr-evi-transit", "--confirm", h])
        copies.append(Path(o["copy"]) if o.get("copy") else None)
        check(f"tidy discard: 사본은 확인한 판의 해시 이름으로 잠금 자리에 남는다({n})",
              o.get("ok") and not transit.exists() and copies[-1]
              == core.local_lock_path("osk-discarded-" + h.removeprefix("sha256:") + ".md", VAULT.root), o)
    check("tidy discard: 경로 문자를 품은 id가 vault 밖 파일을 덮지 않는다",
          escape.read_text(encoding="utf-8") == "관련 없는 파일\n", escape)
    check("tidy discard: 같은 id의 다른 판이 앞 사본을 덮지 않는다",
          None not in copies and copies[0] != copies[1]
          and "첫 판" in copies[0].read_text(encoding="utf-8"), copies)
    escape.unlink(missing_ok=True)
    for c in copies:
        if c is not None:
            c.unlink(missing_ok=True)

    transit.unlink(missing_ok=True)
    check("두 큐가 비어야 정돈할 것이 없다", "정돈할 것이 없다" in ev.tidy_prompt(VAULT, None, "PY", "ENG"))
    shutil.rmtree(cwd.parent, ignore_errors=True)
