"""Read native transcript completion boundaries; never guess that a tool call ended a turn.

Claude native samples split one message into rows (even end_turn thinking/text).
Codex native samples use event_msg.task_complete, with the matching turn_id.
Legacy renderers exist only to verify immutable older prefixes. New capture uses
dialogue-v1: human dialogue verbatim, operational payloads by reference (Bylaws §2).
Claude has no native record that ends an abandoned turn: after the turns an earlier
engine tracked, a turn left without a final answer for IDLE_CLOSE closes as interrupted
at its last row, and the cursor keeps that cut so every later read keeps the boundary.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

IDLE_CLOSE = 12 * 3600  # seconds of silence after which an unfinished Claude turn closes


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
        "read", "read_file", "readfile", "view_image", "read_node", "read_raw", "read_cited"}


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


def _reference(content, locator: str) -> dict:
    return {"coverage": "tool-output-reference", "native_result": locator,
            "sha256": hashlib.sha256(_structured(content).encode("utf-8")).hexdigest()}


def _dialogue_content(content, locator: str):
    """dialogue-v1 allowlist. No truncation or paraphrase of visible dialogue.

    Keep this codec stable: old raw hashes bind its output. Transport extensions
    must not silently enlarge the archive; unrecognised content is a reference.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return [{"type": "attachment_ref", **_reference(content, locator)}]
    result = []
    for item in content:
        if not isinstance(item, dict):
            result.append({"type": "attachment_ref", **_reference(item, locator)})
        elif item.get("type") in {"thinking", "reasoning", "redacted_thinking"}:
            continue
        elif item.get("type") in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
            result.append({"type": "text", "text": item["text"]})
        else:
            result.append({"type": "attachment_ref", **_reference(item, locator)})
    return result


def _dialogue_event(item: dict, locator: str) -> dict | None:
    typ = item.get("type")
    if (typ == "message" and item.get("role") == "assistant"
            and item.get("channel") != "analysis" and item.get("phase") != "analysis"):
        content = _dialogue_content(item.get("content", []), locator)
        return {"type": "message", "role": "assistant", "phase": item.get("phase"), "content": content}
    if typ in {"function_call", "custom_tool_call", "tool_use"}:
        return {"type": "tool_call_ref", "name": item.get("name"),
                "call_id": item.get("call_id", item.get("id")),
                **_reference(item.get("arguments", item.get("input")), locator)}
    if typ in {"function_call_output", "custom_tool_call_output", "tool_result"}:
        return {"type": "tool_result_ref", "tool_use_id": item.get("call_id", item.get("tool_use_id")),
                "content": _reference(item.get("output", item.get("content")), locator)}
    # Explicit allowlist: internal reasoning, summaries, runner/subagent metadata
    # and future native events are not user/assistant dialogue.
    return None


def _terminal(item: dict) -> dict:
    result = {k: item[k] for k in ("type", "turn_id", "next_turn_id") if k in item}
    if item.get("error"):
        error = item["error"]
        result["error"] = ({k: error[k] for k in ("message", "code") if k in error}
                           if isinstance(error, dict) else str(error))
    return result


def _tool_evidence(items: list, locator: str) -> list[str]:
    """One round reference, not hundreds of repeated operational envelopes.

    Hash scheme is dialogue-v1: ordered call/result reference dictionaries with
    native locations removed. Payload hashes still bind every original value.
    """
    if not items:
        return []
    manifest = []
    for item in items:
        source = {k: v for k, v in item.items() if k != "native_result"}
        if isinstance(source.get("content"), dict):
            source["content"] = {k: v for k, v in source["content"].items() if k != "native_result"}
        manifest.append(source)
    return [_dump({"type": "tool_evidence_ref", "native_result": locator,
                   "hash_scheme": "dialogue-tool-manifest-v1",
                   "sha256": hashlib.sha256(_structured(manifest).encode()).hexdigest(),
                   "calls": sum(x["type"] == "tool_call_ref" for x in items),
                   "results": sum(x["type"] == "tool_result_ref" for x in items),
                   "tools": sorted({str(x["name"]) for x in items if x.get("name")})})]


