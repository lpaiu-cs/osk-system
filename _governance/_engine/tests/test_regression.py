"""osk 회귀 수트 — 검토 세션(2026-08-02)의 적대 시나리오를 영속 고정.

격리 원칙 (검토 3차 지적 4): 수트 전체가 임시 mini-vault를 OSK_VAULT_ROOT로
가리키는 **자기 프로세스** 안에서 돈다 — 실 vault는 읽지도 쓰지도 않고,
전역(core.SIGNATURES 등)의 재대입·모듈 reload도 하지 않는다. sync 시험은
별도 임시 git 저장소, 보호영역 생애 fixture는 별도 subprocess에서 돈다.

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

from osk import (core, graph, validate, authority, contract, write,  # noqa: E402
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


def sig_status(node_id, path=None):
    """구 signatures.status의 관측을 core+헬퍼로 재현한다 — 서명 제도는
    폐지됐지만 그 관측을 떠받치던 **인과 DAG 기계**(effective_parents·
    causal_maxima·unresolved_nodes·id 해석)는 그대로 존속하며, 이 재현으로
    그 기계를 signatures 모듈 없이 계속 소진한다. SIGNATURES는 이 수트에서
    임의 append가 가능한 임시 대장으로 쓰인다."""
    recs = core.ledger_read(core.SIGNATURES)
    if node_id in core.unresolved_nodes(recs):
        return "unsigned"
    m = core.causal_maxima(recs, node_id)
    if len(m) != 1 or m[0].get("kind") not in ("sign", "restore"):
        return "unsigned"
    r = m[0]
    hit = None
    for c in (path, r.get("path")):
        c = core.resolve_in_root(c) if c else None
        if c is not None and c.exists() and S._id_of(c) == node_id:
            hit = c
            break
    if hit is None:
        hit = S.locate_by_id(node_id)
        if hit is None:
            return "unsigned"
    try:
        return "signed" if core.sha256_file(hit) == r.get("hash") else "unsigned"
    except OSError:
        return "unsigned"


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
          sig_status("260802-zzzz-rg01", node) == "signed")


# ── 3. 인과 분기 → fail-closed → 모든 head 봉합 재서명으로 해소 ──────────
def test_fork_failclosed_and_reseal():
    node = ROOT / "= Scope/W1/regr-chain.md"     # 2번의 상태를 이어받는다
    h = core.sha256_file(node)
    fork_rid = core._make_rid(int(1754000100.0 * 1000), 0xF00)
    raw_append({"rid": fork_rid, "parents": [], "kind": "sign",
                "node": "260802-zzzz-rg01", "path": str(node.relative_to(ROOT)),
                "hash": h, "at": core.now_iso()})     # 다른 기기 유래 뿌리(병합 산물)
    check("분기(비교 불능) → unsigned fail-closed",
          sig_status("260802-zzzz-rg01", node) == "unsigned")
    check("분기 상태는 판정 불가로 표면화(fail-closed)",
          "260802-zzzz-rg01" in core.unresolved_nodes(
              core.ledger_read(core.SIGNATURES)))
    r = core.ledger_append(core.SIGNATURES, {
        "kind": "sign", "node": "260802-zzzz-rg01",
        "path": str(node.relative_to(ROOT)), "hash": h,
        "reason": "재서명 — 분기 해소"})
    check("재서명이 모든 head를 봉합(parents 2개)", len(r["parents"]) == 2,
          r["parents"])
    check("재서명 후 유일 극대 → signed",
          sig_status("260802-zzzz-rg01", node) == "signed")


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
          sig_status("260802-zzzz-rg10", node) == "unsigned")
    check("판정 불가 노드로 표면화",
          "260802-zzzz-rg10" in core.unresolved_nodes(recs))
    r = core.ledger_append(core.SIGNATURES, {
        "kind": "sign", "node": "260802-zzzz-rg10", "path": rel, "hash": h,
        "reason": "사용자 재서명 — 유입 분기 봉합"})
    check("재서명이 유입 분기까지 봉합(해소 가능성 보존)",
          set(r["parents"]) == {U["rid"], B1}, r["parents"])
    check("봉합 후 signed", sig_status("260802-zzzz-rg10", node) == "signed")


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
    check("정상 서명은 signed", sig_status("260802-zzzz-rg12", node) == "signed")
    raw_append({"kind": "unsign", "node": "260802-zzzz-rg12", "path": rel,
                "hash": core.sha256_file(node), "at": core.now_iso()})
    check("rid 없는 해제는 무시되지 않고 미서명으로 떨어진다",
          sig_status("260802-zzzz-rg12", node) == "unsigned")


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


# ── 4. 보호영역 생애 (지정·pending·양측 CAS 승인·반려·해제 — 격리 subprocess) ──
def test_approval_lifecycle():
    with tempfile.TemporaryDirectory() as td:
        errs = validate.fixture_approval_lifecycle(td)
        check("보호영역 생애(양측 CAS·반려 복원·미보호 거부)", not errs, errs)


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


# ── 8-3. 데몬은 콘솔 없는 pythonw로 돈다 — git bare-spawn 재도입 금지 ──────
def test_daemon_no_bare_git_spawn():
    """검은 콘솔 재발 방지(2026-08-09). 데몬은 pythonw(콘솔 없음)로 돌아,
    직접 subprocess로 git.exe를 spawn하면 Windows가 tick마다 새 콘솔 창을
    할당한다. `_lock_path`가 이 실수를 저질렀고, v2.2.0이 그 함수를 매 tick
    호출로 승격하며 15분 주기 증상으로 표면화됐다. 데몬의 git은 전부
    vault_sync._git(=CREATE_NO_WINDOW 하드닝의 단일 소유자)를 거쳐야 한다 —
    이 계약을 AST로 고정해 새 호출부가 조용히 bare-spawn을 되살리지 못하게 한다."""
    import ast
    src = (ENGINE / "sync_daemon.py").read_text(encoding="utf-8")
    bare = [f"line {n.lineno}: subprocess.{n.func.attr}(...)"
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "subprocess"]
    check("sync_daemon은 subprocess로 직접 git을 spawn하지 않는다 "
          "(전부 vault_sync._git 게이트웨이 경유)", not bare, "; ".join(bare))


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
    from osk import approvals
    backup = approvals.APPROVALS.read_bytes() if approvals.APPROVALS.exists() else None
    try:
        approvals.APPROVALS.parent.mkdir(parents=True, exist_ok=True)
        approvals.APPROVALS.write_text('{"rid": "x", "kind": "protect"\n',
                                       encoding="utf-8")   # 부분 행(손상)
        try:
            rep = validate.run()
            check("손상 대장에도 검증기 생존", True)
            check("손상은 FAIL로 보고", rep["verdict"] == "FAIL"
                  and any("승인 기록부" in list(f)[0] for f in rep["fail"]))
        except Exception as e:
            check("손상 대장에도 검증기 생존", False, repr(e))
    finally:
        if backup is not None:
            approvals.APPROVALS.write_bytes(backup)
        else:
            approvals.APPROVALS.unlink(missing_ok=True)


# ── 11. 대장 스키마 — 앵커 이후 parents·rid·필수 필드 강제 ──────────────
def test_ledger_schema_segment():
    from osk import approvals
    backup = approvals.APPROVALS.read_bytes() if approvals.APPROVALS.exists() else None
    try:
        ms = int(1754000200.0 * 1000)
        r1, r2, r3, r5 = (core._make_rid(ms, i) for i in range(4))
        rows = [
            {"rid": r1, "kind": "protect", "region": "= Scope/W1", "at": "t"},   # 유산(parents 없음)
            {"rid": r2, "parents": [r1], "kind": "approve", "region": "= Scope/W1",
             "at": "t"},                                        # 앵커 — 적법
            {"rid": r3, "parents": [], "kind": "approve", "region": "= Scope/W1",
             "at": "t"},                                        # 위반: 중간의 빈 parents
            {"rid": "not-a-rid", "parents": [r3], "kind": "approve",
             "region": "= Scope/W1", "at": "t"},               # 위반: rid 형식
            {"rid": r5, "parents": ["ghost-rid"], "kind": "revert",
             "region": "= Scope/W1", "at": "t"},               # 위반: 미지의 parent
            {"rid": core._make_rid(ms, 9), "parents": [r5], "kind": "protect"},  # 필드 누락
        ]
        approvals.APPROVALS.parent.mkdir(parents=True, exist_ok=True)
        approvals.APPROVALS.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        rep = validate.run()
        seg = next((list(f.values())[0] for f in rep["fail"]
                    if "승인 대장 스키마" in list(f)[0]), None)
        check("승인 대장 스키마 세그먼트가 위반을 적발", seg is not None, rep["fail"])
        if seg:
            joined = " | ".join(seg)
            for want in ("parents 부재", "rid 형식 위반", "미지의 parent", "필수 필드 누락"):
                check(f"스키마 적발: {want}", want in joined, joined)
    finally:
        if backup is not None:
            approvals.APPROVALS.write_bytes(backup)
        else:
            approvals.APPROVALS.unlink(missing_ok=True)


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


# ── 14. 기준선: 정상 mini-vault + 보호영역 지정 → 검증기 PASS ───────────
# ── 승인 저장소 digest 봉쇄 (PR #14 리뷰 [high]) ────────────────────────
def test_store_digest_confined():
    """신뢰 밖 digest가 승인 저장소(STORE) 밖 경로를 읽지 못한다 — _obj_path는
    정확히 `sha256:<64 소문자 hex>`만 받고 계산 경로의 STORE 봉쇄를 재확인한다."""
    from osk import approvals as A
    import os as _os
    good = "sha256:" + "a" * 64
    p = A._obj_path(good)
    store_s = _os.path.normpath(A.STORE)
    check("정상 digest는 STORE 안으로만 해석",
          _os.path.commonpath([store_s, _os.path.normpath(p)]) == store_s)
    for bad in ("sha256:../" + "a" * 61, "sha256:/etc/passwd",
                "sha256:" + "a" * 63, "sha256:" + "a" * 65, "sha256:" + "A" * 64,
                "sha256:" + "g" * 64, "notsha:" + "a" * 64, "../../etc/passwd",
                "sha256:", "sha256:" + "a" * 62 + "/x"):
        check(f"부적격 digest 거부: {bad[:22]}",
              _raises(lambda b=bad: A._obj_path(b))())
        check(f"_store_get은 부적격 digest를 부재로: {bad[:22]}",
              A._store_get(bad) is None)


# ── 위임 성립의 보호 범위 — 상위는 상속, 하위는 우회 불가 ───────────────
def test_delegation_protection_scope():
    """헌법 10조 1항: 상위 구획의 보호는 그 하위 전체에 미친다 — 사용자가 Facet
    대신 `= Person`을 지정했어도 위임은 성립한다. 반대로 Facet **하위**만
    지정한 것은 Facet의 미보호를 우회하지 못한다(헌법 7조 3항, fail-closed)."""
    from osk import approvals as A, authority
    dnode = ROOT / "= Person/Delegation/regr-deleg.md"
    subdir = ROOT / "= Person/Delegation/sub"
    clause = ("## 위임\n- 대상: 시험 행위\n- 범위: 시험\n"
              "- 조건: 없음\n- 종료: 없음\n")
    def eff():
        return {d["title"]: d["effective"] for d in authority.enumerate_delegations()}
    try:
        dnode.write_text(node_text("260802-zzzz-rgd1", "위임 노드", clause),
                         encoding="utf-8")
        check("미보호에서는 미성립", eff().get("regr-deleg") is False, eff())
        # (a) 상위 구획만 보호 — 하향 상속으로 성립한다
        A.protect("= Person", "상위 지정")
        check("상위 보호는 하위에 미친다(헌법 10조 1항)",
              eff().get("regr-deleg") is True, eff())
        A.unprotect("= Person", "정리")
        # (b) Facet 하위만 보호 — Facet 자신은 미보호라 우회되지 않는다
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "filler.md").write_text("x", encoding="utf-8")
        A.protect("= Person/Delegation/sub", "하위만")
        check("하위 구획만 보호되면 위임 미성립(우회 차단)",
              eff().get("regr-deleg") is False, eff())
        A.unprotect("= Person/Delegation/sub", "정리")
        shutil.rmtree(subdir, ignore_errors=True)
        # (c) Facet 자신 보호 → 성립
        A.protect("= Person/Delegation", "Facet 보호")
        check("위임 Facet 자체 보호 시 위임 성립",
              eff().get("regr-deleg") is True, eff())
        # (d) 열거는 승인본에서 — 작업본에서 지워도 승인본의 노드가 보고된다
        dnode.unlink()
        rows = {d["title"]: d for d in authority.enumerate_delegations()}
        check("승인본에 있던 노드가 열거에서 사라지지 않는다",
              "regr-deleg" in rows, list(rows))
        check("다만 작업본 부재라 미성립",
              rows.get("regr-deleg", {}).get("effective") is False, rows)
        dnode.write_text(node_text("260802-zzzz-rgd1", "위임 노드", clause),
                         encoding="utf-8")
        A.unprotect("= Person/Delegation", "정리")
    finally:
        for r in ("= Person", "= Person/Delegation", "= Person/Delegation/sub"):
            try: A.unprotect(r, "정리")
            except Exception: pass
        shutil.rmtree(subdir, ignore_errors=True)
        dnode.unlink(missing_ok=True)


# ── approve 양측 CAS는 관례가 아니라 계약으로 강제 (PR #14 리뷰 [high]) ──
def test_approve_requires_expect_work():
    """approve는 `expect_work`(검토한 작업본) 없이는 성립하지 않는다 — 기본값을
    두면 내부 호출이 작업본 측 CAS를 건너뛴다. base가 맞아도 생략하면 거부하고
    대장에 아무 기록도 남기지 않는다."""
    from osk import approvals as A
    def raises_any(fn):
        try: fn(); return False
        except (ValueError, TypeError): return True
    (ROOT / "= Scope/W2").mkdir(exist_ok=True)
    f = ROOT / "= Scope/W2/regr-aw.md"
    f.write_text("v1", encoding="utf-8")
    try:
        A.protect("= Scope/W2", "지정")
        f.write_text("v2", encoding="utf-8")            # pending
        base = A.approved_hash("= Scope/W2")
        before = len(A.records())
        check("expect_work 생략 approve 거부(필수 인자)",
              raises_any(lambda: A.approve("= Scope/W2", base)))
        check("expect_work=None approve 거부",
              raises_any(lambda: A.approve("= Scope/W2", base, None)))
        check("거부 시 승인 대장에 기록 없음", len(A.records()) == before)
        work = A.working_tree_hash("= Scope/W2")
        A.approve("= Scope/W2", base, expect_work=work, reason="검토")
        check("양측 CAS 충족 시 승인", A.state("= Scope/W2") == "clean")
    finally:
        try: A.unprotect("= Scope/W2", "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


# ── 승인본 manifest↔blob 결속·복원 가능성 (PR #14 리뷰 [high]) ──────────
def test_approval_baseline_blobs_present():
    """_store_tree가 파일당 한 번 읽어 그 bytes를 박제하므로 manifest가 가리키는
    모든 blob이 실재한다(복원 가능). integrity는 manifest만이 아니라 그것이
    가리키는 blob의 실재까지 확인해 복원 불가능한 승인본을 적발한다."""
    from osk import approvals as A
    (ROOT / "= Scope/W2").mkdir(exist_ok=True)
    f = ROOT / "= Scope/W2/regr-bb.md"
    f.write_text("내용A", encoding="utf-8")
    try:
        A.protect("= Scope/W2", "지정")
        table = A._tree_table(A.approved_hash("= Scope/W2"))
        check("승인본 manifest의 모든 blob 실재(단일 판독 박제)",
              all(A._store_get(h) is not None for h in table.values()), table)
        check("integrity 통과(정상 승인본)", A.integrity() == [], A.integrity())
        # 참조 blob 하나를 삭제 → 복원 불가 승인본을 integrity가 적발
        some_h = next(iter(table.values()))
        A._obj_path(some_h).unlink()
        check("integrity가 blob 부재(복원 불가)를 적발",
              any("복원 불가" in e for e in A.integrity()), A.integrity())
        f.write_text("변경", encoding="utf-8")            # pending
        check("blob 부재 승인본으로의 revert는 사전검증에서 거부",
              _raises(lambda: A.revert("= Scope/W2", A.approved_hash("= Scope/W2"),
                                       A.working_tree_hash("= Scope/W2")))())
        A._store_put("내용A".encode("utf-8"))             # blob 복원
        check("blob 복원 후 integrity 통과", A.integrity() == [], A.integrity())
    finally:
        f.write_text("내용A", encoding="utf-8")            # 승인본으로 복귀 → clean
        try: A.unprotect("= Scope/W2", "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


# ── 내용 주소 저장소는 digest↔bytes 일치를 검증 (PR #14 리뷰 [high]) ────
def test_store_content_verified():
    """저장소는 읽기에서 `sha256(bytes)==digest`를 검증한다 — 같은 경로가 다른
    bytes로 변조되면(동기화 충돌·손상·변조) 부재/손상으로 취급되어 integrity
    FAIL·revert가 쓰기 전에 거부하고, _store_put은 손상 객체를 정상으로 치유한다.
    이 계약을 저장소 접근자 한 곳에서 강제하므로 integrity·revert는 손대지
    않아도 함께 fail-closed 된다."""
    from osk import approvals as A
    (ROOT / "= Scope/W2").mkdir(exist_ok=True)
    f = ROOT / "= Scope/W2/regr-cv.md"
    f.write_text("정본내용", encoding="utf-8")
    try:
        A.protect("= Scope/W2", "지정")
        table = A._tree_table(A.approved_hash("= Scope/W2"))
        h = table["= Scope/W2/regr-cv.md"]
        obj = A._obj_path(h)
        obj.write_bytes(b"corrupted-bytes-not-matching-the-digest")   # 같은 경로 변조
        check("변조된 blob은 _store_get에서 부재로(내용 검증)",
              A._store_get(h) is None)
        check("integrity가 손상 blob을 적발",
              any("복원 불가" in e for e in A.integrity()), A.integrity())
        f.write_text("변경", encoding="utf-8")            # pending
        check("손상 blob 승인본으로의 revert는 사전검증에서 거부",
              _raises(lambda: A.revert("= Scope/W2", A.approved_hash("= Scope/W2"),
                                       A.working_tree_hash("= Scope/W2")))())
        A._store_put("정본내용".encode("utf-8"))          # 손상 객체 치유
        check("_store_put이 손상 객체를 정상으로 치유", A._store_get(h) is not None)
        check("치유 후 integrity 통과", A.integrity() == [], A.integrity())
    finally:
        f.write_text("정본내용", encoding="utf-8")          # 승인본으로 복귀 → clean
        try: A.unprotect("= Scope/W2", "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


# ── 대장 잠금 안 전제 재확인 — 스냅샷 중 유입 기록을 덮지 않는다 ────────
def test_approve_precondition_under_lock():
    """승인본 측 CAS는 대장 **잠금 안에서** 다시 본다 — 작업본 스냅샷이 걸리는
    동안 다른 기기의 승인이 동기화로 들어오면, 그것을 못 본 행이 인과 자식으로
    붙어 사용자가 검토한 적 없는 승인본을 조용히 대체하는 일이 없다."""
    from osk import approvals as A
    reg = "= Scope/W2"
    f = ROOT / "= Scope/W2/regr-race.md"
    (ROOT / "= Scope/W2").mkdir(exist_ok=True)
    f.write_text("v1", encoding="utf-8")
    real = A._store_tree
    try:
        A.protect(reg, "지정")
        base = A.approved_hash(reg)
        f.write_text("v2", encoding="utf-8")                 # pending
        work = A.working_tree_hash(reg)

        def racing(d):                    # 스냅샷 도중 다른 기기 기록이 착지
            A._store_tree = real
            core.ledger_append(A.APPROVALS, {
                "kind": "approve", "region": reg, "base": base,
                "accepted": work, "reason": "다른 기기(시험)"})
            return real(d)
        A._store_tree = racing
        before = len(A.records())
        check("스냅샷 중 승인본이 바뀌면 승인 거부",
              _raises(lambda: A.approve(reg, base, expect_work=work))())
        check("유입 기록 1행만 늘고 내 승인은 미기록",
              len(A.records()) == before + 1, len(A.records()) - before)
        check("현행 승인본은 유입 기록의 것", A.approved_hash(reg) == work)
    finally:
        A._store_tree = real
        try: A.unprotect(reg, "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


# ── 지정의 잠금 안 전제는 stale도 막는다 (PR #14 리뷰) ─────────────────
def test_protect_precondition_rejects_stale():
    """지정 스냅샷 도중 같은 영역의 비교 불능 기록이 유입돼 stale이 되면 지정은
    성립하지 않는다 — `is_protected`는 stale에서도 False라, 그것만 보면 분기가
    표면화되지 않고 새 초기 승인본으로 조용히 봉합된다(본문의 stale 거부와 어긋남)."""
    from osk import approvals as A
    reg = "= Domain/pstale"
    regdir = ROOT / "= Domain" / "pstale"
    real = A._store_tree
    kept = A.APPROVALS.read_text(encoding="utf-8") if A.APPROVALS.exists() else ""
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("v1", encoding="utf-8")
        check("지정 전에는 미보호", A.state(reg) == "unprotected")

        def racing(d):                    # 스냅샷 도중 다기기 분기가 착지
            A._store_tree = real
            recs = A.records()
            head = recs[-1]["rid"] if recs else None
            rid = head
            with open(A.APPROVALS, "a", encoding="utf-8") as fh:
                for i in (1, 2):
                    rid = core._next_rid(rid)
                    fh.write(json.dumps({
                        "rid": rid, "parents": [head] if head else [],
                        "at": core.now_iso(), "kind": "protect", "region": reg,
                        "base": None, "accepted": "sha256:" + f"{i}" * 64,
                        "reason": f"기기{i}(시험)"}, ensure_ascii=False) + "\n")
            return real(d)
        A._store_tree = racing
        check("스냅샷 중 stale이 되면 지정 거부", _raises(lambda: A.protect(reg))())
        A._store_tree = real
        check("분기는 그대로 남는다(봉합되지 않음)", A.state(reg) == "stale")
        check("내 protect 행은 기록되지 않았다",
              sum(1 for r in A.records()
                  if r.get("region") == reg and "시험" not in (r.get("reason") or "")) == 0)
    finally:
        A._store_tree = real
        try: A.APPROVALS.write_text(kept, encoding="utf-8")   # 분기 원상 복구
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 영역째 삭제된 사고도 반려로 복구된다 (PR #14 리뷰) ─────────────────
def test_revert_recreates_deleted_region():
    """보호영역 디렉터리가 통째로 사라져도(rm -r·동기화 삭제) 승인본이 유효하면
    반려가 디렉터리를 다시 만들어 복원한다 — 보호가 되돌려야 할 가장 기본적인
    사고가 영역 삭제인데 그것만 수동 복구를 요구하면 장치의 뜻이 무너진다."""
    from osk import approvals as A
    reg = "= Domain/gone"
    regdir = ROOT / "= Domain" / "gone"
    try:
        (regdir / "sub").mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("본문A", encoding="utf-8")
        (regdir / "sub" / "b.md").write_text("본문B", encoding="utf-8")
        A.protect(reg, "지정")
        shutil.rmtree(regdir)                         # 영역째 삭제 사고
        check("영역이 사라지면 pending", A.state(reg) == "pending")
        check("작업본 tree는 빈 tree(판정 불능이 아님)",
              A.working_tree_hash(reg) is not None)
        A.revert(reg, A.approved_hash(reg), A.working_tree_hash(reg), "복구")
        check("디렉터리가 되살아났다", regdir.is_dir())
        check("파일 내용이 승인본 그대로", (regdir / "a.md").read_text() == "본문A")
        check("하위 디렉터리 파일도 복원", (regdir / "sub" / "b.md").read_text() == "본문B")
        check("복구 후 clean", A.state(reg) == "clean")
    finally:
        try: A.unprotect(reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 이동은 이동으로 기록되고, 반려가 이동 전체를 되돌린다 (시행령 §6 4항) ──
def test_move_recorded_and_reverted_in():
    """밖→보호영역 이동을 반려하면 노드는 삭제되지 않고 **원위치로 돌아간다**.
    기록이 없으면 반려가 이동을 '추가'로만 보아 노드를 지운다 — 출발지에는
    복원할 정보가 없으므로 그대로 소실이다(사용자 판정: 이동을 이동으로)."""
    from osk import approvals as A, write
    src = ROOT / "= Scope/W1/regr-mvin.md"
    dst = ROOT / "= Scope/W3/regr-mvin.md"
    (ROOT / "= Scope/W3").mkdir(parents=True, exist_ok=True)
    try:
        src.write_text(node_text("260802-zzzz-mvi1", "이동 노드", "본문"),
                       encoding="utf-8")
        A.protect("= Scope/W3", "도착지 보호")
        base = A.approved_hash("= Scope/W3")
        r = write.move_node("regr-mvin", "= Scope/W3")
        check("이동 성립", r["ok"], r)
        row = A._latest_move(core.ledger_read(A.MOVES), "to",
                             "= Scope/W3/regr-mvin.md")
        check("이동이 이동으로 기록됐다",
              row is not None and row["from"] == "= Scope/W1/regr-mvin.md"
              and row["node"] == "260802-zzzz-mvi1", row)
        check("도착 영역은 pending", A.state("= Scope/W3") == "pending")
        cs = A.changeset("= Scope/W3")
        check("변경집합이 이동을 이동으로 보인다",
              cs["moves"] == [{"node": "260802-zzzz-mvi1",
                               "from": "= Scope/W1/regr-mvin.md",
                               "to": "= Scope/W3/regr-mvin.md"}], cs)
        A.revert("= Scope/W3", base, A.working_tree_hash("= Scope/W3"), "반려")
        check("노드가 원위치로 돌아왔다(삭제되지 않음)", src.is_file())
        check("도착지에는 없다", not dst.exists())
        check("도착 영역은 clean", A.state("= Scope/W3") == "clean")
    finally:
        try: A.unprotect("= Scope/W3", "정리")
        except Exception: pass
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def test_move_reverted_out_no_duplicate():
    """보호영역→밖 이동을 반려하면 밖의 사본을 **되가져와** 승인본을 복원한다 —
    승인본 재생성만 하면 같은 id가 두 곳에 남는다(복제 금지)."""
    from osk import approvals as A, write
    home = ROOT / "= Scope/W3/regr-mvout.md"
    away = ROOT / "= Scope/W1/regr-mvout.md"
    (ROOT / "= Scope/W3").mkdir(parents=True, exist_ok=True)
    try:
        home.write_text(node_text("260802-zzzz-mvo1", "이동 노드", "본문"),
                        encoding="utf-8")
        A.protect("= Scope/W3", "출발지 보호")
        base = A.approved_hash("= Scope/W3")
        check("이동 성립", write.move_node("regr-mvout", "= Scope/W1")["ok"])
        check("출발 영역은 pending", A.state("= Scope/W3") == "pending")
        cs = A.changeset("= Scope/W3")
        check("나간 이동도 이동으로 보인다",
              cs["moves"] and cs["moves"][0]["to"] == "= Scope/W1/regr-mvout.md",
              cs)
        A.revert("= Scope/W3", base, A.working_tree_hash("= Scope/W3"), "반려")
        check("노드가 제자리로 돌아왔다", home.is_file())
        check("밖의 사본이 남지 않았다(같은 id 하나뿐)", not away.exists())
        check("출발 영역은 clean", A.state("= Scope/W3") == "clean")
    finally:
        try: A.unprotect("= Scope/W3", "정리")
        except Exception: pass
        home.unlink(missing_ok=True)
        away.unlink(missing_ok=True)


def test_move_return_blocked_origin_occupied():
    """이동으로 온 노드는 지우지 않는다 — 되돌리거나 거부한다. 원위치가 다른
    파일로 차 있으면 반려를 거부하고 아무것도 건드리지 않는다."""
    from osk import approvals as A, write
    src = ROOT / "= Scope/W1/regr-mvocc.md"
    dst = ROOT / "= Scope/W3/regr-mvocc.md"
    (ROOT / "= Scope/W3").mkdir(parents=True, exist_ok=True)
    try:
        src.write_text(node_text("260802-zzzz-mvc1", "이동 노드", "본문"),
                       encoding="utf-8")
        A.protect("= Scope/W3", "도착지 보호")
        base = A.approved_hash("= Scope/W3")
        check("이동 성립", write.move_node("regr-mvocc", "= Scope/W3")["ok"])
        src.write_text("원위치에 새로 생긴 다른 파일", encoding="utf-8")
        check("원위치가 차 있으면 반려 거부",
              _raises(lambda: A.revert("= Scope/W3", base,
                                       A.working_tree_hash("= Scope/W3")))())
        check("이동 노드는 그대로(삭제되지 않음)", dst.is_file())
        check("원위치의 새 파일도 그대로",
              src.read_text() == "원위치에 새로 생긴 다른 파일")
        check("영역은 여전히 pending", A.state("= Scope/W3") == "pending")
        src.unlink()                       # 자리를 치우면 반려가 진행된다
        A.revert("= Scope/W3", base, A.working_tree_hash("= Scope/W3"), "반려")
        check("치운 뒤에는 원위치로 복귀", src.is_file())
    finally:
        try: A.unprotect("= Scope/W3", "정리")
        except Exception: pass
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def test_move_chain_reverted_no_duplicate():
    """보호영역을 나온 노드가 미처리인 채 한 번 더 이동하면(양끝 미보호) 그
    hop도 사슬로 기록되고, 반려가 **최종 위치**에서 되가져온다 — 옛 도착지만
    보면 '밖 사본 없음'으로 승인본을 재생성해 같은 id가 둘 남는다(복제 금지)."""
    from osk import approvals as A, write
    home = ROOT / "= Scope/W3/regr-mvchain.md"
    hop1 = ROOT / "= Scope/W1/regr-mvchain.md"
    hop2 = ROOT / "= Scope/W4/regr-mvchain.md"
    (ROOT / "= Scope/W3").mkdir(parents=True, exist_ok=True)
    (ROOT / "= Scope/W4").mkdir(parents=True, exist_ok=True)
    try:
        home.write_text(node_text("260802-zzzz-chn1", "사슬 이동", "본문"),
                        encoding="utf-8")
        A.protect("= Scope/W3", "출발지 보호")
        base = A.approved_hash("= Scope/W3")
        check("hop1 성립", write.move_node("regr-mvchain", "= Scope/W1")["ok"])
        check("hop2 성립(양끝 미보호)",
              write.move_node("regr-mvchain", "= Scope/W4")["ok"])
        rows = [r for r in core.ledger_read(A.MOVES)
                if r.get("node") == "260802-zzzz-chn1"]
        check("두 hop 모두 사슬로 기록됐다", len(rows) == 2, rows)
        A.revert("= Scope/W3", base, A.working_tree_hash("= Scope/W3"), "반려")
        check("노드가 제자리로 돌아왔다", home.is_file())
        check("hop1 자리에 사본 없음", not hop1.exists())
        check("hop2 자리에 사본 없음(중복 id 없음)", not hop2.exists())
        check("출발 영역은 clean", A.state("= Scope/W3") == "clean")
    finally:
        try: A.unprotect("= Scope/W3", "정리")
        except Exception: pass
        for f in (home, hop1, hop2):
            f.unlink(missing_ok=True)
        shutil.rmtree(ROOT / "= Scope/W4", ignore_errors=True)


def test_move_lifecycle_cutoff():
    """승인으로 처분된 과거 이동은 새 반려의 해석 대상이 아니다 — 해석의 경계는
    현재 승인본이 성립한 기록이다. 경계가 없으면 처분된 첫 이탈의 출발지가
    '영역 안 생성분'으로 오독돼 재진입 노드가 **삭제**된다."""
    from osk import approvals as A, write
    p1 = ROOT / "= Person/P1"; p2 = ROOT / "= Person/P2"
    p1.mkdir(parents=True, exist_ok=True); p2.mkdir(parents=True, exist_ok=True)
    node_p1, node_p2 = p1 / "regr-lc.md", p2 / "regr-lc.md"
    w1, w4 = ROOT / "= Scope/W1/regr-lc.md", ROOT / "= Scope/W4/regr-lc.md"
    (ROOT / "= Scope/W4").mkdir(parents=True, exist_ok=True)
    try:
        node_p1.write_text(node_text("260802-zzzz-lcx1", "생애 경계", "본문"),
                           encoding="utf-8")
        A.protect("= Person", "지정")
        base = A.approved_hash("= Person")
        check("이탈 이동 성립", write.move_node("regr-lc", "= Scope/W1")["ok"])
        A.approve("= Person", base, A.working_tree_hash("= Person"), "이탈 수용")
        base2 = A.approved_hash("= Person")
        check("처분 뒤 방랑 hop 성립(사슬 기록)",
              write.move_node("regr-lc", "= Scope/W4")["ok"])
        check("재진입 성립", write.move_node("regr-lc", "= Person/P2")["ok"])
        A.revert("= Person", base2, A.working_tree_hash("= Person"), "재진입 반려")
        check("노드는 직전 위치로 돌아간다(삭제되지 않음)", w4.is_file())
        check("재진입 자리는 비었다", not node_p2.exists())
        check("처분된 과거 자리로 되돌리지 않는다",
              not node_p1.exists() and not w1.exists())
        check("영역은 clean", A.state("= Person") == "clean")
    finally:
        try: A.unprotect("= Person", "정리")
        except Exception: pass
        for f in (node_p1, node_p2, w1, w4):
            f.unlink(missing_ok=True)
        shutil.rmtree(p1, ignore_errors=True); shutil.rmtree(p2, ignore_errors=True)
        shutil.rmtree(ROOT / "= Scope/W4", ignore_errors=True)


def test_move_reentry_single_plan():
    """같은 생애에서 나갔다가 **다른 자리로 재진입**한 노드의 반려 — 해석 단위가
    rel이면 한 파일에 계획이 둘 잡혀 복원이 중도에 깨진다(실측: os.replace
    FileNotFoundError로 부분 변경 방치). 노드 단위 해석은 계획을 하나만 세워
    승인본 원적으로 되돌린다."""
    from osk import approvals as A, write
    p1 = ROOT / "= Person/P1"; p2 = ROOT / "= Person/P2"
    p1.mkdir(parents=True, exist_ok=True); p2.mkdir(parents=True, exist_ok=True)
    node_p1, node_p2 = p1 / "regr-re.md", p2 / "regr-re.md"
    w1 = ROOT / "= Scope/W1/regr-re.md"
    try:
        node_p1.write_text(node_text("260802-zzzz-lcx2", "재진입", "본문"),
                           encoding="utf-8")
        A.protect("= Person", "지정")
        base = A.approved_hash("= Person")
        check("이탈 성립", write.move_node("regr-re", "= Scope/W1")["ok"])
        check("다른 facet 재진입 성립",
              write.move_node("regr-re", "= Person/P2")["ok"])
        A.revert("= Person", base, A.working_tree_hash("= Person"), "반려")
        check("노드가 승인본 원적으로 돌아왔다",
              node_p1.is_file() and "본문" in node_p1.read_text())
        check("재진입 자리·경유지에 사본 없음",
              not node_p2.exists() and not w1.exists())
        check("영역은 clean", A.state("= Person") == "clean")
    finally:
        try: A.unprotect("= Person", "정리")
        except Exception: pass
        for f in (node_p1, node_p2, w1):
            f.unlink(missing_ok=True)
        shutil.rmtree(p1, ignore_errors=True); shutil.rmtree(p2, ignore_errors=True)


def test_move_cutoff_causal_not_clock():
    """생애 경계는 인과다 — rid 크기 비교가 아니다. 시계가 뒤처진 기기의 승인
    이후 이동은 rid가 승인 rid보다 **작지만**, 승인 기록의 `moves_seen`이 보지
    못한 행이므로 해석에 들어간다. 크기 비교였다면 잘려서 반려가 밖의 실물을
    못 본 채 승인본을 재생성해 같은 id가 둘 남는다."""
    from osk import approvals as A
    reg = "= Domain/skew"
    regdir = ROOT / "= Domain" / "skew"
    away = ROOT / "= Scope/W1/regr-skew.md"
    kept = A.MOVES.read_text(encoding="utf-8") if A.MOVES.exists() else None
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "regr-skew.md").write_text(
            node_text("260802-zzzz-skw1", "시계 편차", "본문"), encoding="utf-8")
        A.protect(reg, "지정")
        prot = A.records()[-1]
        check("생애 기록이 이동 경계를 박제한다", "moves_seen" in prot, prot)
        base = A.approved_hash(reg)
        # 뒤처진 시계의 기기에서 승인 **이후** 실행된 이동 — rid는 승인보다 작다
        ms = core._rid_parts(prot["rid"])[0] - 60_000
        rid = core._make_rid(ms, 0)
        check("편차 전제: 이동 rid < 승인 rid",
              core._rid_key(rid) < core._rid_key(prot["rid"]))
        with open(A.MOVES, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "rid": rid, "parents": core.heads(core.ledger_read(A.MOVES)),
                "at": core.now_iso(), "kind": "move", "node": "260802-zzzz-skw1",
                "from": "= Domain/skew/regr-skew.md",
                "to": "= Scope/W1/regr-skew.md"}, ensure_ascii=False) + "\n")
        os.replace(regdir / "regr-skew.md", away)
        check("이동 뒤 pending", A.state(reg) == "pending")
        cs = A.changeset(reg)
        check("표시도 그 이동을 이동으로 본다",
              cs["moves"] and cs["moves"][0]["to"] == "= Scope/W1/regr-skew.md",
              cs)
        A.revert(reg, base, A.working_tree_hash(reg), "반려")
        check("밖의 실물이 회수됐다(같은 id 하나뿐)", not away.exists())
        check("승인본 원적으로 복원", (regdir / "regr-skew.md").is_file())
        check("영역은 clean", A.state(reg) == "clean")
    finally:
        try: A.unprotect(reg, "정리")
        except Exception: pass
        away.unlink(missing_ok=True)
        shutil.rmtree(regdir, ignore_errors=True)
        if kept is None:
            A.MOVES.unlink(missing_ok=True)
        else:
            A.MOVES.write_text(kept, encoding="utf-8")


def test_move_phantom_tail_row_harmless():
    """실패한 마지막 이동의 잔행(기록만 남고 rename 실패)은 무해해야 한다 —
    해석이 마지막 행의 to(의도)가 아니라 **실물이 있는 가장 최근 hop**(사실)을
    현재 위치로 삼는다. 의도를 믿으면 앞선 성공 이동의 실물을 못 보고 승인본을
    재생성해 같은 id가 둘 남는다."""
    from osk import approvals as A, write
    home = ROOT / "= Scope/W3/regr-phantom.md"
    w1 = ROOT / "= Scope/W1/regr-phantom.md"
    w4 = ROOT / "= Scope/W4/regr-phantom.md"
    (ROOT / "= Scope/W3").mkdir(parents=True, exist_ok=True)
    (ROOT / "= Scope/W4").mkdir(parents=True, exist_ok=True)
    try:
        home.write_text(node_text("260802-zzzz-phm2", "잔행", "본문"),
                        encoding="utf-8")
        A.protect("= Scope/W3", "지정")
        base = A.approved_hash("= Scope/W3")
        check("이탈 성립", write.move_node("regr-phantom", "= Scope/W1")["ok"])
        # 두 번째 이동 — 기록만 남고 rename이 실패한 상황(권한·IO 오류)
        A.record_move("260802-zzzz-phm2", w1, w4)
        check("실물은 여전히 첫 도착지에", w1.is_file() and not w4.exists())
        cs = A.changeset("= Scope/W3")
        check("표시도 실물 위치를 낸다",
              cs["moves"] and cs["moves"][0]["to"] == "= Scope/W1/regr-phantom.md",
              cs)
        A.revert("= Scope/W3", base, A.working_tree_hash("= Scope/W3"), "반려")
        check("실물이 원적으로 회수됐다", home.is_file())
        check("경유지·거짓 도착지에 사본 없음(같은 id 하나뿐)",
              not w1.exists() and not w4.exists())
        check("영역은 clean", A.state("= Scope/W3") == "clean")
    finally:
        try: A.unprotect("= Scope/W3", "정리")
        except Exception: pass
        for f in (home, w1, w4):
            f.unlink(missing_ok=True)
        shutil.rmtree(ROOT / "= Scope/W4", ignore_errors=True)


def test_move_unrecorded_outside_protection():
    """양끝 다 보호 밖인 이동은 변경집합과 무관하므로 기록하지 않는다
    (시행령 §6 4항의 범위 그대로 — 기록부를 소음으로 채우지 않는다)."""
    from osk import approvals as A, write
    src = ROOT / "= Scope/W1/regr-mvfree.md"
    dst = ROOT / "= Scope/W3/regr-mvfree.md"
    (ROOT / "= Scope/W3").mkdir(parents=True, exist_ok=True)
    try:
        src.write_text(node_text("260802-zzzz-mvf1", "자유 이동", "본문"),
                       encoding="utf-8")
        before = len(core.ledger_read(A.MOVES))
        check("이동 성립", write.move_node("regr-mvfree", "= Scope/W3")["ok"])
        check("기록부는 그대로", len(core.ledger_read(A.MOVES)) == before)
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


# ── 사용자는 '차이'를 검토한다 — 해시 두 개는 검토가 아니다 (헌법 10조 2항) ──
def test_changeset_lists_difference():
    """헌법 10조 2항은 사용자가 **차이를 검토하여** 승인·반려하라고 한다.
    엔진은 그 차이를 파일 단위(추가·삭제·수정)로 낼 수 있어야 한다."""
    from osk import approvals as A
    reg = "= Domain/csview"
    regdir = ROOT / "= Domain" / "csview"
    try:
        (regdir / "sub").mkdir(parents=True, exist_ok=True)
        (regdir / "keep.md").write_text("그대로", encoding="utf-8")
        (regdir / "gone.md").write_text("사라질 것", encoding="utf-8")
        (regdir / "sub" / "edit.md").write_text("v1", encoding="utf-8")
        A.protect(reg, "지정")
        check("clean에서는 차이가 없다",
              A.changeset(reg) == {"added": [], "removed": [],
                                   "modified": [], "moves": []},
              A.changeset(reg))
        (regdir / "gone.md").unlink()
        (regdir / "sub" / "edit.md").write_text("v2", encoding="utf-8")
        (regdir / "new.md").write_text("새 파일", encoding="utf-8")
        cs = A.changeset(reg)
        check("추가를 집는다", cs["added"] == ["= Domain/csview/new.md"], cs)
        check("삭제를 집는다", cs["removed"] == ["= Domain/csview/gone.md"], cs)
        check("수정을 집는다(하위 디렉터리 포함)",
              cs["modified"] == ["= Domain/csview/sub/edit.md"], cs)
        check("차이가 있으면 pending", A.state(reg) == "pending")
        A.revert(reg, A.approved_hash(reg), A.working_tree_hash(reg), "정리")
        check("반려 뒤 차이 없음", A.changeset(reg)["modified"] == [])
    finally:
        try: A.unprotect(reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── stale은 막다른 상태가 아니다 — 봉합 승인 (Mechanism §3 5항) ─────────
def test_stale_sealed_by_approve():
    """극대가 여럿이면 '모든 head를 잇는 사용자의 새 기록이 봉합한다'(§3 5항).
    네 조작이 전부 stale을 거부하면 그 길이 막혀 다기기 병합 한 번이 영역을
    영구히 고착시킨다 — 봉합 승인(base=None)이 그 길이다."""
    from osk import approvals as A
    reg = "= Domain/sealed"
    regdir = ROOT / "= Domain" / "sealed"
    kept = A.APPROVALS.read_text(encoding="utf-8") if A.APPROVALS.exists() else ""
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("v1", encoding="utf-8")
        A.protect(reg, "지정")
        # 두 기기가 각각 승인 → git 병합이 두 줄을 남긴다(인과 극대 2)
        head = A.records()[-1]["rid"]
        rid = head
        with open(A.APPROVALS, "a", encoding="utf-8") as fh:
            for i in (1, 2):
                rid = core._next_rid(rid)
                fh.write(json.dumps({
                    "rid": rid, "parents": [head], "at": core.now_iso(),
                    "kind": "approve", "region": reg, "base": A.approved_hash(reg),
                    "accepted": A.working_tree_hash(reg), "reason": f"기기{i}"},
                    ensure_ascii=False) + "\n")
        check("병합 뒤 stale", A.state(reg) == "stale")
        forks = [f["rid"] for f in A.divergence(reg)]
        check("갈래가 둘로 보인다", len(forks) == 2)
        check("일반 승인은 거부(현행 승인본이 하나가 아니다)",
              _raises(lambda: A.approve(reg, "sha256:" + "a" * 64,
                                        A.working_tree_hash(reg)))())
        check("갈래 집합 없는 봉합도 거부",
              _raises(lambda: A.approve(reg, None,
                                        A.working_tree_hash(reg)))())
        # 검토 사이 새 갈래 유입 — 검토한 집합과 어긋나면 봉합 거부(본 적 없는
        # 갈래를 함께 봉합하지 않는다)
        rid3 = core._next_rid(max((r["rid"] for r in A.records()),
                                  key=core._rid_key))
        with open(A.APPROVALS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "rid": rid3, "parents": [head], "at": core.now_iso(),
                "kind": "approve", "region": reg, "base": A.approved_hash(reg),
                "accepted": A.working_tree_hash(reg), "reason": "기기3(유입)"},
                ensure_ascii=False) + "\n")
        check("본 적 없는 갈래는 함께 봉합되지 않는다",
              _raises(lambda: A.approve(reg, None, A.working_tree_hash(reg),
                                        seal_heads=forks))())
        forks = [f["rid"] for f in A.divergence(reg)]   # 3갈래 재검토
        (regdir / "a.md").write_text("봉합 시점 상태", encoding="utf-8")
        rec = A.approve(reg, None, expect_work=A.working_tree_hash(reg),
                        reason="분기 봉합", seal_heads=forks)
        check("봉합 승인이 검토한 갈래 셋을 모두 부모로 잇는다",
              len(rec["parents"]) == 3, rec["parents"])
        check("봉합 뒤 clean", A.state(reg) == "clean")
        check("현행 승인본이 봉합 시점 상태", A.approved_hash(reg) == rec["accepted"])
        check("이후 일반 승인이 다시 성립한다", A.divergence(reg) and
              len(A.divergence(reg)) == 1)
    finally:
        try: A.APPROVALS.write_text(kept, encoding="utf-8")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 위상 검증도 derived-from의 id 강제를 본다 (쓰기 통로 밖 유입) ────────
def test_topology_rejects_wiki_node_derived_from():
    """손으로 쓰거나 다기기 동기화로 들어온 노드는 쓰기 통로를 거치지 않는다 —
    위상 검증이 그 자리에서 `derived-from`의 비-id 노드 대상을 잡아야 한다."""
    target = ROOT / "= Scope/W1/regr-topo-t.md"
    bad = ROOT / "= Scope/W1/regr-topo-bad.md"
    try:
        target.write_text(node_text("260802-zzzz-topo", "대상", "본문"),
                          encoding="utf-8")
        bad.write_text(node_text("260802-zzzz-tbad", "비-id 근거", "본문",
                                 'derived-from: "[[regr-topo-t]]"\n'),
                       encoding="utf-8")
        errs = graph.topology_check(graph.Index())
        check("위키 표기의 노드 근거를 위반으로 잡는다",
              any("id로 단다" in e and "regr-topo-bad" in e for e in errs), errs)
        bad.write_text(node_text("260802-zzzz-tbad", "id 근거", "본문",
                                 "derived-from: 260802-zzzz-topo\n"),
                       encoding="utf-8")
        errs = graph.topology_check(graph.Index())
        check("id 근거는 위반이 아니다",
              not any("regr-topo-bad" in e for e in errs), errs)
    finally:
        target.unlink(missing_ok=True)
        bad.unlink(missing_ok=True)


# ── 잠금 자리 해석의 세 형태 (worktree·submodule 포함) ──────────────────
def test_local_lock_path_git_shapes():
    """잠금 자리가 클론 형태마다 달라지면 같은 vault의 두 프로세스가 서로 다른
    파일을 잡아 상호배제가 조용히 깨진다 — `.git`이 디렉터리·파일(gitdir)·
    worktree(commondir)인 세 형태와 git 부재를 모두 같은 규칙으로 푼다."""
    import tempfile as _tf
    base = Path(_tf.mkdtemp(prefix="osk-shape-"))
    try:
        plain = base / "plain"; (plain / ".git").mkdir(parents=True)
        check("`.git` 디렉터리 → 그 안",
              core.local_lock_path("x.lock", plain) == plain / ".git" / "x.lock")
        # worktree: `.git`이 파일이고 그 대상 안에 commondir가 있다
        main_git = base / "main" / ".git"
        (main_git / "worktrees" / "wt").mkdir(parents=True)
        (main_git / "worktrees" / "wt" / "commondir").write_text("../..\n",
                                                                encoding="utf-8")
        wt = base / "wt"; wt.mkdir()
        (wt / ".git").write_text(
            f"gitdir: {main_git / 'worktrees' / 'wt'}\n", encoding="utf-8")
        check("worktree → 공용 git 디렉터리(commondir)",
              core.local_lock_path("x.lock", wt).resolve()
              == (main_git / "x.lock").resolve(),
              core.local_lock_path("x.lock", wt))
        # submodule 등: `.git` 파일이 commondir 없는 디렉터리를 가리킨다
        sub_git = base / "modules" / "s"; sub_git.mkdir(parents=True)
        sm = base / "sm"; sm.mkdir()
        (sm / ".git").write_text(f"gitdir: {sub_git}\n", encoding="utf-8")
        check("gitdir 파일 → 그 디렉터리",
              core.local_lock_path("x.lock", sm).resolve()
              == (sub_git / "x.lock").resolve())
        nogit = base / "nogit"; nogit.mkdir()
        p = core.local_lock_path("x.lock", nogit)
        check("git 부재 → 추적 트리 밖 임시 자리",
              not str(p).startswith(str(nogit)) and p.name.startswith("x-"), p)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ── 구조 충돌은 첫 쓰기 전에 잡는다 — 부분 복원 금지 (PR #14 리뷰) ─────
def test_revert_structure_conflict_no_partial():
    """작업본에서 파일↔디렉터리가 뒤바뀐 평범한 재구성이면, 반영 도중 실패해
    앞선 파일만 덮인 **부분 복원**이 된다. 준비 단계에서 전수로 잡아 거부하므로
    다른 작업본 변경이 그대로 남는다."""
    from osk import approvals as A
    reg = "= Domain/struct"
    regdir = ROOT / "= Domain" / "struct"
    a, sub = regdir / "a.md", regdir / "sub"
    try:
        sub.mkdir(parents=True, exist_ok=True)
        a.write_text("승인본 A", encoding="utf-8")
        (sub / "b.md").write_text("승인본 B", encoding="utf-8")
        A.protect(reg, "지정")
        base = A.approved_hash(reg)
        a.write_text("작업본 A2", encoding="utf-8")      # 사용자의 변경
        shutil.rmtree(sub)
        sub.write_text("이제 파일이다", encoding="utf-8")  # 디렉터리→파일
        check("반려가 구조 충돌로 거부",
              _raises(lambda: A.revert(reg, base, A.working_tree_hash(reg)))())
        check("앞선 파일이 덮이지 않았다(부분 복원 없음)",
              a.read_text() == "작업본 A2")
        check("충돌 객체도 그대로", sub.read_text() == "이제 파일이다")
        # 반대 방향 — 승인본은 디렉터리 자리, 작업본은 그 자리에 파일
        sub.unlink()
        sub.mkdir()
        (sub / "b.md").write_text("승인본 B", encoding="utf-8")
        A.revert(reg, base, A.working_tree_hash(reg), "구조 정리 후 반려")
        check("구조를 바로잡으면 반려 성립", A.state(reg) == "clean")
        check("승인본으로 복원", a.read_text() == "승인본 A")
    finally:
        try: A.unprotect(reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 승인 조작은 노드 쓰기와 같은 잠금으로 직렬화된다 (PR #14 리뷰) ──────
def test_approval_serialized_with_writes():
    """반려의 마지막 전제 확인과 첫 파괴 사이에 정상 쓰기가 끼어들면 검토하지
    않은 변경이 사라진다. 네 조작 모두 노드 쓰기와 **같은** 전역 변경 잠금을
    잡는다 — 엔진이 통제하는 writer는 그 사이에 들어올 수 없다."""
    from osk import approvals as A
    from osk import write
    import sync_daemon
    check("변경 잠금이 sync·update가 쓰는 그 잠금과 같은 파일이다",
          core.mutation_lock_path()
          == sync_daemon._lock_path(ROOT, "osk-mutation.lock"))
    check("잠금 파일은 추적 트리 밖이다",
          not str(core.mutation_lock_path()).startswith(str(ROOT) + os.sep)
          or ".git" in core.mutation_lock_path().parts)
    # 같은 경로를 가리키는 것만으로는 부족하다 — 실제로 상호배제되는지 본다.
    # flock은 open file description마다이므로 같은 프로세스의 두 번째 열기도
    # 경합한다(데몬이 다른 프로세스에서 잡는 상황과 같은 판정).
    with core.mutation_lock():
        with open(sync_daemon._lock_path(ROOT, "osk-mutation.lock"), "w") as fh:
            try:
                core.lock_exclusive(fh, blocking=False)
                core.unlock(fh)
                got = True
            except OSError:
                got = False
        check("변경 잠금 보유 중에는 데몬 쪽 획득이 실패한다(실 상호배제)",
              got is False)
    held = []
    real = core.mutation_lock

    class _Spy(real):
        def __enter__(self):
            held.append(1)
            return super().__enter__()
    reg = "= Domain/lockchk"
    regdir = ROOT / "= Domain" / "lockchk"
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("v1", encoding="utf-8")
        A.mutation_lock = _Spy
        A.protect(reg, "지정")
        (regdir / "a.md").write_text("v2", encoding="utf-8")
        A.approve(reg, A.approved_hash(reg),
                  expect_work=A.working_tree_hash(reg), reason="승인")
        (regdir / "a.md").write_text("v3", encoding="utf-8")
        A.revert(reg, A.approved_hash(reg), A.working_tree_hash(reg), "반려")
        A.unprotect(reg, "해제")
        check("네 조작이 모두 잠금을 잡았다", len(held) == 4, len(held))
    finally:
        A.mutation_lock = real
        shutil.rmtree(regdir, ignore_errors=True)


# ── 부재와 '디렉터리가 아닌 것'은 다르다 (PR #14 리뷰) ─────────────────
def test_region_replaced_by_file_is_pending():
    """부재만 빈 작업본으로 읽는다 — 같은 경로에 일반 파일이 생기면 구조가
    깨진 것이므로 판정 불능(pending)이고 해제도 거부된다. 둘을 접으면 빈
    승인본을 가진 영역이 clean으로 오판되고 해제까지 허용된다."""
    from osk import approvals as A
    reg = "= Domain/asfile"
    regdir = ROOT / "= Domain" / "asfile"
    try:
        regdir.mkdir(parents=True, exist_ok=True)     # 빈 영역 → 승인본도 빈 tree
        A.protect(reg, "지정")
        check("빈 영역은 지정 직후 clean", A.state(reg) == "clean")
        regdir.rmdir()
        check("부재는 빈 작업본 — 여전히 clean", A.state(reg) == "clean")
        regdir.write_text("디렉터리가 파일로 바뀌었다", encoding="utf-8")
        check("같은 경로가 파일이면 판정 불능",
              A.working_tree_hash(reg) is None)
        check("따라서 pending", A.state(reg) == "pending")
        check("해제도 거부된다", _raises(lambda: A.unprotect(reg))())
        # 이 상태에서는 작업본을 판정할 수 없어 호출부가 None을 넘기게 된다.
        # 그때 인자 탓으로 보고하면 사용자가 실제 원인(치울 객체)을 못 본다 —
        # 경로 진단이 인자 검사보다 먼저다.
        try:
            A.revert(reg, A.approved_hash(reg), A.working_tree_hash(reg))
            msg = ""
        except ValueError as e:
            msg = str(e)
        check("거부 사유가 치울 객체를 가리킨다(인자 탓이 아니라)",
              "치우면" in msg, msg)
    finally:
        if regdir.is_file():
            regdir.unlink()
        regdir.mkdir(parents=True, exist_ok=True)
        try: A.unprotect(reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 거부된 반려는 부재조차 바꾸지 않는다 (PR #14 리뷰) ─────────────────
def test_refused_revert_leaves_region_absent():
    """CAS가 어긋나 거부된 반려는 무변이어야 한다 — 영역 재생성도 확인을 통과한
    뒤의 첫 mutation이다. 앞에 두면 '작업본을 건드리지 않았다'면서 부재→빈
    디렉터리라는 변경이 이미 일어난다."""
    from osk import approvals as A
    reg = "= Domain/absent"
    regdir = ROOT / "= Domain" / "absent"
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "a.md").write_text("본문", encoding="utf-8")
        A.protect(reg, "지정")
        base = A.approved_hash(reg)
        shutil.rmtree(regdir)                          # 영역째 삭제
        check("영역 경로가 부재", not regdir.exists())
        check("검토한 작업본이 어긋나면 반려 거부",
              _raises(lambda: A.revert(reg, base, base))())   # base != 빈 tree
        check("거부 후에도 경로는 여전히 부재(무변)", not regdir.exists())
        A.revert(reg, base, A.working_tree_hash(reg), "정상 복구")
        check("맞는 expect_work로는 복구된다",
              regdir.is_dir() and (regdir / "a.md").read_text() == "본문")
    finally:
        try: A.unprotect(reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 반려도 검토한 변경집합에만 성립한다 (PR #14 리뷰) ──────────────────
def test_revert_binds_reviewed_changeset():
    """반려는 파괴적이므로 승인과 **같은 결속**을 쓴다 — 확인 프롬프트 사이에
    에이전트가 더 쓴 변경까지 '사용자가 승인한 반려'로 묶여 사라지면 안 된다."""
    from osk import approvals as A
    reg = "= Domain/rbind"
    regdir = ROOT / "= Domain" / "rbind"
    f = regdir / "a.md"
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        f.write_text("승인본", encoding="utf-8")
        A.protect(reg, "지정")
        base = A.approved_hash(reg)
        f.write_text("검토한 변경 B", encoding="utf-8")
        reviewed = A.working_tree_hash(reg)            # 사용자가 확인한 변경집합
        f.write_text("프롬프트 사이 새 변경 C", encoding="utf-8")   # 에이전트 쓰기
        check("검토하지 않은 C가 섞이면 반려 거부",
              _raises(lambda: A.revert(reg, base, reviewed))())
        check("C가 살아 있다", f.read_text() == "프롬프트 사이 새 변경 C")
        check("영역은 여전히 pending", A.state(reg) == "pending")
        A.revert(reg, base, A.working_tree_hash(reg), "다시 검토 후 반려")
        check("다시 검토한 뒤에는 반려 성립", A.state(reg) == "clean")
        check("승인본으로 복원됨", f.read_text() == "승인본")
    finally:
        try: A.unprotect(reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 반려는 파괴 직전에 전제를 다시 본다 (PR #14 리뷰) ──────────────────
def test_revert_confirms_before_destroying():
    """반려는 파괴적이다 — 준비 도중 다른 기기의 승인이 들어오면, 기록만 막는
    사후 검사로는 부족하고 **작업본을 건드리기 전에** 거부해야 한다. 그러지
    않으면 사용자가 방금 승인한 내용이 옛 승인본으로 덮여 사라진다."""
    from osk import approvals as A
    reg = "= Domain/rcas"
    regdir = ROOT / "= Domain" / "rcas"
    f = regdir / "a.md"
    real_get = A._store_get
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        f.write_text("v1", encoding="utf-8")
        A.protect(reg, "지정")
        base = A.approved_hash(reg)                   # 승인본 A = v1
        f.write_text("v2", encoding="utf-8")
        other = A._store_tree(A.resolve_in_root(reg))  # 다른 기기가 승인할 tree B = v2
        check("반려 전 상태는 pending", A.state(reg) == "pending")

        def racing(h):                    # 준비(blob 적재) 도중 승인 B가 유입
            A._store_get = real_get
            core.ledger_append(A.APPROVALS, {
                "kind": "approve", "region": reg, "base": base,
                "accepted": other, "reason": "다른 기기(시험)"})
            return real_get(h)
        A._store_get = racing
        check("준비 중 승인본이 바뀌면 반려 거부",
              _raises(lambda: A.revert(reg, base, A.working_tree_hash(reg)))())
        A._store_get = real_get
        check("작업본이 옛 승인본으로 덮이지 않았다", f.read_text() == "v2")
        check("현행 승인본은 유입된 승인의 것", A.approved_hash(reg) == other)
        check("유입 승인 기준으로는 clean", A.state(reg) == "clean")
    finally:
        A._store_get = real_get
        try: A.unprotect(reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 지정 뒤의 동시 편집은 오류가 아니라 다음 변경집합이다 ──────────────
def test_protect_concurrent_write_becomes_pending():
    """스냅샷 직후의 정상 동시 편집은 지정을 무효로 만들지 않는다 — 영역이
    곧바로 pending으로 드러나고 사용자가 승인하거나 반려하면 된다. 이것을 하드
    오류로 바꾸면 자가 치유되는 상태를 재시도로 바꿀 뿐이다(파괴적인 반려에만
    작업본 결속을 건다)."""
    from osk import approvals as A
    reg = "= Domain/pcas"
    regdir = ROOT / "= Domain" / "pcas"
    f = regdir / "a.md"
    real_append = A.ledger_append
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        f.write_text("A", encoding="utf-8")

        def racing(path, record, expect=None):
            if record.get("kind") == "protect":       # 박제 뒤·append 전 편집
                A.ledger_append = real_append
                f.write_text("B", encoding="utf-8")
            return real_append(path, record, expect)
        A.ledger_append = racing
        rec = A.protect(reg, "지정")
        A.ledger_append = real_append
        check("지정은 성립한다", bool(rec.get("rid")))
        check("그 편집은 곧바로 변경집합으로 드러난다", A.state(reg) == "pending")
        check("승인본은 스냅샷 시점 상태", A.approved_hash(reg) == rec["accepted"])
        A.revert(reg, A.approved_hash(reg), A.working_tree_hash(reg), "반려")
        check("반려로 처분하면 clean", A.state(reg) == "clean")
        check("작업본이 스냅샷 상태로 복원", f.read_text() == "A")
    finally:
        A.ledger_append = real_append
        try: A.unprotect(reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 해제도 잠금 안 전제 재확인을 거친다 (PR #14 리뷰) ───────────────────
def test_unprotect_precondition_under_lock():
    """해제 판정과 append 사이에 다른 기기의 승인이 동기화로 들어오면 해제는
    성립하지 않는다 — 그러지 않으면 stale이 아니라 unprotected가 되어 사용자가
    보지 못한 최신 승인이 조용히 해제로 덮인다(행의 base도 이미 거짓이다)."""
    from osk import approvals as A
    reg = "= Scope/W2"
    f = ROOT / "= Scope/W2/regr-unp.md"
    (ROOT / "= Scope/W2").mkdir(exist_ok=True)
    f.write_text("v1", encoding="utf-8")
    real_append = A.ledger_append
    try:
        A.protect(reg, "지정")
        base = A.approved_hash(reg)
        d = A.resolve_in_root(reg)
        f.write_text("v2", encoding="utf-8")
        other = A._store_tree(d)          # 다른 기기가 승인할 tree(B)
        f.write_text("v1", encoding="utf-8")          # 작업본은 되돌려 clean
        check("해제 직전 상태는 clean", A.state(reg) == "clean")

        def racing(path, record, expect=None):
            if record.get("kind") == "unprotect":     # 판정 뒤·append 전에 유입
                A.ledger_append = real_append
                real_append(A.APPROVALS, {
                    "kind": "approve", "region": reg, "base": base,
                    "accepted": other, "reason": "다른 기기(시험)"})
            return real_append(path, record, expect)
        A.ledger_append = racing
        check("유입 승인이 있으면 해제 거부", _raises(lambda: A.unprotect(reg))())
        A.ledger_append = real_append
        check("영역은 여전히 보호 중", A.is_protected(reg))
        check("현행 승인본은 유입된 승인의 것", A.approved_hash(reg) == other)
    finally:
        A.ledger_append = real_append
        try: A.revert(reg, A.approved_hash(reg), A.working_tree_hash(reg), "정리")
        except Exception: pass
        try: A.unprotect(reg, "정리")
        except Exception: pass
        f.unlink(missing_ok=True)


# ── stale은 해제가 아니다 — 파일 판정이 fail-open 하지 않는다 ───────────
def test_stale_region_not_unprotected():
    """인과 극대가 둘이면(다기기 병합) 영역은 stale이다 — 판정 불능이지 해제가
    아니므로, 현황에서 사라지거나 파일 판정이 '보호영역 밖 = 제약 없음'으로
    새면 안 된다(fail-open 금지)."""
    from osk import approvals as A
    reg = "= Scope/W2"
    f = ROOT / "= Scope/W2/regr-stale.md"
    (ROOT / "= Scope/W2").mkdir(exist_ok=True)
    f.write_text("v1", encoding="utf-8")
    try:
        A.protect(reg, "지정")
        check("보호 직후 승인본 일치", A.file_matches_baseline(f))
        # 같은 head를 부모로 갖는 기록 두 줄 = 다기기 병합 결과(인과 극대 2).
        # ledger_append는 parents를 스스로 정하므로, 병합 결과 파일을 그대로
        # 흉내내려면 대장에 직접 적는다(git merge가 남기는 상태와 동일).
        head = A.records()[-1]["rid"]
        base_h, work_h = A.approved_hash(reg), A.working_tree_hash(reg)
        kept = A.APPROVALS.read_text(encoding="utf-8")     # 시험 뒤 되돌릴 원본
        rid = head
        with open(A.APPROVALS, "a", encoding="utf-8") as fh:
            for i in (1, 2):
                rid = core._next_rid(rid)
                fh.write(json.dumps({
                    "rid": rid, "parents": [head], "at": core.now_iso(),
                    "kind": "approve", "region": reg, "base": base_h,
                    "accepted": work_h, "reason": f"기기{i}(시험)"},
                    ensure_ascii=False) + "\n")
        check("영역이 stale", A.state(reg) == "stale")
        check("stale 영역도 현황에 남는다", reg in A.protected_regions())
        check("stale 영역 안의 파일은 보호영역 밖으로 새지 않는다",
              A.region_of(f) == reg)
        check("integrity도 stale을 적발",
              any("stale" in e and reg in e for e in A.integrity()))
        f.write_text("아무도 승인한 적 없는 내용", encoding="utf-8")
        check("stale에서 파일 판정은 불일치(fail-closed)",
              A.file_matches_baseline(f) is False)
        new = ROOT / "= Scope/W2/regr-stale-new.md"
        new.write_text("아무도 본 적 없는 새 파일", encoding="utf-8")
        check("stale 영역에 새로 생긴 파일도 불일치",
              A.file_matches_baseline(new) is False)
        new.unlink(missing_ok=True)
    finally:
        try: A.APPROVALS.write_text(kept, encoding="utf-8")   # 분기 원상 복구
        except Exception: pass
        f.unlink(missing_ok=True)
        try: A.unprotect(reg, "정리")
        except Exception: pass


# ── 중첩 영역: 안쪽 승인이 바깥 미승인을 가리지 않는다 ──────────────────
def test_nested_regions_all_checked():
    """파일이 여러 보호영역에 속하면 **전부**의 승인본과 일치해야 한다 — 하위
    구획만 승인하고 바깥 영역은 그 변경을 승인한 적 없는데 일치로 새면 안 된다."""
    from osk import approvals as A
    outer, inner = "= Scope/W3", "= Scope/W3/sub"
    od, idir = ROOT / "= Scope/W3", ROOT / "= Scope/W3/sub"
    x = idir / "x.md"
    try:
        idir.mkdir(parents=True, exist_ok=True)
        x.write_text("v1", encoding="utf-8")
        A.protect(outer, "바깥 지정")
        A.protect(inner, "안쪽 지정")
        check("초기에는 양쪽 다 일치", A.file_matches_baseline(x))
        x.write_text("v2", encoding="utf-8")
        A.approve(inner, A.approved_hash(inner),
                  expect_work=A.working_tree_hash(inner), reason="안쪽만 승인")
        check("안쪽은 clean", A.state(inner) == "clean")
        check("바깥은 pending", A.state(outer) == "pending")
        check("안쪽 승인만으로는 일치로 판정되지 않는다",
              A.file_matches_baseline(x) is False)
        check("가장 안쪽 영역만 물으면 일치", A.file_in_region_baseline(inner, x))
    finally:
        for r in (inner, outer):
            try: A.unprotect(r, "정리")
            except Exception: pass
        shutil.rmtree(od, ignore_errors=True)


# ── revert 미완료(삭제 실패)는 기록하지 않는다 (PR #14 리뷰 [high]) ──────
def test_revert_incomplete_no_record():
    """manifest에 없는 추가 파일의 삭제가 실패하면(권한 등) 작업본이 pending으로
    남고, revert는 복원 완료 확인에 실패해 대장에 기록하지 않는다 — '복원을 마친
    뒤에만 기록'(Mechanism §3 6항) 계약이 위장되지 않는다(fail-closed)."""
    from osk import approvals as A
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        check("root — 삭제 실패를 만들 수 없어 생략(skip)", True)
        return
    reg = "= Domain/revinc"
    regdir = ROOT / "= Domain" / "revinc"
    locked = regdir / "locked"
    try:
        regdir.mkdir(parents=True, exist_ok=True)
        (regdir / "keep.md").write_text("keep-v1", encoding="utf-8")
        A.protect(reg, "지정")
        locked.mkdir()
        (locked / "junk.md").write_text("junk", encoding="utf-8")   # pending(추가)
        before = len(A.records())
        os.chmod(locked, 0o500)                                     # 삭제 불가
        check("삭제 실패 시 revert가 예외로 거부",
              _raises(lambda: A.revert(reg, A.approved_hash(reg),
                                       A.working_tree_hash(reg)))())
        check("삭제 못한 파일이 남아 있다", (locked / "junk.md").exists())
        check("revert 대장 행이 추가되지 않았다", len(A.records()) == before)
        check("영역은 여전히 pending(복원 미완료)", A.state(reg) == "pending")
    finally:
        try:
            os.chmod(locked, 0o700)
        except OSError:
            pass
        shutil.rmtree(locked, ignore_errors=True)     # 추가분 정리 → clean
        try: A.unprotect(reg, "정리")
        except Exception: pass
        shutil.rmtree(regdir, ignore_errors=True)


