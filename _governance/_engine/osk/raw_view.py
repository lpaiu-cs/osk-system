"""Small, deterministic reading views over immutable raw; no model summarization."""
from __future__ import annotations

import json
import re
from collections import Counter

MAX_CHARS = 6000
_ITEM_CHARS = 1200
_SECTIONS = re.compile(r"^### (user|agent|assistant)[ \t\r]*$", re.M)
_JSON_END = re.compile(r"(?:[ \t\r]*\n[ \t\r]*\n|\s*\Z)")
_NATIVE_TYPES = {"text", "input_text", "output_text", "message", "agent_message",
                 "thinking", "reasoning", "redacted_thinking", "function_call",
                 "function_call_output", "custom_tool_call", "custom_tool_call_output",
                 "tool_use", "tool_result", "tool_evidence_ref", "attachment_ref",
                 "image", "image_url", "input_image", "task_complete", "turn_aborted", "superseded"}
_OPAQUE = {"encrypted_content", "thinking", "signature",
           "internal_chat_message_metadata_passthrough"}


def visible_text(value) -> str:
    """Visible text only. Transport metadata and image payloads are not prose."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (visible_text(x) for x in value)))
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
            return visible_text(value[key])
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


def _native_event(value) -> bool:
    """Only capture envelope shapes, never a JSON scalar or arbitrary data object."""
    if isinstance(value, list):
        return all(isinstance(item, dict) and _native_event(item) for item in value)
    if not isinstance(value, dict):
        return False
    if "type" in value:
        return isinstance(value["type"], str) and value["type"] in _NATIVE_TYPES
    return (isinstance(value.get("native_trigger"), str)
            or isinstance(value.get("content"), (str, list))
            or isinstance(value.get("message"), str))


def parse_events(chunk):
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
                if block[cursor] not in "[{":
                    raise ValueError("plain paragraph")
                value, end = decoder.raw_decode(block, cursor)
                if not _native_event(value) or not _JSON_END.match(block, end):
                    raise ValueError("not a complete capture envelope")
            except ValueError:
                end = block.find("\n\n", cursor)
                if end < 0:
                    end = len(block)
                value = block[cursor:end]
            yield match.group(1), value
            cursor = end


_TOOL_OUTPUTS = {"tool_result", "function_call_output", "custom_tool_call_output"}
_CALLS = {"function_call", "custom_tool_call", "tool_use"} | _TOOL_OUTPUTS
# 2026-10-06 사용자 결정 — 원래 세션의 보고를 다시 감사하지 않는다. 처음 문안("a reference
# is not a verified result. Unresolved evidence remains deferred.")은 지어내지 말라는
# 뜻이었으나 도구 참조를 미해결 근거로 읽혀 보류를 불렀다.
_READING_NOTE = (
    "Selection of user/assistant text, not an exhaustive read. "
    "Use a specific query for supporting or contradicting evidence; do not sweep the record or the conversation. "
    "A call is not its result: query the displayed call ID to find related output. "
    "Tool payloads stay as native references. The session acted in good faith: what its "
    "dialogue states about a result is its report, so use it as stated and do not invent "
    "what it does not state."
)


def _budgets(max_chars: int, sides: tuple[int, int] | None) -> tuple[dict, int]:
    """`(쪽마다의 예산, 전체 예산)`. `sides`가 없으면 두 쪽이 한 예산을 같이 쓴다."""
    if sides is not None:
        if len(sides) != 2 or not all(isinstance(n, int) and 200 <= n <= MAX_CHARS for n in sides):
            raise ValueError(f"review sides must be two budgets of 200..{MAX_CHARS} characters")
        return {"user": sides[0], "agent": sides[1]}, sum(sides)
    budget = min(max_chars, MAX_CHARS)
    if budget < 200:
        raise ValueError("review max_chars must be at least 200")
    return {"user": budget, "agent": budget}, budget


def _user_reading(value) -> tuple[str, str, int]:
    if isinstance(value, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in value):
        return "", "tool_trace", 3
    return visible_text(value), "user", 0


def _reading(role: str, value, typ) -> tuple[str, str, int]:
    """사건 하나를 무엇으로 읽는가 — `(글, 종류, 우선순위)`. 우선순위는 작을수록 먼저 고른다. 글이 비면 싣지 않는다."""
    if isinstance(value, dict) and value.get("native_trigger"):
        # Previous input belongs to its original turn, not a new human request.
        return json.dumps(_clean(value), ensure_ascii=False), "native_trigger_context", 0
    if typ == "tool_evidence_ref":
        return json.dumps(value, ensure_ascii=False), "native_evidence_reference", 1
    if typ in {"task_complete", "turn_aborted", "superseded"}:
        return json.dumps(_clean(value), ensure_ascii=False), "terminal", 0
    if role == "user" and typ not in _TOOL_OUTPUTS:
        return _user_reading(value)
    if (typ == "agent_message" and value.get("phase") == "final_answer"
            or typ == "message" and value.get("role") == "assistant"):
        final = value.get("phase") == "final_answer"
        return visible_text(value), "assistant_final" if final else "assistant", 0 if final else 2
    if role == "assistant" or role in {"agent", "plain"} and isinstance(value, (str, list)):
        return visible_text(value), "assistant", 2
    return "", "tool_trace", 3


def _matched(value, terms: list[str]) -> str | None:
    """질의에 맞은 사건의 글 — 가장 이른 맞은 자리의 180자 앞에서 시작한다. 낱말이 다 맞지 않으면 None.
    Include structured arguments/results only for an explicit evidence query."""
    text = (json.dumps(_clean(value), ensure_ascii=False)
            if isinstance(value, (dict, list)) else str(value))
    matches = [re.search(re.escape(term), text, re.IGNORECASE) for term in terms]
    if not all(matches):
        return None
    return text[max(0, min(match.start() for match in matches) - 180):]


def _call_ids(value) -> list[str]:
    blocks = value if isinstance(value, list) else [value]
    calls = [str(x.get("call_id") or x.get("tool_use_id") or x.get("id") or "")[:160]
             for x in blocks if isinstance(x, dict) and x.get("type") in _CALLS]
    return [c for c in calls if c][:4]


def _entries(chunk: str, terms: list[str]) -> tuple[list[dict], Counter]:
    """보일 수 있는 사건들과, 싣지 않은 것의 종류별 수."""
    entries, omitted = [], Counter()
    for number, (role, value) in enumerate(parse_events(chunk), 1):
        typ = value.get("type") if isinstance(value, dict) else None
        if typ in {"reasoning", "thinking", "redacted_thinking"}:
            omitted["opaque_reasoning"] += 1
            continue
        text, kind, priority = _reading(role, value, typ)
        if terms:
            text = _matched(value, terms)
            if text is None:
                omitted["query_nonmatch"] += 1
                continue
        elif not text:
            omitted[kind] += 1
            continue
        entries.append({"event": number, "kind": kind, "text": text, "priority": priority,
                        "calls": _call_ids(value)})
    # Older Codex and Claude records do not label final_answer. The last visible
    # assistant message is a useful last observation, not proof of task success.
    assistants = [x for x in entries if x["kind"] == "assistant"]
    if assistants:
        assistants[-1]["priority"] = 1
    return entries, omitted


def _fitted(entries: list[dict], terms: list[str], budgets: dict, split: bool, omitted: Counter) -> tuple[list, int]:
    """예산에 드는 만큼 고른다 — `(고른 글들, 잘라 낸 수)`. 예산에 밀린 것은 `omitted["budget"]`에 센다."""
    # Prefer later observations/corrections over the start of a long execution.
    ordered = sorted(entries, key=lambda x: (0 if terms else x["priority"], -x["event"]))
    selected, used, clipped = [], {"user": 0, "agent": 0}, 0
    for entry in ordered:
        context = " · call " + ", ".join(entry["calls"]) if terms and entry["calls"] else ""
        label = f"[event {entry['event']} · {entry['kind']}{context}]\n"
        # One shared pool unless split: then the user's words and the response each have their own.
        side = ("user" if entry["kind"] in {"user", "native_trigger_context"} else "agent") if split else "user"
        room = min(budgets[side] if split else _ITEM_CHARS, budgets[side] - used[side] - len(label) - 2)
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
        used[side] += len(label) + len(text) + 2
    return selected, clipped


def project(chunk: str, max_chars: int = MAX_CHARS, query: str | None = None,
            *, sides: tuple[int, int] | None = None) -> dict:
    """Prioritize user input and final answer; search omitted evidence on demand.

    This is a selection view, never a claim of exhaustive review. A query searches
    the whole round locally; it does not page through the archive in model tokens.
    `sides=(user, agent)` gives the user's words and the response their own budgets
    (one item may fill its side) in place of `max_chars`; neither lends to the other.
    """
    budgets, budget = _budgets(max_chars, sides)
    if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 200):
        raise ValueError("query must contain 1..200 characters")
    terms = query.split() if query else []
    entries, omitted = _entries(chunk, terms)
    selected, clipped = _fitted(entries, terms, budgets, bool(sides), omitted)
    rendered = "\n\n".join(selected)
    assert len(rendered) <= budget
    return {"view": "review", "text": rendered, "chars": len(chunk),
            "returned_chars": len(rendered), "budget_chars": budget,
            **({"budget_sides": budgets} if sides else {}),
            "selection_only": True, "truncated": bool(clipped or omitted.get("budget")),
            "visible_events": len(entries), "selected_events": len(selected),
            "clipped_events": clipped, "omitted_events": dict(omitted),
            "query": query, "reading_note": _READING_NOTE}
