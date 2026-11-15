"""선제 회상의 섀도 측정 — 사용자의 말로 검색했다면, 그 턴에 실제로 쓰인 노드가 위쪽에 나왔는가.

지금의 회상은 에이전트가 필요하다고 알아챌 때만 일어난다(`search`·`read_node`). 선제 회상(#245)은
프롬프트를 질의로 삼아 미리 싣는다. 싣기 전에 잴 수 있는 것이 있다: 지난 대화에서 에이전트가 읽거나
고친 노드를, 그 턴을 연 사용자의 말만으로 검색이 찾았겠는가.

    python benchmarks/recall_shadow.py --vault <저장소> [--projects ~/.claude/projects] [--json 결과.json]

- 읽는 것: Claude Code의 전사(저장소 밖)와 저장소의 노드. 저장소에 남기는 것은 없다 — 색인이 `.osk/`에
  잠깐 두었다 지우는 탐침 파일은 엔진이 검색할 때마다 하는 일이다.
- 내는 것: 수뿐이다. 질의도 노드의 이름도 내지 않는다 — 전사에는 사용자의 대화가 있다.
- 한 턴: 사용자의 말에서 다음 사용자의 말까지다. 그 사이에 osk 도구로 읽은(`read_node`) 노드와
  고친(`update_node`) 노드가 "쓰인 노드"다. 그 턴에 새로 만든 노드는 세지 않는다. 이어 받은 세션의 전사는
  앞부분을 원본과 같은 uuid로 가지므로, 턴은 그 턴을 연 사용자 행의 uuid마다 한 번만 센다.
- 질의: 사용자의 말에서 하네스가 끼워 넣은 알림을 뺀 것이다. 앞에서 `--max-query-chars`자까지 쓴다.

읽을 때의 한계:

- 지금의 저장소로 잰다. 그때의 저장소가 아니다. 그 뒤에 생긴 노드가 함께 겨루고, 이름이 바뀐 노드는
  "지금 검색 대상에 없는 이름"으로 빠진다.
- 실제 검색은 세션의 결속(그 프로젝트의 scope)을 순위에 넣는다. 넣지 않은 수와 넣은 수를 함께 낸다. 결속은
  저장소의 대화 커서가 적어 둔 세션의 지금 결속이다 — 커서가 없는 대화의 턴은 결속 없이 잰다. 에이전트의
  검색은 `session`을 실었던 것만 그 결속을 넣어 다시 돌린다.
- 전사가 어느 저장소의 것인지는 가리지 않는다 — 한 기계에서 저장소를 둘 쓰면 섞인다. 이 저장소의 커서가 있는
  대화의 턴 수를 함께 낸다(`in_conversations_this_vault_recorded`).
- 호출의 결과는 보지 않는다 — 받아들여지지 않은 `read_node`·`update_node`도 쓰인 것으로 센다. 에이전트가 그
  노드를 찾아 쓰려 한 턴이기 때문이다. 2026-11-15의 운영 기록에서 이 셈법으로 들고 나는 것은 쓰인 이름
  5,642건 가운데 7건이었다.
- 쓰인 노드는 에이전트가 한 일이지 했어야 할 일이 아니다. 찾지 못해 읽지 않은 노드는 여기에 없다.
- Claude Code의 전사만 읽는다.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "_governance" / "_engine"
sys.path.insert(0, str(ENGINE))
from osk import core, routing, search  # noqa: E402
from osk.harness import base  # noqa: E402
from osk.vault import Vault  # noqa: E402

OSK_TOOL = re.compile(r"mcp__osk[\w-]*__(read_node|update_node|create_node|search)")
KS = (1, 3, 5, 8, 20)
AGENT_K = 8                      # 표면의 `search`가 기본으로 돌려주는 수
SIZES = (("under 20", 0, 20), ("20 to 59", 20, 60), ("60 to 199", 60, 200), ("200 or more", 200, 10 ** 9))


def _words(content) -> str | None:
    """사용자 행의 글. 도구 결과를 실은 행이면 None이다."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return None


def _calls(content):
    """응답 행의 osk 도구 호출 — `(도구, 인자)`."""
    for block in content if isinstance(content, list) else ():
        if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            tool = OSK_TOOL.fullmatch(str(block.get("name") or ""))
            if tool:
                yield tool[1], block["input"]