# ── 승인본은 그 영역의 tree여야 한다 (PR #14 리뷰 [high]) ──────────────
def test_baseline_bound_to_region():
    """승인본 manifest는 **그 영역의** tree일 때만 해석된다 — 영역 밖 항목을 섞은
    tree는 정상 protect/approve가 만들 수 없으므로, 자기 파일 항목이 맞아도 권한
    판정이 성립하지 않는다(권위는 성립인데 복원은 거부되는 승인본 금지)."""
    from osk import approvals as A, authority
    reg = authority.DELEGATION_REGION
    dnode = ROOT / "= Person/Delegation/regr-bind.md"
    outsider = ROOT / "= Domain/regr-outside.md"
    clause = ("## 위임\n- 대상: 시험 행위\n- 범위: 시험\n"
              "- 조건: 없음\n- 종료: 없음\n")
    dnode.write_text(node_text("260802-zzzz-rgd3", "영역 결속 시험", clause),
                     encoding="utf-8")
    outsider.write_text("영역 밖 파일", encoding="utf-8")
    good = None
    try:
        A.protect(reg, "지정")
        good = A.approved_hash(reg)
        check("정상 승인본에서는 위임 성립", A.file_in_region_baseline(reg, dnode))
        # 위임 파일 항목은 현재 파일과 정확히 맞추고, 영역 밖 항목을 하나 섞는다
        entries = sorted(([r, h] for r, h in A._tree_table(good).items()),
                         key=lambda e: e[0])
        entries.append(["= Domain/regr-outside.md",
                        A._store_put(outsider.read_bytes())])
        entries.sort(key=lambda e: e[0])
        evil = A._store_put(A._manifest_blob(entries))
        check("혼합 tree 자체는 형상 검증을 통과", A._tree_table(evil) is not None)
        check("그러나 그 영역의 tree로는 해석되지 않는다",
              A._tree_table_for_region(reg, evil) is None)
        core.ledger_append(A.APPROVALS, {
            "kind": "approve", "region": reg, "base": good,
            "accepted": evil, "reason": "영역 밖 항목 혼입(시험)"})
        check("integrity가 영역 불일치를 적발",
              any("영역 불일치" in e and reg in e for e in A.integrity()), A.integrity())
        check("위임 성립이 부정된다(fail-closed)",
              A.file_in_region_baseline(reg, dnode) is False)
        eff = {d["title"]: d["effective"] for d in authority.enumerate_delegations()}
        check("권위 판정도 미성립", eff.get("regr-bind") is False, eff)
        check("revert도 거부",
              _raises(lambda: A.revert(reg, A.approved_hash(reg),
                                       A.working_tree_hash(reg)))())
        check("영역 밖 파일이 변하지 않음", outsider.read_text() == "영역 밖 파일")
    finally:
        if good:
            core.ledger_append(A.APPROVALS, {
                "kind": "approve", "region": reg, "base": None,
                "accepted": good, "reason": "시험 정리"})
        try: A.unprotect(reg, "정리")
        except Exception: pass
        dnode.unlink(missing_ok=True)
        outsider.unlink(missing_ok=True)


