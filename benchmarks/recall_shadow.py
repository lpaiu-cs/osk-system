"""선제 회상의 섀도 측정 — 사용자의 말로 검색했다면, 그 턴에 실제로 쓰인 노드가 위쪽에 나왔는가.

지금의 회상은 에이전트가 필요하다고 알아챌 때만 일어난다(`search`·`read_node`). 선제 회상(#245)은
프롬프트를 질의로 삼아 미리 싣는다. 싣기 전에 잴 수 있는 것이 있다: 지난 대화에서 에이전트가 읽거나
고친 노드를, 그 턴을 연 사용자의 말만으로 검색이 찾았겠는가.

    python benchmarks/recall_shadow.py --vault <저장소> [--projects ~/.claude/projects] [--json 결과.json]

- 읽는 것: Claude Code의 전사(저장소 밖)와 저장소의 노드. 저장소에 남기는 것은 없다 — 색인이 `.osk/`에
  잠깐 두었다 지우는 탐침 파일은 엔진이 검색할 때마다 하는 일이다.
- 내는 것: 수뿐이다. 질의도 노드의 이름도 내지 않는다 — 전사에는 사용자의 대화가 있다.
- 한 턴: 사용자의 말에서 다음 사용자의 말까지다. 그 사이에 osk 도구로 읽은(`read_node`) 노드와
  고친(`update_node`) 노드가 "쓰인 노드"다. 그 턴에 새로 만든 노드는 세지 않는다.
- 질의: 사용자의 말에서 하네스가 끼워 넣은 알림을 뺀 것이다. 앞에서 `--max-query-chars`자까지 쓴다.
- 질의를 만드는 법을 견준다(`QUERIES`): 사용자의 말 그대로, 바로 앞 사용자의 말을 더한 것, 에이전트가 바로
  앞에 한 말의 끝을 더한 것, 저장소에서 드문 낱말만 남긴 것.
- 검색 없이 재는 것 하나 — 쓰인 노드가 그 대화의 앞 턴들이 쓴 노드 가운데 몇째로 최근이었는가
  (`carried_from_earlier_turns`). 앞 턴에 쓴 노드는 에이전트가 이미 아는 노드다. 그래서 그 대화에서 처음 쓰인
  노드만 따로도 센다(`first_use_in_the_conversation`) — 회상이 보탤 수 있는 몫은 거기다.

읽을 때의 한계:

- 지금의 저장소로 잰다. 그때의 저장소가 아니다. 그 뒤에 생긴 노드가 함께 겨루고, 이름이 바뀐 노드는
  "지금 검색 대상에 없는 이름"으로 빠진다.
- 실제 검색은 세션의 결속(그 프로젝트의 scope)을 순위에 넣는다. 넣지 않은 수와 넣은 수를 함께 낸다. 결속은
  저장소의 대화 커서가 적어 둔 세션의 지금 결속이다 — 커서가 없는 대화의 턴은 결속 없이 잰다.
- 쓰인 노드는 에이전트가 한 일이지 했어야 할 일이 아니다. 찾지 못해 읽지 않은 노드는 여기에 없다.
- Claude Code의 전사만 읽는다.
"""
from __future__ import annotations

import argparse
import hashlib
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
HISTORY = 200                    # 한 전사에서 기억해 두는 앞 턴의 노드 이름 수
RARE = 12                        # "드문 낱말" 질의에 남기는 낱말 수 — 첫 시도의 값이다
REPLY = 500                      # 에이전트의 앞 답에서 질의에 붙이는 끝부분의 글자 수 — 첫 시도의 값이다


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
    """노드를 쓴 턴마다 `{query, previous, reply, earlier, read, updated, searches}`.

    `previous`는 그 전사에서 바로 앞 사용자의 말, `reply`는 그 턴 바로 앞에서 에이전트가 한 마지막 말이고,
    `earlier`는 앞 턴들이 쓰거나 만든 노드의 이름이다(쓴 차례대로, 끝이 가장 최근). 그 턴에 만든 노드는 쓰인
    것에서 뺀다."""
    turn, last_query, last_reply, history = None, None, None, []

    def close(t):
        used = {kind: [n for n in t[kind] if n not in t["created"]] for kind in ("read", "updated")}
        history.extend(t["read"] + t["updated"] + sorted(t["created"]))
        del history[:-HISTORY]
        return {**t, **used} if used["read"] or used["updated"] else None

    for row in rows:
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        if row.get("type") == "user" and not (row.get("isMeta") or row.get("isCompactSummary")):
            text = _words(message.get("content"))
            if text is None or not text.strip() or base.injected_only(text):
                continue
            done = close(turn) if turn else None
            if done:
                yield done
            query = base.GOAL.sub("", base.INJECTED.sub("", text)).strip()
            turn = {"query": query, "previous": last_query, "reply": last_reply, "earlier": list(history),
                    "read": [], "updated": [], "created": set(), "searches": []}
            last_query = query
        elif row.get("type") == "assistant":
            last_reply = (_words(message.get("content")) or "").strip() or last_reply
            for tool, args in _calls(message.get("content")) if turn is not None else ():
                value = args.get({"search": "query", "create_node": "title"}.get(tool, "name"))
                if not isinstance(value, str) or not value.strip():
                    continue
                if tool == "search":
                    turn["searches"].append(value)
                elif tool == "create_node":
                    turn["created"].add(value.strip())
                else:
                    turn["read" if tool == "read_node" else "updated"].append(value.strip())
    done = close(turn) if turn else None
    if done:
        yield done


