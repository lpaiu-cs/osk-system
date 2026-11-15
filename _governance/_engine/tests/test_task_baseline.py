"""과업으로 재는 기준선(`benchmarks/task_baseline.py`) — 실행 기록을 읽는 법과 채점.

실행 기록의 행과 저장소의 노드는 손으로 지었다. 에이전트를 띄우지 않는다.

    python tests/test_task_baseline.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "benchmarks"))
import task_baseline as tb  # noqa: E402


def use(call, tool, **args):
    block = {"type": "tool_use", "id": call, "name": f"mcp__osk-system__{tool}", "input": args}
    return {"type": "assistant", "message": {"content": [block]}}


def out(call, value, **more):
    text = value if isinstance(value, str) else json.dumps(value)        # 한글은 \\u로 온다 — 읽는 쪽이 푼다
    block = {"type": "tool_result", "tool_use_id": call, "content": [{"type": "text", "text": text}], **more}
    return {"type": "user", "message": {"content": [block]}}


REFUSED = {"ok": False, "violations": ["drafter는 하네스명이 아니라 모델명"]}
ROWS = [
    {"type": "system", "subtype": "init"},
    use("1", "search", query="커밋"), out("1", {"result": [{"title": tb.COMMIT}]}),
    use("2", "create_node", title="새"), out("2", REFUSED),
    use("3", "create_node", title="새"), out("3", REFUSED),             # 같은 거부를 또 만났다
    use("4", "read_node", name="없다"), out("4", {"error": "노드 없음: 없다"}),
    use("5", "update_node", name="x"), out("5", "입력이 스키마에 맞지 않는다", is_error=True),
    use("6", "create_node", title="새"), out("6", {"ok": True, "name": "새"}),      # 앞의 두 거부를 넘겼다
    {"type": "result", "subtype": "success", "result": "계정: 로그인 실패에 401을 낸다", "num_turns": 6,
     "duration_ms": 12340},
]
STREAM = "".join(json.dumps(row) + "\n" for row in ROWS) + "찢긴 줄\n"

DEPLOY, BOOKING, PAY = "00_Scope/harbor/배포", "00_Scope/harbor/예약", "00_Scope/harbor/결제"
BEFORE = {tb.STAGING: (DEPLOY, "승인 버튼을 눌러야 돈다"), tb.WATCH: (DEPLOY, "1%를 넘으면 되돌린다"),
          tb.JITTER: ("00_Domain/분산 시스템", "지터를 섞는다")}
QUIET = {"calls": [], "answer": "", "turns": 1, "seconds": 1, "finished": True}
# (과업, 끝난 저장소에서 달라진 노드, 기대하는 검사의 값)
CASES = [
    ("refine", {tb.WATCH: (DEPLOY, "5xx 비율이 0.5%를 넘으면 되돌린다")}, [True, True, True]),
    ("refine", {"되돌리는 기준": (DEPLOY, "0.5%")}, [False, False, False]),
    ("refine", {tb.WATCH: (DEPLOY, "0.5%를 넘으면 되돌린다. 1%를 넘으면 되돌린다")}, [True, True, False]),  # 덧붙이기만
    ("refine", {tb.WATCH: (DEPLOY, "0.5%를 넘으면 되돌린다. 1%에서 0.5%로 낮췄다")}, [True, True, True]),   # 옛 값은 이력
    ("update", {tb.STAGING: (DEPLOY, "CI의 Dry-Run이 통과하면 돈다")}, [True, True, True]),
    ("update", {tb.STAGING: (DEPLOY, "CI의 dry-run이 통과하면 돈다"), "새 절차": (DEPLOY, "dry-run")},
     [True, False, True]),
    ("update", {"새 절차": (DEPLOY, "dry-run")}, [False, False, False]),
    ("update", {tb.STAGING: (DEPLOY, "승인 버튼을 눌러야 돈다. CI의 dry-run도 돈다")}, [True, True, False]),
    ("update", {tb.STAGING: (DEPLOY, "dry-run이 통과하면 돈다. 예전에는 승인 버튼을 눌러야 돌았다")}, [True, True, True]),
    ("record", {"알림": (BOOKING, "24시간 전에 한 번\n\n# 관측\n- 1edc#1")}, [True, True, True]),
    ("record", {"알림": (PAY, "24시간 전에 한 번")}, [True, False, False]),
    ("reject", {"보안 점검": (tb.SECURITY, "# 보안 점검"), "분기 점검": (tb.SECURITY, "취약점을 본다")}, [True, True]),
    ("reject", {"보안 점검": (tb.SECURITY, "취약점을 다루는 군집")}, [True, False]),          # 허브는 그 노드가 아니다
    ("domain", {"웹훅 장애": (PAY, f"웹훅이 몰렸다. [[{tb.JITTER}]]")}, [True, True]),
    ("domain", {"웹훅 장애": (PAY, "웹훅이 몰렸다. 무작위를 섞었다")}, [True, False]),
    ("domain", {"웹훅 장애": (PAY, f"웹훅이 몰렸다. {tb.JITTER}는 규칙을 따랐다")}, [True, False]),   # 제목만 말했다
    ("domain", {tb.JITTER: ("00_Domain/분산 시스템", "지터를 섞는다. 결제 웹훅에서 겪었다")}, [False, False]),
]


def problems() -> list[str]:
    bad = []
    run = tb.record(STREAM)
    got = ([c["tool"] for c in run["calls"]], [c["rejected"] for c in run["calls"]],
           run["answer"], run["turns"], run["seconds"], run["finished"])
    want = (["search", "create_node", "create_node", "read_node", "update_node", "create_node"],
            [False, True, True, True, True, False], "계정: 로그인 실패에 401을 낸다", 6, 12, True)
    if got != want:
        bad.append(f"record: {got}")
    if tb.COMMIT not in run["calls"][0]["output"]:
        bad.append("record: 응답의 한글이 풀리지 않았다")

    applied = tb.score("recall-apply", {}, {}, run)
    got = (applied["completed"], list(applied["checks"].values()), applied["calls"], applied["rejections"],
           applied["rejections_passed"], applied["repeated_rejections"])
    kinds = ["create_node: drafter는 하네스명이 아니라 모델명"] * 2 + ["read_node: 노드 없음: 없다",
                                                              "update_node: 입력이 스키마에 맞지 않는다"]
    if got != (True, [True, True], 6, kinds, 2, 1):
        bad.append(f"score recall-apply: {got}")
    asked = tb.score("recall-ask", {}, {}, {**run, "calls": run["calls"][1:]})
    if (asked["completed"], list(asked["checks"].values())) != (False, [False, False]):
        bad.append(f"score recall-ask: {asked['checks']}")

    for task, moved, want in CASES:
        result = tb.score(task, BEFORE, {**BEFORE, **moved}, QUIET)
        if list(result["checks"].values()) != want or result["completed"] != all(want):
            bad.append(f"score {task} {sorted(moved)}: {result['checks']}")
    cut = tb.score("refine", BEFORE, {**BEFORE, **CASES[0][1]}, {**QUIET, "finished": False})
    if cut["completed"]:
        bad.append("score: 끝나지 않은 실행을 끝낸 것으로 셌다")

    missed = tb.score("recall-apply", {}, {}, {**run, "calls": run["calls"][1:], "answer": "fix: 401을 낸다"})
    if missed["completed"] or any(missed["checks"].values()):
        bad.append(f"score recall-apply, 닿지 않은 실행: {missed['checks']}")
    both = tb.summary([applied, missed])
    got = {k: both[k] for k in ("runs", "completed", "calls", "rejections", "rejections_passed",
                                "runs_with_a_rejection", "repeated_rejections")}
    want = {"runs": 2, "completed": 1, "calls": [5, 6], "rejections": 8, "rejections_passed": 4,
            "runs_with_a_rejection": 2, "repeated_rejections": 2}
    if got != want or both["rejection_kinds"] != {kinds[0]: 4, kinds[2]: 2, kinds[3]: 2}:
        bad.append(f"summary: {got}")
    if set(tb.CHECKS) != set(tb.FIXTURE["tasks"]):
        bad.append("과업과 채점의 이름이 다르다")
    # 바탕의 표시는 판·모델·과업 파일의 해시를 함께 적는다 — 어느 하나가 다르면 build가 그 폴더를 받지 않는다.
    digest = hashlib.sha256(Path(tb.__file__).with_name("task_fixture.json").read_bytes()).hexdigest()[:12]
    if tb.stamp("abc", "m1") != f"abc m1 {digest}":
        bad.append(f"stamp: {tb.stamp('abc', 'm1')}")
    return bad


def main() -> int:
    bad = problems()
    for line in bad:
        print("FAIL", line)
    print(f"과업으로 재는 기준선: 다른 것 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