def test_baseline_pass():
    from osk import approvals
    # 보호영역 하나를 지정하고 clean 상태에서 검증기가 통과하는지 본다
    reg = "= Scope/W1"
    if not approvals.is_protected(reg):
        approvals.protect(reg, "기준선")
    rep = validate.run()
    check("정상 vault 기준선 PASS", rep["verdict"] == "PASS", rep["fail"])
    check("보호영역 현황이 clean으로 판정",
          rep.get("protected_regions", {}).get(reg) == "clean",
          rep.get("protected_regions"))
    # clean↔pending 불변식을 실제로 소진한다 — 항진명제가 아니게 한다
    junk = ROOT / "= Scope/W1/regr-baseline-junk.md"
    junk.write_text(node_text("260802-zzzz-rgba", "지문 변경"), encoding="utf-8")
    try:
        check("영역 내 쓰기가 pending으로 전이", approvals.state(reg) == "pending",
              approvals.state(reg))
        rep2 = validate.run()
        check("검증기 리포트도 pending을 싣는다",
              rep2.get("protected_regions", {}).get(reg) == "pending",
              rep2.get("protected_regions"))
    finally:
        junk.unlink(missing_ok=True)
    check("쓰기 회수 후 clean 복귀", approvals.state(reg) == "clean")
    approvals.unprotect(reg, "기준선 정리")   # 잔여 없이 되돌린다


