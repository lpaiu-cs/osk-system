"""선제 회상의 섀도 측정(`benchmarks/recall_shadow.py`) — 전사에서 턴을 뽑는 법과 수를 내는 법.

전사의 행은 손으로 지었다. 검색기는 대역이다 — 질의마다 정해 둔 차례를 돌려준다.

    python tests/test_recall_shadow.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "benchmarks"))
import recall_shadow as rs  # noqa: E402


def user(text, **more):
    return {"type": "user", "message": {"role": "user", "content": text}, **more}


def result():
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}


def calls(*pairs, **more):
    blocks = [{"type": "tool_use", "name": f"mcp__osk-system__{tool}", "input": args} for tool, args in pairs]
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}, **more}


ROWS = [
    user("첫 질문 <system-reminder>알림</system-reminder>"),
    calls(("search", {"query": "agent query"}), ("read_node", {"name": "A"})),
    result(),                                                    # 도구 결과는 턴을 열지 않는다
    calls(("update_node", {"name": "[[B]]"}), ("create_node", {"title": "New"}), ("read_node", {"name": "New"})),
    user("<task-notification>끝</task-notification>"),            # 하네스의 알림뿐인 입력도 열지 않는다
    calls(("read_node", {"name": "C"})),
    calls(("read_node", {"name": "Side"}), isSidechain=True),    # 곁가지의 행은 세지 않는다
    user("둘째 질문"),
    calls(("search", {"query": "other"})),                       # 노드를 쓰지 않은 턴은 내지 않는다
    user("셋째 질문", isMeta=True),
    user("넷째 질문"),
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}},
        {"type": "tool_use", "name": "mcp__osk-system__read_node", "input": {"name": "A"}}]}},
]


class Stub:
    """질의마다 정해 둔 차례를 돌려주는 검색기. 질의마다 넘어온 결속을 `seen`에 적는다."""

    def __init__(self, ranking: dict):
        self.ranking, self.seen = ranking, {}
        self.paths = [(name, None, SimpleNamespace(id=f"id-{name}")) for name in "ABCDE"]

    def view_search(self, query, k=8, local=None):
        self.seen[query] = local
        ranking = self.ranking.get((query, "bound") if local else query) or self.ranking.get(query, "")
        return [{"id": f"id-{name}"} for name in ranking][:k]


def problems() -> list[str]:
    bad = []
    found = list(rs.turns(ROWS))
    if [t["query"] for t in found] != ["첫 질문", "넷째 질문"]:
        bad.append(f"turns: queries {[t['query'] for t in found]}")
    first = found[0] if found else {}
    if (first.get("read"), first.get("updated"), first.get("searches")) \
            != (["A", "C"], ["[[B]]"], [("agent query", False)]):
        bad.append(f"turns: first turn {first.get('read')} {first.get('updated')} {first.get('searches')}")

    # 첫 턴: A는 둘째, B는 없음, C는 첫째. 넷째 턴: A가 넷째. 에이전트의 검색은 A만 찾는다.
    stub = Stub({"첫 질문": "CAD", "넷째 질문": "DECA", "agent query": "A"})
    report = rs.measure(stub, found, 2000)
    want = {"@1": 0.25, "@3": 0.5, "@5": 0.75, "@8": 0.75, "@20": 0.75}
    if report["recall_by_prompt"] != want:
        bad.append(f"measure: recall {report['recall_by_prompt']}")
    if report["used_nodes"] != {"pairs": 4, "read": 3, "updated": 1, "names_not_searchable_now": 0}:
        bad.append(f"measure: used {report['used_nodes']}")
    if report["turns_with_any_used_node"]["@1"] != 0.5 or report["turns_with_any_used_node"]["@5"] != 1.0:
        bad.append(f"measure: turns {report['turns_with_any_used_node']}")
    if report["mrr"] != round((1 / 2 + 1 + 1 / 4) / 4, 4):
        bad.append(f"measure: mrr {report['mrr']}")
    if report["found_by_the_agents_own_search"] != {"pairs_in_turns_with_a_search": 3, "@8": round(1 / 3, 4)}:
        bad.append(f"measure: agent search {report['found_by_the_agents_own_search']}")
    # 질의는 둘 다 스무 자 아래다 — 쓰인 노드 넷이 그 구간에 들고, 셋이 상위 8건에 든다.
    if report["by_query_chars"]["under 20"] != {"pairs": 4, "recall@8": 0.75} \
            or report["by_query_chars"]["200 or more"] != {"pairs": 0, "recall@8": None}:
        bad.append(f"measure: by size {report['by_query_chars']}")

    # 결속을 넣은 검색: 결속을 아는 대화의 턴만 그 순위로 재고, 모르는 턴은 결속 없는 순위 그대로다.
    stub.ranking[("넷째 질문", "bound")] = "AB"
    bound = rs.measure(stub, [{**t, "conversation": "c1" if t["query"] == "넷째 질문" else "c0"} for t in found],
                       2000, {"c1": {"scopes": ["s"], "primary": "s"}})["with_session_bindings"]
    if (bound["turns_with_a_binding"], bound["recall_by_prompt"]["@1"]) != (1, 0.5):
        bad.append(f"measure: bindings {bound['turns_with_a_binding']} {bound['recall_by_prompt']}")
    if "with_session_bindings" in report:
        bad.append("measure: a bindings report without bindings")

    # 커서가 있는 대화(결속이 없는 것도)의 턴을 따로 센다. 에이전트의 검색은 session을 실었던 것에만 결속을 넘긴다.
    binding = {"scopes": ["s"], "primary": "s"}
    mixed = [{"query": "q", "read": ["A"], "updated": [], "conversation": conversation,
              "searches": [(f"bound {conversation}", True), (f"plain {conversation}", False)]}
             for conversation in ("c1", "c2", "c3")]
    seen = rs.measure(stub, mixed, 2000, {"c1": binding, "c2": None})
    if (seen["turns"]["in_conversations_this_vault_recorded"], seen["with_session_bindings"]["turns_with_a_binding"]) \
            != (2, 1):
        bad.append(f"measure: conversations this vault recorded {seen['turns']}")
    if [stub.seen[f"{how} {c}"] for how in ("bound", "plain") for c in ("c1", "c2", "c3")] \
            != [binding, None, None, None, None, None]:
        bad.append(f"measure: bindings passed to the agent's searches {stub.seen}")

    # 지금 검색 대상에 없는 이름은 따로 센다. 쓰인 노드가 하나도 남지 않은 턴은 재지 않는다.
    gone = rs.measure(stub, [{"query": "q", "read": ["없는 노드"], "updated": [], "searches": []}], 2000)
    if (gone["used_nodes"]["names_not_searchable_now"], gone["turns"]["not_measurable"]) != (1, 1):
        bad.append(f"measure: unknown names {gone['used_nodes']} {gone['turns']}")

    bad += _copies() + _cursors()
    return bad


def _cursors() -> list[str]:
    """커서는 이 저장소의 경로(`root`)가 적힌 것만 센다 — git 디렉터리를 나눠 쓰는 다른 저장소의 커서는 빠진다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".git").mkdir()
        for name, where in (("here", root.resolve()), ("there", root.resolve() / "other")):
            (root / ".git" / f"osk-integration-{name}.json").write_text(json.dumps(
                {"harness": "claude", "session": name, "conversation_id": f"c-{name}", "root": str(where)}),
                encoding="utf-8")
        real, rs.routing.bindings = rs.routing.bindings, lambda vault, session: {"scopes": [session]}
        try:
            got = rs.bindings_by_conversation(SimpleNamespace(root=root))
        finally:
            rs.routing.bindings = real
    return [] if got == {"c-here": {"scopes": ["here"]}} else [f"bindings_by_conversation: {got}"]


