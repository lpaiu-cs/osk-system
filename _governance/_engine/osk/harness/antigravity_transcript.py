"""osk.harness.antigravity_transcript — Antigravity 대화의 단계를 라운드로."""
from __future__ import annotations
import re
from .. import dialogue_codec


# Antigravity steps that are context, not dialogue: hook and system injections, the
# checkpoint and the compaction history. ERROR_MESSAGE is kept as an event.
_AG_CONTEXT = {"EPHEMERAL_MESSAGE", "SYSTEM_MESSAGE", "CHECKPOINT", "CONVERSATION_HISTORY"}


# The host wraps the user's words and appends its own blocks (`\n<TAG>\n…\n</TAG>`). The
# wrapper ends at the last closing tag that only host blocks follow, so a closing tag the user
# typed stays in their words.
_AG_REQUEST = re.compile(r"<USER_REQUEST>\n(.*)\n</USER_REQUEST>((?:\n<([A-Z][A-Z0-9_]*)>\n.*?\n</\3>)*)", re.S)


def _ag_user(content) -> str:
    """The user's own words, byte for byte — the input wraps them in <USER_REQUEST> and
    appends system metadata (local time, settings changes) that the user did not write. An
    input of another shape is kept whole: raw capture must not lose what the user wrote."""
    if not isinstance(content, str):
        return dialogue_codec.dump(content)
    m = _AG_REQUEST.fullmatch(content)
    return m.group(1) if m else content


def _ag_final(step) -> bool:
    """A finished reply: done, with text, and no further tool call."""
    return (step is not None and step.get("type") == "PLANNER_RESPONSE" and step.get("status") == "DONE"
            and not step.get("tool_calls") and isinstance(step.get("content"), str)
            and bool(step["content"].strip()))


def parse(rows: list, sid: str) -> dict:
    """Antigravity `transcript_full.jsonl` rows are steps `{step_index, source, type,
    status, content, tool_calls, thinking}`. A USER_INPUT opens a round; replies are
    PLANNER_RESPONSE text (thinking is internal); tool calls and the steps that answer
    them become one evidence reference. There is no turn-end row: a round ends at the
    next USER_INPUT, or at the end of the file once its last step is a finished reply
    (Stop runs after that step is written)."""
    rounds, seen = [], set()
    start, users, trace, evidence, errors = None, [], [], [], []
    last = last_line = None

    def finish(completion, terminal=None):
        rid = f"{start}:{last.get('step_index') if last else start}"
        agent = trace + dialogue_codec.tool_evidence(evidence, f"antigravity:{sid}:{start}")
        if terminal is not None:
            agent.append(dialogue_codec.dump(terminal))
        if rid not in seen:
            rounds.append({"id": rid, "user": "\n\n".join(users), "agent": "\n\n".join(agent),
                           "end_line": last_line, "completion": completion})
            seen.add(rid)

    for line, row in rows:
        typ, idx = row.get("type"), row.get("step_index")
        if typ is None or idx is None:
            raise ValueError(f"unsupported Antigravity step at line {line}")
        locator = f"antigravity:{sid}:{idx}"
        if typ == "USER_INPUT":
            if start is not None:
                if _ag_final(last):
                    finish("completed")
                else:
                    finish("failed" if errors else "interrupted",
                           {"type": "superseded", "step_index": last.get("step_index") if last else start,
                            "next_step_index": idx, **({"errors": errors} if errors else {})})
            start, users, trace, evidence, errors = idx, [_ag_user(row.get("content"))], [], [], []
            last, last_line = row, line
        elif start is None or typ in _AG_CONTEXT:
            continue
        elif typ == "ERROR_MESSAGE":
            errors.append(dialogue_codec.reference(row.get("content"), locator))
            last, last_line = row, line
        elif typ == "PLANNER_RESPONSE":
            content = row.get("content")
            if isinstance(content, str) and content.strip():
                trace.append(content)
            for n, call in enumerate(row.get("tool_calls") or []):
                if isinstance(call, dict):
                    evidence.append({"type": "tool_call_ref", "name": call.get("name"), "call_id": f"{idx}:{n}",
                                     **dialogue_codec.reference(call.get("args"), locator)})
            last, last_line = row, line
        else:
            # GENERIC, VIEW_FILE, RUN_COMMAND, MCP_TOOL, … — the steps that answer tool calls.
            evidence.append({"type": "tool_result_ref", "tool_use_id": str(idx),
                             "content": dialogue_codec.reference(row.get("content"), locator)})
            last, last_line = row, line
    if start is not None and _ag_final(last):
        finish("completed")
        start = None
    # The open round is named by its user input step, the part of its id that stays when it ends.
    return {"rounds": rounds, "pending_tail": start is not None, "diagnostics": [],
            "tail": {"id": str(start), "user": "\n\n".join(users)} if start is not None and users else None}
