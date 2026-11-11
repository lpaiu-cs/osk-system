"""osk.harness.claude_transcript — Claude 전사의 행을 라운드로.

Claude는 한 메시지를 여러 행으로 나눠 적는다(end_turn의 thinking·text까지). 버려진 턴을 닫는 기록이 없으므로,
앞선 엔진이 따라간 턴들 뒤에서는 마지막 답 없이 IDLE_CLOSE만큼 조용했던 턴을 중단으로 닫는다."""
from __future__ import annotations
from datetime import datetime
from .. import dialogue_codec
from . import base


IDLE_CLOSE = 12 * 3600  # seconds of silence after which an unfinished Claude turn closes


def _epoch(row: dict) -> float | None:
    try:
        return datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _queued(row: dict, delivered: set) -> str | None:
    """Words the user sent while the turn ran. Claude Code keeps them as a `queued_command`
    attachment and absorbs them into that turn: no user row follows. One delivered as its
    own next prompt is read from that prompt instead."""
    a = row.get("attachment")
    if (not isinstance(a, dict) or a.get("type") != "queued_command" or a.get("commandMode") != "prompt"
            or (a.get("origin") or {}).get("kind", "human") != "human" or a.get("source_uuid") in delivered):
        return None
    words = a.get("prompt")
    return words if isinstance(words, str) and words.strip() else None


def _interleaved(evidence: list) -> list:
    """A copy's tool events in its original's append order where a time sort cannot give
    it: the original may write parallel results out of their time order, and a copy in
    message order keeps that order. Such a copy keeps the calls' order and the results'
    order and only moves results behind the calls they streamed between, so merging the
    two back by row time undoes just that move; a tie keeps the copy's order. Copies that
    reorder the calls as well are left to the time sort."""
    rows = [(t, n, e) for n, (t, e) in enumerate(evidence)]
    calls = [r for r in rows if r[2]["type"] == "tool_call_ref"]
    results = [r for r in rows if r[2]["type"] != "tool_call_ref"]
    merged = []
    while calls and results:
        merged.append((calls if calls[0][:2] < results[0][:2] else results).pop(0))
    return [e for *_, e in merged + calls + results]


