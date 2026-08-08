"""osk 회귀 수트 — 검토 세션(2026-08-02)의 적대 시나리오를 영속 고정.

격리 원칙 (검토 3차 지적 4): 수트 전체가 임시 mini-vault를 OSK_VAULT_ROOT로
가리키는 **자기 프로세스** 안에서 돈다 — 실 vault는 읽지도 쓰지도 않고,
전역(core.SIGNATURES 등)의 재대입·모듈 reload도 하지 않는다. sync 시험은
별도 임시 git 저장소, 서명 생애 fixture는 별도 subprocess에서 돈다.

실행: cd <vault> && .venv/bin/python _engine/tests/test_regression.py
"""
from __future__ import annotations
import errno, json, os, shutil, stat, subprocess, sys, tempfile, time, traceback
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
_TMP = tempfile.TemporaryDirectory(prefix="osk-regr-")
MINI = Path(_TMP.name) / "mini-vault"
os.environ["OSK_VAULT_ROOT"] = str(MINI)   # osk import 전에 — 전 모듈이 mini를 본다
sys.path.insert(0, str(ENGINE))

from osk import (core, graph, search, validate, authority, contract, write,  # noqa: E402
                 publish)  # noqa: E402
import osk.signatures as S  # noqa: E402

validate.make_mini_vault(MINI)
ROOT = core.ROOT
assert ROOT == MINI.resolve(), f"격리 실패 — 실 vault를 가리킨다: {ROOT}"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(
        f"{name}{' — ' + str(detail) if detail and not cond else ''}")


def wipe_sig():
    core.SIGNATURES.unlink(missing_ok=True)


def raw_append(rec: dict):
    """대장 계약을 우회한 직접 주입 — 공격·병합 산물 모사 전용."""
    with open(core.SIGNATURES, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def node_text(nid: str, summary="회귀 시험", body="본문", extra=""):
    return (f"---\nid: {nid}\ncreated: 2026-08-02 16:00 (KST)\n"
            f"updated: 2026-08-02 16:00 (KST)\nauthor: agent\ndrafter: agent\n"
            f'summary: "{summary}"\n{extra}---\n\n{body}\n')


def write_case(no: str, **kw):
    d = {"case_no": no, "status": "adjudicated", "parties": [],
         "docketed_at": "2026-08-02T10:00:00+09:00", "pre_sign": {},
         "verdict": "기각", "verdict_at": "2026-08-02T11:00:00+09:00",
         "applied": "회복", "schema_version": 1}
    d.update(kw)
    body = "\n".join(
        f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict, type(None))) else v}"
        for k, v in d.items())          # None → yaml null (파이썬 repr 누출 방지)
    (core.LEDGER / "case" / f"{no}.md").write_text(body + "\n\n본문\n", encoding="utf-8")


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
              core._rid_key(r["rid"]) > core._rid_key(hi) > core._rid_key(lo))


# ── 2. 같은 ms라도 인과 사슬이면 comparable → signed ─────────────────────
def test_same_ms_chain_signed():
    wipe_sig()
    node = ROOT / "= Scope/W1/regr-chain.md"
    node.write_text(node_text("260802-zzzz-rg01", "사슬"), encoding="utf-8")
    with mock.patch("time.time", return_value=1754000100.0):
        core.ledger_append(core.SIGNATURES, {
            "kind": "sign", "node": "260802-zzzz-rg01",
            "path": str(node.relative_to(ROOT)), "hash": "sha256:stale"})
        core.ledger_append(core.SIGNATURES, {
            "kind": "sign", "node": "260802-zzzz-rg01",
            "path": str(node.relative_to(ROOT)), "hash": core.sha256_file(node)})
    recs = core.ledger_read(core.SIGNATURES)
    same = core._rid_parts(recs[0]["rid"])[0] == core._rid_parts(recs[1]["rid"])[0]
    check("같은 ms 사슬 성립(전제)", same)
    check("같은 ms라도 인과 사슬이면 signed(비교 가능)",
          S.status("260802-zzzz-rg01", node) == "signed")


# ── 3. 인과 분기 → fail-closed → 모든 head 봉합 재서명으로 해소 ──────────
def test_fork_failclosed_and_reseal():
    node = ROOT / "= Scope/W1/regr-chain.md"     # 2번의 상태를 이어받는다
    h = core.sha256_file(node)
    fork_rid = core._make_rid(int(1754000100.0 * 1000), 0xF00)
    raw_append({"rid": fork_rid, "parents": [], "kind": "sign",
                "node": "260802-zzzz-rg01", "path": str(node.relative_to(ROOT)),
                "hash": h, "at": core.now_iso()})     # 다른 기기 유래 뿌리(병합 산물)
    check("분기(비교 불능) → unsigned fail-closed",
          S.status("260802-zzzz-rg01", node) == "unsigned")
    check("분기 상태에서 unsign도 거부(fail-closed)",
          _raises(lambda: S.unsign("260802-zzzz-rg01", "시험"))())
    r = core.ledger_append(core.SIGNATURES, {
        "kind": "sign", "node": "260802-zzzz-rg01",
        "path": str(node.relative_to(ROOT)), "hash": h,
        "reason": "재서명 — 분기 해소"})
    check("재서명이 모든 head를 봉합(parents 2개)", len(r["parents"]) == 2,
          r["parents"])
    check("재서명 후 유일 극대 → signed",
          S.status("260802-zzzz-rg01", node) == "signed")


def _raises(fn):
    def run():
        try:
            fn()
            return False
        except ValueError:
            return True
    return run


# ── 3b. 앵커 이후 parents-부재 기록은 가짜 인과를 얻지 못한다 (검토 blocker) ──
def test_anchor_no_order_fallback():
    wipe_sig()
    node = ROOT / "= Scope/W1/regr-anchor.md"
    node.write_text(node_text("260802-zzzz-rg10", "앵커"), encoding="utf-8")
    h = core.sha256_file(node)
    rel = str(node.relative_to(ROOT))
    ms = int(1754000300.0 * 1000)
    L1 = core._make_rid(ms, 0)                       # 유산 sign (parents 없음)
    raw_append({"rid": L1, "kind": "sign", "node": "260802-zzzz-rg10",
                "path": rel, "hash": "sha256:stale", "at": core.now_iso()})
    with mock.patch("time.time", return_value=1754000300.5):
        U = core.ledger_append(core.SIGNATURES, {                   # 앵커 = 해제
            "kind": "unsign", "node": "260802-zzzz-rg10", "path": rel,
            "hash": "sha256:stale", "reason": "사용자 해제"})
    check("앵커가 유산 head를 봉합", U["parents"] == [L1], U["parents"])
    B1 = core._make_rid(ms, 5)      # 구 엔진이 다른 클론에서 쓴 행(병합 유입)
    raw_append({"rid": B1, "kind": "sign", "node": "260802-zzzz-rg10",
                "path": rel, "hash": h, "at": core.now_iso()})
    recs = core.ledger_read(core.SIGNATURES)
    check("앵커 이후 parents-부재는 고립 루트(파일 순서 fallback 금지)",
          core.effective_parents(recs)[B1] == [], core.effective_parents(recs)[B1])
    check("해제가 서명으로 뒤집히지 않는다(fail-closed)",
          S.status("260802-zzzz-rg10", node) == "unsigned")
    check("판정 불가 노드로 표면화",
          "260802-zzzz-rg10" in core.unresolved_nodes(recs))
    r = core.ledger_append(core.SIGNATURES, {
        "kind": "sign", "node": "260802-zzzz-rg10", "path": rel, "hash": h,
        "reason": "사용자 재서명 — 유입 분기 봉합"})
    check("재서명이 유입 분기까지 봉합(해소 가능성 보존)",
          set(r["parents"]) == {U["rid"], B1}, r["parents"])
    check("봉합 후 signed", S.status("260802-zzzz-rg10", node) == "signed")


# ── 3c. 순환·자기 참조·전방 참조는 잘려 항상 비순환 + 해소 가능 ──────────
def test_cycle_normalization():
    wipe_sig()
    node = ROOT / "= Scope/W1/regr-cycle.md"
    node.write_text(node_text("260802-zzzz-rg11", "순환"), encoding="utf-8")
    h, rel = core.sha256_file(node), str(node.relative_to(ROOT))
    ms = int(1754000400.0 * 1000)
    A, B, X = (core._make_rid(ms, i) for i in (0, 1, 2))
    for rid, parents in ((A, [B]), (B, [A]), (X, [X])):   # 순환 + self-parent
        raw_append({"rid": rid, "parents": parents, "kind": "sign",
                    "node": "260802-zzzz-rg11", "path": rel, "hash": h,
                    "at": core.now_iso()})
    recs = core.ledger_read(core.SIGNATURES)
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
    r = core.ledger_append(core.SIGNATURES, {
        "kind": "sign", "node": "260802-zzzz-rg11", "path": rel, "hash": h,
        "reason": "재서명 — 순환 잔재 봉합"})
    check("재서명이 남은 head 전부를 봉합 → 해소",
          set(r["parents"]) == {B, X}
          and S.status("260802-zzzz-rg11", node) == "signed", r["parents"])


# ── 3d. 구조 손상(rid 부재·형식·중복)은 표면화되고 append를 거부한다 ─────
def test_structural_damage():
    for bad, label in (
        [{"kind": "sign", "node": "n-d", "path": "p", "hash": "h"}, "rid 부재"],
        [{"rid": "260802-114u-w9vj", "kind": "sign", "node": "n-d",
          "path": "p", "hash": "h"}, "rid 형식 위반"],
    ):
        wipe_sig()
        raw_append(bad)
        recs = core.ledger_read(core.SIGNATURES)
        check(f"손상 표면화: {label}", bool(core.ledger_damage(recs)))
        check(f"손상 노드 fail-closed: {label}",
              "n-d" in core.damaged_nodes(recs)
              and "n-d" in core.unresolved_nodes(recs))
        check(f"손상 대장에는 append 거부: {label}",
              _raises(lambda: core.ledger_append(core.SIGNATURES, {
                  "kind": "sign", "node": "n-x", "path": "p", "hash": "h"}))())
    wipe_sig()                                   # 중복 rid
    dup = core._make_rid(int(1754000500.0 * 1000), 0)
    for node_id in ("n-e", "n-f"):
        raw_append({"rid": dup, "kind": "sign", "node": node_id,
                    "path": "p", "hash": "h", "at": core.now_iso()})
    recs = core.ledger_read(core.SIGNATURES)
    check("중복 rid 표면화", any("중복" in d for d in core.ledger_damage(recs)))
    check("중복 rid 연루 노드 전부 fail-closed",
          {"n-e", "n-f"} <= core.damaged_nodes(recs))


# ── 3e. rid 없는 해제가 삼켜지지 않는다 (fail-open 방지) ────────────────
def test_ridless_unsign_not_swallowed():
    wipe_sig()
    node = ROOT / "= Scope/W1/regr-ridless.md"
    node.write_text(node_text("260802-zzzz-rg12", "삼킴"), encoding="utf-8")
    rel = str(node.relative_to(ROOT))
    core.ledger_append(core.SIGNATURES, {
        "kind": "sign", "node": "260802-zzzz-rg12", "path": rel,
        "hash": core.sha256_file(node)})
    check("정상 서명은 signed", S.status("260802-zzzz-rg12", node) == "signed")
    raw_append({"kind": "unsign", "node": "260802-zzzz-rg12", "path": rel,
                "hash": core.sha256_file(node), "at": core.now_iso()})
    check("rid 없는 해제는 무시되지 않고 미서명으로 떨어진다",
          S.status("260802-zzzz-rg12", node) == "unsigned")


# ── 3f. 경로 봉쇄·KST 고정 ──────────────────────────────────────────────
def test_root_confinement_and_kst():
    for bad in ("../outside/evil.md", "/etc/passwd", "= Scope/../../etc/hosts"):
        check(f"vault 밖 경로 해석 거부: {bad}", core.resolve_in_root(bad) is None)
    inside = core.resolve_in_root("= Scope/W1")
    check("vault 안 경로는 해석", inside is not None and inside.exists())
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); from osk import core; print(core.now_kst())"
         % str(ENGINE)],
        capture_output=True, text=True,
        env=dict(os.environ, TZ="America/New_York", OSK_VAULT_ROOT=str(MINI)))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    want = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    check("다른 시간대 기기에서도 (KST) 표기가 실제 KST",
          r.stdout.strip().startswith(want[:14]), (r.stdout.strip(), want))


# ── 4. 회복의 구조적 사건 결속 (허위 사건 매트릭스 — 격리 subprocess) ────
def test_restore_binding():
    with tempfile.TemporaryDirectory() as td:
        errs = validate.fixture_signature_lifecycle(td)
        check("회복 결속 매트릭스(허위 6종 차단+정상+분기 왕복)", not errs, errs)


# ── 5. 경로 재사용 공격 — id가 정본 ─────────────────────────────────────
def test_path_reuse():
    wipe_sig()
    a = ROOT / "= Scope/W1/regr-moved.md"
    b = ROOT / "= Scope/W1/regr-dest.md"
    try:
        a.write_text(node_text("260802-zzzz-rg02"), encoding="utf-8")
        core.ledger_append(core.SIGNATURES, {
            "kind": "sign", "node": "260802-zzzz-rg02",
            "path": str(a.relative_to(ROOT)), "hash": core.sha256_file(a)})
        shutil.move(str(a), str(b))                                    # 이동
        a.write_text(node_text("260802-zzzz-dcoy", "미끼"), encoding="utf-8")
        check("이동+경로 재사용에도 signed(id 정본)",
              S.status("260802-zzzz-rg02") == "signed")
        check("미끼 id는 unsigned", S.status("260802-zzzz-dcoy") == "unsigned")
    finally:
        for p in (a, b):
            p.unlink(missing_ok=True)


# ── 6. 구판 강등이 순위를 실제로 뒤집는가 ───────────────────────────────
def test_demotion_reorder():
    wipe_sig()
    oldn = ROOT / "= Scope/W1/regr-old-zqx.md"
    newn = ROOT / "= Scope/W1/regr-new-zqx.md"
    try:
        oldn.write_text(node_text("260802-zzzz-rg03", "구판 zqxtoken",
                                  "zqxtoken " * 30), encoding="utf-8")
        newn.write_text(node_text("260802-zzzz-rg04", "후계 zqxtoken",
                                  "zqxtoken " * 10,
                                  'replaces: "[[regr-old-zqx]]"\n'), encoding="utf-8")
        core.ledger_append(core.SIGNATURES, {
            "kind": "sign", "node": "260802-zzzz-rg04",
            "path": str(newn.relative_to(ROOT)), "hash": core.sha256_file(newn)})
        hits = [h["title"] for h in search.Searcher().work_search("zqxtoken", 5)]
        check("서명된 후계가 구판보다 상위(강등 후 재정렬)",
              hits.index("regr-new-zqx") < hits.index("regr-old-zqx")
              if {"regr-new-zqx", "regr-old-zqx"} <= set(hits) else False, hits)
    finally:
        for p in (oldn, newn):
            p.unlink(missing_ok=True)


# ── 7. 순수 이동·개명의 재색인 감지 ─────────────────────────────────────
def test_fingerprint_move():
    import mcp_server
    a = ROOT / "= Scope/W1/regr-fp-a.md"
    b = ROOT / "= Scope/W1/regr-fp-b.md"
    try:
        a.write_text(node_text("260802-zzzz-rg05"), encoding="utf-8")
        fp1 = mcp_server._vault_fingerprint()
        os.rename(a, b)                                # mtime·크기 불변 이동
        check("순수 이동도 fingerprint 변화", fp1 != mcp_server._vault_fingerprint())
    finally:
        for p in (a, b):
            p.unlink(missing_ok=True)


