"""회귀 수트의 한 갈래: 검증기와 표면의 계약 — 수트, 표면 린트, 운용 안내문, 발행 스키마.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, subprocess, sys, tempfile
from pathlib import Path
from unittest import mock

from regression.support import check, ENGINE, MINI, node_text, rmtree_force, ROOT, skip, _unhang, VAULT, _vault_at, _w
from osk import cli, validate, contract, write, publish  # noqa: E402


# ── 12. validate.run은 전역을 오염시키지 않는다 ─────────────────────────
def test_validate_global_invariance():
    """검증기는 모듈을 다시 읽지 않고 환경을 바꾸지 않으며, 받은 저장소를 어느 모듈에도 전역으로 남기지
    않는다. 전에는 `core`의 저장소 상수가 재대입되지 않는지를 봤다 — 그 상수는 없어졌고, 남은 위험은
    누군가 저장소를 모듈 전역에 쥐는 것이다(구조 시험이 못 보는 모양도 여기서는 값으로 드러난다)."""
    from osk.vault import Vault

    def state():
        held = sorted(f"{name}.{attr}" for name, module in list(sys.modules.items())
                      if module is not None and (name == "osk" or name.startswith("osk.")
                                                 or name in ("mcp_server", "sync_daemon", "vault_sync"))
                      for attr, value in list(vars(module).items()) if isinstance(value, Vault))
        return (held, id(sys.modules["osk.core"]), id(sys.modules["osk.signatures"]),
                os.environ.get("OSK_VAULT_ROOT"))
    before = state()
    validate.run(VAULT, cli.published_tools)
    after = state()
    check("validate.run 전후 전역 불변(재대입·reload 없음)", before == after,
          (before, after))
    check("엔진의 어느 모듈도 저장소를 전역으로 쥐지 않는다", not after[0], after[0])


def test_baseline_pass():
    from osk import approvals, approval_acts
    # 보호영역 하나를 지정하고 clean 상태에서 검증기가 통과하는지 본다
    reg = "00_Scope/W1"
    if not approvals.is_protected(VAULT, reg):
        approval_acts.protect(VAULT, reg, "기준선")
    rep = validate.run(VAULT, cli.published_tools)
    check("정상 vault 기준선 PASS", rep["verdict"] == "PASS", rep["fail"])
    check("보호영역 현황이 clean으로 판정",
          rep.get("protected_regions", {}).get(reg) == "clean",
          rep.get("protected_regions"))
    # clean↔pending 불변식을 실제로 소진한다 — 항진명제가 아니게 한다
    junk = ROOT / "00_Scope/W1/regr-baseline-junk.md"
    junk.write_text(node_text("260802-zzzz-rgba", "지문 변경"), encoding="utf-8")
    try:
        check("영역 내 쓰기가 pending으로 전이", approvals.state(VAULT, reg) == "pending",
              approvals.state(VAULT, reg))
        rep2 = validate.run(VAULT, cli.published_tools)
        check("검증기 리포트도 pending을 싣는다",
              rep2.get("protected_regions", {}).get(reg) == "pending",
              rep2.get("protected_regions"))
    finally:
        junk.unlink(missing_ok=True)
    check("쓰기 회수 후 clean 복귀", approvals.state(VAULT, reg) == "clean")
    approval_acts.unprotect(VAULT, reg, "기준선 정리")   # 잔여 없이 되돌린다


# ── 14b. 자기 참조 PE는 계약 위반 (상태 자체를 불허) ──────────────────
def test_self_referencing_edge():
    p = ROOT / "00_Scope/W1/regr-selfref.md"
    try:
        # 경로형·id형 어느 표기든 자기 참조는 적발한다(derived-from은 id로도)
        for pred, tgt in (("conflicts", "[[00_Scope/W1/regr-selfref]]"),
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
    # §9(비밀값 패턴 표)도 함께 심는다 — 검증기가 구현과 조문의 동치를 보는데,
    # 그 정본이 없으면 이 vault를 쓰는 뒤 시나리오(발행 가드·릴리스 전제)까지
    # 통째로 "정본 부재"로 FAIL한다. 규범이 정본인 검사가 늘면 심는 범위도 는다.
    sec9 = _re.search(r"^## §9 .*?(?=^## §9-2)", real, _re.M | _re.S)
    check("실 Mechanism에 §6-2가 있다", sec is not None)
    check("실 Mechanism에 §9가 있다", sec9 is not None)
    if not sec or not sec9:
        return
    (gov / "Mechanism.md").write_text(          # 특수 노드 — 계약을 갖춘다
        node_text("260802-zzzz-rg50", "표면 계약 시험용",
                  sec.group(0) + "\n" + sec9.group(0)),
        encoding="utf-8")
    check("실 표면은 선언과 동치·권위 비노출", not validate.surface_violations(VAULT),
          validate.surface_violations(VAULT))
    declared = validate.declared_tools(VAULT)
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
         '    return ledger_append(_vault().pins, {})\n\n\n',
         "mcp_server.py", "@mcp.tool()", "권위 대장에 기록"),
        ('def sneak(vault):\n    return ledger_append(vault.signatures, {})\n\n\n',
         "osk/write.py", "def create_node", "권위 대장에 기록"),
        ('def sneak_raw(vault):\n    return ledger_append(vault.pins, {})\n\n\n',
         "osk/raw.py", "def append_rounds", "권위 대장에 기록"),
        ('def sneak_roots(vault):\n    return ledger_append(vault.roots, {})\n\n\n',
         "osk/routing.py", "def bind_session", "권위 대장에 기록"),
        # 표면은 `approvals`를 import하지 않는다 — 닿는다면 다른 모듈을 거쳐서다
        ('@mcp.tool()\ndef approve_it() -> dict:\n'
         '    return ledger_append(write.approvals.ledger_path(_vault()), {})\n\n\n',
         "mcp_server.py", "@mcp.tool()", "권위 대장에 기록"),
        # 승인 기록부는 저장소의 속성이 아니라 `approvals`의 함수가 자리를 낸다
        ('def sneak_approval(vault):\n    return ledger_append(approvals.ledger_path(vault), {})\n\n\n',
         "osk/write.py", "def create_node", "권위 대장에 기록"),
        # 표면 모듈에서 갈라져 나온 모듈도 같은 통로다 — 파일마다 하나씩 심는다. `SURFACE_MODULES`에서
        # 그 파일이 빠지면 사본에 파일이 없어 여기서 깨진다(목록이 분해를 따라가는지는 이 줄들이 지킨다).
        *(('def sneak_split(vault):\n    return ledger_append(vault.pins, {})\n\n\n', rel, anchor, "권위 대장에 기록")
          for rel, anchor in (("osk/nodefile.py", "def render"), ("osk/cluster_gate.py", "def cluster_names"),
                              ("osk/node_checks.py", "def check_edges"), ("osk/node_moves.py", "def move_nodes"),
                              ("osk/conversation_state.py", "def save_state"),
                              ("osk/conversation_capture.py", "def capture"),
                              ("osk/conversation_review.py", "def acknowledge"),
                              ("osk/conversation_cite.py", "def cite"))),
    ):
        with tempfile.TemporaryDirectory() as td:
            eng = Path(td) / "_engine"
            (eng / "osk").mkdir(parents=True)
            for rel, src in srcs.items():
                (eng / rel).write_text(src, encoding="utf-8")
            f = eng / into
            f.write_text(f.read_text(encoding="utf-8").replace(
                anchor, inject + anchor, 1), encoding="utf-8")
            errs = validate.surface_violations(VAULT, eng)
            check(f"표면 드리프트 적발: {into} — {want}",
                  any(want in e for e in errs), errs)
    check("시험이 엔진 원본을 건드리지 않았다",
          (ENGINE / "mcp_server.py").read_text(encoding="utf-8") == real
          and not validate.surface_violations(VAULT))


def test_mcp_hardening_fail_closed():
    """미지 인자 거부는 FastMCP 내부에 기댄다 — 그 자리가 없는 mcp 판에서
    조용히 건너뛰면 오타 인자가 버려진 채 나머지만 적용된다. 기동에서 죽어야 한다."""
    import mcp_server as M
    mgr = M.mcp._tool_manager
    check("현 판에서는 전 도구가 미지 인자를 거부한다",
          all(t.parameters.get("additionalProperties") is False
              for t in mgr._tools.values()))
    for label, patch in (
            ("도구 관리자 부재", lambda: mock.patch.object(M.mcp, "_tool_manager", None)),
            ("도구 목록 부재", lambda: mock.patch.object(mgr, "_tools", {})),
            ("인자 모델 부재", lambda: mock.patch.object(
                next(iter(mgr._tools.values())), "fn_metadata", None))):
        with patch():
            try:
                M._apply_prune()
                raised = None
            except RuntimeError as e:
                raised = e
        check(f"FastMCP 내부 변화는 기동 실패: {label}", raised is not None)


# ── 14h. 표면 스모크 — 선언된 도구 전부를 **직접 호출**한다 (7차 치명) ────
def test_surface_smoke():
    """AST 검사와 fingerprint 호출만으로는 표면 껍데기가 죽어 있어도 통과한다.
    실제로 불러 봐야 이름 가림 계열이 잡힌다."""
    import importlib
    import mcp_server as M
    importlib.reload(M)
    check("모듈 전역 search가 도구 함수에 가려지지 않았다",
          hasattr(M.search_mod, "Searcher"), type(M.search_mod).__name__)

    (ROOT / "00_Scope/WSmoke").mkdir(exist_ok=True)   # 앞선 시험이 pin한 군집 회피
    (ROOT / "00_Scope/WSmoke/WSmoke.md").write_text(
        node_text("260802-zzzz-rgs0", "스모크 허브"), encoding="utf-8")
    # move_cluster가 옮길 하위 군집과 받을 자리 — 둘 다 허브가 있어야 군집이다.
    for nm in ("WSmoke2", "WSmoke3"):
        (ROOT / f"00_Scope/W1/{nm}").mkdir(exist_ok=True)
        (ROOT / f"00_Scope/W1/{nm}/{nm}.md").write_text(
            node_text(f"260802-zzzz-rg{nm[-1]}1", f"{nm} 허브"), encoding="utf-8")
    node = ROOT / "00_Scope/W1/regr-smoke.md"
    node.write_text(node_text("260802-zzzz-rg60", "스모크"), encoding="utf-8")
    # cite_round은 추적 중인 대화의 원본 턴을 인용한다 — 대화 하나를 추적해 둔다.
    from osk import conversation_capture
    talk = Path(tempfile.mkdtemp(prefix="osk-regr-talk-")) / "regr-smoke-talk.jsonl"
    talk.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (
        {"type": "user", "sessionId": "regr-smoke-talk", "uuid": "u1",
         "message": {"role": "user", "content": "질문"}},
        {"type": "assistant", "sessionId": "regr-smoke-talk", "uuid": "a1",
         "message": {"role": "assistant", "id": "m1", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "응답"}]}})), encoding="utf-8")
    tracked = conversation_capture.capture(VAULT, "claude", "regr-smoke-talk", str(talk), "repo/smoke-raw", "00_Scope/W1")
    check("스모크 대화가 추적된다", tracked.get("ok"), tracked)
    calls = {
        "overview": lambda: M.overview("repo/smoke"),
        "search": lambda: M.search("스모크", 3),
        "read_node": lambda: M.read_node("regr-smoke"),
        "run_validators": lambda: M.run_validators(),
        "create_node": lambda: M.create_node("regr-smoke2", "스모크2", "본문",
                                             "fable-5", space="00_Scope/W1"),
        "update_node": lambda: M.update_node("regr-smoke2", summary="고침"),
        "move_nodes": lambda: M.move_nodes(["regr-smoke2"], "00_Scope/WSmoke"),
        "move_cluster": lambda: M.move_cluster("WSmoke2", "00_Scope/W1/WSmoke3"),
        "record_candidate": lambda: M.record_candidate(
            "duplication", ["regr-smoke", "regr-smoke2"], "스모크"),
        "cite_round": lambda: M.cite_round("claude/regr-smoke-talk", quote="질문"),
        "read_cited": lambda: M.read_cited(space="00_Scope/W1"),
        "scope_memory": lambda: M.scope_memory("repo/smoke-wm",
                                                   space="00_Scope/W1"),
    }
    declared = set(validate.declared_tools(VAULT) or [])
    check("스모크가 선언된 도구를 전부 부른다",
          declared == set(calls), sorted(declared ^ set(calls)))
    for name, fn in calls.items():
        try:
            out = fn()
            dead = isinstance(out, dict) and out.get("ok") is False
            check(f"표면 도구 살아 있음: {name}", not dead, out)
        except Exception as e:
            check(f"표면 도구 살아 있음: {name}", False, f"{type(e).__name__}: {e}")
    # 생성이 W1 허브에 건 줄은 scope를 건넌 이동 뒤 scope 간 직접 참조로 남는다(헌법 8조 3항).
    # 이동 응답의 hub_links 안내대로 뺀다.
    check("이동 뒤 출발지 허브의 줄을 뺀다", _unhang("W1", "regr-smoke2").get("ok"))
    r = M.read_node("regr-smoke")
    check("read_node가 hash를 준다(CAS 입력)", r.get("hash", "").startswith("sha256:"))
    check("read_node 경로는 POSIX 표기", "\\" not in r.get("path", ""), r.get("path"))
    check("read_node에 폐지된 signed 필드가 없다", "signed" not in r, r)
    hits = M.search("스모크", 5)
    check("search 결과에 폐지된 signed 필드가 없다",
          all("signed" not in h for h in hits), hits)
    check("search 경로도 POSIX 표기",
          all("\\" not in h.get("path", "") for h in hits), hits)


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
    from osk import evictions as ev
    sources = [ev.record_evict(VAULT, "W1", "regr-tx", text)["rid"]
               for text in ("본문", "새 본문")]

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

                created = {"title": "regr-tx", "summary": "전송", "body": "본문",
                           "drafter": "fable-5", "space": "00_Scope/W1",
                           "edges": {"derived-from": "regr-tx-t"}, "settle": sources[0]}
                # 새 서버 프로세스는 search 전의 생성을 거부한다(Mechanism §6-2 10항)
                out["unsearched"] = await call("create_node", created)
                await s.call_tool("search", {"query": "regr-tx", "k": 3})
                out["create"] = await call("create_node", created)
                out["read"] = await call("read_node", {"name": "regr-tx"})
                out["stale"] = await call("update_node", {
                    "name": "regr-tx", "body": "새 본문",
                    "expect_hash": "sha256:틀림", "settle": sources[1]})
                out["retry"] = await call("update_node", {
                    "name": "regr-tx", "body": "새 본문",
                    "expect_hash": out["read"]["hash"], "settle": sources[1]})
                out["preserved"] = await call("read_node", {"name": "regr-tx"})
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
          o["names"] == sorted(validate.declared_tools(VAULT) or []), o["names"])
    check("전송: search 전의 create_node는 새 서버 프로세스에서 거부된다",
          o["unsearched"].get("ok") is False and "search" in str(o["unsearched"].get("violations")),
          o["unsearched"])
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
    check("전송: 생성·갱신 모두 보존 원본을 받아 저장 후 처분한다",
          all(o[key].get("settlement", {}).get("state") == "recorded"
              and o[key]["settlement"].get("of") == rid
              and o[key]["settlement"].get("outcome") == outcome
              for key, rid, outcome in (("create", sources[0], "node"),
                                        ("retry", sources[1], "merged")))
          and "새 본문" in o["preserved"].get("body", ""), o["retry"])
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
    (ROOT / "00_Scope/W1/regr-tx.md").unlink(missing_ok=True)


# ── 14s. 표면 왕복 — search가 준 이름을 나머지 도구가 받는가 (8차 차단 ③) ──
def test_surface_name_roundtrip():
    """list_nodes를 없앤 설계에서 search는 이름을 얻는 유일한 통로다. 그 이름이
    그대로 쓰이지 않으면 발견과 지목 사이가 끊어진다."""
    import mcp_server as M
    r = _w(write.create_node, VAULT, "regr-rt-name", "왕복", "본문 내용",
           "fable-5", space="00_Scope/W1")
    check("전제: 생성", r["ok"], r)
    hits = [h for h in M.search("왕복", 8) if h["path"].endswith("regr-rt-name.md")]
    check("search가 찾는다", len(hits) == 1, hits)
    h = hits[0]
    check("title은 노드 이름 그대로다(변조 없음)",
          h["title"] == "regr-rt-name", h["title"])
    check("그 title로 read_node가 된다",
          "error" not in M.read_node(h["title"]), M.read_node(h["title"]))
    check("그 title로 update_node가 된다",
          _w(write.update_node, VAULT, h["title"], summary="갱신")["ok"])
    # derived-from의 노드 근거는 id다 — 그 id도 같은 검색 결과가 함께 준다.
    # (발견→지목이 끊어지지 않는 것은 title이 아니라 이 id가 담보한다)
    check("검색 결과가 id도 함께 준다", bool(h.get("id")), h)
    check("그 id를 엣지 대상으로 쓰면 dangling이 아니다",
          not _w(write.create_node, VAULT, "regr-rt-ref", "참조", "본문",
                 "fable-5", space="00_Scope/W1",
                 edges={"derived-from": h["id"]})["dangling"])
    for nm in ("regr-rt-name", "regr-rt-ref"):
        (ROOT / f"00_Scope/W1/{nm}.md").unlink(missing_ok=True)


# ── 14v. 표면 린트 — 스키마 건전·가르침의 회귀를 잡는다 (10차 ④) ────────
def test_surface_lint():
    check("실 표면이 린트를 통과", not validate.surface_lint(VAULT, cli.published_tools),
          validate.surface_lint(VAULT, cli.published_tools))
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
          _w(write.create_node, VAULT, "regr-ov", "조망", "본문", "fable-5",
             space=o["clusters"][0])["ok"], o["clusters"][:3])
    check("engine_rev를 준다", bool(o.get("engine_rev")), o)
    check("열린 사건 목록을 준다", isinstance(o.get("open_cases"), list), o)
    o2 = M.overview("repo/ov")
    check("session을 주면 결속을 함께 준다", "session_scope" in o2, o2)
    check("결속이 없으면 허브 트리를 싣지 않는다", "hub_tree" not in o2, o2)
    r = _w(write.create_node, VAULT, "regr-ov-b", "조망", "본문", "fable-5",
           session="regr-ov", space="00_Scope/W1")
    o3 = M.overview("regr-ov")
    check("결속은 있고 주된 scope가 없으면 결속만 싣는다 (v5.2)",
          r.get("ok") and o3.get("bindings", {}).get("scopes") == ["W1"]
          and o3.get("session_scope") is None and "hub_tree" not in o3, (r, o3.get("bindings")))
    write.set_primary(VAULT, "regr-ov", "W1", "시험 지정")
    o3 = M.overview("regr-ov")
    check("결속된 session이면 그 scope의 허브 트리를 준다(시행령 §3 8항)",
          r.get("ok") and o3.get("session_scope") == "W1"
          and o3.get("hub_tree", "").startswith("- 00_Scope/W1 · 직속 노드 "),
          (r, o3.get("hub_tree")))
    (ROOT / "00_Scope/W1/regr-ov-b.md").unlink(missing_ok=True)
    for c in o["clusters"]:
        (ROOT / c / "regr-ov.md").unlink(missing_ok=True)


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


# ── 18b. CLI 출력은 UTF-8 바이트다 — 콘솔 코드페이지에 인질이 되지 않는다 ──
def test_cli_emit_utf8():
    from osk import cli
    import io, types
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


# ── 개정 2026-09-02: 비밀값 경계와 영역 tree 제외 (#29·#37) ───────────────

def test_setup_doc_drift():
    """운용 안내문이 규범·파서와 갈라지면 검증기가 말한다 (#42).

    감사 2026-09-02이 찾은 문서 표류 한 무리는 조용했다 — 검증기가 문서를 보지
    않으니 감사로만 드러나고, 그 사이 새 세션이 안내문을 먼저 읽고 시작한다.
    §9 1항 패턴표↔`secrets.PATTERNS` 대조와 같은 규율을 안내문에 건다.

    무엇을 망가뜨리면 실패하는가:
      · 대조 다섯(빠진 도구·없는 도구·개수·빠진 명령·없어진 명령) 중 하나를
        지우면 그 단언이 실패한다
      · 명령 집합을 파서가 아니라 소스 정규식에서 만들면 `update`·`release`가
        빠져 ④가 실패한다(구판이 그랬다)
      · 실 안내문이 규범·파서와 갈라지면 마지막 단언이 실패한다
    """
    lab = Path(tempfile.mkdtemp(prefix="osk-setupdoc-"))
    try:
        (lab / "_governance").mkdir()
        (lab / "docs").mkdir()
        tools = ["overview", "search", "read_node", "scope_memory"]
        (lab / "_governance/Mechanism.md").write_bytes(
            ("## §6-2 외부 표면" + chr(10) * 2 + "```" + chr(10)
             + " ".join(tools) + chr(10) + "```" + chr(10)).encode("utf-8"))

        #    표의 행은 **파서에서** 만든다 — 여기에 손으로 적으면 그 목록이
        #    또 하나의 표류 원천이 된다(검사가 막으려는 바로 그것이다).
        _all, _leaves = validate.cli_commands()
        check("파서에서 만든 명령 집합이 위임 명령을 담는다 (#42)",
              {"update", "release"} <= _leaves, sorted(_leaves))
        rows_of = lambda names: chr(10).join(
            f"| `{c}` | 설명 |" for c in sorted(names))

        def _setup(tool_line, count_word, cli_rows):
            body = ("## MCP 서버" + chr(10) * 2
                    + f"도구는 {count_word}이며 그 목록의 정본은 "
                    + "Mechanism §6-2 7항이다 —" + chr(10)
                    + tool_line + chr(10) * 2
                    + "| 명령 | 하는 일 |" + chr(10)
                    + "|---|---|" + chr(10)
                    + cli_rows + chr(10) * 2 + "끝." + chr(10))
            (lab / "docs/SETUP.md").write_bytes(body.encode("utf-8"))

        def _drift():
            return validate.setup_doc_drift(_vault_at(lab))

        rows = rows_of(_leaves)
        full = "`overview` `search` `read_node` `scope_memory`."

        #    ① 규범에 있는 도구가 안내문에 없다
        _setup("`overview` `search` `read_node`.", "넷", rows)
        e = _drift()
        check("빠진 도구를 안내문 대조가 잡는다 (#42)",
              any("도구 목록에 없다: scope_memory" in x for x in e), e)

        #    ② 안내문이 규범에 없는(구판) 도구를 적는다
        _setup(full[:-1] + " `move_node`.", "넷", rows)
        e = _drift()
        check("구판 도구 이름을 안내문 대조가 잡는다 (#42)",
              any("규범에 없는 도구를 적는다: move_node" in x for x in e), e)

        #    ③ 개수 표기가 목록과 함께 늙는다
        _setup(full, "열하나", rows)
        e = _drift()
        check("도구 수 표기 어긋남을 잡는다 (#42)",
              any("도구 수 표기가 규범과 다르다" in x for x in e), e)

        #    ④ 파서에 있는 명령이 CLI 표에 없다 — **위임 명령으로 시험한다.**
        #       구판은 명령 집합을 소스 정규식으로 만들어 `update`·`release`가
        #       하나도 안 걸렸고, 그래서 이 두 행을 지워도 통과했다(리뷰 지적).
        _setup(full, "넷", rows_of(_leaves - {"update", "release"}))
        e = _drift()
        check("CLI 표에서 빠진 위임 명령을 잡는다 (#42)",
              any("CLI 표에 없는 명령이다: release" in x for x in e)
              and any("CLI 표에 없는 명령이다: update" in x for x in e), e)

        #    ⑤ 파서에서 없어진 명령이 안내문에 남아 있다(반대 방향)
        _setup(full, "넷", rows + chr(10) + "| `osk-sign` | 폐지된 명령 |")
        e = _drift()
        check("없어진 명령이 표에 남은 것을 잡는다 (#42)",
              any("없어진 명령이 남아 있다: osk-sign" in x for x in e), e)

        #    ⑥ 묶음 이름(`raw`·`sm`)은 적어도 되고 안 적어도 된다
        _setup(full, "넷", rows + chr(10) + "| `raw` / `sm` | 묶음 |")
        check("묶음 이름은 표류로 세지 않는다 (#42)", _drift() == [], _drift())

        #    ⑦ **설명 칸의 backtick은 명령이 아니다.** 행 전체를 훑으면 설명에
        #       적힌 인자·경로·예시가 전부 '없어진 명령'으로 잡혀, 안내문을 고칠
        #       때마다 거짓 경보가 난다 — 그런 검사는 곧 꺼진다.
        _setup(full, "넷",
               rows + chr(10) + "| `status` | `nonesuch`처럼 쓴다 |")
        check("설명 칸의 backtick을 명령으로 세지 않는다 (#42)",
              _drift() == [], _drift())

        #    ⑧ **표를 못 찾으면 통과가 아니라 오류다.** 구판은 조용히 넘어가서,
        #       안내문을 다시 쓰다 표 머리글이 바뀌는 순간 파서↔안내문 보장을
        #       통째로 잃었다 — 그 뒤로는 명령을 더하든 지우든 아무 말이 없다.
        #       도구 목록 갈래가 '문단을 찾지 못했다'를 내는 것과 같아야 한다.
        body = (lab / "docs/SETUP.md").read_text(encoding="utf-8")
        (lab / "docs/SETUP.md").write_bytes(
            body.replace("| 명령 | 하는 일 |", "| CLI 명령 | 설명 |")
            .encode("utf-8"))
        e = _drift()
        check("CLI 표 머리글이 바뀌면 검사 불성립을 보고한다 (#42)",
              any("CLI 표를 찾지 못했다" in x for x in e), e)
        #       표를 통째로 지운 경우 — 머리글 자체가 없다
        (lab / "docs/SETUP.md").write_bytes(
            ("## MCP 서버" + chr(10) * 2
             + "도구는 넷이며 그 목록의 정본은 Mechanism §6-2 7항이다 —"
             + chr(10) + full + chr(10) * 2 + "끝." + chr(10)).encode("utf-8"))
        e = _drift()
        check("CLI 표가 통째로 없으면 검사 불성립을 보고한다 (#42)",
              any("CLI 표를 찾지 못했다" in x for x in e), e)

        #       도구 목록 문단도 같다 — 그 문단을 찾는 문장이 바뀌면 조용히
        #       넘어가는 대신 불성립을 말해야, 목록 표류가 계속 잡힌다.
        (lab / "docs/SETUP.md").write_bytes(
            ("## MCP 서버" + chr(10) * 2
             + "도구는 넷이며 목록은 아래와 같다 —" + chr(10)   # 정본 문장이 바뀌었다
             + full + chr(10) * 2
             + "| 명령 | 하는 일 |" + chr(10) + "|---|---|" + chr(10)
             + rows + chr(10) * 2 + "끝." + chr(10)).encode("utf-8"))
        e = _drift()
        check("도구 목록 문단을 못 찾으면 검사 불성립을 보고한다 (#42)",
              any("도구 목록 문단을 찾지 못했다" in x for x in e), e)

        #       파서를 세우지 못하는 경우도 통과가 아니다 — 검사가 스스로
        #       꺼지는 자리를 남기지 않는다.
        _setup(full, "넷", rows)
        with mock.patch.object(validate, "cli_commands",
                               side_effect=RuntimeError("파서 고장")):
            e = _drift()
        check("파서를 못 세우면 검사 불성립을 보고한다 (#42)",
              any("파서에서 만들지 못했다" in x for x in e), e)

        #    ⑨ 맞으면 아무 말도 하지 않는다
        _setup(full, "넷", rows)
        check("어긋남이 없으면 조용하다 (#42)", _drift() == [], _drift())
    finally:
        rmtree_force(lab)

    #    ⑩ **이 저장소 자신의** 안내문이 지금 규범·파서와 같다.
    #       위 아홉은 검사가 도는지를 보고, 이것이 실제 표류를 막는 자리다.
    #       `ROOT`는 mini-vault라 `docs/`가 없다 — 그대로 부르면 빈 목록이
    #       돌아와 **무엇을 지워도 통과한다**(첫 판이 그래서 뮤턴트를 살렸다).
    repo = ENGINE.parent.parent
    if not (repo / "docs/SETUP.md").is_file():
        skip("이 저장소의 안내문 대조 (#42)", "docs/SETUP.md 부재 — 인스턴스다")
    else:
        real = validate.setup_doc_drift(_vault_at(repo))
        check("이 저장소의 안내문이 규범·파서와 어긋나지 않는다 (#42)",
              real == [], real)


_SHAPE_ERRORS = ("TypeError", "NameError", "AttributeError", "ImportError", "ModuleNotFoundError", "SyntaxError")


def _one_liner_breaks(code: str, root: Path) -> str | None:
    """`python -c`에 넘기는 한 줄 코드를 임시 저장소에서 실제로 돌린다. 호출 모양이 틀려서 나는 오류(인자 수,
    없는 이름, 없는 모듈)면 그 줄을 돌려준다. 자리 표시 인자(`'<하네스>'`)가 값으로서 거부되는 것은 모양의
    문제가 아니다."""
    r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL, timeout=120, cwd=str(root),
                       env={**os.environ, "OSK_VAULT_ROOT": str(root), "PYTHONPATH": str(ENGINE), "PYTHONUTF8": "1"})
    last = (r.stderr.strip().splitlines() or [""])[-1]
    return last if last.split(":")[0].rsplit(".", 1)[-1] in _SHAPE_ERRORS else None


def test_documented_one_liners():
    """문서가 안내하는 `python -c "from osk import …"` 한 줄 명령은 엔진 함수를 직접 부른다 — 대응하는 CLI가
    없는 자리다. 표면 고정 시험은 여기를 보지 않아서, 함수의 서명이 바뀌면 문서가 조용히 틀린다(구조 개편
    2단계에서 `workspaces.add`가 저장소를 받게 됐는데 문서는 그대로였다). 문서의 한 줄을 그대로 돌려 본다.

    무엇을 망가뜨리면 실패하는가:
      · 문서의 한 줄 명령이 부르는 함수의 인자를 바꾸고 문서를 두면 → 그 문서의 단언이 실패
      · 한 줄 명령에서 import를 빼면 → 그 문서의 단언이 실패
      · 틀린 호출을 맞다고 보면 → 대조군 단언 둘이 실패"""
    import re
    repo = ENGINE.parent.parent
    files = [*sorted(repo.glob("*.md")), *sorted((repo / "docs").rglob("*.md")), *sorted((repo / "_governance").glob("*.md"))]
    found = [(p.relative_to(repo).as_posix(), m.group(2)) for p in files if p.is_file()
             for m in re.finditer(r"""(?:python[\w.]*|py)(?: -\w+)* -c (["'])(.*\bosk\b.*)\1""",
                                  p.read_text(encoding="utf-8"))]
    # 엔진을 import하는 것만 돌린다 — 저장소 주소에 `osk`가 든 다른 한 줄 명령(릴리스 태그 조회)은 네트워크를 탄다
    found = [(name, code) for name, code in found if re.search(r"(?:from|import)\s+osk\b", code)]
    with tempfile.TemporaryDirectory(prefix="osk-one-liner-") as td:
        root = Path(td).resolve()
        validate.make_mini_vault(VAULT, root)
        check("한 줄 명령 대조: 저장소를 넘기지 않는 호출을 잡는다",
              bool(_one_liner_breaks("from osk import workspaces; print(workspaces.add('h', 'r', r's', 'v', 'n'))", root)))
        check("한 줄 명령 대조: import하지 않은 이름을 잡는다",
              bool(_one_liner_breaks("from osk import workspaces; print(workspaces.add(Vault.open(), 'h', 'r', r's', 'v', 'n'))", root)))
        if not found:
            skip("문서의 한 줄 명령이 지금 엔진에서 돈다", "문서에 한 줄 명령이 없다 — 인스턴스다")
            return
        for name, code in found:
            broken = _one_liner_breaks(code, root)
            check(f"문서의 한 줄 명령이 지금 엔진에서 돈다: {name}", not broken, f"{broken} ← {code}")


def test_validate_at_reads_only_a_report():
    """발행과 릴리스의 검증은 자식 프로세스의 마지막 줄을 보고로 읽는다. 보고가 아닌 줄은 무엇이든 "출력 파싱
    실패"라는 거부가 된다 — 릴리스는 이 함수를 관문 없이 부르므로, 여기서 새는 예외는 릴리스를 죽인다."""
    for label, emits in (("JSON이 아닌 줄", "print('not a report')"),
                         ("깊이 겹친 줄", "print('[' * 100000)"),
                         ("자릿수 상한을 넘는 정수", "print('9' * 5000)")):
        with tempfile.TemporaryDirectory(prefix="osk-validation-output-") as td:
            package = Path(td) / "_governance/_engine/osk"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "vault.py").write_text(
                "class Vault:\n    @classmethod\n    def open(cls):\n        return cls()\n", encoding="utf-8")
            (package / "cli.py").write_text("def published_tools():\n    return []\n", encoding="utf-8")
            (package / "validate.py").write_text(
                "import sys\n\ndef run(vault, read_tools):\n    " + emits + "\n    sys.exit(0)\n", encoding="utf-8")
            try:
                errors = publish.validate_at(Path(td))
            except Exception as e:
                errors = [f"올랐다: {type(e).__name__}"]
        check(f"보고가 아닌 출력은 거부다 — {label}",
              len(errors) == 1 and errors[0].startswith("검증기 출력 파싱 실패"), errors)