def turns(rows):
    """노드를 쓴 턴마다 `{id, query, read, updated, searches}`. 그 턴에 만든 노드는 쓰인 것에서 뺀다.

    `id`는 턴을 연 사용자 행의 uuid, `searches`는 `(질의, session을 실었나)`다."""
    turn = None

    def closed(t):
        used = {kind: [n for n in t[kind] if n not in t["created"]] for kind in ("read", "updated")}
        return {**t, **used} if used["read"] or used["updated"] else None

    for row in rows:
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        if row.get("type") == "user" and not (row.get("isMeta") or row.get("isCompactSummary")):
            text = _words(message.get("content"))
            if text is None or not text.strip() or base.injected_only(text):
                continue
            if turn and closed(turn):
                yield closed(turn)
            turn = {"id": row.get("uuid"), "query": base.GOAL.sub("", base.INJECTED.sub("", text)).strip(),
                    "read": [], "updated": [], "created": set(), "searches": []}
        elif row.get("type") == "assistant" and turn is not None:
            for tool, args in _calls(message.get("content")):
                value = args.get({"search": "query", "create_node": "title"}.get(tool, "name"))
                if not isinstance(value, str) or not value.strip():
                    continue
                if tool == "search":
                    turn["searches"].append((value, bool(args.get("session"))))
                elif tool == "create_node":
                    turn["created"].add(value.strip())
                else:
                    turn["read" if tool == "read_node" else "updated"].append(value.strip())
    if turn and closed(turn):
        yield closed(turn)


