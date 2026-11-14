"""회귀 수트의 한 갈래: 노드 쓰기 — 계약, 앵커 편집, 엣지, 결속과 라우팅.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
from unittest import mock

from regression.support import (
    _age_all, _append_round, check, ENGINE, MINI, node_text, rmtree_force, ROOT, _TMP, _unhang, VAULT, _w, wipe_sig
)
from osk import core, graph, contract, routing, write, node_moves, nodefile, topology  # noqa: E402


# ── 위상 검증도 derived-from의 id 강제를 본다 (쓰기 통로 밖 유입) ────────
def test_topology_rejects_wiki_node_derived_from():
    """손으로 쓰거나 다기기 동기화로 들어온 노드는 쓰기 통로를 거치지 않는다 —
    위상 검증이 그 자리에서 `derived-from`의 비-id 노드 대상을 잡아야 한다."""
    target = ROOT / "00_Scope/W1/regr-topo-t.md"
    bad = ROOT / "00_Scope/W1/regr-topo-bad.md"
    try:
        target.write_text(node_text("260802-zzzz-topo", "대상", "본문"),
                          encoding="utf-8")
        bad.write_text(node_text("260802-zzzz-tbad", "제목 근거", "본문",
                                 'derived-from: "[[regr-topo-t]]"\n'),
                       encoding="utf-8")
        errs = topology.topology_check(VAULT, graph.Index(VAULT))
        check("제목 표기의 노드 근거는 위반이 아니다 (v3.7.3)",
              not any("regr-topo-bad" in e for e in errs), errs)
        bad.write_text(node_text("260802-zzzz-tbad", "경로 근거", "본문",
                                 'derived-from: "[[00_Scope/W1/regr-topo-t]]"\n'),
                       encoding="utf-8")
        errs = topology.topology_check(VAULT, graph.Index(VAULT))
        check("경로 표기의 노드 근거를 위반으로 잡는다",
              any("경로 표기는 쓰지 않는다" in e and "regr-topo-bad" in e
                  for e in errs), errs)
        bad.write_text(node_text("260802-zzzz-tbad", "id 근거", "본문",
                                 "derived-from: 260802-zzzz-topo\n"),
                       encoding="utf-8")
        errs = topology.topology_check(VAULT, graph.Index(VAULT))
        check("id 근거는 위반이 아니다",
              not any("regr-topo-bad" in e for e in errs), errs)
    finally:
        target.unlink(missing_ok=True)
        bad.unlink(missing_ok=True)


def test_write_contract():
    wipe_sig()
    r = write.create_node(VAULT, "regr-w1", "쓰기 통로 시험", "본문",
                          "fable-5", space="00_Scope/W1")
    check("create_node 성공", r["ok"] and r["id"], r)
    check("id·시각은 서버가 정한다", core.re.match(core.ID_RE, r["id"]) is not None)
    n = contract.parse(ROOT / r["path"])
    check("author는 agent 고정", str(n.meta["author"]) == "agent")
    check("계약 검증 통과", not contract.validate(n), contract.validate(n))

    check("같은 이름 재생성 거부(중복 후보 자기생산 방지)",
          not _w(write.create_node, VAULT, "regr-w1", "s", "b", "fable-5",
                 space="00_Scope/W1")["ok"])
    check("통치 구획 쓰기 거부",
          not _w(write.create_node, VAULT, "regr-w2", "s", "b", "fable-5",
                 space="_governance")["ok"])
    check("선언 안 된 군집 거부",
          not _w(write.create_node, VAULT, "regr-w3", "s", "b", "fable-5",
                 space="00_Scope/없는scope")["ok"])
    bad = _w(write.create_node, VAULT, "regr-w4", "s", "b", "fable-5",
             space="00_Scope/W1", edges={"conflicts": "regr-w1"})
    check("노드 간 conflicts는 표면에서 거부(닭-달걀)",
          not bad["ok"] and any("열린 사건" in v for v in bad["violations"]), bad)
    check("거부하면 파일을 만들지 않는다(부분 성공 없음)",
          not (ROOT / "00_Scope/W1/regr-w4.md").exists())


def test_contract_values():
    """author·drafter·시각의 **값** (Mechanism §2 2·4항) — 구판은 형식식만 봐서
    `author: robot`·빈/정수 drafter·`2026-02-30`이 검증기와 갱신을 통과했다."""
    ok_ts = "2026-08-01 00:00 (KST)"

    def errs(a='"agent"', d='"fable-5"', c=ok_ts, nid="260801-zzzz-aaaa"):
        text = (f'---\nid: "{nid}"\ncreated: "{c}"\nupdated: "{ok_ts}"\n'
                f'author: {a}\ndrafter: {d}\nsummary: "s"\n---\n\nbody\n')
        return contract.validate(contract.parse_bytes(ROOT / "00_Scope/W1/x.md",
                                                      text.encode()))
    for label, kw in (("author robot", {"a": '"robot"'}), ("author 빈 값", {"a": '""'}),
                      ("drafter 빈 값", {"d": '""'}), ("drafter null", {"d": ""}),
                      ("drafter 정수", {"d": "5"}), ("drafter 대문자·공백", {"d": '"Fable 5!"'}),
                      ("drafter 끝 개행", {"d": '"fable-5\\n"'}),
                      ("created 2026-02-30", {"c": "2026-02-30 10:00 (KST)",
                                              "nid": "260230-zzzz-aaaa"}),
                      ("created 99:99", {"c": "2026-08-01 99:99 (KST)"})):
        check(f"계약 값 위반 적발: {label}", errs(**kw), kw)
    # 실 vault 전수(1,968 노드)에서 나온 drafter는 전부 그대로 유효해야 한다
    census = ("user agent sonnet-5 fable-5 gpt-6 opus-5 ox-alpha claude-opus-5 "
              "claude-fable-5.1 kiro gpt-6-astra opus-5.5 gpt-5.6-sol x-preview-f-free "
              "opus-4.8 claude-opus-5-5 gpt-5 opus antigravity "
              "muse-spark-1.2-contributor-free worker orchestrator mimo-v2.5-free "
              "claude-opus-5.5 gemini-3.1-pro claude-fable-5 sol claude-fable-5-1 "
              "codex-gpt-5").split()
    bad = {d: e for d in census if (e := errs(d=f'"{d}"'))}
    check("실측 drafter 전부 유효", not bad, bad)
    check("author user도 유효", not errs(a='"user"', d='"user"'))
    # 표면 스키마를 거치지 않는 쓰기(내부 호출)도 같은 식에 막힌다
    r = _w(write.create_node, VAULT, "regr-drafter-bad", "s", "b", "Fable 5", space="00_Scope/W1")
    check("스키마 밖 drafter는 쓰기 통로가 거부",
          not r["ok"] and not (ROOT / "00_Scope/W1/regr-drafter-bad.md").exists(), r)


def test_bind_after_write_receipt():
    """파일을 쓴 뒤 결속 append가 실패하면 `ok:false`가 아니라 영수증이다 —
    구판은 노드가 선 채로 실패를 보고해 재시도가 이름 충돌로 거부됐다."""
    import mcp_server as M
    real = write.ledger_append

    def failing(path, record, expect=None):
        if path == VAULT.routing:
            raise PermissionError(13, "공유 위반 모사", str(path))
        return real(path, record, expect)

    # Mechanism §6-2 10항(#273 질문 3): search를 한 번도 부르지 않은 세션은 새 노드를 세우지 못한다
    M._searched = False
    refused = M.create_node("regr-bindfail", "s", "본문", "fable-5", space="00_Scope/W1")
    check("search 없는 세션의 create_node는 거부되고 아무것도 쓰지 않는다",
          refused == {"ok": False, "violations": [M.SEARCH_FIRST]}
          and not (ROOT / "00_Scope/W1/regr-bindfail.md").exists(), refused)
    with mock.patch.object(routing, "ledger_append", failing):
        M.search("bindfail")                       # 한 번 부르면 이 세션의 생성이 열린다
        r = M.create_node("regr-bindfail", "s", "본문", "fable-5",
                          session="regr-bindfail-a", space="00_Scope/W1")
        rr = _w(_append_round, "regr-bindfail-b", "regr-bindfail-rec", "질문", "응답",
                space="00_Scope/W1")  # cite_round가 지나는 기록 통로
    check("결속 실패에도 노드 생성은 ok", r.get("ok") and r.get("id") and r.get("new_hash"), r)
    check("결속은 확인 불가 영수증으로 남는다",
          (r.get("binding") or {}).get("state") == "unconfirmed"
          and r.get("bound_scope") is None and "PermissionError" in r["binding"]["error"], r)
    check("노드는 실제로 섰다", (ROOT / "00_Scope/W1/regr-bindfail.md").exists())
    check("인용 기록은 결속하지 않으므로 영수증도 없다 (v5.2)",
          rr.get("ok") and rr.get("index") == 1 and "binding" not in rr, rr)
    # 영수증이 안내한 대로 — 같은 session과 space로 다음 쓰기를 하면 결속이 선다
    r2 = _w(write.create_node, VAULT, "regr-bindfail-2", "s", "본문", "fable-5",
            session="regr-bindfail-a", space="00_Scope/W1")
    check("안내대로 다음 쓰기에서 결속이 선다",
          r2.get("ok") and r2.get("bound_scope") == "W1" and "binding" not in r2
          and write.bindings(VAULT, "regr-bindfail-a")["scopes"] == ["W1"], r2)


def test_write_cas_body_bound():
    # 서명이 폐지됐으므로 CAS는 **본문 전체 치환**에만 결속한다(Mechanism
    # §6-2 4항) — 부분 변경(엣지 델타·summary)에는 요구하지 않는다.
    node = ROOT / "00_Scope/W1/regr-w1.md"
    r = _w(write.update_node, VAULT, "regr-w1", summary="고친 요약")
    check("summary 변경은 CAS 면제", r["ok"], r)
    check("덮은 요약을 응답에 담는다",
          r.get("replaced_summary") == "쓰기 통로 시험", r)
    r = _w(write.update_node, VAULT, "regr-w1", add_edges={"derived-from": "regr-w1x"})
    check("엣지 델타도 면제", r["ok"], r)
    check("dangling을 응답으로 알린다", "regr-w1x" in r.get("dangling", []), r)
    check("응답에 폐지된 signed 필드가 없다", "signed" not in r, r)
    # 본문 전체 치환은 언제나 CAS
    r = _w(write.update_node, VAULT, "regr-w1", body="새 본문")
    check("본문 치환은 CAS 필수", not r["ok"], r)
    h = core.sha256_file(node)
    r = _w(write.update_node, VAULT, "regr-w1", body="새 본문", expect_hash="sha256:틀림")
    check("CAS 불일치는 거부", not r["ok"], r)
    check("거부 응답에 현재 해시를 담지 않는다",
          not any("sha256:" in str(v) for v in r["violations"]), r)
    r = _w(write.update_node, VAULT, "regr-w1", body="새 본문", expect_hash=h)
    check("올바른 CAS면 통과", r["ok"] and r["new_hash"] != h, r)


def test_anchor_edit():
    """앵커 편집 — 본문의 **부분 변경**은 해시가 아니라 앵커가 관측을 증명한다
    (Mechanism §6-2 4항 "부분 변경에는 요구하지 않는다").

    무엇을 망가뜨리면 이 시험이 실패하는가: 앵커 경로가 해시를 요구하게 되면 ·
    앵커가 없거나 여러 곳인데 쓰면 · `body`와 병용을 허용하면 · 치환이 저장물에
    안 닿으면 · 앵커 본문이 검증 통로(비밀값 필터·왕복 검증)를 건너뛰면 ·
    `updated` 각인이 빠지면."""
    body = ("첫 줄이다.\n\n같은 말이 두 번 나온다.\n중간 줄.\n"
            "같은 말이 두 번 나온다.\n마지막 줄이다.\n")
    r = _w(write.create_node, VAULT, "regr-anchoredit", "앵커 편집 시험", body, "opus-5",
           space="00_Scope/W1")
    check("시험 노드 생성", r.get("ok"), r)
    p = ROOT / "00_Scope/W1/regr-anchoredit.md"
    h0 = core.sha256_file(p)

    # 계약의 핵심 — 부분 변경에 해시를 요구하지 않는다
    r = _w(write.update_node, VAULT, "regr-anchoredit",
           old_text="첫 줄이다.", new_text="첫 줄을 고쳤다.")
    check("앵커 편집은 해시 없이 통과", r.get("ok"), r)
    cur = contract.parse(p).body
    check("치환이 저장물에 반영된다", "첫 줄을 고쳤다." in cur, cur[:80])
    check("앵커는 그 자리만 바꾼다", "마지막 줄이다." in cur, cur[-60:])
    check("본문이 실제로 바뀌었다", core.sha256_file(p) != h0)
    check("새 해시를 돌려준다 — 연쇄가 산다",
          r.get("new_hash") == core.sha256_file(p), r)

    # 유일성이 안전 계약의 전부다
    r = _w(write.update_node, VAULT, "regr-anchoredit",
           old_text="같은 말이 두 번 나온다.", new_text="바꿈")
    check("앵커가 여러 곳이면 거부", not r.get("ok"), r)
    check("거부가 유일하게 만들라고 가리킨다",
          "유일" in " ".join(r.get("violations", [])), r)
    check("맞는 자리마다 앞뒤 문단의 범위를 준다(#273)",
          len(r.get("views", [])) == 2 and "같은 말이" in contract.parse(p).body[
              int(r["views"][1].split(":")[0]):int(r["views"][1].split(":")[1])], r)
    big = "가\n\n" + "나" * 4100 + "\n\n다"
    check("창 끝에 인용의 줄바꿈을 넣지 않는다(#273)",
          nodefile.anchor_windows(big, "나" * 4100 + "\n")[1] == [(3, 4103)],
          nodefile.anchor_windows(big, "나" * 4100 + "\n"))
    check("여러 곳 거부는 파일을 건드리지 않는다",
          contract.parse(p).body.count("같은 말이 두 번 나온다.") == 2)
    r = _w(write.update_node, VAULT, "regr-anchoredit",
           old_text="중간 줄.\n같은 말이 두 번 나온다.", new_text="중간 줄.\n하나만 바꿈")
    check("앞뒤를 붙여 유일하게 만들면 통과", r.get("ok"), r)
    check("거부가 막다른 길이 아니다 — 한 곳만 바뀌었다",
          contract.parse(p).body.count("같은 말이 두 번 나온다.") == 1)

    # 없는 앵커
    r = _w(write.update_node, VAULT, "regr-anchoredit", old_text="없는 문장", new_text="x")
    check("앵커가 없으면 거부", not r.get("ok"), r)
    check("거부가 다시 읽으라고 가리킨다",
          "read_node" in " ".join(r.get("violations", [])), r)

    # 형태가 틀린 요청 — 잠금을 잡기 전에 걸러야 한다
    check("한쪽만 주면 거부",
          not _w(write.update_node, VAULT, "regr-anchoredit", old_text="첫 줄을").get("ok"))
    check("빈 앵커는 거부",
          not _w(write.update_node, VAULT, "regr-anchoredit",
                 old_text="", new_text="x").get("ok"))
    check("old==new는 거부",
          not _w(write.update_node, VAULT, "regr-anchoredit",
                 old_text="중간 줄.", new_text="중간 줄.").get("ok"))
    h = core.sha256_file(p)
    r = _w(write.update_node, VAULT, "regr-anchoredit", body="통째로", expect_hash=h,
           old_text="중간 줄.", new_text="바꿈")
    check("`body`와 앵커 병용은 거부", not r.get("ok"), r)
    check("병용 거부는 파일을 건드리지 않는다", core.sha256_file(p) == h)

    # 앵커 본문도 `body`와 **같은 통로**를 지난다 — 여기가 새면 계약이 반만
    # 산다. 노드 통로에 실제로 걸리는 것은 옵시디언 태그 방어와 렌더 왕복
    # 검증이다(비밀값 필터는 `_raw/`·scope 기억 전용이다 — Mechanism §9 1항).
    r = _w(write.update_node, VAULT, "regr-anchoredit",
           old_text="마지막 줄이다.", new_text="#1227은 심각했다")
    check("앵커 편집도 통로를 지난다", r.get("ok"), r)
    check("태그 방어가 저장물에 반영된다",
          "#1227 은 심각했다" in contract.parse(p).body,
          contract.parse(p).body[-60:])
    r = _w(write.update_node, VAULT, "regr-anchoredit",
           old_text="#1227은 심각했다", new_text="#1227은 심각했었다")
    check("방금 쓴 글 그대로의 앵커도 태그 방어 뒤의 저장본에 맞는다(#292, 창 계산과 함께)",
          r.get("ok") and "#1227 은 심각했었다" in contract.parse(p).body, r)

    # 부분 변경도 변경이다 — 각인이 빠지면 최신성이 거짓이 된다
    before = contract.parse(p).meta.get("updated")
    r = _w(write.update_node, VAULT, "regr-anchoredit",
           old_text="하나만 바꿈", new_text="또 바꿈")
    check("앵커 편집이 updated를 각인한다",
          contract.parse(p).meta.get("updated") is not None and r.get("ok"), r)

    # 지우기는 빈 문자열로 — `new_text` 생략과 구분된다
    r = _w(write.update_node, VAULT, "regr-anchoredit", old_text="또 바꿈\n", new_text="")
    check("빈 new_text로 지운다", r.get("ok"), r)
    check("지워졌다", "또 바꿈" not in contract.parse(p).body)


def test_full_body_dropped():
    """전문 치환은 흘린 것을 비춘다 — 옛 본문의 단위(문단·목록 항목·제목) 가운데 새 본문에
    글자 그대로 없는 것을 `dropped`로 돌려준다(헌법 1조 3항).

    무엇을 망가뜨리면 이 시험이 실패하는가: 흘린 문단을 싣지 않으면 · 옮기기만 한 문단을
    실으면 · 고친 항목 하나 대신 목록 전체를 실으면 · 앵커 편집에도 실으면 · 많을 때
    자르지 않으면."""
    body = ("## 결정\n\n첫 문단은 그대로 둔다.\n\n둘째 문단은 흘릴 것이다.\n\n"
            "- 항목 하나\n- 항목 둘\n- 항목 셋\n")
    p = ROOT / "00_Scope/W1/regr-dropped.md"
    try:
        r = _w(write.create_node, VAULT, "regr-dropped", "전문 치환 거울 시험", body, "opus-5",
               space="00_Scope/W1")
        check("시험 노드 생성", r.get("ok"), r)

        # 둘째 문단을 흘리고, 둘째 항목을 고쳐 쓰고, 첫 문단을 뒤로 옮긴다
        new = "## 결정\n\n- 항목 하나\n- 둘째 항목\n- 항목 셋\n\n첫 문단은 그대로 둔다.\n"
        r = _w(write.update_node, VAULT, "regr-dropped", body=new, expect_hash=core.sha256_file(p))
        check("전문 치환 통과", r.get("ok"), r)
        check("흘린 문단과 고쳐 쓴 항목의 옛 판만 비춘다",
              r.get("dropped") == ["둘째 문단은 흘릴 것이다.", "- 항목 둘"], r.get("dropped"))

        moved = "첫 문단은 그대로 둔다.\n\n## 결정\n\n- 항목 하나\n- 둘째 항목\n- 항목 셋\n"
        r = _w(write.update_node, VAULT, "regr-dropped", body=moved, expect_hash=core.sha256_file(p))
        check("옮기기만 한 전문 치환에는 dropped가 없다", r.get("ok") and "dropped" not in r, r)

        r = _w(write.update_node, VAULT, "regr-dropped", old_text="\n- 항목 셋", new_text="")
        check("앵커 편집에는 dropped가 없다", r.get("ok") and "dropped" not in r, r)

        many = "\n\n".join(f"문단 {i} " + "가" * 400 for i in range(12))
        r = _w(write.update_node, VAULT, "regr-dropped", body=many, expect_hash=core.sha256_file(p))
        check("긴 본문으로 바꾼다", r.get("ok"), r)
        r = _w(write.update_node, VAULT, "regr-dropped", body="모두 지웠다.",
               expect_hash=core.sha256_file(p))
        d = r.get("dropped") or []
        check("많으면 앞의 10개와 남은 개수만 싣는다",
              len(d) == 11 and d[-1] == "… 외 2개", d[-1:])
        check("긴 단위는 300자에서 자른다",
              all(len(t) == 300 and t.endswith("…") for t in d[:10]), [len(t) for t in d])
    finally:
        # 공유 mini-vault의 W1에 노드를 남기지 않는다 — 군집 보고의 미직결 목록은 앞의
        # 20개만 실어, 이름순으로 끼어든 노드가 뒤 시험이 찾는 노드를 밀어낸다.
        p.unlink(missing_ok=True)


def test_edge_single_list_roundtrip():
    """손으로/정본에서 단일 원소 리스트로 저장된 엣지(`derived-from: [<id>]`)를
    가진 노드가 그 엣지를 건드리지 않는 갱신에서 왕복 불일치로 거부되지 않는다
    (리스트 `[x]`와 스칼라 `x`는 대상이 하나로 논리적으로 같다)."""
    tgt = ROOT / "00_Scope/W1/regr-sl-target.md"
    ref = ROOT / "00_Scope/W1/regr-sl-ref.md"
    try:
        tgt.write_text(node_text("260802-zzzz-rgs1", "근거"), encoding="utf-8")
        # 단일 원소 리스트 표기로 손수 저장
        ref.write_text(node_text("260802-zzzz-rgs2", "파생", "본문",
                                 'derived-from: [260802-zzzz-rgs1]\n'),
                       encoding="utf-8")
        check("전제: 단일 원소 리스트로 파싱된다",
              contract.parse(ref).meta.get("derived-from") == ["260802-zzzz-rgs1"])
        r = _w(write.update_node, VAULT, "regr-sl-ref", summary="요약 갱신")
        check("단일 원소 리스트 엣지 노드의 무관한 갱신이 거부되지 않는다",
              r.get("ok") is True, r)
        check("갱신 후에도 근거를 여전히 가리킨다",
              "260802-zzzz-rgs1" in contract.parse(ref).edges("derived-from"))
    finally:
        for p_ in (tgt, ref):
            p_.unlink(missing_ok=True)


def test_write_move_and_pin():
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    before = core.sha256_file(ROOT / "00_Scope/W1/regr-w1.md")
    r = _w(node_moves.move_node, VAULT, "regr-w1", "00_Scope/W2")
    check("move는 바이트 불변(해시 동일)", r["ok"] and r["new_hash"] == before, r)
    check("이동 후 구 경로에 파일이 없다",
          not (ROOT / "00_Scope/W1/regr-w1.md").exists())
    # 생성이 W1 허브에 건 줄은 scope를 건너면 scope 간 직접 참조다(헌법 8조 3항).
    # 응답이 그 줄을 빼라고 알리고, 안내대로 빼야 vault가 검증을 통과한다.
    check("scope를 건너면 출발지 허브의 줄을 빼라고 알린다",
          {"hub": "W1", "remove": ["regr-w1"]} in r.get("hub_links", []), r)
    check("안내대로 출발지 허브의 줄을 뺀다", _unhang("W1", "regr-w1").get("ok"))
    core.ledger_append(VAULT.pins, {"kind": "pin", "target": "00_Scope/W2/",
                                   "reason": "시험"})
    r = _w(node_moves.move_node, VAULT, "regr-w1", "00_Scope/W1")
    check("pin된 군집은 재배정 거부(시행령 §3 4항)", not r["ok"], r)
    check("거부 후에도 제자리", (ROOT / "00_Scope/W2/regr-w1.md").exists())


def test_write_routing():
    VAULT.routing.unlink(missing_ok=True)
    r = _w(write.create_node, VAULT, "regr-r1", "라우팅", "본문", "fable-5",
           session="repo/alpha")
    check("최초 세션은 space를 요구한다", not r["ok"], r)
    r = _w(write.create_node, VAULT, "regr-r1", "라우팅", "본문", "fable-5",
           session="repo/alpha", space="00_Scope/W1")
    check("space를 주면 생성되고 결속에 더한다",
          r["ok"] and r["bound_scope"] == "W1", r)
    check("쓰기는 주된 scope를 정하지 않는다 (v5.2)", write.resolve_session(VAULT, "repo/alpha") is None)
    write.set_primary(VAULT, "repo/alpha", "W1", "시험 지정")
    check("지정 후 자동 라우팅", write.resolve_session(VAULT, "repo/alpha") == "W1")
    r = _w(write.create_node, VAULT, "regr-r2", "자동", "본문", "fable-5",
           session="repo/alpha")
    check("두 번째부터는 space 없이 착지", r["ok"] and "W1" in r["path"], r)
    # 다기기 동시 최초-확정 = 분기 → 미확정으로 fail-closed
    recs = core.ledger_read(VAULT.routing)
    fork = dict(recs[-1])
    raw = {"rid": core._make_rid(core.rid_parts(fork["rid"])[0] + 5, 0),
           "parents": [], "kind": "bind", "session": "repo/alpha",
           "scope": "W2", "at": core.now_iso()}
    with open(VAULT.routing, "a", encoding="utf-8") as f:
        f.write(json.dumps(raw, ensure_ascii=False) + "\n")
    check("분기된 바인딩은 미확정(fail-closed)",
          write.resolve_session(VAULT, "repo/alpha") is None)
    check("미확정이면 다시 space를 요구",
          not _w(write.create_node, VAULT, "regr-r3", "s", "b", "fable-5",
                 session="repo/alpha")["ok"])


def test_write_session_alias():
    """개명 이력이 세션을 흩뜨리지 않는다 — 어느 이름으로 들어와도 한 scope."""
    VAULT.routing.unlink(missing_ok=True)
    write.alias_session(VAULT, "old-name", "new-name", "개명")
    write.alias_session(VAULT, "older-name", "old-name", "더 오래된 이름")
    check("별칭 한 단계", write.canonical_session(VAULT, "old-name") == "new-name")
    check("별칭 사슬을 끝까지 접는다",
          write.canonical_session(VAULT, "older-name") == "new-name")
    check("별칭 없는 키는 그대로", write.canonical_session(VAULT, "무관") == "무관")

    r = _w(write.create_node, VAULT, "regr-a1", "별칭", "본문", "fable-5",
           session="older-name", space="00_Scope/W1")
    check("구 이름으로 최초 확정", r["ok"], r)
    write.set_primary(VAULT, "older-name", "W1", "구 이름으로 지정")
    for name in ("older-name", "old-name", "new-name"):
        check(f"{name}으로 들어와도 같은 scope",
              write.resolve_session(VAULT, name) == "W1", write.resolve_session(VAULT, name))
    rec = [x for x in core.ledger_read(VAULT.routing) if x.get("kind") == "bind"][-1]
    check("결속은 정본 키로 기록된다", rec["session"] == "new-name", rec)

    # 순환 별칭은 접지 않는다(무한 루프 방지)
    write.alias_session(VAULT, "A", "B", "순환 시험")
    write.alias_session(VAULT, "B", "A", "순환 시험")
    got = write.canonical_session(VAULT, "A")
    check("순환 별칭에도 종료하고 원래 키로 남는다", got in ("A", "B"), got)


def test_write_candidate_basis():
    VAULT.candidates.write_text("", encoding="utf-8")
    a = ROOT / "00_Scope/W1/regr-c1.md"; b = ROOT / "00_Scope/W1/regr-c2.md"
    a.write_text(node_text("260802-zzzz-rgc1", "후보 A"), encoding="utf-8")
    b.write_text(node_text("260802-zzzz-rgc2", "후보 B"), encoding="utf-8")
    try:
        r1 = _w(write.record_candidate, VAULT, "duplication", ["regr-c1", "regr-c2"], "시험")
        check("후보 상정", r1["ok"] and not r1["deduped"], r1)
        r2 = _w(write.record_candidate, VAULT, "duplication", ["regr-c1", "regr-c2"], "재시도")
        check("같은 근거는 중복 기록하지 않는다(헌법 12조 2항)",
              r2["deduped"] and r2["rid"] == r1["rid"], r2)
        r3 = _w(write.record_candidate, VAULT, "competition", ["regr-c1", "regr-c2"], "다른 유형")
        check("유형이 다르면 다른 근거", not r3["deduped"], r3)
        b.write_text(node_text("260802-zzzz-rgc2", "후보 B", "내용이 바뀜"),
                     encoding="utf-8")
        r4 = _w(write.record_candidate, VAULT, "duplication", ["regr-c1", "regr-c2"], "상태 변경 후")
        check("상태가 바뀌면 재상정이 열린다(각하의 영구 봉인 방지)",
              not r4["deduped"], r4)
        check("미정의 유형 거부",
              not _w(write.record_candidate, VAULT, "무슨유형", ["regr-c1", "regr-c2"])["ok"])
        check("당사자 부재 거부",
              not _w(write.record_candidate, VAULT, "duplication", ["regr-c1", "없는노드"])["ok"])
    finally:
        for p_ in (a, b):
            p_.unlink(missing_ok=True)


def test_write_serialized():
    """동시 쓰기 2건이 전역 잠금으로 직렬화되는가 (subprocess 실측)."""
    script = (
        "import sys, json; sys.path.insert(0, %r)\n"
        "from osk import core, write, cluster_gate, node_checks, node_moves, nodefile\nfrom osk.vault import Vault\nVAULT = Vault.open()\n"
        "import sys as s\n"
        "r = write.update_node(VAULT, 'regr-r2', add_edges={'derived-from': s.argv[1]})\n"
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
    final = contract.parse(ROOT / "00_Scope/W1/regr-r2.md").edges("derived-from")
    check("두 델타가 모두 보존된다(lost update 없음)",
          "tgt0" in final and "tgt1" in final, final)


# ── 14i. 직렬화 왕복 — 표면이 스스로 파손 노드를 만들지 않는다 (7차 중대 A) ──
def test_render_roundtrip():
    for label, kw in (
        ('따옴표', {"summary": 'He said "hi"'}),
        ('백슬래시', {"summary": r"경로 C:\temp\x"}),
        ('콜론·해시', {"summary": "a: b # c"}),
        ('엣지 따옴표', {"summary": "정상", "edges": {"derived-from": '따옴표"대상'}}),
    ):
        name = f"regr-rt-{abs(hash(label)) % 10000}"
        r = _w(write.create_node, VAULT, name, kw.get("summary", "s"), "본문",
               "fable-5", space="00_Scope/W1", edges=kw.get("edges"))
        p = ROOT / "00_Scope/W1" / f"{name}.md"
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
    r = _w(write.create_node, VAULT, "regr-rt-upd", "정상", "본문", "fable-5",
           space="00_Scope/W1")
    check("전제: 생성", r["ok"], r)
    p = ROOT / "00_Scope/W1/regr-rt-upd.md"
    before = p.read_bytes()
    r2 = _w(write.update_node, VAULT, "regr-rt-upd", summary='깨는 "따옴표"')
    check("update 후에도 파일이 파싱된다",
          bool(contract.parse(p)) if p.exists() else False, r2)
    check("실패였다면 파일이 그대로", r2["ok"] or p.read_bytes() == before)
    p.unlink(missing_ok=True)


# ── 14k. 라우팅 벽돌화 방지 + Space 루트 직속 거부 (7차 중대 C·경미 E) ────
def test_routing_not_bricked():
    VAULT.routing.unlink(missing_ok=True)
    r = _w(write.create_node, VAULT, "regr-dom", "도메인 착지", "본문", "fable-5",
           session="repo/beta", space="00_Domain/D1") if (ROOT / "00_Domain/D1").is_dir()\
        else {"skip": True}
    if not r.get("skip"):
        check("Domain 착지는 세션을 결속하지 않는다(scope 한정)",
              r["ok"] and r.get("bound_scope") is None, r)
        check("따라서 다음 호출이 벽돌이 되지 않는다",
              write.resolve_session(VAULT, "repo/beta") is None)
        (ROOT / "00_Domain/D1/regr-dom.md").unlink(missing_ok=True)
    for root in ("00_Scope", "00_Person", "00_Domain"):
        rr = _w(write.create_node, VAULT, f"regr-root-{root[-2:]}", "루트 직속", "본문",
                "fable-5", space=root)
        check(f"Space 루트 직속 생성 거부: {root}", not rr["ok"], rr)


# ── 14l. 계약 밖 필드는 조용히 지워지지 않는다 (7차 경미 F) ──────────────
def test_extra_field_preserved():
    p = ROOT / "00_Scope/W1/regr-extra.md"
    p.write_text(node_text("260802-zzzz-rg70", "여분 필드", "본문",
                           "custom_field: 사용자가 손으로 넣음\n"), encoding="utf-8")
    try:
        before = p.read_bytes()
        r = _w(write.update_node, VAULT, "regr-extra", summary="고침")
        check("계약 밖 필드가 있으면 표면 수정을 거부", not r["ok"], r)
        check("거부했으므로 필드가 살아 있다", p.read_bytes() == before)
    finally:
        p.unlink(missing_ok=True)


# ── 14m. 순환 별칭은 원래 입력 키로 남는다 (7차 경미 D) ─────────────────
def test_alias_cycle_returns_input():
    VAULT.routing.unlink(missing_ok=True)
    write.alias_session(VAULT, "cX", "cY", "순환")
    write.alias_session(VAULT, "cY", "cX", "순환")
    check("순환에서 cX는 cX로 남는다", write.canonical_session(VAULT, "cX") == "cX",
          write.canonical_session(VAULT, "cX"))
    check("순환에서 cY는 cY로 남는다", write.canonical_session(VAULT, "cY") == "cY",
          write.canonical_session(VAULT, "cY"))


# ── 14n. 자기 자신과의 충돌은 성립하지 않는다 (7차 경미 G) ───────────────
def test_candidate_needs_distinct():
    VAULT.candidates.write_text("", encoding="utf-8")
    p = ROOT / "00_Scope/W1/regr-self.md"
    p.write_text(node_text("260802-zzzz-rg80", "자기 충돌"), encoding="utf-8")
    try:
        r = _w(write.record_candidate, VAULT, "duplication", ["regr-self", "regr-self"])
        check("같은 노드 둘은 거부", not r["ok"], r)
    finally:
        p.unlink(missing_ok=True)


# ── 14o. 엣지 표기 동일성 — 경로형과 스템형은 같은 대상 (8차 잔여 1) ────
def test_edge_target_normalization():
    """derived-from의 **노드** 근거는 제목으로 단다(Mechanism §8 2항, v3.7.3).

    구판은 반대였다 — id만 받고 이름·경로를 거부했다. 근거는 *"경로·이름은
    상태라 이동·개명에 끊어진다"*였는데, 이름에 대해서는 사실이 아니었다:
    `move_node`는 디렉토리만 바꾸고 이름 색인은 경로와 무관하다. **끊어지는
    것은 경로형 쪽**이므로 그것만 거부한다. 구형 id 표기는 계속 받는다."""
    r = _w(write.create_node, VAULT, "regr-norm-t", "대상", "본문", "fable-5",
           space="00_Scope/W1")
    tid = r["id"]
    bad = _w(write.create_node, VAULT, "regr-norm-x", "경로형 근거", "본문",
             "fable-5", space="00_Scope/W1",
             edges={"derived-from": "00_Scope/W1/regr-norm-t"})
    check("노드 근거의 경로 표기는 거부", not bad["ok"], bad)
    check("거부 사유가 제목을 요구한다",
          any("경로 표기는 쓰지 않는다" in v for v in bad.get("violations", [])), bad)
    (ROOT / "00_Scope/W1/regr-norm-x.md").unlink(missing_ok=True)

    ok0 = _w(write.create_node, VAULT, "regr-norm-n", "제목형 근거", "본문", "fable-5",
             space="00_Scope/W1", edges={"derived-from": "regr-norm-t"})
    check("제목형 노드 근거로 생성", ok0.get("ok"), ok0)
    saved = contract.parse(ROOT / "00_Scope/W1/regr-norm-n.md").meta.get("derived-from")
    check("제목은 위키링크로 저장된다", saved == "[[regr-norm-t]]", saved)
    (ROOT / "00_Scope/W1/regr-norm-n.md").unlink(missing_ok=True)

    r0 = _w(write.create_node, VAULT, "regr-norm", "표기 정규화", "본문",
            "fable-5", space="00_Scope/W1", edges={"derived-from": tid})
    check("구형 id 근거도 계속 받는다", r0["ok"], r0)
    r1 = _w(write.update_node, VAULT, "regr-norm", add_edges={"derived-from": tid})
    check("같은 id 추가는 중복 등재하지 않는다", r1.get("no_change") is True, r1)
    r2 = _w(write.update_node, VAULT, "regr-norm",
            add_edges={"derived-from": "[[= Governance/없는문서]]"})
    check("비노드 근거는 derived-from이 받지 않고 관측 절로 보낸다(헌법 제8조 5·6항)",
          not r2["ok"] and "# 관측" in " ".join(r2.get("violations", [])), r2)
    r3 = _w(write.update_node, VAULT, "regr-norm", remove_edges={"derived-from": tid})
    check("id 제거가 유효하다",
          r3["ok"] and tid not in (r3["edges"]["derived-from"] or []), r3)
    for nm in ("regr-norm", "regr-norm-t"):
        (ROOT / f"00_Scope/W1/{nm}.md").unlink(missing_ok=True)


# ── 14p. 무변경 update는 쓰지 않는다 (8차 권고) ─────────────────────────
def test_update_no_change():
    r = _w(write.create_node, VAULT, "regr-noop", "그대로", "본문", "fable-5",
           space="00_Scope/W1")
    check("전제: 생성", r["ok"], r)
    p_ = ROOT / "00_Scope/W1/regr-noop.md"
    before = p_.read_bytes()
    r1 = _w(write.update_node, VAULT, "regr-noop", summary="그대로")
    check("같은 summary는 no_change", r1.get("no_change") is True, r1)
    r2 = _w(write.update_node, VAULT, "regr-noop", body="본문",
            expect_hash=core.sha256_file(p_))
    check("같은 body도 no_change(CAS는 통과시키고 내용으로 판정)",
          r2.get("no_change") is True, r2)
    r2b = _w(write.update_node, VAULT, "regr-noop", body="본문")
    check("body를 주면 CAS가 no-op 판정보다 먼저다", not r2b["ok"], r2b)
    check("파일이 한 바이트도 바뀌지 않았다", p_.read_bytes() == before)
    check("no_change에도 현재 해시를 준다",
          r1["new_hash"] == core.sha256_file(p_), r1)
    r3 = _w(write.update_node, VAULT, "regr-noop", summary="달라짐")
    check("실제 변경은 통과", r3["ok"] and not r3.get("no_change"), r3)
    p_.unlink(missing_ok=True)


# ── 14q. bound_scope는 실제 결속했을 때만 (8차 잔여 2) ──────────────────
def test_bound_scope_honest():
    VAULT.routing.unlink(missing_ok=True)
    (ROOT / "00_Domain/D1").mkdir(parents=True, exist_ok=True)
    # 허브 노드를 골격으로 심는다 — 이 시험의 관심사는 결속이지 첫-노드
    # 규칙이 아니다(그 규칙은 test_cluster_overview가 소진한다)
    idx = ROOT / "00_Domain/D1/D1.md"
    if not idx.exists():
        idx.write_text(node_text("260802-zzzz-d1ix", "D1 허브"),
                       encoding="utf-8")
    r = _w(write.create_node, VAULT, "regr-bs-dom", "도메인", "본문", "fable-5",
           session="repo/gamma", space="00_Domain/D1")
    check("Domain 착지는 결속하지 않는다", r["ok"], r)
    check("따라서 bound_scope를 보고하지 않는다",
          r.get("bound_scope") is None, r)
    check("대장에도 결속이 없다", write.resolve_session(VAULT, "repo/gamma") is None)
    r2 = _w(write.create_node, VAULT, "regr-bs-sc", "스코프", "본문", "fable-5",
            session="repo/gamma", space="00_Scope/W1")
    check("scope 착지에서 결속하고 그때만 보고",
          r2["ok"] and r2.get("bound_scope") == "W1", r2)


# ── 14r. 프로젝트 결속은 집합, 주된 scope는 지정 (Mechanism §6-2 6항 · 시행령 §3 10항) ──
def test_project_bindings():
    """쓰기는 결속에 더할 뿐 주된 scope를 정하지 않는다. 주된 scope는 표시 기록이 정하고,
    v5.2 전의 결속 기록(`primary` 없음)은 주된 scope로 읽는다. 결속 밖 scope를 명시한
    쓰기는 거부되지 않는다 — 한 프로젝트가 여러 scope의 활동을 품는다. 인용 기록은
    루트 `_cited/` 한 자리라 어느 scope를 말해도 같은 기록이다."""
    from osk import raw, organization
    VAULT.routing.unlink(missing_ok=True)
    for n in ("WBindA", "WBindB", "WBindC", "WBindD", "WBindE"):
        d = ROOT / f"00_Scope/{n}"
        d.mkdir(exist_ok=True)
        hub = d / f"{n}.md"
        if not hub.exists():
            hub.write_text(node_text(f"260802-zzzz-{n[-1].lower()}bnd", f"{n} 허브"), encoding="utf-8")
    P = "repo/regr-project"
    # ① 첫 쓰기는 결속에 더할 뿐 주된 scope가 없다
    r = _w(write.create_node, VAULT, "regr-pb-1", "결속", "본문", "fable-5", session=P, space="00_Scope/WBindA")
    check("첫 쓰기가 결속에 더한다", r.get("ok") and r.get("bound_scope") == "WBindA", r)
    b = write.bindings(VAULT, P)
    check("결속 집합에 들었다", b["scopes"] == ["WBindA"], b)
    check("주된 scope는 아직 없다", b["primary"] is None and write.resolve_session(VAULT, P) is None, b)
    r = _w(write.create_node, VAULT, "regr-pb-2", "기본 착지", "본문", "fable-5", session=P)
    check("주된 scope 없이는 space를 요구한다",
          not r.get("ok") and "주된 scope가 없다" in " ".join(r.get("violations", [])), r)
    # ② 결속 밖 scope도 거부하지 않고 집합에 더한다
    r = _w(write.create_node, VAULT, "regr-pb-3", "다른 scope", "본문", "fable-5", session=P, space="00_Scope/WBindB")
    check("결속 밖 scope 쓰기는 통과하고 결속에 더한다", r.get("ok") and r.get("bound_scope") == "WBindB", r)
    check("집합은 순서대로", write.bindings(VAULT, P)["scopes"] == ["WBindA", "WBindB"], write.bindings(VAULT, P))
    r = _w(write.create_node, VAULT, "regr-pb-4", "같은 scope", "본문", "fable-5", session=P, space="00_Scope/WBindB")
    check("이미 결속된 scope는 새 결속으로 보고하지 않는다", r.get("ok") and r.get("bound_scope") is None, r)
    # ③ 주된 scope의 지정 — 그 뒤 space 없는 쓰기가 거기 간다
    write.set_primary(VAULT, P, "WBindA", "시험 지정")
    check("지정이 주된 scope를 세운다", write.resolve_session(VAULT, P) == "WBindA")
    r = _w(write.create_node, VAULT, "regr-pb-5", "기본 착지", "본문", "fable-5", session=P)
    check("space 없는 쓰기는 주된 scope로", r.get("ok") and "WBindA" in r["path"], r)
    # ④ v5.2 전의 결속 기록(`primary` 없음)은 주된 scope로 읽는다
    core.ledger_append(VAULT.routing, {"kind": "bind", "session": "repo/regr-legacy",
                                      "scope": "WBindC", "reason": "구판"})
    check("옛 결속은 주된 scope다", write.resolve_session(VAULT, "repo/regr-legacy") == "WBindC")
    # ④-2 옛 재결속은 교체였다 — 개명 전 scope가 집합에 되살아나지 않는다(`LLM-Second-Brain` 실측)
    L = "repo/regr-legacy-rebind"
    core.ledger_append(VAULT.routing, {"kind": "bind", "session": L, "scope": "WBindOld", "reason": "구판"})
    core.ledger_append(VAULT.routing, {"kind": "bind", "session": L, "scope": "WBindC", "reason": "구판 개명"})
    b = write.bindings(VAULT, L)
    check("옛 재결속은 집합을 바꾼다", b["scopes"] == ["WBindC"] and b["primary"] == "WBindC", b)
    write.bind_session(VAULT, L, "WBindD", "시험", primary=False)
    check("새 결속은 옛 결속 위에 더한다", write.bindings(VAULT, L)["scopes"] == ["WBindC", "WBindD"], write.bindings(VAULT, L))
    # ⑤ 해제는 집합에서 빼고, 주된 scope였으면 비운다
    write.unbind_session(VAULT, P, "WBindA", "시험 해제")
    b = write.bindings(VAULT, P)
    check("해제가 집합과 주된 scope를 비운다", b["scopes"] == ["WBindB"] and b["primary"] is None, b)
    # ⑥ 결속 검토 — 셋 초과·주된 scope 없음, 판정의 적용
    for n in ("WBindC", "WBindD", "WBindE"):
        _w(write.create_node, VAULT, f"regr-pb-{n}", "결속", "본문", "fable-5", session=P, space=f"00_Scope/{n}")
    audit = {a["project"]: a for a in organization.bindings_audit(VAULT)}
    check("결속 검토가 셋 초과와 주된 scope 없음을 싣는다",
          P in audit and {"over_limit", "no_primary"} <= set(audit[P]["flags"]), audit.get(P))
    check("옛 결속만 있는 프로젝트는 검토 대상이 아니다", "repo/regr-legacy" not in audit, sorted(audit))
    applied = organization.apply_binding_decisions(VAULT,
        [{"project": P, "action": "primary", "scope": "WBindB", "reason": "자체 활동"}])
    check("검토 판정이 주된 scope를 지정한다", bool(applied) and write.resolve_session(VAULT, P) == "WBindB", applied)
    check("지정 뒤에는 주된 scope 없음 표지가 사라진다",
          "no_primary" not in {f for a in organization.bindings_audit(VAULT, P) for f in a["flags"]})
    check("결속 검토 문구는 판정 넷을 말한다", "주된 scope 지정" in organization.bindings_prompt(VAULT, P))
    # ⑥-2 없는 scope(개명·삭제)에 결속된 프로젝트는 검토에 오르고, 그 해제는 받는다
    M = "repo/regr-missing"
    write.set_primary(VAULT, M, "WBindC", "시험 지정")
    write.bind_session(VAULT, M, "WBindGone", "개명 전 결속", primary=False)
    check("없는 scope 결속은 검토에 오른다",
          "missing_scope" in {f for a in organization.bindings_audit(VAULT, M) for f in a["flags"]},
          organization.bindings_audit(VAULT, M))
    gone = [{"project": M, "action": "unbind", "scope": "WBindGone", "reason": "개명된 scope"}]
    organization.apply_binding_decisions(VAULT, gone)
    check("없는 scope도 해제한다", write.bindings(VAULT, M)["scopes"] == ["WBindC"]
          and not organization.bindings_audit(VAULT, M), write.bindings(VAULT, M))
    organization.apply_binding_decisions(VAULT, gone)
    check("없는 scope 해제의 재시도는 멱등이다", write.bindings(VAULT, M)["scopes"] == ["WBindC"], write.bindings(VAULT, M))
    # ⑦ 인용 기록은 루트 한 자리 — 어느 scope를 말해도 같은 기록, 결속도 바뀌지 않는다
    rr = _w(raw.append_rounds, VAULT, P, "pb-rec", [{"user": "q", "agent": "a"}], "00_Scope/WBindA")
    check("인용 기록은 루트 `_cited/`에 선다", (rr.get("round_refs") or [""])[0].startswith("_cited/.records/"), rr)
    rr2 = _w(raw.append_rounds, VAULT, P, "pb-rec", [{"user": "q2", "agent": "a2"}], "00_Scope/WBindB")
    check("다른 scope를 말해도 같은 기록에 잇는다", rr2.get("indices") == [2], rr2)
    check("인용은 결속을 바꾸지 않는다",
          write.bindings(VAULT, P)["scopes"] == ["WBindB", "WBindC", "WBindD", "WBindE"], write.bindings(VAULT, P))
    # ⑧ 잘못된 판정 묶음은 통째로 거부된다 — 앞의 판정도 적용되지 않는다(#191 리뷰 P2)
    before = write.bindings(VAULT, P)
    try:
        organization.apply_binding_decisions(VAULT,
            [{"project": P, "action": "unbind", "scope": "WBindC", "reason": "x"},
             {"project": P, "action": "primary", "scope": "NoSuchScope", "reason": "x"}])
        check("잘못된 판정 묶음은 통째로 거부된다", False, "거부하지 않았다")
    except ValueError:
        check("잘못된 판정 묶음은 통째로 거부된다", write.bindings(VAULT, P) == before, write.bindings(VAULT, P))
    # ⑧-2 중간에 실패한 묶음은 같은 묶음의 재시도가 남은 판정부터 잇는다(#191 리뷰 3차)
    real_unbind, calls = routing.unbind_session, []

    def flaky(*a, **kw):
        calls.append(a)
        if len(calls) == 2:
            raise OSError("disk")
        return real_unbind(*a, **kw)
    batch = [{"project": P, "action": "unbind", "scope": s, "reason": "x"} for s in ("WBindD", "WBindE")]
    with mock.patch.object(routing, "unbind_session", flaky):
        try:
            organization.apply_binding_decisions(VAULT, batch)
            check("중간 실패는 예외로 드러난다", False, "예외가 없었다")
        except OSError:
            check("중간 실패는 예외로 드러난다 — 앞의 판정은 적용됐다",
                  write.bindings(VAULT, P)["scopes"] == ["WBindB", "WBindC", "WBindE"], write.bindings(VAULT, P))
    organization.apply_binding_decisions(VAULT, batch)
    check("같은 묶음의 재시도가 남은 판정만 적용한다",
          write.bindings(VAULT, P)["scopes"] == ["WBindB", "WBindC"], write.bindings(VAULT, P))
    # ⑨ 주된 scope 지정의 분기는 일반 결속이 해소하지 못한다(#191 리뷰 P1)
    F = "repo/regr-fork"
    a = write.set_primary(VAULT, F, "WBindA", "기기 A의 지정")
    ms = core.rid_parts(a["rid"])[0]

    def forged(rid_ms, parents, scope, primary):
        with open(VAULT.routing, "a", encoding="utf-8") as f:
            f.write(json.dumps({"rid": core._make_rid(rid_ms, 0), "parents": parents, "kind": "bind",
                                "session": F, "scope": scope, "primary": primary,
                                "at": core.now_iso()}) + "\n")
    forged(ms + 5, [], "WBindB", True)                 # 다른 기기의 지정 — 비교 불능 분기
    check("지정이 갈리면 미확정", write.bindings(VAULT, F)["undetermined"] and write.resolve_session(VAULT, F) is None)
    forged(ms + 10, [a["rid"], core._make_rid(ms + 5, 0)], "WBindC", False)   # 양쪽을 조상으로 둔 일반 결속
    b = write.bindings(VAULT, F)
    check("일반 결속은 지정의 분기를 해소하지 못한다",
          b["undetermined"] and b["primary"] is None and b["scopes"] == ["WBindA", "WBindB", "WBindC"], b)
    write.set_primary(VAULT, F, "WBindA", "사용자 지정")
    check("새 지정이 분기를 해소한다", write.resolve_session(VAULT, F) == "WBindA", write.bindings(VAULT, F))
    for nm, sp in (("regr-bs-dom", "00_Domain/D1"), ("regr-bs-sc", "00_Scope/W1")):
        (ROOT / sp / f"{nm}.md").unlink(missing_ok=True)


# ── 14t. 계약 검증은 목적지 경로로 한다 (8차 차단 ①) ────────────────────
def test_validate_uses_destination_path():
    """되읽은 노드가 임시 파일명을 들면 stem에 걸린 계약 규칙이 무력화된다 —
    표면이 자기 검증기가 위반이라 부르는 노드를 ok로 쓰게 된다."""
    r = _w(write.create_node, VAULT, "regr-selfref-w", "자기 참조", "본문",
           "fable-5", space="00_Scope/W1",
           edges={"derived-from": "regr-selfref-w"})
    check("자기 참조 derived-from을 쓰기 통로가 거부한다", not r["ok"], r)
    check("거부 사유가 계약 문언 그대로",
          any("자기 자신" in v for v in r["violations"]), r)
    check("거부했으므로 파일이 없다",
          not (ROOT / "00_Scope/W1/regr-selfref-w.md").exists())


# ── 14u. 동명 중복이면 쓰기를 거부한다 (8차 차단 ②) ─────────────────────
def test_dup_stem_write_refused():
    """읽기(허브)와 쓰기가 서로 다른 쪽을 고르면 본 파일과 고쳐지는 파일이
    달라진다. 표면은 임의로 한쪽을 택하지 않는다."""
    (ROOT / "00_Scope/W3").mkdir(parents=True, exist_ok=True)
    a = ROOT / "00_Scope/W1/regr-dup.md"
    b = ROOT / "00_Scope/W3/regr-dup.md"
    try:
        a.write_text(node_text("260806-aaaa-1111", "중복 A", "A 본문"), encoding="utf-8")
        b.write_text(node_text("260806-aaaa-3333", "중복 B", "B 본문"), encoding="utf-8")
        check("허브가 중복을 인지", "regr-dup" in graph.Index(VAULT).dup_stems)
        for label, call in (
            ("update", lambda: write.update_node(VAULT, "regr-dup", summary="x")),
            ("move", lambda: node_moves.move_node(VAULT, "regr-dup", "00_Scope/W2")),
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


# ── 14x. 엣지 값의 형은 통로가 거부한다 (10차 정정 ①: 스키마+런타임 이중) ──
def test_edge_value_type_refused():
    for bad in ({"derived-from": 42}, {"derived-from": [{"x": 1}]},
                {"derived-from": ""}, {"derived-from": [None]}):
        r = _w(write.create_node, VAULT, "regr-edgeval", "형 검사", "본문",
               "fable-5", space="00_Scope/W1", edges=bad)
        check(f"엣지 값 {bad} 거부", not r["ok"], r)
        check("거부했으므로 파일이 없다",
              not (ROOT / "00_Scope/W1/regr-edgeval.md").exists())
    # remove_edges도 add_edges와 같은 검사를 받는다(비대칭 제거)
    r0 = _w(write.create_node, VAULT, "regr-sym", "대칭", "본문", "fable-5",
            space="00_Scope/W1", edges={"derived-from": "대상A"})
    check("전제: 생성", r0["ok"], r0)
    for kw in ({"add_edges": {"suported-by": "X"}},
               {"remove_edges": {"suported-by": "X"}}):
        r = _w(write.update_node, VAULT, "regr-sym", **kw)
        check(f"술어 오타 거부: {list(kw)[0]}", not r["ok"], r)
        check("거부 사유에 쓸 수 있는 술어가 실린다",
              any("derived-from" in v for v in r["violations"]), r)
    (ROOT / "00_Scope/W1/regr-sym.md").unlink(missing_ok=True)


# ── 14y. 거부가 주소를 가르친다 (상주 0, 실패 시에만 지불) ───────────────
def test_refusal_teaches_address():
    r = _w(write.create_node, VAULT, "regr-addr", "주소", "본문", "fable-5",
           space="W1")     # 접두 빠뜨린 흔한 실수
    check("접두 없는 space 거부", not r["ok"], r)
    check("거부가 쓸 수 있는 군집을 열거한다",
          any("00_Scope/W1" in v for v in r["violations"]), r)
    r2 = _w(write.create_node, VAULT, "regr-addr", "주소", "본문", "fable-5",
            space="00_Scope/W1", edges={"conflicts": "아무거나"})
    check("conflicts 거부가 형식과 열린 사건을 알린다",
          not r2["ok"] and any("CASE-" in v and "열린 사건" in v
                               for v in r2["violations"]), r2)
    (ROOT / "00_Scope/W1/regr-addr.md").unlink(missing_ok=True)


# ── 14z. id도 핸들로 통한다 — 틀린 '노드 없음' 진단 제거 (10차 ②) ────────
def test_id_as_handle():
    r = _w(write.create_node, VAULT, "regr-idh", "핸들", "본문", "fable-5",
           space="00_Scope/W1")
    check("전제: 생성", r["ok"], r)
    nid = r["id"]
    r2 = _w(write.update_node, VAULT, nid, summary="id로 지목")
    check("응답의 id로 update가 된다", r2["ok"], r2)
    import mcp_server as M
    check("read_node도 id를 받는다", "error" not in M.read_node(nid),
          M.read_node(nid))
    (ROOT / "00_Scope/W1/regr-idh.md").unlink(missing_ok=True)


# ── 손잡이는 이름이다 (v3.7.5) ──────────────────────────────────────────
def test_name_is_the_handle():
    """모든 표면이 **이름을 돌려준다.** 읽은 뒤 그 노드를 근거로 달거나 고치려는
    호출자의 손에 이름이 남아야 한다.

    구판 `read_node`는 `id`만 돌려줬다. 그래서 새 엔진으로도 구형 id 표기
    근거가 계속 태어났다 — v3.7.4 직후 하루에 3간선. 스키마가 *"노드 근거는
    그 `id`로"*라고 **가르치고 있던** 것도 같은 뿌리다(검증은 통로에, 교육은
    스키마에 — 통로만 고치고 스키마를 두면 표기가 수렴하지 않는다).

    무엇을 망가뜨리면 실패하는가:
      · `read_node`가 `name`을 빼면 → 첫 단언이 실패
      · 돌려준 이름이 손잡이로 안 통하면 → 왕복 단언이 실패
    """
    import mcp_server as M
    r = _w(write.create_node, VAULT, "regr-handle", "손잡이", "본문", "fable-5",
           space="00_Scope/W1")
    try:
        check("전제: 생성", r.get("ok"), r)
        check("create_node가 이름을 돌려준다", r.get("name") == "regr-handle", r)
        _age_all()
        got = M.read_node("regr-handle")
        check("read_node가 이름을 돌려준다", got.get("name") == "regr-handle", got)
        check("id도 함께 온다(대장 동일성)", got.get("id") == r["id"], got)
        # 돌려준 이름이 그대로 손잡이가 된다 — 쓰기·근거 양쪽에서.
        r2 = _w(write.update_node, VAULT, got["name"], summary="바뀐 요약")
        check("돌려준 이름으로 고칠 수 있다", r2.get("ok"), r2)
        r3 = _w(write.create_node, VAULT, "regr-handle-src", "근거 단 노드", "본문",
                "fable-5", space="00_Scope/W1",
                edges={"derived-from": got["name"]})
        check("돌려준 이름으로 근거를 달 수 있다", r3.get("ok"), r3)
        check("근거가 제목 표기로 저장된다",
              contract.parse(ROOT / "00_Scope/W1/regr-handle-src.md")
              .meta.get("derived-from") == "[[regr-handle]]",
              r3.get("edges"))
        check("그 근거는 dangling이 아니다", not r3.get("dangling"), r3)
    finally:
        for nm in ("regr-handle", "regr-handle-src"):
            (ROOT / f"00_Scope/W1/{nm}.md").unlink(missing_ok=True)
        _age_all()


# ── 엣지 델타는 누적된 상태 위에서 계산된다 (v3.7.4) ────────────────────
def test_edge_delta_is_cumulative():
    """같은 술어에 `add_edges`와 `remove_edges`를 함께 주면 **둘 다** 반영된다.

    구판은 두 루프가 각자 `n.edges(pred)`로 **원본**을 다시 읽었다. remove가
    "add가 없었던 것처럼" 계산한 값으로 meta를 덮었고, 원본에 지울 것이 하나뿐
    이면 술어를 통째로 `pop`해 **방금 추가한 근거까지 사라졌다** — 그러면서
    `ok: true`를 냈다. "근거를 A에서 B로 바꾼다"는 이관·오타 수정·근거 갱신의
    자연스러운 표현이라 드문 경로가 아니다(v3.7.3 이관에서 두 번 걸렸다).

    그리고 `target_stem`이 위키링크 괄호를 벗긴다. 구판은 같은 노드가
    `[[N]]`·`N`·`N]]` 세 키로 갈려, 맨 이름으로 넣으면 중복 판정이 되고
    `[[이름]]`으로 넣으면 안 됐다.

    무엇을 망가뜨리면 실패하는가:
      · 두 루프 중 하나라도 `n.edges(pred)`로 되돌리면 → 첫 단언이 실패
      · `target_stem`의 괄호 벗기기를 지우면 → 괄호형 중복 단언이 실패
    """
    a = _w(write.create_node, VAULT, "regr-ed-a", "근거A", "본문", "fable-5",
           space="00_Scope/W1")
    _w(write.create_node, VAULT, "regr-ed-b", "근거B", "본문", "fable-5",
       space="00_Scope/W1")
    r = _w(write.create_node, VAULT, "regr-ed-src", "대상", "본문", "fable-5",
           space="00_Scope/W1", edges={"derived-from": a["id"]})
    p = ROOT / "00_Scope/W1/regr-ed-src.md"
    try:
        # 엔진은 id를 제목으로 적는다 — 구형 표기는 손으로 되돌려 만든다.
        p.write_text(p.read_text(encoding="utf-8").replace(
            'derived-from: "[[regr-ed-a]]"', f"derived-from: {a['id']}"), encoding="utf-8")
        _age_all()
        check("전제: 구형 id 근거 하나",
              r.get("ok") and contract.parse(p).meta.get("derived-from") == a["id"], r)
        # 지울 것이 **하나뿐**인 상태 — 구판이 술어를 통째로 지우던 자리다.
        r1 = _w(write.update_node, VAULT, "regr-ed-src",
                add_edges={"derived-from": "regr-ed-a"},
                remove_edges={"derived-from": a["id"]})
        got = contract.parse(p).meta.get("derived-from")
        check("한 호출 add+remove에서 근거가 살아남는다",
              got == "[[regr-ed-a]]", got)
        check("응답도 같은 것을 보고한다",
              r1.get("edges", {}).get("derived-from") == ["regr-ed-a"], r1)

        r2 = _w(write.update_node, VAULT, "regr-ed-src",
                add_edges={"derived-from": "regr-ed-b"},
                remove_edges={"derived-from": "regr-ed-a"})
        got = contract.parse(p).meta.get("derived-from")
        check("A→B 교체가 한 호출로 성립한다", got == "[[regr-ed-b]]", got)

        r3 = _w(write.update_node, VAULT, "regr-ed-src",
                add_edges={"derived-from": "[[regr-ed-b]]"})
        check("괄호형 입력도 중복으로 접힌다", r3.get("no_change") is True, r3)

        for s, want in (("[[N]]", "N"), ("N", "N"),
                        ("[[00_Scope/A/N]]", "N"), ("00_Scope/A/N.md", "N")):
            check(f"target_stem 접기: {s}", contract.target_stem(s) == want,
                  contract.target_stem(s))
        check("id는 접지 않는다",
              contract.target_stem("260802-114u-7lo3") == "260802-114u-7lo3")
    finally:
        for nm in ("regr-ed-src", "regr-ed-a", "regr-ed-b"):
            (ROOT / f"00_Scope/W1/{nm}.md").unlink(missing_ok=True)


# ── id의 폭과 판별자 (v3.7.3) ───────────────────────────────────────────
def test_id_width_and_discriminator():
    """새 `id`의 무작위부는 **8자**이고, 구형 4자 id는 그대로 유효하다
    (Mechanism §2 1항).

    담보가 검증에서 **폭**으로 옮겨갔으므로 폭 자체가 계약이다. 8자가
    아니면(예: 되돌려 4자로) 같은 초 183건에서 충돌이 1%로 올라간다.

    그리고 `ID_RE`는 유효성 검사가 아니라 **판별자**다 — "이 문자열은 id인가
    이름인가"를 여섯 자리에서 가른다. 넓힌 폭이 4|8 두 값에만 머무는지
    고정한다. 넓으면 평범한 노드 제목이 id로 읽힌다."""
    import re as _re
    r = _w(write.create_node, VAULT, "regr-idw", "폭", "본문", "fable-5",
           space="00_Scope/W1")
    try:
        check("생성이 성공한다", r.get("ok"), r)
        nid = r["id"]
        check("무작위부가 8자다", len(nid.split("-")[2]) == 8, nid)
        check("발급한 id가 판별자를 통과한다", _re.match(core.ID_RE, nid), nid)
        check("파일에 그 id가 적혔다",
              contract.parse(ROOT / r["path"]).id == nid)

        ok_forms = ["260802-e3k1-k7f2",            # 구형 4자 — 계속 유효
                    "260802-e3k1-k7f2m9x3"]        # 신형 8자
        for s in ok_forms:
            check(f"id로 판별한다: {s}", _re.match(core.ID_RE, s), s)
        no_forms = ["260802-e3k1-k7f",             # 3자
                    "260802-e3k1-k7f2m",           # 5자
                    "260802-e3k1-k7f2m9x3z",       # 9자
                    "260802-e3k1-k7f2m9x3k7f2",    # 12자
                    "260802-e3k1-K7F2",            # 대문자
                    "회의록 2026-08"]              # 평범한 제목
        for s in no_forms:
            check(f"id가 아니라고 판별한다: {s}", not _re.match(core.ID_RE, s), s)
    finally:
        (ROOT / "00_Scope/W1/regr-idw.md").unlink(missing_ok=True)


def test_write_edge_coordinates():
    """Non-node targets stored before v5.4 keep their exact coordinates: removing one
    round or source preserves every other, and none can be added any more."""
    from osk import raw
    base = ROOT / "00_Scope/W1/regr-write-coordinates"
    sources = ROOT / "_sources/regr-write-coordinates"
    raw_path = ROOT / "00_Scope/W1/_raw/regr-write-coordinates.md"
    prior_route = VAULT.routing.read_bytes() if VAULT.routing.exists() else None
    try:
        base.mkdir(parents=True)
        write.create_node(VAULT, base.name, "test hub", "hub", "fable-5",
                          space=base.relative_to(ROOT).as_posix())
        r = raw.append_rounds(VAULT, "regr-write-coordinates", raw_path.stem,
                              [{"user": "u1", "agent": "a1"},
                               {"user": "u2", "agent": "a2"}], space="00_Scope/W1")
        first, second = r["round_refs"]
        refs = []
        for folder in ("one", "two"):
            s = sources / folder / "evidence.md"
            s.parent.mkdir(parents=True)
            s.write_text("source material", encoding="utf-8")
            refs.append(f"[[{s.relative_to(ROOT).as_posix()}#section]]")
        p = base / "regr-coordinate-node.md"
        p.write_text(node_text("260802-zzzz-wcrd", body="claim",
                               extra=f"derived-from: {json.dumps([first, second] + refs)}\n"),
                     encoding="utf-8")
        refused = _w(write.update_node, VAULT, p.stem, add_edges={"derived-from": first})
        check("a stored round is not added again either", not refused.get("ok"), refused)
        write.update_node(VAULT, p.stem, remove_edges={"derived-from": f"[[{first}]]"})
        check("removing one round, in either notation, preserves the other",
              write.stored_edges(contract.parse(p).meta.get("derived-from")) == [second] + refs)
        write.update_node(VAULT, p.stem, remove_edges={"derived-from": refs[0]})
        check("same filename in different source paths remains distinct",
              write.stored_edges(contract.parse(p).meta.get("derived-from")) == [second, refs[1]])
        target = write.create_node(VAULT, "regr-coordinate-target", "target", "claim", "fable-5",
                                   space=base.relative_to(ROOT).as_posix())
        write.update_node(VAULT, p.stem, add_edges={"derived-from": target["name"]})
        write.update_node(VAULT, p.stem, add_edges={"derived-from": f"[[{target['name']}]]"})
        check("node title and wikilink still deduplicate",
              len(write.stored_edges(contract.parse(p).meta.get("derived-from"))) == 3)
    finally:
        rmtree_force(base)
        rmtree_force(sources)
        raw_path.unlink(missing_ok=True)
        if prior_route is None:
            VAULT.routing.unlink(missing_ok=True)
        else:
            VAULT.routing.write_bytes(prior_route)


def test_node_heading_edge_coordinates():
    """Node heading edges survive create/add/remove and independently trigger rechecks.

    Dropping the anchor in edge_key must fail both storage and propagation checks.
    """
    from osk import rechecks
    base = ROOT / "00_Scope/W1/regr-heading-edges"
    space = base.relative_to(ROOT).as_posix()
    before = rechecks.ledger_path(VAULT).read_bytes() if rechecks.ledger_path(VAULT).exists() else None
    try:
        base.mkdir(parents=True)
        write.create_node(VAULT, base.name, "hub", "hub", "fable-5", space=space)
        target = write.create_node(VAULT, "regr-heading-target", "source",
                                   "## A\n\nFirst result.\n\n## B\n\nSecond result.",
                                   "fable-5", space=space)
        a, b = (f"[[{target['name']}#{h}]]" for h in ("A", "B"))
        made = write.create_node(VAULT, "regr-heading-claim", "claim", "Uses both results.",
                                 "fable-5", space=space, edges={"derived-from": [a, b]})
        path = ROOT / made["path"]

        def stored():
            return write.stored_edges(contract.parse(path).meta.get("derived-from"))

        def pending():
            return {i["target"] for i in rechecks.candidates(VAULT, graph.Index(VAULT))[0]
                    if i["node"] == path.stem}

        check("creation preserves two headings of the same node", stored() == [a, b], stored())
        again = write.update_node(VAULT, path.stem, add_edges={"derived-from": [
            f"[[{target['name']}#A|first]]", f"[[{target['name']}.md#B]]"]})
        check("heading aliases and md suffix deduplicate only the same range",
              again.get("no_change") and stored() == [a, b], stored())
        write.update_node(VAULT, target["name"], old_text="Second result.", new_text="Second result revised.")
        check("changing only B schedules the node's B reference", pending() == {b}, pending())
        write.update_node(VAULT, path.stem, add_edges={"derived-from": b})
        check("re-adding B alone leaves its recheck open", pending() == {b}, pending())
        write.update_node(VAULT, path.stem, rechecked=[b])
        check("rechecked B closes its recheck and preserves A", not pending() and stored() == [a, b])
        write.update_node(VAULT, path.stem, remove_edges={"derived-from": b})
        check("removing B leaves A", stored() == [a], stored())
        write.update_node(VAULT, path.stem, add_edges={"derived-from": b})
        check("adding B beside A preserves both headings", stored() == [a, b], stored())
        write.update_node(VAULT, path.stem, add_edges={"derived-from": target["id"]})
        whole = f"[[{target['name']}]]"
        check("whole-node and heading references remain distinct", stored() == [a, b, whole], stored())
        write.update_node(VAULT, path.stem, remove_edges={"derived-from": target["id"]})
        check("removing the whole-node reference leaves both headings", stored() == [a, b], stored())
        write.update_node(VAULT, target["name"], old_text="First result.", new_text="First result revised.")
        check("changing only A schedules only A", pending() == {a}, pending())
        write.update_node(VAULT, path.stem, remove_edges={"derived-from": a}, add_edges={"derived-from": a})
        check("remove and readd A preserves B and leaves A open",
              stored() == [b, a] and pending() == {a}, (stored(), pending()))
        write.update_node(VAULT, path.stem, rechecked=[a])
        check("rechecked A closes it", not pending(), pending())
    finally:
        rmtree_force(base)
        if before is None:
            rechecks.ledger_path(VAULT).unlink(missing_ok=True)
        else:
            rechecks.ledger_path(VAULT).write_bytes(before)


def test_derived_from_once_per_round():
    """한 세션에서 같은 노드를 세 번 고쳐도 근거는 범위마다 한 번만 앉는다.

    실측(2026-09-25, v3.22.2): 한 세션에서 `update_node`를 세 번 받은 노드의 응답이
    `derived-from`에 같은 기록 경로를 세 번 실었다. 저장본은 그 기록의 서로 다른
    라운드 셋이었는데, 응답이 해석용 `Node.edges`(raw 앵커를 버린다)로 보고해 같은
    근거가 겹친 것처럼 보였다. 저장본의 진짜 중복은 생성이 만들었다 — 같은 근거를
    두 번 받으면 두 번 적었고(distill의 `sources`와 `edges`가 같은 라운드를 줄 때,
    실 인스턴스 3건), 갱신은 새로 더하는 것만 걸러 이미 앉은 중복을 남겼다.

    합치기의 키는 "이미 있는가"만 묻는다. 저장 목록을 키로 줄이면 키가 거친
    자리(URL의 `.md`, 노드의 절 앵커)마다 서로 다른 근거가 지워진다(PR #90 리뷰).
    v5.4부터 `derived-from`은 노드만 받는다(헌법 제8조 5항) — 더하는 근거는 한 노드의
    서로 다른 절이고, 라운드와 URL은 그 전에 저장된 근거로만 남는다.

    무엇을 망가뜨리면 실패하는가:
      · 응답 `edges`를 `Node.edges`로 되돌리면 → ①의 응답 단언
      · 갱신의 바이트 되풀이 접기를 지우면 → ②의 본문 쓰기·표기 변형 단언
      · 생성이 `_merge_edges`를 건너뛰면 → ③
      · `topology.reference_report`의 중복 보고를 지우면 → ②의 경고 단언
      · `edge_key`가 앵커를 버리면 → ①의 저장 단언(다른 절이 하나로 접힌다)
      · `edge_key`가 URL에서도 `.md`를 떼면 → ④의 URL 단언
      · `_merge_edges`가 저장 목록까지 키로 줄이면 → ④의 저장 근거 단언
    """
    from osk import raw
    base = ROOT / "00_Scope/W1/regr-edge-once"
    space = base.relative_to(ROOT).as_posix()
    prior_route = VAULT.routing.read_bytes() if VAULT.routing.exists() else None
    record = None

    def stored(p):
        return write.stored_edges(contract.parse(p).meta.get("derived-from"))

    def warned():
        return [d for d in topology.reference_report(VAULT, graph.Index(VAULT))["duplicate_edges"]
                if d.startswith("regr-edge-once")]
    try:
        base.mkdir(parents=True)
        write.create_node(VAULT, base.name, "test hub", "hub", "fable-5", space=space)
        r = raw.append_rounds(VAULT, "regr-edge-once", base.name,
                              [{"user": f"u{i}", "agent": f"a{i}"} for i in (1, 2, 3)],
                              space="00_Scope/W1")
        record = ROOT / r["path"]
        r1 = r["round_refs"][0]
        t = "regr-edge-once-t"
        write.create_node(VAULT, t, "t", "## s1\n\n가\n\n## s2\n\n나\n\n## s3\n\n다", "fable-5", space=space)
        h1, h2, h3 = (f"[[{t}#s{i}]]" for i in (1, 2, 3))

        # ① 관측 경로 — 한 세션에서 세 번 고치며 매번 한 노드의 다른 절을 근거로 단다.
        #    앞서 단 절을 다른 표기로 되풀어 달아도 같은 근거다.
        a = base / "regr-edge-once-a.md"
        write.create_node(VAULT, a.stem, "a", "claim", "fable-5", space=space)
        for i, refs in enumerate(([h1], [h2, h1], [h3, f"{t}#s2"]), 1):
            out = write.update_node(VAULT, a.stem, summary=f"a{i}",
                                    add_edges={"derived-from": refs})
        check("세 번 고쳐도 절마다 한 번만 저장된다", stored(a) == [h1, h2, h3], stored(a))
        check("응답도 절 앵커째 한 번씩 보고한다",
              sorted(out["edges"]["derived-from"]) == [f"{t}#s{i}" for i in (1, 2, 3)],
              out.get("edges"))

        # ② 이미 저장된 중복(구판의 생성·손 편집이 남긴 것)은 다음 쓰기에서 접힌다.
        b = base / "regr-edge-once-b.md"
        b.write_text(node_text("260802-zzzz-once", body="claim",
                               extra=f'derived-from: ["{h1}", "{h1}", "{h2}"]\n'),
                     encoding="utf-8")
        check("검증기가 저장된 중복을 경고한다",
              warned() == [f"regr-edge-once-b [derived-from] → {h1}"], warned())
        write.update_node(VAULT, b.stem, old_text="claim", new_text="claim v2")
        check("본문만 고치는 쓰기에서도 중복이 접힌다", stored(b) == [h1, h2], stored(b))
        write.update_node(VAULT, b.stem, add_edges={"derived-from": [h3, h1]})
        check("접힌 뒤의 추가도 한 번씩만 앉는다", stored(b) == [h1, h2, h3], stored(b))
        check("접힌 뒤에는 경고가 없다", warned() == [], warned())
        #    표기만 다른 중복은 추가가 정규 표기로 다시 적은 뒤 바이트 비교로 접힌다.
        v = base / "regr-edge-once-v.md"
        v.write_text(node_text("260802-zzzz-oncv", body="claim",
                               extra=f'derived-from: ["{h1}", "{t}#s1"]\n'),
                     encoding="utf-8")
        write.update_node(VAULT, v.stem, add_edges={"derived-from": h2})
        check("표기만 다른 중복이 접혀도 새 근거는 앉는다", stored(v) == [h1, h2], stored(v))

        # ③ 생성도 같은 합치기를 지난다.
        c = base / "regr-edge-once-c.md"
        write.create_node(VAULT, c.stem, "c", "claim", "fable-5", space=space,
                          edges={"derived-from": [h1, f"{t}#s1", h1]})
        check("생성도 같은 근거를 한 번만 적는다", stored(c) == [h1], stored(c))

        # ④ 서로 다른 근거는 합치지 않는다. URL은 확장자까지 주소이고, 저장된 근거는
        #    키로 지우지 않는다. URL과 라운드는 v5.4 전에 저장된 근거다.
        code = "https://github.com/o/r/blob/abc/Makefile"
        doc = code + ".md"
        u = base / "regr-edge-once-u.md"
        u.write_text(node_text("260802-zzzz-oncu", body="claim",
                               extra=f'derived-from: ["[[{code}]]", "[[{doc}]]"]\n'),
                     encoding="utf-8")
        write.update_node(VAULT, u.stem, remove_edges={"derived-from": code})
        check("한 URL을 빼도 확장자만 다른 URL은 남는다", stored(u) == [f"[[{doc}]]"], stored(u))
        refused = _w(write.update_node, VAULT, u.stem, add_edges={"derived-from": code})
        check("URL은 다시 더하지 않는다 — 근거 절로 간다", not refused.get("ok"), refused)
        s = base / "regr-edge-once-s.md"
        kept = [r1, f"[[{code}]]", f"[[{doc}]]", f"[[{a.stem}#s1]]", f"[[{a.stem}#s2]]"]
        s.write_text(node_text("260802-zzzz-oncs", body="claim",
                               extra=f"derived-from: {json.dumps(kept)}\n"),
                     encoding="utf-8")
        write.update_node(VAULT, s.stem, add_edges={"derived-from": h3})
        check("근거를 더해도 저장된 근거는 키로 지워지지 않는다(라운드·URL 둘·절 앵커 둘)",
              stored(s) == kept + [h3], stored(s))
    finally:
        rmtree_force(base)
        if record is not None:
            record.unlink(missing_ok=True)
        if prior_route is None:
            VAULT.routing.unlink(missing_ok=True)
        else:
            VAULT.routing.write_bytes(prior_route)


def test_write_pin_subtree_and_fork():
    """Cluster moves preserve contained pins; unresolved pin is not unpin."""
    from osk import approvals
    base = ROOT / "00_Scope/W1/regr-write-pins"
    saved = {p: p.read_bytes() if p.exists() else None
             for p in (VAULT.pins, approvals.moves_path(VAULT))}
    try:
        # Isolate the pin scenarios from pins intentionally left by earlier fixtures.
        VAULT.pins.write_bytes(b"")
        dirs = [base, base / "regr-pin-A", base / "regr-pin-B",
                base / "regr-pin-A/regr-pin-child"]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            write.create_node(VAULT, d.name, "test hub", "hub", "fable-5",
                              space=d.relative_to(ROOT).as_posix())
        src, dest = dirs[1], dirs[2]
        n = write.create_node(VAULT, "regr-pin-contained", "test node", "claim", "fable-5",
                              space=src.relative_to(ROOT).as_posix())
        def refused():
            result = _w(node_moves.move_cluster, VAULT, src.name, dest.relative_to(ROOT).as_posix())
            return result.get("ok") is False and "pin" in str(result) and src.is_dir()
        for target in (n["id"], dirs[3].relative_to(ROOT).as_posix() + "/"):
            core.ledger_append(VAULT.pins, {"kind": "pin", "target": target})
            check("containing cluster respects pin " + target, refused())
            core.ledger_append(VAULT.pins, {"kind": "unpin", "target": target})
        parent = core.ledger_read(VAULT.pins)[-1]["rid"]
        last = parent
        with VAULT.pins.open("a", encoding="utf-8") as f:
            for _ in range(2):
                last = core._next_rid(last)
                f.write(json.dumps({"rid": last, "parents": [parent], "kind": "pin",
                                    "target": n["id"], "at": core.now_iso()}) + "\n")
        check("valid union-merged pin branches are unresolved",
              not core.ledger_damage(core.ledger_read(VAULT.pins)) and
              core.resolve_one(core.ledger_read(VAULT.pins), n["id"], "target") is None)
        direct = _w(node_moves.move_nodes, VAULT, [n["name"]], dest.relative_to(ROOT).as_posix())
        check("unresolved pin blocks a direct move", direct.get("ok") is False and "pin" in str(direct))
        check("unresolved pin blocks a containing-cluster move", refused())
        core.ledger_append(VAULT.pins, {"kind": "unpin", "target": n["id"]})
        clean_pins = VAULT.pins.read_bytes()
        VAULT.pins.write_bytes(clean_pins + clean_pins.splitlines(keepends=True)[0])
        check("structurally damaged pins block automatic movement", refused())
        VAULT.pins.write_bytes(clean_pins)
        moved = node_moves.move_cluster(VAULT, src.name, dest.relative_to(ROOT).as_posix())
        check("a resolved unpin permits the cluster move",
              moved["ok"] and (dest / src.name / (n["name"] + ".md")).is_file())
    finally:
        rmtree_force(base)
        for p, data in saved.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)