def test_every_cli_command_has_its_handler():
    """문법에 있는 명령은 하는 일이 있다. 명령을 문법(`cli_parser`)에 더하고 실행의 표(`cli._COMMANDS`)에 올리지
    않으면 그 명령은 오류 없이 아무것도 하지 않는다 — 위임 명령(파서 앞에서 갈린다)만 표에 없다."""
    import argparse
    from osk import cli_parser
    subparsers = next(a for a in cli_parser.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    declared = set(subparsers.choices) - set(cli_parser.DELEGATED)
    check("문법의 명령과 실행의 표가 같다", declared == set(cli._COMMANDS),
          (sorted(declared - set(cli._COMMANDS)), sorted(set(cli._COMMANDS) - declared)))


def test_review_packet_sets_aside_a_mistyped_entry():
    """결과 묶음의 항목 하나가 틀리면 그 항목만 받지 않는다(`growth_reviews._validate_packet`). 형이 틀린 값도
    같다 — 재검토 항목의 `outcome`이 목록·객체이면 `TypeError`가 나서 같은 묶음의 다른 판정까지 버려졌다."""
    from osk import growth_reviews
    planned = {"manifest": "m", "candidates": [{"key": "c-1"}], "scope_jobs": [],
               "recheck_jobs": [{"key": "r-1"}, {"key": "r-2"}]}
    packet = {"osk_reviews": {"manifest": "m", "scope": [],
                              "domain": [{"key": "c-1", "outcome": "no_value", "reason": "남길 것이 없다"}],
                              "recheck": [{"key": "r-1", "outcome": ["escalated"], "reason": "r"},
                                          {"key": "r-2", "outcome": {}, "reason": "r"}]}}
    try:
        out = growth_reviews._validate_packet(packet, planned)
    except Exception as e:
        out = {"domain": [], "errors": [repr(e)]}
    check("형이 틀린 항목은 그 항목만 받지 않는다",
          len(out["domain"]) == 1 and out.get("recheck") == [] and len(out["errors"]) == 2
          and all("only escalates or defers" in e for e in out["errors"]), out)


def test_validate_suite_survives_a_crashing_check():
    """검사가 예외를 올려도 수트는 죽지 않는다(시행령 §11). 그 검사의 실패로 적고 다음 검사로 간다 — 받는 자리는
    등록부(`validate.CHECKS`)를 도는 한 곳이다. 승인 기록부를 읽다 죽으면, 그 행에 기대는 검사는 빈 행을 검사한
    헛 PASS 대신 보류로 남는다."""
    def boom(s):
        raise RuntimeError("터졌다")

    saved = validate.CHECKS
    try:
        validate.CHECKS = (("앞 검사", lambda s: []), ("터지는 검사", boom), ("뒤 검사", lambda s: ["위반"]))
        rep = validate.run(VAULT, cli.published_tools)
        check("터진 검사는 그 검사의 실패이고 뒤의 검사는 돈다",
              rep["pass"] == ["앞 검사"] and rep["verdict"] == "FAIL"
              and rep["fail"] == [{"터지는 검사": ["검사 자체 실패: 터졌다"]}, {"뒤 검사": ["위반"]}], rep)
        validate.CHECKS = tuple((name, boom if name == "승인 기록부" else fn) for name, fn in saved)
        rep = validate.run(VAULT, cli.published_tools)
        held = {name for item in rep["skipped"] for name in item}
        check("승인 기록부를 읽다 죽으면 기대는 검사는 보류한다",
              {"승인 기록부": ["검사 자체 실패: 터졌다"]} in rep["fail"]
              and {"승인 대장 rid 유일·형식", "승인 기록부 정합", "승인 대장 스키마"} <= held, rep)
    finally:
        validate.CHECKS = saved


def test_validate_at_uses_snapshot_engine():
    """PYTHONPATH보다 앞서는 호출 cwd가 스냅샷 검증기를 바꿔치기하지 못한다."""
    with tempfile.TemporaryDirectory(prefix="osk-validation-import-") as td:
        snapshot = Path(td) / "snapshot"
        caller = Path(td) / "caller-engine"
        for engine, verdict, failures in (
                (snapshot / "_governance/_engine", "FAIL", [{"snapshot rejection": []}]),
                (caller, "PASS", [])):
            package = engine / "osk"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "vault.py").write_text(
                "class Vault:\n    @classmethod\n    def open(cls):\n        return cls()\n", encoding="utf-8")
            (package / "validate.py").write_text(
                "def run(vault, read_tools):\n    return " + repr({"verdict": verdict, "fail": failures}) + "\n",
                encoding="utf-8")
            (package / "cli.py").write_text("def published_tools():\n    return []\n", encoding="utf-8")
        previous = Path.cwd()
        try:
            os.chdir(caller)
            errors = publish.validate_at(snapshot)
        finally:
            os.chdir(previous)
        check("원본 엔진 cwd에서도 snapshot 검증기를 실행",
              errors == ["검증기 FAIL: snapshot rejection"], errors)
