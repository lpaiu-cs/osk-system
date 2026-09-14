"""Read native transcript completion boundaries; never guess that a tool call ended a turn.

Claude native samples split one message into rows (even end_turn thinking/text).
Codex native samples use event_msg.task_complete, with the matching turn_id.
Conversation content, including tool calls/results, stays in the captured round.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _structured(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _redact(text: str) -> str:
    """Filter decoded string literals without rebuilding JSON objects or keys."""
    from . import secrets
    decoder = json.JSONDecoder()
    parts, last, cursor = [], 0, 0
    while (cursor := text.find('"', cursor)) >= 0:
        try:
            value, end = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError:
            cursor += 1
            continue
        filtered = _redact(value)
        if filtered != value:
            parts.extend((text[last:cursor], json.dumps(filtered)))
            last = end
        cursor = end
    # Keep every unchanged span, including duplicate keys, whitespace and escapes.
    return secrets.filter_text("".join(parts) + text[last:])[0]


def _dump(value) -> str:
    # Decode string boundaries for filtering; raw.write_raw still filters last.
    return _redact(_structured(value))


def _file_read(name: str) -> bool:
    return name.lower().split("__")[-1].split(".")[-1] in {
        "read", "read_file", "readfile", "view_image", "read_node", "read_raw"}


def _mixed_output(name: str) -> bool:
    return name.lower().split("__")[-1].split(".")[-1] in {
        "exec", "exec_command", "bash", "shell", "powershell", "run_command", "read_thread_terminal"}


def _result_content(call: dict | None, content, locator: str):
    if not call or not (_file_read(call["name"]) or _mixed_output(call["name"])):
        return content
    return {"omitted": "read file content (Bylaws 2.2)" if _file_read(call["name"]) else
            "mixed command output; may contain full file content",
            "coverage": "tool-output-reference", "source": call["input"],
            "native_result": locator,
            # A reference hashes the original native result, not its redacted display.
            "sha256": hashlib.sha256(_structured(content).encode("utf-8")).hexdigest()}


def read(path: str, harness: str, conversation_id: str) -> dict:
    stat = Path(path).stat()
    data = Path(path).read_bytes()
    rows, diagnostics = [], []
    lines = data.splitlines(keepends=True)
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("JSON record is not an object")
        except (ValueError, UnicodeError) as exc:
            if n == len(lines) and not line.endswith(b"\n"):
                diagnostics.append(f"incomplete JSONL tail at line {n}")
                break
            raise ValueError(f"unreadable transcript record at line {n}") from exc
        rows.append((n, row))
    if harness == "claude":
        result = _claude(rows, conversation_id)
    elif harness == "codex":
        result = _codex(rows, conversation_id)
        # Unmarked durable rounds used v3.14's event-only serialization. Keep
        # that exact replay (including trace gating) separate from new capture.
        result["codex_v1"] = {r["id"]: r for r in _codex(
            rows, conversation_id, native_users=False)["rounds"]}
    else:
        raise ValueError("harness must be claude or codex")
    result["diagnostics"] = diagnostics + result["diagnostics"]
    result["pending_tail"] = result["pending_tail"] or bool(diagnostics)
    result["native_fingerprint"] = {"size": len(data), "mtime_ns": stat.st_mtime_ns}
    referenced = any('"coverage": "tool-output-reference"' in r["agent"] for r in result["rounds"])
    result["coverage"] = {"mode": "tool-output-reference" if referenced else "inline",
                          "limitation": "Referenced file/command results require retention of the native transcript for dereference; tool calls and user/assistant dialogue are preserved." if referenced else None}
    return result


def _claude(rows: list, sid: str) -> dict:
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
    start, final_message, final_line, final_text = None, None, None, False
    seen = set()

    def finish():
        nonlocal users, trace, start, final_message, final_line, final_text
        if start and final_message and final_text:
            rounds.append({"id": f"{start}:{final_message}", "user": "\n\n".join(users),
                           "agent": "\n\n".join(trace), "end_line": final_line,
                           "completion": "completed"})
            users, trace, start = [], [], None
        final_message, final_line, final_text = None, None, False

    for line, row in rows:
        if row.get("isSidechain") or row.get("agentId"):
            continue
        typ, msg = row.get("type"), row.get("message", {})
        if not isinstance(msg, dict):
            raise ValueError(f"unsupported Claude message at line {line}")
        # All rows of an end_turn message belong to its response, not just the
        # first row, whose content may be thinking and lack the final answer.
        if final_message and not (typ == "assistant" and msg.get("id") == final_message):
            finish()
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
        if isinstance(content, list):
            kept = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls[block.get("id")] = {"name": block.get("name", ""), "input": block.get("input")}
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    block = {**block, "content": _result_content(calls.get(block.get("tool_use_id")),
                             block.get("content"), f"claude:{row.get('sessionId', sid)}:{uid}")}
                kept.append(block)
            content = kept
        if typ == "user" and not tool_result:
            if row.get("isCompactSummary") or row.get("isMeta"):
                continue
            if not start:
                start = uid
            users.append(_dump(content))
        elif start:
            trace.append(f"### {typ}\n\n{_dump(content)}")
            if typ == "assistant" and msg.get("stop_reason") == "end_turn":
                final_message, final_line = msg.get("id"), line
                if not final_message:
                    raise ValueError(f"Claude end_turn without message id at line {line}")
                final_text = final_text or (bool(content.strip()) if isinstance(content, str) else any(
                    isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                    for b in content))
    finish()
    return {"rounds": rounds, "pending_tail": bool(start), "diagnostics": diagnostics}


def _codex(rows: list, sid: str, *, native_users: bool = True) -> dict:
    identities = {r.get("payload", {}).get("id") for _, r in rows if r.get("type") == "session_meta"}
    if identities != {sid}:
        raise ValueError("Codex transcript session_meta.id does not match this conversation")
    rounds, diagnostics, users, trace, seen, calls = [], [], [], [], set(), {}
    turn = None
    user_formats = []

    def add_user(value, origin):
        # Some harness versions emit both envelopes. Pair only plain-text
        # mirrors, one for one; preserve the legacy bytes for captured prefixes.
        # Rich or unrecognized content stays intact rather than being guessed away.
        key = None
        if origin == "legacy":
            if isinstance(value.get("message"), str) and not any(
                    v for k, v in value.items() if k != "message"):
                key = value["message"]
        else:
            content = value["content"]
            if (set(value) <= {"id", "content"} and len(content) == 1
                    and isinstance(content[0], dict) and content[0].get("type") == "text"
                    and isinstance(content[0].get("text"), str)
                    and not any(v for k, v in content[0].items() if k not in {"type", "text"})):
                key = content[0]["text"]
        other = "native" if origin == "legacy" else "legacy"
        if key is not None:
            for i, (previous, previous_key) in enumerate(user_formats):
                if previous == other and previous_key == key:
                    if origin == "legacy":
                        users[i] = _dump(value)
                    user_formats[i] = ("paired", key)
                    return
        users.append(_dump(value))
        user_formats.append((origin, key))

    for line, row in rows:
        typ, p = row.get("type"), row.get("payload", {})
        if not isinstance(p, dict):
            raise ValueError(f"unsupported Codex payload at line {line}")
        event = p.get("type")
        if typ == "event_msg" and event == "task_started":
            if turn and users:
                diagnostics.append(f"unfinished Codex turn {turn}")
            turn, users, trace = p.get("turn_id"), [], []
            user_formats.clear()
        elif typ == "turn_context" and not turn:
            turn = p.get("turn_id")
        elif typ == "event_msg" and event == "user_message" and turn:
            add_user({k: v for k, v in p.items() if k != "type"}, "legacy")
        elif (native_users and typ == "event_msg" and event == "item_completed" and turn
              and isinstance(p.get("item"), dict) and p["item"].get("type") == "UserMessage"):
            if p.get("turn_id") != turn or p.get("thread_id") != sid:
                raise ValueError(f"Codex user item identity mismatch at line {line}")
            item = p["item"]
            if isinstance(item.get("content"), list) and item["content"]:
                add_user({k: v for k, v in item.items() if k != "type"}, "native")
        elif typ == "response_item" and turn and users:
            # event_msg user/agent text mirrors response_item. Keep native tool
            # items and assistant responses once; never mistake tool output for
            # the human's next prompt. No truncation of content or tool results.
            if event in ("function_call", "custom_tool_call"):
                arguments = p.get("arguments", p.get("input"))
                try:
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                except ValueError:
                    pass
                calls[p.get("call_id")] = {"name": p.get("name", ""), "input": arguments}
            elif event in ("function_call_output", "custom_tool_call_output"):
                p = {**p, "output": _result_content(calls.get(p.get("call_id")), p.get("output"),
                                                    f"codex:{sid}:{turn}:{p.get('call_id')}")}
            if event != "message" or p.get("role") == "assistant":
                trace.append(_dump(p))
        elif typ == "event_msg" and event == "task_complete":
            if not turn or p.get("turn_id") != turn:
                raise ValueError(f"Codex completion turn_id mismatch at line {line}")
            # task_complete is authoritative; final_answer alone never commits.
            if not users or not trace or not str(p.get("last_agent_message") or "").strip():
                diagnostics.append(f"incomplete content for completed Codex turn {turn}")
            elif turn not in seen:
                rounds.append({"id": turn, "user": "\n\n".join(users),
                               "agent": "\n\n".join(trace), "end_line": line,
                               "completion": "completed"})
                seen.add(turn)
            turn, users, trace = None, [], []
            user_formats.clear()
        elif typ == "event_msg" and event == "turn_aborted" and turn:
            if p.get("turn_id") and p["turn_id"] != turn:
                raise ValueError(f"Codex abort turn_id mismatch at line {line}")
            if users and turn not in seen:
                # Native abort is a terminal record, not successful work. Keep
                # its partial observations separate from the next user task.
                rounds.append({"id": turn, "user": "\n\n".join(users),
                               "agent": "\n\n".join(trace + [_dump(p)]),
                               "end_line": line, "completion": "aborted"})
                seen.add(turn)
            turn, users, trace = None, [], []
            user_formats.clear()
    return {"rounds": rounds, "pending_tail": bool(users) or bool(diagnostics),
            "diagnostics": diagnostics}