# ── 14b. 자기 참조 PE는 계약 위반 (상태 자체를 불허) ──────────────────
def test_self_referencing_edge():
    p = ROOT / "= Scope/W1/regr-selfref.md"
    try:
        # 경로형·id형 어느 표기든 자기 참조는 적발한다(derived-from은 id로도)
        for pred, tgt in (("conflicts", "[[= Scope/W1/regr-selfref]]"),
                          ("derived-from", "[[regr-selfref.md]]"),
                          ("derived-from", "260802-zzzz-rg30")):
            p.write_text(node_text("260802-zzzz-rg30", "자기 참조", "본문",
                                   f'{pred}: "{tgt}"\n'), encoding="utf-8")
            errs = contract.validate(contract.parse(p))
            check(f"자기 참조 {pred}({tgt})는 계약 위반",
                  any("자기 자신" in e for e in errs), errs)
        p.write_text(node_text("260802-zzzz-rg30", "정상", "본문",
                               'derived-from: "[[regr-other]]"\n'), encoding="utf-8")
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

    # 드리프트 적발은 **사본**에 위반을 심어 시험한다 — 엔진 원본을 건드리지
    # 않는다. 사본은 `SURFACE_MODULES`에서 짓는다: 목록에 모듈이 하나 늘 때
    # 여기 손으로 적은 파일 목록이 따라오지 않으면, 검사가 "판독 실패"로 무너져
    # 정작 심은 위반을 못 본다(실측 — raw.py 편입에서 세 시험이 함께 죽었다).
    srcs = {rel: (ENGINE / rel).read_text(encoding="utf-8")
            for rel in validate.SURFACE_MODULES}
    real = srcs["mcp_server.py"]
    for inject, into, anchor, want in (
        ('@mcp.tool()\ndef sign_node(x: str) -> dict:\n'
         '    return signatures.sign(x, "r", "n")\n\n\n',
         "mcp_server.py", "@mcp.tool()", "선언되지 않은 도구"),
        ('@mcp.tool()\ndef pin_it() -> dict:\n'
         '    return ledger_append(PINS, {})\n\n\n',
         "mcp_server.py", "@mcp.tool()", "권위 대장에 기록"),
        ('def sneak():\n    return ledger_append(SIGNATURES, {})\n\n\n',
         "osk/write.py", "def create_node", "권위 대장에 기록"),
        ('def sneak_raw():\n    return ledger_append(PINS, {})\n\n\n',
         "osk/raw.py", "def append_round", "권위 대장에 기록"),
    ):
        with tempfile.TemporaryDirectory() as td:
            eng = Path(td) / "_engine"
            (eng / "osk").mkdir(parents=True)
            for rel, src in srcs.items():
                (eng / rel).write_text(src, encoding="utf-8")
            f = eng / into
            f.write_text(f.read_text(encoding="utf-8").replace(
                anchor, inject + anchor, 1), encoding="utf-8")
            errs = validate.surface_violations(eng)
            check(f"표면 드리프트 적발: {into} — {want}",
                  any(want in e for e in errs), errs)
    check("시험이 엔진 원본을 건드리지 않았다",
          (ENGINE / "mcp_server.py").read_text(encoding="utf-8") == real
          and not validate.surface_violations())