def _rows(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:            # 찢긴 줄 — 그 줄만 건너뛴다
                continue


def samples(projects: Path) -> tuple[list[dict], int]:
    """전사들의 턴. 이어 받은 세션은 앞부분을 사본으로 가지므로 같은 턴은 한 번만 센다."""
    seen, found, files = set(), [], 0
    for path in sorted(projects.glob("*/*.jsonl")):
        files += 1
        for turn in turns(_rows(path)):
            key = hashlib.sha256(json.dumps([turn["query"], turn["read"], turn["updated"]],
                                            ensure_ascii=False).encode()).hexdigest()
            if key not in seen:
                seen.add(key)
                found.append({**turn, "conversation": path.stem})
    return found, files


def bindings_by_conversation(vault: Vault) -> dict:
    """대화마다 그 세션의 결속 — 저장소의 대화 커서가 적어 둔 세션 키로 읽는다. 실제 검색이 순위에 넣는 것이다."""
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
        if by_session[session]["scopes"]:
            out[state.get("conversation_id")] = by_session[session]
    return out


def _rare(searcher, text: str) -> str:
    """글의 낱말 가운데 저장소에서 가장 드문 것 `RARE`개 — 흔한 말을 덜어 낸 질의."""
    idf = getattr(getattr(searcher, "bm25", None), "idf", None)
    if not idf:
        return text
    tokens = [t for t in dict.fromkeys(search._tokens(text)) if t in idf]
    return " ".join(sorted(tokens, key=lambda t: -idf[t])[:RARE])


# 질의를 만드는 법 — 이름과 `(검색기, 턴) -> 질의`. 첫째가 기준이다(사용자의 말 그대로).
QUERIES = {
    "prompt": lambda s, t: t["query"],
    "prompt + previous prompt": lambda s, t: f'{t["query"]}\n{t.get("previous") or ""}',
    "prompt + end of the agent's last reply": lambda s, t: f'{t["query"]}\n{(t.get("reply") or "")[-REPLY:]}',
    "rare words of the prompt": lambda s, t: _rare(s, t["query"]),
    "rare words of prompt + previous": lambda s, t: _rare(s, f'{t["query"]}\n{t.get("previous") or ""}'),
}


def _pairs(searcher, found: list[dict], max_chars: int, local_of: dict | None) -> tuple[list[dict], dict]:
    """쓰인 노드마다 한 줄 — 질의마다의 순위, 앞 턴들이 쓴 노드 가운데 몇째로 최근인가, 에이전트의 검색이 찾았나."""
    by_name = {stem: node.id for stem, _path, node in searcher.paths}
    known = set(by_name.values())
    rows, counts = [], {"unknown": 0, "not_measurable": 0, "measured": 0, "agent_search": 0, "bound": 0}

    def resolve(name):
        name = name.removeprefix("[[").removesuffix("]]").strip()
        return by_name.get(name) or (name if name in known else None)

    def ranked(query, local=None):
        return [h["id"] for h in searcher.view_search(query[:max_chars], max(KS), local)] if query.strip() else []

    def place(ids, node_id):
        return ids.index(node_id) + 1 if node_id in ids else None

    for turn in found:
        used = {}
        for kind in ("read", "updated"):                  # 읽고 고친 노드는 고친 것으로 센다
            for name in turn[kind]:
                node_id = resolve(name)
                if node_id:
                    used[node_id] = kind
                else:
                    counts["unknown"] += 1
        if not used or not turn["query"][:max_chars]:
            counts["not_measurable"] += 1
            continue
        local = (local_of or {}).get(turn.get("conversation"))
        counts["measured"] += 1
        counts["bound"] += bool(local)
        counts["agent_search"] += bool(turn["searches"])
        lists = {name: ranked(make(searcher, turn), local) for name, make in QUERIES.items()}
        plain = ranked(turn["query"]) if local else lists["prompt"]
        recent = list(dict.fromkeys(filter(None, map(resolve, reversed(turn.get("earlier") or [])))))
        agent = {h["id"] for q in turn["searches"] for h in searcher.view_search(q, AGENT_K)}
        for node_id, kind in used.items():
            rows.append({"turn": counts["measured"], "kind": kind, "size": len(turn["query"][:max_chars]),
                         "plain": place(plain, node_id), "carry": place(recent, node_id),
                         "agent": node_id in agent if turn["searches"] else None,
                         **{name: place(ids, node_id) for name, ids in lists.items()}})
    return rows, counts


def _share(rows: list[dict], hit) -> float | None:
    return round(sum(map(hit, rows)) / len(rows), 4) if rows else None


def _at(rows: list[dict], field: str) -> dict:
    return {f"@{k}": _share(rows, lambda r, k=k: r[field] is not None and r[field] <= k) for k in KS}


def _within(r: dict, field: str) -> bool:
    return r[field] is not None and r[field] <= AGENT_K


def _top(rows: list[dict]) -> dict:
    """질의를 만드는 법마다 상위 8건의 회수."""
    return {name: _share(rows, lambda r, n=name: _within(r, n)) for name in QUERIES}


def _by_size(rows: list[dict]) -> dict:
    """사용자의 말의 길이 구간마다 상위 8건의 회수 — 짧은 지시("진행해")가 수를 끌어내리는지, 무엇이 메우는지 본다."""
    out = {}
    for label, low, high in SIZES:
        chosen = [r for r in rows if low <= r["size"] < high]
        out[label] = {"pairs": len(chosen), f"recall@{AGENT_K}": _top(chosen)}
    return out


def _agent(rows: list[dict]) -> dict:
    """에이전트가 그 턴에 스스로 한 검색이 찾은 몫 — 같은 쌍에서 사용자의 말이 찾은 몫과 나란히."""
    searched = [r for r in rows if r["agent"] is not None]
    return {"pairs_in_turns_with_a_search": len(searched), f"@{AGENT_K}": _share(searched, lambda r: r["agent"]),
            f"prompt@{AGENT_K}_on_those_pairs": _share(searched, lambda r: _within(r, "prompt"))}


def measure(searcher, found: list[dict], max_chars: int, local_of: dict | None = None) -> dict:
    """쓰인 노드가 상위 몇 건 안에 드는가 — 질의를 만드는 법마다, 그리고 검색 없이 앞 턴의 노드를 다시 싣는다면."""
    rows, counts = _pairs(searcher, found, max_chars, local_of)
    fresh = [r for r in rows if r["carry"] is None]          # 그 대화에서 처음 쓰인 노드 — 회상이 보탤 수 있는 몫이다
    turns_hit = {k: len({r["turn"] for r in rows if r["prompt"] is not None and r["prompt"] <= k}) for k in KS}
    sizes = [r["size"] for r in {r["turn"]: r for r in rows}.values()]
    return {
        "turns": {"with_used_nodes": len(found), "measured": counts["measured"],
                  "not_measurable": counts["not_measurable"], "with_a_binding": counts["bound"],
                  "with_an_agent_search": counts["agent_search"]},
        "used_nodes": {"pairs": len(rows), "read": sum(r["kind"] == "read" for r in rows),
                       "updated": sum(r["kind"] == "updated" for r in rows),
                       "names_not_searchable_now": counts["unknown"]},
        "recall": {name: _at(rows, name) for name in QUERIES},
        "recall_without_bindings": {"prompt": _at(rows, "plain")},
        "recall_by_kind": {kind: {"prompt": _at([r for r in rows if r["kind"] == kind], "prompt")}
                           for kind in ("read", "updated")},
        "carried_from_earlier_turns": _at(rows, "carry"),
        f"either@{AGENT_K}": {f"{name} or carried": _share(rows, lambda r, n=name: _within(r, n) or _within(r, "carry"))
                              for name in QUERIES},
        "first_use_in_the_conversation": {"pairs": len(fresh), "recall": {name: _at(fresh, name) for name in QUERIES},
                                          "found_by_the_agents_own_search": _agent(fresh)},
        "turns_with_any_used_node": {f"@{k}": round(turns_hit[k] / counts["measured"], 4) if counts["measured"]
                                     else None for k in KS},
        "mrr": round(sum(1 / r["prompt"] for r in rows if r["prompt"]) / len(rows), 4) if rows else None,
        "found_by_the_agents_own_search": _agent(rows),
        "by_query_chars": _by_size(rows),
        "query_chars": {"median": int(statistics.median(sizes)) if sizes else None, "max_used": max_chars},
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
