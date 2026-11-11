"""osk.harness.kiro_transcript — Kiro 세션의 행을 라운드로."""
from __future__ import annotations
from .. import dialogue_codec


# Kiro `turn_end.stopReason` → completion. Only end_turn is success; the rest keep their
# native terminal record (Bylaws §2 2: failure and interruption stay distinct).
_KIRO_END = {"end_turn": "completed", "cancelled": "aborted", "error": "failed",
             "content_filtered": "failed"}


_KIRO_CALLS = {"tool_call": ("tool_call_ref", "toolCallId", "args"),
               "tool_result": ("tool_result_ref", "toolCallId", "content"),
               "sub_agent_start": ("tool_call_ref", "subSessionId", "prompt"),
               "sub_agent_complete": ("tool_result_ref", "subSessionId", "response")}


_KIRO_EVENTS = {"pending_interaction", "interaction_resolved"}   # tool approvals


def _kiro_user(p: dict, locator: str) -> str:
    content = p.get("content")
    attachments = [a for k in ("images", "documents") for a in p.get(k) or []]
    if isinstance(content, str) and not attachments:
        return dialogue_codec.dump(content)
    content = dialogue_codec.dialogue_content(content, locator)
    content = [{"type": "text", "text": content}] if isinstance(content, str) else content
    return dialogue_codec.dump({"content": content + [{"type": "attachment_ref", **dialogue_codec.reference(a, locator)}
                                        for a in attachments]})


def parse(rows: list, sid: str) -> dict:
    """Kiro `messages.jsonl` rows are `{id, timestamp, payload}`. User rows come before
    the turn they open; `turn_start`…`turn_end` share one executionId. Replies are
    `assistant` rows whose operationType is Say — Reasoning and the compaction Summary
    are internal. Tool calls, sub-agents and approvals become one evidence reference."""
    rounds, diagnostics, queued, seen = [], [], [], set()
    turn, users, trace, evidence = None, [], [], []

    def finish(completion, line, terminal=None):
        agent = trace + dialogue_codec.tool_evidence(evidence, f"kiro:{sid}:{turn}")
        if terminal is not None:
            agent.append(dialogue_codec.dump(terminal))
        if turn not in seen:
            rounds.append({"id": turn, "user": "\n\n".join(
                users or [dialogue_codec.dump({"native_trigger": "inputless", "turn_id": turn})]),
                "agent": "\n\n".join(agent), "end_line": line, "completion": completion})
            seen.add(turn)

    for line, row in rows:
        p = row.get("payload")
        if not isinstance(p, dict):
            raise ValueError(f"unsupported Kiro row at line {line}")
        typ = p.get("type")
        locator = f"kiro:{sid}:{turn}:{row.get('id')}"
        if typ == "user":
            queued.append(_kiro_user(p, f"kiro:{sid}:{row.get('id')}:user"))
        elif typ == "turn_start":
            if turn:
                finish("interrupted", line - 1, {"type": "superseded", "turn_id": turn,
                                                  "next_turn_id": p.get("executionId")})
            turn, users, trace, evidence = p.get("executionId"), queued, [], []
            queued = []
            if not turn:
                raise ValueError(f"Kiro turn_start without executionId at line {line}")
        elif not turn:
            continue          # session metadata, the system prompt and compaction outside turns
        elif typ == "assistant" and p.get("operationType") == "Say":
            content = p.get("content")
            trace.append(content if isinstance(content, str) else dialogue_codec.dump(dialogue_codec.dialogue_content(content, locator)))
        elif typ in _KIRO_CALLS:
            kind, key, value = _KIRO_CALLS[typ]
            ref = dialogue_codec.reference(p.get(value), locator)
            evidence.append({"type": kind, "name": p.get("toolName") or p.get("subAgentName"),
                             "call_id": p.get(key), **ref} if kind == "tool_call_ref" else
                            {"type": kind, "tool_use_id": p.get(key), "content": ref})
        elif typ in _KIRO_EVENTS:
            evidence.append({"type": "event_ref", "event": typ, **dialogue_codec.reference(p, locator)})
        elif typ == "turn_end":
            if p.get("executionId") != turn:
                raise ValueError(f"Kiro turn_end executionId mismatch at line {line}")
            completion = _KIRO_END.get(p.get("stopReason"), "interrupted")
            # A success with reply text needs no terminal record; anything else keeps it.
            finish(completion, line, None if completion == "completed" and trace else
                   {k: p[k] for k in ("type", "stopReason", "stopDetails") if k in p})
            turn, users, trace, evidence = None, [], [], []
    # The open turn's words under the executionId its finished round will keep, so the
    # current request can be cited while it is being answered.
    return {"rounds": rounds, "pending_tail": bool(turn or queued), "diagnostics": diagnostics,
            "tail": {"id": turn, "user": "\n\n".join(users)} if turn and users else None}
