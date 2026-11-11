"""osk.harness.codex_transcript — Codex rollout의 행을 라운드로.

Codex는 턴의 끝을 `event_msg.task_complete`와 그 `turn_id`로 적는다. 이어 받은 대화는 앞 rollout들의 쪽을
함께 읽는다(`history`)."""
from __future__ import annotations
import json
from .. import dialogue_codec


def history(pages: list, sid: str, **codec) -> dict:
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
        return parse(groups[0][1], sid, **codec)
    rounds, diagnostics, ancestor_pending = [], [], 0
    for owner, rows in groups:
        parsed = parse(rows, owner, **codec)
        rounds.extend({**r, "origin_conversation_id": owner} for r in parsed["rounds"])
        if owner != sid:
            ancestor_pending += bool(parsed["pending_tail"])
        else:
            diagnostics.extend(parsed["diagnostics"])
    if len({r["id"] for r in rounds}) != len(rounds):
        raise ValueError("duplicate Codex turn identity across fork owners")
    return {**parsed, "rounds": rounds, "diagnostics": diagnostics,
            "ancestor_pending_tails": ancestor_pending}


def parse(rows: list, sid: str, *, dialogue: bool = False) -> dict:
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
        inputs = users or ([dialogue_codec.dump({"native_trigger": "inputless_after_terminal", **resumable})]
                           if resumable else [])
        if not inputs:
            return False
        if turn not in seen:
            rounds.append({"id": turn, "user": "\n\n".join(inputs),
                           "agent": "\n\n".join(trace + dialogue_codec.tool_evidence(evidence, f"codex:{sid}:{turn}")
                                                 + ([dialogue_codec.dump(dialogue_codec.terminal(terminal) if dialogue else terminal)] if terminal else [])),
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
            content = dialogue_codec.dialogue_content(value.get("content", value.get("message", "")), locator)
            attachments = [v for k in ("images", "local_images") for v in value.get(k, [])]
            if attachments:
                content = ([{"type": "text", "text": content}] if isinstance(content, str) else content) + [
                    {"type": "attachment_ref", **dialogue_codec.reference(v, locator)} for v in attachments]
            rendered = dialogue_codec.dump({"content": content})
        else:
            rendered = dialogue_codec.dump(value)
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
                trigger = ({"content": dialogue_codec.dialogue_content(p.get("content", p.get("output", "")),
                                                        f"codex:{sid}:{turn}:trigger")} if dialogue else {"item": p})
                kind = "goal" if goal else "heartbeat" if p["name"] == "automation_update" else "thread_message"
                users.append(dialogue_codec.dump({"native_trigger": kind, **trigger}))
                user_formats.append(("trigger", None))
                continue
            if event == "message" and p.get("role") == "assistant" and p.get("phase") == "final_answer":
                final_seen = True  # A quiet heartbeat may intentionally finish with empty text.
            if dialogue:
                has_native_trace = has_native_trace or event != "message" or p.get("role") == "assistant"
                entry = dialogue_codec.dialogue_event(p, f"codex:{sid}:{turn}:{p.get('call_id') or p.get('id') or 'assistant'}")
                if entry is not None:
                    if entry["type"] in {"tool_call_ref", "tool_result_ref"}:
                        evidence.append(entry)
                    else:
                        trace.append(dialogue_codec.dump(entry))
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
                p = {**p, "output": dialogue_codec.result_content(calls.get(p.get("call_id")), p.get("output"),
                                                    f"codex:{sid}:{turn}:{p.get('call_id')}")}
            if event != "message" or p.get("role") == "assistant":
                trace.append(dialogue_codec.dump(p))
        elif typ == "event_msg" and event == "task_complete":
            if not turn or p.get("turn_id") != turn:
                raise ValueError(f"Codex completion turn_id mismatch at line {line}")
            if dialogue and not trace and str(p.get("last_agent_message") or "").strip():
                trace.append(dialogue_codec.dump({"type": "message", "role": "assistant", "phase": "final_answer",
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
