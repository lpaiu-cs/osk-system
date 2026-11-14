"""회귀 수트의 한 갈래: 군집과 자리 — 허브, 하위 군집, 이동, 자리 규칙.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, shutil

from regression.support import _age_all, check, node_text, ROOT, _unhang, VAULT, _w
from osk import cli, core, graph, validate, contract, write, cluster_gate, node_moves, placement  # noqa: E402


# ── 22. 군집 허브 노드 (시행령 §3 6항 · Mechanism §6-1) ────────────────────
def test_cluster_overview():
    """각 군집은 동명 허브 노드를 두고, 전 노드가 허브에서 **Link의 방향**을
    따라 도달 가능해야 한다(헌법 3조 8항). 도달은 허브가 가리키는 것으로
    성립하며, 역링크도 `derived-from`도 도달을 만들지 않는다. 저작 신설의 첫 노드는 허브 강제, 이동 형성은
    검증기 보고. 활성화 전에는 보고만 한다(시행령 §11 2항·3항)."""
    from osk import validate

    # 저작 신설: 1차는 신설 관문, 2차는 첫-노드 규칙이 허브를 요구한다
    r = _w(write.create_node, VAULT, "허브 아닌 첫 노드", "s", "본문", "fable-5",
           space="00_Scope/OVW")
    check("신설 1차는 관문 거부", r.get("ok") is False, r)
    r2 = _w(write.create_node, VAULT, "허브 아닌 첫 노드", "s", "본문", "fable-5",
            space="00_Scope/OVW")
    check("신설 2차는 첫-노드 규칙 거부", r2.get("ok") is False, r2)
    v = " ".join(r2.get("violations", []))
    check("거부가 허브 노드를 지목", "허브 노드" in v and "OVW" in v, v)
    check("거부는 노드를 남기지 않는다",
          not (ROOT / "00_Scope/OVW/허브 아닌 첫 노드.md").exists())
    # 허브가 갈래 머리들을 **미리** 참조한다 — 아직 없는 대상은 dangling
    # 경고일 뿐 거부가 아니다(탐색 링크는 자유).
    r3 = _w(write.create_node, VAULT, "OVW", "OVW 군집 허브",
            "갈래: [[OVW-head]] · [[OVW-a]]", "fable-5", space="00_Scope/OVW")
    check("동명 허브 노드는 첫 노드로 통과", r3.get("ok"), r3)

    # 방향 도달 — 간선은 **Link만**이다. derived-from은 도달을 만들지 않는다.
    r4 = _w(write.create_node, VAULT, "OVW-섬", "s", "아무도 가리키지 않는 섬.",
            "fable-5", space="00_Scope/OVW")
    check("허브가 선 뒤 일반 노드 통과", r4.get("ok"), r4)
    r5 = _w(write.create_node, VAULT, "OVW-조상", "s", "갈래의 처음.",
            "fable-5", space="00_Scope/OVW")
    rh = _w(write.create_node, VAULT, "OVW-head", "s", "갈래 머리.", "fable-5",
            space="00_Scope/OVW", edges={"derived-from": r5["id"]})
    check("머리 생성(조상을 derived-from)", rh.get("ok"), rh)
    r6 = _w(write.create_node, VAULT, "OVW-역링크", "s", "[[OVW]]만 가리킨다.",
            "fable-5", space="00_Scope/OVW")
    check("역링크 노드 생성", r6.get("ok"), r6)
    # 순환 안전 — 허브에 닿는 고리(a↔b)와 닿지 않는 고리(c↔d) 둘 다 종료
    ra = _w(write.create_node, VAULT, "OVW-a", "s", "[[OVW-b]]를 본다.",
            "fable-5", space="00_Scope/OVW")
    rb = _w(write.create_node, VAULT, "OVW-b", "s", "[[OVW-a]]를 본다.",
            "fable-5", space="00_Scope/OVW")
    rc = _w(write.create_node, VAULT, "OVW-c", "s", "[[OVW-d]]를 본다.",
            "fable-5", space="00_Scope/OVW")
    rd = _w(write.create_node, VAULT, "OVW-d", "s", "[[OVW-c]]를 본다.",
            "fable-5", space="00_Scope/OVW")
    check("순환쌍 생성", all(x.get("ok") for x in (ra, rb, rc, rd)),
          (ra, rb, rc, rd))
    # 일반 쓰기는 노드를 허브에 건다(시행령 §3 8항). 미리 가리킨 머리에는 줄을 더하지 않는다.
    check("일반 쓰기는 허브에 걸린다", r4.get("placed") == ["OVW"], r4)
    hub = ROOT / "00_Scope/OVW/OVW.md"
    check("미리 가리킨 노드에는 줄을 더하지 않는다",
          contract.parse(hub).body.count("[[OVW-head]]") == 1)
    # 아래 도달 판정은 허브가 머리와 a만 가리키는 상태를 전제로 한다 — 허브를 처음 본문으로
    # 되돌린다. 걸리지 않은 노드는 이제 허브 편집으로 생긴다.
    rr = _w(write.update_node, VAULT, "OVW", body="갈래: [[OVW-head]] · [[OVW-a]]",
            expect_hash=core.sha256_file(hub))
    check("전제: 허브를 처음 본문으로 되돌린다", rr.get("ok"), rr)
    co = validate.cluster_overview_report(VAULT, graph.Index(VAULT))
    st = co.get("00_Scope/OVW")
    check("검사가 군집을 본다", st is not None, co)
    check("허브 존재 인식", st and st["overview"] is True, st)
    orphans = set(st.get("orphans", []))
    check("허브→머리 직접 도달", "OVW-head" not in orphans, st)
    # v3.6.0 — 도달은 Link만 센다. 근거는 조건부인데 도달은 필수이므로
    # 필수를 조건부 위에 얹지 않는다(헌법 3조 8항 · Mechanism §6-1 3항).
    # 이 항목은 v3.5.0에서 정반대였다 — 그때는 derived-from이 도달을 날랐다.
    check("derived-from만으로는 도달하지 않는다 — 조상은 고아",
          "OVW-조상" in orphans, st)
    check("허브에 닿는 고리는 순회가 완주한다(무한루프 없음)",
          not {"OVW-a", "OVW-b"} & orphans, st)
    check("역링크만으로는 고아 — 방향이 반대다", "OVW-역링크" in orphans, st)
    check("섬은 고아", "OVW-섬" in orphans, st)
    check("닿지 않는 고리는 둘 다 고아(여기서도 종료)",
          {"OVW-c", "OVW-d"} <= orphans, st)
    check("미도달 수가 고아 목록과 일치",
          st["unreachable"] == len(orphans) == 5, st)

    # 허브 노드는 군집 밖으로 이동 불가
    r7 = _w(node_moves.move_node, VAULT, "OVW", "00_Scope/W1")
    check("허브 노드 이동 거부", r7.get("ok") is False, r7)
    check("이동 거부가 결박을 설명",
          "허브 노드" in " ".join(r7.get("violations", [])), r7)

    # 활성화 게이트 — 기본 비활성(보고만), 활성화 후 verdict 산입, 해제 원복
    check("기본 비활성", validate.validator_active(VAULT, "cluster-overview") is False)
    rep = validate.run(VAULT, cli.published_tools)
    check("비활성이면 verdict 불산입(그 이유의 fail 없음)",
          not any("군집 허브 노드" in f for d in rep["fail"] for f in d), rep["fail"])
    check("보고는 언제나 실린다", "00_Scope/OVW" in rep.get("cluster_overview", {}))
    core.ledger_append(VAULT.validators,
                       {"kind": "activate", "rule": "cluster-overview"})
    check("활성 판정", validate.validator_active(VAULT, "cluster-overview") is True)
    rep2 = validate.run(VAULT, cli.published_tools)
    check("활성이면 위반이 verdict에 산입", rep2["verdict"] == "FAIL"
          and any("군집 허브 노드" in f for d in rep2["fail"] for f in d), rep2["verdict"])
    core.ledger_append(VAULT.validators,
                       {"kind": "deactivate", "rule": "cluster-overview"})
    check("해제 원복", validate.validator_active(VAULT, "cluster-overview") is False)


# ── 20. 군집 신설의 2단계 확인 (Mechanism §6-2 3항) ─────────────────────
def test_new_cluster_two_phase():
    """구판은 "군집 신설은 사용자 발의다"라며 전면 거부했으나 그 문구는 규범
    무근거였다 — 헌법은 형성의 자동화를 기본으로 둔다(5조 4항·6조 9항).
    현행 관문은 한 번 묻는다: 1차 거부가 신설임을 알리고, 같은 군집에 대한
    재시도는 통과한다. 선의의 에이전트가 오류 원인을 읽고 사용자 허락을
    확인한 뒤 같은 요청을 다시 보내는 한 왕복이 전부다."""
    import time as _time
    r = _w(write.create_node, VAULT, "이상 신설 시험", "s", "본문", "fable-5",
           space="00_Scope/W2P")
    check("새 군집 1차는 거부", r.get("ok") is False, r)
    v = " ".join(r.get("violations", []))
    check("거부가 신설임을 알린다", "새 군집" in v, v)
    check("거부가 재시도 절차를 알려준다", "그대로 다시" in v, v)
    check("1차 거부는 디렉토리를 만들지 않는다",
          not (ROOT / "00_Scope/W2P").is_dir())
    # 재시도는 관문을 지나되, 빈 군집의 첫 노드는 허브가어야 한다(§3 6항) —
    # 두 관문은 겹치지 않고 이어진다: 신설 확인 → 허브 먼저.
    r2 = _w(write.create_node, VAULT, "이상 신설 시험", "s", "본문", "fable-5",
            space="00_Scope/W2P")
    check("재시도는 관문을 지나 첫-노드 규칙에 닿는다",
          r2.get("ok") is False
          and "허브 노드" in " ".join(r2["violations"]), r2)
    check("관문 통과로 군집 디렉토리는 만들어졌다",
          (ROOT / "00_Scope/W2P").is_dir())
    r2b = _w(write.create_node, VAULT, "W2P", "W2P 허브", "[[이상 신설 시험]]",
             "fable-5", space="00_Scope/W2P")
    check("허브 노드가 첫 노드로 통과", r2b.get("ok"), r2b)
    r3 = _w(write.create_node, VAULT, "이상 신설 시험", "s", "본문", "fable-5",
            space="00_Scope/W2P")
    check("허브가 선 군집은 관문 없이 통과", r3.get("ok"), r3)
    r4 = _w(node_moves.move_node, VAULT, "이상 신설 시험", "00_Domain/D2P")
    check("move의 새 군집도 1차 거부", r4.get("ok") is False, r4)
    r5 = _w(node_moves.move_node, VAULT, "이상 신설 시험", "00_Domain/D2P")
    check("move 재시도는 통과 — 이동 형성은 허브 강제가 없다(§3 1항 보호)",
          r5.get("ok"), r5)
    # v3.8.0 — 하위 군집 신설이 열렸다. 분화는 시행령 §3 7항이 권장하는 정상
    # 행위이고, 구판은 그것을 표면으로 수행할 길이 없었다. 다만 **허브가 있는
    # 군집 안**이어야 하고, 하위 허브가 첫 노드여야 한다.
    r6 = _w(write.create_node, VAULT, "깊은 경로", "s", "본문", "fable-5",
            space="00_Scope/W2P/sub")
    check("하위 군집도 첫 노드는 허브여야 한다",
          r6.get("ok") is False
          and "허브 노드" in " ".join(r6["violations"]), r6)
    r6b = _w(write.create_node, VAULT, "sub", "하위 허브", "본문", "fable-5",
             space="00_Scope/W2P/sub")
    check("하위 군집 신설은 2단계 관문 없이 한 번에",
          r6b.get("ok"), r6b)
    check("하위 허브가 그 자리에 섰다",
          (ROOT / "00_Scope/W2P/sub/sub.md").is_file())
    r6c = _w(write.create_node, VAULT, "더 깊은 경로", "s", "본문", "fable-5",
             space="00_Scope/W2P/없는군집/더깊이")
    check("허브 없는 폴더 안으로는 분화할 수 없다",
          r6c.get("ok") is False
          and "허브가 있는 군집 안" in " ".join(r6c["violations"]), r6c)
    r7 = _w(write.create_node, VAULT, "예약명 군집", "s", "본문", "fable-5",
            space="00_Scope/COM1")
    check("군집 이름도 이식성 규칙을 받는다",
          r7.get("ok") is False and "예약 장치명" in " ".join(r7["violations"]), r7)
    check("부적격 신설은 디렉토리를 남기지 않는다",
          not (ROOT / "00_Scope/COM1").is_dir())
    # 만료 — 잊힌 표식이 뒷날의 다른 요청을 무확인 통과시키지 않는다
    cluster_gate._ack_file(VAULT).write_text(
        json.dumps({"00_Scope/WStale2P": _time.time() - 7200}), encoding="utf-8")
    r8 = _w(write.create_node, VAULT, "WStale2P", "허브", "본문", "fable-5",
            space="00_Scope/WStale2P")
    check("만료된 표식은 다시 1차 거부", r8.get("ok") is False, r8)
    r9 = _w(write.create_node, VAULT, "WStale2P", "허브", "본문", "fable-5",
            space="00_Scope/WStale2P")
    check("만료 후 새 왕복은 통과(허브 제목이라 첫-노드 규칙도 충족)",
          r9.get("ok"), r9)


# ── 하위 군집: 허브는 자리다 (v3.8.0) ───────────────────────────────────
def test_nested_clusters():
    """하위 군집은 **폴더**이고, 그 허브는 **폴더와 동명인 노드**다.

    이 판정이 서기 전에는 검증기가 최상위 허브만 알아 층 사이의 도달을 전혀
    검사하지 못했다 — 부모 허브가 하위 허브를 안 가리켜도, 떠난 노드를 계속
    가리켜도 미직결이 0이었다(폴더별로 따로 셌기 때문). 헌법 3조 8항이
    요구하는 것은 *"최상위 허브에서 하위 허브를 거쳐"* 닿는 것이다.

    허브는 노드의 종이 아니라 자리이므로 frontmatter에 표식이 붙지 않는다
    (시행령 §3 7항). 배치가 정본이라는 원칙에서 읽어 내는 것이다.

    무엇을 망가뜨리면 실패하는가:
      · `cluster_overview_report`를 폴더별 묶음으로 되돌리면 → 층 단언이 실패
      · `placement.is_hub`가 stem/parent 비교를 놓으면 → 허브 판정 전부가 실패
      · 하위 신설에 2단계 관문을 붙이면 → 한-번-에 단언이 실패
    """
    import osk.core as C
    base = ROOT / "00_Scope/W1"
    made = []
    try:
        def L(ns):
            return "개요\n\n" + "\n".join(f"- [[{n}]]" for n in ns) + "\n"
        r = _w(write.create_node, VAULT, "regr-nc", "갈래", L(["regr-nc-a"]),
               "fable-5", space="00_Scope/W1/regr-nc")
        check("하위 군집 신설은 한 번에(2단계 관문 없음)", r.get("ok"), r)
        made.append(base / "regr-nc")
        check("새 하위 허브는 위 허브에 걸린다(시행령 §3 8항)",
              r.get("placed") == ["W1"], r)
        check("허브가 폴더와 동명으로 앉는다",
              (base / "regr-nc/regr-nc.md").is_file())
        check("is_hub가 그것을 허브로 본다",
              placement.is_hub(base / "regr-nc/regr-nc.md"))
        check("is_hub가 평범한 노드는 허브로 보지 않는다",
              not placement.is_hub(base / "regr-nc/regr-nc-a.md"))
        for nm in ("regr-nc-a", "regr-nc-b"):
            rn = _w(write.create_node, VAULT, nm, "요", "본문", "fable-5",
                    space="00_Scope/W1/regr-nc")
            check(f"{nm}은 하위 허브부터 최상위 허브까지 걸린다",
                  rn.get("placed") == ["regr-nc", "W1"], rn)
        # 최상위가 하위 허브를 가리키지 않는 상태는 허브 편집으로 생긴다
        check("전제: 최상위에서 하위 허브 줄을 뺀다", _unhang("W1", "regr-nc").get("ok"))

        rep = validate.cluster_overview_report(VAULT, graph.Index(VAULT))
        check("묶음은 최상위 군집이다 — 하위 폴더가 따로 서지 않는다",
              "00_Scope/W1/regr-nc" not in rep, sorted(rep))
        st = rep["00_Scope/W1"]
        check("최상위가 하위 허브를 안 가리키면 그 아래가 통째로 미직결",
              {"regr-nc", "regr-nc-a"} <= set(st.get("orphans") or []),
              st.get("orphans"))

        # 층을 가로지른다 — 최상위가 하위 허브를 가리키면 **그 아래까지** 산다.
        # mini-vault의 W1 허브는 공유물이므로 본문을 되돌린다.
        top = ROOT / "00_Scope/W1/W1.md"
        top_body = contract.parse(top).body
        _w(write.update_node, VAULT, "W1", body=top_body + "\n- [[regr-nc]]\n",
           expect_hash=C.sha256_file(top))
        st = validate.cluster_overview_report(VAULT, graph.Index(VAULT))["00_Scope/W1"]
        check("하위 허브를 가리키니 하위 허브가 산다",
              "regr-nc" not in (st.get("orphans") or []), st.get("orphans"))
        check("그 아래 노드도 층을 거쳐 산다 — 이것이 구판이 못 보던 것",
              "regr-nc-a" not in (st.get("orphans") or []), st.get("orphans"))
        _w(write.update_node, VAULT, "W1", body=top_body,
           expect_hash=C.sha256_file(top))

        # 허브 없는 폴더는 자리로 보고된다.
        (base / "regr-nc-hl").mkdir(exist_ok=True)
        made.append(base / "regr-nc-hl")
        _w(node_moves.move_nodes, VAULT, ["regr-nc-b"], "00_Scope/W1/regr-nc-hl")
        st = validate.cluster_overview_report(VAULT, graph.Index(VAULT))["00_Scope/W1"]
        check("허브 없는 하위 폴더를 hubless로 지목한다",
              "00_Scope/W1/regr-nc-hl" in (st.get("hubless") or []), st)
    finally:
        for d in made:
            shutil.rmtree(d, ignore_errors=True)
        _age_all()


def test_plain_write_placement():
    """일반 쓰기는 노드를 같은 작업 안에서 자리에 건다(시행령 §3 8항) — 착지 군집의 허브부터
    최상위 허브까지 각 층이 아래 고리를 가리키게 한다. 허브 쓰기가 실패하면 노드는 남고, 아직
    아래 고리를 가리키지 않는 허브만 `hub_links`로 알린다. 반증 가능성:
      · `write._place`를 빼면 → placed 단언이 전부 실패
      · 실패 보고가 이은 층까지 내면 → 층 사이 실패의 hub_links 단언이 실패
      · Workbench에도 걸면 → transit 단언이 실패
    """
    from unittest import mock
    import osk.core as C
    top = ROOT / "00_Scope/W1/W1.md"
    top_body = contract.parse(top).body
    sub = ROOT / "00_Scope/W1/regr-pl"
    made = []
    real = write.update_node_locked

    def refuse(vault, name, *a, **kw):
        if name == "W1":
            raise write.WriteError("계약·위상 위반 — 쓰지 않았다", ["시험: 허브 쓰기 거부"])
        return real(vault, name, *a, **kw)

    try:
        # 결속된 세션이 space 없이 쓰면 scope 뿌리에 앉고 최상위 허브에 걸린다
        r = _w(write.create_node, VAULT, "regr-pl-bind", "자리", "본문", "fable-5",
               session="regr-pl", space="00_Scope/W1")
        made.append(ROOT / "00_Scope/W1/regr-pl-bind.md")
        check("전제: 결속 쓰기", r.get("ok"), r)
        write.set_primary(VAULT, "regr-pl", "W1", "시험 지정")
        r = _w(write.create_node, VAULT, "regr-pl-root", "자리", "본문", "fable-5",
               session="regr-pl")
        made.append(ROOT / "00_Scope/W1/regr-pl-root.md")
        check("space 없는 결속 쓰기는 scope 뿌리에 앉는다",
              r.get("path") == "00_Scope/W1/regr-pl-root.md", r)
        check("그 노드는 최상위 허브에 걸린다", r.get("placed") == ["W1"], r)
        check("허브 끝에 Link 줄이 더해진다",
              contract.parse(top).body.rstrip().endswith("- [[regr-pl-root]]"))

        # 허브 쓰기가 실패하면 노드는 남고, 그 허브를 hub_links로 알린다
        with mock.patch.object(write, "update_node_locked", side_effect=refuse):
            r = _w(write.create_node, VAULT, "regr-pl-fail", "자리", "본문", "fable-5",
                   space="00_Scope/W1")
        made.append(ROOT / "00_Scope/W1/regr-pl-fail.md")
        check("허브 쓰기가 실패해도 노드는 남는다",
              r.get("ok") and (ROOT / "00_Scope/W1/regr-pl-fail.md").is_file(), r)
        check("못 이은 허브를 hub_links로 알린다",
              r.get("hub_links") == [{"hub": "W1", "add": ["regr-pl-fail"]}], r)
        check("실패 사유를 싣는다",
              "시험: 허브 쓰기 거부" in (r.get("placement_error") or []), r)
        check("실패하면 placed를 싣지 않는다", "placed" not in r, r)

        # 층 사이에서 멈추면 이은 층은 빼고 못 이은 층만 알린다
        r = _w(write.create_node, VAULT, "regr-pl", "갈래", "개요", "fable-5",
               space="00_Scope/W1/regr-pl")
        made.append(sub)
        check("새 하위 허브는 위 허브에 걸린다", r.get("placed") == ["W1"], r)
        check("전제: 최상위에서 하위 허브 줄을 뺀다", _unhang("W1", "regr-pl").get("ok"))
        with mock.patch.object(write, "update_node_locked", side_effect=refuse):
            r = _w(write.create_node, VAULT, "regr-pl-a", "자리", "본문", "fable-5",
                   space="00_Scope/W1/regr-pl")
        check("하위 허브는 이었다",
              "[[regr-pl-a]]" in contract.parse(sub / "regr-pl.md").body)
        check("최상위만 못 이은 층으로 알린다",
              r.get("hub_links") == [{"hub": "W1", "add": ["regr-pl"]}], r)

        # Workbench는 자체 계약이라 걸지 않는다
        r = _w(write.create_node, VAULT, "regr-pl-wb", "자리", "본문", "fable-5",
               space="00_Scope/Workbench/transit")
        made.append(ROOT / "00_Scope/Workbench/transit/regr-pl-wb.md")
        check("전제: transit 쓰기", r.get("ok"), r)
        check("Workbench에는 자리를 걸지 않는다",
              "placed" not in r and "placement_error" not in r, r)
    finally:
        for p in made:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        _w(write.update_node, VAULT, "W1", body=top_body, expect_hash=C.sha256_file(top))
        _age_all()


def test_move_nodes_and_cluster():
    """`move_nodes`는 **전부 아니면 전무**이고, `move_cluster`는 폴더째 옮긴다.

    분화는 갈래를 골라 내리는 일(N개 지정)이고 재편은 통째로 옮기는 일이다 —
    성격이 달라 표면이 둘이다. 재편은 폴더가 가므로 **하위의 하위가 공짜로
    따라간다.**

    무엇을 망가뜨리면 실패하는가:
      · 검사 전에 옮기기 시작하면 → 전무 단언이 실패
      · `move_cluster`가 최상위를 건너게 하면 → 거부 단언이 실패
      · `_stale_hub_links`를 지우면 → 안내 단언이 실패
    """
    base = ROOT / "00_Scope/W1"
    made = []
    try:
        for sub in ("regr-mv", "regr-mv2"):
            _w(write.create_node, VAULT, sub, "갈래", "개요\n", "fable-5",
               space=f"00_Scope/W1/{sub}")
            made.append(base / sub)
        for nm in ("regr-mv-a", "regr-mv-b"):
            _w(write.create_node, VAULT, nm, "요", "본문", "fable-5",
               space="00_Scope/W1/regr-mv")
        hub = base / "regr-mv/regr-mv.md"
        import osk.core as C
        _w(write.update_node, VAULT, "regr-mv",
           body="개요\n\n- [[regr-mv-a]]\n- [[regr-mv-b]]\n",
           expect_hash=C.sha256_file(hub))

        # 전부-아니면-전무 — 허브를 섞으면 아무것도 안 옮긴다.
        bad = _w(node_moves.move_nodes, VAULT, ["regr-mv-a", "regr-mv"],
                 "00_Scope/W1/regr-mv2")
        check("허브가 섞이면 거부", bad.get("ok") is False, bad)
        check("거부했으므로 아무것도 안 옮겼다",
              (base / "regr-mv/regr-mv-a.md").is_file())

        r = _w(node_moves.move_nodes, VAULT, ["regr-mv-a", "regr-mv-b"],
               "00_Scope/W1/regr-mv2")
        check("묶음 이동", r.get("ok") and r["count"] == 2, r)
        # 안내는 **양쪽**이어야 한다. 출발지만 알리면 그 안내를 따른 호출자가
        # 링크를 지우고 도착지에서 고아를 만든다(표면 감사 실측).
        hl = r["hub_links"]
        check("출발지 허브에서 뺄 것을 알린다",
              any(set(h.get("remove") or []) == {"regr-mv-a", "regr-mv-b"}
                  for h in hl), hl)
        check("도착지 허브에 더할 것을 알린다",
              any(set(h.get("add") or []) == {"regr-mv-a", "regr-mv-b"}
                  for h in hl), hl)
        check("허브 손잡이는 제목이다 — 경로가 아니라",
              all("/" not in h["hub"] for h in hl), hl)
        dup = _w(node_moves.move_nodes, VAULT, ["regr-mv-a", "regr-mv-a"], "00_Scope/W1/regr-mv")
        check("같은 노드를 두 번 옮길 수 없다", dup.get("ok") is False, dup)

        # 재편 — 폴더째, 그리고 하위의 하위가 따라간다.
        _w(write.create_node, VAULT, "regr-mv-deep", "갈래", "개요\n", "fable-5",
           space="00_Scope/W1/regr-mv2/regr-mv-deep")
        rc = _w(node_moves.move_cluster, VAULT, "regr-mv2", "00_Scope/W1/regr-mv")
        check("군집째 이동", rc.get("ok"), rc)
        check("하위의 하위가 따라왔다",
              (base / "regr-mv/regr-mv2/regr-mv-deep/regr-mv-deep.md").is_file())
        check("노드 수를 보고한다", rc["nodes"] >= 4, rc)

        # `WriteError`는 violations가 있으면 메시지를 그쪽에 싣는다 — 단언은
        # 실제로 오는 자리에 건다(v3.7.0에서 값을 치르고 배운 것).
        bad2 = _w(node_moves.move_cluster, VAULT, "regr-mv2", "00_Person/Module")
        check("최상위 군집을 건널 수 없다",
              bad2.get("ok") is False
              and any("증류" in v for v in bad2.get("violations", [])), bad2)
        bad3 = _w(node_moves.move_cluster, VAULT, "regr-mv-a", "00_Scope/W1/regr-mv")
        check("허브가 아니면 군집째 옮길 수 없다",
              bad3.get("ok") is False and "군집이 아니다" in str(bad3), bad3)

        # 노드는 scope를 건널 수 있다 — 막지 않고 **알린다.** 침묵하면
        # 호출자가 ok:true를 받고 검증기 FAIL로 알게 된다(표면 감사 실측).
        cs = _w(node_moves.move_nodes, VAULT, ["regr-mv-a"], "00_Person/Module")
        check("노드의 scope 건너기는 허용된다", cs.get("ok"), cs)
        check("건넜다는 사실을 알린다",
              cs.get("crossed_scope", {}).get("nodes") == ["regr-mv-a"], cs)
        check("왜 위험한지와 무엇을 하라를 함께 준다",
              "hub_links" in cs["crossed_scope"]["why"], cs["crossed_scope"])
        # 되돌아오는 것도 건너는 이동이다(Person → Scope). 같은 최상위 안의
        # 이동을 보려면 W1 안에서 옮겨야 한다.
        back = _w(node_moves.move_nodes, VAULT, ["regr-mv-a"], "00_Scope/W1/regr-mv")
        check("되돌아오는 것도 건너는 이동이다",
              back.get("crossed_scope", {}).get("nodes") == ["regr-mv-a"], back)
        same = _w(node_moves.move_nodes, VAULT, ["regr-mv-a"], "00_Scope/W1/regr-mv2")
        check("같은 최상위 안에서는 알리지 않는다",
              "crossed_scope" not in same, same)
    finally:
        for d in made:
            shutil.rmtree(d, ignore_errors=True)
        _age_all()


# ── 이동은 참조 위상을 새로 깨지 않는다 (v4.0.0) ─────────────────────────
def test_move_topology_refused():
    """노드를 옮기면 소속이 바뀌어 **그 노드의 나가고 들어오는 참조**의 판정이
    바뀐다. 이동이 새로 만드는 위반은 쓰기 전에 거부한다.

    구판은 scope를 건너는 `move_nodes`에 `ok`와 허브 안내만 냈다. 검증기는
    FAIL(scope 간 Link·derived-from, Domain의 `_raw` 근거)이었고 그 노드들의
    무관한 다음 쓰기가 거부됐다 — 같은 간선을 `create_node`는 처음부터 거부한다
    (2026-09-24 재현).

    무엇을 망가뜨리면 실패하는가:
      · `_move_topology` 호출을 지우면 → 첫 두 거부 단언
      · 들어오는 참조 후보(`mentioning`)를 빼면 → `regr-mt2 →` 단언
      · 함께 옮기는 노드를 이동 후 소속으로 보지 않으면 → 묶음 이동 단언
      · 이미 있던 위반까지 세면 → 마지막 단언(설계 D10)
    """
    w1 = ROOT / "00_Scope/W1"
    mt, mx, md = (ROOT / "00_Scope/regr-MT", ROOT / "00_Scope/regr-MX",
                  ROOT / "00_Domain/regr-MD")
    files = {
        "regr-mt1": node_text("260925-rgmt-0001", body="곁가지 [[regr-mt2]]"),
        "regr-mt2": node_text("260925-rgmt-0002", body="위에 선다 [[regr-mt1]]",
                              extra="derived-from: 260925-rgmt-0001\n"),
        "regr-mt3": node_text("260925-rgmt-0003", body="원료에서",
                              extra='derived-from: "[[00_Scope/W1/_raw/regr-mt#1]]"\n'),
        "regr-mt4": node_text("260925-rgmt-0004", body="참조 없음"),
        "regr-mt6": node_text("260925-rgmt-0006", body="경로형 [[00_Scope/W1/regr-mt7]]"),
        "regr-mt7": node_text("260925-rgmt-0007", body="대상"),
        "regr-mt8": node_text("260925-rgmt-0008",
                              body="원료에서\n\n## 근거\n\n- 2026-09-25 · `c38d8ef2#1` · 지지 — 관측"),
    }
    try:
        for d in (mt, mx, md):
            d.mkdir(parents=True, exist_ok=True)
        for stem, text in files.items():
            (w1 / f"{stem}.md").write_text(text, encoding="utf-8")
        _age_all()
        r1 = _w(node_moves.move_nodes, VAULT, ["regr-mt1"], "00_Scope/regr-MT")
        v1 = " | ".join(r1.get("violations") or [])
        check("scope를 건너 참조를 끊는 이동은 거부한다",
              r1.get("ok") is False and "regr-mt1 → regr-mt2" in v1, r1)
        check("들어오는 참조(Link·derived-from)도 짚는다",
              "regr-mt2 → regr-mt1" in v1 and "regr-mt2 → 260925-rgmt-0001" in v1, v1)
        check("고칠 길을 준다", "remove_edges" in v1 and "names" in v1, v1)
        check("아무것도 옮기지 않았다", (w1 / "regr-mt1.md").is_file())
        r2 = _w(node_moves.move_nodes, VAULT, ["regr-mt3"], "00_Domain/regr-MD")
        check("인용 기록 근거를 단 노드는 Domain으로 못 간다",
              r2.get("ok") is False and "Domain의 인용 기록" in str(r2)
              and (w1 / "regr-mt3.md").is_file(), r2)
        r8 = _w(node_moves.move_nodes, VAULT, ["regr-mt8"], "00_Domain/regr-MD")
        check("근거 절에 인용 라운드를 단 노드도 Domain으로 못 간다",
              r8.get("ok") is False and "(근거 절)" in str(r8)
              and (w1 / "regr-mt8.md").is_file(), r8)
        r3 = _w(node_moves.move_nodes, VAULT, ["regr-mt1", "regr-mt2"], "00_Scope/regr-MT")
        check("서로 잇는 노드는 함께 옮기면 된다", r3.get("ok"), r3)
        r4 = _w(node_moves.move_nodes, VAULT, ["regr-mt4"], "00_Domain/regr-MD")
        check("참조 없는 노드의 건너기는 그대로 된다", r4.get("ok"), r4)
        # 이미 있던 위반은 이 이동의 몫이 아니다(설계 D10)
        (w1 / "regr-mt5.md").write_text(
            node_text("260925-rgmt-0005", body="남의 scope [[regr-mt1]]"), encoding="utf-8")
        _age_all()
        r5 = _w(node_moves.move_nodes, VAULT, ["regr-mt5"], "00_Scope/regr-MX")
        check("이미 있던 위반은 이동을 막지 않는다", r5.get("ok"), r5)
        # 경로형 Link는 옛 자리를 가리킨다 — 함께 옮기면 끊긴 참조이지 위반이 아니다
        r6 = _w(node_moves.move_nodes, VAULT, ["regr-mt6", "regr-mt7"], "00_Scope/regr-MX")
        check("함께 옮기는 노드 사이의 경로형 Link는 이동을 막지 않는다",
              r6.get("ok"), r6)
    finally:
        for stem in (*files, "regr-mt5"):
            (w1 / f"{stem}.md").unlink(missing_ok=True)
        for d in (mt, mx, md):
            shutil.rmtree(d, ignore_errors=True)
        _age_all()


# ── 노드 자리의 한 판정 (v4.0.0) ─────────────────────────────────────────
def test_node_place_rule():
    """색인·보호영역·조망·배치 검증기·쓰기 관문이 **같은 자리 규칙**을 쓰는가.

    구판은 소비자마다 따로 보았다. 보호영역은 점 접두를 빼는데 색인은 넣어서,
    승인되지 않은 `.hidden.md`가 기억으로 읽히고 고쳐지는 동안 영역은 clean이었다.
    밑줄 접두 군집(`_misc`)은 표면이 만들어 주고, 조망은 숨기면서 노드 수에는
    세고, 검색은 찾고, `_priv`는 scope가 됐다(Mechanism §1 4항 — 밑줄 접두
    구획에는 노드를 두지 않는다). `.md` 대소문자는 Windows에서만 무시해 같은
    트리가 기기마다 다른 노드 집합이었다.

    무엇을 망가뜨리면 실패하는가:
      · `placement.space_of_parts`의 `_off_node` 판정을 지우면 → 점·밑줄 구획의
        파일이 색인·scope 목록에 돌아오고 배치 검증기가 조용해진다
      · `cluster_gate.new_cluster_gate`의 자리 검사를 지우면 → `_misc` 신설이 확인 한 번으로
        통과한다(디렉토리가 남는다)
      · `move_cluster`의 자리 검사를 지우면 → 이미 있는 `_` 구획 안으로 군집이 간다
      · `placement.is_md`를 호스트에 묶으면 → 대소문자 구분 호스트에서 `.MD` 노드가 빠진다
    """
    made = []
    D, S = VAULT.domain, VAULT.scope
    try:
        # (a) 점 접두 — 파일 자체든 디렉토리든 노드 자리가 아니다.
        hid = ROOT / S / "W1/.regr-hidden.md"
        inner = ROOT / S / "W1/.regr-notes/regr-dot-inner.md"
        inner.parent.mkdir(parents=True, exist_ok=True)
        made += [hid, inner.parent]
        hid.write_text(node_text("260802-dot0-0001", "숨은 노드", "HIDDEN"),
                       encoding="utf-8")
        inner.write_text(node_text("260802-dot0-0002", "숨은 폴더", "INNER"),
                         encoding="utf-8")
        i = graph.Index(VAULT)
        check("점 접두 파일·디렉토리의 .md는 색인에 들지 않는다",
              ".regr-hidden" not in i.names and "regr-dot-inner" not in i.names,
              sorted(n for n in i.names if "regr-" in n))
        r = _w(write.update_node, VAULT, "regr-dot-inner", old_text="INNER",
               new_text="EDITED")
        check("쓰기 통로도 그 파일을 잡지 못한다",
              r.get("ok") is False and "INNER" in inner.read_text(encoding="utf-8"),
              r)
        r = _w(write.create_node, VAULT, "regr-dot-new", "s", "b", "fable-5",
               space=f"{S}/W1/.regr-notes")
        check("점 접두 구획으로의 생성은 거부된다",
              r.get("ok") is False
              and not (inner.parent / "regr-dot-new.md").exists(), r)
        lv = placement.layout_violations(VAULT)
        check("배치 검증기가 점 접두 구획의 노드형 파일을 보고한다",
              sum(".regr-hidden.md" in v or "regr-dot-inner.md" in v
                  for v in lv) == 2, lv)
        check("`_raw` 안의 점 구획은 그대로 raw다",
              placement.space_of(VAULT, ROOT / S / "W1/_raw/.records/x.txt")[0] == "raw")

        # (b) 밑줄 접두 디렉토리 — 표면이 새로 만들지 못한다.
        ack_before = set(cluster_gate._read_ack(VAULT))
        for space in (f"{D}/_regr-misc", f"{S}/W1/_regr-sub", f"{S}/_regr-new"):
            hub = space.rsplit("/", 1)[1]
            rs = [_w(write.create_node, VAULT, hub, "허브", "본문", "fable-5",
                     space=space)]
            # 표식은 **1차 뒤에** 본다 — 재시도가 표식을 소비하므로 두 번 뒤에는
            # 남긴 적이 있어도 보이지 않는다.
            acked = space in set(cluster_gate._read_ack(VAULT)) - ack_before
            rs.append(_w(write.create_node, VAULT, hub, "허브", "본문", "fable-5",
                         space=space))
            check(f"밑줄 구획 신설은 확인을 거쳐도 거부된다: {space}",
                  all(x.get("ok") is False for x in rs), rs)
            check(f"거부가 디렉토리도 확인 표식도 남기지 않는다: {space}",
                  not (ROOT / space).exists() and not acked)
            shutil.rmtree(ROOT / space, ignore_errors=True)
        # 구판이 이미 만들어 둔 밑줄 구획 — 읽지도 쓰지도 않고 보고한다.
        priv = ROOT / S / "_regr-priv"
        sub = ROOT / S / "W1/_regr-sub2"
        for d in (priv, sub):
            d.mkdir(parents=True, exist_ok=True)
            made.append(d)
        (priv / "_regr-priv.md").write_text(
            node_text("260802-und0-0001", "밑줄 scope 허브", "PRIV"),
            encoding="utf-8")
        (sub / "_regr-sub2.md").write_text(
            node_text("260802-und0-0002", "밑줄 하위 허브", "SUB"),
            encoding="utf-8")
        i = graph.Index(VAULT)
        check("밑줄 구획의 노드형 파일은 색인에 들지 않는다",
              "_regr-priv" not in i.names and "_regr-sub2" not in i.names)
        check("밑줄 구획은 scope가 아니다",
              "_regr-priv" not in placement.scope_names(VAULT), placement.scope_names(VAULT))
        check("밑줄 구획은 조망의 군집이 아니다",
              not any("_regr-" in c for c in cluster_gate.cluster_names(VAULT)))
        lv = placement.layout_violations(VAULT)
        check("배치 검증기가 밑줄 구획의 노드형 파일을 보고한다",
              sum("_regr-priv.md" in v or "_regr-sub2.md" in v
                  for v in lv) == 2, lv)
        r = _w(write.create_node, VAULT, "regr-und-leaf", "s", "b", "fable-5",
               space=f"{S}/_regr-priv")
        check("이미 있는 밑줄 구획으로의 생성도 거부된다",
              r.get("ok") is False
              and not (priv / "regr-und-leaf.md").exists(), r)
        check("밑줄로 시작하는 **파일 이름**은 여전히 노드 자리다",
              placement.is_node_home(placement.space_of_parts(VAULT, (D, "X", "_a.md"))))
        # 군집째 옮기기도 같은 판정을 받는다.
        mc = ROOT / S / "W1/regr-mc"
        mc.mkdir(parents=True, exist_ok=True)
        made.append(mc)
        (mc / "regr-mc.md").write_text(
            node_text("260802-und0-0003", "옮길 하위 허브", "MC"),
            encoding="utf-8")
        r = _w(node_moves.move_cluster, VAULT, "regr-mc", f"{S}/W1/_regr-sub2")
        check("밑줄 구획 안으로는 군집을 옮기지 못한다",
              r.get("ok") is False and (mc / "regr-mc.md").is_file()
              and not (sub / "regr-mc").exists(), r)

        # (c) `.md` 대소문자는 호스트가 아니라 이름이 정한다. 구판이 대소문자
        # 구분 호스트(macOS·Linux)에서 계산하던 값을 씌워도 결과가 같아야 한다.
        had = hasattr(graph, "_MD_CASE_INSENSITIVE")
        saved = getattr(graph, "_MD_CASE_INSENSITIVE", None)
        up = ROOT / S / "W1/regr-Host.MD"
        made.append(up)
        up.write_text(node_text("260802-case-0001", "대문자 확장자", "본문"),
                      encoding="utf-8")
        graph._MD_CASE_INSENSITIVE = False
        try:
            check("대소문자 구분 호스트에서도 `.MD`는 노드다",
                  placement.is_md("Note.MD") and placement.is_md("Note.Md")
                  and "regr-Host" in graph.Index(VAULT).names)
        finally:
            if had:
                graph._MD_CASE_INSENSITIVE = saved
            else:
                del graph._MD_CASE_INSENSITIVE
    finally:
        for p in made:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def test_upgrading_underscore_scope():
    """docs/UPGRADING의 밑줄 구획 이행 절차가 v4에서 실제로 닫히는가. v3가 만들어
    둔 `_` scope와 그 결속·scope 기억을, 폴더·허브·기억 파일을 밑줄 없이 옮기고
    `write.bind_session` 한 줄로 결속을 새 행으로 바꿔 옮긴다.

    무엇을 망가뜨리면 실패하는가:
      · `bind_session`이 이미 결속된 키에 새 결속을 적지 못하면 → 옮긴 뒤 쓰기 단언
      · 문서의 호출 모양 `write.bind_session(VAULT, <키>, <scope>, <사유>)`가 바뀌면 → 같은 단언
      · scope 기억 자리가 문서의 `_scope_memory/<scope>.md`와 달라지면 → 기억 단언"""
    from osk import scope_memory
    S = VAULT.scope
    key = "regr-upgrade-key"
    old, new = ROOT / S / "_regr-legacy", ROOT / S / "regr-legacy"
    mem = ROOT / S / "Workbench/_scope_memory"
    mem_old, mem_new = mem / "_regr-legacy.md", mem / "regr-legacy.md"
    try:
        old.mkdir(parents=True)
        (old / "_regr-legacy.md").write_text(
            node_text("260926-upg0-0001", "옛 scope 허브", "HUB"), encoding="utf-8")
        mem.mkdir(parents=True, exist_ok=True)
        mem_old.write_text("- 옛 scope의 기억.", encoding="utf-8")
        write.bind_session(VAULT, key, "_regr-legacy", "v3가 만든 결속")
        _age_all()
        r = _w(write.create_node, VAULT, "regr-upgrade-a", "s", "b", "fable-5", session=key)
        check("v4는 밑줄 scope에 결속된 세션의 쓰기를 거부한다",
              r.get("ok") is False and not (old / "regr-upgrade-a.md").exists(), r)
        # UPGRADING: 폴더·허브·기억 파일을 밑줄 없이 옮기고 결속을 새 행으로 바꾼다
        old.rename(new)
        (new / "_regr-legacy.md").rename(new / "regr-legacy.md")
        mem_old.rename(mem_new)
        write.bind_session(VAULT, key, "regr-legacy", "v4 upgrade")
        _age_all()
        r = _w(write.create_node, VAULT, "regr-upgrade-a", "s", "b", "fable-5", session=key)
        check("옮긴 뒤 같은 세션의 쓰기가 새 scope에 앉는다",
              r.get("ok") and (new / "regr-upgrade-a.md").exists(), r)
        m = _w(scope_memory.read, VAULT, key)
        check("옮긴 scope 기억을 같은 세션이 읽는다",
              m.get("ok") and "옛 scope의 기억" in m.get("text", ""), m)
        check("옮긴 폴더에는 배치 위반이 없다",
              not [v for v in placement.layout_violations(VAULT) if "regr-legacy" in v],
              placement.layout_violations(VAULT))
    finally:
        shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(new, ignore_errors=True)
        for p in (mem_old, mem_new):
            p.unlink(missing_ok=True)
        _age_all()


def test_upgrading_protected_scope():
    """docs/UPGRADING의 보호영역 개명 절차가 v4에서 닫히는가 (PR #103 리뷰). 보호
    지정은 경로(`region`)에 묶여 이동을 따라가지 않는다. 먼저 옮기면 옛 영역은 승인도
    해제도 되지 않는 pending이 되고 새 경로는 보호 밖이다. 문서대로 해제 → 이동 →
    곧바로 새 경로 지정을 하면 새 경로가 보호되고 옛 영역은 해제로 닫히며, 그 뒤에
    고친 링크는 새 영역의 변경집합으로 검토된다.

    무엇을 망가뜨리면 실패하는가:
      · 보호가 이동을 따라가게 바뀌면 → 첫 단언(문서가 적은 까닭이 사실이 아니게 된다)
      · 해제나 재지정이 빠지거나 성립하지 않으면 → 둘째 단언"""
    from osk import approvals, approval_acts
    S = VAULT.scope
    old, new = ROOT / S / "_regr-guarded", ROOT / S / "regr-guarded"
    reg_old, reg_new = f"{S}/_regr-guarded", f"{S}/regr-guarded"
    kept = approvals.ledger_path(VAULT).read_bytes() if approvals.ledger_path(VAULT).exists() else None

    def move(src, dst):
        src.rename(dst)
        (dst / f"{src.name}.md").rename(dst / f"{dst.name}.md")

    try:
        old.mkdir(parents=True)
        (old / "_regr-guarded.md").write_text(
            node_text("260926-upg0-0002", "보호된 옛 scope 허브", "HUB"), encoding="utf-8")
        approval_acts.protect(VAULT, reg_old, "v3에서 지정")
        move(old, new)
        check("해제 없이 옮기면 옛 영역은 pending에 묶이고 새 경로는 보호 밖이다",
              approvals.state(VAULT, reg_old) == "pending"
              and approvals.region_of(VAULT, new / "regr-guarded.md") is None,
              (approvals.state(VAULT, reg_old), approvals.region_of(VAULT, new / "regr-guarded.md")))
        # UPGRADING: 되돌린 뒤 해제 → 이동 → 새 경로 지정
        move(new, old)
        approval_acts.unprotect(VAULT, reg_old, "v4 upgrade")
        move(old, new)
        approval_acts.protect(VAULT, reg_new, "v4 upgrade")
        check("문서대로 옮기면 새 경로가 보호되고 옛 영역은 해제로 닫힌다",
              approvals.state(VAULT, reg_new) == "clean"
              and approvals.state(VAULT, reg_old) == "unprotected"
              and approvals.region_of(VAULT, new / "regr-guarded.md") == reg_new,
              (approvals.state(VAULT, reg_new), approvals.state(VAULT, reg_old)))
        hub = new / "regr-guarded.md"
        hub.write_text(hub.read_text(encoding="utf-8") + "고친 링크\n", encoding="utf-8")
        check("다시 지정한 뒤 고친 것은 새 영역의 변경집합이 된다",
              approvals.state(VAULT, reg_new) == "pending", approvals.state(VAULT, reg_new))
    finally:
        if kept is None:
            approvals.ledger_path(VAULT).unlink(missing_ok=True)
        else:
            approvals.ledger_path(VAULT).write_bytes(kept)
        shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(new, ignore_errors=True)
        _age_all()