# ── 8. sync: 순서·충돌 표면화 + lock 위치 + SYNC_ENABLED 게이트 ──────────
def test_sync():
    import sync_daemon
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bare = td / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

        def clone(name):
            d = td / name
            subprocess.run(["git", "clone", "-q", str(bare), str(d)], check=True)
            subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
            return d
        A = clone("A")
        (A / "f.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q", "origin", "HEAD:main"], check=True)
        B = clone("B")
        # A가 원격을 전진시키고, B는 추적 파일이 dirty
        (A / "g.txt").write_text("remote\n")
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "remote-change"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q"], check=True)
        (B / "f.txt").write_text("base\nlocal\n")      # dirty (비충돌 라인)
        st = sync_daemon.once(B)
        ahead = subprocess.run(
            ["git", "-C", str(B), "rev-list", "--left-right", "--count",
             "HEAD...origin/main"], capture_output=True, text=True).stdout.split()
        check("sync once=ok (dirty+원격 전진 통합)", st == "ok", st)
        check("sync 후 ahead/behind 0", ahead == ["0", "0"], ahead)

        # 데몬 엔트리: lock은 <root>/.git/ 안, SYNC_ENABLED 없으면 기동 거부
        env = {k: v for k, v in os.environ.items() if k != "SYNC_ENABLED"}
        env["OSK_VAULT_ROOT"] = str(B)
        r = subprocess.run([sys.executable, str(ENGINE / "sync_daemon.py"), "--once"],
                           capture_output=True, text=True, env=env, timeout=60)
        check("SYNC_ENABLED 부재 → 기동 거부(기본 꺼짐, 템플릿 계약)",
              r.returncode != 0 and "비활성" in (r.stderr + r.stdout), r)
        env["SYNC_ENABLED"] = "1"
        r = subprocess.run([sys.executable, str(ENGINE / "sync_daemon.py"), "--once"],
                           capture_output=True, text=True, env=env, timeout=60)
        check("SYNC_ENABLED=1 → once 실행", r.returncode == 0 and "ok" in r.stdout,
              (r.returncode, r.stdout, r.stderr))
        check("lock은 실제 git 디렉터리 안(추적 트리 오염 없음)",
              (B / ".git/osk-sync.lock").exists()
              and not (B / "osk-sync.lock").exists()
              and not (ENGINE / "osk-sync.lock").exists())

        # worktree(.git이 디렉터리가 아니라 파일)에서도 추적 트리로 폴백하지 않는다
        wt = td / "B-wt"
        subprocess.run(["git", "-C", str(B), "worktree", "add", "-q",
                        "--detach", str(wt)], check=True)
        subprocess.run([sys.executable, str(ENGINE / "sync_daemon.py"), "--once"],
                       capture_output=True, text=True, timeout=60,
                       env=dict(env, OSK_VAULT_ROOT=str(wt)))
        stray = list(wt.rglob("osk-sync.lock")) + list(ENGINE.glob("osk-sync.lock"))
        check("worktree(.git이 파일)에서도 추적 트리에 lock을 만들지 않는다",
              not stray, stray)

        # 대장 동시 append는 교착되지 않는다 — union merge (.gitattributes)
        led = "= Scope/Workbench/_ledger/signatures.jsonl"
        attrs = (ENGINE.parent.parent / ".gitattributes").read_text(encoding="utf-8")
        for repo in (A, B):
            (repo / ".gitattributes").write_text(attrs, encoding="utf-8")
        (A / led).parent.mkdir(parents=True, exist_ok=True)
        (A / led).write_text('{"rid":"base","kind":"sign"}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(A), "pull", "-q", "--rebase"], check=True)
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "ledger-base"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q"], check=True)
        check("B가 대장 기준선을 받는다", sync_daemon.once(B) == "ok")
        with open(A / led, "a", encoding="utf-8") as f:
            f.write('{"rid":"a-side","kind":"sign"}\n')
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "a-side"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q"], check=True)
        with open(B / led, "a", encoding="utf-8") as f:
            f.write('{"rid":"b-side","kind":"sign"}\n')
        st3 = sync_daemon.once(B)
        body = (B / led).read_text(encoding="utf-8")
        check("다기기 대장 동시 append가 교착되지 않는다(union merge)",
              st3 == "ok", st3)
        check("양쪽 기록이 모두 보존된다(한쪽이 소실되지 않는다)",
              "a-side" in body and "b-side" in body, body)
        check("병합 결과에 충돌 마커가 없다", "<<<<<<<" not in body)

        # 진짜 충돌은 상태 표면화
        subprocess.run(["git", "-C", str(A), "pull", "-q", "--rebase"], check=True)
        (A / "f.txt").write_text("conflict-remote\n")
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "c2"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q"], check=True)
        (B / "f.txt").write_text("conflict-local\n")
        st2 = sync_daemon.once(B)
        check("충돌은 삼키지 않고 표면화",
              "충돌" in st2 or "rejected" in st2 or "실패" in st2, st2)


# ── 8-2. 동기화 대상 브랜치 고정 ─────────────────────────────────────────
def test_sync_pins_main():
    """데몬은 HEAD를 따라가지 않는다 — 다른 브랜치가 checkout돼 있어도 정본은
    언제나 main이다. 깨끗하면 전환하고, 더러우면 아무것도 하지 않는다."""
    import sync_daemon, vault_sync
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bare = td / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        R = td / "R"
        subprocess.run(["git", "clone", "-q", str(bare), str(R)], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(R), "config", k, v], check=True)
        (R / "f.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(R), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(R), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(R), "push", "-q", "origin", "HEAD:main"],
                       check=True)
        subprocess.run(["git", "-C", str(R), "branch", "-M", "main"], check=True)
        check("전제: main 고정 상수", vault_sync.SYNC_BRANCH == "main")

        # ① 깨끗한 다른 브랜치 → main으로 전환하고 동기화한다
        subprocess.run(["git", "-C", str(R), "checkout", "-q", "-b", "side"],
                       check=True)
        (R / "onmain.txt").write_text("via daemon\n")
        st = sync_daemon.once(R)
        cur = vault_sync.current_branch(R)
        check("깨끗한 side 브랜치에서 main으로 전환 후 동기화", st == "ok", st)
        check("전환 결과 HEAD가 main", cur == "main", cur)
        on_main = subprocess.run(
            ["git", "-C", str(R), "ls-tree", "-r", "--name-only", "main"],
            capture_output=True, text=True).stdout
        check("커밋이 main에 실렸다", "onmain.txt" in on_main, on_main)
        side_has = subprocess.run(
            ["git", "-C", str(R), "ls-tree", "-r", "--name-only", "side"],
            capture_output=True, text=True).stdout
        check("side 브랜치는 건드리지 않았다", "onmain.txt" not in side_has, side_has)

        # ② 더러운 다른 브랜치 → 전환하지 않고 거부, 아무것도 쓰지 않는다
        subprocess.run(["git", "-C", str(R), "checkout", "-q", "side"], check=True)
        (R / "f.txt").write_text("base\n진행 중인 수정\n")   # 추적 파일 수정
        before = subprocess.run(["git", "-C", str(R), "rev-parse", "main"],
                                capture_output=True, text=True).stdout.strip()
        st2 = sync_daemon.once(R)
        check("더러운 비-main에서는 동기화를 거부한다",
              "브랜치 고정 실패" in st2, st2)
        check("거부 시 HEAD를 옮기지 않는다",
              vault_sync.current_branch(R) == "side", vault_sync.current_branch(R))
        check("거부 시 사용자의 미커밋 작업이 그대로 남는다",
              "진행 중인 수정" in (R / "f.txt").read_text(encoding="utf-8"))
        after = subprocess.run(["git", "-C", str(R), "rev-parse", "main"],
                               capture_output=True, text=True).stdout.strip()
        check("거부 시 main은 전진하지 않는다", before == after, (before, after))

        # ③ detached HEAD(깨끗)도 main으로 회복한다
        subprocess.run(["git", "-C", str(R), "checkout", "-q", "--", "f.txt"],
                       check=True)
        subprocess.run(["git", "-C", str(R), "checkout", "-q", "--detach"], check=True)
        check("detached HEAD 전제", vault_sync.current_branch(R) is None)
        st3 = sync_daemon.once(R)
        check("detached HEAD에서도 main으로 회복", st3 == "ok", st3)
        check("회복 후 HEAD가 main", vault_sync.current_branch(R) == "main")


# ── 9. conflicts 적격 — 열린 사건 또는 존치 상호+실재 존치 사건 결속 ─────
def test_conflicts_semantics():
    write_case("CASE-2026-9001", status="docketed", verdict=None,
               parties=["260802-zzzz-rga1"])          # regr-ca가 당사자
    write_case("CASE-2026-9002", parties=["260802-zzzz-rgb1"])     # adjudicated(기각)
    write_case("CASE-2026-9003", verdict="존치",
               parties=["260802-zzzz-rgm1", "260802-zzzz-rgm2"])
    write_case("CASE-2026-9004", verdict="존치",
               parties=["260802-zzzz-rgm5", "260802-zzzz-rgm6"])
    core.PINS.write_text("", encoding="utf-8")                # 대장 파일(사건 아님)
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
            p = ROOT / f"= Scope/W1/{stem}.md"
            p.write_text(text, encoding="utf-8")
            paths.append(p)
        errs = graph.topology_check(graph.Index())
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
    backup = core.SIGNATURES.read_bytes() if core.SIGNATURES.exists() else None
    try:
        core.SIGNATURES.write_text('{"rid": "x", "kind": "sign"\n', encoding="utf-8")
        try:
            rep = validate.run()
            check("손상 대장에도 검증기 생존", True)
            check("손상은 FAIL로 보고", rep["verdict"] == "FAIL"
                  and any("서명 기록부" in list(f)[0] for f in rep["fail"]))
        except Exception as e:
            check("손상 대장에도 검증기 생존", False, repr(e))
    finally:
        if backup is not None:
            core.SIGNATURES.write_bytes(backup)


# ── 11. 대장 스키마 — 앵커 이후 parents·rid·필수 필드 강제 ──────────────
def test_ledger_schema_segment():
    backup = core.SIGNATURES.read_bytes() if core.SIGNATURES.exists() else None
    try:
        ms = int(1754000200.0 * 1000)
        r1, r2, r3, r5 = (core._make_rid(ms, i) for i in range(4))
        rows = [
            {"rid": r1, "kind": "sign", "node": "a", "path": "p", "hash": "h",
             "at": "t"},                                        # 유산(parents 없음)
            {"rid": r2, "parents": [r1], "kind": "sign", "node": "a", "path": "p",
             "hash": "h", "at": "t"},                           # 앵커 — 적법
            {"rid": r3, "parents": [], "kind": "sign", "node": "a", "path": "p",
             "hash": "h", "at": "t"},                           # 위반: 중간의 빈 parents
            {"rid": "not-a-rid", "parents": [r3], "kind": "sign", "node": "a",
             "path": "p", "hash": "h", "at": "t"},              # 위반: rid 형식
            {"rid": r5, "parents": ["ghost-rid"], "kind": "sign", "node": "a",
             "path": "p", "hash": "h", "at": "t"},              # 위반: 미지의 parent
            {"rid": core._make_rid(ms, 9), "parents": [r5], "kind": "sign"},  # 필드 누락
        ]
        core.SIGNATURES.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        rep = validate.run()
        seg = next((list(f.values())[0] for f in rep["fail"]
                    if "대장 스키마" in list(f)[0]), None)
        check("대장 스키마 세그먼트가 위반을 적발", seg is not None, rep["fail"])
        if seg:
            joined = " | ".join(seg)
            for want in ("parents 부재", "rid 형식 위반", "미지의 parent", "필수 필드 누락"):
                check(f"스키마 적발: {want}", want in joined, joined)
    finally:
        if backup is not None:
            core.SIGNATURES.write_bytes(backup)


# ── 12. validate.run은 전역을 오염시키지 않는다 ─────────────────────────
def test_validate_global_invariance():
    before = (core.ROOT, core.SIGNATURES, core.LEDGER,
              id(sys.modules["osk.core"]), id(sys.modules["osk.signatures"]),
              os.environ.get("OSK_VAULT_ROOT"))
    validate.run()
    after = (core.ROOT, core.SIGNATURES, core.LEDGER,
             id(sys.modules["osk.core"]), id(sys.modules["osk.signatures"]),
             os.environ.get("OSK_VAULT_ROOT"))
    check("validate.run 전후 전역 불변(재대입·reload 없음)", before == after,
          (before, after))


# ── 13. 권한 검사는 봉투 평가기 전까지 절대 proceed를 내지 않는다 ────────
def test_authority_hold():
    for action in ("노드 이동", "서명", "아무 문자열"):
        v = authority.check(action)
        check(f"authority.check({action!r}) = hold", v["verdict"] == "hold",
              v["verdict"])


# ── 14. 기준선: 정상 mini-vault + 앵커 대장 → 검증기 PASS ───────────────
def test_baseline_pass():
    wipe_sig()
    node = ROOT / "= Scope/W1/regr-chain.md"     # 2·3번이 만든 정상 노드 재사용
    core.ledger_append(core.SIGNATURES, {
        "kind": "sign", "node": "260802-zzzz-rg01",
        "path": str(node.relative_to(ROOT)), "hash": core.sha256_file(node),
        "reason": "기준선"})
    rep = validate.run()
    check("정상 vault 기준선 PASS", rep["verdict"] == "PASS", rep["fail"])
    check("기준선 서명 판정 1건 signed", rep.get("signed_nodes") == "1/1",
          rep.get("signed_nodes"))


# ── 14b. 자기 참조 PE는 계약 위반 (상태 자체를 불허) ──────────────────
def test_self_referencing_edge():
    p = ROOT / "= Scope/W1/regr-selfref.md"
    try:
        for pred, tgt in (("replaces", "[[regr-selfref]]"),
                          ("conflicts", "[[= Scope/W1/regr-selfref]]"),
                          ("supported-by", "[[regr-selfref.md]]")):
            p.write_text(node_text("260802-zzzz-rg30", "자기 참조", "본문",
                                   f'{pred}: "{tgt}"\n'), encoding="utf-8")
            errs = contract.validate(contract.parse(p))
            check(f"자기 참조 {pred}({tgt})는 계약 위반",
                  any("자기 자신" in e for e in errs), errs)
        p.write_text(node_text("260802-zzzz-rg30", "정상", "본문",
                               'replaces: "[[regr-other]]"\n'), encoding="utf-8")
        check("남을 가리키는 PE는 통과",
              not contract.validate(contract.parse(p)))
    finally:
        p.unlink(missing_ok=True)


# ── 14c. 외부 표면 계약 (Mechanism §6-2) ─────────────────────────────
def test_surface_contract():
    # mini-vault에 규범의 §6-2를 옮겨 심는다 — 표면 검사의 정본은 Mechanism이다
    gov = ROOT / "_governance"
    gov.mkdir(parents=True, exist_ok=True)
    real = (ENGINE.parent / "Mechanism.md").read_text(encoding="utf-8")
    import re as _re
    sec = _re.search(r"^## §6-2 .*?(?=^## §7)", real, _re.M | _re.S)
    check("실 Mechanism에 §6-2가 있다", sec is not None)
    if not sec:
        return
    (gov / "Mechanism.md").write_text(          # 특수 노드 — 계약을 갖춘다
        node_text("260802-zzzz-rg50", "표면 계약 시험용", sec.group(0)),
        encoding="utf-8")
    check("실 표면은 선언과 동치·권위 비노출", not validate.surface_violations(),
          validate.surface_violations())
    declared = validate.declared_tools()
    check("Mechanism이 도구 목록을 선언한다", bool(declared), declared)
    check("선언 목록에 서명 권위가 없다",
          declared is not None
          and not ({"sign", "unsign", "restore"} & set(declared)), declared)

    # 드리프트 적발은 **사본**에 위반을 심어 시험한다 — 엔진 원본을 건드리지 않는다
    real = (ENGINE / "mcp_server.py").read_text(encoding="utf-8")
    wsrc = (ENGINE / "osk/write.py").read_text(encoding="utf-8")
    for inject, into, want in (
        ('@mcp.tool()\ndef sign_node(x: str) -> dict:\n'
         '    return signatures.sign(x, "r", "n")\n\n\n',
         "mcp_server.py", "선언되지 않은 도구"),
        ('@mcp.tool()\ndef pin_it() -> dict:\n'
         '    return ledger_append(PINS, {})\n\n\n',
         "mcp_server.py", "권위 대장에 기록"),
        ('def sneak():\n    return ledger_append(SIGNATURES, {})\n\n\n',
         "osk/write.py", "권위 대장에 기록"),
    ):
        with tempfile.TemporaryDirectory() as td:
            eng = Path(td) / "_engine"
            (eng / "osk").mkdir(parents=True)
            (eng / "mcp_server.py").write_text(real, encoding="utf-8")
            (eng / "osk/write.py").write_text(wsrc, encoding="utf-8")
            f = eng / into
            anchor = "@mcp.tool()" if into == "mcp_server.py" else "def create_node"
            f.write_text(f.read_text(encoding="utf-8").replace(
                anchor, inject + anchor, 1), encoding="utf-8")
            errs = validate.surface_violations(eng)
            check(f"표면 드리프트 적발: {into} — {want}",
                  any(want in e for e in errs), errs)
    check("시험이 엔진 원본을 건드리지 않았다",
          (ENGINE / "mcp_server.py").read_text(encoding="utf-8") == real
          and not validate.surface_violations())


# ── 14d. 헌법 12조 5항 후단 — 입건 당사자는 판결 전까지 재서명 불가 ────
def test_open_case_blocks_signing():
    wipe_sig()
    node = ROOT / "= Scope/W1/regr-party.md"
    node.write_text(node_text("260802-zzzz-rg40", "당사자"), encoding="utf-8")
    check("평시에는 서명된다",
          bool(S.sign(node, "평시", "260802-zzzz-rg40"))
          and S.status("260802-zzzz-rg40", node) == "signed")
    write_case("CASE-2026-9100", status="docketed", verdict=None,
               parties=["260802-zzzz-rg40"])
    node.write_text(node_text("260802-zzzz-rg40", "당사자", "수정됨"), encoding="utf-8")
    check("입건 중에는 재서명이 거부된다(헌법 12조 5항)",
          _raises(lambda: S.sign(node, "재서명 시도", "260802-zzzz-rg40"))())
    check("입건 중 재서명 시도가 대장에 기록을 남기지 않는다",
          len([r for r in core.ledger_read(core.SIGNATURES)
               if r.get("node") == "260802-zzzz-rg40"]) == 1)
    (core.LEDGER / "case" / "CASE-2026-9100.md").unlink()
    check("사건 종결 후에는 재서명된다",
          bool(S.sign(node, "판결 후", "260802-zzzz-rg40"))
          and S.status("260802-zzzz-rg40", node) == "signed")


# ── 14e. 대장 행 형상 — 비-dict 행·비문자열 parents (4차 조건 나) ──────
def test_ledger_row_shape():
    wipe_sig()
    core.SIGNATURES.write_text("123\n", encoding="utf-8")
    check("비-dict 행은 부분 행과 동류의 손상으로 거부",
          _raises(lambda: core.ledger_read(core.SIGNATURES))())
    rep = validate.run()
    check("비-dict 행에도 검증기가 죽지 않고 FAIL 보고",
          rep["verdict"] == "FAIL")
    check("status()도 죽지 않는다",
          _raises(lambda: S.status("아무개"))() or True)

    wipe_sig()                       # unhashable parents 원소
    node = ROOT / "= Scope/W1/regr-shape.md"
    node.write_text(node_text("260802-zzzz-rg41", "형상"), encoding="utf-8")
    rel = str(node.relative_to(ROOT))
    r1 = core.ledger_append(core.SIGNATURES, {
        "kind": "sign", "node": "260802-zzzz-rg41", "path": rel,
        "hash": core.sha256_file(node)})
    raw_append({"rid": core._make_rid(core._rid_parts(r1["rid"])[0] + 1, 0),
                "parents": [{"x": 1}, 42, r1["rid"]], "kind": "unsign",
                "node": "260802-zzzz-rg41", "path": rel,
                "hash": r1["hash"], "at": core.now_iso()})
    par = core.effective_parents(core.ledger_read(core.SIGNATURES))
    check("비문자열 parents 원소는 여과되고 판정이 죽지 않는다",
          all(all(isinstance(x, str) for x in v) for v in par.values()), par)
    check("남은 정상 부모는 보존된다",
          S.status("260802-zzzz-rg41", node) == "unsigned")
    check("형상 이상에도 ledger_append가 산다",
          bool(core.ledger_append(core.SIGNATURES, {
              "kind": "sign", "node": "260802-zzzz-rg41", "path": rel,
              "hash": core.sha256_file(node), "reason": "봉합"})))


# ── 14f. 파싱 불가 위임 파일이 권한 검사를 죽이지 않는다 ────────────────
def test_broken_delegation_isolated():
    memo = ROOT / "= Person/Delegation/temp-memo.md"
    try:
        memo.write_text("frontmatter 없는 임시 메모\n", encoding="utf-8")
        v = authority.check("아무 행위")
        check("파싱 불가 위임 파일에도 authority.check가 산다",
              v["verdict"] == "hold")
        dels = authority.enumerate_delegations()
        check("파싱 불가 파일은 위임으로 세지 않는다(fail-closed)",
              all(not d["effective"] for d in dels if d.get("broken")))
        check("broken으로 표면화된다", any(d.get("broken") for d in dels))
    finally:
        memo.unlink(missing_ok=True)


