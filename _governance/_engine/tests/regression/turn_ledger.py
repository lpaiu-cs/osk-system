"""회귀 수트의 한 갈래: 턴 계기 — 전사 계측과 에피소드 판정.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, tempfile
from pathlib import Path

from regression.support import check, ENGINE, rmtree_force


def test_turn_ledger():
    """osk 턴 계기 — 트랜스크립트 계측의 함정 셋과 에피소드 판정 (#19 · #20).

    사료 두 편이 "전부 `osk_turn_ledger.py`가 잰다"고 적었으나 그 파일은 없었다
    (#42 6항). 이 시나리오는 합성 트랜스크립트로 계기를 세운다 — 실 코퍼스는
    사용자 대화라 수트에 넣지 않는다.

    무엇을 망가뜨리면 실패하는가:
      · `tool_use_id` 전역 중복 제거를 지우면 → 재개 사본이 호출로 세어진다
      · 텍스트 블록의 도구 이름을 호출로 세면 → deferred tools 목록이 호출이 된다
      · 실패를 `is_error`로 보면 → `"ok": false` 거부가 실패에서 빠진다
      · `message.id` 접기를 지우면 → 블록마다 쪼개진 다중 도구 메시지가 마지막
        블록 하나로 보인다
      · 블록을 메시지 안에서 한 번만 세지 않으면 → 같은 세션 사본이 n_tools를
        부풀린다
      · 에피소드가 성공에서 닫히지 않으면 → 둘이 하나로 합쳐진다
      · 경로를 세션마다 두지 않고 대화로 평탄화하면 → 형제 분기의 호출이 섞여
        A의 거부가 B의 증류로 판정된다 (PR #50 리뷰 4차)
      · 같은 거부의 에피소드를 가장 먼저 닫힌 것으로 고르지 않으면 → 늦게 닫힌
        분기의 결과가 앞선다
      · 발화 토큰을 호출마다 통째로 붙이면 → 다중 호출 메시지가 두 번 합산된다 (리뷰)
      · 창을 먼저 자르지 않으면 → `--until` 뒤의 성공이 과거 창의 에피소드를 닫고
        대장 나이가 음수가 된다 (리뷰)
      · 세션을 탄생 시각으로 자르지 않으면 → 창 뒤에 태어난 재개 세션의 id가
        과거 창의 세션 수에 든다 (리뷰 4차)
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "osk_turn_ledger", ENGINE / "scripts" / "osk_turn_ledger.py")
    tl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tl)

    lab = Path(tempfile.mkdtemp(prefix="osk-ledger-"))
    try:
        proj = lab / "projects" / "E--fixture"
        proj.mkdir(parents=True)
        P = "mcp__osk-system__"
        state = {"prompt": 1000, "t": 0, "last_out": 0, "at": None}

        def _ts():
            if state["at"]:
                return state["at"]
            state["t"] += 1
            return f"2026-09-01T10:{state['t'] // 60:02d}:{state['t'] % 60:02d}.000Z"

        def _assistant(recs, sess, mid, blocks, out=50, prompt=None):
            """한 과금 메시지 — 블록마다 레코드로 쪼개고 usage는 공유한다(함정 2)."""
            if prompt is not None:
                state["prompt"] = prompt
            usage = {"input_tokens": state["prompt"], "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0, "output_tokens": out}
            ts = _ts()
            for b in blocks:
                recs.append({"type": "assistant", "sessionId": sess, "timestamp": ts,
                             "message": {"id": mid, "role": "assistant", "usage": usage, "content": [b]}})
            state["last_out"] = out

        def _result(recs, sess, tid, text, is_error=False, bump=True):
            recs.append({"type": "user", "sessionId": sess, "timestamp": _ts(),
                         "message": {"role": "user", "content": [
                             {"type": "tool_result", "tool_use_id": tid,
                              "content": [{"type": "text", "text": text}], "is_error": is_error}]}})
            if bump:      # 다음 프롬프트 = 이번 프롬프트 + 발화 + 결과/1.5 (게이트 안)
                state["prompt"] += state["last_out"] + round(len(text) / 1.5)

        def _use(tid, name):
            args = ({"session": "repo", "text": "draft", "expect_hash": "sha256:" + "0" * 64}
                    if name == "scope_memory" else {})
            return {"type": "tool_use", "id": tid, "name": P + name, "input": args}

        def _dump(name, recs):
            (proj / name).write_text(chr(10).join(json.dumps(r, ensure_ascii=False) for r in recs) + chr(10),
                                     encoding="utf-8")

        rej1 = '{"ok": false, "scope": "W", "violations": ["1527자로 상한 1500자를 27자 넘는다. **순서대로** 하라 — ' + "x" * 400 + '"]}'
        ok1 = '{"ok": true, "scope": "W", "chars": 1480, "remaining": 20, "hash": "sha256:' + "a" * 64 + '", "text": "' + "m" * 1400 + '"}'
        rej2 = '{"ok": false, "scope": "W", "violations": ["1600자로 상한 1500자를 100자 넘는다. ' + "y" * 300 + '"]}'
        okn = '{"ok": true, "name": "n1", "path": "00_Scope/W/n1.md", "id": "260901-0000-n1x1", "new_hash": "sha256:' + "b" * 64 + '", "dangling": []}'
        ok2 = '{"ok": true, "scope": "W", "chars": 1450, "remaining": 50, "hash": "sha256:' + "c" * 64 + '", "text": "' + "n" * 1300 + '"}'
        rej3 = '{"ok": false, "scope": "W", "violations": ["1530자로 상한 1500자를 30자 넘는다. ' + "z" * 300 + '"]}'
        ok3 = '{"ok": true, "scope": "W", "chars": 1490, "remaining": 10, "hash": "sha256:' + "d" * 64 + '", "text": "' + "p" * 1300 + '"}'
        rej4 = '{"ok": false, "scope": "W", "violations": ["1540자로 상한 1500자를 40자 넘는다. ' + "w" * 300 + '"]}'
        ok4a = '{"ok": true, "scope": "W", "chars": 1500, "remaining": 0, "hash": "sha256:' + "e" * 64 + '", "text": "' + "q" * 1300 + '"}'
        ok4b = '{"ok": true, "scope": "W", "chars": 1495, "remaining": 5, "hash": "sha256:' + "f" * 64 + '", "text": "' + "r" * 1300 + '"}'
        okov = '{"ok": true, "clusters": []}'

        # ── 대화 1 (S1) ─────────────────────────────────────────────────
        A: list[dict] = []
        _assistant(A, "S1", "M1", [{"type": "thinking", "thinking": "…"}, _use("T1", "scope_memory")])
        _result(A, "S1", "T1", rej1)
        _assistant(A, "S1", "M2", [_use("T2", "scope_memory")])
        _result(A, "S1", "T2", ok1)
        # 함정 1 — deferred tools 목록(텍스트 블록)이 도구 이름을 담는다
        _assistant(A, "S1", "M3", [{"type": "text", "text": "도구 목록: " + P + "scope_memory, " + P + "search"}], out=20)
        state["prompt"] += 20
        _assistant(A, "S1", "M4", [_use("T3", "scope_memory")])
        _result(A, "S1", "T3", rej2)
        _assistant(A, "S1", "M5", [_use("T4", "create_node")])
        _result(A, "S1", "T4", okn)
        _assistant(A, "S1", "M6", [_use("T5", "scope_memory")])
        _result(A, "S1", "T5", ok2)
        # 한 메시지에 osk 도구 둘 — 블록마다 레코드(코퍼스 그대로). 발화 50은 한 번만.
        _assistant(A, "S1", "M7", [_use("T6", "read_node"), _use("T7", "search")])
        _result(A, "S1", "T6", '{"ok": true, "title": "n1"}', bump=False)
        _result(A, "S1", "T7", '{"ok": true, "results": []}')
        _assistant(A, "S1", "M8", [_use("T8", "update_node")])
        _result(A, "S1", "T8", "MCP error: timeout", is_error=True)
        #    혼합 메시지 — osk 하나와 비-osk 하나가 한 메시지에 (실측 50/728).
        #    메시지 전체의 발화를 osk가 다 가져가면 분모·분자가 부푼다(리뷰 6차).
        _assistant(A, "S1", "M19", [_use("T19", "overview"),
                                    {"type": "tool_use", "id": "TB", "name": "Bash", "input": {}}])
        _result(A, "S1", "T19", okov, bump=False)
        _result(A, "S1", "TB", "ok")
        _dump("a.jsonl", A)
        # 함정 3 — 재개 사본, **다른 sessionId** 아래(코퍼스: 537 중 411) + 새 호출 하나
        B = [dict(r, sessionId="S1-resume") for r in A[:4]]
        _assistant(B, "S1-resume", "M9", [_use("T9", "overview")], out=30)
        _result(B, "S1-resume", "T9", okov)
        _dump("b.jsonl", B)
        # 사본의 둘째 모양 — **같은 sessionId** 아래(537 중 126)
        _dump("c.jsonl", A[:4])

        # ── 대화 2 (S2) — 원 세션이 상한 초과 직후 끝나고 재개 세션이 닫는다 ──
        def _s2(orig_name, resume_name):
            O: list[dict] = []
            _assistant(O, "S2", "M10", [_use("T10", "scope_memory")])
            _result(O, "S2", "T10", rej3)
            R = [dict(r, sessionId="S2-resume") for r in O]
            _assistant(R, "S2-resume", "M11", [_use("T11", "scope_memory")])
            _result(R, "S2-resume", "T11", ok3)
            _dump(orig_name, O)
            _dump(resume_name, R)

        _s2("z-orig.jsonl", "0-resume.jsonl")        # 재개 파일이 **먼저** 정렬된다

        # ── 대화 3 (S3) — 형제 분기: 같은 거부에서 A는 잘라 통과, B는 증류 뒤 통과 ──
        #    시각순으로 섞으면 [거부, B의 create_node, A의 성공]이 되어 A가 증류로
        #    판정된다(리뷰 4차). 경로마다 따로 보면 A가 먼저 닫고, 그 하나만 센다.
        O3: list[dict] = []
        _assistant(O3, "S3", "M12", [_use("T12", "scope_memory")])
        p_fork = state["prompt"]                       # 분기 지점의 프롬프트
        _result(O3, "S3", "T12", rej4)
        d_a = round(len(rej4) / 1.5)                   # A가 실은 rej4의 증분
        d_b = d_a + 10                                 # B는 훅 주입 10만큼 문맥이 다르다
        FB = [dict(r, sessionId="S3-B") for r in O3]
        _assistant(FB, "S3-B", "M14", [_use("T14", "create_node")],          # t2
                   prompt=p_fork + 50 + d_b)
        _result(FB, "S3-B", "T14", okn)
        FA = [dict(r, sessionId="S3-A") for r in O3]
        _assistant(FA, "S3-A", "M13", [_use("T13", "scope_memory")],         # t3 — 먼저 닫힌다
                   prompt=p_fork + 50 + d_a)
        _result(FA, "S3-A", "T13", ok4a)
        _assistant(FB, "S3-B", "M15", [_use("T15", "scope_memory")])         # t4
        _result(FB, "S3-B", "T15", ok4b)
        _dump("s3-o.jsonl", O3); _dump("s3-b.jsonl", FB); _dump("s3-a.jsonl", FA)

        # ── 대화 4 (S4) — 9월 1일 세션을 9월 2일에 재개: 재개 세션은 창 뒤에 태어났다 ──
        O4: list[dict] = []
        _assistant(O4, "S4", "M16", [_use("T16", "overview")])
        _result(O4, "S4", "T16", okov)
        R4 = [dict(r, sessionId="S4-resume") for r in O4]
        state["at"] = "2026-09-02T09:00:00.000Z"
        _assistant(R4, "S4-resume", "M17", [_use("T17", "overview")])
        state["at"] = "2026-09-02T09:00:05.000Z"
        _result(R4, "S4-resume", "T17", okov)
        state["at"] = None
        _dump("s4-o.jsonl", O4); _dump("s4-r.jsonl", R4)

        raw_ids = sum(json.dumps(r).count('"tool_use"') for r in A + B + A[:4])
        check("전제: 파일들에 tool_use 블록이 사본까지 더 많다", raw_ids > 9, raw_ids)

        full = tl.read_corpus(lab / "projects", [])
        end = tl._ts("2026-09-02T00:00:00Z")
        corpus = tl.window(full, end)
        tl.attribute_costs(corpus)
        uses, res, msgs = corpus["uses"], corpus["results"], corpus["msgs"]

        check("재개 사본은 한 호출로 센다 (#20 함정 3)", len(uses) == 17, sorted(uses))
        check("텍스트 블록의 도구 이름은 호출이 아니다 (#20 함정 1)",
              len([u for u in uses.values() if u["name"] == "scope_memory"]) == 9,
              [u["name"] for u in uses.values()])
        check("쪼개진 레코드는 한 과금 메시지다 — 사본이 어디에 있든 (#20 함정 2)",
              len(msgs["M1"]["blocks"]) == 1 and msgs["M1"]["tool_uses"] == ["T1"]
              and msgs["M1"]["sessions"] == {"S1", "S1-resume"}, msgs["M1"])
        check("블록마다 쪼개진 다중 도구 메시지도 한 메시지로 접힌다 (#20 함정 2)",
              len(msgs["M7"]["blocks"]) == 2 and set(msgs["M7"]["tool_uses"]) == {"T6", "T7"}, msgs["M7"])
        check("실패는 is_error가 아니라 ok로 본다 (#20)",
              res["T1"]["ok"] is False and res["T1"]["is_error"] is False
              and res["T8"]["ok"] is None and res["T8"]["is_error"] is True, (res["T1"], res["T8"]))
        check("상한 초과 거부에서 초과폭을 읽는다",
              res["T1"]["overflow"] == (1527, 1500, 27) and res["T3"]["overflow"] == (1600, 1500, 100),
              (res["T1"]["overflow"], res["T3"]["overflow"]))
        check("도구 하나인 메시지에만 페이로드를 귀속하고 게이트를 지난다",
              uses["T1"]["gated"] and uses["T1"]["alone"]
              and not uses["T6"]["alone"] and uses["T6"]["payload_tokens"] is None,
              (uses["T1"].get("ratio"), uses["T6"]))
        check("다중 호출 메시지의 발화는 나눠 붙어 합이 한 번이다 (리뷰)",
              uses["T6"]["out_share"] == 25 and uses["T7"]["out_share"] == 25
              and uses["T1"]["out_share"] == 50, (uses["T6"]["out_share"], uses["T7"]["out_share"]))
        check("재개·분기 세션은 원 세션과 한 대화다 (리뷰)",
              uses["T1"]["conversation"] == uses["T9"]["conversation"]
              and uses["T10"]["conversation"] == uses["T11"]["conversation"]
              and uses["T12"]["conversation"] == uses["T13"]["conversation"] == uses["T15"]["conversation"],
              (uses["T9"]["conversation"], uses["T11"]["conversation"], uses["T15"]["conversation"]))
        check("창 뒤 재개의 새 호출은 시각으로 걸러진다 — 사본이 든 메시지는 남는다 (리뷰 5차)",
              "T17" not in uses and "T16" in uses
              and msgs["M16"]["sessions"] == {"S4", "S4-resume"},
              (msgs["M16"]["sessions"], "T16" in uses, "T17" in uses))
        check("분기점의 페이로드는 후속마다 세어 합한다 (리뷰 5차)",
              uses["T12"]["successors"] == 2
              and uses["T12"]["payload_tokens"] == d_a + d_b
              and len(uses["T12"]["ratios"]) == 2
              and uses["T1"]["successors"] == 1,
              (uses["T12"].get("successors"), uses["T12"].get("payload_tokens"), d_a + d_b))

        eps = tl.episodes(corpus)
        by = {e["start_tid"]: e for e in eps}
        check("에피소드는 거부마다 하나 — 넷", len(eps) == 4 and set(by) == {"T1", "T3", "T10", "T12"}, sorted(by))
        check("첫 에피소드는 잘라서 통과 — 잘린 분량 1527−1480=47자",
              by["T1"]["outcome"] == "trim" and by["T1"]["trimmed"] == 47 and by["T1"]["rejections"] == 1, by["T1"])
        check("둘째 에피소드는 증류로 이어졌다", by["T3"]["outcome"] == "distill" and by["T3"]["distill"] == 1, by["T3"])
        check("원 세션 끝의 거부를 재개 세션의 성공이 닫는다 (리뷰)",
              by["T10"]["outcome"] == "trim" and by["T10"]["trimmed"] == 1530 - 1490, by["T10"])
        check("형제 분기는 섞이지 않고, 먼저 닫힌 경로의 결과 하나만 센다 (리뷰 4차)",
              by["T12"]["outcome"] == "trim" and by["T12"]["trimmed"] == 1540 - 1500
              and by["T12"]["distill"] == 0, by["T12"])

        #    파일명 순서를 뒤집어도 같다 — 재개 파일이 나중에 정렬되게
        (proj / "z-orig.jsonl").unlink(); (proj / "0-resume.jsonl").unlink()
        _s2("0-orig.jsonl", "z-resume.jsonl")
        full2 = tl.read_corpus(lab / "projects", [])
        s2 = tl.summarize(full2, 0.0, tl._ts("2026-09-03T00:00:00Z"), None)
        check("파일명 순서에 결과가 매이지 않는다 (리뷰)",
              s2["scope_memory"]["episodes"] == 4 and s2["scope_memory"]["episodes_unresolved"] == 0
              and s2["scope_memory"]["trimmed_chars_total"] == 47 + 40 + 40, s2["scope_memory"])
        check("창을 넓히면 재개의 새 호출이 들어온다",
              s2["calls"] == 18 and s2["conversations"] == 4, (s2["calls"], s2["conversations"]))

        # 집계 — 창 끝 = 9월 2일 0시
        #    대장 행의 시각은 rid(UUIDv7)에서 온다 — `at`은 settle의 계약 필드가
        #    아니라(§9-2 12항: of·outcome·target), `at`으로 자르면 계약에 맞는
        #    미래 settle이 과거 창의 처분 상태를 바꾼다(리뷰 6차).
        def _rid(iso, n):
            h = f"{int(tl._ts(iso) * 1000):012x}"
            return f"{h[:8]}-{h[8:12]}-7000-8000-{n:012d}"

        e1 = _rid("2026-08-10T00:00:00Z", 1)
        e2 = _rid("2026-08-30T00:00:00Z", 2)
        e3 = _rid("2026-09-01T12:00:00Z", 3)
        led = lab / "evictions.jsonl"
        led.write_text(chr(10).join(json.dumps(r, ensure_ascii=False) for r in [
            {"kind": "evict", "rid": e1, "scope": "W", "at": "2026-08-10T00:00:00Z", "text": "x"},
            {"kind": "evict", "rid": e2, "scope": "W", "at": "2026-08-30T00:00:00Z", "text": "y"},
            {"kind": "evict", "rid": e3, "scope": "W", "at": "2026-09-01T12:00:00Z", "text": "z"},
            #    계약 그대로 — `at`이 없다. 창 안(8/31)이라 세어야 한다.
            {"kind": "settle", "rid": _rid("2026-08-31T00:00:00Z", 4),
             "of": e2, "outcome": "node", "target": "n1"},
            #    창 뒤(9/3)의 처분 — `at`이 없어도 과거 창에 들어오면 안 된다.
            {"kind": "settle", "rid": _rid("2026-09-03T00:00:00Z", 5),
             "of": e1, "outcome": "discarded"},
        ]) + chr(10), encoding="utf-8")
        s = tl.summarize(full2, 0.0, end, led)
        m, ev = s["scope_memory"], s["evictions"]
        check("집계: 호출 17 · 실패 5 · 거부 4 · 대화 4",
              s["calls"] == 17 and s["failures"] == 5 and m["overflow_rejections"] == 4
              and s["conversations"] == 4,
              (s["calls"], s["failures"], m["overflow_rejections"], s["conversations"]))

        #    **과거 창은 뒤에 오는 기록에 흔들리지 않는다** (리뷰 5차의 요지).
        #    S1 전체를 복사한 후손 재개를 새로 더한다 — 구판은 이 순간 S1-resume에
        #    고유 메시지가 사라져 탄생 추정이 뒤로 밀리고, 창에서 세션이 빠지면서
        #    그 세션에만 있던 메시지의 호출까지 사라졌다. 지금은 자르는 기준이
        #    메시지 시각 하나라 같은 창이 같은 답을 낸다.
        #    재개는 **한 세션**의 선형 이력을 복사한다 — S1 전체를 복사하고 새 호출
        #    하나를 잇는다(두 세션을 섞으면 없던 인접이 생겨 픽스처가 거짓이 된다).
        D = [dict(r, sessionId="S1-resume3") for r in A]
        state["at"] = "2026-09-02T12:00:00.000Z"
        _assistant(D, "S1-resume3", "M18", [_use("T18", "search")])
        _result(D, "S1-resume3", "T18", '{"ok": true, "results": []}')
        state["at"] = None
        _dump("s1-r3.jsonl", D)
        again = tl.summarize(tl.read_corpus(lab / "projects", []), 0.0, end, led)
        #    `files`는 지금 훑은 코퍼스의 크기이지 창의 값이 아니다 — 빼고 본다.
        diff = [k for k in s if k != "files" and again.get(k) != s.get(k)]
        check("뒤에 온 후손 재개가 과거 창의 보고를 바꾸지 않는다 (리뷰 5차)",
              not diff, diff)
        check("집계: 도구별 실패 수도 ok로 세고, 같은 도구 재호출만 재시도로 센다",
              s["tools"]["scope_memory"]["fail"] == 4 and s["tools"]["update_node"]["fail"] == 1
              and s["retry_after_fail"] == 3,
              ({k: v["fail"] for k, v in s["tools"].items()}, s["retry_after_fail"]))
        wm = tl.window(full2, end)
        tl.attribute_costs(wm)
        #    발화는 **osk만 있는** 메시지에서만, 메시지당 한 번.
        out_once = sum(int(e_["usage"].get("output_tokens", 0)) for e_ in wm["msgs"].values()
                       if e_["tool_uses"] and len(e_["blocks"]) == len(e_["tool_uses"]))
        pay = sum((u.get("payload_tokens") or 0) for u in wm["uses"].values() if u.get("gated"))
        check("집계: 토큰 합의 발화 몫은 메시지당 한 번이다 (리뷰)",
              s["tokens_total"] == round(out_once + pay)
              and uses["T6"]["out_share"] + uses["T7"]["out_share"] == 50,
              (s["tokens_total"], out_once, pay))
        check("혼합 메시지의 호출은 발화를 못 받고, 그 수를 낸다 (리뷰 6차)",
              uses["T19"]["out_share"] is None and uses["T19"]["payload_tokens"] is None
              and s["speech_unattributed_calls"] == 1
              and uses["T1"]["out_share"] == 50,
              (uses["T19"].get("out_share"), s["speech_unattributed_calls"]))
        check("집계: 잘린 분량은 잘라서 통과한 에피소드만 센다",
              m["trimmed_chars_total"] == 47 + 40 + 40 and m["episodes_trim"] == 3 and m["episodes_distill"] == 1, m)
        check("퇴출 기록부(창 끝 기준): 3 중 1 처분, 미처분 2, 가장 오래된 23일",
              ev and ev["evict"] == 3 and ev["settled"] == 1 and ev["open"] == 2
              and round(ev["oldest_days"]) == 23, ev)
        #    `at`이 없어도 rid로 시각을 알므로 창 뒤의 처분은 들어오지 않는다.
        #    창을 그 뒤로 넓히면 세어진다 — 검사가 헛돌지 않는다는 증거다.
        ev_late = tl.summarize(full2, 0.0, tl._ts("2026-09-04T00:00:00Z"), led)["evictions"]
        check("`at` 없는 미래 settle이 과거 창에 들어오지 않는다 (리뷰 6차)",
              ev["settled"] == 1 and ev_late["settled"] == 2 and ev_late["open"] == 1,
              (ev["settled"], ev_late["settled"], ev_late["open"]))

        # 창을 먼저 자른다 (리뷰) — T3 거부 직후에 창이 끝나면 둘째 에피소드는 미해결
        cut = msgs["M4"]["ts"] + 0.5
        sw = tl.summarize(full2, 0.0, cut, led)
        mw, evw = sw["scope_memory"], sw["evictions"]
        check("창 뒤의 성공이 창 안의 거부를 닫지 않는다 (리뷰)",
              mw["episodes"] == 2 and mw["episodes_unresolved"] == 1 and mw["episodes_distill"] == 0
              and sw["calls"] == 3, (mw, sw["calls"]))
        check("창 뒤의 evict는 없는 것이고 나이는 음수가 아니다 (리뷰)",
              evw["evict"] == 2 and evw["open"] == 1 and evw["oldest_days"] > 0
              and evw["settled"] == 1, evw)
        out = tl.render(s)
        check("출력은 집계뿐 — 본문을 싣지 않는다",
              all(x not in out for x in ("mmmm", "nnnn", "xxxx", "pppp", "qqqq", "rrrr")), out[:200])

        # 실제 open-hwp → rhwp 오종결의 최소 재현. 하네스 경로가 같아도
        # OSK session과 scope가 모두 같고 저장본을 바꾼 쓰기만 닫아야 한다.
        boundary = []
        state["at"] = None

        def call(tid, session, scope="W", *, ok=True, read=False, **result):
            b = _use(tid, "scope_memory")
            b["input"] = {"session": session}
            if not read:
                b["input"]["edits"] = [{"old_text": "kept", "new_text": "updated"}]
            _assistant(boundary, "boundary", "M-" + tid, [b])
            data = {"ok": ok, "scope": scope, "chars": 1400, "hash": "sha256:" + "a" * 64, **result}
            if not ok:
                data["violations"] = ["1513자로 상한 1500자를 13자 넘는다."]
            _result(boundary, "boundary", tid, json.dumps(data, ensure_ascii=False))
            return b

        call("A-reject", "open-hwp", ok=False, recovery={"session": "open-hwp", "scope": "W"})
        call("B-reject", "rhwp", ok=False)
        call("B-done", "rhwp", changed=True)
        call("A-read", "open-hwp", read=True)
        call("A-noop", "open-hwp", changed=False)
        call("A-other-scope", "open-hwp", "Other", changed=True)
        call("A-unknown", "open-hwp")   # 구판 edits 응답: 변경 증거가 없다
        legacy_noop = call("A-legacy-noop", "open-hwp")
        legacy_noop["input"] = {"session": "open-hwp", "text": "same", "expect_hash": "sha256:" + "a" * 64}
        call("A-pending", "open-hwp", changed=True, recovery={"session": "open-hwp", "scope": "W"})
        _dump("boundary.jsonl", boundary)
        partial = tl.read_corpus(lab / "projects", [])
        eps = {e["start_tid"]: e for e in tl.episodes(partial)}
        check("다른 키의 성공·읽기·no-op·다른 scope·미확인 쓰기는 원 거부를 닫지 않는다",
              eps["A-reject"]["outcome"] == "unresolved", eps["A-reject"])
        check("두 OSK 키의 거부는 별개 에피소드다",
              eps.get("B-reject", {}).get("end_tid") == "B-done", eps)
        check("변경 증거가 없는 성공은 미확인으로 센다",
              eps["A-reject"].get("unverified_writes") == 1, eps["A-reject"])

        call("A-done", "open-hwp", evicted="fixture-evict")
        call("alias-reject", "old-name", ok=False, canonical_session="canonical")
        call("alias-done", "canonical", changed=True, canonical_session="canonical")
        call("cas-reject", "cas", ok=False)
        cas = call("cas-done", "cas")
        cas["input"] = {"session": "cas", "text": "new", "expect_hash": "sha256:" + "0" * 64}
        call("missing-reject", None, scope=None, ok=False)
        call("missing-done", None, scope=None, changed=True)
        _dump("boundary.jsonl", boundary)
        full = tl.read_corpus(lab / "projects", [])
        eps = {e["start_tid"]: e for e in tl.episodes(full)}
        check("같은 키·scope의 실제 evict 쓰기가 원 거부를 닫는다",
              eps["A-reject"].get("end_tid") == "A-done", eps["A-reject"])
        check("응답이 증명한 정본 별칭은 같은 키다",
              eps["alias-reject"].get("end_tid") == "alias-done", eps["alias-reject"])
        check("구판 전문 쓰기는 CAS 전후 해시로 변경을 입증한다",
              eps["cas-reject"].get("end_tid") == "cas-done", eps["cas-reject"])
        check("키·scope 누락을 같은 키로 추측하지 않는다",
              eps["missing-reject"]["outcome"] == "unresolved", eps["missing-reject"])
        summary = tl.summarize(full, 0, max(m["ts"] for m in full["msgs"].values()) + 1, None)
        check("노드 쓰기 동반을 자율 증류 성공률로 발표하지 않는다",
              summary["scope_memory"].get("autonomy") == "not_measured"
              and "자율" in tl.render(summary))
        # 구판 거부 뒤에 처음 나온 정본 키도 진행 중 에피소드에 반영한다.
        boundary = []
        call("late-success-reject", "late-success-old", ok=False)
        call("late-success-done", "late-success-old", changed=True,
             canonical_session="late-success-canonical")
        call("late-refusal-first", "late-refusal-old", ok=False)
        call("late-refusal-next", "late-refusal-old", ok=False,
             canonical_session="late-refusal-canonical")
        call("late-refusal-done", "late-refusal-canonical", changed=True)
        call("renamed-reject", "renamed-old", ok=False, canonical_session="renamed-middle")
        call("renamed-done", "renamed-old", changed=True, canonical_session="renamed-final")

        # 두 이름으로 열렸던 같은 recovery를 합친다. 공유 검색·노드 쓰기는
        # 중복 계수하지 않고, 서로 다른 이름으로 한 거부·미확인 쓰기는 보존한다.
        call("merge-first", "merge-old", ok=False)
        call("merge-old-unknown", "merge-old")
        for tid, name in (("merge-search-before", "search"), ("merge-node-before", "create_node")):
            _assistant(boundary, "boundary", "M-" + tid, [_use(tid, name)])
            _result(boundary, "boundary", tid, '{"ok": true}')
        call("merge-next", "merge-canonical", ok=False)
        call("merge-canonical-unknown", "merge-canonical")
        for tid, name in (("merge-search-shared", "search"), ("merge-node-shared", "update_node")):
            _assistant(boundary, "boundary", "M-" + tid, [_use(tid, name)])
            _result(boundary, "boundary", tid, '{"ok": true}')
        call("merge-other-scope", "merge-old", "Other", ok=False)
        merge_parent = [dict(r, sessionId="late-merge-parent") for r in boundary]
        call("merge-link", "merge-old", "Other", read=True, canonical_session="merge-canonical")
        call("merge-done", "merge-old", changed=True)
        _dump("boundary.jsonl", boundary)
        late = tl.read_corpus(lab / "projects", [])
        tl.attribute_costs(late)
        eps = {e["start_tid"]: e for e in tl.episodes(late)}
        check("성공에서 늦게 배운 정본 키가 구판 거부를 닫는다",
              eps["late-success-reject"].get("end_tid") == "late-success-done"
              and eps["late-success-reject"]["session"] == "late-success-canonical",
              eps["late-success-reject"])
        check("거부에서 늦게 배운 정본 키가 같은 recovery를 둘로 쪼개지 않는다",
              eps["late-refusal-first"].get("end_tid") == "late-refusal-done"
              and eps["late-refusal-first"]["rejections"] == 2
              and "late-refusal-next" not in eps, eps["late-refusal-first"])
        check("같은 요청 키가 새 정본 이름을 알리면 이미 접힌 pending도 따라간다",
              eps["renamed-reject"].get("end_tid") == "renamed-done"
              and eps["renamed-reject"]["session"] == "renamed-final", eps["renamed-reject"])
        merged = eps["merge-first"]
        check("기존 별칭·정본 pending을 최초 거부 하나로 합치고 개별 쓰기를 보존한다",
              merged.get("end_tid") == "merge-done" and merged["session"] == "merge-canonical"
              and merged["rejections"] == 2 and merged["unverified_writes"] == 2
              and merged["tokens"] == sum(tl._cost(late["uses"][t]) for t in ("merge-first", "merge-next"))
              and "merge-next" not in eps, merged)
        check("병합 전후 공유 검색·노드 쓰기는 한 번씩만 센다",
              merged["search"] == 2 and merged["distill"] == 2, merged)
        check("정본 키를 배운 scope 밖 pending도 옮기되 서로 다른 scope는 합치지 않는다",
              eps["merge-other-scope"]["outcome"] == "unresolved"
              and eps["merge-other-scope"]["session"] == "merge-canonical"
              and eps["merge-other-scope"]["scope"] == "Other", eps["merge-other-scope"])
        _dump("late-merge-parent.jsonl", merge_parent)
        resumed = {e["start_tid"]: e for e in tl.episodes(tl.read_corpus(lab / "projects", []))}
        check("복사된 접두부의 둘째 pending이 후손의 병합 뒤에 별개 에피소드로 남지 않는다",
              "merge-next" not in resumed and resumed["merge-first"].get("end_tid") == "merge-done",
              {k: v for k, v in resumed.items() if k.startswith("merge-")})
        cut = late["msgs"]["M-late-success-done"]["ts"] - 0.5
        earlier = {e["start_tid"]: e for e in tl.episodes(tl.window(late, cut))}
        check("창 뒤의 정본 키·성공이 과거 창의 구판 거부를 닫지 않는다",
              earlier["late-success-reject"]["outcome"] == "unresolved"
              and earlier["late-success-reject"]["session"] == "late-success-old",
              earlier["late-success-reject"])

        # 형제 경로 A의 별칭 증거로 B의 정본 키 성공을 연결하면 안 된다.
        boundary = []
        call("late-fork-reject", "late-fork-old", ok=False)
        prefix = list(boundary)
        call("late-fork-alias", "late-fork-old", read=True, canonical_session="late-fork-canonical")
        _dump("late-fork-a.jsonl", [dict(r, sessionId="late-fork-a") for r in boundary])
        boundary = prefix
        call("late-fork-done", "late-fork-canonical", changed=True)
        _dump("late-fork-b.jsonl", [dict(r, sessionId="late-fork-b") for r in boundary])
        forks = tl.read_corpus(lab / "projects", [])
        eps = {e["start_tid"]: e for e in tl.episodes(forks)}
        check("늦게 배운 별칭은 같은 접두부를 공유하는 형제 경로로 새지 않는다",
              eps["late-fork-reject"]["outcome"] == "unresolved", eps["late-fork-reject"])

    finally:
        rmtree_force(lab)