def _turn(uuid, *names, text="같은 말"):
    return [user(text, **({"uuid": uuid} if uuid else {})), calls(*[("read_node", {"name": n}) for n in names])]


def _copies() -> list[str]:
    """이어 받은 세션의 사본은 원본과 같은 uuid를 가진다 — uuid마다 한 번만 세고, 도중에 끊긴 사본보다 쓴 노드가
    많은 쪽을 남긴다. uuid가 다르면 말과 노드가 같아도 따로 센다. uuid가 없는 턴은 합치지 않는다."""
    files = {"a": _turn("u1", "A") + _turn("u2", "A") + _turn("u3", "A") + _turn("u5", "A", "B") + _turn(None, "C"),
             "b": _turn("u1", "A") + _turn("u4", "A") + _turn("u3", "A", "B") + _turn("u5", "A") + _turn(None, "C")}
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "project"
        folder.mkdir()
        for name, rows in files.items():
            (folder / f"{name}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
                                                  + "찢긴 줄\n", encoding="utf-8")
        found, count = rs.samples(Path(tmp))
    got = sorted((t["id"] or "-", t["conversation"], tuple(t["read"])) for t in found)
    want = [("-", "a", ("C",)), ("-", "b", ("C",)), ("u1", "a", ("A",)), ("u2", "a", ("A",)),
            ("u3", "b", ("A", "B")), ("u4", "b", ("A",)), ("u5", "a", ("A", "B"))]
    return [] if (got, count) == (want, 2) else [f"samples: {got} from {count} files"]


def main() -> int:
    bad = problems()
    for line in bad:
        print("FAIL", line)
    print(f"회상의 섀도 측정: 다른 것 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
