"""Read native transcript completion boundaries; never guess that a tool call ended a turn.

Claude native samples split one message into rows (even end_turn thinking/text).
Codex native samples use event_msg.task_complete, with the matching turn_id.
Legacy renderers exist only to verify immutable older prefixes. New capture uses
dialogue-v1: human dialogue verbatim, operational payloads by reference (Bylaws §2).
Claude has no native record that ends an abandoned turn: after the turns an earlier
engine tracked, a turn left without a final answer for IDLE_CLOSE closes as interrupted
at its last row, and the cursor keeps that cut so every later read keeps the boundary.
Codex compresses a rollout left untouched for 7 days to `<name>.zst` in place, keeping its
mtime and declaring the plain size, and restores the plain file on resume (#203). Every
byte boundary, cursor and fingerprint names the plain bytes, whichever form is on disk.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path
from . import secrets

IDLE_CLOSE = 12 * 3600  # seconds of silence after which an unfinished Claude turn closes


class ZstdMissing(OSError):
    """This device's environment cannot decompress a Codex rollout that is there (#203)."""


def _zstd():
    try:
        import zstandard
    except ImportError as exc:
        raise ZstdMissing("Codex compressed rollout (.jsonl.zst) needs the zstandard package; "
                          "reinstall _governance/_engine/requirements.txt into this vault's .venv") from exc
    return zstandard


def on_disk(path) -> Path | None:
    """The rollout at `path` in the form on disk now, the plain file while both exist."""
    p = Path(path)
    plain = p.with_suffix("") if p.suffix == ".zst" else p
    return next((f for f in (plain, plain.with_name(plain.name + ".zst")) if f.exists()), None)


def rollouts(folder: Path, pattern: str) -> list[Path]:
    """Files matching the `.jsonl` glob `pattern`, plain or compressed, one per rollout."""
    return list(folder.glob(pattern)) + [
        p for p in folder.glob(pattern + ".zst") if not p.with_suffix("").exists()]


def open_native(path):
    """A native transcript's plain bytes as a binary stream."""
    p = on_disk(path) or Path(path)
    if p.suffix != ".zst":
        return p.open("rb")
    zstandard = _zstd()
    return io.BufferedReader(zstandard.ZstdDecompressor().stream_reader(p.open("rb"), read_across_frames=True))


def _stat(p: Path) -> dict:
    """Size in plain bytes and mtime: a compressed rollout keeps both, so it is no new turn."""
    if p.suffix != ".zst":
        stat = p.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    zstandard = _zstd()
    with p.open("rb") as f:
        try:
            size = zstandard.frame_content_size(f.read(18))
        except zstandard.ZstdError as exc:
            raise ValueError(f"unreadable Codex compressed rollout: {exc}") from exc
    if size < 0:  # Codex always declares it; count the bytes of a file that does not.
        with open_native(p) as stream:
            size = sum(map(len, iter(lambda: stream.read(1 << 20), b"")))
    return {"size": size, "mtime_ns": p.stat().st_mtime_ns}