def _rows(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:            # 찢긴 줄 — 그 줄만 건너뛴다
                continue


def samples(projects: Path) -> tuple[list[dict], int]:
    """전사들의 턴. 이어 받은 세션은 앞부분을 원본과 같은 uuid로 가지므로 턴은 uuid마다 한 번만 센다 — 도중에
    끊긴 사본은 원본의 앞부분이라, 쓴 노드가 더 많은 쪽을 남긴다. uuid가 없는 행의 턴은 합치지 않는다."""
    kept, files = {}, 0
    for path in sorted(projects.glob("*/*.jsonl")):
        files += 1
        for turn in turns(_rows(path)):
            key = turn["id"] or object()
            old = kept.get(key)
            if old is None or len(turn["read"] + turn["updated"]) > len(old["read"] + old["updated"]):
                kept[key] = {**turn, "conversation": path.stem}
    return list(kept.values()), files


def bindings_by_conversation(vault: Vault) -> dict:
    """대화마다 그 세션의 결속 — 저장소의 대화 커서가 적어 둔 세션 키로 읽는다. 실제 검색이 순위에 넣는 것이다.

    커서가 있는 대화는 모두 돌려준다. 결속에 scope가 없으면 값은 None이다."""
    out, by_session = {}, {}
    for cursor in core.local_lock_path("x", vault.root).parent.glob("osk-integration-*.json"):
        try:
            state = json.loads(cursor.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        session = state.get("session") if isinstance(state, dict) else None
        if not isinstance(session, str) or state.get("harness") != "claude":
            continue
        if session not in by_session:
            by_session[session] = routing.bindings(vault, session)
        out[state.get("conversation_id")] = by_session[session] if by_session[session]["scopes"] else None
    return out


def measure(searcher, found: list[dict], max_chars: int, local_of: dict | None = None) -> dict:
    """쓰인 노드마다, 사용자의 말로 한 검색에서 그 노드가 몇째였는가. `local_of`를 주면 결속을 넣은 순위도 잰다."""
    by_name = {stem: node.id for stem, _path, node in searcher.paths}
    known = set(by_name.values())
    ranks, kinds, pulled, lengths = [], [], [], []      # 쓰인 노드마다: 순위, 읽음·고침, 에이전트의 검색이 찾았나
    bound_ranks, bound_turns = [], 0                    # 결속을 넣은 검색의 순위 — 결속을 모르는 턴은 위와 같다
    sizes = []                                          # 쓰인 노드마다: 그 턴의 질의 길이(자)
    unknown = unsearchable = with_agent_search = in_vault = 0
    any_hit = {k: 0 for k in KS}
    for turn in found:
        used = {}
        for kind in ("read", "updated"):                  # 읽고 고친 노드는 고친 것으로 센다
            for name in turn[kind]:
                name = name.removeprefix("[[").removesuffix("]]").strip()
                node_id = by_name.get(name) or (name if name in known else None)
                if node_id:
                    used[node_id] = kind
                else:
                    unknown += 1
        query = turn["query"][:max_chars]
        if not used or not query:
            unsearchable += 1
            continue
        lengths.append(len(query))
        hits = [h["id"] for h in searcher.view_search(query, max(KS))]
        local = (local_of or {}).get(turn.get("conversation"))
        in_vault += turn.get("conversation") in (local_of or {})
        agent = {h["id"] for q, bound in turn["searches"]                 # 결속은 session을 실었던 검색에만
                 for h in searcher.view_search(q, AGENT_K, local if bound else None)}
        with_agent_search += bool(turn["searches"])
        here = [hits.index(node_id) + 1 if node_id in hits else None for node_id in used]
        if local:
            bound_turns += 1
            hits = [h["id"] for h in searcher.view_search(query, max(KS), local)]
        bound_ranks += [hits.index(node_id) + 1 if node_id in hits else None for node_id in used]
        sizes += [len(query)] * len(used)
        ranks += here
        kinds += list(used.values())
        pulled += [node_id in agent if turn["searches"] else None for node_id in used]   # None: 검색이 없던 턴
        best = min((r for r in here if r), default=None)
        for k in KS:
            any_hit[k] += best is not None and best <= k
    turns_measured, pairs = len(lengths), len(ranks)
    searched = [p for p in pulled if p is not None]

    def recall(k, kind=None, of=ranks):
        chosen = [r for r, x in zip(of, kinds) if kind in (None, x)]
        return round(sum(r is not None and r <= k for r in chosen) / len(chosen), 4) if chosen else None

    def by_size(of):
        """질의 길이의 구간마다 상위 8건의 회수 — 짧은 지시("진행해")가 수를 끌어내리는지 본다."""
        out = {}
        for label, low, high in SIZES:
            chosen = [r for r, n in zip(of, sizes) if low <= n < high]
            out[label] = {"pairs": len(chosen),
                          "recall@8": round(sum(r is not None and r <= 8 for r in chosen) / len(chosen), 4)
                          if chosen else None}
        return out

    bound = {} if local_of is None else {"with_session_bindings": {
        "turns_with_a_binding": bound_turns,
        "recall_by_prompt": {f"@{k}": recall(k, of=bound_ranks) for k in KS},
        "recall_by_prompt_read": {f"@{k}": recall(k, "read", bound_ranks) for k in KS},
        "recall_by_prompt_updated": {f"@{k}": recall(k, "updated", bound_ranks) for k in KS}}}
    return {
        **bound,
        "turns": {"with_used_nodes": len(found), "measured": turns_measured, "not_measurable": unsearchable,
                  "in_conversations_this_vault_recorded": in_vault, "with_an_agent_search": with_agent_search},
        "used_nodes": {"pairs": pairs, "read": kinds.count("read"), "updated": kinds.count("updated"),
                       "names_not_searchable_now": unknown},
        "recall_by_prompt": {f"@{k}": recall(k) for k in KS},
        "recall_by_prompt_read": {f"@{k}": recall(k, "read") for k in KS},
        "recall_by_prompt_updated": {f"@{k}": recall(k, "updated") for k in KS},
        "turns_with_any_used_node": {f"@{k}": round(any_hit[k] / turns_measured, 4) if turns_measured else None
                                     for k in KS},
        "mrr": round(sum(1 / r for r in ranks if r) / pairs, 4) if pairs else None,
        "found_by_the_agents_own_search": {
            "pairs_in_turns_with_a_search": len(searched),
            f"@{AGENT_K}": round(sum(searched) / len(searched), 4) if searched else None},
        "query_chars": {"median": int(statistics.median(lengths)) if lengths else None,
                        "max_used": max_chars},
        "by_query_chars": by_size(ranks if local_of is None else bound_ranks),
        "searchable_nodes": len(searcher.paths),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="선제 회상의 섀도 측정 — 수만 낸다")
    ap.add_argument("--vault", required=True, type=Path)
    ap.add_argument("--projects", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--max-query-chars", type=int, default=2000)
    ap.add_argument("--json", type=Path, help="결과를 이 파일에도 쓴다")
    a = ap.parse_args(argv)
    found, files = samples(a.projects)
    vault = Vault.open(a.vault)
    report = {"transcripts": files,
              **measure(search.Searcher(vault), found, a.max_query_chars, bindings_by_conversation(vault))}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if a.json:
        a.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
