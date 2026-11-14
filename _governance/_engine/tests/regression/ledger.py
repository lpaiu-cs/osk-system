"""회귀 수트의 한 갈래: 대장과 인과 — rid, 갈래와 봉합, 행 경계, 손상.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from unittest import mock

from regression.support import (
    check, ENGINE, MINI, node_text, _raises, rmtree_force, ROOT, VAULT, _vault_at, _w, wipe_sig
)
from osk import cli, core, graph, validate, write, topology  # noqa: E402
import osk.signatures as S  # noqa: E402


def raw_append(rec: dict):
    """대장 계약을 우회한 직접 주입 — 공격·병합 산물 모사 전용."""
    with open(VAULT.signatures, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def sig_status(node_id, path=None):
    """구 signatures.status의 관측을 core+헬퍼로 재현한다 — 서명 제도는
    폐지됐지만 그 관측을 떠받치던 **인과 DAG 기계**(effective_parents·
    causal_maxima·unresolved_nodes·id 해석)는 그대로 존속하며, 이 재현으로
    그 기계를 signatures 모듈 없이 계속 소진한다. SIGNATURES는 이 수트에서
    임의 append가 가능한 임시 대장으로 쓰인다."""
    recs = core.ledger_read(VAULT.signatures)
    if node_id in core.unresolved_nodes(recs):
        return "unsigned"
    m = core.causal_maxima(recs, node_id)
    if len(m) != 1 or m[0].get("kind") not in ("sign", "restore"):
        return "unsigned"
    r = m[0]
    hit = None
    for c in (path, r.get("path")):
        c = core.resolve_in_root(VAULT, c) if c else None
        if c is not None and c.exists() and S.id_of(c) == node_id:
            hit = c
            break
    if hit is None:
        hit = graph.locate_by_id(VAULT, node_id)
        if hit is None:
            return "unsigned"
    try:
        return "signed" if core.sha256_file(hit) == r.get("hash") else "unsigned"
    except OSError:
        return "unsigned"


def write_case(no: str, **kw):
    d = {"case_no": no, "status": "adjudicated", "parties": [],
         "docketed_at": "2026-08-02T10:00:00+09:00", "pre_sign": {},
         "verdict": "기각", "verdict_at": "2026-08-02T11:00:00+09:00",
         "applied": "회복", "schema_version": 1}
    d.update(kw)
    body = "\n".join(
        f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict, type(None))) else v}"
        for k, v in d.items())          # None → yaml null (파이썬 repr 누출 방지)
    (VAULT.ledger / "case" / f"{no}.md").write_text(body + "\n\n본문\n", encoding="utf-8")


# ── 1. rid 단조: 프로세스 경계 + 병합 후 물리 순서 무관(최대값이 바닥) ────
def test_rid_monotone():
    with tempfile.TemporaryDirectory() as td:
        led = Path(td) / "l.jsonl"
        rids = []
        with mock.patch("time.time", return_value=1754000000.0):  # ms 고정
            for i in range(100):
                # 매 호출이 파일에서 최대 rid를 읽는다 = 프로세스 재시작 등가
                r = core.ledger_append(led, {"kind": "sign", "node": f"x{i}",
                                             "hash": "h", "path": "p"})
                r2 = core.ledger_append(led, {"kind": "unsign", "node": f"x{i}",
                                              "hash": "h", "path": "p"})
                rids += [r["rid"], r2["rid"]]
        check("rid 단조(같은 ms 200연속·재시작 등가)", rids == sorted(rids))
        recs = core.ledger_read(led)
        check("parents 인과 사슬 연속", all(
            recs[i]["parents"] == [recs[i - 1]["rid"]] for i in range(1, len(recs))))
        bad = sum(1 for i in range(100)
                  if core.causal_maxima(recs, f"x{i}")[0]["kind"] != "unsign")
        check("sign→unsign 인과 판정 오판 0/100", bad == 0, f"{bad}/100")

        # 병합 산물: 물리 마지막 행이 최대 rid가 아니어도 최대값이 바닥
        led2 = Path(td) / "m.jsonl"
        now_ms = int(1754000000.0 * 1000)
        hi = core._make_rid(now_ms + 60_000, 7)      # 미래 ms(다른 기기 시계 앞섬)
        lo = core._make_rid(now_ms - 60_000, 3)
        for rid in (hi, lo):                          # 물리 순서: hi 먼저, lo가 마지막 행
            with open(led2, "a", encoding="utf-8") as f:
                f.write(json.dumps({"rid": rid, "kind": "sign", "node": "m",
                                    "path": "p", "hash": "h"}) + "\n")
        with mock.patch("time.time", return_value=1754000000.0):
            r = core.ledger_append(led2, {"kind": "sign", "node": "m",
                                          "path": "p", "hash": "h"})
        check("병합 후 rid 바닥=정본 최대값(마지막 행 아님)",
              core.rid_key(r["rid"]) > core.rid_key(hi) > core.rid_key(lo))


# ── 2. 같은 ms라도 인과 사슬이면 comparable → signed ─────────────────────
def test_same_ms_chain_signed():
    wipe_sig()
    node = ROOT / "00_Scope/W1/regr-chain.md"
    node.write_text(node_text("260802-zzzz-rg01", "사슬"), encoding="utf-8")
    with mock.patch("time.time", return_value=1754000100.0):
        core.ledger_append(VAULT.signatures, {
            "kind": "sign", "node": "260802-zzzz-rg01",
            "path": str(node.relative_to(ROOT)), "hash": "sha256:stale"})
        core.ledger_append(VAULT.signatures, {
            "kind": "sign", "node": "260802-zzzz-rg01",
            "path": str(node.relative_to(ROOT)), "hash": core.sha256_file(node)})
    recs = core.ledger_read(VAULT.signatures)
    same = core.rid_parts(recs[0]["rid"])[0] == core.rid_parts(recs[1]["rid"])[0]
    check("같은 ms 사슬 성립(전제)", same)
    check("같은 ms라도 인과 사슬이면 signed(비교 가능)",
          sig_status("260802-zzzz-rg01", node) == "signed")


# ── 3. 인과 분기 → fail-closed → 모든 head 봉합 재서명으로 해소 ──────────
def test_fork_failclosed_and_reseal():
    node = ROOT / "00_Scope/W1/regr-chain.md"     # 2번의 상태를 이어받는다
    h = core.sha256_file(node)
    fork_rid = core._make_rid(int(1754000100.0 * 1000), 0xF00)
    raw_append({"rid": fork_rid, "parents": [], "kind": "sign",
                "node": "260802-zzzz-rg01", "path": str(node.relative_to(ROOT)),
                "hash": h, "at": core.now_iso()})     # 다른 기기 유래 뿌리(병합 산물)
    check("분기(비교 불능) → unsigned fail-closed",
          sig_status("260802-zzzz-rg01", node) == "unsigned")
    check("분기 상태는 판정 불가로 표면화(fail-closed)",
          "260802-zzzz-rg01" in core.unresolved_nodes(
              core.ledger_read(VAULT.signatures)))
    r = core.ledger_append(VAULT.signatures, {
        "kind": "sign", "node": "260802-zzzz-rg01",
        "path": str(node.relative_to(ROOT)), "hash": h,
        "reason": "재서명 — 분기 해소"})
    check("재서명이 모든 head를 봉합(parents 2개)", len(r["parents"]) == 2,
          r["parents"])
    check("재서명 후 유일 극대 → signed",
          sig_status("260802-zzzz-rg01", node) == "signed")


# ── 3b. 앵커 이후 parents-부재 기록은 가짜 인과를 얻지 못한다 (검토 blocker) ──
def test_anchor_no_order_fallback():
    wipe_sig()
    node = ROOT / "00_Scope/W1/regr-anchor.md"
    node.write_text(node_text("260802-zzzz-rg10", "앵커"), encoding="utf-8")
    h = core.sha256_file(node)
    rel = str(node.relative_to(ROOT))
    ms = int(1754000300.0 * 1000)
    L1 = core._make_rid(ms, 0)                       # 유산 sign (parents 없음)
    raw_append({"rid": L1, "kind": "sign", "node": "260802-zzzz-rg10",
                "path": rel, "hash": "sha256:stale", "at": core.now_iso()})
    with mock.patch("time.time", return_value=1754000300.5):
        U = core.ledger_append(VAULT.signatures, {                   # 앵커 = 해제
            "kind": "unsign", "node": "260802-zzzz-rg10", "path": rel,
            "hash": "sha256:stale", "reason": "사용자 해제"})
    check("앵커가 유산 head를 봉합", U["parents"] == [L1], U["parents"])
    B1 = core._make_rid(ms, 5)      # 구 엔진이 다른 클론에서 쓴 행(병합 유입)
    raw_append({"rid": B1, "kind": "sign", "node": "260802-zzzz-rg10",
                "path": rel, "hash": h, "at": core.now_iso()})
    recs = core.ledger_read(VAULT.signatures)
    check("앵커 이후 parents-부재는 고립 루트(파일 순서 fallback 금지)",
          core.effective_parents(recs)[B1] == [], core.effective_parents(recs)[B1])
    check("해제가 서명으로 뒤집히지 않는다(fail-closed)",
          sig_status("260802-zzzz-rg10", node) == "unsigned")
    check("판정 불가 노드로 표면화",
          "260802-zzzz-rg10" in core.unresolved_nodes(recs))
    r = core.ledger_append(VAULT.signatures, {
        "kind": "sign", "node": "260802-zzzz-rg10", "path": rel, "hash": h,
        "reason": "사용자 재서명 — 유입 분기 봉합"})
    check("재서명이 유입 분기까지 봉합(해소 가능성 보존)",
          set(r["parents"]) == {U["rid"], B1}, r["parents"])
    check("봉합 후 signed", sig_status("260802-zzzz-rg10", node) == "signed")


# ── 3c. 순환·자기 참조·전방 참조는 잘려 항상 비순환 + 해소 가능 ──────────
def test_cycle_normalization():
    wipe_sig()
    node = ROOT / "00_Scope/W1/regr-cycle.md"
    node.write_text(node_text("260802-zzzz-rg11", "순환"), encoding="utf-8")
    h, rel = core.sha256_file(node), str(node.relative_to(ROOT))
    ms = int(1754000400.0 * 1000)
    A, B, X = (core._make_rid(ms, i) for i in (0, 1, 2))
    for rid, parents in ((A, [B]), (B, [A]), (X, [X])):   # 순환 + self-parent
        raw_append({"rid": rid, "parents": parents, "kind": "sign",
                    "node": "260802-zzzz-rg11", "path": rel, "hash": h,
                    "at": core.now_iso()})
    recs = core.ledger_read(VAULT.signatures)
    par = core.effective_parents(recs)
    check("자기 참조·전방 참조 간선 절단(A→B, X→X)",
          par[A] == [] and par[X] == [], par)
    check("후방 참조는 정상 인과로 보존(B→A)", par[B] == [A], par)

    def reachable(start):            # 순환이면 무한 루프 — 유한 종료가 곧 비순환
        seen, stack = set(), [start]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(par.get(x, []))
        return seen
    check("대장은 언제나 비순환(자기 조상 없음)",
          all(rid not in reachable(rid) - {rid} for rid in par))
    maxima = core.causal_maxima(recs, "260802-zzzz-rg11")
    check("순환 잔재에서도 기록이 소거되지 않는다(극대 0 아님)",
          len(maxima) >= 1, len(maxima))
    check("판정 불가로 표면화(극대≠1)",
          "260802-zzzz-rg11" in core.unresolved_nodes(recs))
    check("손상은 아니다(구조는 온전)", not core.ledger_damage(recs))
    r = core.ledger_append(VAULT.signatures, {
        "kind": "sign", "node": "260802-zzzz-rg11", "path": rel, "hash": h,
        "reason": "재서명 — 순환 잔재 봉합"})
    check("재서명이 남은 head 전부를 봉합 → 해소",
          set(r["parents"]) == {B, X}
          and sig_status("260802-zzzz-rg11", node) == "signed", r["parents"])


# ── 3d. 구조 손상(rid 부재·형식·중복)은 표면화되고 append를 거부한다 ─────
def test_structural_damage():
    for bad, label in (
        [{"kind": "sign", "node": "n-d", "path": "p", "hash": "h"}, "rid 부재"],
        [{"rid": "260802-114u-w9vj", "kind": "sign", "node": "n-d",
          "path": "p", "hash": "h"}, "rid 형식 위반"],
    ):
        wipe_sig()
        raw_append(bad)
        recs = core.ledger_read(VAULT.signatures)
        check(f"손상 표면화: {label}", bool(core.ledger_damage(recs)))
        check(f"손상 노드 fail-closed: {label}",
              "n-d" in core.damaged_nodes(recs)
              and "n-d" in core.unresolved_nodes(recs))
        check(f"손상 대장에는 append 거부: {label}",
              _raises(lambda: core.ledger_append(VAULT.signatures, {
                  "kind": "sign", "node": "n-x", "path": "p", "hash": "h"}))())
    wipe_sig()                                   # 중복 rid
    dup = core._make_rid(int(1754000500.0 * 1000), 0)
    for node_id in ("n-e", "n-f"):
        raw_append({"rid": dup, "kind": "sign", "node": node_id,
                    "path": "p", "hash": "h", "at": core.now_iso()})
    recs = core.ledger_read(VAULT.signatures)
    check("중복 rid 표면화", any("중복" in d for d in core.ledger_damage(recs)))
    check("중복 rid 연루 노드 전부 fail-closed",
          {"n-e", "n-f"} <= core.damaged_nodes(recs))


# ── 3e. rid 없는 해제가 삼켜지지 않는다 (fail-open 방지) ────────────────
def test_ridless_unsign_not_swallowed():
    wipe_sig()
    node = ROOT / "00_Scope/W1/regr-ridless.md"
    node.write_text(node_text("260802-zzzz-rg12", "삼킴"), encoding="utf-8")
    rel = str(node.relative_to(ROOT))
    core.ledger_append(VAULT.signatures, {
        "kind": "sign", "node": "260802-zzzz-rg12", "path": rel,
        "hash": core.sha256_file(node)})
    check("정상 서명은 signed", sig_status("260802-zzzz-rg12", node) == "signed")
    raw_append({"kind": "unsign", "node": "260802-zzzz-rg12", "path": rel,
                "hash": core.sha256_file(node), "at": core.now_iso()})
    check("rid 없는 해제는 무시되지 않고 미서명으로 떨어진다",
          sig_status("260802-zzzz-rg12", node) == "unsigned")


# ── 3f. 경로 봉쇄·KST 고정 ──────────────────────────────────────────────
def test_root_confinement_and_kst():
    for bad in ("../outside/evil.md", "/etc/passwd", "00_Scope/../../etc/hosts"):
        check(f"vault 밖 경로 해석 거부: {bad}", core.resolve_in_root(VAULT, bad) is None)
    inside = core.resolve_in_root(VAULT, "00_Scope/W1")
    check("vault 안 경로는 해석", inside is not None and inside.exists())
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); from osk import core; from osk.vault import Vault; VAULT = Vault.open(); print(core.now_kst())"
         % str(ENGINE)],
        capture_output=True, text=True,
        env=dict(os.environ, TZ="America/New_York", OSK_VAULT_ROOT=str(MINI)))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    want = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    check("다른 시간대 기기에서도 (KST) 표기가 실제 KST",
          r.stdout.strip().startswith(want[:14]), (r.stdout.strip(), want))


# ── 5. 경로 재사용 공격 — id가 정본 ─────────────────────────────────────
def test_path_reuse():
    wipe_sig()
    a = ROOT / "00_Scope/W1/regr-moved.md"
    b = ROOT / "00_Scope/W1/regr-dest.md"
    try:
        a.write_text(node_text("260802-zzzz-rg02"), encoding="utf-8")
        core.ledger_append(VAULT.signatures, {
            "kind": "sign", "node": "260802-zzzz-rg02",
            "path": str(a.relative_to(ROOT)), "hash": core.sha256_file(a)})
        shutil.move(str(a), str(b))                                    # 이동
        a.write_text(node_text("260802-zzzz-dcoy", "미끼"), encoding="utf-8")
        check("이동+경로 재사용에도 signed(id 정본)",
              sig_status("260802-zzzz-rg02") == "signed")
        check("미끼 id는 unsigned", sig_status("260802-zzzz-dcoy") == "unsigned")
    finally:
        for p in (a, b):
            p.unlink(missing_ok=True)


# ── 6. (구판 강등 시험 폐지 — replaces·강등 기제는 2술어 개정에서 제거됐다.
#        개정은 같은 id의 제자리 갱신이므로 후계·강등 자체가 성립하지 않는다.) ──


# ── 7. 순수 이동·개명의 재허브 감지 ─────────────────────────────────────
def test_fingerprint_move():
    import mcp_server
    a = ROOT / "00_Scope/W1/regr-fp-a.md"
    b = ROOT / "00_Scope/W1/regr-fp-b.md"
    try:
        a.write_text(node_text("260802-zzzz-rg05"), encoding="utf-8")
        fp1 = mcp_server._vault_fingerprint()
        os.rename(a, b)                                # mtime·크기 불변 이동
        check("순수 이동도 fingerprint 변화", fp1 != mcp_server._vault_fingerprint())
    finally:
        for p in (a, b):
            p.unlink(missing_ok=True)


# ── 9. conflicts 적격 — 열린 사건 또는 존치 상호+실재 존치 사건 결속 ─────
def test_conflicts_semantics():
    write_case("CASE-2026-9001", status="docketed", verdict=None,
               parties=["260802-zzzz-rga1"])          # regr-ca가 당사자
    write_case("CASE-2026-9002", parties=["260802-zzzz-rgb1"])     # adjudicated(기각)
    write_case("CASE-2026-9003", verdict="존치",
               parties=["260802-zzzz-rgm1", "260802-zzzz-rgm2"])
    write_case("CASE-2026-9004", verdict="존치",
               parties=["260802-zzzz-rgm5", "260802-zzzz-rgm6"])
    VAULT.pins.write_text("", encoding="utf-8")                # 대장 파일(사건 아님)
    files = {
        "regr-ca": node_text("260802-zzzz-rga1", "열린 사건", "본문",
                             'conflicts: "[[CASE-2026-9001]]"\n'),
        "regr-cb": node_text("260802-zzzz-rgb1", "종결 사건", "본문",
                             'conflicts: "[[CASE-2026-9002]]"\n'),
        "regr-cc": node_text("260802-zzzz-rgc1", "대장 파일", "본문",
                             'conflicts: "[[pins]]"\n'),
        "regr-cd": node_text("260802-zzzz-rgd1", "비당사자", "본문",
                             'conflicts: "[[CASE-2026-9001]]"\n'),
        "regr-cm1": node_text("260802-zzzz-rgm1", "존치 상호", "본문",
                              'conflicts: "[[regr-cm2]]"\n'),
        "regr-cm2": node_text("260802-zzzz-rgm2", "존치 상호", "본문",
                              'conflicts: "[[regr-cm1]]"\n'),
        "regr-cm3": node_text("260802-zzzz-rgm3", "사건 없는 상호", "본문",
                              'conflicts: "[[regr-cm4]]"\n'),
        "regr-cm4": node_text("260802-zzzz-rgm4", "사건 없는 상호", "본문",
                              'conflicts: "[[regr-cm3]]"\n'),
        "regr-cm5": node_text("260802-zzzz-rgm5", "편방향", "본문",
                              'conflicts: "[[regr-cm6]]"\n'),
        "regr-cm6": node_text("260802-zzzz-rgm6", "편방향 대상", "본문"),
    }
    paths = []
    try:
        for stem, text in files.items():
            p = ROOT / f"00_Scope/W1/{stem}.md"
            p.write_text(text, encoding="utf-8")
            paths.append(p)
        errs = topology.topology_check(VAULT, graph.Index(VAULT))
        check("열린(docketed) 사건 참조는 적격",
              not any("regr-ca" in e for e in errs), [e for e in errs if "regr-ca" in e])
        check("종결(adjudicated) 사건 참조는 부적격",
              any("regr-cb" in e and "부적격" in e for e in errs), errs)
        check("비당사자의 열린 사건 참조는 부적격(헌법 12조 5항)",
              any("regr-cd" in e and "당사자" in e for e in errs), errs)
        check("사건 아닌 대장 파일(pins)은 부적격",
              any("regr-cc" in e for e in errs))
        check("존치 상호+실재 존치 사건 결속은 적격",
              not any("regr-cm1" in e or "regr-cm2 →" in e for e in errs),
              [e for e in errs if "regr-cm1" in e])
        check("사건 없는 상호 존치는 부적격",
              any("regr-cm3" in e and "존치 사건=False" in e for e in errs))
        check("편방향은 사건이 있어도 부적격",
              any("regr-cm5" in e and "상호=False" in e for e in errs))
    finally:
        for p in paths:
            p.unlink(missing_ok=True)


# ── 10. 대장 손상 → 검증기는 죽지 않고 FAIL 보고 ────────────────────────
def test_ledger_corruption_resilience():
    from osk import approvals
    backup = approvals.ledger_path(VAULT).read_bytes() if approvals.ledger_path(VAULT).exists() else None
    try:
        approvals.ledger_path(VAULT).parent.mkdir(parents=True, exist_ok=True)
        approvals.ledger_path(VAULT).write_text('{"rid": "x", "kind": "protect"\n',
                                       encoding="utf-8")   # 부분 행(손상)
        try:
            rep = validate.run(VAULT, cli.published_tools)
            check("손상 대장에도 검증기 생존", True)
            check("손상은 FAIL로 보고", rep["verdict"] == "FAIL"
                  and any("승인 기록부" in list(f)[0] for f in rep["fail"]))
        except Exception as e:
            check("손상 대장에도 검증기 생존", False, repr(e))
    finally:
        if backup is not None:
            approvals.ledger_path(VAULT).write_bytes(backup)
        else:
            approvals.ledger_path(VAULT).unlink(missing_ok=True)


_LINE_SEPS = "a\u2028b\u2029c\x85d"


def test_ledger_line_boundaries():
    """행 경계는 "\\n"뿐이다(v3.22.2). ①개행 없이 끝난 완결 기록 뒤 append는 개행을
    채운다 — 구판은 새 기록을 같은 행에 붙여 대장 전체를 '손상'으로 만들었다.
    ②찢긴 꼬리는 여전히 손상으로 거부되고 파일은 그대로다. ③U+2028·U+2029·U+0085는
    `ensure_ascii=False` 기록에 날것으로 서는데, 구판 `splitlines()`가 거기서 행을
    끊어 후보 대장과 이동 대장(→ 모든 영역의 protect·approve·revert)을 막았다."""
    seps = _LINE_SEPS
    p = VAULT.signatures.parent / "regr-linebound.jsonl"
    try:
        r1 = core.ledger_append(p, {"kind": "a"})
        p.write_bytes(p.read_bytes().rstrip(b"\r\n"))       # 수동 복구가 남기는 꼬리
        r2 = core.ledger_append(p, {"kind": "b", "note": seps})
        recs = core.ledger_read(p)
        check("개행 없는 완결 꼬리 뒤 append — 두 기록 모두 판독",
              [r.get("rid") for r in recs] == [r1["rid"], r2["rid"]]
              and r2["parents"] == [r1["rid"]], recs)
        check("append 뒤 파일은 개행으로 끝난다", p.read_bytes().endswith(b"\n"))
        check("구분 문자가 필드 안에서 왕복한다", recs[-1].get("note") == seps, recs[-1])
        check("새 행은 구분 문자를 이스케이프해 쓴다 — 구판 기기가 판독한다",
              [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
              == [ln for ln in p.read_text(encoding="utf-8").split("\n") if ln.strip()])
        with open(p, "a", encoding="utf-8") as f:              # 구판이 날것으로 쓴 행
            f.write(json.dumps({**r2, "rid": core._next_rid(r2["rid"]), "parents": [r2["rid"]]},
                               ensure_ascii=False) + "\n")
        check("구판이 날것으로 쓴 구분 문자 행도 판독된다",
              core.ledger_read(p)[-1].get("note") == seps)
        check("그 뒤 append도 성립", core.ledger_append(p, {"kind": "d"}).get("rid"))
        p.write_bytes(json.dumps(r1).encode() + b"\r\n")
        check("CRLF 행도 판독된다", len(core.ledger_read(p)) == 1)
        torn = json.dumps(r1).encode() + b'\n{"rid": "x", "kind"'
        p.write_bytes(torn)
        try:
            core.ledger_append(p, {"kind": "c"})
            check("찢긴 꼬리 뒤 append는 거부된다", False)
        except ValueError as e:
            check("찢긴 꼬리 뒤 append는 거부된다", "부분 행" in str(e), e)
        check("거부된 append는 찢긴 꼬리를 묻지 않는다", p.read_bytes() == torn)
    finally:
        p.unlink(missing_ok=True)


def test_ledger_separators_candidate():
    """U+2028 등을 담은 사유가 MCP `record_candidate`를 지나 후보 대장에서 왕복한다."""
    import mcp_server as M
    seps = _LINE_SEPS
    a = ROOT / "00_Scope/W1/regr-ls1.md"; b = ROOT / "00_Scope/W1/regr-ls2.md"
    a.write_text(node_text("260924-zzzz-ls01", "후보 A"), encoding="utf-8")
    b.write_text(node_text("260924-zzzz-ls02", "후보 B"), encoding="utf-8")
    try:
        r = M.record_candidate("duplication", ["regr-ls1", "regr-ls2"], seps)
        check("구분 문자 사유의 후보 상정", r.get("ok"), r)
        try:
            last = core.ledger_read(VAULT.candidates)[-1]
            check("후보 대장이 판독되고 사유가 보존된다", last.get("reason") == seps, last)
        except ValueError as e:
            check("후보 대장이 판독되고 사유가 보존된다", False, e)
        r = M.record_candidate("competition", ["regr-ls1", "regr-ls2"], "다음")
        check("그 뒤 후보 상정도 성립", r.get("ok"), r)
    finally:
        a.unlink(missing_ok=True); b.unlink(missing_ok=True)


def test_ledger_separators_move():
    """U+2028 제목 노드의 보호영역 이동 뒤에도 이동 대장이 판독되어 protect·approve·revert가 산다."""
    from osk import approvals as A, approval_acts, region_tree
    import mcp_server as M
    # 제목 검사는 이제 이 문자를 막으므로, 그 전에 생긴(손으로 만든) 노드로 시험한다
    t = "regr\u2028ls"
    src, dst = ROOT / f"00_Scope/W1/{t}.md", ROOT / f"00_Scope/W3/{t}.md"
    (ROOT / "00_Scope/W3").mkdir(parents=True, exist_ok=True)
    (ROOT / "00_Scope/W4").mkdir(parents=True, exist_ok=True)
    src.write_text(node_text("260924-zzzz-ls03", "구분 문자 제목"), encoding="utf-8")
    try:
        approval_acts.protect(VAULT, "00_Scope/W3", "도착지 보호")
        base = A.approved_hash(VAULT, "00_Scope/W3")
        mv = M.move_nodes([t], "00_Scope/W3")
        check("구분 문자 제목 노드의 보호영역 이동", mv.get("ok"), mv)
        try:
            row = A.latest_move(core.ledger_read(A.moves_path(VAULT)), "to", f"00_Scope/W3/{t}.md")
            check("이동 대장이 판독되고 이동이 기록됐다",
                  row is not None and row["node"] == "260924-zzzz-ls03", row)
        except ValueError as e:
            check("이동 대장이 판독되고 이동이 기록됐다", False, e)
        check("도착 영역은 pending", A.state(VAULT, "00_Scope/W3") == "pending")
        approval_acts.revert(VAULT, "00_Scope/W3", base, region_tree.working_tree_hash(VAULT, "00_Scope/W3"), "반려")
        check("그 뒤 반려가 성립한다(원위치 복귀)", src.is_file() and not dst.exists())
        check("구분 문자 이동 뒤에도 다른 영역 protect 성립",
              approval_acts.protect(VAULT, "00_Scope/W4", "다른 영역").get("kind") == "protect")
        M.move_nodes([t], "00_Scope/W3")
        approval_acts.approve(VAULT, "00_Scope/W3", base, region_tree.working_tree_hash(VAULT, "00_Scope/W3"), "승인")
        check("그 뒤 승인이 성립한다", A.state(VAULT, "00_Scope/W3") == "clean"
              and dst.is_file())
    finally:
        for reg in ("00_Scope/W3", "00_Scope/W4"):
            try: approval_acts.unprotect(VAULT, reg, "정리")
            except Exception: pass
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def test_jsonl_readers_line_separators():
    """대장 밖의 JSONL(제공자 출력·갱신 저널)도 행 경계는 "\\n"뿐이다 — serde_json·
    JSON.stringify·`ensure_ascii=False`는 U+2028 등을 날것으로 둔다."""
    import importlib.util as _ilu
    from osk import response_growth as rg, growth_reviews, update_txn
    _spec = _ilu.spec_from_file_location("osk_recover_seps", ENGINE / "scripts" / "recover.py")
    _rec = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rec)

    def ok(name, fn):
        try:
            check(name, fn())
        except ValueError as e:
            check(name, False, e)
    td = Path(tempfile.mkdtemp(prefix="osk-seps-"))
    try:
        out = td / "out.jsonl"
        packet = {"osk_reviews": {}}
        out.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in (
            {"type": "system", "note": _LINE_SEPS},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": json.dumps(packet), "usage": {"input_tokens": 3, "output_tokens": 1}})),
            encoding="utf-8")
        ok("제공자 최종 출력 판독 — 구분 문자 행", lambda: growth_reviews._final_packet(out) == packet)
        ok("제공자 사용량 판독 — 구분 문자 행",
           lambda: rg.cache_usage(out, {"harness": "claude"})["measured"])
        j = td / "00_Scope/Workbench/_ledger/update.jsonl"
        j.parent.mkdir(parents=True)
        j.write_text(json.dumps({"kind": "done", "txn": "TSEP", "note": _LINE_SEPS},
                                ensure_ascii=False) + "\n", encoding="utf-8")
        ok("갱신 저널 관대한 판독 — 구분 문자 행",
           lambda: [r.get("txn") for r in update_txn.journal_lenient(_vault_at(td))] == ["TSEP"])
        ok("recover.py 저널 판독 — 구분 문자 행", lambda: _rec._journal_done(td, "TSEP"))
    finally:
        rmtree_force(td)


# ── 11. 대장 스키마 — 앵커 이후 parents·rid·필수 필드 강제 ──────────────
def test_ledger_schema_segment():
    from osk import approvals
    backup = approvals.ledger_path(VAULT).read_bytes() if approvals.ledger_path(VAULT).exists() else None
    try:
        ms = int(1754000200.0 * 1000)
        r1, r2, r3, r5 = (core._make_rid(ms, i) for i in range(4))
        rows = [
            {"rid": r1, "kind": "protect", "region": "00_Scope/W1", "at": "t"},   # 유산(parents 없음)
            {"rid": r2, "parents": [r1], "kind": "approve", "region": "00_Scope/W1",
             "at": "t"},                                        # 앵커 — 적법
            {"rid": r3, "parents": [], "kind": "approve", "region": "00_Scope/W1",
             "at": "t"},                                        # 위반: 중간의 빈 parents
            {"rid": "not-a-rid", "parents": [r3], "kind": "approve",
             "region": "00_Scope/W1", "at": "t"},               # 위반: rid 형식
            {"rid": r5, "parents": ["ghost-rid"], "kind": "revert",
             "region": "00_Scope/W1", "at": "t"},               # 위반: 미지의 parent
            {"rid": core._make_rid(ms, 9), "parents": [r5], "kind": "protect"},  # 필드 누락
        ]
        approvals.ledger_path(VAULT).parent.mkdir(parents=True, exist_ok=True)
        approvals.ledger_path(VAULT).write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        rep = validate.run(VAULT, cli.published_tools)
        seg = next((list(f.values())[0] for f in rep["fail"]
                    if "승인 대장 스키마" in list(f)[0]), None)
        check("승인 대장 스키마 세그먼트가 위반을 적발", seg is not None, rep["fail"])
        if seg:
            joined = " | ".join(seg)
            for want in ("parents 부재", "rid 형식 위반", "미지의 parent", "필수 필드 누락"):
                check(f"스키마 적발: {want}", want in joined, joined)
    finally:
        if backup is not None:
            approvals.ledger_path(VAULT).write_bytes(backup)
        else:
            approvals.ledger_path(VAULT).unlink(missing_ok=True)


# ── 14d. (입건-재서명 차단 시험 폐지 — 서명 제도가 폐지됐다. 입건 당사자
#          노드의 노출·권위는 이제 보호영역 승인본이 다룬다.) ──


# ── 14e. 대장 행 형상 — 비-dict 행·비문자열 parents (4차 조건 나) ──────
def test_ledger_row_shape():
    wipe_sig()
    VAULT.signatures.write_text("123\n", encoding="utf-8")
    check("비-dict 행은 부분 행과 동류의 손상으로 거부",
          _raises(lambda: core.ledger_read(VAULT.signatures))())
    rep = validate.run(VAULT, cli.published_tools)
    check("비-dict 행에도 검증기가 죽지 않고 FAIL 보고",
          rep["verdict"] == "FAIL")

    wipe_sig()                       # unhashable parents 원소
    node = ROOT / "00_Scope/W1/regr-shape.md"
    node.write_text(node_text("260802-zzzz-rg41", "형상"), encoding="utf-8")
    rel = str(node.relative_to(ROOT))
    r1 = core.ledger_append(VAULT.signatures, {
        "kind": "sign", "node": "260802-zzzz-rg41", "path": rel,
        "hash": core.sha256_file(node)})
    raw_append({"rid": core._make_rid(core.rid_parts(r1["rid"])[0] + 1, 0),
                "parents": [{"x": 1}, 42, r1["rid"]], "kind": "unsign",
                "node": "260802-zzzz-rg41", "path": rel,
                "hash": r1["hash"], "at": core.now_iso()})
    par = core.effective_parents(core.ledger_read(VAULT.signatures))
    check("비문자열 parents 원소는 여과되고 판정이 죽지 않는다",
          all(all(isinstance(x, str) for x in v) for v in par.values()), par)
    check("남은 정상 부모는 보존된다",
          sig_status("260802-zzzz-rg41", node) == "unsigned")
    check("형상 이상에도 ledger_append가 산다",
          bool(core.ledger_append(VAULT.signatures, {
              "kind": "sign", "node": "260802-zzzz-rg41", "path": rel,
              "hash": core.sha256_file(node), "reason": "봉합"})))


# ── 14j. 헌법 12조 5항 이행 — 열린 사건 conflicts는 표면에서 달린다 ──────
def test_conflicts_open_case_path():
    r = _w(write.create_node, VAULT, "regr-cparty", "당사자", "본문", "fable-5",
           space="00_Scope/W1")
    check("전제: 당사자 노드", r["ok"], r)
    nid = r["id"]
    write_case("CASE-2026-9200", status="docketed", verdict=None, parties=[nid])
    r2 = _w(write.update_node, VAULT, "regr-cparty",
            add_edges={"conflicts": "CASE-2026-9200"})
    check("당사자는 열린 사건 표지를 달 수 있다(헌법 12조 5항)", r2["ok"], r2)
    check("conflicts 표지 부착은 updated을 갱신하지 않는다(시행령 §1 4항)",
          r2.get("updated_kept") is True, r2)
    check("전역 검증기도 이 상태를 위반으로 보지 않는다",
          not any("regr-cparty" in str(f) for f in validate.run(VAULT, cli.published_tools)["fail"]))
    # 비당사자·미종결 아님·부재 사건은 거부
    r3 = _w(write.create_node, VAULT, "regr-nonparty", "비당사자", "본문",
            "fable-5", space="00_Scope/W1",
            edges={"conflicts": "CASE-2026-9200"})
    check("비당사자의 사건 참조는 거부", not r3["ok"], r3)
    r4 = _w(write.create_node, VAULT, "regr-nocase", "부재 사건", "본문",
            "fable-5", space="00_Scope/W1",
            edges={"conflicts": "CASE-2026-9999"})
    check("실재하지 않는 사건 참조는 거부(dangling 통과 금지)", not r4["ok"], r4)
    for nm in ("regr-cparty", "regr-nonparty", "regr-nocase"):
        (ROOT / f"00_Scope/W1/{nm}.md").unlink(missing_ok=True)
    (VAULT.ledger / "case" / "CASE-2026-9200.md").unlink(missing_ok=True)


# ── 15. 정합성 검사 — 충돌 후보 검출 (헌법 12조 1·2항) ─────────────────
def test_conflict_candidates():
    # lineage-fork는 계보 술어 폐지로 사라졌다. 기계 판정이 남는 유형은
    # duplication(동명 stem의 독립 노드) 하나다.
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    a = ROOT / "00_Scope/W1/regr-dup.md"
    b = ROOT / "00_Scope/W2/regr-dup.md"
    try:
        a.write_text(node_text("260802-zzzz-rg20", "동명 A"), encoding="utf-8")
        b.write_text(node_text("260802-zzzz-rg21", "동명 B"), encoding="utf-8")
        cands = validate.conflict_candidates(graph.Index(VAULT))
        check("duplication 검출(동명 stem)",
              any("duplication" in x and "regr-dup" in x for x in cands), cands)
        rep = validate.run(VAULT, cli.published_tools)
        check("충돌 후보는 검증기 FAIL로 사용자 심의 요청",
              rep["verdict"] == "FAIL"
              and any("정합성 검사" in list(f)[0] for f in rep["fail"]))
        before = len(core.ledger_read(VAULT.candidates))
        validate.run(VAULT, cli.published_tools)
        check("검증기는 후보를 대장에 자동 기록하지 않는다(자동 집행 없음)",
              len(core.ledger_read(VAULT.candidates)) == before)
    finally:
        for p_ in (a, b):
            p_.unlink(missing_ok=True)
    check("후보 해소 후 기준선 복귀",
          not any("duplication" in x and "regr-dup" in x
                  for x in validate.conflict_candidates(graph.Index(VAULT))))


def test_ledger_damage_is_a_kind():
    """대장의 손상은 형으로 말한다(코드 규범 7항). 읽지 못한 것(`OSError`)과도, 거부(`ValueError`)와도 다른
    형이고, 대장을 기록으로 해석하지 못하는 길은 모두 그 형으로 오른다 — 받는 쪽이 문구를 읽지 않게 한다."""
    from osk.errors import LedgerDamage
    with tempfile.TemporaryDirectory(prefix="osk-ledger-damage-") as td:
        ledger = Path(td) / "ledger.jsonl"
        nested = "[" * 100_000 + "]" * 100_000
        for label, data in (("찢긴 행", b'{"rid": 1'), ("기록이 아닌 행", b"[1, 2]\n"),
                            ("깊이 겹친 행", nested.encode("ascii")), ("UTF-8이 아닌 바이트", b'{"a": "\xff"}\n'),
                            # 해석기는 자릿수 상한을 넘는 정수에 `JSONDecodeError`가 아닌 `ValueError`를 낸다
                            ("자릿수 상한을 넘는 정수", b'{"n": ' + b"9" * 5000 + b"}\n"),
                            ("rid 형식 위반", b'{"rid": "not-a-rid"}\n')):
            ledger.write_bytes(data)
            structural = label == "rid 형식 위반"       # 행은 읽히고, 잇는 자리에서 손상으로 막힌다
            try:
                core.ledger_read(ledger)
                read_as = None
            except LedgerDamage as e:
                read_as = e
            check(f"읽기 — {label}", (read_as is None) == structural
                  and (structural or "수동 복구" in str(read_as)), read_as)
            try:
                core.ledger_append(ledger, {"kind": "x"})
                appended = "appended"
            except LedgerDamage:
                appended = "damage"
            check(f"손상된 대장에는 잇지 않는다 — {label}", appended == "damage" and ledger.read_bytes() == data,
                  appended)
        sound = Path(td) / "sound.jsonl"
        try:
            core.ledger_append(sound, {"kind": "x"}, expect=lambda records: "거부한다")
            refused = None
        except LedgerDamage:
            refused = "damage"
        except ValueError:
            refused = "refusal"
        check("거부는 손상이 아니다", refused == "refusal", refused)
        check("없는 대장은 빈 대장이다", core.ledger_read(Path(td) / "absent.jsonl") == [])
    # 제 행을 보고 손상을 말하는 기록부도 같다. 종류나 결과가 목록·객체인 행은 집합과 견줄 수 없어, 손상을
    # 말하기 전에 TypeError가 났다 — 받는 쪽이 손상만 받게 좁혀진 뒤에는 그것이 위로 샌다.
    from osk import growth_ledger, rechecks
    book = growth_ledger.ledger_path(VAULT)
    held = book.read_bytes() if book.exists() else None
    book.parent.mkdir(parents=True, exist_ok=True)
    try:
        for label, row in (("종류가 목록", {"kind": []}), ("종류가 객체", {"kind": {}}),
                           ("결과가 목록", {"kind": "review", "outcome": []})):
            book.write_text(json.dumps({"rid": core._make_rid(1, 1), **row}) + "\n", encoding="utf-8")
            try:
                growth_ledger.ledger_records(VAULT)
                kind = "read"
            except LedgerDamage:
                kind = "damage"
            except Exception as e:
                kind = type(e).__name__
            check(f"성장 기록부의 손상도 LedgerDamage다 — {label}", kind == "damage", kind)
        check("재검토는 손상된 성장 기록부를 받아 넘긴다", rechecks._carry_reports(VAULT, None, {"held": {}}, {}) == 0)
    finally:
        book.write_bytes(held) if held is not None else book.unlink()
