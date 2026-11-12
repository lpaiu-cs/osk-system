"""회귀 수트의 한 갈래: scope 기억 — 상한과 거부, 앵커 일괄, 훅, 복구.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, subprocess, sys, tempfile
from pathlib import Path
from unittest import mock

from regression.support import check, ENGINE, _hook_context, node_text, rmtree_force, ROOT, VAULT, _w
from osk import write, placement  # noqa: E402


# ── 19. scope 기억 — 상한이 곧 승격의 문턱 (Mechanism §9-2) ─────────────────
def test_scope_memory():
    """상한은 저장 용량의 제한이 아니라 문턱이다. 그래서 초과는 **거부**하고,
    거부는 잔여·해시·넘긴 자수와 정리 안내를 돌려준다(전문은 싣지 않는다 —
    §9-2 5항). 자동 절단·자동 요약은 두지 않는다."""
    from osk import scope_memory as wm
    for n in ("WWm", "WWmB"):
        (ROOT / f"00_Scope/{n}").mkdir(exist_ok=True)
    S = "repo/regr-wm"

    r = _w(wm.replace, VAULT, S, "- 첫 엔트리", None, "00_Scope/WWm")
    write.set_primary(VAULT, S, "WWm", "시험 지정")       # 쓰기는 결속에 더할 뿐이다(v5.2)
    # 저장본은 개행으로 끝난다 — 길이는 저장된 전문 기준이어야 잔여 계산이 맞다
    check("최초 쓰기", r.get("ok") and r["chars"] == len("- 첫 엔트리"), r)
    check("성공이 저장한 것은 되읽어 확인된다",
          _w(wm.read, VAULT, S)["text"].strip() == "- 첫 엔트리", r)
    check("잔여를 함께 낸다", r["remaining"] == wm.LIMIT - r["chars"], r)

    # §9-2 5항 — 성공한 쓰기는 전문을 싣지 않는다. 호출자가 방금 보낸 바이트
    # 그대로라 정보가 아니고, 14일 실측에서 osk 비용의 10.4%를 썼다. 대신
    # 잔여·해시는 남는다: 잔여는 호출자가 셀 수 없고(초과폭 중앙값 49자),
    # 해시는 다음 쓰기가 재읽기 없이 연쇄하는 값이다.
    check("성공은 전문을 싣지 않는다", "text" not in r, sorted(r))
    check("성공도 자수·상한·잔여는 낸다",
          {"chars", "limit", "remaining"} <= set(r), sorted(r))
    check("성공이 낸 해시가 저장물과 맞는다 — 연쇄가 산다",
          r["hash"] == _w(wm.read, VAULT, S)["hash"], r)
    check("읽기는 전문을 낸다", "text" in _w(wm.read, VAULT, S))
    check("퇴출 원칙이 성공 응답에도 실린다", "지우는 것이 정상" in r["eviction"])
    # 첫 쓰기는 결속에 더한다 — 주된 scope는 지정이 세운다(v5.2)
    check("첫 쓰기가 결속에 더한다", "WWm" in write.bindings(VAULT, S)["scopes"])
    check("이후에는 space 없이 읽힌다", _w(wm.read, VAULT, S).get("ok") is True)

    h = r["hash"]
    check("hash 없이 덮어쓰기 거부",
          _w(wm.replace, VAULT, S, "- 다른 것").get("ok") is False)
    check("틀린 hash 거부",
          _w(wm.replace, VAULT, S, "- 다른 것", "sha256:00").get("ok") is False)
    check("거부해도 전문을 돌려준다",
          _w(wm.replace, VAULT, S, "- 다른 것")["text"].strip() == "- 첫 엔트리")
    r2 = _w(wm.replace, VAULT, S, "- 갱신됨", h)
    check("맞는 hash로 전체 치환",
          r2.get("ok") and _w(wm.read, VAULT, S)["text"].strip() == "- 갱신됨", r2)

    # 상한 초과 — 거부하고, 파일은 그대로이며, 안내가 순서를 준다
    big = chr(10).join(f"- 엔트리 {i} 길게 이어지는 내용이 계속된다" for i in range(120))
    r3 = _w(wm.replace, VAULT, S, big, r2["hash"])
    v = " ".join(r3.get("violations", []))
    check("상한 초과는 거부", r3.get("ok") is False, r3)
    check("거부가 넘친 크기를 알린다", r3.get("rejected_chars", 0) > wm.LIMIT, r3)
    # §9-2 5항 — 상한 초과에는 전문을 싣지 않는다. 이 거부는 디스크를 바꾸지
    # 않았고, 호출자가 다듬을 것은 저장본이 아니라 방금 보낸 자기 초안이다.
    # 해시 불일치(위)와 갈리는 지점이 그것이다 — 거기선 저장본이 호출자가
    # 모르는 것이 돼 있으므로 전문이 필수다.
    check("상한 초과는 전문을 싣지 않는다", "text" not in r3, sorted(r3))
    check("상한 초과도 해시·잔여는 낸다",
          {"hash", "remaining", "rejected_chars"} <= set(r3), sorted(r3))
    check("거부는 파일을 건드리지 않는다",
          _w(wm.read, VAULT, S)["text"].strip() == "- 갱신됨")
    check("거부가 낸 해시로 곧장 이어 쓸 수 있다 — 재읽기 불필요",
          r3["hash"] == _w(wm.read, VAULT, S)["hash"], r3)
    check("안내가 끝난 상태의 정리와 보존할 지식을 구분한다",
          "끝난 작업 상태·중복은 정리" in v and "남길 지식" in v, v)
    check("안내가 기존 노드 갱신을 먼저 말한다", "update_node" in v and "먼저 보존" in v, v)
    check("안내가 배선을 요구한다", "배선" in v, v)
    check("안내가 착지 scope를 알린다", "00_Scope/WWm" in v, v)

    # 비밀값 필터의 새 적용 지점 — 작업 기억은 vault 안이고 에이전트가 쓴다
    r4 = _w(wm.replace, VAULT, S, "- 토큰 ghp_" + "a" * 36 + " 조심",
            _w(wm.read, VAULT, S)["hash"])
    check("작업 기억에도 비밀값 필터", r4.get("filtered") == ["github-token"], r4)
    # 응답이 아니라 **저장물**에 남지 않아야 한다 — 커밋되는 것이 그것이다
    check("원본이 남지 않는다", "ghp_" not in _w(wm.read, VAULT, S)["text"], r4)
    # 성공이 전문을 안 실으므로 치환 결과가 안 보인다. 표면 예산이 막혀
    # 상주 스키마엔 못 적으니, 치환이 실제로 난 때만 응답이 알려야 한다 —
    # 안 알리면 호출자의 기억과 저장본이 갈린 채 다음 통합이 그 위에 얹힌다.
    check("치환되면 다시 읽으라고 알린다",
          "읽어" in r4.get("filtered_note", ""), r4)
    # 치환이 없으면 붙지 않는다 — 모든 응답에 실으면 소음이고 예산이다.
    # 여기서 새로 쓰지 않는다: 쓰기를 끼우면 아래가 쥔 `r4["hash"]`가 낡는다.
    check("치환이 없으면 안 붙는다", "filtered_note" not in r2, sorted(r2))

    # 다른 scope의 기억은 그 scope의 해시로 잇는다 — 남의 해시는 거부다
    check("다른 scope의 해시로는 거부",
          _w(wm.replace, VAULT, S, "- x", r4["hash"], "00_Scope/WWmB").get("ok") is False)
    check("거부는 파일을 만들지 않는다",
          not (ROOT / "00_Scope/Workbench/_scope_memory/WWmB.md").exists())

    # 전부 비우는 것은 정상 동작이다 — 퇴출이 유실이 아니라는 계약의 실행형.
    # 다만 직전 상태가 어디에도 남지 않으므로, 크게 줄면 사라진 전문을 함께
    # 돌려줘 같은 턴 안에서 되돌릴 수 있게 한다.
    before = _w(wm.read, VAULT, S)["text"]
    r5 = _w(wm.replace, VAULT, S, "", r4["hash"])
    check("전부 비울 수 있다", r5.get("ok") and r5["chars"] == 0, r5)
    check("비우면 사라진 전문이 온다", r5.get("replaced_text") == before, r5)
    check("사라진 양을 알린다", "사라졌다" in r5.get("note", ""), r5)
    check("되돌리는 법을 알린다", "그대로 다시 보내라" in r5.get("note", ""), r5)
    back = _w(wm.replace, VAULT, S, r5["replaced_text"], r5["hash"])
    check("돌려준 전문으로 복원된다",
          back.get("ok") and _w(wm.read, VAULT, S)["text"] == before, back)
    # 늘어나거나 조금 줄어든 쓰기에는 붙이지 않는다 — 모든 응답에 실으면 소음이다
    grow = _w(wm.replace, VAULT, S, before + chr(10) + "- 한 줄 더", back["hash"])
    check("늘어난 쓰기엔 붙지 않는다", "replaced_text" not in grow, sorted(grow))

    # 경계 — 문서가 정한 계수만큼은 실제로 담겨야 한다. 저장본의 개행을 계수에
    # 넣으면 유효 상한이 1499가 되고, 문서를 따른 호출자가 반드시 한 번 튕긴다.
    exact = _w(wm.replace, VAULT, S, "가" * wm.LIMIT, _w(wm.read, VAULT, S)["hash"])
    check("정확히 상한은 통과", exact.get("ok") and exact["chars"] == wm.LIMIT, exact)
    over = _w(wm.replace, VAULT, S, "나" * (wm.LIMIT + 1), exact["hash"])
    check("상한+1은 거부", over.get("ok") is False, over)
    check("거부가 보낸 글자수를 센다",
          over.get("rejected_chars") == wm.LIMIT + 1, over)

    # 정규화 — 상한은 NFC 정본 형태 위에서 잰다 (Mechanism §9-2 9항)
    nfd = _w(wm.replace, VAULT, S, "가" * 10, _w(wm.read, VAULT, S)["hash"])
    check("NFD 자모는 NFC로 접혀 세어진다", nfd.get("chars") == 10, nfd)

    # frontmatter로 오독되면 검증기가 통째로 FAIL한다 — 통로에서 막는다
    dash = _w(wm.replace, VAULT, S, "---" + chr(10) + "본문", _w(wm.read, VAULT, S)["hash"])
    check("`---` 선두 거부", dash.get("ok") is False, dash)
    check("거부 뒤에도 배치 검증기는 깨끗하다", not placement.layout_violations(VAULT))

    # 승격 지시가 **근거가 무엇인지**까지 알려야 한다. 말해주지 않으면 코드를
    # 못 보는 호출자는 (2)에서 멈춘다(감사에서 실측). 근거는 그 지식이 나온
    # 곳이며 — 작업 기억은 경유지이지 원료가 아니다(헌법 9조 1항 "원료가 된",
    # Workbench 계약 4.2 "작업 상태는 근거로 참조하지 않는다").
    big2 = "다" * (wm.LIMIT + 100)
    ov = " ".join(_w(wm.replace, VAULT, S, big2, _w(wm.read, VAULT, S)["hash"])
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

    # 결속 밖 scope의 기억 쓰기는 통과하고 결속에 더한다 (v5.2 — 시행령 §3 10항)
    xs = _w(wm.replace, VAULT, S, "- x", None, "00_Scope/WWmB")
    check("결속 밖 scope의 기억 쓰기는 통과한다",
          xs.get("ok") and (ROOT / "00_Scope/Workbench/_scope_memory/WWmB.md").exists(), xs)
    check("주된 scope 밖의 기억은 안내에 space를 명시한다 (#191 리뷰)",
          'space="00_Scope/WWmB"' in xs.get("session_note", "")
          and 'space="00_Scope/WWmB"' in _w(wm.read, VAULT, S, "00_Scope/WWmB").get("session_note", ""), xs)
    check("그 쓰기가 결속에 더한다 — 주된 scope는 그대로",
          write.bindings(VAULT, S)["scopes"] == ["WWm", "WWmB"] and write.resolve_session(VAULT, S) == "WWm",
          write.bindings(VAULT, S))

    # 이 계층은 git을 부르지 않는다 — 동기화 계약은 데몬이 소유한다
    src = (Path(wm.__file__).read_text(encoding="utf-8"))
    check("wm은 vault_sync를 부르지 않는다",
          "vault_sync" not in src and "commit_push" not in src)
    check("응답에 sync 키가 없다", "sync" not in _w(wm.read, VAULT, S))


# ── 19a. 작업 상태를 근거로 걸면 계약 4.2로 진단한다 ──────────────────────
def test_workbench_state_not_evidence():
    """작업 기억을 `derived-from`으로 걸면 "scope 간 직접 참조"로 진단되던 것을
    바로잡는다. 그것은 위상 문제가 아니다 — 어느 scope에서 걸어도 계약이
    금지한다(Workbench 계약 4.2). 위상으로 진단하면 받는 쪽은 scope를 바꿔
    보려 하고, 그 길은 없다. 감사가 잡은 `space` 오진과 같은 계열이다."""
    from osk import scope_memory as wm
    (ROOT / "00_Scope/WEv").mkdir(exist_ok=True)
    idx = ROOT / "00_Scope/WEv/WEv.md"     # 첫-노드 규칙 충족용 허브 골격
    if not idx.exists():
        idx.write_text(node_text("260802-zzzz-wevi", "WEv 허브"),
                       encoding="utf-8")
    _w(wm.replace, VAULT, "repo/regr-ev", "- 경유지 내용", None, "00_Scope/WEv")
    r = _w(write.create_node, VAULT, "regr-ev-node", "근거 오지정 시험", "본문",
           "fable-5", space="00_Scope/WEv",
           edges={"derived-from": "[[00_Scope/Workbench/_scope_memory/WEv]]"})
    v = " ".join(r.get("violations", []))
    check("작업 상태 근거는 거부", r.get("ok") is False, r)
    check("위상이 아니라 계약 4.2로 진단한다",
          "작업 상태는 근거로 쓰지 않는다" in v and "scope 간 직접 참조" not in v, v)
    check("근거가 무엇인지 함께 말한다", "그 지식이 나온 곳" in v, v)


# ── 19b. 작업 기억 훅 경로 — `show`는 전문 그대로 낸다 ──────────────────────
def test_scope_memory_edits():
    """앵커 일괄 쓰기 (§9-2 4항) — 부분 변경의 증거는 해시가 아니라 앵커다.

    무엇을 망가뜨리면 실패하는가: 앵커 경로가 해시를 요구하게 되면 · 앵커가
    없거나 여러 곳인데 쓰면 · 하나가 틀렸는데 나머지를 쓰면(전부 아니면 전무) ·
    상한을 순결과가 아니라 중간 상태에 걸면 · 앵커 불일치 거부가 전문을 안
    실으면 · 상한 초과 거부가 전문을 실으면 · `text`와 병용을 허용하면 · 연속
    거부 계수가 안 오르거나 성공이 안 지우면."""
    from osk import scope_memory as wm
    (ROOT / "00_Scope/WWmE").mkdir(exist_ok=True)
    S = "repo/regr-wm-edits"
    p = ROOT / "00_Scope/Workbench/_scope_memory/WWmE.md"

    # 빈 기억에 edits는 갈 데가 없다 — 첫 쓰기는 text
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "x", "new_text": "y"}], space="00_Scope/WWmE")
    check("빈 기억에 edits는 거부", not r.get("ok") and "첫 쓰기" in " ".join(r["violations"]), r)
    r = _w(wm.replace, VAULT, S, "# 머리\n- 첫째 줄\n- 둘째 줄\n- 셋째 줄\n", None, "00_Scope/WWmE")
    check("text로 첫 쓰기", r.get("ok"), r)
    write.set_primary(VAULT, S, "WWmE", "시험 지정")
    h0 = r["hash"]

    # 계약의 핵심 — 해시 없이, 앵커로
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "- 둘째 줄\n", "new_text": "- 둘째 줄을 고쳤다\n"}])
    check("edits는 해시 없이 통과", r.get("ok"), r)
    check("저장물에 반영", "둘째 줄을 고쳤다" in wm._read(p), wm._read(p))
    check("다른 줄은 그대로", "- 셋째 줄" in wm._read(p))
    check("성공은 전문을 안 싣는다(5항)", "text" not in r, sorted(r))
    check("해시가 바뀌었다", r["hash"] != h0)

    # 유일성
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "줄", "new_text": "행"}])
    check("여러 곳이면 거부", not r.get("ok") and "유일하게" in " ".join(r["violations"]), r)
    check("여러 곳 거부는 파일을 안 건드린다", wm._read(p).count("줄") == 3)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "- 없는 줄\n", "new_text": ""}])
    check("없으면 거부", not r.get("ok") and "나오지 않는다" in " ".join(r["violations"]), r)
    check("앵커 불일치 거부는 전문을 싣는다(5항)", r.get("text", "").startswith("# 머리"), sorted(r))
    check("거부문이 전문에서 복사하라고 가리킨다", "복사" in " ".join(r["violations"]), r)

    # 전부 아니면 전무 — 첫 항목은 맞고 둘째가 틀리면 첫 항목도 안 쓴다
    before = wm._read(p)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "- 첫째 줄\n", "new_text": "- 첫째 줄(수정)\n"},
                                 {"old_text": "- 없는 줄\n", "new_text": ""}])
    check("하나 틀리면 전부 안 쓴다", not r.get("ok") and wm._read(p) == before, r)

    # 그 시점의 작업본 — 앞 연산이 만든 자리를 뒤 연산이 앵커로 쓴다
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "- 셋째 줄", "new_text": "- 셋째 줄\n- 넷째 줄"},
                                 {"old_text": "- 넷째 줄", "new_text": "- 넷째 줄(뒤 연산)"}])
    check("뒤 연산이 앞 연산의 결과에 앵커한다", r.get("ok") and "넷째 줄(뒤 연산)" in wm._read(p), r)

    # 형태 — 잠금 밖에서 거른다
    check("text와 edits 병용은 거부",
          not _w(wm.replace, VAULT, S, "전문", r["hash"], edits=[{"old_text": "a", "new_text": "b"}]).get("ok"))
    check("빈 edits는 거부", not _w(wm.replace, VAULT, S, edits=[]).get("ok"))
    check("형태 틀린 항목은 거부", not _w(wm.replace, VAULT, S, edits=[{"old_text": "a"}]).get("ok"))
    check("빈 앵커는 거부", not _w(wm.replace, VAULT, S, edits=[{"old_text": "", "new_text": "b"}]).get("ok"))
    check("old==new는 거부", not _w(wm.replace, VAULT, S, edits=[{"old_text": "- 첫째 줄\n", "new_text": "- 첫째 줄\n"}]).get("ok"))

    # 상한은 순결과에만 — 넣기를 먼저 두어 중간 상태는 넘치고 순결과는 맞게 한다
    big_a, big_b = "가" * 700, "나" * 700
    r = _w(wm.replace, VAULT, S, "# 머리\n- A " + big_a + "\n- B " + big_b + "\n", _w(wm.read, VAULT, S)["hash"])
    check("상한 근처 기준선", r.get("ok") and r["chars"] > 1400, r)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "# 머리\n", "new_text": "# 머리\n- C " + "다" * 600 + "\n"},
                                 {"old_text": "\n- B " + big_b, "new_text": ""}])
    check("중간은 넘치고 순결과는 맞으면 통과", r.get("ok"), r)
    check("B는 빠지고 C는 들어갔다", "- C" in wm._read(p) and "- B" not in wm._read(p))

    # 순결과 초과 — 거부, 전문 없음, 넘긴 자수, 재시도 안내는 edits 형태로
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "# 머리\n", "new_text": "# 머리\n- D " + "라" * 300 + "\n"}])
    check("순결과 초과는 거부", not r.get("ok") and r.get("rejected_chars", 0) > wm.LIMIT, r)
    check("상한 초과 거부는 전문을 안 싣는다(5항)", "text" not in r, sorted(r))
    check("재시도 안내가 한 edits로 빼고 넣으라 한다", "한 `edits`에" in " ".join(r["violations"]), r)

    # 연속 거부 계수 — 3회째에 "접고" 가 실리고, 성공이 지운다
    add_big = [{"old_text": "# 머리\n", "new_text": "# 머리\n- E " + "마" * 300 + "\n"}]
    r2 = _w(wm.replace, VAULT, S, edits=add_big)
    r3 = _w(wm.replace, VAULT, S, edits=add_big)
    check("2회째엔 아직 접으라 하지 않는다", "접고" not in " ".join(r2["violations"]), r2)
    check("3회째에 접으라 한다", "접고" in " ".join(r3["violations"]) and "3회째" in " ".join(r3["violations"]), r3)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "- C " + "다" * 600 + "\n", "new_text": ""}])
    check("자리를 만들면 통과", r.get("ok"), r)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "# 머리\n", "new_text": "# 머리\n- F " + "바" * 900 + "\n"}])
    check("성공이 계수를 지운다 — 다음 초과는 1회째", not r.get("ok") and "접고" not in " ".join(r["violations"]), r)

    # 기존 부속 계약이 edits에도 산다
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": "# 머리\n", "new_text": "# 머리\n- 토큰 ghp_" + "a" * 36 + "\n"}])
    check("edits에도 비밀값 필터", r.get("ok") and r.get("filtered") == ["github-token"] and "filtered_note" in r, r)
    cur = wm._read(p)
    r = _w(wm.replace, VAULT, S, edits=[{"old_text": cur, "new_text": "# 머리\n"}])
    check("크게 줄면 replaced_text가 온다", r.get("ok") and r.get("replaced_text") == cur, sorted(r))


def test_cadence_hook():
    """Own-conversation timing survives empty memory, resume and other writers."""
    import io, importlib, types, uuid
    from osk import scope_memory as wm, conversation_capture
    hooks = ENGINE / "scripts/hooks"
    sys.path.insert(0, str(hooks))
    hook = importlib.import_module("claude_prompt_submit")
    start_hook = importlib.import_module("claude_session_start")
    with tempfile.TemporaryDirectory(prefix="osk-cadence-test-") as td:
        cwd = Path(td) / "cadence-project"
        cwd.mkdir()
        sid = "cadence-" + uuid.uuid4().hex
        transcript = Path(td) / "native.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n", encoding="utf-8")
        payload = {"session_id": sid, "harness": "codex", "cwd": str(cwd),
                   "transcript_path": str(transcript)}
        write.bind_session(VAULT, cwd.name, "W1")

        def turn(module=hook, event="UserPromptSubmit"):
            buf = io.BytesIO()
            real_in, real_out = sys.stdin, sys.stdout
            # 턴마다 다른 사건이다 — 같은 입력의 되풀이는 이중 등록의 두 번째 호출로 가려진다.
            payload["turn_id"] = uuid.uuid4().hex
            try:
                sys.stdin = io.StringIO(json.dumps(payload))
                sys.stdout = types.SimpleNamespace(buffer=buf)
                module.main()
            finally:
                sys.stdin, sys.stdout = real_in, real_out
            return _hook_context(buf.getvalue(), event)

        outs = [turn() for _ in range(9)]
        check("미설정은 처음 경고하고 2~8턴은 조용하며 9턴에 검토",
              "검토 경고" in outs[0] and "[osk 케이던스" not in outs[0]
              and not any(outs[1:8]) and "함께 실어" in outs[8], outs)
        check("원문 부재를 증류 완료로 가장하지 않는다", "아직 없다" in outs[8], outs[8])
        for _ in range(3):
            turn()
        before = conversation_capture.status(VAULT, "codex", sid)["prompt_count"]
        turn(start_hook, "SessionStart")
        check("같은 대화 재개는 통합 시계를 지우지 않는다", conversation_capture.status(VAULT, "codex", sid)["prompt_count"] == before)
        prior = wm.read(VAULT, cwd.name)
        wm.replace(VAULT, cwd.name, "- 다른 작성자가 보존한 지식", prior["hash"])
        outs = [turn() for _ in range(3)]
        check("다른 작성자의 공유 기억 갱신은 내 15턴 검토를 닫지 않는다",
              not any(outs[:2]) and "단독 턴이어도" in outs[2] and "다른 작성자" in outs[2], outs)
        check("15턴 뒤 매 턴 재촉하지 않는다", turn() == "")
        check("내 대화의 계수는 공유 hash와 별개", conversation_capture.status(VAULT, "codex", sid)["prompt_count"] == 16)
        wm.replace(VAULT, cwd.name, prior["text"], wm.read(VAULT, cwd.name)["hash"])


def test_scope_recovery_handoff():
    """초과 → 중단 → 새 프로세스 훅 → no-op → 실제 통합의 경계를 시험한다."""
    import uuid
    from osk import scope_memory as wm, evictions as ev
    S, scope = "regr-recovery", "WRecovery"
    (ROOT / f"00_Scope/{scope}").mkdir(exist_ok=True)
    wm._pending_clear(VAULT, S)
    cur = "- 남길 지식 " + "가" * 700 + "\n- 끝난 상태 " + "나" * 700
    r = _w(wm.replace, VAULT, S, cur, None, f"00_Scope/{scope}")
    write.set_primary(VAULT, S, scope, "시험 지정")
    check("복구 기준선", r.get("ok"), r)
    check("쓰기 응답은 세션 키와 실제 변경을 싣는다",
          r.get("session") == S and r.get("canonical_session") == S and r.get("changed") is True, r)
    before = len(ev.records(VAULT))
    draft = cur + "\n- 거부된 새 초안 " + "다" * 200
    first = None
    for n in range(3):
        rejected = _w(wm.replace, VAULT, S, draft, r["hash"])
        check(f"복구 초과 {n + 1}회", not rejected.get("ok") and rejected.get("recovery"), rejected)
        if first is None:
            first = rejected["recovery"]["since"]
        check("재거부로 관측 시점이 늦춰지지 않는다", rejected["recovery"]["since"] == first)
        check("거부는 scope 이름 대신 호출한 세션 키를 이어 준다",
              rejected.get("session") == S and f'session="{S}"' in rejected.get("session_note", "")
              and "저장 위치" in rejected.get("session_note", ""), rejected)
    check("3회째에는 현재 재시도를 접는다", "접고" in " ".join(rejected["violations"]))
    check("초안은 표식에 보관하지 않는다", "거부된 새 초안" not in wm._pending_path(VAULT, S).read_text(encoding="utf-8"))
    check("실패만으로 evict가 생기지 않는다", len(ev.records(VAULT)) == before)
    check("읽기에 복구 대기가 보인다", _w(wm.read, VAULT, S).get("recovery", {}).get("session") == S)
    write.alias_session(VAULT, "regr-recovery-old", S)
    check("별칭도 같은 복구 대기", wm.recovery(VAULT, "regr-recovery-old") == wm.recovery(VAULT, S))
    alias_read = _w(wm.read, VAULT, "regr-recovery-old")
    check("별칭은 호출 인자와 정본 키를 구별한다",
          alias_read.get("session") == "regr-recovery-old" and alias_read.get("canonical_session") == S
          and 'session="regr-recovery-old"' in alias_read.get("session_note", ""), alias_read)

    # 실제 훅 파일을 새 프로세스에서 실행한다. Codex/Claude 공통 wire 입력이다.
    cwd = Path(tempfile.mkdtemp(prefix="osk-recovery-")) / S
    cwd.mkdir()
    sid = "regr-" + uuid.uuid4().hex
    hooks = ENGINE / "scripts" / "hooks"
    payload = {"session_id": sid, "cwd": str(cwd), "source": "startup",
               "hook_event_name": "SessionStart", "permission_mode": "default"}
    from osk import conversation_capture
    native = cwd / "native.jsonl"
    native.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n", encoding="utf-8")
    payload.update(harness="codex", transcript_path=str(native))
    env = {**os.environ, "OSK_VAULT_ROOT": str(ROOT), "PYTHONPATH": str(ENGINE)}

    def run_hook(name):
        event = "SessionStart" if name == "claude_session_start.py" else "UserPromptSubmit"
        payload["hook_event_name"] = event
        payload["turn_id"] = uuid.uuid4().hex   # 호출마다 다른 사건(이중 등록 판정과 구별)
        sub = subprocess.run([sys.executable, str(hooks / name)],
                             input=json.dumps(payload).encode("utf-8"), capture_output=True,
                             env=env, cwd=str(cwd), timeout=30)
        check("복구 훅 프로세스 성공", sub.returncode == 0, sub.stderr[-300:])
        return _hook_context(sub.stdout, event)

    try:
        unbound = cwd / "unbound"
        unbound.mkdir()
        payload["cwd"] = str(unbound)
        payload["session_id"] = "unbound-" + sid
        payload.pop("transcript_path")
        out = run_hook("claude_session_start.py")
        check("결속 없는 세션도 JSON으로 시작 안내를 주입한다",
              out.startswith("[osk 세션 시작") and "아직 scope 결속이 없다" in out)
        payload["cwd"] = str(cwd)
        payload.update(session_id=sid, transcript_path=str(native))
        out = run_hook("claude_session_start.py")
        check("새 세션은 evict 없이도 복구를 싣는다", "[osk scope 복구 대기" in out, out[:300])
        check("복구는 현재 저장본과 기존 노드 우선 행동을 싣는다", cur in out and "update_node" in out and "엔트리 단위" in out)
        check("세션 시작은 노드를 처음 쓰기 전의 overview를 안내한다",
              f'이 세션에서 노드를 처음 쓰기 전에 `overview(session="{S}")`를 한 번 불러 군집·허브 트리(`hub_tree`)·'
              '열린 사건·근거 재검토 후보를 확인하라. 새 노드는 허브 트리에서 주제를 덮는 가장 깊은 군집에 둔다'
              f'(시행령 §3 8항). 기억을 묻는 질문에는 `search(session="{S}")`를 먼저 쓴다. 노드를 새로 쓰기 전에도 '
              '그 주제로 먼저 search해 다른 scope와 Domain까지 비슷한 기존 노드를 찾고, 그 주장에 기대면 참조를 건다.'
              in out, out[:600])
        check("세션 시작은 허브 트리와 자리 규칙을 안내한다",
              "hub_tree" in out and "시행령 §3 8항" in out, out[:600])
        check("세션 시작은 scope를 세션 키로 바꾸지 않게 안내한다",
              f'session="{S}"' in out and "저장 위치" in out and "대신하지 않는다" in out)
        check("훅은 표식을 소비하지 않는다", wm.recovery(VAULT, S) is not None)
        payload["hook_event_name"] = "UserPromptSubmit"
        payload["prompt"] = "계속"
        # The unbound probe above uses its own ID, not this scope's conversation.
        for count in (8, 14):
            while conversation_capture.status(VAULT, "codex", sid)["prompt_count"] < count:
                conversation_capture.tick(VAULT, "codex", sid)
            out = run_hook("claude_prompt_submit.py")
            check(f"{count + 1}턴은 새 요약 추가 대신 복구를 싣는다",
                   "[osk scope 복구 대기" in out and "이 세션에서 배운 것을" not in out and cur in out)
            check("케이던스도 같은 세션 키의 복구를 안내한다",
                  f'session="{S}"' in out and "대신하지 않는다" in out)
        check("15턴 뒤 매 턴 재촉하지 않는다", run_hook("claude_prompt_submit.py") == "")

        native.write_text("malformed native transcript\n", encoding="utf-8")
        payload.pop("harness", None)  # Parsing during harness detection raises.
        env.pop("OSK_HARNESS", None)
        out = run_hook("claude_prompt_submit.py")
        check("전사 판독 실패도 scope 복구와 세션 키를 숨기지 않는다",
              "포착·통합 진단" in out and "[osk scope 복구 대기" in out
              and cur in out and f'session="{S}"' in out and "대신하지 않는다" in out)

        same = _w(wm.replace, VAULT, S, cur, r["hash"])
        check("동일 전문의 성공은 대기를 소비하지 않는다", same.get("ok") and same.get("recovery"), same)
        check("동일 전문은 changed false", same.get("changed") is False, same)
        same = _w(wm.replace, VAULT, S, edits=[{"old_text": "남길 지식", "new_text": "임시 표현"},
                                         {"old_text": "임시 표현", "new_text": "남길 지식"}])
        check("상쇄된 edits 성공도 대기를 소비하지 않는다", same.get("ok") and same.get("recovery"), same)
        check("상쇄된 edits는 changed false", same.get("changed") is False, same)
        check("no-op으로 evict도 늘지 않는다", len(ev.records(VAULT)) == before)

        # 구판의 키만 든 표식도 다음 세션에서 복구할 수 있어야 한다.
        wm._pending_path(VAULT, S).write_text(S, encoding="utf-8")
        check("구판 표식 판독과 시점 한계 표시", wm.recovery(VAULT, S)["legacy"])
        out = run_hook("claude_session_start.py")
        check("구판 표식도 새 훅에 보인다", "[osk scope 복구 대기" in out)
        status = subprocess.run([sys.executable, "-m", "osk.cli", "status"],
                                env=env, capture_output=True, timeout=30)
        parsed = json.loads(status.stdout)
        check("CLI는 evict와 복구를 별도로 싣는다",
              "evictions" in parsed and any(x["session"] == S for x in parsed["scope_recovery"]))

        # 다른 세션의 쓰기가 같은 scope를 바꿔도 이 세션의 미완료 의도를 대신
        # 완료했다고 할 수 없다. 현재 본문으로 다시 판단하고 CAS/앵커를 쓴다.
        other = _w(wm.replace, VAULT, "regr-recovery-other", cur + "\n- 공유 변경", r["hash"], f"00_Scope/{scope}")
        check("다른 작성자의 성공은 기존 복구를 소비하지 않는다", other.get("ok") and wm.recovery(VAULT, S) is not None)
        stale = _w(wm.replace, VAULT, S, "이전 초안", r["hash"])
        check("낡은 해시 거부도 복구를 소비하지 않는다", not stale.get("ok") and wm.recovery(VAULT, S) is not None)
        done = _w(wm.replace, VAULT, S, edits=[{"old_text": "\n- 끝난 상태 " + "나" * 700, "new_text": ""}])
        check("실제 정리는 evict를 남기고 복구를 끝낸다", done.get("ok") and done.get("evicted") and wm.recovery(VAULT, S) is None, done)
        check("실제 정리는 changed true", done.get("changed") is True, done)
        check("실제로 덜어 낸 저장본만 evict", ev.records(VAULT)[-1]["text"] == "- 끝난 상태 " + "나" * 700)
        out = run_hook("claude_session_start.py")
        check("성공 뒤 복구는 사라지고 정돈이 나타난다", "[osk scope 복구 대기" not in out and done["evicted"] in out)
    finally:
        for suffix in ("count", "hash"):
            (Path(tempfile.gettempdir()) / "osk-cadence" / f"{sid}.{suffix}").unlink(missing_ok=True)
        wm._pending_clear(VAULT, S)
        rmtree_force(cwd.parent)


def test_scope_memory_cli():
    """훅이 이 출력을 문맥에 그대로 넣으므로 감싸는 껍데기가 있으면 훅마다
    벗기는 코드를 쓰게 된다. 결속이 없으면 빈 출력이고, 그것은 오류가 아니다."""
    from osk import cli
    from osk import scope_memory as wm
    import io, types
    (ROOT / "00_Scope/WWmCli").mkdir(exist_ok=True)
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

    _, plain = run(["sm", "show", "--session", S, "--space", "00_Scope/WWmCli"])
    check("결속 전 show는 빈 출력", plain == "", repr(plain))

    # 계약 §3.4가 말하는 훅의 첫 호출 — 결속도 space도 없는 새 저장소.
    # 여기서 위반 JSON이 나오면 훅이 그것을 문맥에 넣거나 종료코드를 보고
    # 조용히 건너뛴다. 둘 다 나쁘므로 빈 출력·종료코드 0이어야 한다.
    out0, plain0 = run(["sm", "show", "--session", "repo/never-bound"])
    check("결속 없는 show는 빈 출력", plain0 == "", repr(plain0))
    check("결속 없는 show는 종료코드 0", out0.get("exit") in (None, 0), out0)

    out, _ = run(["sm", "write", "--session", S, "--space", "00_Scope/WWmCli"],
                 "- 훅으로 쓴 엔트리")
    check("CLI 쓰기", out.get("ok") is True, out)
    # 주된 scope 지정은 사용자 전속이다 — 단말 확인 없이는 중단한다(헌법 10조 1~2항 · #191 리뷰 3차)
    out, _ = run(["routing", "primary", "--project", S, "--scope", "WWmCli"], stdin="")   # 비단말 stdin
    check("routing primary는 단말 확인 없이 중단한다",
          str(out.get("exit", "")).startswith("중단") and write.resolve_session(VAULT, S) is None, out)
    with mock.patch.object(cli, "_confirm", lambda prompt: None):
        out, _ = run(["routing", "primary", "--project", S, "--scope", "WWmCli"])
    check("CLI로 주된 scope를 지정한다",
          out.get("ok") is True and out.get("primary") == "WWmCli"
          and write.resolve_session(VAULT, S) == "WWmCli", out)

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
    # CLI가 거부 페이로드를 삼키지 않는지가 요점이다. 전문은 §9-2 5항에 따라
    # 상한 초과에는 실리지 않으므로, 실려야 하는 것들로 확인한다.
    check("CLI 거부도 거부 상세를 그대로 낸다",
          out.get("violations") and out.get("rejected_chars", 0) > wm.LIMIT
          and "hash" in out, out)
    check("CLI 상한 초과도 전문은 없다", "text" not in out, sorted(out))