# ── 14g. 쓰기 통로 — 계약 강제·CAS 서명 결속·델타·라우팅 ────────────────
def _w(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except write.WriteError as e:
        return {"ok": False, "violations": e.violations, **e.extra}


def test_write_contract():
    wipe_sig()
    r = write.create_node("regr-w1", "쓰기 통로 시험", "본문",
                          "fable-5", space="= Scope/W1")
    check("create_node 성공", r["ok"] and r["id"], r)
    check("id·시각은 서버가 정한다", core.re.match(core.ID_RE, r["id"]) is not None)
    n = contract.parse(ROOT / r["path"])
    check("author는 agent 고정", str(n.meta["author"]) == "agent")
    check("계약 검증 통과", not contract.validate(n), contract.validate(n))

    check("같은 이름 재생성 거부(중복 후보 자기생산 방지)",
          not _w(write.create_node, "regr-w1", "s", "b", "fable-5",
                 space="= Scope/W1")["ok"])
    check("통치 구획 쓰기 거부",
          not _w(write.create_node, "regr-w2", "s", "b", "fable-5",
                 space="_governance")["ok"])
    check("선언 안 된 군집 거부",
          not _w(write.create_node, "regr-w3", "s", "b", "fable-5",
                 space="= Scope/없는scope")["ok"])
    bad = _w(write.create_node, "regr-w4", "s", "b", "fable-5",
             space="= Scope/W1", edges={"conflicts": "regr-w1"})
    check("노드 간 conflicts는 표면에서 거부(닭-달걀)",
          not bad["ok"] and any("열린 사건" in v for v in bad["violations"]), bad)
    check("거부하면 파일을 만들지 않는다(부분 성공 없음)",
          not (ROOT / "= Scope/W1/regr-w4.md").exists())


def test_write_cas_bound_to_signature():
    node = ROOT / "= Scope/W1/regr-w1.md"
    nid = contract.parse(node).id
    # 미서명 노드 — 무-body 변경은 CAS 면제
    r = _w(write.update_node, "regr-w1", summary="고친 요약")
    check("미서명 노드의 summary 변경은 CAS 면제", r["ok"], r)
    check("덮은 요약을 응답에 담는다",
          r.get("replaced_summary") == "쓰기 통로 시험", r)
    r = _w(write.update_node, "regr-w1", add_edges={"supported-by": "regr-w1x"})
    check("미서명 노드의 엣지 델타도 면제", r["ok"], r)
    check("dangling을 응답으로 알린다", "regr-w1x" in r.get("dangling", []), r)
    # 본문 전체 치환은 언제나 CAS
    r = _w(write.update_node, "regr-w1", body="새 본문")
    check("본문 치환은 CAS 필수", not r["ok"], r)
    h = core.sha256_file(node)
    r = _w(write.update_node, "regr-w1", body="새 본문", expect_hash="sha256:틀림")
    check("CAS 불일치는 거부", not r["ok"], r)
    check("거부 응답에 현재 해시를 담지 않는다",
          not any("sha256:" in str(v) for v in r["violations"]), r)
    r = _w(write.update_node, "regr-w1", body="새 본문", expect_hash=h)
    check("올바른 CAS면 통과", r["ok"] and r["new_hash"] != h, r)

    # 서명 노드 — 무-body 변경에도 CAS 필수
    core.ledger_append(core.SIGNATURES, {
        "kind": "sign", "node": nid, "path": str(node.relative_to(ROOT)),
        "hash": core.sha256_file(node), "reason": "시험"})
    check("전제: signed", S.status(nid, node) == "signed")
    r = _w(write.update_node, "regr-w1", summary="몰래 고침")
    check("서명 노드의 summary 변경은 CAS 필수(관측 증명)", not r["ok"], r)
    check("거부 응답이 서명 사실과 rid를 알린다",
          r.get("signed") is True and r.get("signature_rid"), r)
    check("거부했으므로 서명이 그대로다", S.status(nid, node) == "signed")
    r = _w(write.update_node, "regr-w1", summary="정당한 수정",
           expect_hash=core.sha256_file(node))
    check("해시를 대면 통과하고 서명 무효화를 알린다",
          r["ok"] and r.get("was_signed") and r.get("now_unsigned"), r)
    check("쓰기 후 미서명", S.status(nid, node) == "unsigned")


def test_write_move_and_pin():
    (ROOT / "= Scope/W2").mkdir(exist_ok=True)
    before = core.sha256_file(ROOT / "= Scope/W1/regr-w1.md")
    r = _w(write.move_node, "regr-w1", "= Scope/W2")
    check("move는 바이트 불변(해시 동일)", r["ok"] and r["new_hash"] == before, r)
    check("이동 후 구 경로에 파일이 없다",
          not (ROOT / "= Scope/W1/regr-w1.md").exists())
    core.ledger_append(core.PINS, {"kind": "pin", "target": "= Scope/W2/",
                                   "reason": "시험"})
    r = _w(write.move_node, "regr-w1", "= Scope/W1")
    check("pin된 군집은 재배정 거부(시행령 §3 4항)", not r["ok"], r)
    check("거부 후에도 제자리", (ROOT / "= Scope/W2/regr-w1.md").exists())


def test_write_routing():
    core.ROUTING.unlink(missing_ok=True)
    r = _w(write.create_node, "regr-r1", "라우팅", "본문", "fable-5",
           session="repo/alpha")
    check("최초 세션은 space를 요구한다", not r["ok"], r)
    r = _w(write.create_node, "regr-r1", "라우팅", "본문", "fable-5",
           session="repo/alpha", space="= Scope/W1")
    check("space를 주면 생성되고 세션이 확정된다",
          r["ok"] and r["bound_scope"] == "W1", r)
    check("확정 후 자동 라우팅", write.resolve_session("repo/alpha") == "W1")
    r = _w(write.create_node, "regr-r2", "자동", "본문", "fable-5",
           session="repo/alpha")
    check("두 번째부터는 space 없이 착지", r["ok"] and "W1" in r["path"], r)
    # 다기기 동시 최초-확정 = 분기 → 미확정으로 fail-closed
    recs = core.ledger_read(core.ROUTING)
    fork = dict(recs[-1])
    raw = {"rid": core._make_rid(core._rid_parts(fork["rid"])[0] + 5, 0),
           "parents": [], "kind": "bind", "session": "repo/alpha",
           "scope": "W2", "at": core.now_iso()}
    with open(core.ROUTING, "a", encoding="utf-8") as f:
        f.write(json.dumps(raw, ensure_ascii=False) + "\n")
    check("분기된 바인딩은 미확정(fail-closed)",
          write.resolve_session("repo/alpha") is None)
    check("미확정이면 다시 space를 요구",
          not _w(write.create_node, "regr-r3", "s", "b", "fable-5",
                 session="repo/alpha")["ok"])


def test_write_session_alias():
    """개명 이력이 세션을 흩뜨리지 않는다 — 어느 이름으로 들어와도 한 scope."""
    core.ROUTING.unlink(missing_ok=True)
    write.alias_session("old-name", "new-name", "개명")
    write.alias_session("older-name", "old-name", "더 오래된 이름")
    check("별칭 한 단계", write.canonical_session("old-name") == "new-name")
    check("별칭 사슬을 끝까지 접는다",
          write.canonical_session("older-name") == "new-name")
    check("별칭 없는 키는 그대로", write.canonical_session("무관") == "무관")

    r = _w(write.create_node, "regr-a1", "별칭", "본문", "fable-5",
           session="older-name", space="= Scope/W1")
    check("구 이름으로 최초 확정", r["ok"], r)
    for name in ("older-name", "old-name", "new-name"):
        check(f"{name}으로 들어와도 같은 scope",
              write.resolve_session(name) == "W1", write.resolve_session(name))
    rec = [x for x in core.ledger_read(core.ROUTING) if x.get("kind") == "bind"][-1]
    check("결속은 정본 키로 기록된다", rec["session"] == "new-name", rec)

    # 순환 별칭은 접지 않는다(무한 루프 방지)
    write.alias_session("A", "B", "순환 시험")
    write.alias_session("B", "A", "순환 시험")
    got = write.canonical_session("A")
    check("순환 별칭에도 종료하고 원래 키로 남는다", got in ("A", "B"), got)


def test_write_candidate_basis():
    core.CANDIDATES.write_text("", encoding="utf-8")
    a = ROOT / "= Scope/W1/regr-c1.md"; b = ROOT / "= Scope/W1/regr-c2.md"
    a.write_text(node_text("260802-zzzz-rgc1", "후보 A"), encoding="utf-8")
    b.write_text(node_text("260802-zzzz-rgc2", "후보 B"), encoding="utf-8")
    try:
        r1 = _w(write.record_candidate, "duplication", ["regr-c1", "regr-c2"], "시험")
        check("후보 상정", r1["ok"] and not r1["deduped"], r1)
        r2 = _w(write.record_candidate, "duplication", ["regr-c1", "regr-c2"], "재시도")
        check("같은 근거는 중복 기록하지 않는다(헌법 12조 2항)",
              r2["deduped"] and r2["rid"] == r1["rid"], r2)
        r3 = _w(write.record_candidate, "competition", ["regr-c1", "regr-c2"], "다른 유형")
        check("유형이 다르면 다른 근거", not r3["deduped"], r3)
        b.write_text(node_text("260802-zzzz-rgc2", "후보 B", "내용이 바뀜"),
                     encoding="utf-8")
        r4 = _w(write.record_candidate, "duplication", ["regr-c1", "regr-c2"], "상태 변경 후")
        check("상태가 바뀌면 재상정이 열린다(각하의 영구 봉인 방지)",
              not r4["deduped"], r4)
        check("미정의 유형 거부",
              not _w(write.record_candidate, "무슨유형", ["regr-c1", "regr-c2"])["ok"])
        check("당사자 부재 거부",
              not _w(write.record_candidate, "duplication", ["regr-c1", "없는노드"])["ok"])
    finally:
        for p_ in (a, b):
            p_.unlink(missing_ok=True)


def test_write_serialized():
    """동시 쓰기 2건이 전역 잠금으로 직렬화되는가 (subprocess 실측)."""
    script = (
        "import sys, json; sys.path.insert(0, %r)\n"
        "from osk import write\n"
        "import sys as s\n"
        "r = write.update_node('regr-r2', add_edges={'supported-by': s.argv[1]})\n"
        "print(json.dumps({'ok': r['ok'], 'edges': r['edges']['supported-by']}))\n"
        % str(ENGINE))
    sf = Path(_TMP.name) / "concurrent.py"
    sf.write_text(script, encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(sf), f"tgt{i}"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=dict(os.environ,
                                                  OSK_VAULT_ROOT=str(MINI)))
             for i in range(2)]
    outs = [p.communicate() for p in procs]
    check("동시 쓰기 2건 모두 성공", all(p.returncode == 0 for p in procs),
          [o[1][-200:] for o in outs])
    final = contract.parse(ROOT / "= Scope/W1/regr-r2.md").edges("supported-by")
    check("두 델타가 모두 보존된다(lost update 없음)",
          "tgt0" in final and "tgt1" in final, final)


# ── 14h. 표면 스모크 — 도구 7종을 **직접 호출**한다 (7차 치명) ──────────
def test_surface_smoke():
    """AST 검사와 fingerprint 호출만으로는 표면 껍데기가 죽어 있어도 통과한다.
    실제로 불러 봐야 이름 가림 계열이 잡힌다."""
    import importlib
    import mcp_server as M
    importlib.reload(M)
    check("모듈 전역 search가 도구 함수에 가려지지 않았다",
          hasattr(M.search_mod, "Searcher"), type(M.search_mod).__name__)

    (ROOT / "= Scope/WSmoke").mkdir(exist_ok=True)   # 앞선 시험이 pin한 군집 회피
    node = ROOT / "= Scope/W1/regr-smoke.md"
    node.write_text(node_text("260802-zzzz-rg60", "스모크"), encoding="utf-8")
    calls = {
        "overview": lambda: M.overview("repo/smoke"),
        "search": lambda: M.search("스모크", 3),
        "read_node": lambda: M.read_node("regr-smoke"),
        "run_validators": lambda: M.run_validators(),
        "create_node": lambda: M.create_node("regr-smoke2", "스모크2", "본문",
                                             "fable-5", space="= Scope/W1"),
        "update_node": lambda: M.update_node("regr-smoke2", summary="고침"),
        "move_node": lambda: M.move_node("regr-smoke2", "= Scope/WSmoke"),
        "record_candidate": lambda: M.record_candidate(
            "duplication", ["regr-smoke", "regr-smoke2"], "스모크"),
    }
    for name, fn in calls.items():
        try:
            out = fn()
            dead = isinstance(out, dict) and out.get("ok") is False
            check(f"표면 도구 살아 있음: {name}", not dead, out)
        except Exception as e:
            check(f"표면 도구 살아 있음: {name}", False, f"{type(e).__name__}: {e}")
    r = M.read_node("regr-smoke")
    check("read_node가 hash를 준다(CAS 입력)", r.get("hash", "").startswith("sha256:"))
    hits = M.search("스모크", 5)
    check("search 결과에 signed 표시", all("signed" in h for h in hits), hits)


# ── 14i. 직렬화 왕복 — 표면이 스스로 파손 노드를 만들지 않는다 (7차 중대 A) ──
def test_render_roundtrip():
    for label, kw in (
        ('따옴표', {"summary": 'He said "hi"'}),
        ('백슬래시', {"summary": r"경로 C:\temp\x"}),
        ('콜론·해시', {"summary": "a: b # c"}),
        ('엣지 따옴표', {"summary": "정상", "edges": {"supported-by": '따옴표"대상'}}),
    ):
        name = f"regr-rt-{abs(hash(label)) % 10000}"
        r = _w(write.create_node, name, kw.get("summary", "s"), "본문",
               "fable-5", space="= Scope/W1", edges=kw.get("edges"))
        p = ROOT / "= Scope/W1" / f"{name}.md"
        if r.get("ok"):
            try:
                back = contract.parse(p)
                ok = str(back.meta["summary"]) == kw.get("summary", "s")
            except Exception as e:
                ok = False
                r = {"parse": str(e)}
            check(f"성공 보고했으면 되읽힌다: {label}", ok, r)
        else:
            check(f"거부했으면 파일이 없다: {label}", not p.exists(), r)
        p.unlink(missing_ok=True)

    # update가 파일을 파손시키고 오류를 내는 일이 없다(부분 성공 금지)
    r = _w(write.create_node, "regr-rt-upd", "정상", "본문", "fable-5",
           space="= Scope/W1")
    check("전제: 생성", r["ok"], r)
    p = ROOT / "= Scope/W1/regr-rt-upd.md"
    before = p.read_bytes()
    r2 = _w(write.update_node, "regr-rt-upd", summary='깨는 "따옴표"')
    check("update 후에도 파일이 파싱된다",
          bool(contract.parse(p)) if p.exists() else False, r2)
    check("실패였다면 파일이 그대로", r2["ok"] or p.read_bytes() == before)
    p.unlink(missing_ok=True)


# ── 14j. 헌법 12조 5항 이행 — 열린 사건 conflicts는 표면에서 달린다 ──────
def test_conflicts_open_case_path():
    r = _w(write.create_node, "regr-cparty", "당사자", "본문", "fable-5",
           space="= Scope/W1")
    check("전제: 당사자 노드", r["ok"], r)
    nid = r["id"]
    write_case("CASE-2026-9200", status="docketed", verdict=None, parties=[nid])
    r2 = _w(write.update_node, "regr-cparty",
            add_edges={"conflicts": "CASE-2026-9200"})
    check("당사자는 열린 사건 표지를 달 수 있다(헌법 12조 5항)", r2["ok"], r2)
    check("conflicts 표지 부착은 updated을 갱신하지 않는다(시행령 §1 4항)",
          r2.get("updated_kept") is True, r2)
    check("전역 검증기도 이 상태를 위반으로 보지 않는다",
          not any("regr-cparty" in str(f) for f in validate.run()["fail"]))
    # 비당사자·미종결 아님·부재 사건은 거부
    r3 = _w(write.create_node, "regr-nonparty", "비당사자", "본문",
            "fable-5", space="= Scope/W1",
            edges={"conflicts": "CASE-2026-9200"})
    check("비당사자의 사건 참조는 거부", not r3["ok"], r3)
    r4 = _w(write.create_node, "regr-nocase", "부재 사건", "본문",
            "fable-5", space="= Scope/W1",
            edges={"conflicts": "CASE-2026-9999"})
    check("실재하지 않는 사건 참조는 거부(dangling 통과 금지)", not r4["ok"], r4)
    for nm in ("regr-cparty", "regr-nonparty", "regr-nocase"):
        (ROOT / f"= Scope/W1/{nm}.md").unlink(missing_ok=True)
    (core.LEDGER / "case" / "CASE-2026-9200.md").unlink(missing_ok=True)


# ── 14k. 라우팅 벽돌화 방지 + Space 루트 직속 거부 (7차 중대 C·경미 E) ────
def test_routing_not_bricked():
    core.ROUTING.unlink(missing_ok=True)
    r = _w(write.create_node, "regr-dom", "도메인 착지", "본문", "fable-5",
           session="repo/beta", space="= Domain/D1") if (ROOT / "= Domain/D1").is_dir() \
        else {"skip": True}
    if not r.get("skip"):
        check("Domain 착지는 세션을 결속하지 않는다(scope 한정)",
              r["ok"] and r.get("bound_scope") is None, r)
        check("따라서 다음 호출이 벽돌이 되지 않는다",
              write.resolve_session("repo/beta") is None)
        (ROOT / "= Domain/D1/regr-dom.md").unlink(missing_ok=True)
    for root in ("= Scope", "= Person", "= Domain"):
        rr = _w(write.create_node, f"regr-root-{root[-2:]}", "루트 직속", "본문",
                "fable-5", space=root)
        check(f"Space 루트 직속 생성 거부: {root}", not rr["ok"], rr)


# ── 14l. 계약 밖 필드는 조용히 지워지지 않는다 (7차 경미 F) ──────────────
def test_extra_field_preserved():
    p = ROOT / "= Scope/W1/regr-extra.md"
    p.write_text(node_text("260802-zzzz-rg70", "여분 필드", "본문",
                           "custom_field: 사용자가 손으로 넣음\n"), encoding="utf-8")
    try:
        before = p.read_bytes()
        r = _w(write.update_node, "regr-extra", summary="고침")
        check("계약 밖 필드가 있으면 표면 수정을 거부", not r["ok"], r)
        check("거부했으므로 필드가 살아 있다", p.read_bytes() == before)
    finally:
        p.unlink(missing_ok=True)


# ── 14m. 순환 별칭은 원래 입력 키로 남는다 (7차 경미 D) ─────────────────
def test_alias_cycle_returns_input():
    core.ROUTING.unlink(missing_ok=True)
    write.alias_session("cX", "cY", "순환")
    write.alias_session("cY", "cX", "순환")
    check("순환에서 cX는 cX로 남는다", write.canonical_session("cX") == "cX",
          write.canonical_session("cX"))
    check("순환에서 cY는 cY로 남는다", write.canonical_session("cY") == "cY",
          write.canonical_session("cY"))


# ── 14n. 자기 자신과의 충돌은 성립하지 않는다 (7차 경미 G) ───────────────
def test_candidate_needs_distinct():
    core.CANDIDATES.write_text("", encoding="utf-8")
    p = ROOT / "= Scope/W1/regr-self.md"
    p.write_text(node_text("260802-zzzz-rg80", "자기 충돌"), encoding="utf-8")
    try:
        r = _w(write.record_candidate, "duplication", ["regr-self", "regr-self"])
        check("같은 노드 둘은 거부", not r["ok"], r)
    finally:
        p.unlink(missing_ok=True)