# ── 14d. (입건-재서명 차단 시험 폐지 — 서명 제도가 폐지됐다. 입건 당사자
#          노드의 노출·권위는 이제 보호영역 승인본이 다룬다.) ──


# ── 14e. 대장 행 형상 — 비-dict 행·비문자열 parents (4차 조건 나) ──────
def test_ledger_row_shape():
    wipe_sig()
    core.SIGNATURES.write_text("123\n", encoding="utf-8")
    check("비-dict 행은 부분 행과 동류의 손상으로 거부",
          _raises(lambda: core.ledger_read(core.SIGNATURES))())
    rep = validate.run()
    check("비-dict 행에도 검증기가 죽지 않고 FAIL 보고",
          rep["verdict"] == "FAIL")

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
          sig_status("260802-zzzz-rg41", node) == "unsigned")
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


def test_write_cas_body_bound():
    # 서명이 폐지됐으므로 CAS는 **본문 전체 치환**에만 결속한다(Mechanism
    # §6-2 4항) — 부분 변경(엣지 델타·summary)에는 요구하지 않는다.
    node = ROOT / "= Scope/W1/regr-w1.md"
    r = _w(write.update_node, "regr-w1", summary="고친 요약")
    check("summary 변경은 CAS 면제", r["ok"], r)
    check("덮은 요약을 응답에 담는다",
          r.get("replaced_summary") == "쓰기 통로 시험", r)
    r = _w(write.update_node, "regr-w1", add_edges={"derived-from": "regr-w1x"})
    check("엣지 델타도 면제", r["ok"], r)
    check("dangling을 응답으로 알린다", "regr-w1x" in r.get("dangling", []), r)
    check("응답에 폐지된 signed 필드가 없다", "signed" not in r, r)
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


def test_edge_single_list_roundtrip():
    """손으로/정본에서 단일 원소 리스트로 저장된 엣지(`derived-from: [<id>]`)를
    가진 노드가 그 엣지를 건드리지 않는 갱신에서 왕복 불일치로 거부되지 않는다
    (리스트 `[x]`와 스칼라 `x`는 대상이 하나로 논리적으로 같다)."""
    tgt = ROOT / "= Scope/W1/regr-sl-target.md"
    ref = ROOT / "= Scope/W1/regr-sl-ref.md"
    try:
        tgt.write_text(node_text("260802-zzzz-rgs1", "근거"), encoding="utf-8")
        # 단일 원소 리스트 표기로 손수 저장
        ref.write_text(node_text("260802-zzzz-rgs2", "파생", "본문",
                                 'derived-from: [260802-zzzz-rgs1]\n'),
                       encoding="utf-8")
        check("전제: 단일 원소 리스트로 파싱된다",
              contract.parse(ref).meta.get("derived-from") == ["260802-zzzz-rgs1"])
        r = _w(write.update_node, "regr-sl-ref", summary="요약 갱신")
        check("단일 원소 리스트 엣지 노드의 무관한 갱신이 거부되지 않는다",
              r.get("ok") is True, r)
        check("갱신 후에도 근거를 여전히 가리킨다",
              "260802-zzzz-rgs1" in contract.parse(ref).edges("derived-from"))
    finally:
        for p_ in (tgt, ref):
            p_.unlink(missing_ok=True)


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
        "r = write.update_node('regr-r2', add_edges={'derived-from': s.argv[1]})\n"
        "print(json.dumps({'ok': r['ok'], 'edges': r['edges']['derived-from']}))\n"
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
    final = contract.parse(ROOT / "= Scope/W1/regr-r2.md").edges("derived-from")
    check("두 델타가 모두 보존된다(lost update 없음)",
          "tgt0" in final and "tgt1" in final, final)


# ── 14h. 표면 스모크 — 선언된 도구 전부를 **직접 호출**한다 (7차 치명) ────
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
        "append_raw": lambda: M.append_raw("repo/smoke-raw", "regr-smoke-rec",
                                           "질문", "응답", space="= Scope/W1"),
        "read_raw": lambda: M.read_raw("[[= Scope/W1/_raw/regr-smoke-rec.md#1]]"),
        "scope_memory": lambda: M.scope_memory("repo/smoke-wm",
                                                   space="= Scope/W1"),
    }
    declared = set(validate.declared_tools() or [])
    check("스모크가 선언된 도구를 전부 부른다",
          declared == set(calls), sorted(declared ^ set(calls)))
    for name, fn in calls.items():
        try:
            out = fn()
            dead = isinstance(out, dict) and out.get("ok") is False
            check(f"표면 도구 살아 있음: {name}", not dead, out)
        except Exception as e:
            check(f"표면 도구 살아 있음: {name}", False, f"{type(e).__name__}: {e}")
    r = M.read_node("regr-smoke")
    check("read_node가 hash를 준다(CAS 입력)", r.get("hash", "").startswith("sha256:"))
    check("read_node 경로는 POSIX 표기", "\\" not in r.get("path", ""), r.get("path"))
    check("read_node에 폐지된 signed 필드가 없다", "signed" not in r, r)
    hits = M.search("스모크", 5)
    check("search 결과에 폐지된 signed 필드가 없다",
          all("signed" not in h for h in hits), hits)
    check("search 경로도 POSIX 표기",
          all("\\" not in h.get("path", "") for h in hits), hits)


# ── 14i. 직렬화 왕복 — 표면이 스스로 파손 노드를 만들지 않는다 (7차 중대 A) ──
def test_render_roundtrip():
    for label, kw in (
        ('따옴표', {"summary": 'He said "hi"'}),
        ('백슬래시', {"summary": r"경로 C:\temp\x"}),
        ('콜론·해시', {"summary": "a: b # c"}),
        ('엣지 따옴표', {"summary": "정상", "edges": {"derived-from": '따옴표"대상'}}),
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
    """derived-from의 **노드** 근거는 id로만 단다(Mechanism §8 2항) — 경로·이름은
    상태라 이동·개명에 끊어진다. 비노드 근거는 위키링크로 그대로 받는다."""
    r = _w(write.create_node, "regr-norm-t", "대상", "본문", "fable-5",
           space="= Scope/W1")
    tid = r["id"]
    for form in ("= Scope/W1/regr-norm-t", "regr-norm-t"):
        bad = _w(write.create_node, "regr-norm-x", "경로형 근거", "본문",
                 "fable-5", space="= Scope/W1", edges={"derived-from": form})
        check(f"노드 근거의 비-id 표기는 거부: {form}", not bad["ok"], bad)
        check("거부 사유가 id를 요구한다",
              any("id로 단다" in v for v in bad.get("violations", [])), bad)
    r0 = _w(write.create_node, "regr-norm", "표기 정규화", "본문",
            "fable-5", space="= Scope/W1", edges={"derived-from": tid})
    check("id 근거로는 생성", r0["ok"], r0)
    r1 = _w(write.update_node, "regr-norm", add_edges={"derived-from": tid})
    check("같은 id 추가는 중복 등재하지 않는다", r1.get("no_change") is True, r1)
    r2 = _w(write.update_node, "regr-norm",
            add_edges={"derived-from": "[[= Governance/없는문서]]"})
    check("비노드 근거는 위키링크로 받는다", r2["ok"], r2)
    r3 = _w(write.update_node, "regr-norm", remove_edges={"derived-from": tid})
    check("id 제거가 유효하다",
          r3["ok"] and tid not in (r3["edges"]["derived-from"] or []), r3)
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
    # 허브 노드를 골격으로 심는다 — 이 시험의 관심사는 결속이지 첫-노드
    # 규칙이 아니다(그 규칙은 test_cluster_overview가 소진한다)
    idx = ROOT / "= Domain/D1/D1.md"
    if not idx.exists():
        idx.write_text(node_text("260802-zzzz-d1ix", "D1 허브"),
                       encoding="utf-8")
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
                    "edges": {"derived-from": "regr-tx-t"}})   # dict 인자
                out["read"] = await call("read_node", {"name": "regr-tx"})
                out["stale"] = await call("update_node", {
                    "name": "regr-tx", "body": "새 본문",
                    "expect_hash": "sha256:틀림"})
                out["retry"] = await call("update_node", {
                    "name": "regr-tx", "body": "새 본문",
                    "expect_hash": out["read"]["hash"]})
                out["delta"] = await call("update_node", {
                    "name": "regr-tx",
                    "add_edges": {"derived-from": "regr-tx-t2"}})  # dict 인자
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
          o["delta"].get("ok") and "regr-tx-t2" in o["delta"]["edges"]["derived-from"],
          o["delta"])
    check("전송: run_validators가 서버 안에서도 동작(이벤트 루프 안)",
          o["validators"].get("verdict") in ("PASS", "FAIL")
          and not any("asyncio" in str(x) for f in o["validators"]["fail"]
                      for x in list(f.values())[0]),
          o["validators"]["fail"])
    check("전송: search 결과에 폐지된 signed 필드가 없다",
          all("signed" not in h for h in o["search"]), o["search"])
    (ROOT / "= Scope/W1/regr-tx.md").unlink(missing_ok=True)


# ── 14s. 표면 왕복 — search가 준 이름을 나머지 도구가 받는가 (8차 차단 ③) ──
def test_surface_name_roundtrip():
    """list_nodes를 없앤 설계에서 search는 이름을 얻는 유일한 통로다. 그 이름이
    그대로 쓰이지 않으면 발견과 지목 사이가 끊어진다."""
    import mcp_server as M
    r = _w(write.create_node, "regr-rt-name", "왕복", "본문 내용",
           "fable-5", space="= Scope/W1")
    check("전제: 생성", r["ok"], r)
    hits = [h for h in M.search("왕복", 8) if h["path"].endswith("regr-rt-name.md")]
    check("search가 찾는다", len(hits) == 1, hits)
    h = hits[0]
    check("title은 노드 이름 그대로다(변조 없음)",
          h["title"] == "regr-rt-name", h["title"])
    check("그 title로 read_node가 된다",
          "error" not in M.read_node(h["title"]), M.read_node(h["title"]))
    check("그 title로 update_node가 된다",
          _w(write.update_node, h["title"], summary="갱신")["ok"])
    # derived-from의 노드 근거는 id다 — 그 id도 같은 검색 결과가 함께 준다.
    # (발견→지목이 끊어지지 않는 것은 title이 아니라 이 id가 담보한다)
    check("검색 결과가 id도 함께 준다", bool(h.get("id")), h)
    check("그 id를 엣지 대상으로 쓰면 dangling이 아니다",
          not _w(write.create_node, "regr-rt-ref", "참조", "본문",
                 "fable-5", space="= Scope/W1",
                 edges={"derived-from": h["id"]})["dangling"])
    for nm in ("regr-rt-name", "regr-rt-ref"):
        (ROOT / f"= Scope/W1/{nm}.md").unlink(missing_ok=True)


# ── 14t. 계약 검증은 목적지 경로로 한다 (8차 차단 ①) ────────────────────
def test_validate_uses_destination_path():
    """되읽은 노드가 임시 파일명을 들면 stem에 걸린 계약 규칙이 무력화된다 —
    표면이 자기 검증기가 위반이라 부르는 노드를 ok로 쓰게 된다."""
    r = _w(write.create_node, "regr-selfref-w", "자기 참조", "본문",
           "fable-5", space="= Scope/W1",
           edges={"derived-from": "regr-selfref-w"})
    check("자기 참조 derived-from을 쓰기 통로가 거부한다", not r["ok"], r)
    check("거부 사유가 계약 문언 그대로",
          any("자기 자신" in v for v in r["violations"]), r)
    check("거부했으므로 파일이 없다",
          not (ROOT / "= Scope/W1/regr-selfref-w.md").exists())


