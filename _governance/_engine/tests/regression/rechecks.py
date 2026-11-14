"""회귀 수트의 한 갈래: 근거 재검토와 읽기의 캐시.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import os, subprocess, tempfile
from pathlib import Path
from unittest import mock

from regression.support import _age_all, check, node_text, _raises, rmtree_force, ROOT, skip, VAULT, _vault_at, _w
from osk import core, graph, contract, write, node_moves, placement  # noqa: E402


def test_rechecks():
    """근거의 상태가 바뀌면 참조 노드가 재검토 후보가 되고, 점검 완료는 기록된다
    (시행령 §7 2·3항 · Mechanism §4-1 · §8 4항).

    무엇을 망가뜨리면 실패하는가:
      · 생성이 `bound`를 적지 않으면 → 생성 직후 완료 단언
      · 판정이 대상 상태를 안 보면 → 근거 변경 후보 단언
      · `rechecked`에 적은 근거를 닫지 않으면 → unchanged·updated 단언
      · 근거를 `add_edges`로 다시 넣기만 해도 닫으면 → 배선만 다시 넣은 단언
      · 읽거나 보여 준 기록이 없는 쪽을 거절하면 → 기록 없음 단언
      · 이어 적기가 없으면 → 노드만 고친 뒤 완료 단언
      · 판정이 노드 상태를 안 보면 → 엔진 밖 변경 후보 단언
      · 제목 범위가 다음 동급 제목에서 끊기지 않으면 → 다른 절 변경 단언
      · 중복 제목을 해석하면 → 해석 불능 단언
      · 쓰기가 기준선보다 제 기록을 먼저 적으면 → 기준선 단언
      · 상태를 파일 전체로 재면 → 요약·배선만 바뀐 근거·노드 단언
      · 본문 경계가 판독과 어긋나면 → `parse_bytes` 대조 단언"""
    from osk import rechecks
    W = ROOT / "00_Scope/W1"
    src = ROOT / "_sources" / "regr-rc.md"
    made = [W / f"regr-rc-{x}.md" for x in ("T", "N", "H", "Old", "New", "W", "S", "R", "Hub", "Src")]
    st = lambda f: rechecks.state(f.read_bytes())
    led = rechecks.ledger_path(VAULT)
    lbefore = led.read_bytes() if led.exists() else None
    hub = W / "W1.md"
    hub_before = hub.read_bytes()

    def why(title, ref=None):
        items, _ = rechecks.candidates(VAULT, graph.Index(VAULT))
        return {i["why"] for i in items if i["node"] == title
                and (ref is None or i["target"] == ref)}

    def last(title, **kw):
        nid = graph.Index(VAULT).node(W / f"{title}.md").id
        rs = [r for r in core.ledger_read(led) if r.get("node") == nid
              and all(r.get(k) == v for k, v in kw.items())]
        return rs[-1] if rs else {}

    try:
        _w(write.create_node, VAULT, "regr-rc-T", "s", "근거 1판.", "fable-5", space="00_Scope/W1")
        rn = _w(write.create_node, VAULT, "regr-rc-N", "s", "주장.", "fable-5",
                space="00_Scope/W1", edges={"derived-from": "regr-rc-T"})
        check("전제: 근거를 단 생성", rn.get("ok"), rn)
        check("생성은 bound를 적고 쌍은 완료다",
              not why("regr-rc-N") and last("regr-rc-N").get("result") == "bound",
              last("regr-rc-N"))

        # 관련 상태는 본문이다(헌법 8조) — 요약·배선만 바뀐 노드는 근거로서도, 참조
        # 노드로서도 바뀌지 않았다
        for raw_ in (b"---\nid: x\n---\n\nbody\n", b"---\r\nid: x\r\n---\r\n\r\nbody\r\n"):
            got = raw_[contract.body_offset(raw_):].decode().replace("\r\n", "\n")
            check("본문 경계는 판독(parse_bytes)과 같다",
                  got == contract.parse_bytes("x.md", raw_).body, got)
        _w(write.create_node, VAULT, "regr-rc-S", "s", "요약 실험 근거.", "fable-5", space="00_Scope/W1")
        _w(write.create_node, VAULT, "regr-rc-R", "s", "요약 실험 주장.", "fable-5",
           space="00_Scope/W1", edges={"derived-from": "regr-rc-S"})
        _w(write.update_node, VAULT, "regr-rc-S", summary="근거 요약만 바꾼다")
        _w(write.update_node, VAULT, "regr-rc-S", add_edges={"derived-from": "regr-rc-T"})
        check("근거의 요약·배선만 바뀌면 후보가 아니다", not why("regr-rc-R"))
        rows0 = len(core.ledger_read(led))
        _w(write.update_node, VAULT, "regr-rc-R", summary="참조 노드 요약만 바꾼다")
        check("참조 노드의 요약만 바뀌면 완료가 그대로이고 새로 적지 않는다",
              not why("regr-rc-R") and len(core.ledger_read(led)) == rows0)
        _w(write.update_node, VAULT, "regr-rc-S", old_text="근거.", new_text="근거를 고쳤다.")
        check("근거의 본문이 바뀌면 후보다", why("regr-rc-R") == {"근거가 바뀌었다"})
        idof = lambda title: graph.Index(VAULT).node(W / f"{title}.md").id
        seen3 = {idof("regr-rc-R"): st(W / "regr-rc-R.md"), idof("regr-rc-S"): st(W / "regr-rc-S.md")}
        _w(write.update_node, VAULT, "regr-rc-S", summary="읽은 뒤 요약만 바뀐다")
        r = _w(write.update_node, VAULT, "regr-rc-R", summary="요약과 함께 점검을 적는다",
               rechecked=["regr-rc-S"], _seen=seen3)
        check("읽은 뒤 요약만 바뀐 근거를 요약과 함께 rechecked에 적으면 unchanged로 닫힌다",
              r.get("rechecked") and not why("regr-rc-R")
              and last("regr-rc-R").get("result") == "unchanged", r)

        _w(write.update_node, VAULT, "regr-rc-T", old_text="1판", new_text="2판")
        check("근거가 바뀌면 후보다", why("regr-rc-N") == {"근거가 바뀌었다"})
        r = _w(write.update_node, VAULT, "regr-rc-N", add_edges={"derived-from": "regr-rc-T"})
        check("근거를 add_edges로 다시 넣기만 해서는 닫히지 않는다",
              r.get("no_change") and "rechecked" not in r
              and why("regr-rc-N") == {"근거가 바뀌었다"}, r)
        r = _w(write.update_node, VAULT, "regr-rc-N", rechecked=["regr-rc-T", "regr-rc-S", "regr-rc-없음"])
        check("바꿀 것 없이 rechecked에 적으면 unchanged로 닫히고, 근거가 아닌 항목은 비춘다",
              r.get("no_change") and r.get("rechecked") == ["[[regr-rc-T]]"]
              and r.get("recheck_unmatched", {}).get("targets") == ["regr-rc-S", "regr-rc-없음"]
              and not why("regr-rc-N") and last("regr-rc-N").get("result") == "unchanged", r)

        _w(write.update_node, VAULT, "regr-rc-T", old_text="2판", new_text="3판")
        r = _w(write.update_node, VAULT, "regr-rc-N", old_text="주장.", new_text="고친 주장.",
               rechecked=["regr-rc-T"])
        check("본문과 함께 rechecked에 적으면 updated로 닫힌다",
              r.get("ok") and not why("regr-rc-N")
              and last("regr-rc-N").get("result") == "updated", r)

        _w(write.update_node, VAULT, "regr-rc-N", old_text="고친 주장.", new_text="또 고친 주장.")
        check("노드만 고치면 완료가 이어진다",
              not why("regr-rc-N")
              and last("regr-rc-N").get("reason") == rechecks.CARRIED, last("regr-rc-N"))

        p = W / "regr-rc-N.md"
        p.write_bytes(p.read_bytes() + "밖에서 더한 줄.\n".encode())
        _age_all()
        check("엔진 밖에서 노드가 바뀌면 후보다", why("regr-rc-N") == {"노드가 바뀌었다"})

        # 제목 범위 근거 — 노드의 절이다. 엔진 밖의 바이트 편집으로 절을 바꾼다.
        hsrc = W / "regr-rc-Src.md"
        _w(write.create_node, VAULT, hsrc.stem, "s",
           "# 계획\n\n## A\n\n가.\n\n```\n## 코드 속\n```\n\n## B\n\n나.", "fable-5",
           space="00_Scope/W1")
        ref = f"[[{hsrc.stem}#A]]"
        _w(write.create_node, VAULT, "regr-rc-H", "s", "절 근거.", "fable-5",
           space="00_Scope/W1", edges={"derived-from": ref})
        check("제목 범위 근거도 완료로 시작한다", not why("regr-rc-H", ref))
        hsrc.write_bytes(hsrc.read_bytes().replace("나.".encode(), "다.".encode()))
        _age_all()
        check("다른 절이 바뀌어도 그 절의 근거는 완료다", not why("regr-rc-H", ref))
        hsrc.write_bytes(hsrc.read_bytes().replace("가.".encode(), "라.".encode()))
        _age_all()
        check("그 절이 바뀌면 후보다", why("regr-rc-H", ref) == {"근거가 바뀌었다"})
        data = hsrc.read_bytes()
        check("코드 속 `##`는 제목이 아니다",
              rechecks.heading_range(data, "코드 속") is None
              and b"## B" not in rechecks.heading_range(data, "A"))
        check("중복 제목은 해석 불능이다",
              rechecks.heading_range(b"## A\n1\n## A\n2\n", "A") is None)
        check("raw 라운드는 추적하지 않는다",
              rechecks.target("00_Scope/W1/_raw/.records/x.txt#1", graph.Index(VAULT)) is None)
        src.parent.mkdir(exist_ok=True)
        src.write_text("# 계획\n\n## A\n\n가.\n", encoding="utf-8")
        check("비노드 파일과 그 제목 범위는 추적하지 않는다(헌법 제8조 5항)",
              rechecks.target("[[_sources/regr-rc.md]]", graph.Index(VAULT)) is None
              and rechecks.target("[[_sources/regr-rc.md#A]]", graph.Index(VAULT)) is None)
        # 허브는 갈래의 입구다 — 허브 본문이 바뀌어도 그 허브를 인용한 노드는 후보가
        # 아니다(시행령 §7 2항 단서). 제목 범위로 가리켜도 같다.
        check("허브는 제목 범위로 가리켜도 추적하지 않는다",
              rechecks.target("[[W1]]", graph.Index(VAULT)) is None
              and rechecks.target("[[W1#W1]]", graph.Index(VAULT)) is None)
        _w(write.create_node, VAULT, "regr-rc-Hub", "s", "허브를 인용한 주장.", "fable-5",
           space="00_Scope/W1", edges={"derived-from": "W1"})
        hub.write_bytes(hub_before + "\n허브에 덧붙인 링크 목록.\n".encode())
        _age_all()
        check("허브 본문이 바뀌어도 그 허브를 인용한 노드는 후보가 아니다",
              not why("regr-rc-Hub"))
        hub.write_bytes(hub_before)
        # 제목이 사라져도 근거는 남는다 — 해석 불능 후보이지 뺀 근거가 아니다
        hsrc.write_bytes(hsrc.read_bytes().replace("## A\n".encode(), "## A2\n".encode()))
        _age_all()
        check("제목이 사라진 근거는 해석 불능 후보로 남는다",
              why("regr-rc-H", ref) == {"근거를 해석할 수 없다"})
        from osk import growth_plan
        hid = graph.Index(VAULT).node(W / "regr-rc-H.md").id
        job = {"id": hid, "target_key": graph.Index(VAULT).node(hsrc).id + "#A", "target": ref}
        check("그 작업은 근거를 뺀 것으로 닫히지 않는다",
              growth_plan.recheck_status(VAULT, job, graph.Index(VAULT))["status"] == "pending")

        # 변경분: 점검 때의 판(상태 해시가 같은 판)을 이력에서 찾아 바뀐 쪽의 diff를 싣는다
        from unittest import mock
        t = W / "regr-rc-T.md"
        old_t = t.read_bytes()
        _w(write.update_node, VAULT, "regr-rc-N", rechecked=["regr-rc-T"])
        _w(write.update_node, VAULT, "regr-rc-T", old_text="3판", new_text="4판")
        idx = graph.Index(VAULT)
        nid = idx.node(W / "regr-rc-N.md").id
        tkey = rechecks.target("[[regr-rc-T]]", idx)[0]
        with mock.patch.object(rechecks, "_versions", return_value=[b"other", old_t]):
            ch = rechecks.change(VAULT, nid, tkey, "[[regr-rc-T]]", idx)
        check("점검 때의 판을 골라 근거의 diff를 싣는다",
              ch.get("side") == "target" and "-근거 3판." in ch.get("diff", "")
              and "+근거 4판." in ch.get("diff", ""), ch)
        with mock.patch.object(rechecks, "_versions", return_value=[b"other"]):
            ch = rechecks.change(VAULT, nid, tkey, "[[regr-rc-T]]", idx)
        check("판을 못 찾으면 전문을 읽으라고 한다", "diff" not in ch and ch.get("note"), ch)
        with tempfile.TemporaryDirectory() as g:
            def git(*a, when="2026-09-01T10:00:00+09:00"):
                subprocess.run(["git", "-C", g, *a], check=True, capture_output=True,
                               env=dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when,
                                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))
            git("init", "-q")
            (Path(g) / "a b.md").write_bytes(b"old\n")
            git("add", "-A"); git("commit", "-qm", "1")
            (Path(g) / "a b.md").write_bytes(b"new\n")
            git("add", "-A"); git("commit", "-qm", "2", when="2026-09-03T10:00:00+09:00")
            got = rechecks._versions(_vault_at(g), "a b.md", "2026-09-02T10:00:00+09:00")
            check("git 이력에서 점검 시각 앞뒤의 판을 꺼낸다", got == [b"old\n", b"new\n"], got)

        # 표면의 `read_node` 기록이 있으면 그 판이 쓰기 직전과 같을 때만 완료를 적는다
        n_id, t_id = idof("regr-rc-N"), idof("regr-rc-T")
        seen = {n_id: st(W / "regr-rc-N.md"), t_id: st(t)}
        _w(write.update_node, VAULT, "regr-rc-T", old_text="4판", new_text="5판")    # 읽은 뒤 바뀐다
        r = _w(write.update_node, VAULT, "regr-rc-N", rechecked=["regr-rc-T"], _seen=seen)
        check("읽은 뒤 근거가 바뀌었으면 완료를 적지 않는다",
              r.get("recheck_unread") and why("regr-rc-N") == {"근거가 바뀌었다"}, r)
        seen[t_id] = st(t)
        r = _w(write.update_node, VAULT, "regr-rc-N", rechecked=["regr-rc-T"],
               _seen={**seen, n_id: "sha256:0"})
        check("읽은 노드 판이 지금과 다르면 완료를 적지 않는다", r.get("recheck_unread"), r)
        r = _w(write.update_node, VAULT, "regr-rc-N", rechecked=["regr-rc-T"], _seen=seen)
        check("읽은 판 그대로면 완료를 적는다", r.get("rechecked") and not why("regr-rc-N"), r)

        # 부분 열람도 읽은 판을 고정한다 — 전문 치환용 해시는 여전히 주지 않는다
        import mcp_server as M
        M._SEEN.clear()
        v = M.read_node("regr-rc-T", view="outline")
        check("부분 열람은 CAS 해시를 주지 않고 읽은 판만 기억한다",
              "hash" not in v and M._SEEN.get(t_id) == st(t), v)
        # 읽은 판을 고친 쓰기는 쓴 판을 잇는다 — 쓰기 응답 해시의 CAS 연쇄와 같다
        _w(write.update_node, VAULT, "regr-rc-T", old_text="5판", new_text="6판")
        seen2 = {n_id: st(W / "regr-rc-N.md"), t_id: st(t)}
        _w(write.update_node, VAULT, "regr-rc-N", old_text="밖에서 더한 줄.",
           new_text="밖에서 더한 줄을 고쳤다.", _seen=seen2)
        check("읽은 판을 고친 쓰기는 쓴 판을 잇는다",
              seen2[n_id] == st(W / "regr-rc-N.md"))
        blind = {}
        _w(write.update_node, VAULT, "regr-rc-N", old_text="줄을 고쳤다.", new_text="줄을 고쳤다!",
           _seen=blind)
        check("쓰기 전 판을 읽지 않은 쓰기는 잇지 않는다", n_id not in blind, blind)
        seen2[n_id] = st(W / "regr-rc-N.md")
        r = _w(write.update_node, VAULT, "regr-rc-N", rechecked=["regr-rc-T"], _seen=seen2)
        check("이은 판으로 rechecked에 적으면 완료를 적는다",
              r.get("rechecked") and not why("regr-rc-N"), r)
        # 표면이 다시 떠 읽은 기록이 비었다 — 검토자가 적은 대로 둔다(헌법 1조 3항)
        _w(write.update_node, VAULT, "regr-rc-T", old_text="6판", new_text="7판")
        r = _w(write.update_node, VAULT, "regr-rc-N", rechecked=["regr-rc-T"], _seen={})
        check("읽은 기록이 없으면 적은 대로 완료를 적는다",
              r.get("rechecked") and not r.get("recheck_unread") and not why("regr-rc-N"), r)
        # 읽은 기록은 노드 id에 묶인다 — 읽은 뒤 근거가 다른 군집으로 옮겨지고 고쳐져도 '기록
        # 없음'이 되어 낡은 점검이 완료로 적히지 않는다
        moved = {n_id: st(W / "regr-rc-N.md"), t_id: st(t)}
        mv = _w(node_moves.move_nodes, VAULT, ["regr-rc-T"], "00_Scope/W1/regr-rc-sub")
        check("전제: 근거를 하위 군집으로 옮긴다", mv.get("ok"), mv)
        _w(write.update_node, VAULT, "regr-rc-T", old_text="7판", new_text="8판")
        r = _w(write.update_node, VAULT, "regr-rc-N", rechecked=["regr-rc-T"], _seen=moved)
        check("읽은 뒤 옮겨지고 바뀐 근거는 완료를 적지 않는다",
              r.get("recheck_unread") and why("regr-rc-N") == {"근거가 바뀌었다"}, r)
        _w(node_moves.move_nodes, VAULT, ["regr-rc-T"], "00_Scope/W1")

        # 이행 전 derived-from에 남은 비노드 근거는 재지 않는다 — 바뀌어도 후보가 아니고,
        # rechecked에 적어도 닫을 쌍이 없다(헌법 제8조 5항)
        fref = "[[_sources/regr-rc.md]]"
        (W / "regr-rc-W.md").write_text(node_text("260802-zzzz-rcwf", body="파일 근거.",
                                                  extra=f'derived-from: "{fref}"\n'), encoding="utf-8")
        src.write_text(src.read_text(encoding="utf-8") + "\n덧붙임.\n", encoding="utf-8")
        _age_all()
        r = _w(write.update_node, VAULT, "regr-rc-W", rechecked=[fref])
        check("이행 전의 비노드 근거는 후보도 완료도 아니다",
              not why("regr-rc-W") and (r.get("recheck_unmatched") or {}).get("targets") == [fref], r)

        # 대장을 읽지 못해도 노드 쓰기는 된다 — 완료만 믿지 않는다
        good = led.read_bytes()
        led.write_bytes(good + '{"kind": "complete", "node'.encode())
        r = _w(write.update_node, VAULT, "regr-rc-N", old_text="또 고친 주장.", new_text="다시 고친 주장.")
        check("찢긴 대장 행이 있어도 노드를 고칠 수 있다",
              r.get("ok") and r.get("recheck_error"), r)
        check("그동안 쌍은 완료로 보지 않는다", why("regr-rc-N") == {"대장을 믿을 수 없다"})
        led.write_bytes(good)

        # 기준선: 기록이 없는 대장에서 첫 쓰기는 기존 쌍을 먼저 적는다
        led.unlink(missing_ok=True)
        old = W / "regr-rc-Old.md"
        old.write_text(node_text("260925-0002-rcold001",
                                 extra='derived-from: "[[regr-rc-T]]"\n'), encoding="utf-8")
        _age_all()
        _w(write.create_node, VAULT, "regr-rc-New", "s", "새 노드.", "fable-5", space="00_Scope/W1")
        check("첫 쓰기가 기존 쌍을 기준선으로 적는다",
              not why("regr-rc-Old")
              and last("regr-rc-Old").get("reason") == rechecks.BASELINE, last("regr-rc-Old"))
        rows = core.ledger_read(led)
        check("한 번에 적은 기록은 앞 기록을 부모로 잇는다",
              all(b["parents"] == [a["rid"]] for a, b in zip(rows, rows[1:])), rows[:2])
        check("기록이 있으면 기준선은 다시 적지 않는다", rechecks.ensure_baseline(VAULT) == 0)
    finally:
        hub.write_bytes(hub_before)
        for p in made + [src, W / "regr-rc-sub" / "regr-rc-T.md"]:
            p.unlink(missing_ok=True)
        rmtree_force(W / "regr-rc-sub")
        if lbefore is None:
            led.unlink(missing_ok=True)
        else:
            led.write_bytes(lbefore)
        _age_all()


def test_overview_rechecks_cache():
    """overview의 재검토 요약은 색인 지문에 묶여 접힌다(#194) — 그대로인 vault에서는
    노드 전수를 다시 읽지 않고, 대장만 바뀌거나 노드 본문만 바뀌어도 다시 잰다. 판독이
    잠깐 실패한 요약은 접지 않는다 — 잠금이 풀려도 지문은 그대로다.

    무엇을 망가뜨리면 실패하는가:
      · 접지 않으면 → 그대로인 vault 단언
      · 삼킨 판독 실패(`rechecks.misses`)를 보지 않으면 → 잠금이 풀린 단언
      · 키를 노드 지문(`fp[1]`)으로 좁히거나 보지 않으면 → 대장만 바뀐 단언
        (노드가 그대로면 색인이 살아남아 그 색인에 접은 요약도 남는다)"""
    import mcp_server as M
    from osk import rechecks
    W = ROOT / "00_Scope/W1"
    made = [W / f"regr-ovc-{x}.md" for x in ("T", "N")]
    led = rechecks.ledger_path(VAULT)
    lbefore = led.read_bytes() if led.exists() else None
    hub = W / "W1.md"
    hub_before = hub.read_bytes()
    real, calls = rechecks.report, []

    def report(vault, idx=None, limit=5):
        calls.append(1)
        return real(vault, idx, 10_000)                # 다른 시험이 남긴 후보에 밀리지 않게 전부 싣는다

    def mine():
        items = (M.overview().get("rechecks") or {}).get("items", [])
        return {(i["node"], i["why"]) for i in items if i["node"].startswith("regr-ovc-")}

    try:
        _w(write.create_node, VAULT, "regr-ovc-T", "s", "근거 1판.", "fable-5", space="00_Scope/W1")
        _w(write.create_node, VAULT, "regr-ovc-N", "s", "주장.", "fable-5", space="00_Scope/W1",
           edges={"derived-from": "regr-ovc-T"})
        _w(write.update_node, VAULT, "regr-ovc-T", old_text="1판", new_text="고친 판")
        _age_all()           # 크기가 바뀌는 수정만 쓴다 — mtime을 고정하므로 지문은 크기로 가른다
        rechecks.report = report
        M._idx().nodes       # 색인은 먼저 짓는다 — 잠금은 재검토 자신의 판독에만 건다

        read_bytes = Path.read_bytes

        def locked(self):
            if self.name == "regr-ovc-T.md":
                raise PermissionError(13, "다른 프로세스가 잠갔다", str(self))
            return read_bytes(self)

        Path.read_bytes = locked
        try:
            got = mine()
        finally:
            Path.read_bytes = read_bytes         # 내용도 메타데이터도 그대로 풀린다
        check("전제: 잠긴 근거의 쌍은 요약에서 빠진다", not got, got)
        got = mine()
        check("잠금이 풀리면 지문이 같아도 다시 잰다",
              got == {("regr-ovc-N", "근거가 바뀌었다")}, got)
        n = len(calls)
        mine()
        check("그대로인 vault에서는 재검토 요약을 다시 재지 않는다", len(calls) == n, len(calls) - n)
        r = _w(write.update_node, VAULT, "regr-ovc-N", rechecked=["regr-ovc-T"])
        _age_all()
        check("전제: 노드는 그대로이고 대장만 바뀐다", r.get("no_change") and r.get("rechecked"), r)
        check("대장만 바뀌어도 다시 잰다", not mine(), mine())
        p = W / "regr-ovc-N.md"
        p.write_bytes(p.read_bytes() + "밖에서 더한 줄.\n".encode())
        _age_all()
        check("노드 본문이 바뀌면 다시 잰다", mine() == {("regr-ovc-N", "노드가 바뀌었다")}, mine())
    finally:
        rechecks.report = real
        hub.write_bytes(hub_before)
        for p in made:
            p.unlink(missing_ok=True)
        if lbefore is None:
            led.unlink(missing_ok=True)
        else:
            led.write_bytes(lbefore)
        _age_all()


def test_review_empty_memory_and_incomplete_region():
    from osk import approvals as A, scope_memory as sm, approval_acts, region_tree
    scope = "ReviewEmptyCAS"
    scope_dir = ROOT / "00_Scope" / scope
    region = "00_Domain/ReviewScanError"
    region_dir = ROOT / region
    saved = {p: p.read_bytes() if p.exists() else None
             for p in (VAULT.routing, A.ledger_path(VAULT))}
    try:
        scope_dir.mkdir()
        old = sm.replace(VAULT, "review-empty-cas", "old decision", space=f"00_Scope/{scope}")
        write.set_primary(VAULT, "review-empty-cas", scope, "시험 지정")
        empty = sm.replace(VAULT, "review-empty-cas", "", expect_hash=old["hash"])
        refused = _w(sm.replace, VAULT, "review-empty-cas", "stale decision", old["hash"])
        check("빈 기억에도 stale hash를 거부한다", refused.get("ok") is False, refused)
        check("stale 쓰기가 삭제를 되돌리지 않는다", sm.read(VAULT, "review-empty-cas")["text"] == "")
        check("빈 기억의 현행 hash는 새 쓰기를 허용한다",
              sm.replace(VAULT, "review-empty-cas", "new decision", empty["hash"])["ok"])

        region_dir.mkdir()
        (region_dir / "reviewed.txt").write_text("reviewed", encoding="utf-8")
        baseline = approval_acts.protect(VAULT, region, "scan error fixture")
        denied = region_dir / "new-subtree"
        denied.mkdir()
        (denied / "new.txt").write_text("not reviewed", encoding="utf-8")
        work = region_tree.working_tree_hash(VAULT, region)
        real_scandir = os.scandir
        def scan_error(path):
            if Path(path) == denied:
                raise PermissionError(13, "fixture directory unreadable", str(path))
            return real_scandir(path)
        with mock.patch("os.scandir", side_effect=scan_error):
            check("보호영역 열거 실패는 작업본 판정 불능", region_tree.working_tree_hash(VAULT, region) is None)
            check("보호영역 열거 실패는 clean이 아니다", A.state(VAULT, region) == "pending")
            check("열거 실패로 보호를 해제하지 않는다", _raises(lambda: approval_acts.unprotect(VAULT, region))())
            try:
                approval_acts.approve(VAULT, region, baseline["accepted"], work)
                rejected = False
            except OSError:
                rejected = True
            check("부분 열거를 새 승인본으로 저장하지 않는다", rejected)
        check("열거 실패 뒤에도 승인본은 유지된다", A.approved_hash(VAULT, region) == baseline["accepted"])
    finally:
        rmtree_force(scope_dir)
        sm.sm_path(VAULT, scope).unlink(missing_ok=True)
        rmtree_force(region_dir)
        for p, data in saved.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)


def test_review_root_reparse_and_lazy_search():
    import mcp_server as M
    # 고립된 새 root로 실제 최상위 junction/symlink를 검사한다.
    with tempfile.TemporaryDirectory() as td:
        vault, outside = Path(td) / "vault", Path(td) / "outside"
        vault.mkdir()
        outside.mkdir()
        marker = outside / "review-outside.md"
        marker.write_text(node_text("260802-zzzz-9030", body="outside marker"), encoding="utf-8")
        for base in ("00_Domain", "_sources"):
            link = vault / base
            if os.name == "nt":
                made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                      capture_output=True).returncode == 0
            else:
                try:
                    link.symlink_to(outside, target_is_directory=True)
                    made = True
                except OSError:
                    made = False
            if not made:
                skip("최상위 리파스 포인트 봉쇄", "fixture 링크를 만들 수 없다")
                continue
            try:
                idx = graph.Index(_vault_at(vault))
                check(f"최상위 {base} 링크는 완전한 색인이 아니다", not idx.complete)
                check(f"최상위 {base} 링크의 외부 파일을 색인하지 않는다",
                      "review-outside" not in idx.names and "review-outside" not in idx.nonnode)
                signature, _ = graph.index_signature(_vault_at(vault))
                check(f"최상위 {base} 링크의 외부 내용은 지문에도 없다", not signature)
            finally:
                if os.name == "nt":
                    link.rmdir()  # junction 자체만 제거; 외부 트리를 순회하지 않는다.
                else:
                    link.unlink()
    p = ROOT / "00_Scope/W1/review-lazy-search.md"
    duplicate = ROOT / "00_Scope/W1/review-lazy-duplicate.md"
    try:
        p.write_text(node_text("260802-zzzz-9031", body="uniquelazysearchmarker"), encoding="utf-8")
        _age_all()
        M._index, M._searcher, M._fingerprint = None, None, None
        with mock.patch.object(M.search_mod, "Searcher", side_effect=AssertionError("조회에서 BM25 생성")):
            check("read_node는 BM25 없이 동작한다", "error" not in M.read_node(p.stem))
            check("overview는 BM25 없이 동작한다", "error" not in M.overview())
            duplicate.write_bytes(p.read_bytes())
            check("BM25 분리 후에도 중복 id 핸들은 거부한다", "error" in M.read_node("260802-zzzz-9031"))
            duplicate.unlink()
        _age_all()
        check("검색 호출은 BM25를 생성해 결과를 낸다",
              any(r["title"] == p.stem for r in M.search("uniquelazysearchmarker")))
    finally:
        p.unlink(missing_ok=True)
        duplicate.unlink(missing_ok=True)
        M._index, M._searcher, M._fingerprint = None, None, None


def test_read_cache_dependencies():
    """A title read must stay local; evidence edits must not rebuild BM25."""
    import mcp_server as M
    p = ROOT / "00_Scope/W1/cache-candidate.md"
    bad = ROOT / "00_Scope/W1/cache-broken.md"
    twin = ROOT / "00_Scope/W2/cache-candidate.md"
    twin.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = ROOT / "00_Scope/W1/_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    evidence = raw_dir / "cache-evidence.txt"
    renamed = raw_dir / "cache-renamed.txt"
    ledger = VAULT.ledger / "cache-events.jsonl"
    try:
        p.write_text(node_text("260922-benc-00000001", body="originalcachetoken"), encoding="utf-8")
        bad.write_text("---\nbroken: [\n---\n", encoding="utf-8")
        _age_all()
        M._index, M._searcher, M._fingerprint = None, None, None
        with mock.patch.object(graph.Index, "parse_all", side_effect=AssertionError("full parse")):
            result = M.read_node(p.stem)
            check("제목 조회는 전수 파싱 없이 본문·바이트 해시를 낸다",
                  result.get("body", "").strip() == "originalcachetoken"
                  and result.get("hash") == core.sha256_file(p))
            check("없는 제목도 전수 파싱하지 않는다", "노드 없음" in M.read_node("cache-missing")["error"])
            check("파손 제목을 전수 파싱 없이 보고한다", "파싱 실패" in M.read_node(bad.stem)["error"])
            twin.write_bytes(p.read_bytes())
            _age_all()
            check("제목 후보 중복은 계속 거부한다", "같은 이름" in M.read_node(p.stem)["error"])
            twin.write_text("---\nbroken: [\n---\n", encoding="utf-8")
            _age_all()
            check("파손된 동명 후보는 정상 후보의 소유권을 가리지 않는다",
                  "error" not in M.read_node(p.stem))
        twin.unlink()
        _age_all()
        s = M._s()
        before = M._fingerprint
        for path in (evidence, ledger):
            path.write_text("new evidence\n", encoding="utf-8")
            _age_all()
            with mock.patch.object(M.search_mod, "Searcher", side_effect=AssertionError("BM25 rebuilt")):
                check("비노드 변경 뒤에도 검색 결과·검색기를 재사용한다",
                      M._s() is s and any(x["title"] == p.stem for x in M.search("originalcachetoken")))
            check("새 비노드 이름은 같은 Index에서도 즉시 해석된다",
                  M._index.resolve(path.stem)[0] == "nonnode")
        check("비노드 서명은 변하고 노드 서명은 유지된다",
              before[0] != M._fingerprint[0] and before[1] == M._fingerprint[1])
        evidence.rename(renamed)
        _age_all()
        check("비노드 개명·삭제가 검색기 재사용에 가려지지 않는다",
              M._s() is s and s.idx.resolve(evidence.stem)[0] == "dangling"
              and s.idx.resolve(renamed.stem)[0] == "nonnode")
        p.write_text(node_text("260922-benc-00000001", body="replacementcachetoken"), encoding="utf-8")
        _age_all()
        check("노드 편집은 새 검색 결과로 재구축한다",
              any(x["title"] == p.stem for x in M.search("replacementcachetoken"))
              and M._searcher is not s)
        check("교체된 본문은 낡은 검색 결과에 남지 않는다",
              not any(x["title"] == p.stem for x in M.search("originalcachetoken")))
        signature = graph.index_signature

        def incomplete(vault, errors=None):
            result = signature(vault, errors)
            if errors is not None:
                errors.append("injected incomplete signature")
            return result

        with mock.patch.object(graph, "index_signature", incomplete):
            first = M._s()
            check("불완전 지문은 키로 쓰지 않고 검색기를 재사용하지 않는다",
                  M._fingerprint is None and M._s() is not first)
        _age_all()
        M._s()
        refresh = graph.Index.refresh_nonnode

        def failed_refresh(idx):
            refresh(idx)
            idx.scan_errors.append("injected evidence refresh failure")

        renamed.write_text("changed evidence\n", encoding="utf-8")
        _age_all()
        with mock.patch.object(graph.Index, "refresh_nonnode", failed_refresh):
            M._idx()
            check("비노드 새 관측이 불완전하면 캐시 키·BM25를 버린다",
                  M._fingerprint is None and M._searcher is None and not M._index.complete)
    finally:
        for path in (p, bad, twin, evidence, renamed, ledger):
            path.unlink(missing_ok=True)
        M._index, M._searcher, M._fingerprint = None, None, None


def test_reparse_cache_membership():
    """A resolved node alias must participate in node-cache invalidation."""
    import mcp_server as M
    alias = ROOT / "00_Scope/W1/_raw/cache-alias.md"
    alias.parent.mkdir(parents=True, exist_ok=True)
    # Simulate file reparse resolution even where symlink creation is unavailable.
    # All fixture bytes stay inside the mini-vault; outside resolution is simulated.
    alias.write_text(node_text("260922-benc-00000009", body="fixture"), encoding="utf-8")
    destination = ROOT / "00_Scope/W1/cache-target.md"
    resolve, reparse = Path.resolve, placement.is_reparse

    def resolved(path, *args, **kwargs):
        return destination if path == alias else resolve(path, *args, **kwargs)

    def is_reparse(entry):
        return True if Path(getattr(entry, "path", entry)) == alias else reparse(entry)

    try:
        _age_all()
        M._index, M._searcher, M._fingerprint = None, None, None
        with mock.patch.object(Path, "resolve", resolved), mock.patch.object(placement, "is_reparse", is_reparse):
            check("raw 안의 노드 별칭은 실제 소속으로 조회한다", "error" not in M.read_node(alias.stem))
            first, fp = M._index, M._fingerprint
            destination = ROOT / "00_Scope/W2/cache-target.md"
            M._idx()
            check("같은 메타데이터라도 링크 목적지가 바뀌면 노드 캐시를 버린다",
                  M._index is not first and M._fingerprint[1] != fp[1])
            assert "error" not in M.read_node(alias.stem) and alias in M._index.parsed
            first = M._index
            destination = ROOT.parent / "not-a-vault-node.md"  # No outside file is created or read.
            check("vault 밖으로 분류된 목적지는 후보·파싱 캐시에서 제거한다",
                  alias.stem not in M._idx()._by_name and alias not in M._index.parsed)
            destination = ROOT / "_sources/cache-target.txt"
            check("노드 소속을 벗어난 별칭은 캐시된 노드로 조회되지 않는다",
                  "error" in M.read_node(alias.stem) and M._index is not first
                  and alias.stem not in M._index._by_name
                  and M._index.resolve(alias.stem)[0] == "nonnode")
        alias.unlink()
        target = ROOT / "00_Scope/W1/cache-native-target.md"
        evidence = ROOT / "_sources/cache-native-target.txt"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(node_text("260922-benc-0000000a", body="fixture"), encoding="utf-8")
            evidence.write_bytes(target.read_bytes())
            try:
                alias.symlink_to(target)
            except OSError as ex:
                skip("실제 파일 링크의 캐시 소속 전환", str(ex))
                return
            _age_all()
            M._index, M._searcher, M._fingerprint = None, None, None
            # 같은 노드로 가는 파일 링크는 한 노드를 두 자리에 두는 중복 소속이다 —
            # 저장 방식의 변용을 허용하지 않으므로 사본과 같이 거부한다(Mechanism §2 1항).
            rr = M.read_node(alias.stem)
            check("실제 파일 링크는 중복 소속으로 거부하고 링크를 지우라고 한다",
                  "error" in rr and "링크를 지워" in rr["error"], rr)
            alias.unlink()
            alias.symlink_to(evidence)
            check("실제 파일 링크도 비노드 전환 뒤 노드 후보에서 제거한다",
                  "error" in M.read_node(alias.stem) and alias.stem not in M._index._by_name
                  and alias not in M._index.parsed)
        finally:
            alias.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            evidence.unlink(missing_ok=True)
    finally:
        alias.unlink(missing_ok=True)
        M._index, M._searcher, M._fingerprint = None, None, None
