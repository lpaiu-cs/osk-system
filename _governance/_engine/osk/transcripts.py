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

import json
from pathlib import Path
from . import transcript_files
from .harness import antigravity_transcript, claude_transcript, codex_transcript, kiro_transcript


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
    fingerprint = transcript_files.native_fingerprint(path, harness, conversation_id)
    rows, diagnostics, pages, rejoined = [], [], [], []
    n = 0
    for native, limit in transcript_files.native_files(path, harness, conversation_id):
        page = []
        lines = transcript_files.page_lines(native, limit)
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
        result = claude_transcript.parse(rows, conversation_id, rules=rules)
        readable = claude_transcript.parse(rows, conversation_id, dialogue=True, rules=rules)
        if result.pop("rewound", False):
            kept = {r["id"] for r in result["rounds"]}
            unwound = claude_transcript.parse(rows, conversation_id, rules=rules, rewinds=False)
            result["reverted"] = [r["id"] for r in unwound["rounds"] if r["id"] not in kept]
    elif harness == "codex":
        result = codex_transcript.history(pages, conversation_id)
        readable = codex_transcript.history(pages, conversation_id, dialogue=True)
        # The conversation's own page names how it was started (`codex_exec` for a script).
        result["originator"] = pages[-1][0][1].get("payload", {}).get("originator")
        result["reverted"] = transcript_files.reverted(path, harness, conversation_id)
    elif harness == "kiro":
        # Kiro rows carry no conversation ID; the folder is the conversation's own.
        if Path(path).parent.name != conversation_id:
            raise ValueError("Kiro transcript folder does not match this conversation")
        result = readable = kiro_transcript.parse(rows, conversation_id)
    elif harness == "antigravity":
        # Steps carry no conversation ID either: the file sits in `brain/<ID>/`.
        if conversation_id not in Path(path).parts:
            raise ValueError("Antigravity transcript folder does not match this conversation")
        result = readable = antigravity_transcript.parse(rows, conversation_id)
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