# ── 14o. 엣지 표기 동일성 — 경로형과 스템형은 같은 대상 (8차 잔여 1) ────
def test_edge_target_normalization():
    r = _w(write.create_node, "regr-norm-t", "대상", "본문", "fable-5",
           space="= Scope/W1")
    r0 = _w(write.create_node, "regr-norm", "표기 정규화", "본문",
            "fable-5", space="= Scope/W1",
            edges={"supported-by": "= Scope/W1/regr-norm-t"})
    check("전제: 경로형 엣지로 생성", r0["ok"], r0)
    r1 = _w(write.update_node, "regr-norm",
            add_edges={"supported-by": "regr-norm-t"})
    check("스템형 추가는 경로형과 같은 대상 — 중복 등재하지 않는다",
          len(r1["edges"]["supported-by"]) == 1, r1["edges"])
    check("변경이 없으므로 no_change", r1.get("no_change") is True, r1)
    r2 = _w(write.update_node, "regr-norm",
            remove_edges={"supported-by": "regr-norm-t"})
    check("스템형 제거가 경로형 엣지에 유효하다",
          r2["ok"] and not r2["edges"]["supported-by"], r2)
    for nm in ("regr-norm", "regr-norm-t"):
        (ROOT / f"= Scope/W1/{nm}.md").unlink(missing_ok=True)


# ── 14p. 무변경 update는 쓰지 않는다 (8차 권고) ─────────────────────────
def test_update_no_change():
    r = _w(write.create_node, "regr-noop", "그대로", "본문", "fable-5",
           space="= Scope/W1")
    check("전제: 생성", r["ok"], r)
    p_ = ROOT / "= Scope/W1/regr-noop.md"
    before = p_.read_bytes()
    r1 = _w(write.update_node, "regr-noop", summary="그대로")
    check("같은 summary는 no_change", r1.get("no_change") is True, r1)
    r2 = _w(write.update_node, "regr-noop", body="본문",
            expect_hash=core.sha256_file(p_))
    check("같은 body도 no_change(CAS는 통과시키고 내용으로 판정)",
          r2.get("no_change") is True, r2)
    r2b = _w(write.update_node, "regr-noop", body="본문")
    check("body를 주면 CAS가 no-op 판정보다 먼저다", not r2b["ok"], r2b)
    check("파일이 한 바이트도 바뀌지 않았다", p_.read_bytes() == before)
    check("no_change에도 현재 해시를 준다",
          r1["new_hash"] == core.sha256_file(p_), r1)
    r3 = _w(write.update_node, "regr-noop", summary="달라짐")
    check("실제 변경은 통과", r3["ok"] and not r3.get("no_change"), r3)
    p_.unlink(missing_ok=True)


# ── 14q. bound_scope는 실제 결속했을 때만 (8차 잔여 2) ──────────────────
def test_bound_scope_honest():
    core.ROUTING.unlink(missing_ok=True)
    (ROOT / "= Domain/D1").mkdir(parents=True, exist_ok=True)
    r = _w(write.create_node, "regr-bs-dom", "도메인", "본문", "fable-5",
           session="repo/gamma", space="= Domain/D1")
    check("Domain 착지는 결속하지 않는다", r["ok"], r)
    check("따라서 bound_scope를 보고하지 않는다",
          r.get("bound_scope") is None, r)
    check("대장에도 결속이 없다", write.resolve_session("repo/gamma") is None)
    r2 = _w(write.create_node, "regr-bs-sc", "스코프", "본문", "fable-5",
            session="repo/gamma", space="= Scope/W1")
    check("scope 착지에서 결속하고 그때만 보고",
          r2["ok"] and r2.get("bound_scope") == "W1", r2)
    r3 = _w(write.create_node, "regr-bs-3", "이미 결속", "본문", "fable-5",
            session="repo/gamma")
    check("이미 결속된 세션은 다시 보고하지 않는다",
          r3["ok"] and r3.get("bound_scope") is None, r3)
    for nm, sp in (("regr-bs-dom", "= Domain/D1"), ("regr-bs-sc", "= Scope/W1"),
                   ("regr-bs-3", "= Scope/W1")):
        (ROOT / sp / f"{nm}.md").unlink(missing_ok=True)


# ── 14r. 실 MCP 전송 계층 — stdio JSON-RPC로 dict 인자를 보낸다 (8차) ────
def test_mcp_transport():
    """도구를 파이썬으로 부르는 것과 JSON-RPC로 부르는 것은 다른 일이다.
    검토 세션이 8차에서 직접 시험한 것을 수트에 영속화한다."""
    import asyncio
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as e:
        check("MCP 클라이언트 가용", False, f"import 실패: {e}")
        return

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=[str(ENGINE / "mcp_server.py")],
            env=dict(os.environ, OSK_VAULT_ROOT=str(MINI)))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = sorted(t.name for t in (await s.list_tools()).tools)
                out = {"names": names}

                async def call(tool, args):
                    res = await s.call_tool(tool, args)
                    return json.loads(res.content[0].text)

                out["create"] = await call("create_node", {
                    "title": "regr-tx", "summary": "전송", "body": "본문",
                    "drafter": "fable-5", "space": "= Scope/W1",
                    "edges": {"supported-by": "regr-tx-t"}})   # dict 인자
                out["read"] = await call("read_node", {"name": "regr-tx"})
                out["stale"] = await call("update_node", {
                    "name": "regr-tx", "body": "새 본문",
                    "expect_hash": "sha256:틀림"})
                out["retry"] = await call("update_node", {
                    "name": "regr-tx", "body": "새 본문",
                    "expect_hash": out["read"]["hash"]})
                out["delta"] = await call("update_node", {
                    "name": "regr-tx",
                    "add_edges": {"supported-by": "regr-tx-t2"}})  # dict 인자
                out["validators"] = await call("run_validators", {})
                res = await s.call_tool("search", {"query": "전송", "k": 3})
                items = [json.loads(c.text) for c in res.content]
                out["search"] = (items[0] if len(items) == 1
                                 and isinstance(items[0], list) else items)
                return out

    try:
        o = asyncio.run(asyncio.wait_for(run(), timeout=120))
    except Exception as e:
        check("전송 계층 왕복", False, f"{type(e).__name__}: {e}")
        return
    check("전송: 선언 목록과 도구 목록이 동치",
          o["names"] == sorted(validate.declared_tools() or []), o["names"])
    check("전송: dict 인자(edges)가 통과한다", o["create"].get("ok"), o["create"])
    check("전송: read_node가 hash를 준다",
          o["read"].get("hash", "").startswith("sha256:"), o["read"])
    check("전송: 낡은 해시는 ok:false로 거부(예외가 아니라)",
          o["stale"].get("ok") is False and o["stale"].get("violations"),
          o["stale"])
    check("전송: 거부 응답에 현재 해시가 없다",
          not any("sha256:" in str(v) for v in o["stale"].get("violations", [])),
          o["stale"])
    check("전송: 재읽기 후 재시도 성공(재시도 계약)", o["retry"].get("ok"), o["retry"])
    check("전송: dict 인자(add_edges) 델타 적용",
          o["delta"].get("ok") and "regr-tx-t2" in o["delta"]["edges"]["supported-by"],
          o["delta"])
    check("전송: run_validators가 서버 안에서도 동작(이벤트 루프 안)",
          o["validators"].get("verdict") in ("PASS", "FAIL")
          and not any("asyncio" in str(x) for f in o["validators"]["fail"]
                      for x in list(f.values())[0]),
          o["validators"]["fail"])
    check("전송: search 결과에 signed 동봉",
          all("signed" in h for h in o["search"]), o["search"])
    (ROOT / "= Scope/W1/regr-tx.md").unlink(missing_ok=True)


# ── 14s. 표면 왕복 — search가 준 이름을 나머지 도구가 받는가 (8차 차단 ③) ──
def test_surface_name_roundtrip():
    """list_nodes를 없앤 설계에서 search는 이름을 얻는 유일한 통로다. 그 이름이
    그대로 쓰이지 않으면 발견과 지목 사이가 끊어진다 — 표면 쓰기의 결과는 언제나
    미서명이므로 이것은 예외가 아니라 기본 경로다."""
    import mcp_server as M
    r = _w(write.create_node, "regr-rt-name", "왕복", "본문 내용",
           "fable-5", space="= Scope/W1")
    check("전제: 생성(→미서명)", r["ok"] and not r["signed"], r)
    hits = [h for h in M.search("왕복", 8) if h["path"].endswith("regr-rt-name.md")]
    check("search가 찾는다", len(hits) == 1, hits)
    h = hits[0]
    check("미서명이 signed 필드로 표시된다", h["signed"] is False, h)
    check("title은 노드 이름 그대로다(변조 없음)",
          h["title"] == "regr-rt-name", h["title"])
    check("그 title로 read_node가 된다",
          "error" not in M.read_node(h["title"]), M.read_node(h["title"]))
    check("그 title로 update_node가 된다",
          _w(write.update_node, h["title"], summary="갱신")["ok"])
    check("그 title을 엣지 대상으로 쓰면 dangling이 아니다",
          not _w(write.create_node, "regr-rt-ref", "참조", "본문",
                 "fable-5", space="= Scope/W1",
                 edges={"supported-by": h["title"]})["dangling"])
    for nm in ("regr-rt-name", "regr-rt-ref"):
        (ROOT / f"= Scope/W1/{nm}.md").unlink(missing_ok=True)


# ── 14t. 계약 검증은 목적지 경로로 한다 (8차 차단 ①) ────────────────────
def test_validate_uses_destination_path():
    """되읽은 노드가 임시 파일명을 들면 stem에 걸린 계약 규칙이 무력화된다 —
    표면이 자기 검증기가 위반이라 부르는 노드를 ok로 쓰게 된다."""
    r = _w(write.create_node, "regr-selfref-w", "자기 참조", "본문",
           "fable-5", space="= Scope/W1",
           edges={"replaces": "regr-selfref-w"})
    check("자기 참조 replaces를 쓰기 통로가 거부한다", not r["ok"], r)
    check("거부 사유가 계약 문언 그대로",
          any("자기 자신" in v for v in r["violations"]), r)
    check("거부했으므로 파일이 없다",
          not (ROOT / "= Scope/W1/regr-selfref-w.md").exists())


# ── 14u. 동명 중복이면 쓰기를 거부한다 (8차 차단 ②) ─────────────────────
def test_dup_stem_write_refused():
    """읽기(색인)와 쓰기가 서로 다른 쪽을 고르면 본 파일과 고쳐지는 파일이
    달라진다. 표면은 임의로 한쪽을 택하지 않는다."""
    (ROOT / "= Scope/W3").mkdir(parents=True, exist_ok=True)
    a = ROOT / "= Scope/W1/regr-dup.md"
    b = ROOT / "= Scope/W3/regr-dup.md"
    try:
        a.write_text(node_text("260806-aaaa-1111", "중복 A", "A 본문"), encoding="utf-8")
        b.write_text(node_text("260806-aaaa-3333", "중복 B", "B 본문"), encoding="utf-8")
        check("색인이 중복을 인지", "regr-dup" in graph.Index().dup_stems)
        for label, call in (
            ("update", lambda: write.update_node("regr-dup", summary="x")),
            ("move", lambda: write.move_node("regr-dup", "= Scope/W2")),
        ):
            r = _w(call)
            check(f"{label}는 동명 중복을 거부", not r["ok"], r)
            check(f"{label} 거부 사유가 중복임을 밝힌다",
                  any("같은 이름" in v for v in r["violations"]), r)
        check("거부했으므로 두 파일 모두 그대로",
              "A 본문" in a.read_text(encoding="utf-8")
              and "B 본문" in b.read_text(encoding="utf-8"))
    finally:
        a.unlink(missing_ok=True); b.unlink(missing_ok=True)


# ── 14v. 표면 린트 — 스키마 건전·가르침의 회귀를 잡는다 (10차 ④) ────────
def test_surface_lint():
    check("실 표면이 린트를 통과", not validate.surface_lint(),
          validate.surface_lint())
    import mcp_server as M
    import asyncio, json as _json
    ts = asyncio.run(M.mcp.list_tools())
    for t_ in ts:
        s = t_.inputSchema or {}
        props, req = s.get("properties") or {}, set(s.get("required") or [])
        check(f"{t_.name}: required ⊆ properties", req <= set(props))
        check(f"{t_.name}: 자동 title 주석 없음",
              not [k for k, v in props.items()
                   if isinstance(v, dict) and "title" in v])
    cn = [x for x in ts if x.name == "create_node"][0]
    check("create_node의 title 인자는 보존된다",
          "title" in cn.inputSchema["properties"])
    check("drafter가 스키마 패턴으로 가둬진다",
          "pattern" in cn.inputSchema["properties"]["drafter"])
    check("edges 술어가 스키마 enum으로 가둬진다",
          "supported-by" in _json.dumps(cn.inputSchema["properties"]["edges"]))


# ── 14w. overview — 주소를 선제 조회할 수 있다 (10차 ②) ─────────────────
def test_overview():
    import mcp_server as M
    o = M.overview()
    check("군집 목록을 준다", isinstance(o.get("clusters"), list) and o["clusters"], o)
    check("그 값이 그대로 space로 통한다",
          _w(write.create_node, "regr-ov", "조망", "본문", "fable-5",
             space=o["clusters"][0])["ok"], o["clusters"][:3])
    check("engine_rev를 준다", bool(o.get("engine_rev")), o)
    check("열린 사건 목록을 준다", isinstance(o.get("open_cases"), list), o)
    o2 = M.overview("repo/ov")
    check("session을 주면 결속을 함께 준다", "session_scope" in o2, o2)
    for c in o["clusters"]:
        (ROOT / c / "regr-ov.md").unlink(missing_ok=True)


# ── 14x. 엣지 값의 형은 통로가 거부한다 (10차 정정 ①: 스키마+런타임 이중) ──
def test_edge_value_type_refused():
    for bad in ({"supported-by": 42}, {"supported-by": [{"x": 1}]},
                {"supported-by": ""}, {"supported-by": [None]}):
        r = _w(write.create_node, "regr-edgeval", "형 검사", "본문",
               "fable-5", space="= Scope/W1", edges=bad)
        check(f"엣지 값 {bad} 거부", not r["ok"], r)
        check("거부했으므로 파일이 없다",
              not (ROOT / "= Scope/W1/regr-edgeval.md").exists())
    # remove_edges도 add_edges와 같은 검사를 받는다(비대칭 제거)
    r0 = _w(write.create_node, "regr-sym", "대칭", "본문", "fable-5",
            space="= Scope/W1", edges={"supported-by": "대상A"})
    check("전제: 생성", r0["ok"], r0)
    for kw in ({"add_edges": {"suported-by": "X"}},
               {"remove_edges": {"suported-by": "X"}}):
        r = _w(write.update_node, "regr-sym", **kw)
        check(f"술어 오타 거부: {list(kw)[0]}", not r["ok"], r)
        check("거부 사유에 쓸 수 있는 술어가 실린다",
              any("supported-by" in v for v in r["violations"]), r)
    (ROOT / "= Scope/W1/regr-sym.md").unlink(missing_ok=True)


# ── 14y. 거부가 주소를 가르친다 (상주 0, 실패 시에만 지불) ───────────────
def test_refusal_teaches_address():
    r = _w(write.create_node, "regr-addr", "주소", "본문", "fable-5",
           space="W1")     # 접두 빠뜨린 흔한 실수
    check("접두 없는 space 거부", not r["ok"], r)
    check("거부가 쓸 수 있는 군집을 열거한다",
          any("= Scope/W1" in v for v in r["violations"]), r)
    r2 = _w(write.create_node, "regr-addr", "주소", "본문", "fable-5",
            space="= Scope/W1", edges={"conflicts": "아무거나"})
    check("conflicts 거부가 형식과 열린 사건을 알린다",
          not r2["ok"] and any("CASE-" in v and "열린 사건" in v
                               for v in r2["violations"]), r2)
    (ROOT / "= Scope/W1/regr-addr.md").unlink(missing_ok=True)


# ── 14z. id도 핸들로 통한다 — 틀린 '노드 없음' 진단 제거 (10차 ②) ────────
def test_id_as_handle():
    r = _w(write.create_node, "regr-idh", "핸들", "본문", "fable-5",
           space="= Scope/W1")
    check("전제: 생성", r["ok"], r)
    nid = r["id"]
    r2 = _w(write.update_node, nid, summary="id로 지목")
    check("응답의 id로 update가 된다", r2["ok"], r2)
    import mcp_server as M
    check("read_node도 id를 받는다", "error" not in M.read_node(nid),
          M.read_node(nid))
    (ROOT / "= Scope/W1/regr-idh.md").unlink(missing_ok=True)


