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
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": " 에이전트의 답 "}]}},
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

    # 앞 턴의 것: 넷째 턴의 바로 앞 사용자의 말은 노드를 쓰지 않은 둘째 질문이고, 에이전트가 바로 앞에 한 말과
    # 앞 턴들이 쓰거나 만든 노드가 호출한 차례대로 따라온다. 첫 턴에는 앞의 말이 없다.
    last = found[-1] if found else {}
    if (last.get("previous"), last.get("reply"), last.get("earlier"), first.get("reply")) \
            != ("둘째 질문", "에이전트의 답", ["A", "[[B]]", "New", "New", "C"], None):
        bad.append(f"turns: carried {last.get('previous')} {last.get('reply')} {last.get('earlier')}")

    # 첫 턴: A는 둘째, B는 없음, C는 첫째. 넷째 턴: A가 넷째. 에이전트의 검색은 A만 찾는다.
    stub = Stub({"첫 질문": "CAD", "넷째 질문": "DECA", "agent query": "A", "넷째 질문\n둘째 질문": "A",
                 "넷째 질문\n에이전트의 답": "BA"})
    report = rs.measure(stub, found, 2000)
    want = {"@1": 0.25, "@3": 0.5, "@5": 0.75, "@8": 0.75, "@20": 0.75}
    if report["recall"]["prompt"] != want or report["recall_without_bindings"]["prompt"] != want:
        bad.append(f"measure: recall {report['recall']['prompt']} {report['recall_without_bindings']}")
    # 앞 사용자의 말을 더한 질의는 넷째 턴의 A를 첫째로 올린다. 첫 턴에는 더할 말이 없어 질의가 달라지고 찾지 못한다.
    if report["recall"]["prompt + previous prompt"]["@1"] != 0.25:
        bad.append(f"measure: with the previous prompt {report['recall']['prompt + previous prompt']}")
    # 에이전트의 앞 말을 더한 질의는 넷째 턴의 A를 둘째에 둔다.
    replied = report["recall"]["prompt + end of the agent's last reply"]
    if (replied["@1"], replied["@3"]) != (0.0, 0.25):
        bad.append(f"measure: with the agent's reply {replied}")
    # 검색 없이: 넷째 턴의 A는 앞 턴들이 쓴 노드 가운데 셋째로 최근이다(C, B, A — New는 지금 검색 대상에 없다).
    carried = report["carried_from_earlier_turns"]
    if (carried["@1"], carried["@3"]) != (0.0, 0.25) or report["either@8"]["prompt or carried"] != 0.75:
        bad.append(f"measure: carried {carried} {report['either@8']}")
    # 그 대화에서 처음 쓰인 노드는 첫 턴의 셋이다 — 넷째 턴의 A는 앞 턴에 쓴 것이라 빠진다.
    fresh = report["first_use_in_the_conversation"]
    if (fresh["pairs"], fresh["recall"]["prompt"]["@1"], fresh["recall"]["prompt"]["@3"]) \
            != (3, round(1 / 3, 4), round(2 / 3, 4)) \
            or fresh["found_by_the_agents_own_search"] != report["found_by_the_agents_own_search"]:
        bad.append(f"measure: first use {fresh}")
    if report["used_nodes"] != {"pairs": 4, "read": 3, "updated": 1, "names_not_searchable_now": 0}:
        bad.append(f"measure: used {report['used_nodes']}")
    if report["turns_with_any_used_node"]["@1"] != 0.5 or report["turns_with_any_used_node"]["@5"] != 1.0:
        bad.append(f"measure: turns {report['turns_with_any_used_node']}")
    if report["mrr"] != round((1 / 2 + 1 + 1 / 4) / 4, 4):
        bad.append(f"measure: mrr {report['mrr']}")
    # 검색이 있던 턴은 첫 턴뿐이다 — 그 셋 가운데 에이전트의 검색은 하나를, 사용자의 말은 둘을 찾는다.
    if report["found_by_the_agents_own_search"] != {"pairs_in_turns_with_a_search": 3, "@8": round(1 / 3, 4),
                                                    "prompt@8_on_those_pairs": round(2 / 3, 4)}:
        bad.append(f"measure: agent search {report['found_by_the_agents_own_search']}")
    # 사용자의 말은 둘 다 스무 자 아래다 — 쓰인 노드 넷이 그 구간에 들고, 셋이 상위 8건에 든다.
    short, long = report["by_query_chars"]["under 20"], report["by_query_chars"]["200 or more"]
    if (short["pairs"], short["recall@8"]["prompt"]) != (4, 0.75) \
            or long != {"pairs": 0, "recall@8": dict.fromkeys(rs.QUERIES)}:
        bad.append(f"measure: by size {report['by_query_chars']}")

    # 결속을 넣은 검색: 결속을 아는 대화의 턴만 그 순위로 재고, 모르는 턴은 결속 없는 순위 그대로다.
    stub.ranking[("넷째 질문", "bound")] = "AB"
    bound = rs.measure(stub, [{**t, "conversation": "c1" if t["query"] == "넷째 질문" else "c0"} for t in found],
                       2000, {"c1": {"scopes": ["s"], "primary": "s"}})
    if (bound["turns"]["with_a_binding"], bound["recall"]["prompt"]["@1"],
            bound["recall_without_bindings"]["prompt"]["@1"]) != (1, 0.5, 0.25):
        bad.append(f"measure: bindings {bound['turns']} {bound['recall']['prompt']}")
    if report["turns"]["with_a_binding"] != 0:
        bad.append("measure: a binding counted without bindings")

    # 커서가 있는 대화(결속이 없는 것도)의 턴을 따로 센다. 에이전트의 검색은 session을 실었던 것에만 결속을 넘긴다.
    binding = {"scopes": ["s"], "primary": "s"}
    mixed = [{"query": "q", "read": ["A"], "updated": [], "conversation": conversation,
              "searches": [(f"bound {conversation}", True), (f"plain {conversation}", False)]}
             for conversation in ("c1", "c2", "c3")]
    seen = rs.measure(stub, mixed, 2000, {"c1": binding, "c2": None})
    if (seen["turns"]["in_conversations_this_vault_recorded"], seen["turns"]["with_a_binding"]) != (2, 1):
        bad.append(f"measure: conversations this vault recorded {seen['turns']}")
    if [stub.seen[f"{how} {c}"] for how in ("bound", "plain") for c in ("c1", "c2", "c3")] \
            != [binding, None, None, None, None, None]:
        bad.append(f"measure: bindings passed to the agent's searches {stub.seen}")

    # 최근의 차례는 호출한 차례다: read A → update B → read C 다음 턴이 C를 쓰면 C가 가장 최근이다.
    order = list(rs.turns([user("t1"), calls(("read_node", {"name": "A"}), ("update_node", {"name": "B"})),
                           calls(("read_node", {"name": "C"})), user("t2"), calls(("read_node", {"name": "C"}))]))
    if rs.measure(stub, order, 2000)["carried_from_earlier_turns"]["@1"] != 0.25:
        bad.append(f"measure: carried in call order {order[-1].get('earlier')}")

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
        inside, read = rs.samples(Path(tmp), {"b"})     # `only` 밖의 전사는 읽지 않는다 — u5는 b의 사본이 남는다
    got = sorted((t["id"] or "-", t["conversation"], tuple(t["read"])) for t in found)
    want = [("-", "a", ("C",)), ("-", "b", ("C",)), ("u1", "a", ("A",)), ("u2", "a", ("A",)),
            ("u3", "b", ("A", "B")), ("u4", "b", ("A",)), ("u5", "a", ("A", "B"))]
    only = sorted((t["id"] or "-", t["conversation"], tuple(t["read"])) for t in inside)
    want_only = [("-", "b", ("C",)), ("u1", "b", ("A",)), ("u3", "b", ("A", "B")), ("u4", "b", ("A",)),
                 ("u5", "b", ("A",))]
    bad = [] if (got, count) == (want, 2) else [f"samples: {got} from {count} files"]
    return bad + ([] if (only, read) == (want_only, 1) else [f"samples only: {only} from {read} files"])


def main() -> int:
    bad = problems()
    for line in bad:
        print("FAIL", line)
    print(f"회상의 섀도 측정: 다른 것 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