def _native_files(path: str, harness: str, sid: str) -> list[tuple[Path, int | None]]:
    """Follow declared Codex pages, never neighbouring tasks or filename order."""
    current, limit, pages = Path(path).resolve(), None, []
    allowed = {sid}
    while True:
        if any(current.samefile(p) for p, _ in pages):
            raise ValueError("cyclic Codex history_base")
        pages.append((current, limit))
        if harness != "codex":
            return pages
        with current.open("rb") as stream:
            first = next((line for line in stream if line.strip()), b"")
            row = json.loads(first)
            meta = row.get("payload") if isinstance(row, dict) else None
            if (not isinstance(meta, dict) or row.get("type") != "session_meta"
                    or not isinstance(meta.get("id"), str) or meta["id"] not in allowed):
                raise ValueError("Codex history identity mismatch: session_meta.id does not match this conversation")
            if limit is not None and limit < stream.tell():
                raise ValueError("Codex history byte boundary excludes its identity")
        base = meta.get("history_base")
        if base is None:
            return list(reversed(pages))
        # A page may continue its own conversation or the explicitly declared
        # fork parent. A history filename alone never authorizes a foreign ID.
        allowed = {meta["id"]}
        parent = meta.get("forked_from_id")
        if parent is not None:
            if not isinstance(parent, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", parent):
                raise ValueError("invalid Codex fork parent identity")
            allowed.add(parent)
        if (not isinstance(base, dict)
                or not isinstance(base.get("thread_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", base["thread_id"])
                or type(base.get("end_byte_offset")) is not int or base["end_byte_offset"] <= 0):
            raise ValueError("invalid Codex history_base identity/byte boundary")
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        pattern = f"*[-_]{base['thread_id']}.jsonl"
        matches = []
        for folder, glob in (
            (current.parent, pattern), (home / "sessions", "*/*/*/" + pattern),
            (home / "archived_sessions", pattern)):
            for p in folder.glob(glob):
                # Windows extended paths and ordinary paths can name one file.
                if p.is_file() and not any(p.samefile(other) for other in matches):
                    matches.append(p.resolve())
        if len(matches) != 1:
            raise ValueError("Codex history_base source missing or ambiguous")
        current, limit = matches.pop(), base["end_byte_offset"]


def native_fingerprint(path: str, harness: str, sid: str) -> dict:
    pages = _native_files(path, harness, sid)
    stat = pages[-1][0].stat()
    result = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if len(pages) > 1:
        result["history"] = [{"path": str(p), "through": limit,
                              "size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns}
                             for p, limit in pages[:-1]]
    return result


def _page_lines(native: Path, remaining: int | None):
    with native.open("rb") as stream:
        while remaining is None or remaining > 0:
            line = stream.readline(-1 if remaining is None else remaining)
            if not line:
                if remaining:
                    raise ValueError("Codex history byte boundary exceeds source")
                break
            if remaining is not None:
                if not line.endswith(b"\n"):
                    raise ValueError("Codex history byte boundary splits a record")
                remaining -= len(line)
            yield line


def native_lines(path: str, harness: str, sid: str):
    for native, remaining in _native_files(path, harness, sid):
        yield from _page_lines(native, remaining)


def read(path: str, harness: str, conversation_id: str, *, rules: dict | None = None) -> dict:
    """`rules` (Claude only) applies the newer turn rules after `rules["since"]`, the last
    round an earlier engine tracked: queued words join their turn, and an unfinished turn
    closes at a stored `cuts` row or after IDLE_CLOSE of silence before `now`."""
    fingerprint = native_fingerprint(path, harness, conversation_id)
    rows, diagnostics, pages = [], [], []
    n = 0
    for native, limit in _native_files(path, harness, conversation_id):
        page = []
        for line in _page_lines(native, limit):
            n += 1
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("JSON record is not an object")
            except (ValueError, UnicodeError) as exc:
                if not line.endswith(b"\n"):
                    diagnostics.append(f"incomplete JSONL tail at line {n}")
                    break
                raise ValueError(f"unreadable transcript record at line {n}") from exc
            page.append((n, row))
        rows.extend(page)
        pages.append(page)
    if harness == "claude":
        result = _claude(rows, conversation_id, rules=rules)
        readable = _claude(rows, conversation_id, dialogue=True, rules=rules)
    elif harness == "codex":
        result = _codex_history(pages, conversation_id)
        readable = _codex_history(pages, conversation_id, dialogue=True)
        # The conversation's own page names how it was started (`codex_exec` for a script).
        result["originator"] = pages[-1][0][1].get("payload", {}).get("originator")
    elif harness == "kiro":
        # Kiro rows carry no conversation ID; the folder is the conversation's own.
        if Path(path).parent.name != conversation_id:
            raise ValueError("Kiro transcript folder does not match this conversation")
        result = readable = _kiro(rows, conversation_id)
    elif harness == "antigravity":
        # Steps carry no conversation ID either: the file sits in `brain/<ID>/`.
        if conversation_id not in Path(path).parts:
            raise ValueError("Antigravity transcript folder does not match this conversation")
        result = readable = _antigravity(rows, conversation_id)
    else:
        raise ValueError("harness must be claude, codex, kiro or antigravity")
    if [r["id"] for r in readable["rounds"]] != [r["id"] for r in result["rounds"]]:
        raise ValueError("dialogue capture changed native completion boundaries")
    result["dialogue_v1"] = {r["id"]: r for r in readable["rounds"]}
    result["tail"] = readable.get("tail")
    result["diagnostics"] = diagnostics + result["diagnostics"]
    result["pending_tail"] = result["pending_tail"] or bool(diagnostics)
    result["native_fingerprint"] = fingerprint
    referenced = any('"tool_evidence_ref"' in r["agent"] for r in readable["rounds"])
    result["coverage"] = {"mode": "tool-output-reference" if referenced else "inline",
                          "capture_codec": "dialogue-v1",
                          "limitation": "User/assistant dialogue is preserved; tool payloads and attachments are native references. Internal reasoning and transport metadata are outside capture scope. Referenced evidence requires its native transcript."}
    if result.get("ancestor_pending_tails"):
        result["coverage"]["ancestor_pending_tails"] = result["ancestor_pending_tails"]
    return result


def _codex_history(pages: list, sid: str, **codec) -> dict:
    """Parse each native owner independently; child turns cannot finish a parent tail.
    A fork's parent turns carry `origin_conversation_id`: they stay the parent's."""
    groups = []
    for page in pages:
        meta = page[0][1].get("payload", {}) if page else {}
        owner = meta.get("id") if isinstance(meta, dict) else None
        if not isinstance(owner, str) or not owner or page[0][1].get("type") != "session_meta":
            raise ValueError("Codex page identity missing")
        if any(not isinstance(r.get("payload"), dict) or r["payload"].get("id") != owner
               for _, r in page if r.get("type") == "session_meta"):
            raise ValueError("Codex page contains a foreign session identity")
        if groups and groups[-1][0] == owner:
            groups[-1][1].extend(page)
        else:
            if groups and meta.get("forked_from_id") != groups[-1][0]:
                raise ValueError("Codex fork does not declare its history owner")
            groups.append((owner, page[:]))
    if not groups or groups[-1][0] != sid or len({g[0] for g in groups}) != len(groups):
        raise ValueError("Codex fork lineage does not end in this conversation")
    if len(groups) == 1:
        return _codex(groups[0][1], sid, **codec)
    rounds, diagnostics, ancestor_pending = [], [], 0
    for owner, rows in groups:
        parsed = _codex(rows, owner, **codec)
        rounds.extend({**r, "origin_conversation_id": owner} for r in parsed["rounds"])
        if owner != sid:
            ancestor_pending += bool(parsed["pending_tail"])
        else:
            diagnostics.extend(parsed["diagnostics"])
    if len({r["id"] for r in rounds}) != len(rounds):
        raise ValueError("duplicate Codex turn identity across fork owners")
    return {**parsed, "rounds": rounds, "diagnostics": diagnostics,
            "ancestor_pending_tails": ancestor_pending}


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


def _claude(rows: list, sid: str, *, dialogue: bool = False, rules: dict | None = None) -> dict:
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
    cuts = set(rules.get("cuts") or ()) if rules else set()
    now = rules.get("now") if rules else None
    delivered = {r.get("commandUuid") for _, r in rows if r.get("type") == "queue-operation"
                 and r.get("operation") == "remove" and r.get("reason") not in (None, "absorbed_mid_turn")}
    last = None       # (uuid, line, epoch) of the open turn's latest dialogue row
    resumed = None    # the round an idle close just ended; its later rows continue it
    continued = None

    def emit(rid, completion, end_line, **extra):
        nonlocal users, trace, start, native_results, evidence, new, continued, last
        locator = f"claude:{rid}"
        events = [e for _, e in evidence]
        found = {"id": rid, "user": "\n\n".join(users),
                 "agent": "\n\n".join(trace + _tool_evidence(events, locator)), "end_line": end_line,
                 "completion": completion, "native_results": native_results, **extra}
        if continued:
            found["continued_from"] = continued
        # A resumed or forked copy rewrites rows in message order, so results
        # that streamed between parallel calls move after them. Row timestamps
        # survive the copy: the original append order is offered only to match
        # a copied prefix, never to capture.
        timed = [e for _, e in sorted(evidence, key=lambda x: x[0])]
        if timed != events and all(t for t, _ in evidence):
            found["agent_time_order"] = "\n\n".join(trace + _tool_evidence(timed, locator))
        rounds.append(found)
        users, trace, start, native_results, evidence, continued, last = [], [], None, [], [], None, None
        new = new or (rules is not None and rid == since)
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
                    event = _dialogue_event(block, locator) if isinstance(block, dict) else None
                    if event and event["type"] in {"tool_call_ref", "tool_result_ref"}:
                        if start:
                            evidence.append((row.get("timestamp") or "", event))
                        continue
                    values = [event] if event is not None else _dialogue_content([block], locator)
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
                    if start and call and (_file_read(call["name"]) or _mixed_output(call["name"])):
                        native_results.append(locator)
                    block = {**block, "content": _result_content(call, block.get("content"), locator)}
                kept.append(block)
            content = kept
        if typ == "user" and not tool_result:
            if row.get("isCompactSummary") or row.get("isMeta"):
                continue
            if not start:
                start, resumed = uid, None
            users.append(_dump(content))
        elif start:
            if not dialogue or content:
                trace.append(f"### {typ}\n\n{_dump(content)}")
            if typ == "assistant" and msg.get("stop_reason") == "end_turn":
                final_message, final_line = msg.get("id"), line
                if not final_message:
                    raise ValueError(f"Claude end_turn without message id at line {line}")
                final_text = final_text or (bool(content.strip()) if isinstance(content, str) else any(
                    isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                    for b in content))
        if start:
            last = (uid, line, at)
            if uid in cuts and not final_message:
                interrupt()
    finish()
    if new and start and now is not None and last and last[2] is not None and now - last[2] >= IDLE_CLOSE:
        interrupt()
    # The open turn is not a round, but its user words may be cited (Mechanism §9 9항).
    return {"rounds": rounds, "pending_tail": bool(start), "diagnostics": diagnostics,
            "tail": {"id": start, "user": "\n\n".join(users)} if start and users else None}


def _codex(rows: list, sid: str, *, dialogue: bool = False) -> dict:
    identities = {r.get("payload", {}).get("id") for _, r in rows if r.get("type") == "session_meta"}
    if identities != {sid}:
        raise ValueError("Codex transcript session_meta.id does not match this conversation")
    rounds, diagnostics, users, trace, seen, calls = [], [], [], [], set(), {}
    turn = None
    user_formats = []
    resumable, final_seen = None, False
    evidence, has_native_trace, compaction_seen = [], False, False

    def finish(completion, line, terminal=None):
        nonlocal resumable
        inputs = users or ([_dump({"native_trigger": "inputless_after_terminal", **resumable})]
                           if resumable else [])
        if not inputs:
            return False
        if turn not in seen:
            rounds.append({"id": turn, "user": "\n\n".join(inputs),
                           "agent": "\n\n".join(trace + _tool_evidence(evidence, f"codex:{sid}:{turn}")
                                                 + ([_dump(_terminal(terminal) if dialogue else terminal)] if terminal else [])),
                           "end_line": line, "completion": completion})
            seen.add(turn)
        if completion in ("failed", "aborted", "interrupted"):
            if users:
                # Prior input is context, not proof that this is the same task.
                resumable = {"previous_input_turn": turn, "previous_input": users[:]}
        else:
            resumable = None
        return True

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
        if dialogue:
            locator = f"codex:{sid}:{turn}:user"
            content = _dialogue_content(value.get("content", value.get("message", "")), locator)
            attachments = [v for k in ("images", "local_images") for v in value.get(k, [])]
            if attachments:
                content = ([{"type": "text", "text": content}] if isinstance(content, str) else content) + [
                    {"type": "attachment_ref", **_reference(v, locator)} for v in attachments]
            rendered = _dump({"content": content})
        else:
            rendered = _dump(value)
        other = "native" if origin == "legacy" else "legacy"
        if key is not None:
            for i, (previous, previous_key) in enumerate(user_formats):
                if previous == other and previous_key == key:
                    if origin == "legacy":
                        users[i] = rendered
                    user_formats[i] = ("paired", key)
                    return
        users.append(rendered)
        user_formats.append((origin, key))

    for line, row in rows:
        typ, p = row.get("type"), row.get("payload", {})
        if not isinstance(p, dict):
            raise ValueError(f"unsupported Codex payload at line {line}")
        event = p.get("type")
        if typ == "event_msg" and event == "task_started":
            if turn and (users or (has_native_trace if dialogue else trace)):
                if not finish("interrupted", line - 1, {
                        "type": "superseded", "turn_id": turn, "next_turn_id": p.get("turn_id")}):
                    diagnostics.append(f"unfinished Codex turn {turn}")
            turn, users, trace = p.get("turn_id"), [], []
            evidence, has_native_trace, compaction_seen = [], False, False
            final_seen = False
            user_formats.clear()
        elif typ == "turn_context" and not turn:
            turn = p.get("turn_id")
        elif typ == "compacted" and turn:
            compaction_seen = True
        elif typ == "event_msg" and event == "user_message" and turn:
            add_user({k: v for k, v in p.items() if k != "type"}, "legacy")
        elif (typ == "event_msg" and event == "item_completed" and turn
              and isinstance(p.get("item"), dict) and p["item"].get("type") == "UserMessage"):
            if p.get("turn_id") != turn or p.get("thread_id") != sid:
                raise ValueError(f"Codex user item identity mismatch at line {line}")
            item = p["item"]
            if isinstance(item.get("content"), list) and item["content"]:
                add_user({k: v for k, v in item.items() if k != "type"}, "native")
        elif typ == "response_item" and turn:
            meta = p.get("internal_chat_message_metadata_passthrough") or {}
            if meta.get("turn_id") not in (None, turn):
                raise ValueError(f"Codex response identity mismatch at line {line}")
            goal = (event == "message" and p.get("role") == "user"
                    and meta.get("content_item_kinds") == ["goal.internal_context"])
            delivered = (event == "function_call_output" and not p.get("call_id")
                         and p.get("namespace") == "codex_app"
                         and p.get("name") in {"automation_update", "send_message_to_thread"}
                         and meta.get("turn_id") == turn
                         and (p["name"] != "send_message_to_thread" or not users and not resumable))
            if goal or delivered:
                # These are native execution triggers, not new human requests. A turn
                # that already had input or resumable context reads as before, so its
                # tracked hash holds; only the formerly untrackable turn is new.
                trigger = ({"content": _dialogue_content(p.get("content", p.get("output", "")),
                                                        f"codex:{sid}:{turn}:trigger")} if dialogue else {"item": p})
                kind = "goal" if goal else "heartbeat" if p["name"] == "automation_update" else "thread_message"
                users.append(_dump({"native_trigger": kind, **trigger}))
                user_formats.append(("trigger", None))
                continue
            if event == "message" and p.get("role") == "assistant" and p.get("phase") == "final_answer":
                final_seen = True  # A quiet heartbeat may intentionally finish with empty text.
            if dialogue:
                has_native_trace = has_native_trace or event != "message" or p.get("role") == "assistant"
                entry = _dialogue_event(p, f"codex:{sid}:{turn}:{p.get('call_id') or p.get('id') or 'assistant'}")
                if entry is not None:
                    if entry["type"] in {"tool_call_ref", "tool_result_ref"}:
                        evidence.append(entry)
                    else:
                        trace.append(_dump(entry))
                continue
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
            if dialogue and not trace and str(p.get("last_agent_message") or "").strip():
                trace.append(_dump({"type": "message", "role": "assistant", "phase": "final_answer",
                                    "content": p["last_agent_message"]}))
            # task_complete is authoritative; final_answer alone never commits.
            if (compaction_seen and not users and not trace and not has_native_trace
                    and not p.get("error") and not p.get("last_agent_message")):
                pass  # Native maintenance completion has no dialogue to capture.
            elif p.get("error"):
                if not finish("failed", line, p):
                    diagnostics.append(f"missing input for failed Codex turn {turn}")
            elif (users or resumable) and (has_native_trace if dialogue else trace) and (
                    str(p.get("last_agent_message") or "").strip() or final_seen):
                finish("completed", line, p if not p.get("last_agent_message") else None)
            else:
                diagnostics.append(f"incomplete content for completed Codex turn {turn}")
            turn, users, trace = None, [], []
            evidence, has_native_trace, compaction_seen = [], False, False
            user_formats.clear()
        elif typ == "event_msg" and event == "turn_aborted" and turn:
            if p.get("turn_id") and p["turn_id"] != turn:
                raise ValueError(f"Codex abort turn_id mismatch at line {line}")
            # Native abort is a terminal record, not successful work.
            if not finish("aborted", line, p) and (has_native_trace if dialogue else trace):
                diagnostics.append(f"missing input for aborted Codex turn {turn}")
            turn, users, trace = None, [], []
            evidence, has_native_trace, compaction_seen = [], False, False
            user_formats.clear()
    return {"rounds": rounds, "pending_tail": bool(users) or bool(turn) or bool(diagnostics),
            "diagnostics": diagnostics,
            "tail": {"id": turn, "user": "\n\n".join(users)} if turn and users else None}


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
        return _dump(content)
    content = _dialogue_content(content, locator)
    content = [{"type": "text", "text": content}] if isinstance(content, str) else content
    return _dump({"content": content + [{"type": "attachment_ref", **_reference(a, locator)}
                                        for a in attachments]})


def _kiro(rows: list, sid: str) -> dict:
    """Kiro `messages.jsonl` rows are `{id, timestamp, payload}`. User rows come before
    the turn they open; `turn_start`…`turn_end` share one executionId. Replies are
    `assistant` rows whose operationType is Say — Reasoning and the compaction Summary
    are internal. Tool calls, sub-agents and approvals become one evidence reference."""
    rounds, diagnostics, queued, seen = [], [], [], set()
    turn, users, trace, evidence = None, [], [], []

    def finish(completion, line, terminal=None):
        agent = trace + _tool_evidence(evidence, f"kiro:{sid}:{turn}")
        if terminal is not None:
            agent.append(_dump(terminal))
        if turn not in seen:
            rounds.append({"id": turn, "user": "\n\n".join(
                users or [_dump({"native_trigger": "inputless", "turn_id": turn})]),
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
            trace.append(content if isinstance(content, str) else _dump(_dialogue_content(content, locator)))
        elif typ in _KIRO_CALLS:
            kind, key, value = _KIRO_CALLS[typ]
            ref = _reference(p.get(value), locator)
            evidence.append({"type": kind, "name": p.get("toolName") or p.get("subAgentName"),
                             "call_id": p.get(key), **ref} if kind == "tool_call_ref" else
                            {"type": kind, "tool_use_id": p.get(key), "content": ref})
        elif typ in _KIRO_EVENTS:
            evidence.append({"type": "event_ref", "event": typ, **_reference(p, locator)})
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
        return _dump(content)
    m = _AG_REQUEST.fullmatch(content)
    return m.group(1) if m else content


def _ag_final(step) -> bool:
    """A finished reply: done, with text, and no further tool call."""
    return (step is not None and step.get("type") == "PLANNER_RESPONSE" and step.get("status") == "DONE"
            and not step.get("tool_calls") and isinstance(step.get("content"), str)
            and bool(step["content"].strip()))


def _antigravity(rows: list, sid: str) -> dict:
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
        agent = trace + _tool_evidence(evidence, f"antigravity:{sid}:{start}")
        if terminal is not None:
            agent.append(_dump(terminal))
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
            errors.append(_reference(row.get("content"), locator))
            last, last_line = row, line
        elif typ == "PLANNER_RESPONSE":
            content = row.get("content")
            if isinstance(content, str) and content.strip():
                trace.append(content)
            for n, call in enumerate(row.get("tool_calls") or []):
                if isinstance(call, dict):
                    evidence.append({"type": "tool_call_ref", "name": call.get("name"), "call_id": f"{idx}:{n}",
                                     **_reference(call.get("args"), locator)})
            last, last_line = row, line
        else:
            # GENERIC, VIEW_FILE, RUN_COMMAND, MCP_TOOL, … — the steps that answer tool calls.
            evidence.append({"type": "tool_result_ref", "tool_use_id": str(idx),
                             "content": _reference(row.get("content"), locator)})
            last, last_line = row, line
    if start is not None and _ag_final(last):
        finish("completed")
        start = None
    # The open round is named by its user input step, the part of its id that stays when it ends.
    return {"rounds": rounds, "pending_tail": start is not None, "diagnostics": [],
            "tail": {"id": str(start), "user": "\n\n".join(users)} if start is not None and users else None}
