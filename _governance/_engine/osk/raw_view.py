"""Small, deterministic reading views over immutable raw; no model summarization."""
from __future__ import annotations

import json
import re
from collections import Counter

MAX_CHARS = 6000
_ITEM_CHARS = 1200
_SECTIONS = re.compile(r"^### (user|agent|assistant)[ \t\r]*$", re.M)
_OPAQUE = {"encrypted_content", "thinking", "signature",
           "internal_chat_message_metadata_passthrough"}


def _text(value) -> str:
    """Visible text only. Transport metadata and image payloads are not prose."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(x) for x in value)))
    if not isinstance(value, dict):
        return ""
    if value.get("type") in {"thinking", "reasoning", "redacted_thinking"}:
        return ""
    if value.get("type") in {"image", "image_url", "input_image"}:
        return "[image: inspect the original evidence if relevant]"
    if value.get("type") == "attachment_ref":
        return "[attachment reference: " + str(value.get("native_result", "")) + "]"
    for key in ("text", "message", "content"):
        if key in value:
            return _text(value[key])
    return ""


def _clean(value):
    """Search tool evidence without echoing opaque fields back into the model."""
    if isinstance(value, dict):
        if value.get("type") in {"thinking", "reasoning", "redacted_thinking"}:
            return "[opaque reasoning omitted]"
        if value.get("type") in {"image", "image_url", "input_image"}:
            return "[image omitted]"
        return {k: _clean(v) for k, v in value.items() if k not in _OPAQUE}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return _clean(json.loads(value))
        except ValueError:
            pass
    return value


def _events(chunk):
    """Accept both native JSON envelopes and manually written plain raw."""
    decoder = json.JSONDecoder()
    sections = list(_SECTIONS.finditer(chunk))
    if not sections:
        yield "plain", chunk
        return
    for n, match in enumerate(sections):
        block = chunk[match.end():sections[n + 1].start() if n + 1 < len(sections) else len(chunk)]
        cursor = 0
        while cursor < len(block):
            while cursor < len(block) and block[cursor].isspace():
                cursor += 1
            if cursor == len(block):
                break
            try:
                value, end = decoder.raw_decode(block, cursor)
            except ValueError:
                end = block.find("\n\n", cursor)
                if end < 0:
                    end = len(block)
                value = block[cursor:end]
            yield match.group(1), value
            cursor = end


def project(chunk: str, max_chars: int = MAX_CHARS, query: str | None = None) -> dict:
    """Prioritize user input and final answer; search omitted evidence on demand.

    This is a selection view, never a claim of exhaustive review. A query searches
    the whole round locally; it does not page through the archive in model tokens.
    """
    budget = min(max_chars, MAX_CHARS)
    if budget < 200:
        raise ValueError("review max_chars must be at least 200")
    if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 200):
        raise ValueError("query must contain 1..200 characters")
    terms = query.split() if query else []
    entries, omitted = [], Counter()
    for number, (role, value) in enumerate(_events(chunk), 1):
        typ = value.get("type") if isinstance(value, dict) else None
        if typ in {"reasoning", "thinking", "redacted_thinking"}:
            omitted["opaque_reasoning"] += 1
            continue
        trigger = value.get("native_trigger") if isinstance(value, dict) else None
        if trigger:
            # Previous input belongs to its original turn, not a new human request.
            text = json.dumps(_clean(value), ensure_ascii=False)
            kind, priority = "native_trigger_context", 0
        elif role == "user" and typ not in {"tool_result", "function_call_output", "custom_tool_call_output"}:
            if isinstance(value, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in value):
                text, kind, priority = "", "tool_trace", 3
            else:
                text, kind, priority = _text(value), "user", 0
        elif (typ == "agent_message" and value.get("phase") == "final_answer"
              or typ == "message" and value.get("role") == "assistant"):
            final = value.get("phase") == "final_answer"
            text, kind, priority = _text(value), "assistant_final" if final else "assistant", 0 if final else 2
        elif role == "assistant" or role in {"agent", "plain"} and isinstance(value, (str, list)):
            text, kind, priority = _text(value), "assistant", 2
        elif typ in {"task_complete", "turn_aborted", "superseded"}:
            text, kind, priority = json.dumps(_clean(value), ensure_ascii=False), "terminal", 0
        elif typ == "tool_evidence_ref":
            text, kind, priority = json.dumps(value, ensure_ascii=False), "native_evidence_reference", 1
        else:
            text, kind, priority = "", "tool_trace", 3
        if terms:
            # Include structured arguments/results only for an explicit evidence query.
            text = (json.dumps(_clean(value), ensure_ascii=False)
                    if isinstance(value, (dict, list)) else str(value))
            matches = [re.search(re.escape(term), text, re.IGNORECASE) for term in terms]
            if not all(matches):
                omitted["query_nonmatch"] += 1
                continue
            start = max(0, min(match.start() for match in matches) - 180)
            text = text[start:]
        elif not text:
            omitted[kind] += 1
            continue
        blocks = value if isinstance(value, list) else [value]
        calls = [str(x.get("call_id") or x.get("tool_use_id") or x.get("id") or "")[:160]
                 for x in blocks if isinstance(x, dict) and x.get("type") in {
                     "function_call", "function_call_output", "custom_tool_call",
                     "custom_tool_call_output", "tool_use", "tool_result"}]
        entries.append({"event": number, "kind": kind, "text": text, "priority": priority,
                        "calls": [c for c in calls if c][:4]})
    # Older Codex and Claude records do not label final_answer. The last visible
    # assistant message is a useful last observation, not proof of task success.
    assistants = [x for x in entries if x["kind"] == "assistant"]
    if assistants:
        assistants[-1]["priority"] = 1
    # Prefer later observations/corrections over the start of a long execution.
    ordered = sorted(entries, key=lambda x: (0 if terms else x["priority"], -x["event"]))
    selected, used, clipped = [], 0, 0
    for entry in ordered:
        context = " · call " + ", ".join(entry["calls"]) if terms and entry["calls"] else ""
        label = f"[event {entry['event']} · {entry['kind']}{context}]\n"
        room = min(_ITEM_CHARS, budget - used - len(label) - 2)
        if room < 80:
            omitted["budget"] += 1
            continue
        text = entry["text"]
        if len(text) > room:
            # Keep both boundaries of long user/final messages. Query hits instead
            # retain the matched context at the start of the selected window.
            text = (text[:room - 1] + "…" if terms else
                    text[:room // 2 - 1] + "…\n" + text[-(room - room // 2 - 1):])
            clipped += 1
        selected.append(label + text)
        used += len(label) + len(text) + 2
    rendered = "\n\n".join(selected)
    assert len(rendered) <= budget
    return {"view": "review", "text": rendered, "chars": len(chunk),
            "returned_chars": len(rendered), "budget_chars": budget,
            "selection_only": True, "truncated": bool(clipped or omitted.get("budget")),
            "visible_events": len(entries), "selected_events": len(selected),
            "clipped_events": clipped, "omitted_events": dict(omitted),
            "query": query,
            "reading_note": "Selection of user/assistant text, not an exhaustive read or verified conclusion. "
                            "Use a specific query for supporting or contradicting evidence; do not sweep the raw. "
                            "A call is not its result: query the displayed call ID to find related output. "
                            "New dialogue raw stores tool payloads in its native evidence reference; a reference is not a verified result. "
                            "Unresolved evidence remains deferred."}