def _structured(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _redact(text: str) -> str:
    """Filter decoded string literals without rebuilding JSON objects or keys."""
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
    current, limit, pages = (on_disk(path) or Path(path)).resolve(), None, []
    allowed = {sid}
    while True:
        if any(current.samefile(p) for p, _ in pages):
            raise ValueError("cyclic Codex history_base")
        pages.append((current, limit))
        if harness != "codex":
            return pages
        with open_native(current) as stream:
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
            for p in rollouts(folder, glob):
                # Windows extended paths and ordinary paths can name one file.
                if p.is_file() and not any(p.samefile(other) for other in matches):
                    matches.append(p.resolve())
        if len(matches) != 1:
            raise ValueError("Codex history_base source missing or ambiguous")
        current, limit = matches.pop(), base["end_byte_offset"]


def workspace(path: str, harness: str) -> dict | None:
    """이 대화의 작업 폴더와 하네스 판본 — 일회성 작업 폴더의 판별에만 쓴다(Mechanism §6-2 6항).
    앞쪽 행만 읽는다. Claude는 행마다, Codex는 첫 `session_meta`가 담는다. 모르면 None이다."""
    try:
        with open_native(path) as f:
            for n, line in enumerate(f):
                if n >= 50:
                    break
                row = json.loads(line)
                if harness == "claude" and isinstance(row, dict) and isinstance(row.get("cwd"), str):
                    return {"cwd": row["cwd"], "version": row.get("version")}
                if harness == "codex" and isinstance(row, dict) and row.get("type") == "session_meta":
                    meta = row.get("payload") or {}
                    return {"cwd": meta.get("cwd"), "version": meta.get("cli_version")}
    except (OSError, ValueError):
        pass
    return None


def native_fingerprint(path: str, harness: str, sid: str) -> dict:
    pages = _native_files(path, harness, sid)
    result = _stat(pages[-1][0])
    if len(pages) > 1:
        # A page is named in its plain form: compressing it is no change.
        result["history"] = [{"path": str(p.with_suffix("") if p.suffix == ".zst" else p),
                              "through": limit, **_stat(p)} for p, limit in pages[:-1]]
    return result


def _page_lines(native: Path, remaining: int | None):
    with open_native(native) as stream:
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


def reverted(path: str, harness: str, sid: str) -> list[str]:
    """Turns Codex `thread/revert` took out of this conversation (#208). The revert leaves
    the old page as it was and starts a new one that keeps it only up to a byte bound: a
    turn the conversation's own earlier page started past that bound is reverted. A fork
    parent's later turns are the parent's, not reverted."""
    turns = []
    for page, limit in _native_files(path, harness, sid)[:-1]:
        offset, owner = 0, None
        for line in _page_lines(page, None):
            start, offset = offset, offset + len(line)
            if owner is None and line.strip():
                owner = json.loads(line).get("payload", {}).get("id")
                if owner != sid:
                    break
            elif start >= limit:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # An unreadable tail row can only refuse more, never accept.
                p = row.get("payload") if isinstance(row, dict) else None
                if (row.get("type") == "event_msg" and isinstance(p, dict)
                        and p.get("type") == "task_started" and p.get("turn_id")):
                    turns.append(p["turn_id"])
    return turns


def native_lines(path: str, harness: str, sid: str):
    for native, remaining in _native_files(path, harness, sid):
        yield from _page_lines(native, remaining)


# How many following lines a complete line that does not parse may take in (#243).
REJOIN_LINES = 16
LINE_SEPARATOR = " ".encode()


def _rejoin(line: bytes, rest) -> tuple[dict | None, int]:
    """Claude Code once wrote a record with raw newlines inside a string, splitting it over
    lines (#243): copying a parent's history it wrote each U+2028 in a string as a line end
    (#252). Join the lines after `line` until the whole reads as exactly one object — first
    with U+2028 back at each break, so a copy reads as its parent does, then with the raw
    newlines kept, control characters allowed in strings. `(None, _)` when none does."""
    pieces = [line]
    for extra in range(1, REJOIN_LINES + 1):
        more = next(rest, None)
        if more is None:
            break
        pieces.append(more)
        separated = b"".join(p[:-1] + LINE_SEPARATOR for p in pieces[:-1]) + more
        for joined in (separated, b"".join(pieces)):
            try:
                row = json.loads(joined, strict=False)
            except (ValueError, UnicodeError):
                continue
            return (row if isinstance(row, dict) else None), extra
    return None, 0


def read(path: str, harness: str, conversation_id: str, *, rules: dict | None = None) -> dict:
    """`rules` (Claude only) applies the newer turn rules after `rules["since"]`, the last
    round an earlier engine tracked: queued words join their turn, and an unfinished turn
    closes at a stored `cuts` row or after IDLE_CLOSE of silence before `now`. After
    `rules["prompts_since"]` a prompt with another `promptId` closes the open turn (#218).
    After `rules["harness_since"]` a notice the harness sent as a prompt is not the user's
    turn (#275), and a branch a rewind dropped is not read (#235): `reverted` names the
    rounds the dropped rows made."""
    fingerprint = native_fingerprint(path, harness, conversation_id)
    rows, diagnostics, pages, rejoined = [], [], [], []
    n = 0
    for native, limit in _native_files(path, harness, conversation_id):
        page = []
        lines = _page_lines(native, limit)
        for line in lines:
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
                row, extra = _rejoin(line, lines)
                if row is None:
                    raise ValueError(f"unreadable transcript record at line {n}") from exc
                rejoined.append([n, n + extra])
                n += extra
            page.append((n, row))
        rows.extend(page)
        pages.append(page)
    if harness == "claude":
        result = _claude(rows, conversation_id, rules=rules)
        readable = _claude(rows, conversation_id, dialogue=True, rules=rules)
        if result.pop("rewound", False):
            kept = {r["id"] for r in result["rounds"]}
            result["reverted"] = [r["id"] for r in _claude(rows, conversation_id, rules=rules, rewinds=False)["rounds"]
                                  if r["id"] not in kept]
    elif harness == "codex":
        result = _codex_history(pages, conversation_id)
        readable = _codex_history(pages, conversation_id, dialogue=True)
        # The conversation's own page names how it was started (`codex_exec` for a script).
        result["originator"] = pages[-1][0][1].get("payload", {}).get("originator")
        result["reverted"] = reverted(path, harness, conversation_id)
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
    if rejoined:
        result["coverage"]["rejoined_lines"] = rejoined
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


def _own_history(rows: list, sid: str) -> bool:
    """이 파일이 이 대화의 것인가. 부모의 과거와 자식의 UI 기록만 든 갓 갈라진 파일이면 False이고,
    이 대화로 이어지지 않는 다른 대화의 행이 섞였으면 ValueError다."""
    identities = {r["sessionId"] for _, r in rows if r.get("sessionId")}
    if identities == {sid}:
        return True
    own = next((i for i, (_, r) in enumerate(rows) if r.get("sessionId") == sid and r.get("uuid")), None)
    if sid in identities and own is None:
        return False
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
    return True


def _has_tool_result(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


class _ClaudeTurns:
    """Claude 전사의 행을 차례로 받아 턴을 라운드로 닫는다. 열린 턴에 대해 아는 것이 여기 있다.

    턴은 사용자의 말(`prompted`)로 열리고 응답(`reply`)으로 이어지며, 세 가지로 닫힌다: 최종 응답
    (`finish`), 끊김(`interrupt` — 저장된 자리나 오랜 침묵), 다음 사용자의 말."""

    def __init__(self, rows: list, sid: str, dialogue: bool, rules: dict | None, rewinds: bool, injected_only):
        self.sid, self.dialogue, self.rules, self.injected_only = sid, dialogue, rules, injected_only
        self.rounds, self.diagnostics, self.users, self.trace, self.calls = [], [], [], [], {}
        self.native_results = []
        self.evidence = []  # (native row timestamp, event) in file order
        self.start, self.final_message, self.final_line, self.final_text = None, None, None, False
        self.seen = set()
        # The newer rules apply after `since`; a stored cut closes its turn on every read.
        self.since = rules.get("since") if rules else None
        self.new = rules is not None and self.since is None
        # A new prompt closes the open turn after `prompts_since`, the last round an engine without
        # that rule tracked (#218): a turn that ends in a tool call has no end_turn to close it.
        self.splits = rules is not None and "prompts_since" in rules
        self.split = self.splits and rules["prompts_since"] is None
        # Rows the harness wrote that are not the user's conversation — a notice it sent as a
        # prompt (#275), a branch a rewind dropped (#235) — read as such after `harness_since`,
        # the last round an engine without that rule tracked.
        self.marks = rules is not None and "harness_since" in rules
        self.mark = self.marks and rules["harness_since"] is None
        self.dropped, self.rewound = (_rewound(rows) if self.marks and rewinds else set()), False
        self.cuts = set(rules.get("cuts") or ()) if rules else set()
        self.now = rules.get("now") if rules else None
        self.delivered = {r.get("commandUuid") for _, r in rows if r.get("type") == "queue-operation"
                          and r.get("operation") == "remove" and r.get("reason") not in (None, "absorbed_mid_turn")}
        self.last = None       # (uuid, line, epoch) of the open turn's latest dialogue row
        self.resumed = None    # the round an idle close just ended; its later rows continue it
        self.continued = None
        # the open turn's promptId, a reply, its last row ended it
        self.prompt, self.replied, self.done = None, False, False

    def emit(self, rid, completion, end_line, **extra):
        locator = f"claude:{rid}"
        events = [e for _, e in self.evidence]
        found = {"id": rid, "user": "\n\n".join(self.users),
                 "agent": "\n\n".join(self.trace + _tool_evidence(events, locator)), "end_line": end_line,
                 "completion": completion, "native_results": self.native_results, **extra}
        if self.continued:
            found["continued_from"] = self.continued
        # A resumed or forked copy rewrites rows in message order, so results
        # that streamed between parallel calls move after them. Row timestamps
        # survive the copy: the original append order is offered only to match
        # a copied prefix, never to capture.
        if all(t for t, _ in self.evidence):
            timed = [e for _, e in sorted(self.evidence, key=lambda x: x[0])]
            orders = [o for o in (timed, _interleaved(self.evidence)) if o != events]
            if orders:
                found["agent_time_orders"] = list(dict.fromkeys(
                    "\n\n".join(self.trace + _tool_evidence(o, locator)) for o in orders))
        self.rounds.append(found)
        self.users, self.trace, self.start, self.native_results = [], [], None, []
        self.evidence, self.continued, self.last = [], None, None
        rules = self.rules
        self.new = self.new or (rules is not None and rid == self.since)
        self.split = self.split or (self.splits and rid == rules["prompts_since"])
        self.mark = self.mark or (self.marks and rid == rules["harness_since"])
        return rid

    def finish(self) -> None:
        if self.start and self.final_message and self.final_text:
            self.emit(f"{self.start}:{self.final_message}", "completed", self.final_line)
            self.resumed = None
        self.final_message, self.final_line, self.final_text = None, None, False

    def interrupt(self) -> None:
        # No final answer will come: close the turn at its latest row, which the cursor keeps.
        self.resumed = self.emit(f"{self.start}:{self.last[0]}", "interrupted", self.last[1], cut=self.last[0])

    def row(self, line: int, row: dict) -> None:
        if row.get("isSidechain") or row.get("agentId"):
            return
        if self.mark and row.get("uuid") in self.dropped:
            self.rewound = True
            return
        typ, msg = row.get("type"), row.get("message", {})
        if not isinstance(msg, dict):
            raise ValueError(f"unsupported Claude message at line {line}")
        # All rows of an end_turn message belong to its response, not just the
        # first row, whose content may be thinking and lack the final answer.
        if self.final_message and not (typ == "assistant" and msg.get("id") == self.final_message):
            self.finish()
        if typ == "attachment" and self.new and self.start:
            self.queued(line, row)
            return
        if typ not in ("user", "assistant"):
            return
        uid = row.get("uuid")
        if not uid:
            raise ValueError(f"Claude message without stable uuid at line {line}")
        if uid in self.seen:
            return
        self.seen.add(uid)
        content = msg.get("content")
        if not isinstance(content, (str, list)):
            raise ValueError(f"unsupported Claude content at line {line}")
        self.message(line, row, typ, msg, uid, content)

    def queued(self, line: int, row: dict) -> None:
        """턴이 도는 동안 사용자가 보낸 말은 그 턴에 든다."""
        words = _queued(row, self.delivered)
        uid = row.get("uuid")
        if words and not (uid and uid in self.seen):
            # The message is the turn's latest activity: it moves the idle clock,
            # and a cut stored at it closes the turn after these words on every read.
            self.users.append(words)
            if uid:
                self.seen.add(uid)
                self.last = (uid, line, _epoch(row))
                if uid in self.cuts:
                    self.interrupt()

    def message(self, line: int, row: dict, typ: str, msg: dict, uid, content) -> None:
        """사용자나 응답의 행 하나."""
        tool_result = _has_tool_result(content)
        notice = self.mark and typ == "user" and not tool_result and self.injected_only(_words(content))
        at = _epoch(row)
        self.idle_or_resumed(uid, typ, tool_result, at)
        if isinstance(content, list):
            content = self.blocks(row, typ, uid, content, tool_result)
        if typ == "user" and not tool_result:
            if row.get("isCompactSummary") or row.get("isMeta"):
                return
            self.prompted(row, uid, content, notice)
        elif self.start:
            self.reply(line, typ, msg, content)
        if self.start:
            self.last = (uid, line, at)
            self.done = tool_result or (typ == "assistant" and msg.get("stop_reason") in ("end_turn", "stop_sequence"))
            if uid in self.cuts and not self.final_message:
                self.interrupt()

    def idle_or_resumed(self, uid, typ: str, tool_result: bool, at) -> None:
        """오래 조용했던 턴은 이 행 앞에서 끊고, 끊긴 턴의 뒤따르는 행은 그 턴을 잇는다."""
        if (self.new and self.start and not self.final_message and self.last and self.last[2] is not None
                and at is not None and at - self.last[2] >= IDLE_CLOSE):
            self.interrupt()
        if self.new and not self.start and self.resumed and (typ == "assistant" or tool_result):
            self.start, self.continued, self.resumed = uid, self.resumed, None

    def blocks(self, row: dict, typ: str, uid, content: list, tool_result: bool) -> list:
        """내용의 블록들을 남길 꼴로 바꾼다. 도구 호출과 결과는 원본을 가리키는 참조가 된다."""
        kept = []
        for block in content:
            if self.dialogue:
                kept.extend(self.dialogue_block(row, typ, uid, block, tool_result))
                continue
            if isinstance(block, dict) and block.get("type") == "tool_use":
                self.calls[block.get("id")] = {"name": block.get("name", ""), "input": block.get("input")}
            if isinstance(block, dict) and block.get("type") == "tool_result":
                call = self.calls.get(block.get("tool_use_id"))
                locator = f"claude:{row.get('sessionId', self.sid)}:{uid}"
                if self.start and call and (_file_read(call["name"]) or _mixed_output(call["name"])):
                    self.native_results.append(locator)
                block = {**block, "content": _result_content(call, block.get("content"), locator)}
            kept.append(block)
        return kept

    def dialogue_block(self, row: dict, typ: str, uid, block, tool_result: bool) -> list:
        locator = f"claude:{row.get('sessionId', self.sid)}:{uid}"
        if typ == "user" and not tool_result:
            locator = f"claude-user:{uid}"
        event = _dialogue_event(block, locator) if isinstance(block, dict) else None
        if event and event["type"] in {"tool_call_ref", "tool_result_ref"}:
            if self.start:
                self.evidence.append((row.get("timestamp") or "", event))
            return []
        values = [event] if event is not None else _dialogue_content([block], locator)
        if self.start and (typ != "user" or tool_result):
            self.native_results.extend(locator for v in values if isinstance(v, dict)
                                       and (v.get("native_result") or isinstance(v.get("content"), dict)
                                            and v["content"].get("native_result")))
        return values

    def prompted(self, row: dict, uid, content, notice) -> None:
        """사용자의 말 — 턴을 열거나, 열린 턴에 든다."""
        # Another prompt is another turn. A turn that ended in a tool call (the app's
        # next-step box, ScheduleWakeup) closes there; an interruption keeps its promptId.
        # A notice the harness sent as a prompt is neither (#275): an open turn goes on,
        # and work it starts with no turn open is a round without the user's words.
        if (not notice and self.split and self.start and self.replied
                and row.get("promptId") not in (None, self.prompt)):
            self.emit(f"{self.start}:{self.last[0]}", "completed" if self.done else "interrupted", self.last[1])
        if not self.start:
            self.start, self.resumed, self.prompt, self.replied = uid, None, row.get("promptId"), False
        if not notice:
            self.users.append(_dump(content))

    def reply(self, line: int, typ: str, msg: dict, content) -> None:
        """열린 턴의 응답이나 도구 결과."""
        self.replied = True
        if not self.dialogue or content:
            self.trace.append(f"### {typ}\n\n{_dump(content)}")
        if typ == "assistant" and msg.get("stop_reason") == "end_turn":
            self.final_message, self.final_line = msg.get("id"), line
            if not self.final_message:
                raise ValueError(f"Claude end_turn without message id at line {line}")
            self.final_text = self.final_text or (bool(content.strip()) if isinstance(content, str) else any(
                isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                for b in content))

    def result(self) -> dict:
        self.finish()
        if (self.new and self.start and self.now is not None and self.last and self.last[2] is not None
                and self.now - self.last[2] >= IDLE_CLOSE):
            self.interrupt()
        # The open turn is not a round, but its user words may be cited (Mechanism §9 9항).
        return {"rounds": self.rounds, "pending_tail": bool(self.start), "diagnostics": self.diagnostics,
                "rewound": self.rewound,
                "tail": {"id": self.start, "user": "\n\n".join(self.users)} if self.start and self.users else None}


def _claude(rows: list, sid: str, *, dialogue: bool = False, rules: dict | None = None,
            rewinds: bool = True) -> dict:
    from .harness.base import injected_only   # the harness package imports this module
    rows = [(line, r) for line, r in rows if not r.get("isSidechain") and not r.get("agentId")]
    if not _own_history(rows, sid):
        # A freshly forked file can contain only parent history and child UI metadata.
        return {"rounds": [], "pending_tail": False, "diagnostics": []}
    turns = _ClaudeTurns(rows, sid, dialogue, rules, rewinds, injected_only)
    for line, row in rows:
        turns.row(line, row)
    return turns.result()


def _mirror_text(value: dict, origin: str) -> str | None:
    """같은 말을 두 봉투가 실어 올 때 둘을 짝짓는 열쇠. 글만 든 것만 짝짓는다 — 다른 것이 든 내용은
    짐작으로 지우지 않고 그대로 둔다."""
    if origin == "legacy":
        if isinstance(value.get("message"), str) and not any(
                v for k, v in value.items() if k != "message"):
            return value["message"]
        return None
    content = value["content"]
    if (set(value) <= {"id", "content"} and len(content) == 1
            and isinstance(content[0], dict) and content[0].get("type") == "text"
            and isinstance(content[0].get("text"), str)
            and not any(v for k, v in content[0].items() if k not in {"type", "text"})):
        return content[0]["text"]
    return None


class _CodexTurns:
    """Codex 전사의 행을 차례로 받아 턴을 라운드로 닫는다. 열린 턴에 대해 아는 것이 여기 있다.

    턴은 `task_started`로 열리고 `task_complete`(`completed`)나 `turn_aborted`(`aborted`)로 닫힌다. 닫히지
    않은 채 다음 턴이 열리면 끊긴 것이다(`started`)."""

    def __init__(self, sid: str, dialogue: bool):
        self.sid, self.dialogue = sid, dialogue
        self.rounds, self.diagnostics, self.users, self.trace, self.seen, self.calls = [], [], [], [], set(), {}
        self.turn = None
        self.user_formats = []
        self.resumable, self.final_seen = None, False
        self.evidence, self.has_native_trace, self.compaction_seen = [], False, False

    def open(self, turn) -> None:
        """턴 하나의 것을 비운다. `turn`은 새로 열린 턴이거나, 닫힌 뒤의 None이다."""
        self.turn, self.users, self.trace = turn, [], []
        self.evidence, self.has_native_trace, self.compaction_seen = [], False, False
        self.user_formats.clear()

    def has_trace(self):
        return self.has_native_trace if self.dialogue else self.trace

    def finish(self, completion, line, terminal=None) -> bool:
        inputs = self.users or ([_dump({"native_trigger": "inputless_after_terminal", **self.resumable})]
                                if self.resumable else [])
        if not inputs:
            return False
        if self.turn not in self.seen:
            self.rounds.append({
                "id": self.turn, "user": "\n\n".join(inputs),
                "agent": "\n\n".join(self.trace + _tool_evidence(self.evidence, f"codex:{self.sid}:{self.turn}")
                                      + ([_dump(_terminal(terminal) if self.dialogue else terminal)] if terminal else [])),
                "end_line": line, "completion": completion})
            self.seen.add(self.turn)
        if completion in ("failed", "aborted", "interrupted"):
            if self.users:
                # Prior input is context, not proof that this is the same task.
                self.resumable = {"previous_input_turn": self.turn, "previous_input": self.users[:]}
        else:
            self.resumable = None
        return True

    def add_user(self, value: dict, origin: str) -> None:
        # Some harness versions emit both envelopes. Pair only plain-text
        # mirrors, one for one; preserve the legacy bytes for captured prefixes.
        # Rich or unrecognized content stays intact rather than being guessed away.
        key = _mirror_text(value, origin)
        if self.dialogue:
            locator = f"codex:{self.sid}:{self.turn}:user"
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
            for i, (previous, previous_key) in enumerate(self.user_formats):
                if previous == other and previous_key == key:
                    if origin == "legacy":
                        self.users[i] = rendered
                    self.user_formats[i] = ("paired", key)
                    return
        self.users.append(rendered)
        self.user_formats.append((origin, key))

    def row(self, line: int, row: dict) -> None:
        typ, p = row.get("type"), row.get("payload", {})
        if not isinstance(p, dict):
            raise ValueError(f"unsupported Codex payload at line {line}")
        event = p.get("type")
        if typ == "event_msg":
            self.event(line, p, event)
        elif typ == "turn_context" and not self.turn:
            self.turn = p.get("turn_id")
        elif typ == "compacted" and self.turn:
            self.compaction_seen = True
        elif typ == "response_item" and self.turn:
            self.response(line, p, event)

    def event(self, line: int, p: dict, event) -> None:
        if event == "task_started":
            self.started(line, p)
        elif event == "user_message" and self.turn:
            self.add_user({k: v for k, v in p.items() if k != "type"}, "legacy")
        elif (event == "item_completed" and self.turn
              and isinstance(p.get("item"), dict) and p["item"].get("type") == "UserMessage"):
            self.user_item(line, p)
        elif event == "task_complete":
            self.completed(line, p)
        elif event == "turn_aborted" and self.turn:
            self.aborted(line, p)

    def started(self, line: int, p: dict) -> None:
        if self.turn and (self.users or self.has_trace()):
            if not self.finish("interrupted", line - 1, {
                    "type": "superseded", "turn_id": self.turn, "next_turn_id": p.get("turn_id")}):
                self.diagnostics.append(f"unfinished Codex turn {self.turn}")
        self.open(p.get("turn_id"))
        self.final_seen = False

    def user_item(self, line: int, p: dict) -> None:
        if p.get("turn_id") != self.turn or p.get("thread_id") != self.sid:
            raise ValueError(f"Codex user item identity mismatch at line {line}")
        item = p["item"]
        if isinstance(item.get("content"), list) and item["content"]:
            self.add_user({k: v for k, v in item.items() if k != "type"}, "native")

    def trigger(self, p: dict, event, meta: dict) -> str | None:
        """이 항목이 사람의 말이 아니라 턴을 일으킨 것이면 그 종류."""
        if (event == "message" and p.get("role") == "user"
                and meta.get("content_item_kinds") == ["goal.internal_context"]):
            return "goal"
        if (event == "function_call_output" and not p.get("call_id")
                and p.get("namespace") == "codex_app"
                and p.get("name") in {"automation_update", "send_message_to_thread"}
                and meta.get("turn_id") == self.turn
                and (p["name"] != "send_message_to_thread" or not self.users and not self.resumable)):
            return "heartbeat" if p["name"] == "automation_update" else "thread_message"
        return None

    def response(self, line: int, p: dict, event) -> None:
        meta = p.get("internal_chat_message_metadata_passthrough") or {}
        if meta.get("turn_id") not in (None, self.turn):
            raise ValueError(f"Codex response identity mismatch at line {line}")
        kind = self.trigger(p, event, meta)
        if kind:
            # These are native execution triggers, not new human requests. A turn
            # that already had input or resumable context reads as before, so its
            # tracked hash holds; only the formerly untrackable turn is new.
            trigger = ({"content": _dialogue_content(p.get("content", p.get("output", "")),
                                                    f"codex:{self.sid}:{self.turn}:trigger")}
                       if self.dialogue else {"item": p})
            self.users.append(_dump({"native_trigger": kind, **trigger}))
            self.user_formats.append(("trigger", None))
            return
        if event == "message" and p.get("role") == "assistant" and p.get("phase") == "final_answer":
            self.final_seen = True  # A quiet heartbeat may intentionally finish with empty text.
        if self.dialogue:
            self.dialogue_item(p, event)
        else:
            self.traced_item(p, event)

    def dialogue_item(self, p: dict, event) -> None:
        self.has_native_trace = self.has_native_trace or event != "message" or p.get("role") == "assistant"
        entry = _dialogue_event(p, f"codex:{self.sid}:{self.turn}:{p.get('call_id') or p.get('id') or 'assistant'}")
        if entry is not None:
            if entry["type"] in {"tool_call_ref", "tool_result_ref"}:
                self.evidence.append(entry)
            else:
                self.trace.append(_dump(entry))

    def traced_item(self, p: dict, event) -> None:
        # event_msg user/agent text mirrors response_item. Keep native tool
        # items and assistant responses once; never mistake tool output for
        # the human's next prompt. No truncation of content or tool results.
        if event in ("function_call", "custom_tool_call"):
            arguments = p.get("arguments", p.get("input"))
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except ValueError:
                pass
            self.calls[p.get("call_id")] = {"name": p.get("name", ""), "input": arguments}
        elif event in ("function_call_output", "custom_tool_call_output"):
            p = {**p, "output": _result_content(self.calls.get(p.get("call_id")), p.get("output"),
                                                f"codex:{self.sid}:{self.turn}:{p.get('call_id')}")}
        if event != "message" or p.get("role") == "assistant":
            self.trace.append(_dump(p))

    def maintenance(self, p: dict) -> bool:
        """Native maintenance completion has no dialogue to capture."""
        return bool(self.compaction_seen and not self.users and not self.trace and not self.has_native_trace
                    and not p.get("error") and not p.get("last_agent_message"))

    def answered(self, p: dict) -> bool:
        return bool((self.users or self.resumable) and self.has_trace() and (
            str(p.get("last_agent_message") or "").strip() or self.final_seen))

    def completed(self, line: int, p: dict) -> None:
        if not self.turn or p.get("turn_id") != self.turn:
            raise ValueError(f"Codex completion turn_id mismatch at line {line}")
        if self.dialogue and not self.trace and str(p.get("last_agent_message") or "").strip():
            self.trace.append(_dump({"type": "message", "role": "assistant", "phase": "final_answer",
                                     "content": p["last_agent_message"]}))
        # task_complete is authoritative; final_answer alone never commits.
        if self.maintenance(p):
            pass
        elif p.get("error"):
            if not self.finish("failed", line, p):
                self.diagnostics.append(f"missing input for failed Codex turn {self.turn}")
        elif self.answered(p):
            self.finish("completed", line, p if not p.get("last_agent_message") else None)
        else:
            self.diagnostics.append(f"incomplete content for completed Codex turn {self.turn}")
        self.open(None)

    def aborted(self, line: int, p: dict) -> None:
        if p.get("turn_id") and p["turn_id"] != self.turn:
            raise ValueError(f"Codex abort turn_id mismatch at line {line}")
        # Native abort is a terminal record, not successful work.
        if not self.finish("aborted", line, p) and self.has_trace():
            self.diagnostics.append(f"missing input for aborted Codex turn {self.turn}")
        self.open(None)

    def result(self) -> dict:
        return {"rounds": self.rounds,
                "pending_tail": bool(self.users) or bool(self.turn) or bool(self.diagnostics),
                "diagnostics": self.diagnostics,
                "tail": {"id": self.turn, "user": "\n\n".join(self.users)} if self.turn and self.users else None}


def _codex(rows: list, sid: str, *, dialogue: bool = False) -> dict:
    identities = {r.get("payload", {}).get("id") for _, r in rows if r.get("type") == "session_meta"}
    if identities != {sid}:
        raise ValueError("Codex transcript session_meta.id does not match this conversation")
    turns = _CodexTurns(sid, dialogue)
    for line, row in rows:
        turns.row(line, row)
    return turns.result()


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