# ── 14u. 동명 중복이면 쓰기를 거부한다 (8차 차단 ②) ─────────────────────
def test_dup_stem_write_refused():
    """읽기(허브)와 쓰기가 서로 다른 쪽을 고르면 본 파일과 고쳐지는 파일이
    달라진다. 표면은 임의로 한쪽을 택하지 않는다."""
    (ROOT / "= Scope/W3").mkdir(parents=True, exist_ok=True)
    a = ROOT / "= Scope/W1/regr-dup.md"
    b = ROOT / "= Scope/W3/regr-dup.md"
    try:
        a.write_text(node_text("260806-aaaa-1111", "중복 A", "A 본문"), encoding="utf-8")
        b.write_text(node_text("260806-aaaa-3333", "중복 B", "B 본문"), encoding="utf-8")
        check("허브가 중복을 인지", "regr-dup" in graph.Index().dup_stems)
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
          "derived-from" in _json.dumps(cn.inputSchema["properties"]["edges"]))


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
    for bad in ({"derived-from": 42}, {"derived-from": [{"x": 1}]},
                {"derived-from": ""}, {"derived-from": [None]}):
        r = _w(write.create_node, "regr-edgeval", "형 검사", "본문",
               "fable-5", space="= Scope/W1", edges=bad)
        check(f"엣지 값 {bad} 거부", not r["ok"], r)
        check("거부했으므로 파일이 없다",
              not (ROOT / "= Scope/W1/regr-edgeval.md").exists())
    # remove_edges도 add_edges와 같은 검사를 받는다(비대칭 제거)
    r0 = _w(write.create_node, "regr-sym", "대칭", "본문", "fable-5",
            space="= Scope/W1", edges={"derived-from": "대상A"})
    check("전제: 생성", r0["ok"], r0)
    for kw in ({"add_edges": {"suported-by": "X"}},
               {"remove_edges": {"suported-by": "X"}}):
        r = _w(write.update_node, "regr-sym", **kw)
        check(f"술어 오타 거부: {list(kw)[0]}", not r["ok"], r)
        check("거부 사유에 쓸 수 있는 술어가 실린다",
              any("derived-from" in v for v in r["violations"]), r)
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
    # lineage-fork는 계보 술어 폐지로 사라졌다. 기계 판정이 남는 유형은
    # duplication(동명 stem의 독립 노드) 하나다.
    (ROOT / "= Scope/W2").mkdir(exist_ok=True)
    a = ROOT / "= Scope/W1/regr-dup.md"
    b = ROOT / "= Scope/W2/regr-dup.md"
    try:
        a.write_text(node_text("260802-zzzz-rg20", "동명 A"), encoding="utf-8")
        b.write_text(node_text("260802-zzzz-rg21", "동명 B"), encoding="utf-8")
        cands = validate.conflict_candidates(graph.Index())
        check("duplication 검출(동명 stem)",
              any("duplication" in x and "regr-dup" in x for x in cands), cands)
        rep = validate.run()
        check("충돌 후보는 검증기 FAIL로 사용자 심의 요청",
              rep["verdict"] == "FAIL"
              and any("정합성 검사" in list(f)[0] for f in rep["fail"]))
        before = len(core.ledger_read(core.CANDIDATES))
        validate.run()
        check("검증기는 후보를 대장에 자동 기록하지 않는다(자동 집행 없음)",
              len(core.ledger_read(core.CANDIDATES)) == before)
    finally:
        for p_ in (a, b):
            p_.unlink(missing_ok=True)
    check("후보 해소 후 기준선 복귀",
          not any("duplication" in x and "regr-dup" in x
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

            # 갱신이 통치 문서를 덮으면 그 차이가 통치 구획 보호영역의 변경집합
            # 으로 남는다 — 사용자의 승인이 수용 기록이다 (Mechanism §1-2 6항 ·
            # 시행령 §10 2항). 여기서는 updater가 통치 문서를 실제로 덮는지만 본다
            # (수용=승인의 생애는 보호영역 fixture가 소진한다).
            gp = ROOT / "_governance/UpdDoc.md"
            (can / "_governance/UpdDoc.md").write_text(
                node_text("260802-uupd-0002", "정본 규범 문서", "1조 개정."),
                encoding="utf-8")
            git(can, "add", "-A")
            git(can, "commit", "-qm", "gov v2")
            _rel("v9.0.1")
            update.run(source="bundle", bundle=str(can), apply=True)
            check("갱신이 통치 문서를 정본 내용으로 덮는다",
                  "1조 개정" in gp.read_text(encoding="utf-8"))
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


# ── 18. `_raw/` 기록 통로 (헌법 4조 3~4항 · 시행령 §2 · Mechanism §9 4~6항) ──
def test_raw_append():
    """`_raw/`는 증거다. 이 수트가 지키는 것은 세 가지다 — 한 번 쓴 바이트가
    다시는 바뀌지 않는 것, 라운드 번호가 이미 쓴 근거 참조를 배신하지 않는 것,
    그리고 비밀값이 이 통로를 우회해 남지 않는 것."""
    from osk import raw, secrets
    (ROOT / "= Scope/WRaw").mkdir(exist_ok=True)
    S, REC = "repo/regr-raw", "2026-08-21-regr"
    p = ROOT / "= Scope/WRaw/_raw" / f"{REC}.md"

    r1 = _w(raw.append_round, S, REC, "첫 질문", "첫 응답", space="= Scope/WRaw")
    check("최초 라운드는 1", r1.get("index") == 1, r1)
    check("round_ref가 근거 표기 그대로다",
          r1.get("round_ref") == f"[[= Scope/WRaw/_raw/{REC}.md#1]]", r1)
    check("첫 기록이 세션을 결속한다", write.resolve_session(S) == "WRaw")

    r2 = _w(raw.append_round, S, REC, "둘째 질문", "둘째 응답")   # 라우팅으로 착지
    check("index 단조 증가", r2.get("index") == 2, r2)
    body = p.read_text(encoding="utf-8")
    check("먼저 쓴 라운드가 그대로 남는다", "첫 질문" in body and "첫 응답" in body)

    # 대화 본문의 숫자 H2가 라운드 제목으로 오독되지 않는다 (Mechanism §8 3항)
    _w(raw.append_round, S, REC, "본문에 ## 24 가 있다", "응답\n## 7\n끝")
    body = p.read_text(encoding="utf-8")
    check("escape로 라운드 수가 늘지 않는다", raw.rounds(body) == [1, 2, 3],
          raw.rounds(body))
    check("escape는 역연산이 있다",
          raw.unescape_numeric_h2(raw.escape_numeric_h2("## 7\n\\## 9"))
          == "## 7\n\\## 9")

    # 비밀값은 통로에서 치환된다 (시행령 §2 3항 — 우회 경로를 두지 않는다).
    # fixture를 쪼개 쓰는 이유: 릴리스의 비밀값 스캔이 전 파일을 훑으므로
    # (release.py, secrets.py만 자기 면제) 소스에 완전형을 두면 선언이 막힌다.
    r4 = _w(raw.append_round, S, REC,
            "키 " + "AKIA" + "IOSFODNN7EXAMPLE" + " 준다", "받았다")
    body = p.read_text(encoding="utf-8")
    check("비밀값이 기록에 남지 않는다", ("AKIA" + "IOSFODNN7EXAMPLE") not in body)
    check("치환 사실을 호출자에게 알린다",
          r4.get("filtered") == ["aws-access-key"], r4)

    # 접두부 보존 — 증거를 소급해 고쳐 쓰지 못한다 (Mechanism §9 4항)
    before = p.read_bytes()
    try:
        secrets.write_raw(p, "과거를 지운 새 내용")
        check("접두부 훼손을 거부한다", False, "거부하지 않았다")
    except ValueError as e:
        check("접두부 훼손을 거부한다", "append 아님" in str(e), e)
    check("거부는 파일을 건드리지 않는다", p.read_bytes() == before)

    # 손상된 index 열 위에는 이어 쓰지 않는다 (Mechanism §9 6항)
    bad = ROOT / "= Scope/WRaw/_raw/corrupt.md"
    bad.write_text("## 1\n\n본문\n\n## 1\n\n중복\n", encoding="utf-8")
    r = _w(raw.append_round, S, "corrupt", "질문", "응답")
    check("중복 index 기록에 이어 쓰지 않는다", r.get("ok") is False, r)

    # 통로 fail-closed — `_raw/` 밖은 이 함수로 쓸 수 없다
    try:
        secrets.write_raw(ROOT / "= Scope/WRaw/노드.md", "x")
        check("`_raw/` 밖 경로를 거부한다", False, "거부하지 않았다")
    except ValueError as e:
        check("`_raw/` 밖 경로를 거부한다", "_raw" in str(e), e)

    # 라운드는 쌍이다 / 착지·이름은 fail-closed
    check("빈 응답을 거부한다",
          _w(raw.append_round, S, REC, "질문만", "  ").get("ok") is False)
    check("결속 없는 세션은 착지를 요구한다",
          _w(raw.append_round, "repo/unbound-raw", "r", "q", "a").get("ok") is False)
    check("맨 scope 이름을 거부한다",
          _w(raw.append_round, "repo/x", "r", "q", "a", space="WRaw").get("ok") is False)
    check("Windows 예약 장치명을 기록 이름으로 거부한다",
          _w(raw.append_round, S, "COM1", "q", "a").get("ok") is False)

    # 이식성 기준으로 같은 이름은 같은 정본 (시행령 §2 1항)
    r5 = _w(raw.append_round, S, REC.upper(), "대소문자", "같은 파일")
    check("대소문자만 다른 이름은 같은 기록으로 접힌다",
          r5.get("path", "").endswith(f"{REC}.md"), r5)


# ── 18b. 훅 경로 — 실제 대화 바이트를 stdin으로 받는다 (헌법 4조 3항) ──────
def test_raw_cli_path():
    """표면의 `append_raw`는 에이전트가 **서술한** 라운드를 받는다. 전량 포착은
    전사를 그대로 나를 수 있어야 성립하므로, 같은 통로에 기계 입력 경로를 둔다.
    여기서 지키는 것은 봉투 계약과 **배치의 원자성**이다 — 라운드마다 따로
    쓰면 중간 거부에서 '있었던 대화의 일부'가 남는다."""
    from osk import cli, raw
    import io, types
    (ROOT / "= Scope/WRawCli").mkdir(exist_ok=True)
    SP, S, REC = "= Scope/WRawCli", "hook/regr", "2026-08-21-hook"

    def run(argv, payload=None):
        out, real_emit, real_stdin = {}, cli._emit, sys.stdin
        try:
            cli._emit = out.update
            if payload is not None:
                sys.stdin = types.SimpleNamespace(
                    isatty=lambda: False,
                    buffer=io.BytesIO(payload.encode("utf-8")))
            try:
                cli.main(argv)
            except SystemExit as e:
                out["exit"] = e.code
        finally:
            cli._emit, sys.stdin = real_emit, real_stdin
        return out

    r = run(["raw", "append", "--session", S, "--record", REC, "--space", SP],
            json.dumps({"rounds": [{"user": f"질문{i}", "agent": f"응답{i}"}
                                   for i in (1, 2, 3)]}, ensure_ascii=False))
    check("배치가 한 번에 이어진다", r.get("indices") == [1, 2, 3], r)

    st = run(["raw", "status", "--session", S, "--record", REC])
    check("status가 기록된 분량을 센다",
          (st.get("rounds"), st.get("next_index")) == (3, 4), st)

    # 배치 원자성 — 기존 기록이 있는 상태에서 중간 거부
    p = ROOT / SP / "_raw" / f"{REC}.md"
    before = p.read_bytes()
    r = run(["raw", "append", "--session", S, "--record", REC],
            json.dumps({"rounds": [{"user": "좋다", "agent": "응답"},
                                   {"user": "나쁘다", "agent": "  "}]},
                       ensure_ascii=False))
    check("중간 거부는 배치 전체를 무른다", r.get("ok") is False, r)
    check("거부 시 종료코드가 0이 아니다", r.get("exit") == 1, r)
    check("거부는 기존 기록을 건드리지 않는다", p.read_bytes() == before)

    # 봉투 모양 — 라운드 하나만 보낼 때 감싸기를 강요하지 않는다
    r = run(["raw", "append", "--session", S, "--record", REC],
            json.dumps({"user": "홑겹", "agent": "응답"}, ensure_ascii=False))
    check("라운드 하나는 감싸지 않아도 된다", r.get("indices") == [4], r)
    r = run(["raw", "append", "--session", S, "--record", REC],
            json.dumps([{"user": "배열", "agent": "응답"}], ensure_ascii=False))
    check("배열 봉투도 받는다", r.get("indices") == [5], r)

    # 플래그가 봉투를 이긴다 — 거는 쪽의 뜻이 생성기의 값보다 앞선다
    r = run(["raw", "append", "--session", S, "--record", REC],
            json.dumps({"record": "다른이름", "user": "q", "agent": "a"},
                       ensure_ascii=False))
    check("플래그 record가 봉투를 이긴다",
          r.get("path", "").endswith(f"{REC}.md"), r)

    # 손상 기록은 셀 수 없다고 말한다 — 그 위에 이어 붙이게 두지 않는다
    (ROOT / SP / "_raw" / "dmg.md").write_text(
        "## 2\n\n본문\n\n## 1\n\n역행\n", encoding="utf-8")
    st = run(["raw", "status", "--session", S, "--record", "dmg"])
    check("손상 기록은 damaged로 보고한다",
          st.get("damaged") is True and st.get("next_index") is None, st)

    # stdin은 바이트로 읽고 UTF-8로 푼다 — 콘솔 코드페이지에 인질이 되지 않는다
    buf = io.BytesIO()
    real = sys.stdout
    try:
        sys.stdout = types.SimpleNamespace(buffer=buf)
        cli._emit({"한글": "값"})
    finally:
        sys.stdout = real
    check("_emit은 UTF-8 바이트를 낸다",
          json.loads(buf.getvalue().decode("utf-8")) == {"한글": "값"},
          buf.getvalue())


# ── 18c. 명시 회상 — 근거에서 증거로 번역 없이 간다 (시행령 §2 5항) ────────
def test_raw_read():
    """`_raw/`는 작업 검색에서 빠지므로(헌법 11조 3항) 좌표로 연다. 이 수트가
    지키는 것은 **왕복**이다 — 쓰기가 준 `round_ref`가 `derived-from`에 앉고,
    그 값이 그대로 읽기의 입력이어야 근거에서 증거로 가는 데 번역이 끼지 않는다."""
    from osk import raw
    (ROOT / "= Scope/WRawRd").mkdir(exist_ok=True)
    SP, S, REC = "= Scope/WRawRd", "repo/regr-read", "rec"

    w = _w(raw.append_rounds, S, REC, [
        {"user": "첫 질문", "agent": "첫 응답"},
        {"user": "둘째 질문", "agent": "본문에\n## 7\n숫자 제목"},
        {"user": "긴 것", "agent": "가" * 3000},
    ], SP)
    check("세 라운드 기록", w.get("indices") == [1, 2, 3], w)

    # 왕복 — 쓰기가 준 좌표를 그대로 읽기에 넣는다
    r = _w(raw.read_round, w["round_refs"][1])
    check("round_ref가 그대로 읽기의 입력이다", r.get("index") == 2, r)
    check("라운드 본문이 온다", "둘째 질문" in r.get("text", ""), r)
    check("이웃 라운드가 섞이지 않는다",
          "첫 질문" not in r.get("text", "") and "긴 것" not in r.get("text", ""))

    # escape 역연산 — 파일엔 `\## 7`, 회상엔 `## 7` (Mechanism §8 3항)
    p = ROOT / SP / "_raw" / f"{REC}.md"
    check("파일에는 escape된 채로 있다", "\\## 7" in p.read_text(encoding="utf-8"))
    check("회상은 escape를 되돌린다",
          "\n## 7\n" in r.get("text", "") and "\\##" not in r.get("text", ""), r)

    # 절단은 조용히 하지 않는다
    r3 = _w(raw.read_round, w["round_refs"][2], 500)
    check("긴 라운드는 잘린다", len(r3.get("text", "")) == 500, r3)
    check("자른 사실과 원 길이를 알린다",
          r3.get("truncated") is True and r3.get("chars") > 3000, r3)

    # 좌표가 없으면 목차까지만 — 본문을 쏟지 않는다
    idx = _w(raw.read_round, f"{SP}/_raw/{REC}.md")
    check("index 없으면 목차", idx.get("rounds") == 3 and "text" not in idx, idx)
    check("목차는 미리보기만 싣는다",
          [x["preview"] for x in idx["index"]][:2] == ["첫 질문", "둘째 질문"], idx)

    # 표기 관용 — 저장 표기·맨 표기 모두 같은 좌표로 읽힌다
    for label, ref in (("위키링크", f"[[{SP}/_raw/{REC}.md#1]]"),
                       ("맨 표기", f"{SP}/_raw/{REC}.md#1")):
        check(f"좌표 표기: {label}", _w(raw.read_round, ref).get("index") == 1)

    # scope의 기록 목록 — 좌표를 모를 때의 출발점
    ls = _w(raw.list_records, SP)
    check("기록 목록", [x["record"] for x in ls.get("records", [])] == [REC], ls)
    check("목록은 라운드 수를 센다", ls["records"][0]["rounds"] == 3, ls)

    # 봉쇄는 쓰기와 같은 규율이다 — 읽기라고 느슨하면 vault 밖을 읽는 창이 된다
    for label, ref in (
            ("vault 밖 탈출", "[[../../../../etc/passwd]]"),
            ("_raw 밖 노드", f"[[{SP}/어떤노드.md]]"),
            ("없는 기록", f"[[{SP}/_raw/없다.md#1]]")):
        check(f"회상 봉쇄: {label}", _w(raw.read_round, ref).get("ok") is False)
    miss = _w(raw.read_round, f"[[{SP}/_raw/{REC}.md#9]]")
    check("없는 라운드는 있는 것을 알려준다",
          miss.get("ok") is False and "[1, 2, 3]" in str(miss.get("violations")),
          miss)

    # 목차의 길이와 본문의 길이는 같은 것을 재야 한다 — 예산을 재려고 목차를
    # 본 호출자가 실제 응답과 다른 수를 쥐면 안 된다(감사 지적).
    check("목차 chars == 회상 chars",
          [x["chars"] for x in idx["index"]]
          == [_w(raw.read_round, r).get("chars") for r in w["round_refs"]],
          (idx["index"], w["round_refs"]))


# ── 18d. 형식이 어긋난 `space`를 조용히 버리지 않는다 (감사 적발) ───────────
def test_raw_space_misdiagnosis():
    """결속이 **있는데도** 형식이 틀린 `space` 하나 때문에 "결속이 없다"고
    답하던 오진의 고정. 틀린 원인을 지목하는 거부는 거부하지 않느니만 못하다 —
    받는 쪽은 멀쩡한 세션 키를 바꿔가며 헤맨다(코드를 보지 않은 감사에서 실측)."""
    from osk import raw
    (ROOT / "= Scope/WRawSp").mkdir(exist_ok=True)
    S = "repo/regr-space"
    ok = _w(raw.append_round, S, "rec", "q", "a", space="= Scope/WRawSp")
    check("결속을 만든다", ok.get("index") == 1, ok)
    check("결속이 섰다", write.resolve_session(S) == "WRawSp")

    r = _w(raw.append_round, S, "rec", "q2", "a2", space="WRawSp")   # 맨 이름
    v = " ".join(r.get("violations", []))
    check("형식 오류를 space 자신의 문제로 지목한다",
          r.get("ok") is False and "space 표기가 아니다" in v, r)
    check("결속이 있는데 '결속이 없다'고 하지 않는다", "결속이 없다" not in v, r)

    # 진짜 미결속·없는 scope의 진단은 그대로여야 한다 (오진을 반대로 만들지 않기)
    r2 = _w(raw.append_round, "repo/regr-unbound-2", "rec", "q", "a")
    check("진짜 미결속은 결속을 지목한다",
          "결속이 없다" in " ".join(r2.get("violations", [])), r2)
    r3 = _w(raw.append_round, S, "rec", "q", "a", space="= Scope/없는스코프")
    check("없는 scope는 scope를 지목한다",
          "scope가 아니다" in " ".join(r3.get("violations", [])), r3)
    check("결속만으로도 계속 이어진다",
          _w(raw.append_round, S, "rec", "q3", "a3").get("index") == 2)

    # status도 같은 규율 — 조용히 0을 내지 않는다
    check("status도 형식 오류를 거부한다",
          _w(raw.record_state, S, "rec", "WRawSp").get("ok") is False)


# ── 18d-2. 한 세션의 기록이 여러 scope로 번지지 않는다 (Mechanism §6-2 6항) ──
def test_raw_binding_confines_scope():
    """결속이 선 세션에 다른 `space`를 주면 **쓰기 전에** 거부한다.

    허용하면 같은 기록 이름이 두 scope에 앉아 시행령 §2 1항의 '세션당 정본
    하나'가 깨지고, 결속은 그대로라 다음 호출은 원래 자리로 돌아가 한 대화가
    두 파일을 오간다. `_raw/`는 append-only이고 표면에 삭제가 없으므로 쓴 뒤에
    알리는 것으로는 되돌릴 수 없다."""
    from osk import raw
    for n in ("WBindA", "WBindB"):
        (ROOT / f"= Scope/{n}").mkdir(exist_ok=True)
    S = "repo/regr-bind"
    check("첫 쓰기가 결속을 세운다",
          _w(raw.append_rounds, S, "rec", [{"user": "q", "agent": "a"}],
             "= Scope/WBindA").get("indices") == [1])

    r = _w(raw.append_rounds, S, "rec", [{"user": "딴데", "agent": "씀"}],
           "= Scope/WBindB")
    v = " ".join(r.get("violations", []))
    check("교차 scope는 거부", r.get("ok") is False, r)
    check("거부가 현재 결속을 알려준다", "= Scope/WBindA" in v, v)
    check("건너간 자리에 파일이 생기지 않았다",
          not (ROOT / "= Scope/WBindB/_raw/rec.md").exists())
    check("정본은 하나뿐", raw.record_state(S, "rec")["rounds"] == 1)

    # 결속과 같은 space를 중복 명시하는 것은 무해하므로 통과해야 한다
    check("결속과 같은 space는 통과",
          _w(raw.append_rounds, S, "rec", [{"user": "q2", "agent": "a2"}],
             "= Scope/WBindA").get("indices") == [2])
    check("space 생략도 통과",
          _w(raw.append_rounds, S, "rec2",
             [{"user": "q", "agent": "a"}]).get("indices") == [1])
    # 미결속 세션은 여전히 어디로든 첫 착지를 정할 수 있다
    check("미결속 세션의 첫 착지는 자유",
          _w(raw.append_rounds, "repo/regr-bind-2", "rec",
             [{"user": "q", "agent": "a"}], "= Scope/WBindB").get("indices") == [1])


# ── 18e. 재시도 중복 거부 (Mechanism §9 7항 · 시행령 §2 1항) ────────────────
def test_raw_replay_rejected():
    """응답이 유실되면 호출자는 같은 배치를 다시 보낸다. `_raw/`는 append-only이고
    표면에 삭제가 없으므로 그렇게 생긴 중복은 되돌릴 수 없다 — 쓰기 전에 막는다.
    조용히 접지 않고 **거부**하는 것은, 접으면 쓰지 않고 성공을 보고하는 것이 되어
    호출자가 무슨 일이 있었는지 모르기 때문이다."""
    from osk import raw
    (ROOT / "= Scope/WRawRp").mkdir(exist_ok=True)
    SP, S = "= Scope/WRawRp", "repo/regr-replay"
    B = [{"user": "q1", "agent": "a1"}, {"user": "q2", "agent": "a2"}]

    check("최초 배치", _w(raw.append_rounds, S, "rec", B, SP).get("indices") == [1, 2])
    r = _w(raw.append_rounds, S, "rec", B)
    check("같은 배치 재시도는 거부", r.get("ok") is False, r)
    check("거부가 어디를 보라고 알려준다",
          "read_raw" in " ".join(r.get("violations", [])), r)
    check("거부는 아무것도 쓰지 않았다", raw.record_state(S, "rec")["rounds"] == 2)

    check("꼬리 일부만 재시도해도 거부",
          _w(raw.append_rounds, S, "rec", [B[1]]).get("ok") is False)
    check("다른 내용은 통과",
          _w(raw.append_rounds, S, "rec",
             [{"user": "q3", "agent": "a3"}]).get("indices") == [3])
    # 꼬리가 아닌 옛 라운드의 반복은 재시도가 아니다 — 정말 같은 말이 다시 오갔을
    # 수 있고, 그것을 막으면 대화를 기록하지 못한다.
    check("꼬리가 아니면 같은 내용도 통과",
          _w(raw.append_rounds, S, "rec", [B[0]]).get("indices") == [4])

    # 비밀값이 든 라운드의 재시도 — 저장본은 치환됐으므로 치환 전으로 견주면
    # 다른 것이 되어 빠져나간다. 판정은 치환 뒤로 한다.
    SEC = [{"user": "키 " + "AKIA" + "IOSFODNN7EXAMPLE" + " 준다",
            "agent": "받았다"}]
    check("비밀값 라운드 최초",
          _w(raw.append_rounds, S, "sec", SEC, SP).get("indices") == [1])
    check("비밀값 라운드의 재시도도 거부",
          _w(raw.append_rounds, S, "sec", SEC).get("ok") is False)



# ── 24. 군집 허브 노드는 위임이 아니다 (시행령 §5 1항) ─────────────────────
def test_index_node_not_delegation():
    """Delegation Facet도 노드 군집이라 동명 허브을 두는데(헌법 3조 8항),
    v3.3.0에서 허브가 승인본에 들어가는 순간 위임으로 열거되어 절 형식
    검사가 영구 실패했다(실측). 열거가 허브를 걸러야 두 제도가 공존한다."""
    from osk import authority, validate
    idx = ROOT / "= Person/Delegation/Delegation.md"
    made = not idx.exists()
    if made:
        idx.write_text(node_text("260802-zzzz-dgix", "위임 Facet 허브"),
                       encoding="utf-8")
    try:
        dels = authority.enumerate_delegations()
        check("허브 노드는 위임으로 열거되지 않는다",
              all(d["title"] != "Delegation" for d in dels), dels)
        rep = validate.run()
        check("허브 노드가 위임 3요건을 깨뜨리지 않는다",
              not any("위임 3요건" in k for d in rep["fail"] for k in d),
              rep["fail"])
    finally:
        if made:
            idx.unlink(missing_ok=True)


# ── 23. 옵시디언 태그 방어 (Mechanism §8 7항) ───────────────────────────────
def test_obsidian_tag_defense():
    """`#숫자`에 조사·가운뎃점이 직결되면 옵시디언 태그가 된다(순수 숫자만은
    태그가 아님). 거부 없이 공백 하나를 넣어 태그화만 끊는다 — 종결 문자와
    코드 구획은 불변, `_raw/`는 전사 보존이라 적용하지 않는다."""
    from osk import scope_memory as sm
    f = write._space_numeric_tags
    check("조사 직결은 띄운다", f("#1227은 문제") == "#1227 은 문제")
    check("가운뎃점도 띄운다", f("#1227·1228") == "#1227 ·1228")
    check("종결 문자는 불변",
          f("이슈 #2027, PR #114 적용(#2071)") == "이슈 #2027, PR #114 적용(#2071)")
    check("인라인 코드는 불변", f("`#1227은` 밖 #1227은") == "`#1227은` 밖 #1227 은")
    check("펜스 안은 불변",
          f("```\n#1227은\n```\n#1227은") == "```\n#1227은\n```\n#1227 은")
    check("raw anchor 불변", f("[[rec#3]] 근거") == "[[rec#3]] 근거")

    # 쓰기 경로 통합 — 저장된 본문이 이미 방어된 형태다
    r = _w(write.create_node, "태그 방어 시험", "s", "이슈 #1227은 심각했다.",
           "fable-5", space="= Scope/W1")
    check("생성 통과", r.get("ok"), r)
    stored = (ROOT / "= Scope/W1/태그 방어 시험.md").read_text(encoding="utf-8")
    check("노드 본문에 방어 반영", "#1227 은 심각했다" in stored, stored[-80:])

    # scope 기억 경로
    r2 = _w(sm.replace, "WTag", "- #1227·1228 병합 건", None, "= Scope/W1")
    check("scope 기억 방어 반영", "#1227 ·1228" in r2.get("text", ""), r2.get("text"))

    # 제목 거부문 — 원인은 지목하되 대체 표기는 처방하지 않는다(사용자 결정)
    r3 = _w(write.create_node, "PR#1 판정", "s", "본문", "fable-5",
            space="= Scope/W1")
    check("제목 # 거부 유지", r3.get("ok") is False, r3)
    v = " ".join(r3.get("violations", []))
    check("거부가 원인을 지목", "링크로 가리킬 수" in v, v)
    check("거부가 대체 표기를 처방하지 않는다", "PR-1" not in v, v)


# ── 22. 군집 개요 노드 (시행령 §3 6항 · Mechanism §6-1) ────────────────────
def test_cluster_overview():
    """각 군집은 동명 허브 노드를 두고, 전 노드가 허브에서 **참조의 방향**을
    따라 도달 가능해야 한다(헌법 3조 8항). 노드가 허브를 가리키는 역링크는
    도달을 만들지 않는다. 저작 신설의 첫 노드는 허브 강제, 이동 형성은
    검증기 보고. 활성화 전에는 보고만 한다(시행령 §11 2항·3항)."""
    from osk import validate

    # 저작 신설: 1차는 신설 관문, 2차는 첫-노드 규칙이 허브를 요구한다
    r = _w(write.create_node, "허브 아닌 첫 노드", "s", "본문", "fable-5",
           space="= Scope/OVW")
    check("신설 1차는 관문 거부", r.get("ok") is False, r)
    r2 = _w(write.create_node, "허브 아닌 첫 노드", "s", "본문", "fable-5",
            space="= Scope/OVW")
    check("신설 2차는 첫-노드 규칙 거부", r2.get("ok") is False, r2)
    v = " ".join(r2.get("violations", []))
    check("거부가 허브 노드를 지목", "허브 노드" in v and "OVW" in v, v)
    check("거부는 노드를 남기지 않는다",
          not (ROOT / "= Scope/OVW/허브 아닌 첫 노드.md").exists())
    # 허브가 갈래 머리들을 **미리** 참조한다 — 아직 없는 대상은 dangling
    # 경고일 뿐 거부가 아니다(탐색 링크는 자유).
    r3 = _w(write.create_node, "OVW", "OVW 군집 허브",
            "갈래: [[OVW-head]] · [[OVW-a]]", "fable-5", space="= Scope/OVW")
    check("동명 허브 노드는 첫 노드로 통과", r3.get("ok"), r3)

    # 방향 도달 — 허브→머리→(derived-from)→조상. 역링크·섬은 고아다.
    r4 = _w(write.create_node, "OVW-섬", "s", "아무도 가리키지 않는 섬.",
            "fable-5", space="= Scope/OVW")
    check("허브가 선 뒤 일반 노드 통과", r4.get("ok"), r4)
    r5 = _w(write.create_node, "OVW-조상", "s", "갈래의 처음.",
            "fable-5", space="= Scope/OVW")
    rh = _w(write.create_node, "OVW-head", "s", "갈래 머리.", "fable-5",
            space="= Scope/OVW", edges={"derived-from": r5["id"]})
    check("머리 생성(조상을 derived-from)", rh.get("ok"), rh)
    r6 = _w(write.create_node, "OVW-역링크", "s", "[[OVW]]만 가리킨다.",
            "fable-5", space="= Scope/OVW")
    check("역링크 노드 생성", r6.get("ok"), r6)
    # 순환 안전 — 허브에 닿는 고리(a↔b)와 닿지 않는 고리(c↔d) 둘 다 종료
    ra = _w(write.create_node, "OVW-a", "s", "[[OVW-b]]를 본다.",
            "fable-5", space="= Scope/OVW")
    rb = _w(write.create_node, "OVW-b", "s", "[[OVW-a]]를 본다.",
            "fable-5", space="= Scope/OVW")
    rc = _w(write.create_node, "OVW-c", "s", "[[OVW-d]]를 본다.",
            "fable-5", space="= Scope/OVW")
    rd = _w(write.create_node, "OVW-d", "s", "[[OVW-c]]를 본다.",
            "fable-5", space="= Scope/OVW")
    check("순환쌍 생성", all(x.get("ok") for x in (ra, rb, rc, rd)),
          (ra, rb, rc, rd))
    co = validate.cluster_overview_report(graph.Index())
    st = co.get("= Scope/OVW")
    check("검사가 군집을 본다", st is not None, co)
    check("허브 존재 인식", st and st["overview"] is True, st)
    orphans = set(st.get("orphans", []))
    check("허브→머리 직접 도달", "OVW-head" not in orphans, st)
    check("derived-from 사슬로 조상 도달", "OVW-조상" not in orphans, st)
    check("허브에 닿는 고리는 순회가 완주한다(무한루프 없음)",
          not {"OVW-a", "OVW-b"} & orphans, st)
    check("역링크만으로는 고아 — 방향이 반대다", "OVW-역링크" in orphans, st)
    check("섬은 고아", "OVW-섬" in orphans, st)
    check("닿지 않는 고리는 둘 다 고아(여기서도 종료)",
          {"OVW-c", "OVW-d"} <= orphans, st)
    check("미도달 수가 고아 목록과 일치",
          st["unreachable"] == len(orphans) == 4, st)

    # 허브 노드는 군집 밖으로 이동 불가
    r7 = _w(write.move_node, "OVW", "= Scope/W1")
    check("허브 노드 이동 거부", r7.get("ok") is False, r7)
    check("이동 거부가 결박을 설명",
          "허브 노드" in " ".join(r7.get("violations", [])), r7)

    # 활성화 게이트 — 기본 비활성(보고만), 활성화 후 verdict 산입, 해제 원복
    check("기본 비활성", validate.validator_active("cluster-overview") is False)
    rep = validate.run()
    check("비활성이면 verdict 불산입(그 이유의 fail 없음)",
          not any("군집 허브 노드" in f for d in rep["fail"] for f in d), rep["fail"])
    check("보고는 언제나 실린다", "= Scope/OVW" in rep.get("cluster_overview", {}))
    core.ledger_append(core.VALIDATORS,
                       {"kind": "activate", "rule": "cluster-overview"})
    check("활성 판정", validate.validator_active("cluster-overview") is True)
    rep2 = validate.run()
    check("활성이면 위반이 verdict에 산입", rep2["verdict"] == "FAIL"
          and any("군집 허브 노드" in f for d in rep2["fail"] for f in d), rep2["verdict"])
    core.ledger_append(core.VALIDATORS,
                       {"kind": "deactivate", "rule": "cluster-overview"})
    check("해제 원복", validate.validator_active("cluster-overview") is False)


# ── 21. 1회용 대화 id는 세션 키가 될 수 없다 (Mechanism §6-2 6항) ──────────
def test_ephemeral_session_key():
    """세션 키는 첫 성공이 **영구 결속**한다. 그래서 대화마다 새로 나는 값을
    넣으면 그 대화가 끝나는 순간 그 scope의 기억으로 돌아올 길이 사라진다.
    도구 설명이 금지하고 있었으나 설명은 강제가 아니었고, 실측으로 두 건이
    굳었다(2026-08-24). 세 쓰기 표면 모두에서 막고, **막는 자리는 파일 쓰기
    앞**이어야 한다 — 결속은 뒤에 오는 경로가 있어 뒤에서 막으면 파일만 남는다."""
    from osk import raw, scope_memory as sm
    U = "2df63e9c-b6fd-4499-8d2a-f53c4921e243"          # 실측된 그 형태
    U32 = "2df63e9cb6fd44998d2af53c4921e243"            # 하이픈 없는 UUID
    before = core.ROUTING.read_text(encoding="utf-8") if core.ROUTING.exists() else ""

    r = _w(write.create_node, "1회용 키 시험", "s", "본문", "fable-5",
           session=U, space="= Scope/W1")
    check("create_node가 UUID 세션을 거부", r.get("ok") is False, r)
    v = " ".join(r.get("violations", []))
    check("거부가 1회용 id임을 지목", "1회용 대화 id" in v, v)
    check("거부가 대안을 알려준다", "저장소 이름처럼" in v, v)
    check("거부는 노드를 남기지 않는다",
          not (ROOT / "= Scope/W1/1회용 키 시험.md").exists())

    check("하이픈 없는 UUID도 거부",
          _w(write.create_node, "1회용 키 시험2", "s", "본문", "fable-5",
             session=U32, space="= Scope/W1").get("ok") is False)

    rr = _w(raw.append_rounds, U, "rec", [{"user": "q", "agent": "a"}],
            space="= Scope/W1")
    check("append_raw도 거부", rr.get("ok") is False, rr)
    check("raw 거부는 원인을 세션 키로 지목",
          "1회용 대화 id" in " ".join(rr.get("violations", [])), rr)

    rs = _w(sm.replace, U, "- 엔트리", None, "= Scope/W1")
    check("scope_memory도 거부", rs.get("ok") is False, rs)

    after = core.ROUTING.read_text(encoding="utf-8") if core.ROUTING.exists() else ""
    check("거부는 라우팅 대장에 결속을 남기지 않는다", before == after)

    # 실제 세션 키는 그대로 통과해야 한다 — 과잉 거부는 이 관문의 실패다.
    for good in ("open-hwp", "rhwp", "aw2", "lpaiu-cs/ltm-vault",
                 "causal-spacetime", "illustratorAI"):
        check(f"실제 키 `{good}`는 통과", write.ephemeral_session_errors(good) == [])
    check("빈 세션은 이 관문이 관여하지 않는다",
          write.ephemeral_session_errors(None) == [])


# ── 19. scope 기억 — 상한이 곧 승격의 문턱 (Mechanism §9-2) ─────────────────
def test_scope_memory():
    """상한은 저장 용량의 제한이 아니라 문턱이다. 그래서 초과는 **거부**하고,
    거부는 **전문과 순서**를 함께 돌려준다 — 자동 절단·자동 요약을 두면 그
    신호가 조용히 소비되어 아무 일도 일어나지 않는다."""
    from osk import scope_memory as wm
    for n in ("WWm", "WWmB"):
        (ROOT / f"= Scope/{n}").mkdir(exist_ok=True)
    S = "repo/regr-wm"

    r = _w(wm.replace, S, "- 첫 엔트리", None, "= Scope/WWm")
    # 저장본은 개행으로 끝난다 — 길이는 저장된 전문 기준이어야 잔여 계산이 맞다
    check("최초 쓰기", r.get("ok") and r["text"].strip() == "- 첫 엔트리"
          and r["chars"] == len(r["text"]), r)
    check("잔여를 함께 낸다", r["remaining"] == wm.LIMIT - r["chars"], r)
    check("퇴출 원칙이 성공 응답에도 실린다", "지우는 것이 정상" in r["eviction"])
    # 첫 쓰기가 결속을 세운다 — 빠뜨렸더니 이후 호출이 전부 막혔다(실측)
    check("첫 쓰기가 결속을 세운다", write.resolve_session(S) == "WWm")
    check("이후에는 space 없이 읽힌다", _w(wm.read, S).get("ok") is True)

    h = r["hash"]
    check("hash 없이 덮어쓰기 거부",
          _w(wm.replace, S, "- 다른 것").get("ok") is False)
    check("틀린 hash 거부",
          _w(wm.replace, S, "- 다른 것", "sha256:00").get("ok") is False)
    check("거부해도 전문을 돌려준다",
          _w(wm.replace, S, "- 다른 것")["text"].strip() == "- 첫 엔트리")
    r2 = _w(wm.replace, S, "- 갱신됨", h)
    check("맞는 hash로 전체 치환", r2["text"].strip() == "- 갱신됨", r2)

    # 상한 초과 — 거부하고, 파일은 그대로이며, 안내가 순서를 준다
    big = chr(10).join(f"- 엔트리 {i} 길게 이어지는 내용이 계속된다" for i in range(120))
    r3 = _w(wm.replace, S, big, r2["hash"])
    v = " ".join(r3.get("violations", []))
    check("상한 초과는 거부", r3.get("ok") is False, r3)
    check("거부가 넘친 크기를 알린다", r3.get("rejected_chars", 0) > wm.LIMIT, r3)
    check("거부해도 현재 전문이 온다", r3["text"].strip() == "- 갱신됨", r3)
    check("거부는 파일을 건드리지 않는다",
          _w(wm.read, S)["text"].strip() == "- 갱신됨")
    check("안내가 순서를 준다 — 정리가 먼저",
          v.index("정리하라") < v.index("노드"), v)
    check("안내가 기존 노드 갱신을 먼저 말한다", "기존 노드에 갱신" in v, v)
    check("안내가 배선을 요구한다", "배선" in v, v)
    check("안내가 착지 scope를 알린다", "= Scope/WWm" in v, v)

    # 비밀값 필터의 새 적용 지점 — 작업 기억은 vault 안이고 에이전트가 쓴다
    r4 = _w(wm.replace, S, "- 토큰 ghp_" + "a" * 36 + " 조심",
            _w(wm.read, S)["hash"])
    check("작업 기억에도 비밀값 필터", r4.get("filtered") == ["github-token"], r4)
    check("원본이 남지 않는다", "ghp_" not in r4["text"], r4)

    # 한 세션의 작업 기억이 여러 scope로 번지지 않는다
    check("교차 scope 거부",
          _w(wm.replace, S, "- x", r4["hash"], "= Scope/WWmB").get("ok") is False)
    check("건너간 자리에 파일이 없다",
          not (ROOT / "= Scope/Workbench/_scope_memory/WWmB.md").exists())

    # 전부 비우는 것은 정상 동작이다 — 퇴출이 유실이 아니라는 계약의 실행형.
    # 다만 직전 상태가 어디에도 남지 않으므로, 크게 줄면 사라진 전문을 함께
    # 돌려줘 같은 턴 안에서 되돌릴 수 있게 한다.
    before = _w(wm.read, S)["text"]
    r5 = _w(wm.replace, S, "", r4["hash"])
    check("전부 비울 수 있다", r5.get("ok") and r5["chars"] == 0, r5)
    check("비우면 사라진 전문이 온다", r5.get("replaced_text") == before, r5)
    check("사라진 양을 알린다", "사라졌다" in r5.get("note", ""), r5)
    check("되돌리는 법을 알린다", "그대로 다시 보내라" in r5.get("note", ""), r5)
    back = _w(wm.replace, S, r5["replaced_text"], r5["hash"])
    check("돌려준 전문으로 복원된다", back.get("text") == before, back)
    # 늘어나거나 조금 줄어든 쓰기에는 붙이지 않는다 — 모든 응답에 실으면 소음이다
    grow = _w(wm.replace, S, before + chr(10) + "- 한 줄 더", back["hash"])
    check("늘어난 쓰기엔 붙지 않는다", "replaced_text" not in grow, sorted(grow))

    # 경계 — 문서가 정한 계수만큼은 실제로 담겨야 한다. 저장본의 개행을 계수에
    # 넣으면 유효 상한이 1499가 되고, 문서를 따른 호출자가 반드시 한 번 튕긴다.
    exact = _w(wm.replace, S, "가" * wm.LIMIT, _w(wm.read, S)["hash"])
    check("정확히 상한은 통과", exact.get("ok") and exact["chars"] == wm.LIMIT, exact)
    over = _w(wm.replace, S, "나" * (wm.LIMIT + 1), exact["hash"])
    check("상한+1은 거부", over.get("ok") is False, over)
    check("거부가 보낸 글자수를 센다",
          over.get("rejected_chars") == wm.LIMIT + 1, over)

    # 정규화 — 없으면 같은 글이 기기에 따라 두 배로 세어져 상한이 기기 의존이 된다
    nfd = _w(wm.replace, S, "가" * 10, _w(wm.read, S)["hash"])
    check("NFD 자모는 NFC로 접혀 세어진다", nfd.get("chars") == 10, nfd)

    # frontmatter로 오독되면 검증기가 통째로 FAIL한다 — 통로에서 막는다
    dash = _w(wm.replace, S, "---" + chr(10) + "본문", _w(wm.read, S)["hash"])
    check("`---` 선두 거부", dash.get("ok") is False, dash)
    check("거부 뒤에도 배치 검증기는 깨끗하다", not graph.layout_violations())

    # 승격 지시가 **근거가 무엇인지**까지 알려야 한다. 말해주지 않으면 코드를
    # 못 보는 호출자는 (2)에서 멈춘다(감사에서 실측). 근거는 그 지식이 나온
    # 곳이며 — 작업 기억은 경유지이지 원료가 아니다(헌법 9조 1항 "원료가 된",
    # Workbench 계약 4.2 "작업 상태는 근거로 참조하지 않는다").
    big2 = "다" * (wm.LIMIT + 100)
    ov = " ".join(_w(wm.replace, S, big2, _w(wm.read, S)["hash"])
                  .get("violations", []))
    # 근거를 **무조건** 요구하면 승격마다 대화를 raw에 남기게 되고, 승격은
    # 10턴마다 압박이 걸리므로 결국 전량 포착으로 우회 복귀한다. 규범도 그렇게
    # 읽히지 않는다 — 의무는 증류에 붙고 증류에는 원료가 전제된다(헌법 9조 1항).
    check("근거를 조건부로 요구한다", "근거가 있으면" in ov, ov)
    check("원료가 없을 때 무엇을 할지 알려준다",
          "원료 없이" in ov and "본문에" in ov, ov)
    check("기존 노드의 id를 첫 선택지로 든다", "`id`" in ov, ov)
    check("raw는 다툴 만한 주장일 때로 한정한다", "다툴 만한" in ov, ov)
    # 감사 실증: 이 안내가 없어 감사자가 검색 없이 새 노드를 만들었고, 사후
    # 검색에서 인접 주제의 기존 노드를 발견했다 — 중복 금지(헌법 3조 7항).
    check("승격 전에 기존 노드 탐색을 지시한다", "`search`" in ov, ov)
    check("작업 기억을 근거로 오인하지 않게 한다",
          "경유지" in ov and "가리킬 수 없다" not in ov, ov)

    # 착지 거부에도 전문·잔여가 실린다 (§9-2 5항)
    xs = _w(wm.replace, S, "- x", _w(wm.read, S)["hash"], "= Scope/WWmB")
    check("교차 scope 거부에도 전문이 온다",
          "text" in xs and "remaining" in xs and "eviction" in xs, sorted(xs))

    # 이 계층은 git을 부르지 않는다 — 동기화 계약은 데몬이 소유한다
    src = (Path(wm.__file__).read_text(encoding="utf-8"))
    check("wm은 vault_sync를 부르지 않는다",
          "vault_sync" not in src and "commit_push" not in src)
    check("응답에 sync 키가 없다", "sync" not in _w(wm.read, S))


# ── 19a. 작업 상태를 근거로 걸면 계약 4.2로 진단한다 ──────────────────────
def test_workbench_state_not_evidence():
    """작업 기억을 `derived-from`으로 걸면 "scope 간 직접 참조"로 진단되던 것을
    바로잡는다. 그것은 위상 문제가 아니다 — 어느 scope에서 걸어도 계약이
    금지한다(Workbench 계약 4.2). 위상으로 진단하면 받는 쪽은 scope를 바꿔
    보려 하고, 그 길은 없다. 감사가 잡은 `space` 오진과 같은 계열이다."""
    from osk import scope_memory as wm
    (ROOT / "= Scope/WEv").mkdir(exist_ok=True)
    idx = ROOT / "= Scope/WEv/WEv.md"     # 첫-노드 규칙 충족용 허브 골격
    if not idx.exists():
        idx.write_text(node_text("260802-zzzz-wevi", "WEv 허브"),
                       encoding="utf-8")
    _w(wm.replace, "repo/regr-ev", "- 경유지 내용", None, "= Scope/WEv")
    r = _w(write.create_node, "regr-ev-node", "근거 오지정 시험", "본문",
           "fable-5", space="= Scope/WEv",
           edges={"derived-from": "[[= Scope/Workbench/_scope_memory/WEv]]"})
    v = " ".join(r.get("violations", []))
    check("작업 상태 근거는 거부", r.get("ok") is False, r)
    check("위상이 아니라 계약 4.2로 진단한다",
          "작업 상태는 근거로 쓰지 않는다" in v and "scope 간 직접 참조" not in v, v)
    check("근거가 무엇인지 함께 말한다", "그 지식이 나온 곳" in v, v)


# ── 19b. 작업 기억 훅 경로 — `show`는 전문 그대로 낸다 ──────────────────────
def test_scope_memory_cli():
    """훅이 이 출력을 문맥에 그대로 넣으므로 감싸는 껍데기가 있으면 훅마다
    벗기는 코드를 쓰게 된다. 결속이 없으면 빈 출력이고, 그것은 오류가 아니다."""
    from osk import cli
    from osk import scope_memory as wm
    import io, types
    (ROOT / "= Scope/WWmCli").mkdir(exist_ok=True)
    S = "repo/regr-wm-cli"

    def run(argv, stdin=None):
        out, buf = {}, io.BytesIO()
        real_emit, real_in, real_out = cli._emit, sys.stdin, sys.stdout
        try:
            cli._emit = out.update
            sys.stdout = types.SimpleNamespace(buffer=buf)
            if stdin is not None:
                sys.stdin = types.SimpleNamespace(
                    isatty=lambda: False, buffer=io.BytesIO(stdin.encode("utf-8")))
            try:
                cli.main(argv)
            except SystemExit as e:
                out["exit"] = e.code
        finally:
            cli._emit, sys.stdin, sys.stdout = real_emit, real_in, real_out
        return out, buf.getvalue().decode("utf-8")

    _, plain = run(["sm", "show", "--session", S, "--space", "= Scope/WWmCli"])
    check("결속 전 show는 빈 출력", plain == "", repr(plain))

    # 계약 §3.4가 말하는 훅의 첫 호출 — 결속도 space도 없는 새 저장소.
    # 여기서 위반 JSON이 나오면 훅이 그것을 문맥에 넣거나 종료코드를 보고
    # 조용히 건너뛴다. 둘 다 나쁘므로 빈 출력·종료코드 0이어야 한다.
    out0, plain0 = run(["sm", "show", "--session", "repo/never-bound"])
    check("결속 없는 show는 빈 출력", plain0 == "", repr(plain0))
    check("결속 없는 show는 종료코드 0", out0.get("exit") in (None, 0), out0)

    out, _ = run(["sm", "write", "--session", S, "--space", "= Scope/WWmCli"],
                 "- 훅으로 쓴 엔트리")
    check("CLI 쓰기", out.get("ok") is True, out)

    _, plain = run(["sm", "show", "--session", S])
    check("show는 JSON이 아니라 전문 그대로",
          plain.strip() == "- 훅으로 쓴 엔트리" and not plain.lstrip().startswith("{"),
          repr(plain))
    out, _ = run(["sm", "show", "--session", S, "--json"])
    check("--json은 상태 전체",
          out.get("text", "").strip() == "- 훅으로 쓴 엔트리"
          and out.get("chars") == len(out["text"]), out)

    big = chr(10).join(f"- 엔트리 {i} 길게 이어지는 내용" for i in range(150))
    out, _ = run(["sm", "write", "--session", S,
                  "--expect-hash", out["hash"]], big)
    check("CLI 상한 초과는 종료코드 0이 아니다", out.get("exit") == 1, out)
    check("CLI 거부도 전문을 돌려준다",
          out.get("text", "").strip() == "- 훅으로 쓴 엔트리", out)


# ── 20. 군집 신설의 2단계 확인 (Mechanism §6-2 3항) ─────────────────────
def test_new_cluster_two_phase():
    """구판은 "군집 신설은 사용자 발의다"라며 전면 거부했으나 그 문구는 규범
    무근거였다 — 헌법은 형성의 자동화를 기본으로 둔다(5조 4항·6조 9항).
    현행 관문은 한 번 묻는다: 1차 거부가 신설임을 알리고, 같은 군집에 대한
    재시도는 통과한다. 선의의 에이전트가 오류 원인을 읽고 사용자 허락을
    확인한 뒤 같은 요청을 다시 보내는 한 왕복이 전부다."""
    import time as _time
    r = _w(write.create_node, "이상 신설 시험", "s", "본문", "fable-5",
           space="= Scope/W2P")
    check("새 군집 1차는 거부", r.get("ok") is False, r)
    v = " ".join(r.get("violations", []))
    check("거부가 신설임을 알린다", "새 군집" in v, v)
    check("거부가 재시도 절차를 알려준다", "그대로 다시" in v, v)
    check("1차 거부는 디렉토리를 만들지 않는다",
          not (ROOT / "= Scope/W2P").is_dir())
    # 재시도는 관문을 지나되, 빈 군집의 첫 노드는 허브가어야 한다(§3 6항) —
    # 두 관문은 겹치지 않고 이어진다: 신설 확인 → 허브 먼저.
    r2 = _w(write.create_node, "이상 신설 시험", "s", "본문", "fable-5",
            space="= Scope/W2P")
    check("재시도는 관문을 지나 첫-노드 규칙에 닿는다",
          r2.get("ok") is False
          and "허브 노드" in " ".join(r2["violations"]), r2)
    check("관문 통과로 군집 디렉토리는 만들어졌다",
          (ROOT / "= Scope/W2P").is_dir())
    r2b = _w(write.create_node, "W2P", "W2P 허브", "[[이상 신설 시험]]",
             "fable-5", space="= Scope/W2P")
    check("허브 노드가 첫 노드로 통과", r2b.get("ok"), r2b)
    r3 = _w(write.create_node, "이상 신설 시험", "s", "본문", "fable-5",
            space="= Scope/W2P")
    check("허브가 선 군집은 관문 없이 통과", r3.get("ok"), r3)
    r4 = _w(write.move_node, "이상 신설 시험", "= Domain/D2P")
    check("move의 새 군집도 1차 거부", r4.get("ok") is False, r4)
    r5 = _w(write.move_node, "이상 신설 시험", "= Domain/D2P")
    check("move 재시도는 통과 — 이동 형성은 허브 강제가 없다(§3 1항 보호)",
          r5.get("ok"), r5)
    r6 = _w(write.create_node, "깊은 경로", "s", "본문", "fable-5",
            space="= Scope/W2P/sub")
    check("Space 루트 아래가 아니면 즉시 거부",
          r6.get("ok") is False
          and "Space 루트 바로 아래" in " ".join(r6["violations"]), r6)
    r7 = _w(write.create_node, "예약명 군집", "s", "본문", "fable-5",
            space="= Scope/COM1")
    check("군집 이름도 이식성 규칙을 받는다",
          r7.get("ok") is False and "예약 장치명" in " ".join(r7["violations"]), r7)
    check("부적격 신설은 디렉토리를 남기지 않는다",
          not (ROOT / "= Scope/COM1").is_dir())
    # 만료 — 잊힌 표식이 뒷날의 다른 요청을 무확인 통과시키지 않는다
    write._ack_file().write_text(
        json.dumps({"= Scope/WStale2P": _time.time() - 7200}), encoding="utf-8")
    r8 = _w(write.create_node, "WStale2P", "허브", "본문", "fable-5",
            space="= Scope/WStale2P")
    check("만료된 표식은 다시 1차 거부", r8.get("ok") is False, r8)
    r9 = _w(write.create_node, "WStale2P", "허브", "본문", "fable-5",
            space="= Scope/WStale2P")
    check("만료 후 새 왕복은 통과(허브 제목이라 첫-노드 규칙도 충족)",
          r9.get("ok"), r9)


if __name__ == "__main__":
    for fn in [test_posix_rel_is_os_independent, test_portable_title,
               test_cli_delegation, test_rid_monotone, test_same_ms_chain_signed,
               test_fork_failclosed_and_reseal, test_anchor_no_order_fallback,
               test_cycle_normalization, test_structural_damage,
               test_ridless_unsign_not_swallowed, test_root_confinement_and_kst,
               test_approval_lifecycle,
               test_path_reuse, test_fingerprint_move,
               test_sync, test_conflicts_semantics,
               test_ledger_corruption_resilience, test_ledger_schema_segment,
               test_validate_global_invariance, test_authority_hold,
               test_self_referencing_edge, test_surface_contract,
               test_ledger_row_shape,
               test_broken_delegation_isolated, test_write_contract,
               test_write_cas_body_bound, test_edge_single_list_roundtrip,
               test_write_move_and_pin,
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
               test_sync_pins_main, test_daemon_no_bare_git_spawn,
               test_publish_manifest, test_publish_guards,
               test_conflict_candidates,
               test_release_and_update,
               test_store_digest_confined, test_delegation_protection_scope,
               test_approve_requires_expect_work,
               test_approval_baseline_blobs_present,
               test_store_content_verified,
               test_revert_incomplete_no_record,
               test_approve_precondition_under_lock,
               test_unprotect_precondition_under_lock,
               test_protect_precondition_rejects_stale,
               test_protect_concurrent_write_becomes_pending,
               test_revert_confirms_before_destroying,
               test_revert_recreates_deleted_region,
               test_revert_binds_reviewed_changeset,
               test_move_recorded_and_reverted_in,
               test_move_reverted_out_no_duplicate,
               test_move_return_blocked_origin_occupied,
               test_move_chain_reverted_no_duplicate,
               test_move_lifecycle_cutoff, test_move_reentry_single_plan,
               test_move_cutoff_causal_not_clock,
               test_move_phantom_tail_row_harmless,
               test_move_unrecorded_outside_protection,
               test_changeset_lists_difference, test_stale_sealed_by_approve,
               test_topology_rejects_wiki_node_derived_from,
               test_local_lock_path_git_shapes,
               test_revert_structure_conflict_no_partial,
               test_approval_serialized_with_writes,
               test_region_replaced_by_file_is_pending,
               test_refused_revert_leaves_region_absent,
               test_stale_region_not_unprotected,
               test_nested_regions_all_checked,
               test_baseline_bound_to_region,
               test_baseline_pass, test_raw_append, test_raw_cli_path,
               test_raw_read, test_raw_space_misdiagnosis,
               test_raw_binding_confines_scope, test_raw_replay_rejected,
               test_scope_memory, test_workbench_state_not_evidence,
               test_scope_memory_cli, test_new_cluster_two_phase,
               test_ephemeral_session_key, test_cluster_overview,
               test_obsidian_tag_defense, test_index_node_not_delegation]:
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