def _words(content) -> str | None:
    """A prompt's text when it is text alone — what `injected_only` judges."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(b, dict) and b.get("type") == "text" for b in content):
        return "\n".join(str(b.get("text") or "") for b in content)
    return None


def _rewound(rows: list) -> set:
    """Rows a Claude rewind dropped (#235). The app appends `last-prompt` with `rewound` and
    the kept leaf, and the next message hangs on that leaf: the rows written after the leaf
    and before that mark are the dropped branch. They stay in the file."""
    at = {r["uuid"]: i for i, (_, r) in enumerate(rows) if r.get("uuid")}
    dropped = set()
    for i, (_, r) in enumerate(rows):
        if r.get("type") == "last-prompt" and r.get("rewound") is True and r.get("leafUuid") in at:
            dropped.update(x["uuid"] for _, x in rows[at[r["leafUuid"]] + 1:i] if x.get("uuid"))
    return dropped


def parse(rows: list, sid: str, *, dialogue: bool = False, rules: dict | None = None,
            rewinds: bool = True) -> dict:
    rows = [(line, r) for line, r in rows if not r.get("isSidechain") and not r.get("agentId")]
    identities = {r["sessionId"] for _, r in rows if r.get("sessionId")}
    if identities != {sid}:
        own = next((i for i, (_, r) in enumerate(rows) if r.get("sessionId") == sid and r.get("uuid")), None)
        if sid in identities and own is None:
            # A freshly forked file can contain only parent history and child UI metadata.
            return {"rounds": [], "pending_tail": False, "diagnostics": []}
        ancestors = {r.get("uuid") for _, r in rows[:own] if r.get("uuid")} if own is not None else set()
        by_uuid = {r["uuid"]: r for _, r in rows[:own] if r.get("uuid")} if own is not None else {}
        chain, visited = set(), set()
        cursor = rows[own][1].get("parentUuid") if own is not None else None
        while cursor in by_uuid and cursor not in visited:
            visited.add(cursor)
            ancestor = by_uuid[cursor]
            chain.add(ancestor.get("sessionId"))
            cursor = ancestor.get("parentUuid") or ancestor.get("logicalParentUuid")
        foreign = {r.get("sessionId") for _, r in rows[:own]
                   if r.get("type") in ("user", "assistant") and r.get("sessionId") != sid}
        if (own is None or not ancestors or rows[own][1].get("parentUuid") not in ancestors
                or not foreign.issubset(chain)
                or any(r.get("sessionId") not in (None, sid) for _, r in rows[own:])):
            raise ValueError("Claude transcript sessionId does not match a linked ancestor prefix")
    rounds, diagnostics, users, trace, calls = [], [], [], [], {}
    native_results = []
    evidence = []  # (native row timestamp, event) in file order
    start, final_message, final_line, final_text = None, None, None, False
    seen = set()
    # The newer rules apply after `since`; a stored cut closes its turn on every read.
    since = rules.get("since") if rules else None
    new = rules is not None and since is None
    # A new prompt closes the open turn after `prompts_since`, the last round an engine without
    # that rule tracked (#218): a turn that ends in a tool call has no end_turn to close it.
    splits = rules is not None and "prompts_since" in rules
    split = splits and rules["prompts_since"] is None
    # Rows the harness wrote that are not the user's conversation — a notice it sent as a
    # prompt (#275), a branch a rewind dropped (#235) — read as such after `harness_since`,
    # the last round an engine without that rule tracked.
    marks = rules is not None and "harness_since" in rules
    mark = marks and rules["harness_since"] is None
    dropped, rewound = (_rewound(rows) if marks and rewinds else set()), False
    cuts = set(rules.get("cuts") or ()) if rules else set()
    now = rules.get("now") if rules else None
    delivered = {r.get("commandUuid") for _, r in rows if r.get("type") == "queue-operation"
                 and r.get("operation") == "remove" and r.get("reason") not in (None, "absorbed_mid_turn")}
    last = None       # (uuid, line, epoch) of the open turn's latest dialogue row
    resumed = None    # the round an idle close just ended; its later rows continue it
    continued = None
    prompt, replied, done = None, False, False  # the open turn's promptId, a reply, its last row ended it

    def emit(rid, completion, end_line, **extra):
        nonlocal users, trace, start, native_results, evidence, new, split, mark, continued, last
        locator = f"claude:{rid}"
        events = [e for _, e in evidence]
        found = {"id": rid, "user": "\n\n".join(users),
                 "agent": "\n\n".join(trace + dialogue_codec.tool_evidence(events, locator)), "end_line": end_line,
                 "completion": completion, "native_results": native_results, **extra}
        if continued:
            found["continued_from"] = continued
        # A resumed or forked copy rewrites rows in message order, so results
        # that streamed between parallel calls move after them. Row timestamps
        # survive the copy: the original append order is offered only to match
        # a copied prefix, never to capture.
        if all(t for t, _ in evidence):
            timed = [e for _, e in sorted(evidence, key=lambda x: x[0])]
            orders = [o for o in (timed, _interleaved(evidence)) if o != events]
            if orders:
                found["agent_time_orders"] = list(dict.fromkeys(
                    "\n\n".join(trace + dialogue_codec.tool_evidence(o, locator)) for o in orders))
        rounds.append(found)
        users, trace, start, native_results, evidence, continued, last = [], [], None, [], [], None, None
        new = new or (rules is not None and rid == since)
        split = split or (splits and rid == rules["prompts_since"])
        mark = mark or (marks and rid == rules["harness_since"])
        return rid

    def finish():
        nonlocal final_message, final_line, final_text, resumed
        if start and final_message and final_text:
            emit(f"{start}:{final_message}", "completed", final_line)
            resumed = None
        final_message, final_line, final_text = None, None, False

    def interrupt():
        # No final answer will come: close the turn at its latest row, which the cursor keeps.
        nonlocal resumed
        resumed = emit(f"{start}:{last[0]}", "interrupted", last[1], cut=last[0])

    for line, row in rows:
        if row.get("isSidechain") or row.get("agentId"):
            continue
        if mark and row.get("uuid") in dropped:
            rewound = True
            continue
        typ, msg = row.get("type"), row.get("message", {})
        if not isinstance(msg, dict):
            raise ValueError(f"unsupported Claude message at line {line}")
        # All rows of an end_turn message belong to its response, not just the
        # first row, whose content may be thinking and lack the final answer.
        if final_message and not (typ == "assistant" and msg.get("id") == final_message):
            finish()
        if typ == "attachment" and new and start:
            words = _queued(row, delivered)
            uid = row.get("uuid")
            if words and not (uid and uid in seen):
                # The message is the turn's latest activity: it moves the idle clock,
                # and a cut stored at it closes the turn after these words on every read.
                users.append(words)
                if uid:
                    seen.add(uid)
                    last = (uid, line, _epoch(row))
                    if uid in cuts:
                        interrupt()
            continue
        if typ not in ("user", "assistant"):
            continue
        uid = row.get("uuid")
        if not uid:
            raise ValueError(f"Claude message without stable uuid at line {line}")
        if uid in seen:
            continue
        seen.add(uid)
        content = msg.get("content")
        if not isinstance(content, (str, list)):
            raise ValueError(f"unsupported Claude content at line {line}")
        tool_result = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        notice = mark and typ == "user" and not tool_result and base.injected_only(_words(content))
        at = _epoch(row)
        if (new and start and not final_message and last and last[2] is not None and at is not None
                and at - last[2] >= IDLE_CLOSE):
            interrupt()
        if new and not start and resumed and (typ == "assistant" or tool_result):
            start, continued, resumed = uid, resumed, None
        if isinstance(content, list):
            kept = []
            for block in content:
                if dialogue:
                    locator = f"claude:{row.get('sessionId', sid)}:{uid}"
                    if typ == "user" and not tool_result:
                        locator = f"claude-user:{uid}"
                    event = dialogue_codec.dialogue_event(block, locator) if isinstance(block, dict) else None
                    if event and event["type"] in {"tool_call_ref", "tool_result_ref"}:
                        if start:
                            evidence.append((row.get("timestamp") or "", event))
                        continue
                    values = [event] if event is not None else dialogue_codec.dialogue_content([block], locator)
                    if start and (typ != "user" or tool_result):
                        native_results.extend(locator for v in values if isinstance(v, dict)
                                              and (v.get("native_result") or isinstance(v.get("content"), dict)
                                                   and v["content"].get("native_result")))
                    kept.extend(values)
                    continue
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls[block.get("id")] = {"name": block.get("name", ""), "input": block.get("input")}
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    call = calls.get(block.get("tool_use_id"))
                    locator = f"claude:{row.get('sessionId', sid)}:{uid}"
                    if start and call and (dialogue_codec.reads_file(call["name"]) or dialogue_codec.mixes_output(call["name"])):
                        native_results.append(locator)
                    block = {**block, "content": dialogue_codec.result_content(call, block.get("content"), locator)}
                kept.append(block)
            content = kept
        if typ == "user" and not tool_result:
            if row.get("isCompactSummary") or row.get("isMeta"):
                continue
            # Another prompt is another turn. A turn that ended in a tool call (the app's
            # next-step box, ScheduleWakeup) closes there; an interruption keeps its promptId.
            # A notice the harness sent as a prompt is neither (#275): an open turn goes on,
            # and work it starts with no turn open is a round without the user's words.
            if not notice and split and start and replied and row.get("promptId") not in (None, prompt):
                emit(f"{start}:{last[0]}", "completed" if done else "interrupted", last[1])
            if not start:
                start, resumed, prompt, replied = uid, None, row.get("promptId"), False
            if not notice:
                users.append(dialogue_codec.dump(content))
        elif start:
            replied = True
            if not dialogue or content:
                trace.append(f"### {typ}\n\n{dialogue_codec.dump(content)}")
            if typ == "assistant" and msg.get("stop_reason") == "end_turn":
                final_message, final_line = msg.get("id"), line
                if not final_message:
                    raise ValueError(f"Claude end_turn without message id at line {line}")
                final_text = final_text or (bool(content.strip()) if isinstance(content, str) else any(
                    isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                    for b in content))
        if start:
            last = (uid, line, at)
            done = tool_result or (typ == "assistant" and msg.get("stop_reason") in ("end_turn", "stop_sequence"))
            if uid in cuts and not final_message:
                interrupt()
    finish()
    if new and start and now is not None and last and last[2] is not None and now - last[2] >= IDLE_CLOSE:
        interrupt()
    # The open turn is not a round, but its user words may be cited (Mechanism §9 9항).
    return {"rounds": rounds, "pending_tail": bool(start), "diagnostics": diagnostics, "rewound": rewound,
            "tail": {"id": start, "user": "\n\n".join(users)} if start and users else None}