# ── 14aa. 발행 절차 v2 — 매니페스트·가드 (모두 fail-closed) ─────────────
def _pub_fixture(td):
    """사설 mini-vault + 가짜 공개 저장소 + 매니페스트."""
    pub = Path(td) / "public"
    pub.mkdir()
    subprocess.run(["git", "init", "-q", str(pub)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(pub), "config", k, v], check=True)
    (pub / "LICENSE").write_text("MIT\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(pub), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(pub), "commit", "-qm", "init"], check=True)
    man = Path(td) / "manifest.txt"
    man.write_text(
        "MAP  _governance/ -> _governance/\n"
        "KEEP LICENSE\n"
        "DENY _ledger/\n"
        "DENY __pycache__/\n"
        "SKEL = Scope/\n", encoding="utf-8")
    return pub, man


def test_publish_manifest():
    with tempfile.TemporaryDirectory() as td:
        _pub, man = _pub_fixture(td)
        m = publish.parse_manifest(man)
        check("MAP·KEEP·DENY·SKEL 파싱",
              m["map"] == [("_governance/", "_governance/")]
              and m["keep"] == ["LICENSE"] and "_ledger/" in m["deny"]
              and m["skel"] == ["= Scope/"], m)

        def raises(f):
            try:
                f(); return False
            except publish.PublishError:
                return True
        bad = Path(td) / "bad.txt"
        bad.write_text("PUBLISH everything\n", encoding="utf-8")
        check("미정의 지시어는 PublishError",
              raises(lambda: publish.parse_manifest(bad)))
        noarrow = Path(td) / "noarrow.txt"
        noarrow.write_text("MAP a/ b/\n", encoding="utf-8")
        check("MAP에 화살표가 없으면 거부",
              raises(lambda: publish.parse_manifest(noarrow)))
        empty = Path(td) / "empty.txt"
        empty.write_text("# 주석뿐\n", encoding="utf-8")
        check("MAP 없는 매니페스트 거부",
              raises(lambda: publish.parse_manifest(empty)))


def test_publish_guards():
    """가드는 전부 fail-closed다 — 하나라도 걸리면 아무것도 쓰지 않는다."""
    gov = ROOT / "_governance"
    gov.mkdir(parents=True, exist_ok=True)
    (gov / "records").mkdir(exist_ok=True)
    doc, rec = gov / "PubDoc.md", gov / "records" / "pub-rec.md"
    led = gov / "_ledger"
    mine = []
    try:
        # 통치 문서·사료는 특수한 노드 — 계약을 갖춘다 (시행령 §10 1항)
        doc.write_text(node_text("260802-pppp-0001", "발행 시험 문서"),
                       encoding="utf-8")
        rec.write_text(node_text("260802-pppp-0010", "발행 시험 사료"),
                       encoding="utf-8")
        led.mkdir(exist_ok=True)
        (led / "secret.jsonl").write_text('{"a":1}\n', encoding="utf-8")
        mine = [doc, rec, led / "secret.jsonl"]
        with tempfile.TemporaryDirectory() as td:
            pub, man = _pub_fixture(td)
            m = publish.parse_manifest(man)
            items = publish.collect(m)
            rels = {r for _s, r in items}
            check("DENY가 대장을 제외한다",
                  not any("_ledger" in r for r in rels), sorted(rels))
            check("통치 문서와 사료는 포함",
                  "_governance/PubDoc.md" in rels
                  and "_governance/records/pub-rec.md" in rels, sorted(rels))

            # 통치 문서는 서명 없이 발행된다 — 비준은 정본 확정·비준증빙이고
            # 서명은 인스턴스의 수용 기록이다 (헌법 14조 1항·시행령 §10 2항).
            # 통치 구획 안의 노드형은 특수 노드로서 정상이다.
            check("통치 구획의 노드형은 지식 가드에 걸리지 않는다",
                  not publish.guard_knowledge(items),
                  publish.guard_knowledge(items))

            # 지식 유출 — 통치 구획 밖의 노드형 파일
            man2 = Path(td) / "m2.txt"
            man2.write_text("MAP  = Scope/W1/ -> nodes/\n", encoding="utf-8")
            leak = ROOT / "= Scope/W1/pub-leak.md"
            leak.write_text(node_text("260802-pppp-0002", "새어나갈 노드"),
                            encoding="utf-8")
            try:
                i2 = publish.collect(publish.parse_manifest(man2))
                check("Space의 노드형 파일은 차단",
                      any("노드형" in e for e in publish.guard_knowledge(i2)),
                      publish.guard_knowledge(i2))
            finally:
                leak.unlink(missing_ok=True)

            # 비밀값
            rec.write_text('token = "ghp_' + "A" * 36 + '"\n', encoding="utf-8")
            se = publish.guard_secrets(items)
            check("비밀값이 들어 있으면 차단", any("비밀값" in e for e in se), se)
            check("보고에 비밀값 자체는 싣지 않는다",
                  not any("ghp_" in e for e in se))
            rec.write_text(node_text("260802-pppp-0010", "발행 시험 사료"),
                           encoding="utf-8")

            # 보고 모드는 아무것도 쓰지 않는다
            before = sorted(str(x.relative_to(pub)) for x in pub.rglob("*")
                            if x.is_file() and ".git/" not in str(x))
            p_items = [(s, r) for s, r in items]
            rep = publish.plan(pub, m, p_items)
            check("보고가 add를 정확히 센다",
                  len(rep["add"]) == len(p_items), rep)
            check("KEEP 파일은 remove에 들어가지 않는다",
                  "LICENSE" not in rep["remove"], rep["remove"])
            check("보고는 공개 트리를 건드리지 않는다",
                  sorted(str(x.relative_to(pub)) for x in pub.rglob("*")
                         if x.is_file() and ".git/" not in str(x)) == before)

            # 매니페스트 밖 파일이 디스크에 있어도 커밋되지 않는다
            # (`git add -A`가 가드를 통째로 우회한 실사고의 고정)
            _rep = validate.run()
            check("가드 전제: 검증기 PASS", _rep["verdict"] == "PASS",
                  _rep["fail"])
            # 비ASCII 이름 — git ls-files의 기본 출력은 이 이름을 따옴표와
            # 8진 이스케이프로 감싼다. 그 문자열을 그대로 쓰면 want와 어긋나
            # 매번 remove로 잡히고 스테이지도 삭제도 빗나간다.
            ko = gov / "records" / "한글 사료.md"
            ko.write_text(node_text("260802-pppp-0011", "한글 이름 사료"),
                          encoding="utf-8")
            mine.append(ko)
            items = publish.collect(m)
            p_ko = publish.plan(pub, m, items)
            check("비ASCII 경로가 인용부호 없이 판독된다",
                  not any(r.startswith('"') for r in p_ko["remove"]),
                  p_ko["remove"][:3])
            check("새 한글 파일은 add로 잡힌다",
                  "_governance/records/한글 사료.md" in p_ko["add"], p_ko["add"])
            stray = pub / "stray-leftover.md"
            stray.write_text(node_text("260802-pppp-0003", "떠도는 잔재"),
                             encoding="utf-8")
            rep_a = publish.run(pub, apply=True, message="가드 시험",
                                manifest=man)
            check("적용: 커밋됨", rep_a.get("committed"), rep_a)
            tracked = subprocess.run(
                ["git", "-C", str(pub), "ls-files", "-z"], capture_output=True,
                text=True).stdout.split("\0")
            check("매니페스트 밖 파일은 커밋되지 않는다",
                  "stray-leftover.md" not in tracked, tracked)
            check("그 파일은 디스크에 그대로 남는다", stray.exists())
            check("한글 이름 파일은 실제로 커밋된다",
                  "_governance/records/한글 사료.md" in tracked, tracked)
            p_after = publish.plan(pub, m, publish.collect(m))
            check("두 번째 발행에서 remove로 되잡히지 않는다",
                  not p_after["remove"] and not p_after["add"], p_after)
            stray.unlink()

            # 적용 — 가드를 직접 통과시킨 뒤 build로
            publish.build(p_items, m, pub)
            check("통치 문서가 공개에 있다", (pub / "_governance/PubDoc.md").exists())
            check("대장은 공개에 없다",
                  not (pub / "_governance/_ledger").exists())
            check("골격이 생긴다", (pub / "= Scope/.gitkeep").exists())
    finally:
        for f in mine:
            f.unlink(missing_ok=True)
        (gov / "_ledger").rmdir() if led.is_dir() and not any(led.iterdir()) else None
        if (gov / "records").is_dir() and not any((gov / "records").iterdir()):
            (gov / "records").rmdir()


# ── 15. 정합성 검사 — 충돌 후보 검출 (헌법 12조 1·2항) ─────────────────
def test_conflict_candidates():
    a = ROOT / "= Scope/W1/regr-heir-a.md"
    b = ROOT / "= Scope/W1/regr-heir-b.md"
    c = ROOT / "= Scope/W1/regr-ancestor.md"
    try:
        c.write_text(node_text("260802-zzzz-rg20", "선행"), encoding="utf-8")
        a.write_text(node_text("260802-zzzz-rg21", "후계 A", "본문",
                               'replaces: "[[regr-ancestor]]"\n'), encoding="utf-8")
        b.write_text(node_text("260802-zzzz-rg22", "후계 B", "본문",
                               'replaces: "[[= Scope/W1/regr-ancestor]]"\n'),
                     encoding="utf-8")
        cands = validate.conflict_candidates(graph.Index())
        check("lineage-fork 검출(경로형 표기 포함)",
              any("lineage-fork" in x and "regr-ancestor" in x for x in cands), cands)
        rep = validate.run()
        check("충돌 후보는 검증기 FAIL로 사용자 심의 요청",
              rep["verdict"] == "FAIL"
              and any("정합성 검사" in list(f)[0] for f in rep["fail"]))
        before = len(core.ledger_read(core.CANDIDATES))
        validate.run()
        check("검증기는 후보를 대장에 자동 기록하지 않는다(자동 집행 없음)",
              len(core.ledger_read(core.CANDIDATES)) == before)
    finally:
        for p_ in (a, b, c):
            p_.unlink(missing_ok=True)
    check("후보 해소 후 기준선 복귀",
          not any("lineage-fork" in x
                  for x in validate.conflict_candidates(graph.Index())))


# ── 15b. 정본 릴리스와 갱신 (Mechanism §1-2 · 시행령 §10 6항) ────────────
def test_release_and_update():
    from osk import release, update

    def git(root, *args):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True)

    mine = []
    try:
        with tempfile.TemporaryDirectory() as td:
            can = Path(td) / "canonical"
            (can / "_governance/_engine/scripts").mkdir(parents=True)
            # 릴리스는 **스냅샷 안의 엔진**으로 검증하므로 정본 픽스처도 엔진을
            # 담는다(실제 정본은 프레임워크 자신이므로 항상 담고 있다).
            shutil.copytree(ENGINE / "osk", can / "_governance/_engine/osk",
                            ignore=shutil.ignore_patterns("__pycache__"))
            (can / "_governance/records").mkdir()
            (can / "docs").mkdir()
            (can / "_governance/UpdDoc.md").write_text(
                node_text("260802-uupd-0002", "정본 규범 문서", "1조."),
                encoding="utf-8")
            (can / "_governance/records/갱신 사료.md").write_text(
                node_text("260802-uupd-0003", "갱신 사료"), encoding="utf-8")
            (can / "_governance/_engine/eng_upd.py").write_text("X = 1\n",
                                                                encoding="utf-8")
            (can / "_governance/_engine/scripts/publish-manifest.txt").write_text(
                "MAP  _governance/ -> _governance/\nMAP  docs/ -> docs/\n"
                "KEEP LICENSE\nKEEP README.md\nDENY _ledger/\nDENY __pycache__/\n"
                "SKEL = Scope/\nSKEL = UpdSkel/\n", encoding="utf-8")
            (can / "docs/UPD-SETUP.md").write_text("# 설치\n", encoding="utf-8")
            (can / "README.md").write_text("readme\n", encoding="utf-8")
            (can / "LICENSE").write_text("MIT\n", encoding="utf-8")
            git(can, "init", "-q")
            git(can, "config", "user.email", "t@t")
            git(can, "config", "user.name", "t")
            git(can, "add", "-A")
            git(can, "commit", "-qm", "base")

            def _rel(ver, root=None):
                """선언 + 안내대로 작업 트리 맞추기. release는 작업 트리를
                건드리지 않으므로(외부 수정 보호), 픽스처가 사용자를 대신해
                `git checkout <ver> -- release.json`을 수행한다."""
                root = root if root is not None else can
                rep_ = release.run(ver, apply=True, root=root)
                subprocess.run(["git", "-C", str(root), "checkout", ver, "--",
                                "release.json"], capture_output=True)
                return rep_

            rep = _rel("v9.0.0")
            check("릴리스 선언: 증빙 생성·커밋·태그",
                  rep["applied"] and rep.get("tagged") == "v9.0.0", rep)
            att = json.loads((can / "release.json").read_text(encoding="utf-8"))
            check("증빙은 자신을 담지 않는다", "release.json" not in att["files"])
            check("증빙이 전 파일을 덮는다",
                  "_governance/UpdDoc.md" in att["files"]
                  and "README.md" in att["files"], sorted(att["files"])[:5])

            def uerr(f):
                try:
                    f()
                    return None
                except (release.ReleaseError, update.UpdateError) as e:
                    return str(e)
            check("중복 버전은 선언 전에 거부(버전 불변)",
                  "이미 선언된 버전" in (uerr(lambda: release.run(
                      "v9.0.0", apply=True, root=can)) or ""))

            # 보고 모드 — 아무것도 쓰지 않는다. KEEP은 정본 저장소 전용.
            r0 = update.run(source="bundle", bundle=str(can))
            check("갱신 보고: applied=False", r0["ok"] and not r0["applied"], r0)
            check("KEEP은 적용 대상이 아니다",
                  "_governance/UpdDoc.md" in r0["add"]
                  and all("README" not in x and "LICENSE" not in x
                          for x in r0["add"]), r0["add"])
            check("보고는 쓰지 않는다", not (ROOT / "docs/UPD-SETUP.md").exists())

            # 최초 편입 — 빈 저널에서 adopt는 사전 존재(다른 내용) 파일도 정본으로
            # 기준선 삼는다. adopt는 이 최초 편입에만 허용된다(P2).
            (ROOT / "_governance").mkdir(parents=True, exist_ok=True)
            (ROOT / "_governance/UpdDoc.md").write_text(
                "기존 인스턴스의 다른 내용\n", encoding="utf-8")
            check("최초 편입 전 저널은 비어 있다", not update.has_history())
            # 적용 — 파일·골격·저널
            r1 = update.run(source="bundle", bundle=str(can), apply=True,
                            adopt=True)
            check("adopt 최초 편입: 사전 존재 파일을 정본으로 덮는다",
                  "정본 규범 문서" in
                  (ROOT / "_governance/UpdDoc.md").read_text(encoding="utf-8"))
            check("편입 후 관리 이력이 생긴다", update.has_history())
            mine += [ROOT / "_governance/UpdDoc.md",
                     ROOT / "_governance/records/갱신 사료.md",
                     ROOT / "_governance/_engine/eng_upd.py",
                     ROOT / "docs/UPD-SETUP.md",
                     ROOT / "_governance/_engine/scripts/publish-manifest.txt"]
            check("적용: 파일이 들어온다",
                  (ROOT / "_governance/UpdDoc.md").exists()
                  and (ROOT / "docs/UPD-SETUP.md").exists(), r1)
            # 허용 밖 골격(`= UpdSkel`)은 만들어지지 않고, 허용 루트는 이미 있으면
            # 그대로 둔다 — 골격은 **최상위 Space 루트 셋**에만 허용된다 (P1)
            check("허용 밖 SKEL은 만들어지지 않는다",
                  not (ROOT / "= UpdSkel").exists())
            check("_allowed_skel: 최상위 Space 루트만 허용",
                  update._allowed_skel("= Scope") is not None
                  and update._allowed_skel("= Domain") is not None
                  and update._allowed_skel("= Scope/UserData") is None
                  and update._allowed_skel("= Scope/UserData/newdir") is None
                  and update._allowed_skel("= NotASpace") is None)
            recs = core.ledger_read(update.UPDATE_JOURNAL)
            check("저널: begin·apply·done",
                  {"begin", "apply", "done"} <= {r.get("kind") for r in recs})
            check("현재 버전 판정", update.current_version() == "v9.0.0")

            # 멱등 — 재실행은 전부 same
            r2 = update.run(source="bundle", bundle=str(can))
            check("재실행은 전부 same",
                  not r2["add"] and not r2["update"] and not r2["conflict"], r2)

            # 문서 드리프트 → 덮지 않고 사이드카
            (ROOT / "_governance/UpdDoc.md").write_text(
                node_text("260802-uupd-0002", "정본 규범 문서", "로컬 개정."),
                encoding="utf-8")
            r3 = update.run(source="bundle", bundle=str(can))
            check("로컬 수정 문서는 conflict",
                  "_governance/UpdDoc.md" in r3["conflict"], r3)
            r4 = update.run(source="bundle", bundle=str(can), apply=True)
            side = ROOT / "_governance/UpdDoc.md.upstream-v9.0.0"
            mine.append(side)
            check("사이드카가 생기고 원본은 보존",
                  side.exists() and "로컬 개정" in
                  (ROOT / "_governance/UpdDoc.md").read_text(encoding="utf-8"), r4)

            # 엔진 드리프트 → 갱신 전체 중단. 이미 관리 중이면 adopt는 force로
            # 쓰이지 않는다(최초 편입 전용) — 거부된다(P2).
            (ROOT / "_governance/_engine/eng_upd.py").write_text("X = 2\n",
                                                                 encoding="utf-8")
            e = uerr(lambda: update.run(source="bundle", bundle=str(can),
                                        apply=True))
            check("엔진 로컬 수정은 갱신 전체 중단", e is not None and "엔진" in e, e)
            e2 = uerr(lambda: update.run(source="bundle", bundle=str(can),
                                         apply=True, adopt=True))
            check("관리 중 인스턴스에서 adopt는 거부(최초 편입 전용)",
                  e2 is not None and "최초 편입" in e2, e2)
            # 사용자가 로컬 수정을 정리(정본 내용으로 복원) → 이후 갱신 정상
            (ROOT / "_governance/_engine/eng_upd.py").write_text("X = 1\n",
                                                                 encoding="utf-8")

            # 사용자가 사이드카 충돌을 수용(로컬 개정 폐기, upstream v9.0.0 복원)
            # → 이 문서의 baseline과 일치해 다음 갱신이 깨끗이 덮을 수 있다.
            (ROOT / "_governance/UpdDoc.md").write_text(
                node_text("260802-uupd-0002", "정본 규범 문서", "1조."),
                encoding="utf-8")

            # 갱신이 통치 문서를 덮으면 서명이 자동으로 풀린다 — 재서명이 수용
            # 기록이다 (Mechanism §1-2 6항 · 시행령 §10 2항)
            gp = ROOT / "_governance/UpdDoc.md"
            S.sign(gp, "수용 시험", "260802-uupd-0002")
            check("갱신 후 수용 서명 성립",
                  S.status("260802-uupd-0002", gp) == "signed")
            (can / "_governance/UpdDoc.md").write_text(
                node_text("260802-uupd-0002", "정본 규범 문서", "1조 개정."),
                encoding="utf-8")
            git(can, "add", "-A")
            git(can, "commit", "-qm", "gov v2")
            _rel("v9.0.1")
            update.run(source="bundle", bundle=str(can), apply=True)
            check("갱신이 덮으면 서명이 풀린다(수용 재확인 대기)",
                  S.status("260802-uupd-0002", gp) == "unsigned"
                  and "1조 개정" in gp.read_text(encoding="utf-8"))
            check("갱신 후 현재 버전 갱신", update.current_version() == "v9.0.1")

            # 비준증빙 위반 — 변조·부재는 중단. 증빙 밖 미추적 파일은 허용.
            (can / "docs/UPD-SETUP.md").write_text("# 변조\n", encoding="utf-8")
            e = uerr(lambda: update.run(source="bundle", bundle=str(can)))
            check("해시 불일치는 중단", e is not None and "해시 불일치" in e, e)
            (can / "docs/UPD-SETUP.md").write_text("# 설치\n", encoding="utf-8")
            # 증빙 밖(미추적) 파일은 적용되지 않으므로 갱신을 막지 않는다 —
            # 적용은 오직 증빙이 모는 파일만 하고 그 하나하나가 해시 검증된다
            # (pyc·.DS_Store 부산물이 딸린 디렉터리 bundle을 못 쓰게 하지 않는다)
            (can / "sneaky.md").write_text("x\n", encoding="utf-8")
            r7 = update.run(source="bundle", bundle=str(can))
            check("증빙 밖 파일은 갱신을 막지 않고 적용되지도 않는다",
                  r7["ok"] and not (ROOT / "sneaky.md").exists(), r7)
            (can / "sneaky.md").unlink()
            e = uerr(lambda: update.run(source="bundle",
                                        bundle=str(Path(td) / "noatt-없는트리")))
            check("비준증빙 없는 출처는 거부", e is not None, e)

            # 갱신 기본은 브랜치 HEAD가 아니라 최신 정식 릴리스 태그다 (P2)
            check("갱신 기본은 최신 릴리스 태그",
                  update.latest_release_tag(str(can)) == "v9.0.1",
                  update.latest_release_tag(str(can)))

            # 삭제 전파 — 정본에서 빠진 파일은 인스턴스에서도 제거(로컬 무수정) (P1)
            (can / "_governance/DropMe.md").write_text(
                node_text("260802-uupd-0009", "곧 삭제될 규범"), encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add DropMe")
            _rel("v9.0.2")
            update.run(source="bundle", bundle=str(can), apply=True)
            drop = ROOT / "_governance/DropMe.md"; mine.append(drop)
            check("삭제 전 파일이 관리된다", drop.exists())
            git(can, "rm", "-q", "_governance/DropMe.md")
            git(can, "commit", "-qm", "drop DropMe")
            _rel("v9.0.3")
            rdel = update.run(source="bundle", bundle=str(can), apply=True)
            check("정본에서 빠진 파일은 인스턴스에서도 삭제된다",
                  not drop.exists()
                  and "_governance/DropMe.md" in rdel.get("removed", []), rdel)

            # 삭제 대상이라도 로컬 수정이 있으면 보존·보고 (P1)
            (can / "_governance/KeepMe.md").write_text(
                node_text("260802-uupd-000a", "삭제되나 보존"), encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add KeepMe")
            _rel("v9.0.4")
            update.run(source="bundle", bundle=str(can), apply=True)
            keep = ROOT / "_governance/KeepMe.md"; mine.append(keep)
            keep.write_text(node_text("260802-uupd-000a", "로컬에서 고쳤다"),
                            encoding="utf-8")
            git(can, "rm", "-q", "_governance/KeepMe.md")
            git(can, "commit", "-qm", "drop KeepMe")
            _rel("v9.0.5")
            rkeep = update.run(source="bundle", bundle=str(can), apply=True)
            check("로컬 수정된 삭제 대상은 보존된다",
                  keep.exists()
                  and "_governance/KeepMe.md" in rkeep.get("remove_conflict", []),
                  rkeep)

            # 사전존재·동일내용 파일도 기준선을 남긴다 — 다음 릴리스 drift 오판 금지 (P1)
            probe = "_governance/_engine/adopt_probe.py"
            (can / probe).write_text("P = 1\n", encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add probe")
            _rel("v9.0.6")
            (ROOT / probe).parent.mkdir(parents=True, exist_ok=True)
            (ROOT / probe).write_text("P = 1\n", encoding="utf-8")  # 저널 없이 존재
            mine.append(ROOT / probe)
            rp = update.run(source="bundle", bundle=str(can))
            check("사전존재·동일내용은 rebaseline", probe in rp["rebaseline"], rp)
            update.run(source="bundle", bundle=str(can), apply=True)   # 기준선 기록
            (can / probe).write_text("P = 2\n", encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "probe v2")
            _rel("v9.0.7")
            # 기준선이 없었다면 base=None→engine_drift로 갱신 전체가 중단됐을 것
            rp2 = update.run(source="bundle", bundle=str(can), apply=True)
            check("기준선 덕에 엔진 drift 오판 없이 깨끗이 갱신",
                  (ROOT / probe).read_text(encoding="utf-8") == "P = 2\n", rp2)

            # 동명 브랜치가 태그를 가리지 못한다 — refs/tags 명시 fetch (P1)
            # 태그 v9.0.0과 동명인 브랜치를 HEAD(다른·최신 커밋)에 만들어 둔다
            git(can, "branch", "v9.0.0", "HEAD")
            tf = update.fetch_git(str(can), "v9.0.0", Path(td) / "ftag")
            relf = json.loads((tf / "release.json").read_text(encoding="utf-8"))
            check("동명 브랜치가 있어도 태그의 커밋을 받는다",
                  relf["version"] == "v9.0.0", relf.get("version"))
            git(can, "branch", "-q", "-D", "v9.0.0")

            # 손상된 update.jsonl은 검증기가 FAIL로 잡는다 — 단순 로그가 아니다 (P2)
            uj = core.LEDGER / "update.jsonl"
            orig_uj = uj.read_text(encoding="utf-8") if uj.exists() else ""
            with open(uj, "a", encoding="utf-8") as _f:
                _f.write('{"rid":"bad-rid","kind":"apply","path":"X",'
                         '"hash":"h"}\n')
            repv = validate.run()
            check("손상된 update.jsonl은 검증기 FAIL",
                  repv["verdict"] == "FAIL"
                  and any("update.jsonl" in str(v) for v in repv["fail"]),
                  repv["fail"])
            uj.write_text(orig_uj, encoding="utf-8")

            # 선언 도중 외부 커밋이 들어오면 **CAS**가 막고 아무것도 남지 않는다 (P1)
            def _head(r):
                return subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"],
                                      capture_output=True, text=True).stdout.strip()
            _real_bt = release.build_attestation

            def _outsider_commits(root_, ver_, ref_="HEAD"):
                att_ = _real_bt(root_, ver_, ref_)
                # 증빙을 뜬 뒤·설치 전에 다른 프로세스가 커밋한다
                (can / "outsider.md").write_text("외부 작업\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(can), "add", "-A"],
                               capture_output=True)
                subprocess.run(["git", "-C", str(can), "commit", "-qm",
                                "outsider"], capture_output=True)
                return att_
            release.build_attestation = _outsider_commits
            try:
                e = uerr(lambda: release.run("v9.3.0", apply=True, root=can))
            finally:
                release.build_attestation = _real_bt
            outsider_head = _head(can)
            check("외부 커밋이 끼어들면 CAS가 선언을 막는다",
                  e is not None and "CAS" in e, e)
            check("외부 커밋은 파괴되지 않는다(HEAD 유지)",
                  (can / "outsider.md").exists()
                  and subprocess.run(["git", "-C", str(can), "log", "-1",
                                      "--format=%s"], capture_output=True,
                                     text=True).stdout.strip() == "outsider",
                  outsider_head)
            check("실패 선언의 태그는 남지 않는다",
                  not subprocess.run(["git", "-C", str(can), "tag", "-l",
                                      "v9.3.0"], capture_output=True,
                                     text=True).stdout.strip())

            # 변조 pre-commit hook은 --no-verify로 무력화 — self-invalid 릴리스 방지 (P2)
            probe2 = can / "_governance" / "hooktouch.md"
            probe2.write_text(node_text("260802-uupd-000c", "훅 미변조 확인"),
                              encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add hooktouch")
            hook = can / ".git" / "hooks" / "pre-commit"
            hook.parent.mkdir(parents=True, exist_ok=True)
            # 훅이 tracked 파일을 수정·stage하려 시도한다(우회되면 원본 유지)
            hook.write_text(
                "#!/bin/sh\n"
                'printf "TAMPERED\\n" >> "_governance/hooktouch.md"\n'
                'git add "_governance/hooktouch.md"\n', encoding="utf-8")
            hook.chmod(0o755)
            before = probe2.read_text(encoding="utf-8")
            _rel("v9.4.0")
            check("release 커밋은 변조 hook을 태우지 않는다(--no-verify)",
                  probe2.read_text(encoding="utf-8") == before
                  and "TAMPERED" not in probe2.read_text(encoding="utf-8"))
            hook.unlink()

            # clean-tree 면제는 정확히 release.json만 — release.json.bak은 아니다 (P2)
            (can / "release.json.bak").write_text("x\n", encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add bak")
            _rel("v9.1.0")
            (can / "release.json.bak").write_text("변조\n", encoding="utf-8")
            check("release.json.bak 수정은 clean-tree에서 면제되지 않는다",
                  "깨끗하지 않다" in (uerr(lambda: release.run(
                      "v9.1.1", apply=True, root=can)) or ""))
            git(can, "checkout", "-q", "--", "release.json.bak")

            # release 보고가 직전 릴리스 대비 삭제(removed)를 포함한다 (P3)
            (can / "_governance/RmRep.md").write_text(
                node_text("260802-uupd-000b", "삭제 보고 대상"), encoding="utf-8")
            git(can, "add", "-A"); git(can, "commit", "-qm", "add RmRep")
            _rel("v9.2.0")
            git(can, "rm", "-q", "_governance/RmRep.md")
            git(can, "commit", "-qm", "rm RmRep")
            rrep = release.run("v9.2.1", apply=False, root=can)
            check("release 보고가 removed에 삭제 파일을 담는다",
                  "_governance/RmRep.md" in (rrep.get("removed") or []), rrep)

            # 인스턴스 소유 바닥·SKEL 봉쇄 — 악의 매니페스트도 못 쓴다(엔진 상수) (P1)
            # apply_set 단계에서 봉쇄되는지를 본다(쓰기 없음 — 대량삭제 부작용 회피)
            ev = Path(td) / "evil"
            (ev / "_governance/_engine/scripts").mkdir(parents=True)
            (ev / "= Scope").mkdir()
            (ev / "= Scope/침투.md").write_text("x\n", encoding="utf-8")
            (ev / "_governance/x/_ledger").mkdir(parents=True)
            (ev / "_governance/x/_ledger/x.jsonl").write_text("{}\n",
                                                              encoding="utf-8")
            (ev / "_governance/_engine/scripts/publish-manifest.txt").write_text(
                'MAP  = Scope/ -> = Scope/\nMAP  _governance/ -> _governance/\n'
                'SKEL = Scope/Workbench/_ledger\nSKEL ../escape\n',
                encoding="utf-8")
            att2 = {"version": "v9.6.6", "at": core.now_iso(),
                    "files": {core.posix_rel(f, ev): core.sha256_file(f)
                              for f in ev.rglob("*") if f.is_file()}}
            (ev / "release.json").write_text(
                json.dumps(att2, ensure_ascii=False), encoding="utf-8")
            ev_rel = update.load_release(ev)
            ev_targets, ev_skel, ev_skipped = update.apply_set(ev, ev_rel)
            ev_dests = [d for _s, d in ev_targets]      # apply_set는 (src,dest) 사상
            check("Space 바닥은 target이 아니라 skip(침투 차단)",
                  "= Scope/침투.md" not in ev_dests
                  and any("침투" in s for s in ev_skipped), (ev_dests, ev_skipped))
            check("_ledger 조각도 target이 아니다",
                  "_governance/x/_ledger/x.jsonl" not in ev_dests, ev_dests)
            check("SKEL의 바닥 파고들기·루트 탈출은 허용 밖",
                  ev_skel == []
                  and sum("SKEL 허용 밖" in s for s in ev_skipped) == 2,
                  (ev_skel, ev_skipped))
            check("_allowed_skel: 빈 Space 루트만 허용",
                  update._allowed_skel("= Scope") is not None
                  and update._allowed_skel("= Scope/Workbench/_ledger") is None
                  and update._allowed_skel("../evil") is None
                  and update._allowed_skel("_ledger") is None)

            # 경로 봉쇄 — 증빙 key·저널 path의 vault 탈출 차단 (P1)
            check("_within: 정상 상대경로는 통과",
                  update._within(ROOT, "_governance/Constitution.md") is not None)
            check("_within: 상위 탈출·절대경로는 봉쇄",
                  update._within(ROOT, "_governance/../../payload") is None
                  and update._within(ROOT, "../escape") is None
                  and update._within(ROOT, "/etc/passwd") is None)
            esc = {"version": "v1.0.0",
                   "files": {"_governance/../../payload": "sha256:00"}}
            check("증빙 경로 탈출은 대조 단계에서 중단",
                  any("트리 밖" in e for e in update.verify_attestation(can, esc)),
                  update.verify_attestation(can, esc))

            # ROOT 내부 symlink alias는 경로 정체성 훼손으로 거부 — 다른 파일로
            # write 재지정 차단 (P1). docs가 _governance로 가는 symlink라 하자.
            syroot = Path(td) / "syroot"
            (syroot / "_governance").mkdir(parents=True)
            (syroot / "_governance" / "target.md").write_text("t\n",
                                                              encoding="utf-8")
            try:
                (syroot / "docs").symlink_to(syroot / "_governance")
                sy_ok = True
            except OSError:
                sy_ok = True                     # symlink 불가 환경이면 통과 처리
            if (syroot / "docs").is_symlink():
                check("ROOT 내부 symlink alias(docs->_governance)는 거부",
                      update._canon_rel(syroot, "docs/target.md") is None
                      and update._canon_rel(syroot, "_governance/target.md")
                      == "_governance/target.md")

            # 적용 트랜잭션 — 도중 실패 시 파일 원상복구, 저널에 apply 안 남김 (P1)
            trbase = Path(td) / "txn"
            (trbase / "_governance/_engine/scripts").mkdir(parents=True)
            (trbase / "_governance/A.md").write_text(
                node_text("260802-uupd-000d", "A"), encoding="utf-8")
            (trbase / "_governance/B.md").write_text(
                node_text("260802-uupd-000e", "B"), encoding="utf-8")
            (trbase / "_governance/_engine/scripts/publish-manifest.txt"
             ).write_text("MAP  _governance/ -> _governance/\nDENY _ledger/\n"
                          "DENY __pycache__/\n", encoding="utf-8")
            att_t = {"version": "v1.0.0", "at": core.now_iso(),
                     "files": {core.posix_rel(f, trbase): core.sha256_file(f)
                               for f in trbase.rglob("*") if f.is_file()}}
            (trbase / "release.json").write_text(
                json.dumps(att_t, ensure_ascii=False), encoding="utf-8")
            # _write_atomic이 B.md write에서 OSError를 던지게 해 트랜잭션 실패 유발
            # (A는 써진 뒤 B에서 실패 → 원상복구로 A·B 모두 사라져야 한다)
            real_wa = update._write_atomic

            def _boom(dst, data):
                if dst.name == "B.md":
                    raise OSError("boom")
                return real_wa(dst, data)
            uj0 = core.LEDGER / "update.jsonl"
            j_before = uj0.read_text(encoding="utf-8") if uj0.exists() else ""
            update._write_atomic = _boom
            try:
                et = uerr(lambda: update.run(source="bundle", bundle=str(trbase),
                                             apply=True))
            finally:
                update._write_atomic = real_wa
            j_after = uj0.read_text(encoding="utf-8") if uj0.exists() else ""
            check("적용 중 실패는 UpdateError로 중단",
                  et is not None and "원상복구" in et, et)
            check("실패한 트랜잭션은 파일을 남기지 않는다(A·B 부재)",
                  not (ROOT / "_governance/A.md").exists()
                  and not (ROOT / "_governance/B.md").exists())
            new_j = j_after[len(j_before):]
            check("실패 트랜잭션은 apply 저널을 안 남긴다(begin·rollback만)",
                  '"kind": "apply"' not in new_j and '"kind": "rollback"' in new_j,
                  new_j[-300:])

            # 크래시-안전 트랜잭션: 커밋 여부로 rollback/roll-forward를 결정한다 (P1)
            victim_rel = "_governance/UpdDoc.md"
            victim = ROOT / victim_rel
            orig_v = victim.read_bytes()

            def _stage_txn(txn):
                """_txn_begin으로 실제 프로토콜대로 트랜잭션을 시작해 둔다."""
                update._txn_begin(txn, "v9.9.9", [victim_rel])

            # ① 미커밋(done 없음) → pre-image로 rollback
            _stage_txn("txnAAA")
            victim.write_bytes(b"HALF-APPLIED\n")     # 크래시로 남은 부분 적용
            act = update._txn_recover([{"kind": "begin", "txn": "txnAAA"}])
            check("미커밋 트랜잭션은 pre-image로 rollback",
                  act == "rollback" and victim.read_bytes() == orig_v
                  and not update.TXN_MANIFEST.exists(), act)
            # ② 커밋됨(done(txn) 있음) → 파일은 새 판 유지, 표식만 정리
            _stage_txn("txnBBB")
            victim.write_bytes(b"NEW-VERSION\n")
            act = update._txn_recover([{"kind": "done", "txn": "txnBBB"}])
            check("커밋된 트랜잭션은 roll-forward(파일 유지·표식 정리)",
                  act == "roll-forward"
                  and victim.read_bytes() == b"NEW-VERSION\n"
                  and not update.TXN_MANIFEST.exists(), act)
            victim.write_bytes(orig_v)
            # ③ 백업 손상 → fail-closed(백업 보존·중단)
            _stage_txn("txnCCC")
            (update.TXN_BACKUP / "000000").write_bytes(b"CORRUPT\n")
            ec = uerr(lambda: update._txn_recover([{"kind": "begin",
                                                    "txn": "txnCCC"}]))
            check("백업 손상은 fail-closed로 중단하고 txn 영역을 보존",
                  ec is not None and "손상" in ec and update.TXN_MANIFEST.is_file(),
                  ec)
            update._txn_clear()
            victim.write_bytes(orig_v)

            # 커밋 전 apply 기록은 판정에서 보이지 않는다 (파일↔저널 정합, P1)
            uncommitted = [
                {"rid": "00000000-0000-7000-8000-00000000c001", "parents": [],
                 "kind": "apply", "txn": "T1", "path": "F", "hash": "sha256:aa"},
            ]
            check("미커밋 apply는 baseline이 되지 않는다",
                  update.last_applied_hash(uncommitted, "F") is None
                  and "F" not in update.managed_paths(uncommitted)
                  and not update.has_history(uncommitted))
            committed_recs = uncommitted + [
                {"rid": "00000000-0000-7000-8000-00000000c002",
                 "parents": ["00000000-0000-7000-8000-00000000c001"],
                 "kind": "done", "txn": "T1"}]
            check("done(txn) 이후에는 같은 기록이 baseline이 된다",
                  update.last_applied_hash(committed_recs, "F") == "sha256:aa"
                  and update.has_history(committed_recs))

            # 미커밋 기록은 **후보**에서만 빠지고 인과 사슬은 남아야 한다 —
            # 목록에서 빼면 parents 간선이 끊겨 정상 복구·재적용 뒤에도 baseline이
            # 사라진다(T1 커밋 → T2 크래시·롤백 → T3 커밋). (P1)
            def _r(n):
                return f"00000000-0000-7000-8000-{n:012d}"
            chain3 = [
                {"rid": _r(11), "parents": [], "kind": "apply", "txn": "T1",
                 "path": "F", "hash": "sha256:old"},
                {"rid": _r(12), "parents": [_r(11)], "kind": "done", "txn": "T1"},
                {"rid": _r(13), "parents": [_r(12)], "kind": "apply", "txn": "T2",
                 "path": "F", "hash": "sha256:mid"},     # 크래시·롤백(미커밋)
                {"rid": _r(14), "parents": [_r(13)], "kind": "apply", "txn": "T3",
                 "path": "F", "hash": "sha256:new"},
                {"rid": _r(15), "parents": [_r(14)], "kind": "done", "txn": "T3"},
            ]
            check("미커밋이 사슬을 끊지 않는다(재적용 baseline 유지)",
                  update.last_applied_hash(chain3, "F") == "sha256:new"
                  and update.managed_paths(chain3) == {"F": "sha256:new"},
                  (update.last_applied_hash(chain3, "F"),
                   update.managed_paths(chain3)))
            check("미커밋만 있으면 직전 커밋 baseline이 남는다",
                  update.last_applied_hash(chain3[:3], "F") == "sha256:old",
                  update.last_applied_hash(chain3[:3], "F"))
            rm3 = chain3 + [
                {"rid": _r(16), "parents": [_r(15)], "kind": "remove",
                 "txn": "T4", "path": "F"},
                {"rid": _r(17), "parents": [_r(16)], "kind": "done", "txn": "T4"}]
            check("커밋된 remove가 극대면 관리에서 빠진다",
                  update.last_applied_hash(rm3, "F") is None
                  and update.managed_paths(rm3) == {})

            # release 증빙은 **커밋 트리**에서 뜬다 — guard 이후 working tree가
            # 바뀌어도 증빙이 커밋과 어긋나지 않는다(TOCTOU) (P1)
            wt_victim = can / "docs/UPD-SETUP.md"
            wt_orig = wt_victim.read_bytes()
            head_h = release.tree_hashes(can)
            wt_victim.write_bytes(b"EXTERNAL EDIT\n")        # 외부 프로세스 모사
            check("tree_hashes는 working tree 오염과 무관(커밋 트리를 읽는다)",
                  release.tree_hashes(can) == head_h,
                  "docs/UPD-SETUP.md")
            wt_victim.write_bytes(wt_orig)

            # 태그 전 전수 재대조 — 증빙이 커밋 트리와 다르면 선언을 중단한다 (P1)
            _real_ba = release.build_attestation

            def _bad_ba(root_, ver_, ref_="HEAD"):
                att_ = _real_ba(root_, ver_, ref_)
                k = sorted(att_["files"])[0]
                att_["files"][k] = "sha256:" + "0" * 64      # 어긋난 증빙
                return att_
            pre_head_r = subprocess.run(
                ["git", "-C", str(can), "rev-parse", "HEAD"],
                capture_output=True, text=True).stdout.strip()
            release.build_attestation = _bad_ba
            try:
                ebad = uerr(lambda: release.run("v9.5.0", apply=True, root=can))
            finally:
                release.build_attestation = _real_ba
            post_head_r = subprocess.run(
                ["git", "-C", str(can), "rev-parse", "HEAD"],
                capture_output=True, text=True).stdout.strip()
            tags_r = subprocess.run(["git", "-C", str(can), "tag", "-l", "v9.5.0"],
                                    capture_output=True, text=True).stdout.strip()
            check("증빙≠커밋 트리면 태그 전에 중단한다",
                  ebad is not None and "커밋 트리가 증빙과 다르다" in ebad, ebad)
            check("중단 시 커밋·태그가 남지 않는다",
                  post_head_r == pre_head_r and not tags_r,
                  (post_head_r == pre_head_r, tags_r))

            # CAS 구조에서는 index·working tree 변조가 릴리스 커밋에 **영향을
            # 주지 못한다** — 커밋을 object로 직접 만들기 때문이다(공격 벡터 소멸).
            _real_run = release.subprocess.run

            def _tamper_index(cmd, *a, **k):
                if isinstance(cmd, list) and "commit-tree" in cmd:
                    (can / "release.json").write_text(
                        json.dumps({"version": "vTAMPERED", "at": "x",
                                    "files": {}}), encoding="utf-8")
                    _real_run(["git", "-C", str(can), "add", "--",
                               "release.json"], capture_output=True)
                return _real_run(cmd, *a, **k)
            release.subprocess.run = _tamper_index
            try:
                rep_t = _rel("v9.6.0")
            finally:
                release.subprocess.run = _real_run
            _tagged_att = json.loads(subprocess.run(
                ["git", "-C", str(can), "show", "v9.6.0:release.json"],
                capture_output=True, text=True).stdout)
            check("index 변조는 릴리스 커밋에 영향을 주지 못한다",
                  rep_t.get("applied") and _tagged_att["version"] == "v9.6.0",
                  _tagged_att.get("version"))
            check("태그는 검증한 그 커밋에 붙는다",
                  subprocess.run(["git", "-C", str(can), "rev-parse", "v9.6.0^{commit}"],
                                 capture_output=True, text=True).stdout.strip()
                  == rep_t.get("commit"), rep_t.get("commit"))

            # 매니페스트가 증빙에 없으면 control plane으로 쓰지 않는다 (P2)
            _noman = Path(td) / "noman"
            (_noman / "_governance/_engine/scripts").mkdir(parents=True)
            (_noman / "_governance/X.md").write_text(
                node_text("260802-uupd-000f", "증빙된 파일"), encoding="utf-8")
            (_noman / "_governance/_engine/scripts/publish-manifest.txt"
             ).write_text("MAP  _governance/ -> _governance/\n", encoding="utf-8")
            _att_nm = {"version": "v1.0.0", "at": core.now_iso(),
                       "files": {"_governance/X.md":
                                 core.sha256_file(_noman / "_governance/X.md")}}
            (_noman / "release.json").write_text(
                json.dumps(_att_nm, ensure_ascii=False), encoding="utf-8")
            enm = uerr(lambda: update.apply_set(_noman,
                                                update.load_release(_noman)))
            check("증빙에 없는 매니페스트는 적용 범위로 쓰지 않는다",
                  enm is not None and "비준증빙에 없다" in enm, enm)

            # 실행 비트가 붙은 파일은 릴리스에서 거부 — 증빙이 mode를 안 싣는다 (P2)
            _exe = can / "runme.sh"
            _exe.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            _exe.chmod(0o755)
            subprocess.run(["git", "-C", str(can), "add", "-A"],
                           capture_output=True)
            subprocess.run(["git", "-C", str(can), "commit", "-qm", "exe"],
                           capture_output=True)
            eexe = uerr(lambda: release.tree_hashes(can))
            check("100755(실행 비트)도 릴리스에서 거부",
                  eexe is not None and "지원하지 않는" in eexe, eexe)
            subprocess.run(["git", "-C", str(can), "rm", "-q", "-f", "runme.sh"],
                           capture_output=True)
            subprocess.run(["git", "-C", str(can), "commit", "-qm", "drop exe"],
                           capture_output=True)

            # tree_hashes는 git object tree를 읽는다 — symlink는 지원 밖으로 거부 (P2)
            (can / "linky").symlink_to("README.md")
            subprocess.run(["git", "-C", str(can), "add", "-A"],
                           capture_output=True)
            subprocess.run(["git", "-C", str(can), "commit", "-qm", "symlink"],
                           capture_output=True)
            esym = uerr(lambda: release.tree_hashes(can))
            check("symlink 등 비정규 mode는 릴리스에서 거부",
                  esym is not None and "지원하지 않는" in esym, esym)
            subprocess.run(["git", "-C", str(can), "rm", "-q", "--cached",
                            "linky"], capture_output=True)
            (can / "linky").unlink()
            subprocess.run(["git", "-C", str(can), "commit", "-qm", "drop link"],
                           capture_output=True)

            # 사이드카 충돌 판정은 **삭제 예정 경로**도 본다 (P2)
            _p_rm = {"conflict": [("_governance/UpdDoc.md", "_governance/A.md")],
                     "remove": ["_governance/A.md.upstream-v9.9.9"]}
            _coll = update._sidecar_plan(
                _p_rm, can, "v9.9.9",
                set() | set(_p_rm["remove"]))[3]
            check("삭제 예정 경로와 겹치는 사이드카는 충돌로 잡는다",
                  "_governance/A.md.upstream-v9.9.9" in _coll, _coll)

            # 디렉터리 rollback 실패는 fail-closed — 허용은 ENOENT·ENOTEMPTY뿐 (P2)
            update._txn_begin("txnDF", "v9.9.9", ["_governance/dfx/f.md"])
            _saved_rmdir = update.Path.rmdir

            def _boom_rmdir(self):
                raise OSError(errno.EACCES, "denied")
            try:
                update.Path.rmdir = _boom_rmdir
                edf = uerr(lambda: update._txn_recover(
                    [{"kind": "begin", "txn": "txnDF"}]))
                check("디렉터리 복구의 권한 오류는 fail-closed",
                      edf is not None and "디렉터리 복구 실패" in edf, edf)
            finally:
                update.Path.rmdir = _saved_rmdir
                update._txn_clear()

            # 새 디렉터리도 rollback 대상 — 파일만 되돌리면 SKEL 잔재가 남는다 (P2)
            deep = "_governance/_engine/newpkg/sub/mod.py"
            update._txn_begin("txnDIR", "v9.9.9", [deep])
            man_d = json.loads(update.TXN_MANIFEST.read_text(encoding="utf-8"))
            check("manifest가 새 디렉터리를 기록한다",
                  "_governance/_engine/newpkg" in (man_d.get("dirs") or [])
                  and "_governance/_engine/newpkg/sub" in (man_d.get("dirs") or []),
                  man_d.get("dirs"))
            update._write_atomic(ROOT / deep, b"X = 1\n")   # 디렉터리까지 생성
            check("적용으로 깊은 디렉터리가 생긴다",
                  (ROOT / "_governance/_engine/newpkg/sub").is_dir())
            act_d = update._txn_recover([{"kind": "begin", "txn": "txnDIR"}])
            check("rollback이 파일과 새 디렉터리를 모두 되돌린다",
                  act_d == "rollback"
                  and not (ROOT / deep).exists()
                  and not (ROOT / "_governance/_engine/newpkg").exists(), act_d)

            # 사이드카는 사용자 작업을 덮지 않는다 (P1)
            sc_dest = "_governance/UpdDoc.md"
            sc_side = sc_dest + ".upstream-v9.9.9"
            (ROOT / sc_side).write_text("사용자 수동 병합 작업\n", encoding="utf-8")
            mine.append(ROOT / sc_side)
            _pfake = {"conflict": [("_governance/UpdDoc.md", sc_dest)]}
            w, kept, held, coll = update._sidecar_plan(_pfake, can, "v9.9.9", set())
            check("다른 내용의 기존 사이드카는 손대지 않는다(보존·보고)",
                  not w and not kept and sc_side in held and not coll,
                  (w, kept, held, coll))
            (ROOT / sc_side).write_bytes((can / "_governance/UpdDoc.md").read_bytes())
            w2, kept2, held2, _c = update._sidecar_plan(_pfake, can, "v9.9.9", set())
            check("같은 내용이면 그대로 인정(재기록 없음)",
                  not w2 and sc_side in kept2 and not held2, (w2, kept2, held2))
            _c2 = update._sidecar_plan(_pfake, can, "v9.9.9", {sc_side})[3]
            check("사이드카가 관리 파일과 겹치면 충돌로 잡는다",
                  sc_side in _c2, _c2)
            (ROOT / sc_side).unlink(missing_ok=True)

            # 저널에 부분 행이 남아도 복구에 도달한다 — 크래시가 append 도중일
            # 때 엄격 판독으로 막히면 '다음 실행이 복구'가 성립하지 않는다 (P1).
            # 실제 저널을 오염시키지 않도록 임시 저널로 격리해 검사한다.
            _saved_j = update.UPDATE_JOURNAL
            _tmp_j = Path(td) / "uj_partial.jsonl"
            _tmp_j.write_text('{"kind": "done", "txn": "TOK"}\n'
                              '{"kind": "begin", "txn": "TPART"',   # 잘린 행
                              encoding="utf-8")
            try:
                update.UPDATE_JOURNAL = _tmp_j
                lenient = update._journal_lenient()
                check("관대한 판독은 부분 행을 건너뛰고 나머지를 읽는다",
                      [r.get("txn") for r in lenient] == ["TOK"], lenient)
                try:
                    core.ledger_read(_tmp_j)
                    _strict_failed = False
                except ValueError:
                    _strict_failed = True     # 엄격 판독은 손상으로 본다
                check("엄격 판독은 같은 저널에서 손상을 보고한다", _strict_failed)
            finally:
                update.UPDATE_JOURNAL = _saved_j
            # 부분 행뿐인 저널에서도 복구는 수행된다(목록이 비어도 rollback)
            victim2 = ROOT / "_governance/UpdDoc.md"
            orig_v2 = victim2.read_bytes()
            update._txn_begin("txnPART2", "v9.9.9", ["_governance/UpdDoc.md"])
            victim2.write_bytes(b"HALF\n")
            actp = update._txn_recover([])          # done 기록을 못 읽은 상황
            check("판독 가능한 done이 없으면 복구는 rollback",
                  actp == "rollback" and victim2.read_bytes() == orig_v2, actp)

            # 같은 버전인데 릴리스 identity가 다르면 거부 — 태그 force-move 방어 (P1)
            check("적용 완료 기록에 릴리스 identity(attest)가 남는다",
                  any(r.get("kind") == "done" and r.get("attest")
                      for r in core.ledger_read(core.LEDGER / "update.jsonl")))
            _ver_now = update.current_version()
            if _ver_now:                     # 같은 버전으로 다시 릴리스(force-move 모사)
                forced = Path(td) / "forced"
                shutil.copytree(can, forced)
                shutil.rmtree(forced / ".git")
                (forced / "_governance/UpdDoc.md").write_text(
                    node_text("260802-uupd-0002", "정본 규범 문서", "위조판."),
                    encoding="utf-8")
                subprocess.run(["git", "-C", str(forced), "init", "-q"],
                               capture_output=True)
                for _k, _v in (("user.email", "t@t"), ("user.name", "t")):
                    subprocess.run(["git", "-C", str(forced), "config", _k, _v],
                                   capture_output=True)
                subprocess.run(["git", "-C", str(forced), "add", "-A"],
                               capture_output=True)
                subprocess.run(["git", "-C", str(forced), "commit", "-qm", "forced"],
                               capture_output=True)
                _rel(_ver_now, forced)
                efm = uerr(lambda: update.run(source="bundle", bundle=str(forced),
                                              apply=True))
                check("같은 버전·다른 identity는 거부(force-move 방어)",
                      efm is not None and "identity" in efm, efm)

            # 다기기 병렬 적용 — 값이 같은 극대는 동치로 수렴한다 (P2)
            def _rr(n):
                return f"00000000-0000-7000-8000-{n:012d}"
            forked = [
                {"rid": _rr(31), "parents": [], "kind": "apply", "txn": "TA",
                 "path": "F", "hash": "sha256:same"},
                {"rid": _rr(32), "parents": [_rr(31)], "kind": "done", "txn": "TA"},
                # 다른 기기가 같은 릴리스를 적용한 뒤 union 병합된 갈래
                {"rid": _rr(33), "parents": [], "kind": "apply", "txn": "TB",
                 "path": "F", "hash": "sha256:same"},
                {"rid": _rr(34), "parents": [_rr(33)], "kind": "done", "txn": "TB"},
            ]
            check("값이 같은 병렬 극대는 동치로 수렴(baseline 유지)",
                  update.last_applied_hash(forked, "F") == "sha256:same",
                  update.last_applied_hash(forked, "F"))
            conflicted = forked[:3] + [
                {"rid": _rr(35), "parents": [_rr(33)], "kind": "done", "txn": "TB"}]
            conflicted[2] = dict(conflicted[2], hash="sha256:other")
            check("값이 갈리는 병렬 극대는 여전히 미확정",
                  update.last_applied_hash(conflicted, "F") is None,
                  update.last_applied_hash(conflicted, "F"))

            # 저널 디렉터리 엔트리 내구화 — 표식을 지우기 전에 저널의 이름이
            # 살아 있어야 baseline이 유실되지 않는다 (P1)
            _fs_calls = []
            _real_fsd = update._fsync_dir
            try:
                update._fsync_dir = lambda d: (_fs_calls.append(str(d)),
                                               _real_fsd(d))[1]
                update._fsync_journal_home()
            finally:
                update._fsync_dir = _real_fsd
            check("저널 홈과 그 조상이 ROOT까지 내구화된다",
                  str(update.UPDATE_JOURNAL.parent) in _fs_calls
                  and str(ROOT) in _fs_calls, _fs_calls[:4])

            # 삭제됐던 파일의 rollback은 **권한까지** 되돌린다 (P2)
            _permf = ROOT / "_governance/permtest.md"
            _permf.write_text("x\n", encoding="utf-8")
            os.chmod(_permf, 0o600)
            mine.append(_permf)
            update._txn_begin("txnPERM", "v9.9.9", ["_governance/permtest.md"])
            _manp = json.loads(update.TXN_MANIFEST.read_text(encoding="utf-8"))
            check("manifest가 pre-image의 mode를 담는다",
                  _manp["entries"][0].get("mode") == 0o600,
                  _manp["entries"][0].get("mode"))
            _permf.unlink()                       # remove를 적용한 상태를 모사
            update._txn_recover([{"kind": "begin", "txn": "txnPERM"}])
            check("삭제 rollback이 내용과 권한을 모두 복원한다",
                  _permf.exists()
                  and stat.S_IMODE(_permf.stat().st_mode) == 0o600,
                  oct(stat.S_IMODE(_permf.stat().st_mode)) if _permf.exists() else None)

            # roll-forward도 저널 홈 내구화를 재시도한 뒤에 표식을 지운다 (P1)
            _fs2 = []
            _real_fsd2 = update._fsync_dir
            update._txn_begin("txnRF", "v9.9.9", [])
            try:
                update._fsync_dir = lambda d: (_fs2.append(str(d)),
                                               _real_fsd2(d))[1]
                actrf = update._txn_recover([{"kind": "done", "txn": "txnRF"}])
            finally:
                update._fsync_dir = _real_fsd2
            check("roll-forward가 저널 홈을 내구화한 뒤 표식을 지운다",
                  actrf == "roll-forward"
                  and str(update.UPDATE_JOURNAL.parent) in _fs2
                  and not update.TXN_MANIFEST.exists(), (actrf, _fs2[:3]))

            # chmod 결과까지 내구화한다 (P2)
            _permf2 = ROOT / "_governance/permtest2.md"
            _permf2.write_text("y\n", encoding="utf-8")
            os.chmod(_permf2, 0o600)
            mine.append(_permf2)
            update._txn_begin("txnPERM2", "v9.9.9", ["_governance/permtest2.md"])
            _permf2.unlink()
            _ff = []
            _real_ffile = update._fsync_file
            try:
                update._fsync_file = lambda q: (_ff.append(str(q)),
                                                _real_ffile(q))[1]
                update._txn_recover([{"kind": "begin", "txn": "txnPERM2"}])
            finally:
                update._fsync_file = _real_ffile
            check("권한 복원 뒤 파일 메타데이터를 내구화한다",
                  str(_permf2) in _ff
                  and stat.S_IMODE(_permf2.stat().st_mode) == 0o600, _ff[:3])

            # 정상 원자 교체도 mode를 **fsync 앞에서** 확정한다 — 뒤에 chmod하면
            # 그 메타데이터가 내구화되지 않아 권한만 0600으로 남을 수 있다 (P2).
            #
            # 검사하는 것은 *어느 호출을 쓰느냐*가 아니라 **mode 확정이 fsync보다
            # 앞서느냐**다. 호출 이름으로 검사하면 fchmod가 없는 환경(Windows)의
            # chmod 대체 경로는 영영 검증되지 않고, 그 환경에서는 수트가 fchmod를
            # 읽는 자리에서 먼저 죽는다. 그래서 POSIX에서도 fchmod를 지운 채 한 번
            # 더 돌려 **양쪽 분기를 같은 불변식으로** 검사한다.
            #
            # `scripts/recover.py`는 이 함수의 의도적 중복이므로 같은 잣대를 댄다 —
            # 중복이 갈라지는 순간을 여기서 잡는다.
            def _mode_before_fsync(mod, tag, drop_fchmod):
                order = []
                real_fchmod = getattr(os, "fchmod", None)
                real_chmod, real_fsync = os.chmod, os.fsync
                f = ROOT / ("_governance/permtest3-%s.md" % tag)
                f.write_text("z\n", encoding="utf-8")
                real_chmod(f, 0o640)
                mine.append(f)
                try:
                    if drop_fchmod:
                        if real_fchmod is not None:
                            del os.fchmod              # 대체 경로를 강제한다
                    elif real_fchmod is not None:
                        os.fchmod = lambda fd_, m_: (order.append("mode"),
                                                     real_fchmod(fd_, m_))[1]
                    os.chmod = lambda p_, m_: (order.append("mode"),
                                               real_chmod(p_, m_))[1]
                    os.fsync = lambda fd_: (order.append("fsync"),
                                            real_fsync(fd_))[1]
                    mod._write_atomic(f, b"new\n")
                finally:
                    if real_fchmod is not None:
                        os.fchmod = real_fchmod
                    os.chmod, os.fsync = real_chmod, real_fsync
                return (("mode" in order and "fsync" in order
                         and order.index("mode") < order.index("fsync")),
                        stat.S_IMODE(f.stat().st_mode) == 0o640
                        and f.read_bytes() == b"new\n",
                        order[:4])

            import importlib.util as _ilu
            _rspec = _ilu.spec_from_file_location(
                "osk_recover_probe", ENGINE / "scripts" / "recover.py")
            _rmod = _ilu.module_from_spec(_rspec)
            _rspec.loader.exec_module(_rmod)
            for _mod, _who in ((update, "update"), (_rmod, "recover")):
                for _tag, _drop, _via in ((_who + "a", False, "fchmod"),
                                          (_who + "b", True, "chmod 대체")):
                    if _drop is False and not hasattr(os, "fchmod"):
                        continue                       # Windows — 그 분기가 없다
                    _o, _k, _d = _mode_before_fsync(_mod, _tag, _drop)
                    check("%s: 원자 교체는 mode를 fsync 앞에서 확정한다(%s)"
                          % (_who, _via), _o, _d)
                    check("%s: 원자 교체가 기존 권한을 유지한다(%s)"
                          % (_who, _via), _k)

            # 트랜잭션 정리 실패는 fail-closed (P2)
            update._txn_begin("txnCLR", "v9.9.9", [])
            _saved = update.shutil.rmtree
            try:
                update.shutil.rmtree = lambda *a, **k: None   # 정리가 안 되는 상황
                ecl = uerr(update._txn_clear)
                check("트랜잭션 정리 실패는 fail-closed",
                      ecl is not None and "정리 실패" in ecl, ecl)
            finally:
                update.shutil.rmtree = _saved
                update._txn_clear()

            # has_history — apply/remove/done 이력 유무 (adopt 게이팅 근거) (P2)
            check("has_history: 빈/이력",
                  not update.has_history([])
                  and not update.has_history([{"kind": "begin"}])
                  and update.has_history([{"kind": "apply"}]))

            # 디렉터리 bundle은 snapshot을 뜬다 — 검증 후 원본 변경 TOCTOU 차단 (P1)
            snb = Path(td) / "snapbundle"
            (snb / "docs").mkdir(parents=True)
            (snb / "docs/x.md").write_text("orig\n", encoding="utf-8")
            snap = update.fetch_bundle(str(snb), Path(td) / "snapdst")
            (snb / "docs/x.md").write_text("MUTATED\n", encoding="utf-8")  # 이후 변경
            check("디렉터리 bundle은 snapshot이라 원본 변경에 영향 없다",
                  (snap / "docs/x.md").read_text(encoding="utf-8") == "orig\n"
                  and snap != snb)

            # 동시 데몬 잠금 — update가 mutation 잠금을 잡으면 데몬 tick은 건너뛴다
            # (git repo인 can으로 검사 — once는 is_git_repo를 먼저 본다) (P1)
            import sync_daemon as _sd
            _lp = _sd._lock_path(can, "osk-mutation.lock")
            _held = open(_lp, "w")
            try:
                update.lock_exclusive(_held, blocking=False)   # update처럼 선점
                check("데몬은 update가 잡은 mutation 잠금에서 tick을 건너뛴다",
                      _sd.once(can) == "locked")
            finally:
                update.unlock(_held); _held.close()

            # 데몬이 pending 트랜잭션 표식을 보면 tick을 거부한다 (P1)
            _tm = can / ".osk" / "txn" / "manifest.json"
            _tm.parent.mkdir(parents=True, exist_ok=True)
            _tm.write_text('{"txn":"x","entries":[]}', encoding="utf-8")
            try:
                check("데몬은 미완료 트랜잭션 표식에서 tick을 거부",
                      _sd.once(can) == "pending-txn", _sd.once(can))
            finally:
                shutil.rmtree(can / ".osk", ignore_errors=True)

            # 데몬 실행 중에는 갱신하지 않는다 — 구버전 데몬 bootstrap 방어 (P1)
            _sing = open(_sd._lock_path(ROOT, "osk-sync.lock"), "w")
            try:
                update.lock_exclusive(_sing, blocking=False)   # 데몬처럼 선점
                eds = uerr(lambda: update.run(source="bundle", bundle=str(can),
                                              apply=True))
                check("데몬 실행 중 갱신은 거부",
                      eds is not None and "데몬" in eds, eds)
            finally:
                update.unlock(_sing); _sing.close()

            # MAP 사상 — publish.collect과 같은 src/a -> dst/a (P2)
            man_id = {"map": [("_governance/", "_governance/"),
                              (".mcp.json.example", ".mcp.json.example")],
                      "deny": [], "skel": [], "keep": []}
            check("MAP 항등 사상",
                  update._map_dest("_governance/C.md", man_id) == "_governance/C.md"
                  and update._map_dest(".mcp.json.example", man_id)
                  == ".mcp.json.example")
            man_nz = {"map": [("src/", "dst/")],
                      "deny": [], "skel": [], "keep": []}
            check("MAP 비항등 사상(src/a -> dst/a)·미매칭은 None",
                  update._map_dest("src/a/b.md", man_nz) == "dst/a/b.md"
                  and update._map_dest("other/x", man_nz) is None)

            # 버전 계약 — release와 updater 자동 탐색이 같은 vX.Y.Z를 쓴다 (P2)
            check("release: 느슨한 버전(v2.2)은 선언 전에 거부",
                  "vX.Y.Z" in (uerr(lambda: release.run(
                      "v2.2", apply=True, root=can)) or ""))
            check("release: 비형식(vfoo)도 거부",
                  "vX.Y.Z" in (uerr(lambda: release.run(
                      "vfoo", apply=True, root=can)) or ""))
            badrel = Path(td) / "badrel"; badrel.mkdir()
            (badrel / "release.json").write_text(
                json.dumps({"version": "v2.2", "files": {}}), encoding="utf-8")
            check("load_release: 느슨한 version은 증빙 판독에서 거부",
                  "version 형식" in (uerr(
                      lambda: update.load_release(badrel)) or ""))

            # ROOT 내부 `..` 재진입도 거부 — floor 판정 우회 차단 (P1)
            check("_within/_canon_rel: ROOT 내부 재진입(../)은 거부, 정상은 통과",
                  update._within(ROOT, "docs/../= Scope/x") is None
                  and update._canon_rel(ROOT, "= Scope/../_governance/x") is None
                  and update._canon_rel(ROOT, "_governance/x") == "_governance/x")

            # skip은 적용 baseline을 가리지 않는다 — conflict 사건과 상태 분리 (P2)
            j = [{"rid": "00000000-0000-7000-8000-00000000ba01",
                  "parents": [], "kind": "apply", "path": "F",
                  "hash": "sha256:aaa"},
                 {"rid": "00000000-0000-7000-8000-00000000ba02",
                  "parents": ["00000000-0000-7000-8000-00000000ba01"],
                  "kind": "skip", "skipped_path": "F", "why": "conflict"}]
            check("skip 뒤에도 baseline은 마지막 apply",
                  update.last_applied_hash(j, "F") == "sha256:aaa"
                  and "F" in update.managed_paths(j))

            # 명시 ref/pin은 정식 태그여야 한다 — 브랜치로 경계 우회 금지 (P2)
            check("tag_exists: 태그만 인정(브랜치·부재는 False)",
                  update.tag_exists(str(can), "v9.0.1")
                  and not update.tag_exists(str(can), "main")
                  and not update.tag_exists(str(can), "master")
                  and not update.tag_exists(str(can), "v0.0.0"))
            check("git: 비형식 ref(main)는 형식에서 거부(네트워크 전)",
                  "vX.Y.Z" in (uerr(
                      lambda: update.run(source="git", ref="main")) or ""))

            # 현재 판본은 인과 극대로 판정한다(물리 마지막 행이 아니다) —
            # sibling last_applied_hash와 같은 규율, union 병합 대장이므로.
            chain = [
                {"rid": "00000000-0000-7000-8000-000000000001",
                 "parents": [], "kind": "done", "version": "vA", "at": "t1"},
                {"rid": "00000000-0000-7000-8000-000000000002",
                 "parents": ["00000000-0000-7000-8000-000000000001"],
                 "kind": "done", "version": "vB", "at": "t2"},
            ]
            check("선형 사슬은 인과 첨단(tip)을 낸다",
                  update.current_version(chain) == "vB",
                  update.current_version(chain))
            # 두 기기의 동시 갱신(비교 불능 분기) — done[-1]이라면 물리 마지막
            # 하나를 결정적인 양 내지만, 인과 극대는 미확정 None이다(fail-closed)
            fork = chain[:1] + [
                {"rid": "00000000-0000-7000-8000-0000000000ff",
                 "parents": [], "kind": "done", "version": "vX", "at": "t9"}]
            check("동시 갱신(비교 불능 분기)은 미확정 None",
                  update.current_version(fork) is None,
                  update.current_version(fork))

    finally:
        # 뒷정리 — 이후 기준선 PASS 유지
        for f in mine:
            f.unlink(missing_ok=True)
        for d in (ROOT / "= UpdSkel", ROOT / "docs",
                  ROOT / "_governance/_engine/scripts",
                  ROOT / "_governance/_engine", ROOT / "_governance/records"):
            try:
                (d / ".gitkeep").unlink(missing_ok=True)
                d.rmdir()
            except OSError:
                pass


# ── 15c. 위임 명령은 인자를 해석하지 않고 그대로 넘긴다 ──────────────────
def test_cli_delegation():
    """`osk update`·`osk release`의 계약은 "해석하지 않고 넘긴다"이다.
    `argparse.REMAINDER`로 받으면 잔여의 첫 토큰이 `-`로 시작할 때 상위 파서가
    자기 옵션으로 먼저 해석해 `osk update --apply`가 죽는다 — 위임한다고
    광고해 놓고 정작 갱신·릴리스의 **적용 형태를 못 부르는** 상태였다."""
    from osk import cli
    import osk.update as _u, osk.release as _r
    seen = {}
    ru, rr = _u.main, _r.main
    try:
        _u.main = lambda rest=None: seen.__setitem__("update", rest)
        _r.main = lambda rest=None: seen.__setitem__("release", rest)
        for argv, key, want in (
                (["update", "--apply"], "update", ["--apply"]),
                (["update", "--apply", "--to", "v1.2.3"], "update",
                 ["--apply", "--to", "v1.2.3"]),
                (["update"], "update", []),          # 인자 없는 보고도 그대로
                (["release", "--version", "v1.2.3", "--apply"], "release",
                 ["--version", "v1.2.3", "--apply"])):
            seen.clear()
            # 위임이 깨지면 상위 파서가 `SystemExit(2)`로 죽는다. 그대로 두면
            # 수트 전체가 중단돼 **어느 검사가 깨졌는지 보이지 않는다** — 실패는
            # 실패로 보고되어야 한다.
            try:
                cli.main(argv)
            except SystemExit as e:
                seen[key] = f"<SystemExit {e.code}>"
            check(f"위임: osk {' '.join(argv)}", seen.get(key) == want, seen)
    finally:
        _u.main, _r.main = ru, rr

    # 위임 명령이 `osk --help` 목록에서 사라지면 발견 가능성을 잃는다.
    import io, contextlib
    buf = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
        cli.main(["--help"])
    check("위임 명령도 osk --help 목록에 남는다",
          "update" in buf.getvalue() and "release" in buf.getvalue())


# ── 16. 제목은 모든 기기에서 파일명·Link 대상이 될 수 있어야 한다 ────────
def test_portable_title():
    """제목이 곧 파일명이자 Link 대상이므로, 한 기기에서만 표현 가능한 이름은
    다른 기기의 체크아웃이나 Link 해소를 깨뜨린다. 검사 대상이 **원본 문자열**
    이어야 한다는 것도 여기서 고정한다 — `strip()` 뒤를 검사하면 후행 공백과
    양끝 제어문자가 검사를 통과한 뒤 파일명에 그대로 들어간다."""
    valid = ["정상 제목", "대괄호[있음", "괄호(있음)", "밑줄_있음", "점.중간.있음"]
    invalid = {
        "": "빈 제목", " ": "공백뿐", "foo ": "후행 공백", " foo": "선행 공백",
        "foo\t": "후행 탭", "foo\n": "후행 개행", "a\tb": "중간 제어문자",
        ".foo": "선행 점", "foo.": "후행 점",
        "foo/bar": "슬래시", "foo\\bar": "역슬래시", "foo:bar": "콜론",
        'foo"bar': "따옴표", "foo|bar": "파이프", "foo?bar": "물음표",
        "foo*bar": "별표", "foo<bar": "부등호", "foo#1": "샵(Link 절단)",
        "foo]bar": "닫는 대괄호(Link 절단)",
        "CON": "예약 장치명", "CON.md": "확장자 붙은 예약 장치명", "COM1": "예약 포트명",
        "COM¹": "예약 포트명(위첨자)", "LPT³.txt": "확장자 붙은 예약 포트명(위첨자)",
        "NUL.tar.gz": "다중 확장자 예약 장치명",
        "CONIN$": "콘솔 장치명", "CONOUT$.md": "확장자 붙은 콘솔 장치명",
        "COM1 .foo": "장치명 뒤 공백", "LPT1  .x": "장치명 뒤 공백 여러 개",
        "가" * 85: "UTF-8 258바이트(ext4 상한 초과)",
        "a" * 253: "ASCII 256바이트(ext4 상한 초과)",
    }
    valid += ["COMMENT", "CON2", "COM0", "가" * 84, "a" * 252]
    bad_ok = [t for t in valid if write._title_errors(t)]
    check("적격 제목은 통과한다", not bad_ok, bad_ok)
    missed = [f"{k!r}({why})" for k, why in invalid.items()
              if not write._title_errors(k)]
    check("부적격 제목은 전부 거부된다", not missed, missed)

    # 이식성 키 — 대소문자·유니코드 정규화만 다른 이름은 같은 경로다
    import unicodedata as _ud
    check("대소문자만 다른 이름은 같은 키",
          write._portable_name_key("Example") == write._portable_name_key("example"))
    check("NFC·NFD만 다른 이름은 같은 키",
          write._portable_name_key(_ud.normalize("NFC", "가나"))
          == write._portable_name_key(_ud.normalize("NFD", "가나")))
    check("서로 다른 이름은 다른 키",
          write._portable_name_key("Example") != write._portable_name_key("Exampl3"))

    # create_node가 대소문자 변종을 거부한다 (Linux에서만 나던 구멍)
    made = ROOT / "= Scope/W1/Regr-Case.md"
    try:
        write.create_node(title="Regr-Case", summary="이식성 시험", body="본문",
                          drafter="sonnet-5", space="= Scope/W1")
        try:
            write.create_node(title="regr-case", summary="충돌해야 한다", body="본문",
                              drafter="sonnet-5", space="= Scope/W1")
            check("대소문자만 다른 형제 이름은 거부된다", False, "생성되어 버렸다")
        except write.WriteError as e:
            check("대소문자만 다른 형제 이름은 거부된다",
                  "Regr-Case" in str(e.violations), e.violations)
    finally:
        made.unlink(missing_ok=True)
        (ROOT / "= Scope/W1/regr-case.md").unlink(missing_ok=True)


# ── 17. 경로 키는 기기 표기에 의존하지 않는다 (OS 무관 고정) ───────────
def test_posix_rel_is_os_independent():
    """`posix_rel`이 항상 슬래시 표기를 내는지 **실행 OS와 무관하게** 고정한다.

    pin·DENY 회귀는 실제 파일시스템을 쓰므로 Windows에서만 이 결함을 잡는다 —
    POSIX에서는 수정 전 코드도 슬래시를 내서 회귀가 처음부터 없었던 것처럼
    통과한다. 그래서 여기서는 Pure*Path로 두 표기를 직접 만들어 불변식 자체를
    못박는다. 이 시험은 파일시스템을 건드리지 않는다."""
    from pathlib import PureWindowsPath, PurePosixPath
    win = core.posix_rel(PureWindowsPath('C:\\vault\\= Scope\\W2\\a.md'), PureWindowsPath('C:\\vault'))
    check("Windows 표기도 슬래시로 접힌다", win == "= Scope/W2/a.md", win)
    pos = core.posix_rel(PurePosixPath("/vault/= Scope/W2/a.md"),
                         PurePosixPath("/vault"))
    check("POSIX 표기는 그대로다", pos == "= Scope/W2/a.md", pos)
    check("두 표기가 같은 키로 접힌다", win == pos, (win, pos))

    # 이 결함의 소비자 두 곳과 같은 형태로, 규칙 문자열 대조가 성립하는지
    d = core.posix_rel(PureWindowsPath('C:\\v\\_governance\\_engine\\osk\\x.py'), PureWindowsPath('C:\\v\\_governance'))
    check("DENY 조각이 걸린다", "_engine/" in d, d)
    pin = core.posix_rel(PureWindowsPath('C:\\v\\= Scope\\W2'), PureWindowsPath('C:\\v')) + "/"
    check("pin 대상 표기와 일치한다", pin == "= Scope/W2/", pin)


if __name__ == "__main__":
    for fn in [test_posix_rel_is_os_independent, test_portable_title,
               test_cli_delegation, test_rid_monotone, test_same_ms_chain_signed,
               test_fork_failclosed_and_reseal, test_anchor_no_order_fallback,
               test_cycle_normalization, test_structural_damage,
               test_ridless_unsign_not_swallowed, test_root_confinement_and_kst,
               test_restore_binding,
               test_path_reuse, test_demotion_reorder, test_fingerprint_move,
               test_sync, test_conflicts_semantics,
               test_ledger_corruption_resilience, test_ledger_schema_segment,
               test_validate_global_invariance, test_authority_hold,
               test_self_referencing_edge, test_surface_contract,
               test_open_case_blocks_signing, test_ledger_row_shape,
               test_broken_delegation_isolated, test_write_contract,
               test_write_cas_bound_to_signature, test_write_move_and_pin,
               test_write_routing, test_write_session_alias,
               test_write_candidate_basis,
               test_write_serialized, test_surface_smoke,
               test_render_roundtrip, test_conflicts_open_case_path,
               test_routing_not_bricked, test_extra_field_preserved,
               test_alias_cycle_returns_input, test_candidate_needs_distinct,
               test_edge_target_normalization, test_update_no_change,
               test_bound_scope_honest, test_mcp_transport,
               test_surface_lint, test_overview, test_edge_value_type_refused,
               test_refusal_teaches_address, test_id_as_handle,
               test_surface_name_roundtrip, test_validate_uses_destination_path,
               test_dup_stem_write_refused,
               test_sync_pins_main,
               test_publish_manifest, test_publish_guards,
               test_conflict_candidates,
               test_release_and_update,
               test_baseline_pass]:
        try:
            fn()
        except Exception as e:
            FAIL.append(f"{fn.__name__} 예외: {e!r}\n"
                        + "".join(traceback.format_exc().splitlines(True)[-6:]))
    print(f"회귀 수트: 통과 {len(PASS)} / 실패 {len(FAIL)}  (mini-vault: {MINI})")
    for f in FAIL:
        print("FAIL:", f)
    for p in PASS:
        print("  ✓", p)
    sys.exit(1 if FAIL else 0)
