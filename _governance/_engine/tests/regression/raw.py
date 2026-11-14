"""회귀 수트의 한 갈래: 인용 기록과 외부 루트 — 덧쓰기, 좌표, 결속.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import os
from pathlib import Path
from unittest import mock

from regression.support import (
    _append_round, check, node_text, rmtree_force, ROOT, _rounds_of, skip, _TMP, _unhang, VAULT, _w
)
from osk import core, graph, contract, write, topology  # noqa: E402


# ── 18. 인용 기록 통로 (헌법 4조 3~4항 · 시행령 §2 · Mechanism §9 4~6항) ──
def test_raw_append():
    """인용 기록은 증거다. 이 수트가 지키는 것은 세 가지다 — 한 번 쓴 바이트가
    다시는 바뀌지 않는 것, 라운드 번호가 이미 쓴 근거 참조를 배신하지 않는 것,
    그리고 비밀값이 이 통로를 우회해 남지 않는 것."""
    from osk import raw, secrets
    (ROOT / "00_Scope/WRaw").mkdir(exist_ok=True)
    S, REC = "repo/regr-raw", "2026-08-21-regr"
    p = ROOT / "_cited/.records" / f"{REC}.txt"          # 루트 `_cited/` — 한 자리다(헌법 4조 3항)

    r1 = _w(_append_round, S, REC, "첫 질문", "첫 응답", space="00_Scope/WRaw")
    check("최초 라운드는 1", r1.get("index") == 1, r1)
    check("round_ref가 근거 표기 그대로다",
          r1.get("round_ref") == f"_cited/.records/{REC}.txt#1", r1)
    check("인용은 결속을 세우지 않는다", write.resolve_session(VAULT, S) is None)

    r2 = _w(_append_round, S, REC, "둘째 질문", "둘째 응답")   # space 없이도 같은 기록
    check("index 단조 증가", r2.get("index") == 2, r2)
    body = p.read_text(encoding="utf-8")
    check("먼저 쓴 라운드가 그대로 남는다", "첫 질문" in body and "첫 응답" in body)

    # 대화 본문의 숫자 H2가 라운드 제목으로 오독되지 않는다 (Mechanism §8 3항)
    _w(_append_round, S, REC, "본문에 ## 24 가 있다", "응답\n## 7\n끝")
    body = p.read_text(encoding="utf-8")
    check("escape로 라운드 수가 늘지 않는다", raw.rounds(body) == [1, 2, 3],
          raw.rounds(body))
    check("escape는 역연산이 있다",
          raw.unescape_numeric_h2(raw.escape_numeric_h2("## 7\n\\## 9"))
          == "## 7\n\\## 9")

    # 비밀값은 통로에서 치환된다 (시행령 §2 3항 — 우회 경로를 두지 않는다).
    # fixture를 쪼개 쓰는 이유: 릴리스의 비밀값 스캔이 전 파일을 훑으므로
    # (release.py, secrets.py만 자기 면제) 소스에 완전형을 두면 선언이 막힌다.
    r4 = _w(_append_round, S, REC,
            "키 " + "AKIA" + "IOSFODNN7EXAMPLE" + " 준다", "받았다")
    body = p.read_text(encoding="utf-8")
    check("비밀값이 기록에 남지 않는다", ("AKIA" + "IOSFODNN7EXAMPLE") not in body)
    check("치환 사실을 호출자에게 알린다",
          r4.get("filtered") == ["aws-access-key"], r4)

    # 접두부 보존 — 증거를 소급해 고쳐 쓰지 못한다 (Mechanism §9 4항)
    before = p.read_bytes()
    try:
        raw.write_raw(VAULT, p, "과거를 지운 새 내용")
        check("접두부 훼손을 거부한다", False, "거부하지 않았다")
    except ValueError as e:
        check("접두부 훼손을 거부한다", "append 아님" in str(e), e)
    check("거부는 파일을 건드리지 않는다", p.read_bytes() == before)

    # 손상된 index 열 위에는 이어 쓰지 않는다 (Mechanism §9 6항)
    bad = ROOT / "00_Scope/WRaw/_raw/corrupt.md"   # 구판 기록 — 새 기록은 _cited에 선다
    bad.parent.mkdir(exist_ok=True)
    bad.write_text("## 1\n\n본문\n\n## 1\n\n중복\n", encoding="utf-8")
    r = _w(_append_round, S, "corrupt", "질문", "응답")
    check("중복 index 기록에 이어 쓰지 않는다", r.get("ok") is False, r)
    bad.unlink()  # The deliberately invalid legacy fixture must not leak into publish tests.

    # 통로 fail-closed — `_raw/` 밖은 이 함수로 쓸 수 없다
    try:
        raw.write_raw(VAULT, ROOT / "00_Scope/WRaw/노드.md", "x")
        check("`_raw/` 밖 경로를 거부한다", False, "거부하지 않았다")
    except ValueError as e:
        check("`_raw/` 밖 경로를 거부한다", "_raw" in str(e), e)

    # 라운드는 쌍이다 / 착지·이름은 fail-closed
    check("빈 응답을 거부한다",
          _w(_append_round, S, REC, "질문만", "  ").get("ok") is False)
    check("결속 없는 프로젝트의 인용도 루트 한 자리에 선다 (v5.2)",
          _w(_append_round, "repo/unbound-raw", "regr-unbound-r", "q", "a")
          .get("round_ref") == "_cited/.records/regr-unbound-r.txt#1")
    check("Windows 예약 장치명을 기록 이름으로 거부한다",
          _w(_append_round, S, "COM1", "q", "a").get("ok") is False)

    # 이식성 기준으로 같은 이름은 같은 정본 (시행령 §2 1항)
    r5 = _w(_append_round, S, REC.upper(), "대소문자", "같은 파일")
    check("대소문자만 다른 이름은 같은 기록으로 접힌다",
          r5.get("path", "").endswith(f"{REC}.txt"), r5)


# ── 18c. 명시 회상 — 근거에서 증거로 번역 없이 간다 (시행령 §2 5항) ────────
def test_external_roots():
    """외부 루트(시행령 §2 8항 · Mechanism §1 6항). 별칭 좌표의 끊김은 셋으로 갈린다 — 선언되지
    않은 별칭과 루트 안에 없는 파일은 끊김이고, 선언된 루트가 이 기기에 없으면 보류다. 위치는
    vault 밖이어야 하고, 좌표는 그 위치 안으로 봉쇄된다."""
    import io
    from osk import cli, evidence, roots
    ext = Path(_TMP.name) / "ext-root"
    (ext / "notes").mkdir(parents=True)
    (ext / "notes/a.md").write_text("# 결론\n\nx\n", encoding="utf-8")
    (Path(_TMP.name) / "outside.md").write_text("x", encoding="utf-8")

    def problem(coord, at=None):
        return evidence.check(VAULT, coord, None, None, at or roots.Roots(VAULT))

    def refused(label, fn, *a, says=""):
        try:
            fn(*a)
            check(label, False, "거부되지 않았다")
        except ValueError as e:
            check(label, says in str(e), e)

    check("별칭 좌표의 모양 — 드라이브 문자·URL·원본 턴·절대 경로는 아니다",
          roots.split("docs:notes/a.md") == ("docs", "notes/a.md")
          and not any(roots.split(c) for c in ("C:/x", "c:x", "https://e.com/a", "native:claude:a:b",
                                               "docs:/x")))
    for bad in ("d", "Docs", "http", "a_b"):
        refused(f"쓸 수 없는 별칭: {bad}", roots.declare, VAULT, bad, "n", says="쓸 수 없는 별칭")

    check("선언 전 — 끊김", problem("docs:notes/a.md") == "선언되지 않은 별칭: docs:notes/a.md"
          and graph.Index(VAULT).resolve("docs:notes/a.md") == ("dangling",))
    with mock.patch.object(core, "mutation_lock", wraps=core.mutation_lock) as lock:
        roots.declare(VAULT, "docs", "외부 문서", "https://example.com/docs.git")
    check("선언은 작업 트리 잠금 안에서 적는다(데몬의 rebase와 직렬화)", lock.call_count == 1, lock.call_count)
    check("선언하면 `/`가 있어도 경로가 아니라 바깥이다",
          graph.Index(VAULT).resolve("docs:notes/a.md") == ("external",))
    seen = graph.Index(VAULT)
    with mock.patch.object(roots, "declarations", wraps=roots.declarations) as read:
        same = [seen.resolve(c) for c in ("docs:notes/a.md", "docs:x.md", "other:x.md")]
    check("한 색인은 선언 대장을 한 번 읽는다",
          same == [("external",), ("external",), ("dangling",)] and read.call_count == 1,
          (same, read.call_count))
    held = roots.Roots(VAULT)
    check("선언됐지만 이 기기에 없으면 보류 — 루트마다 한 번",
          problem("docs:notes/a.md", held) is None and problem("docs:x.md", held) is None
          and held.held == {"docs": "이 기기에 위치가 없다"}, held.held)

    refused("vault 안의 위치", roots.mount, VAULT, "docs", str(ROOT / "00_Scope"), says="vault 안에 있거나")
    refused("vault를 품는 위치", roots.mount, VAULT, "docs", str(ROOT.parent), says="vault 안에 있거나")
    refused("선언되지 않은 별칭에는 위치를 두지 않는다", roots.mount, VAULT, "other", str(ext),
            says="선언되지 않은 별칭")
    roots.mount(VAULT, "docs", str(ext))
    check("위치는 이 기기의 설정에", roots.locations(VAULT) == {"docs": os.path.abspath(ext)},
          roots.locations(VAULT))

    here = roots.Roots(VAULT)
    check("있는 파일과 제목", problem("docs:notes/a.md#결론", here) is None,
          problem("docs:notes/a.md#결론", here))
    check("없는 제목", problem("docs:notes/a.md#없다", here) == "없는 제목: docs:notes/a.md#없다")
    check("없는 파일", problem("docs:notes/b.md", here) == "없는 파일: docs:notes/b.md")
    check("루트 밖으로 나가는 경로",
          problem("docs:../outside.md", here) == "루트 안으로 해석되지 않는 경로: docs:../outside.md")
    try:
        os.symlink(ext / "notes", ext / "link", target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        skip("위치 아래의 symlink 거부", f"symlink를 만들 수 없다: {e}")
    else:
        check("위치 아래의 symlink 거부",
              problem("docs:link/a.md", here) == "루트 안으로 해석되지 않는 경로: docs:link/a.md")
    check("있는 루트는 보류가 아니다", here.held == {}, here.held)

    moved = ext.with_name("ext-moved")
    ext.rename(moved)
    gone = roots.Roots(VAULT)
    check("위치가 사라지면 보류", problem("docs:notes/a.md", gone) is None
          and gone.held.get("docs", "").startswith("이 기기에 없는 위치"), gone.held)
    moved.rename(ext)

    with mock.patch("sys.stdin", io.StringIO()):
        try:
            cli.main(["roots", "retire", "docs"])
            check("단말 없는 해제는 중단된다", False)
        except SystemExit as e:
            check("단말 없는 해제는 중단된다",
                  "중단" in str(e.code) and roots.alias_problem("docs", roots.declarations(VAULT)) is None,
                  e.code)
    with mock.patch.object(core, "mutation_lock", wraps=core.mutation_lock) as lock:
        roots.retire(VAULT, "docs", "옮김")
    check("해제는 확인과 기록을 한 작업 트리 잠금 안에서 한다", lock.call_count == 1, lock.call_count)
    check("해제한 별칭은 끊김", problem("docs:notes/a.md") == "해제된 별칭: docs:notes/a.md"
          and graph.Index(VAULT).resolve("docs:notes/a.md") == ("dangling",))
    # 공유 색인(MCP)은 대장이 바뀌면 지문이 달라져 비노드 갱신을 부른다 — 선언도 그때 다시 읽는다
    check("색인은 갱신 전까지 선언을 다시 읽지 않는다", seen.resolve("docs:notes/a.md") == ("external",))
    seen.refresh_nonnode()
    check("비노드 갱신이 선언을 다시 읽는다", seen.resolve("docs:notes/a.md") == ("dangling",))
    refused("해제한 별칭은 다시 해제하지 않는다", roots.retire, VAULT, "docs", says="선언된 별칭이 아니다")
    check("조회는 선언 상태와 이 기기의 위치를 함께 낸다",
          [(r["alias"], r["declaration"], r["note"], r.get("here")) for r in roots.show(VAULT)["roots"]]
          == [("docs", "해제된 별칭", "옮김", "있음")], roots.show(VAULT))
    roots.unmount(VAULT, "docs")
    check("위치를 지운다", roots.locations(VAULT) == {})
    refused("없는 위치는 지우지 않는다", roots.unmount, VAULT, "docs", says="위치가 없는")
    roots.config_path(VAULT).unlink()


def test_non_node_evidence_is_an_evidence_line():
    """`derived-from`은 노드만 받고 비노드 근거는 노드 끝 근거 절의 줄이다(헌법 제8조 5·6항).

    무엇을 망가뜨리면 실패하는가:
      · `node_checks.check_edges`의 비노드 거부를 지우면 → 첫 단언 묶음
      · 빼는 갈래(`adding=False`)까지 거부하면 → 옛 대상 제거 단언
      · 증류가 인용 라운드를 derived-from에 다시 달거나 줄을 보지 않으면 → 증류 단언 셋
      · 미해석 근거 좌표를 참조 판독에서 빼면 → 쓰기 응답 단언
      · Domain 근거 절의 라운드를 위상에서 빼면 → Domain 단언 둘
      · 재검토가 비노드를 다시 재면 → 마지막 단언
    """
    from osk import distillation as D, evidence, raw, rechecks
    sp, dom = "00_Scope/W1", ROOT / "00_Domain/regr-EvD"
    rec = raw.append_rounds(VAULT, "repo/regr-evid", "regr-evid-rec", [{"user": "관측", "agent": "결과"}], sp)
    ref, coord = rec["round_refs"][0], raw.coordinate(VAULT, rec["path"], 1)
    src = ROOT / "_sources/regr-evid.md"
    src.parent.mkdir(exist_ok=True)
    src.write_text("# 결과\n\nx\n", encoding="utf-8")
    line = f"- 2026-10-31 · `{coord}` · 지지 — 관측"
    made = ROOT / sp / "regr-evid.md"
    legacy = ROOT / sp / "regr-evid-old.md"
    try:
        for bad in (ref, coord, "https://example.com/a", "_sources/regr-evid.md", "docs:a.md"):
            r = _w(write.create_node, VAULT, "regr-evid-bad", "s", "본문", "fable-5", space=sp,
                   edges={"derived-from": bad})
            check(f"derived-from은 비노드를 받지 않는다: {bad}",
                  not r.get("ok") and "derived-from은 노드만" in str(r), r)
        spec = {"key": "regr-evid", "sources": [ref], "hub": "W1"}
        r = _w(D.create_node, VAULT, spec, title="regr-evid", summary="s", body="주장.",
               drafter="fable-5", space=sp)
        check("근거 줄 없는 인용 라운드는 증류하지 않는다", not r.get("ok") and coord in str(r), r)
        r = _w(D.create_node, VAULT, spec, title="regr-evid", summary="s",
               body="주장.\n\n## 근거\n\n" + line, drafter="fable-5", space=sp)
        check("근거 줄로 단 인용 라운드는 증류된다",
              r.get("distillation", {}).get("status") == "complete", r)
        check("인용 라운드는 derived-from에 달지 않는다", "derived-from" not in contract.parse(made).meta)
        huge = line.replace("#1`", "#1–100000000`")
        check("기록에 다 있지 않은 범위는 펴지 않는다",
              evidence.cited_rounds(VAULT, "주장.\n\n## 근거\n\n" + huge) == set()
              and evidence.cited_rounds(VAULT, "주장.\n\n## 근거\n\n" + line) == {raw.canonical_ref(VAULT, ref)})
        r = _w(write.update_node, VAULT, "regr-evid", old_text=line,
               new_text=line + "\n" + line.replace("#1`", "#9`"))
        items = (r.get("reference_review") or {}).get("items", [])
        check("해석되지 않는 근거 좌표는 쓰기 응답이 보고한다",
              r.get("ok") and [(i["relation"], i["issue"]) for i in items]
              == [("근거", "evidence_unresolved")] and "없는 라운드" in items[0]["problem"], r)
        # 이행 전의 옛 비노드 대상은 읽히고, 뺄 수 있다
        legacy.write_text(node_text("260802-zzzz-evd1", body="옛 주장",
                                    extra=f'derived-from: "{ref}"\n'), encoding="utf-8")
        r = _w(write.update_node, VAULT, "regr-evid-old", remove_edges={"derived-from": ref})
        check("옛 비노드 대상은 뺄 수 있다",
              r.get("ok") and "derived-from" not in contract.parse(legacy).meta, r)
        # Domain 노드의 근거 절은 인용 기록을 가리키지 않는다 — 쓰기와 전역 검사 모두
        dom.mkdir(parents=True, exist_ok=True)
        (dom / "regr-EvD.md").write_text(node_text("260802-zzzz-evd2", "허브"), encoding="utf-8")
        r = _w(write.create_node, VAULT, "regr-evd-claim", "s", "주장.\n\n## 근거\n\n" + line, "fable-5",
               space="00_Domain/regr-EvD")
        check("Domain 노드의 근거 줄은 인용 라운드를 가리키지 않는다",
              not r.get("ok") and "Domain의 인용 기록 직접 참조(근거 절)" in str(r), r)
        (dom / "regr-evd-hand.md").write_text(node_text("260802-zzzz-evd3",
                                                        body="주장.\n\n## 근거\n\n" + line), encoding="utf-8")
        check("손으로 쓴 Domain 근거 줄은 전역 위상 검사가 잡는다",
              any("regr-evd-hand → " + coord in e for e in topology.topology_check(VAULT, graph.Index(VAULT))))
        idx = graph.Index(VAULT)
        check("재검토는 비노드를 재지 않는다",
              rechecks.target("[[_sources/regr-evid.md]]", idx) is None
              and rechecks.target("regr-evid", idx) is not None)
    finally:
        _unhang("W1", "regr-evid")
        for p in (made, legacy, src, ROOT / rec["path"]):
            p.unlink(missing_ok=True)
        rmtree_force(dom)
        D._job_path(VAULT, "regr-evid").unlink(missing_ok=True)


def test_raw_read():
    """`_raw/`는 작업 검색에서 빠지므로(헌법 11조 3항) 좌표로 연다. 이 수트가
    지키는 것은 **왕복**이다 — 쓰기가 준 `round_ref`가 `derived-from`에 앉고,
    그 값이 그대로 읽기의 입력이어야 근거에서 증거로 가는 데 번역이 끼지 않는다."""
    from osk import raw
    (ROOT / "00_Scope/WRawRd").mkdir(exist_ok=True)
    SP, S, REC = "00_Scope/WRawRd", "repo/regr-read", "read-rec"

    w = _w(raw.append_rounds, VAULT, S, REC, [
        {"user": "첫 질문", "agent": "첫 응답"},
        {"user": "둘째 질문", "agent": "본문에\n## 7\n숫자 제목"},
        {"user": "긴 것", "agent": "가" * 3000},
    ], SP)
    check("세 라운드 기록", w.get("indices") == [1, 2, 3], w)

    # 왕복 — 쓰기가 준 좌표를 그대로 읽기에 넣는다
    r = _w(raw.read_round, VAULT, w["round_refs"][1])
    check("round_ref가 그대로 읽기의 입력이다", r.get("index") == 2, r)
    check("라운드 본문이 온다", "둘째 질문" in r.get("text", ""), r)
    check("이웃 라운드가 섞이지 않는다",
          "첫 질문" not in r.get("text", "") and "긴 것" not in r.get("text", ""))

    # escape 역연산 — 파일엔 `\## 7`, 회상엔 `## 7` (Mechanism §8 3항)
    p = ROOT / w["path"]
    check("파일에는 escape된 채로 있다", "\\## 7" in p.read_text(encoding="utf-8"))
    check("회상은 escape를 되돌린다",
          "\n## 7\n" in r.get("text", "") and "\\##" not in r.get("text", ""), r)

    # 절단은 조용히 하지 않는다
    r3 = _w(raw.read_round, VAULT, w["round_refs"][2], 500)
    check("긴 라운드는 잘린다", len(r3.get("text", "")) == 500, r3)
    check("자른 사실과 원 길이를 알린다",
          r3.get("truncated") is True and r3.get("chars") > 3000, r3)

    # 좌표가 없으면 목차까지만 — 본문을 쏟지 않는다
    idx = _w(raw.read_round, VAULT, f"{SP}/_cited/{REC}.md")
    check("index 없으면 목차", idx.get("rounds") == 3 and "text" not in idx, idx)
    check("목차는 미리보기만 싣는다",
          [x["preview"] for x in idx["index"]][:2] == ["첫 질문", "둘째 질문"], idx)

    # 표기 관용 — 저장 표기·맨 표기 모두 같은 좌표로 읽힌다
    for label, ref in (("위키링크", f"[[{SP}/_cited/{REC}.md#1]]"),
                       ("맨 표기", f"{SP}/_cited/{REC}.md#1")):
        check(f"좌표 표기: {label}", _w(raw.read_round, VAULT, ref).get("index") == 1)

    # 근거 줄의 좌표 — 옛 이름은 이름 그대로, 해시 이름은 줄인 앞자리, 이어진 라운드는 `#N–M`
    # (Mechanism §8 9항·§9 8항). 회상이 같은 좌표를 돌려주므로 근거 줄에 그대로 옮긴다.
    by_name = _w(raw.read_round, VAULT, f"{REC}#2")
    check("옛 이름 좌표로 연다", by_name.get("index") == 2 and by_name.get("coordinate") == f"{REC}#2", by_name)
    span = _w(raw.read_round, VAULT, f"{REC}#1–2")
    check("이어진 라운드를 한 번에 연다", [x.get("index") for x in span.get("rounds", [])] == [1, 2], span)
    check("거꾸로 된 범위는 거부한다", _w(raw.read_round, VAULT, f"{REC}#2–1").get("ok") is False)
    # 범위는 있는 라운드만 예산 안에서 편다 — 라운드마다 200자, 본문 합이 max_chars를 넘지 않는다
    check("없는 라운드가 낀 범위는 거부한다", _w(raw.read_round, VAULT, f"{REC}#2–100000000").get("ok") is False)
    check("라운드마다 200자를 못 주는 범위는 거부한다", _w(raw.read_round, VAULT, f"{REC}#1–3", 599).get("ok") is False)
    tight = _w(raw.read_round, VAULT, f"{REC}#1–3", 600)
    check("범위는 예산을 라운드마다 나눈다",
          [len(x.get("text", "")) <= 200 for x in tight.get("rounds", [])] == [True] * 3
          and tight["rounds"][2].get("truncated") is True, tight)
    _w(raw.append_rounds, VAULT, S, "claude-ab12cd34" + "0" * 24, [{"user": "해시 질문", "agent": "해시 응답"}], SP)
    short = _w(raw.read_round, VAULT, "ab12cd34#1")
    check("줄인 해시 좌표로 연다",
          short.get("coordinate") == "ab12cd34#1" and "해시 질문" in short.get("text", ""), short)

    # 기록 목록 — 좌표를 모를 때의 출발점. 루트 `_cited/` 한 자리다(v5.2)
    ls = _w(raw.list_records, VAULT)
    mine = [x for x in ls.get("records", []) if x["record"] == REC]
    check("기록 목록", len(mine) == 1 and ls.get("scope") == "_cited", ls.get("scope"))
    check("목록은 라운드 수를 센다", bool(mine) and mine[0]["rounds"] == 3, mine)
    check("옛 자리의 목록은 비어 있다", _w(raw.list_records, VAULT, SP).get("records") == [])

    # 봉쇄는 쓰기와 같은 규율이다 — 읽기라고 느슨하면 vault 밖을 읽는 창이 된다
    for label, ref in (
            ("vault 밖 탈출", "[[../../../../etc/passwd]]"),
            ("기록 구획 밖 노드", f"[[{SP}/어떤노드.md]]"),
            ("없는 기록", f"[[{SP}/_cited/없다.md#1]]")):
        check(f"회상 봉쇄: {label}", _w(raw.read_round, VAULT, ref).get("ok") is False)
    miss = _w(raw.read_round, VAULT, f"[[{SP}/_cited/{REC}.md#9]]")
    check("없는 라운드는 있는 것을 알려준다",
          miss.get("ok") is False and "[1, 2, 3]" in str(miss.get("violations")),
          miss)

    # 목차의 길이와 본문의 길이는 같은 것을 재야 한다 — 예산을 재려고 목차를
    # 본 호출자가 실제 응답과 다른 수를 쥐면 안 된다(감사 지적).
    check("목차 chars == 회상 chars",
          [x["chars"] for x in idx["index"]]
          == [_w(raw.read_round, VAULT, r).get("chars") for r in w["round_refs"]],
          (idx["index"], w["round_refs"]))


# ── 18d. 형식이 어긋난 `space`를 조용히 버리지 않는다 (감사 적발) ───────────
def test_raw_space_misdiagnosis():
    """결속이 **있는데도** 형식이 틀린 `space` 하나 때문에 "결속이 없다"고
    답하던 오진의 고정. 틀린 원인을 지목하는 거부는 거부하지 않느니만 못하다 —
    받는 쪽은 멀쩡한 프로젝트 키를 바꿔가며 헤맨다(코드를 보지 않은 감사에서 실측).
    자리는 scope 기억이다 — 인용 기록은 루트 한 자리라 착지 판정이 없다(v5.2)."""
    from osk import scope_memory as sm
    (ROOT / "00_Scope/WRawSp").mkdir(exist_ok=True)
    S = "repo/regr-space"
    ok = _w(sm.replace, VAULT, S, "- 엔트리", None, "00_Scope/WRawSp")
    check("결속을 만든다", ok.get("ok"), ok)
    check("결속에 들었다", "WRawSp" in write.bindings(VAULT, S)["scopes"], write.bindings(VAULT, S))
    write.set_primary(VAULT, S, "WRawSp", "시험 지정")
    check("주된 scope가 섰다", write.resolve_session(VAULT, S) == "WRawSp")

    r = _w(sm.replace, VAULT, S, "- 엔트리2", ok.get("hash"), "WRawSp")   # 맨 이름
    v = " ".join(r.get("violations", []))
    check("형식 오류를 space 자신의 문제로 지목한다",
          r.get("ok") is False and "space 표기가 아니다" in v, r)
    check("결속이 있는데 '결속이 없다'고 하지 않는다", "결속이 없다" not in v, r)

    # 진짜 미결속·없는 scope의 진단은 그대로여야 한다 (오진을 반대로 만들지 않기)
    r2 = _w(sm.replace, VAULT, "repo/regr-unbound-2", "- x", None)
    check("진짜 미결속은 결속을 지목한다",
          "결속이 없다" in " ".join(r2.get("violations", [])), r2)
    r3 = _w(sm.replace, VAULT, S, "- x", ok.get("hash"), "00_Scope/없는스코프")
    check("없는 scope는 scope를 지목한다",
          "scope가 아니다" in " ".join(r3.get("violations", [])), r3)
    check("결속만으로도 계속 이어진다",
          _w(sm.replace, VAULT, S, "- 엔트리3", ok.get("hash")).get("ok"))


# ── 18d-2. 인용 기록은 루트 한 자리 — scope를 말해도 번지지 않는다 (헌법 4조 3항) ──
def test_raw_binding_confines_scope():
    """인용 기록은 루트 `_cited/` 한 자리다(헌법 4조 3항 · Mechanism §9 5항). 어느 scope를
    말해도 같은 기록에 잇고, scope 아래에 기록이 생기지 않으며, 인용은 결속을 세우지 않는다."""
    from osk import raw
    for n in ("WBindA", "WBindB"):
        (ROOT / f"00_Scope/{n}").mkdir(exist_ok=True)
    S = "repo/regr-bind"
    check("첫 쓰기",
          _w(raw.append_rounds, VAULT, S, "rec", [{"user": "q", "agent": "a"}],
             "00_Scope/WBindA").get("indices") == [1])
    r = _w(raw.append_rounds, VAULT, S, "rec", [{"user": "딴데", "agent": "씀"}],
           "00_Scope/WBindB")
    check("다른 scope를 말해도 같은 기록에 잇는다", r.get("indices") == [2], r)
    check("scope 아래에 기록이 생기지 않았다",
          not (ROOT / "00_Scope/WBindA/_cited").exists()
          and not (ROOT / "00_Scope/WBindB/_cited").exists())
    check("정본은 하나뿐", _rounds_of(S, "rec") == 2)
    check("인용은 결속하지 않는다",
          write.resolve_session(VAULT, S) is None and write.bindings(VAULT, S)["scopes"] == [], write.bindings(VAULT, S))
    check("space 생략도 통과",
          _w(raw.append_rounds, VAULT, S, "rec2",
             [{"user": "q", "agent": "a"}]).get("indices") == [1])


# ── 18e. 재시도 중복 거부 (Mechanism §9 7항 · 시행령 §2 1항) ────────────────
def test_raw_replay_rejected():
    """응답이 유실되면 호출자는 같은 배치를 다시 보낸다. 직전 꼬리와 내용이
    같은 배치는 쓰지 않되, 조용히 접지 않고 **거부**한다 — 호출자가 무슨 일이
    있었는지 알게 한다."""
    from osk import raw
    (ROOT / "00_Scope/WRawRp").mkdir(exist_ok=True)
    SP, S = "00_Scope/WRawRp", "repo/regr-replay"
    B = [{"user": "q1", "agent": "a1"}, {"user": "q2", "agent": "a2"}]

    check("최초 배치", _w(raw.append_rounds, VAULT, S, "replay-rec", B, SP).get("indices") == [1, 2])
    r = _w(raw.append_rounds, VAULT, S, "replay-rec", B)
    check("같은 배치 재시도는 거부", r.get("ok") is False, r)
    check("거부가 어디를 보라고 알려준다",
          "read_cited" in " ".join(r.get("violations", [])), r)
    check("거부는 아무것도 쓰지 않았다", _rounds_of(S, "replay-rec") == 2)

    check("꼬리 일부만 재시도해도 거부",
          _w(raw.append_rounds, VAULT, S, "replay-rec", [B[1]]).get("ok") is False)
    check("다른 내용은 통과",
          _w(raw.append_rounds, VAULT, S, "replay-rec",
             [{"user": "q3", "agent": "a3"}]).get("indices") == [3])
    # 꼬리가 아닌 옛 라운드의 반복은 재시도가 아니다 — 정말 같은 말이 다시 오갔을
    # 수 있고, 그것을 막으면 대화를 기록하지 못한다.
    check("꼬리가 아니면 같은 내용도 통과",
          _w(raw.append_rounds, VAULT, S, "replay-rec", [B[0]]).get("indices") == [4])

    # 비밀값이 든 라운드의 재시도 — 저장본은 치환됐으므로 치환 전으로 견주면
    # 다른 것이 되어 빠져나간다. 판정은 치환 뒤로 한다.
    SEC = [{"user": "키 " + "AKIA" + "IOSFODNN7EXAMPLE" + " 준다",
            "agent": "받았다"}]
    check("비밀값 라운드 최초",
          _w(raw.append_rounds, VAULT, S, "replay-sec", SEC, SP).get("indices") == [1])
    check("비밀값 라운드의 재시도도 거부",
          _w(raw.append_rounds, VAULT, S, "replay-sec", SEC).get("ok") is False)
