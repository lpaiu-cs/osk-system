"""Durable, local conversation capture/review cursors; shared scope memory is not an ACK."""
from __future__ import annotations
from .core import SCOPE

import hashlib
import json
import os
import re
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from . import core, raw, scope_memory, transcripts, write
from . import harness as adapters
from ._portalock import lock_exclusive, unlock

SOFT, HARD = 9, 15
MAX_REVIEW_ROUNDS = 15
ENDED_AFTER = 12 * 3600  # seconds without a transcript change: the conversation ended (Mechanism §9-4 3항)


def _identity(harness: str, conversation_id: str) -> str:
    if harness not in adapters.NAMES or not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", conversation_id):
        raise ValueError(f"explicit {'/'.join(adapters.NAMES)} harness and actual conversation_id required")
    return hashlib.sha256(f"{core.ROOT.resolve()}\n{harness}\n{conversation_id}".encode()).hexdigest()


def state_path(harness: str, conversation_id: str) -> Path:
    key = _identity(harness, conversation_id)
    root_key = hashlib.sha256(str(core.ROOT.resolve()).encode()).hexdigest()[:16]
    return core.local_lock_path(f"osk-integration-{root_key}-{key}.json", core.ROOT).with_suffix(".json")


def _locked(harness: str, conversation_id: str):
    return _locked_path(state_path(harness, conversation_id))


@contextmanager
def _locked_path(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.with_suffix(".lock").open("a+b") as f:
        lock_exclusive(f)
        try:
            yield p
        finally:
            unlock(f)


def _load(p: Path, harness: str, sid: str) -> dict:
    if not p.exists():
        # Raw identity follows the native conversation across vault copies;
        # operational ownership remains ROOT-specific in state_path.
        return {"version": 1, "root": str(core.ROOT.resolve()), "harness": harness,
                "conversation_id": sid, "session": None, "space": None, "transcript_path": None,
                "record": _record(harness, sid),
                "rounds": [], "reviewed_count": 0, "prompt_count": 0,
                "reviewed_prompt_count": 0, "snapshots": {}, "reviews": [],
                "capture_pending": False, "capture_error": None,
                "native_fingerprint": None, "coverage": None}
    s = json.loads(p.read_text(encoding="utf-8"))
    return _validate_state(s, harness, sid)


def _record(harness: str, sid: str) -> str:
    key = hashlib.sha256((harness + "\n" + sid).encode()).hexdigest()[:32]
    return f"{harness}-{key}"


def _validate_state(s: dict, harness: str, sid: str) -> dict:
    """Validate already read bytes; status reports must not reread or repair them."""
    if (s.get("version"), s.get("root"), s.get("harness"), s.get("conversation_id")) != (
            1, str(core.ROOT.resolve()), harness, sid):
        raise ValueError("integration state identity/version mismatch; not reset")
    if not (isinstance(s.get("rounds"), list) and isinstance(s.get("snapshots"), dict)
            and isinstance(s.get("reviewed_count"), int)
            and 0 <= s["reviewed_count"] <= len(s["rounds"])):
        raise ValueError("damaged integration cursor; not reset")
    return s


def _save(p: Path, s: dict) -> None:
    write._atomic_write(p, json.dumps(s, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _snapshot(s: dict) -> str | None:
    if not s["rounds"]:
        return None
    return "sha256:" + hashlib.sha256(json.dumps(
        [s["root"], s["harness"], s["conversation_id"], s["rounds"]],
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _capture_recovery(s: dict) -> dict:
    """Advice only; it never relaxes capture identity or acknowledges stored raw."""
    phase = s.get("capture_failure_phase", "unknown")
    if s.get("capture_error"):
        basis = "recorded_phase" if phase != "unknown" else "legacy_diagnostic"
        if phase == "unknown":
            error = s["capture_error"]
            if any(t in error for t in ("착지", "세션 키", "scope changed", "scope is invalid")):
                phase = "landing"
            elif any(t in error for t in ("prefix", "접두부", "저장된 기록보다 짧다")):
                phase = "replay"
            elif any(t in error for t in ("identity", "session_meta", "sessionId", "history_base")):
                phase = "read"
            elif "FileNotFoundError" in error or "native transcript path unavailable" in error:
                phase = "source"
                try:
                    found = _locate_transcript(s["harness"], s["conversation_id"],
                                               s.get("capture_path") or s.get("transcript_path"))
                    available = bool(found and Path(found).is_file())
                    missing = not found or not Path(found).exists()
                except (OSError, ValueError):
                    basis = "source_lookup_failed"
                else:
                    basis = "source_lookup"
                    if available:
                        return {"state": "retryable", "phase": phase, "basis": basis,
                                "next_action": "같은 ID의 원본 후보를 찾았다. 현재 엔진으로 capture하여 신원·접두부를 다시 검증한다."}
                    if missing and not (s["rounds"] or s["prompt_count"] or s.get("native_fingerprint")):
                        phase = "awaiting_native"
        actions = {
            "awaiting_native": "같은 대화의 원본이 생성된 뒤 다시 capture한다.",
            "landing": "기존 대화의 scope를 유지하며 명시적인 세션 키와 착지를 확인한다.",
            "source": "같은 대화 ID의 원본을 복구하거나 올바른 전사 경로를 명시한다.",
            "read": "원본의 대화 ID·형식·연결된 과거 전사를 확인하고 같은 대화로 재시도한다.",
            "replay": "추적 중인 턴 순서와 원본 접두부(이어받은 부모 턴 포함)를 확인한다. 기존 좌표나 검토 커서를 초기화하지 않는다.",
        }
        return {"state": "awaiting_input" if phase == "awaiting_native" else "action_required",
                "phase": phase, "basis": basis, "next_action": actions.get(
                    phase, "원본 경로·대화 신원·착지를 확인하고 같은 대화를 다시 capture한다.")}
    if s.get("capture_pending"):
        return {"state": "pending", "phase": "tail", "basis": "saved_cursor",
                "next_action": "종료 기록을 확인한 뒤 같은 대화를 다시 capture한다. 열린 꼬리를 완료로 처리하지 않는다."}
    if not (s.get("native_fingerprint") or s["rounds"]):
        return {"state": "awaiting_input", "phase": "unobserved", "basis": "saved_cursor",
                "next_action": "같은 대화의 원본이 생성된 뒤 다시 capture한다."}
    return {"state": "ready", "phase": "complete", "basis": "saved_cursor", "next_action": None}


def _view(s: dict) -> dict:
    rs, n = s["rounds"], s["reviewed_count"]
    repairs = s.get("repair_pending", {})
    repair_refs = {ref for item in repairs.values() for ref in item["refs"]}
    return {"ok": not bool(s["capture_error"]), "harness": s["harness"],
            "conversation_id": s["conversation_id"], "session": s["session"],
            "record": s["record"], "transcript_path": s["transcript_path"],
            "captured_through": rs[-1]["id"] if rs else None,
            "reviewed_through": rs[n-1]["id"] if n else None,
            "captured_rounds": len(rs), "reviewed_rounds": n,
            "aborted_rounds": sum(r.get("completion") == "aborted" for r in rs),
            "failed_rounds": sum(r.get("completion") == "failed" for r in rs),
            "interrupted_rounds": sum(r.get("completion") == "interrupted" for r in rs),
            "pending": bool(s["capture_pending"] or len(rs) > n or repairs),
            "repair_pending": repairs,
            "capture_pending": s["capture_pending"], "capture_error": s["capture_error"],
            "capture_recovery": _capture_recovery(s),
            "coverage": s.get("coverage"), "excluded": s.get("excluded"),
            "inherited_rounds": len((s.get("inherited") or {}).get("rounds", [])),
            "pending_refs": [r["ref"] for i, r in enumerate(rs) if i >= n or r["ref"] in repair_refs],
            "through": _snapshot(s),
            "prompt_count": s["prompt_count"], "reviewed_prompt_count": s["reviewed_prompt_count"],
            "response_growth": {k: v for k, v in s.get("response_growth", {}).items() if k != "seen"},
            "response_growth_stop": s.get("response_growth_stop"),
            "response_growth_route": s.get("response_growth_route"),
            "last_review": s["reviews"][-1] if s["reviews"] else None}


def status(harness: str, conversation_id: str) -> dict:
    with _locked(harness, conversation_id) as p:
        return _current_view(_load(p, harness, conversation_id), p)


def blocked(limit: int = 10) -> dict:
    """Conversations whose capture is stuck on this device, newest first (§9-3 3항).
    `observed` is the cursor's last save; the hooks keep only a one-line notice."""
    from datetime import datetime
    probe = state_path("claude", "probe")
    rows = []
    for p in probe.parent.glob("-".join(probe.name.split("-")[:3]) + "-*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            if s.get("capture_error") and s.get("root") == str(core.ROOT.resolve()):
                rows.append({"harness": s["harness"], "conversation_id": s["conversation_id"],
                             "error": s["capture_error"], "observed": datetime.fromtimestamp(
                                 p.stat().st_mtime, core.KST).strftime(core.TS_FMT)})
        except (OSError, ValueError, KeyError):
            continue
    rows.sort(key=lambda r: r["observed"], reverse=True)
    return {"count": len(rows), "conversations": rows[:limit]}


def memory_seen(harness: str, conversation_id: str, digest: str) -> bool:
    """Was this conversation last given the shared memory at exactly `digest`? A hook then
    carries one line instead of the whole text; a changed hash carries the whole again."""
    with _locked(harness, conversation_id) as p:
        return _load(p, harness, conversation_id).get("memory_shown") == digest


def note_memory(harness: str, conversation_id: str, digest: str) -> None:
    """The hook's output really carried the whole shared memory at `digest`."""
    with _locked(harness, conversation_id) as p:
        s = _load(p, harness, conversation_id)
        if s.get("memory_shown") != digest:
            s["memory_shown"] = digest
            _save(p, s)


def turn_key(harness: str, round_id: str) -> str:
    """The original turn's identity for citations and review refs. A Claude round is
    `<first user row>:<final message>`; its first user row already names the turn while
    it is still open, so a citation of the open turn and of the finished round agree."""
    return round_id.split(":", 1)[0] if harness == "claude" else round_id


_SESSION_LOCATOR = re.compile(r"claude:[A-Za-z0-9_.-]+:")


def _turn_hashes(pair: dict, dialogue_v1: dict | None = None) -> list[str]:
    """Local evidence of what was reviewed — the dialogue text never enters the vault.
    The first hash is the turn's own; a resumed copy also offers its original row order.
    A copy may rewrite result locators with its own conversation ID, so that part is
    left out: a copied turn hashes the same in parent and child."""
    shown = (dialogue_v1 or {}).get(pair["id"], pair)
    digest = lambda agent: core.sha256_bytes(_SESSION_LOCATOR.sub(
        "claude:*:", json.dumps([shown["user"], agent], ensure_ascii=False)).encode("utf-8"))
    return [digest(a) for a in filter(None, (shown["agent"], shown.get("agent_time_order")))] or [digest("")]


def _fork_parent_turns(s: dict, rounds: list, dialogue_v1: dict | None = None) -> list:
    """A Codex fork declares each page's owner. The parent's turns stay the parent's to
    review — by its own cursor, in its own scope — and never become this conversation's
    new work, whether or not the parent is tracked here. They are kept as references with
    their hashes, so a later capture refuses a changed parent prefix."""
    parent = [r for r in rounds if r.get("origin_conversation_id") not in (None, s["conversation_id"])]
    if rounds[:len(parent)] != parent:
        raise ValueError("Codex fork parent turns must precede the conversation's own; capture remains pending")
    known = (s.get("inherited") or {}).get("rounds")
    if known is not None and ([k["id"] for k in known] != [r["id"] for r in parent] or any(
            k["hash"] not in _turn_hashes(r, dialogue_v1) for k, r in zip(known, parent))):
        raise ValueError("inherited native prefix changed; capture remains pending")
    if parent and known is None:
        s["inherited"] = {"rounds": [
            {"id": r["id"], "ref": raw.native_ref("codex", r["origin_conversation_id"], r["id"]),
             "hash": _turn_hashes(r, dialogue_v1)[0]} for r in parent]}
    return rounds[len(parent):]


def _inherited_rounds(s: dict, rounds: list, dialogue_v1: dict | None = None) -> list:
    """Reuse only the caller's copied prefix, with native ID and byte evidence."""
    if s["harness"] == "codex":
        return _fork_parent_turns(s, rounds, dialogue_v1)
    if s["harness"] != "claude" or not s["space"]:
        return rounds
    scope = raw._scope_of_space(s["space"])
    path = raw.record_path(scope, s["record"])
    inherited = raw.inherited_prefix(path) or s.get("inherited")
    if not inherited and (path.exists() or s["rounds"]):
        return rounds  # A conversation already tracking its own turns keeps them.
    cache = {}

    def matches(pair, source):
        if raw.is_native(source["ref"]):
            # A parent tracked by original turn: same turn ID and the same reviewed text.
            return source["hash"] in _turn_hashes(pair, dialogue_v1)
        name, number = raw.parse_ref(source["ref"])
        if "/".join(raw._raw_file(name).relative_to(core.ROOT).parts[:2]) != s["space"]:
            raise ValueError("inherited source crossed scope; capture remains pending")
        if name not in cache:
            text = raw.read_exact(raw._raw_file(name))
            cache[name] = text, raw._round_spans(text)
        text, spans = cache[name]
        block = text[slice(*spans[number])]
        if core.sha256_bytes(block.rstrip("\n").encode()) != source["hash"]:
            raise ValueError("inherited raw changed; capture remains pending")
        readable = raw._round_body(block).startswith(raw._DIALOGUE_V1)
        if readable:
            pair = (dialogue_v1 or {})[pair["id"]]
        # A raw owner may have captured another conversation's copied history.
        # Match generated result locators by native result UUID, not raw ownership.
        locator = re.compile(
            (r'("native_result": "claude:)' if readable else
             r'("content": \{"coverage": "tool-output-reference", "native_result": "claude:)')
            + r'([A-Za-z0-9_.-]+):([A-Za-z0-9_.-]+)(")')
        origins = {m[3]: m[2] for m in locator.finditer(block)}
        references = Counter(pair.get("native_results", []))
        occurrences = Counter("claude:" + m[2] + ":" + m[3] for m in locator.finditer(pair["agent"]))
        if any(occurrences[ref] != count for ref, count in references.items()
               if ref.startswith("claude:" + s["conversation_id"] + ":")):
            return False  # A dialogue quotation makes replacement ambiguous.
        from . import secrets
        # The copy may hold the tool evidence in message order; the original
        # captured append order, which the rows' timestamps still give.
        for text in filter(None, (pair["agent"], pair.get("agent_time_order"))):
            agent = locator.sub(
                lambda m: m[1] + origins.get(m[3], m[2]) + ":" + m[3] + m[4]
                if m[2] == s["conversation_id"] and references["claude:" + m[2] + ":" + m[3]]
                else m[0], text)
            expected = raw._block(number, raw.escape_numeric_h2(pair["user"]), raw.escape_numeric_h2(agent),
                                  dialogue_id=pair["id"] if readable else None)
            if raw._round_body(block) == raw._round_body(secrets.filter_text(expected)[0]):
                return True
        return False

    if inherited is None:
        known = {}
        if rounds and re.fullmatch(r"[0-9a-fA-F-]{36}", rounds[0]["id"].split(":")[0]):
            # ponytail: one local-state scan on first capture; index native IDs if this grows costly.
            probe = state_path("claude", s["conversation_id"])
            prefix = "-".join(probe.name.split("-")[:3]) + "-"
            for candidate in sorted(probe.parent.glob(prefix + "*.json")):
                if candidate == probe:
                    continue
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                    if data.get("harness") != "claude" or data.get("root") != s["root"]:
                        continue
                    owner = _load(candidate, "claude", data["conversation_id"])
                    if candidate != state_path("claude", owner["conversation_id"]):
                        continue
                    owner_scope = owner["space"] or (
                        (SCOPE + '/') + (write.resolve_session(owner["session"]) or ""))
                    if owner_scope != s["space"]:
                        continue
                    for source in owner["rounds"]:
                        known.setdefault(source["id"], []).append(source)
                except (OSError, ValueError, KeyError):
                    continue
        shared = []
        for pair in rounds:
            candidates = known.get(pair["id"], [])
            if not candidates:
                break
            source = next((r for r in candidates if matches(pair, r)), None)
            if source is None:
                raise ValueError("copied native round ID has different content; capture remains pending")
            shared.append(source)
        inherited = {"rounds": shared} if shared else None
    if inherited:
        prefix = inherited["rounds"]
        if (len(rounds) < len(prefix)
                or any(pair["id"] != source["id"] or not matches(pair, source)
                       for pair, source in zip(rounds, prefix))):
            raise ValueError("inherited native prefix changed; capture remains pending")
        s["inherited"] = inherited
        return rounds[len(prefix):]
    return rounds


def _locate_transcript(harness: str, sid: str, saved: str | None = None) -> str | None:
    """Recover a moved native file by exact ID; ambiguity requires an explicit path."""
    _identity(harness, sid)
    if saved and Path(saved).exists():
        return str(Path(saved).resolve())
    matches = sorted({p.resolve() for p in adapters.get(harness).transcripts(sid) if p.is_file()})
    if len(matches) > 1:
        raise ValueError("multiple transcripts for this ID; provide explicit harness/transcript_path")
    # transcripts.read checks the native identity before this path is saved.
    return str(matches[0]) if matches else saved


def capture(harness: str, conversation_id: str, transcript_path: str | None,
            session: str, space: str | None = None) -> dict:
    """Track this conversation's completed turns for review. The dialogue stays in the
    harness transcript: the cursor keeps original-turn refs and hashes, never vault text
    (헌법 4조 3항 · 시행령 §2 2항). Evidence enters `_cited/` only through `cite`."""
    with _locked(harness, conversation_id) as p:
        s = _load(p, harness, conversation_id)
        s["capture_pending"], s["capture_error"] = True, None
        s.pop("capture_failure_phase", None)
        _save(p, s)  # Persist intent before reading; failures remain pending.
        phase = "landing"
        try:
            pinned = s["space"]
            stored = next((r["ref"] for r in s["rounds"] if not raw.is_native(r["ref"])), None)
            if not pinned and stored:
                name, _ = raw.parse_ref(stored)
                pinned = "/".join(raw._raw_file(name).relative_to(core.ROOT).parts[:2])
            if pinned:
                scope = raw._scope_of_space(pinned)
                if not scope:
                    raise ValueError("saved conversation scope is invalid; capture remains pending")
                pinned = (SCOPE + '/') + scope
            destination, bound = write.resolve_landing(session, space, raw._CONFINE)
            requested = (SCOPE + '/') + destination if destination else None
            if pinned and not requested and s["session"]:
                # A generic cwd key must not bind unrelated conversations.
                # Resume this native ID using its explicitly chosen session.
                session = s["session"]
                destination, bound = write.resolve_landing(session, pinned, raw._CONFINE)
                requested = (SCOPE + '/') + destination
            if (pinned and requested and pinned != requested
                    or s["session"] and s["session"] != session
                    and not (bound and requested == pinned or not pinned and requested)):
                raise ValueError("conversation scope changed; existing capture was not moved")
            s["session"] = session
            s["space"] = pinned or requested
            requested_path = (str(Path(transcript_path).resolve()) if transcript_path else
                              s.get("capture_path") or s["transcript_path"])
            if not s["transcript_path"]:
                s["transcript_path"] = requested_path  # First-capture intent survives an absent/unflushed file.
            _save(p, s)
            phase = "source"
            native_path = _locate_transcript(harness, conversation_id, requested_path)
            if not native_path:
                if not (s["rounds"] or s["prompt_count"] or s.get("native_fingerprint")):
                    phase = "awaiting_native"
                raise ValueError("native transcript path unavailable; use integration capture with --transcript")
            # Retain the attempted source across a crash, separately from the last
            # accepted source. Only a validation rejection clears this intent below.
            s["capture_path"] = native_path
            _save(p, s)
            phase = "read"
            parsed = transcripts.read(native_path, harness, conversation_id)
            if parsed.get("originator") == "codex_exec":
                # A scripted `codex exec` run — osk's own scheduled runs included — is not a
                # conversation to learn from: its turns are neither tracked nor reviewed
                # (Mechanism §9-4 3항). Rounds an earlier engine stored keep their review.
                s.update(excluded="codex_exec", capture_pending=False, capture_error=None,
                         transcript_path=native_path, native_fingerprint=parsed["native_fingerprint"])
                s.pop("capture_path", None)
                _save(p, s)
                return {**_current_view(s, p), "appended": 0, "changed": 0}
            coverage = parsed["coverage"]
            if not s["rounds"]:
                s["coverage"] = coverage
            _save(p, s)
            # ponytail: serialize tracking until its cursor is saved; use per-scope
            # locks if capture throughput matters.
            with _locked_path(core.local_lock_path("osk-capture-prefix.lock")):
                phase = "replay" if s["space"] else "landing"
                rounds = _inherited_rounds(s, parsed["rounds"], parsed.get("dialogue_v1"))
                if harness == "codex":
                    # A recovered past turn follows the turns already tracked: keep the saved order.
                    order = [r["id"] for r in s["rounds"]]
                    by_id = {r["id"]: r for r in rounds}
                    if len(by_id) != len(rounds) or any(rid not in by_id for rid in order):
                        raise ValueError("native round identity prefix changed; the review cursor was not moved")
                    known = set(order)
                    rounds = [by_id[rid] for rid in order] + [r for r in rounds if r["id"] not in known]
                ids = [r["id"] for r in rounds]
                if len(ids) != len(set(ids)) or ids[:len(s["rounds"])] != [r["id"] for r in s["rounds"]]:
                    raise ValueError("native round identity prefix changed; the review cursor was not moved")
                if rounds and not s["space"]:
                    # Tracking writes nothing, but citing a turn needs its landing (Mechanism §9 9항).
                    raise write.WriteError("착지 미정 — 추적하지 않았다. 결속된 세션 키 또는 명시 space로 capture한다")
                # Existing snapshots bind the saved entries: keep them, add the new turns.
                # An unreviewed original turn whose words changed under its ID is tracked
                # as it reads now, and snapshots taken before the change are dropped: no
                # review closes words the reviewer did not see. A reviewed turn keeps the
                # version its review judged.
                prior = {r["id"]: r for r in s["rounds"]}
                tracked, changed = [], []
                for i, r in enumerate(rounds):
                    hashes = _turn_hashes(r, parsed.get("dialogue_v1"))
                    entry = prior.get(r["id"])
                    if (entry and raw.is_native(entry["ref"]) and entry["hash"] not in hashes
                            and i >= s["reviewed_count"]):
                        entry = {**entry, "hash": hashes[0]}
                        changed.append(i)
                    tracked.append(entry or {
                        "id": r["id"], "ref": raw.native_ref(harness, conversation_id, turn_key(harness, r["id"])),
                        "completion": r["completion"], "hash": hashes[0]})
                s["rounds"] = tracked
                if changed:
                    s["snapshots"] = {t: v for t, v in s["snapshots"].items()
                                      if v.get("repair_refs") or v["count"] <= changed[0]}
                appended = len(s["rounds"]) - len(prior)
                if appended or changed:
                    token = _snapshot(s)
                    s["snapshots"].setdefault(token, {"count": len(s["rounds"]), "prompt_count": s["prompt_count"]})
                s["capture_pending"] = parsed["pending_tail"]
                s["capture_error"] = "; ".join(parsed["diagnostics"]) or None
                # Replace an established source only after identity and raw-prefix replay
                # succeed. A rejected explicit path must not poison later pathless retries.
                s["transcript_path"] = native_path
                s.pop("capture_path", None)
                s["coverage"] = coverage
                if s["capture_error"]:
                    s["capture_failure_phase"] = "read"
                s["native_fingerprint"] = parsed["native_fingerprint"]
                _save(p, s)
                return {**_current_view(s, p), "appended": appended, "changed": len(changed)}
        except Exception as exc:
            if isinstance(exc, FileNotFoundError) and phase in {"source", "read"}:
                phase = ("source" if s["rounds"] or s["prompt_count"] or s.get("native_fingerprint")
                         else "awaiting_native")
            s["capture_pending"], s["capture_error"] = True, f"{type(exc).__name__}: {exc}"
            s["capture_failure_phase"] = phase
            # Only a validation refusal discards the candidate source; an I/O failure
            # keeps it for the next pathless retry.
            if phase != "landing" and isinstance(exc, ValueError):
                s.pop("capture_path", None)
            _save(p, s)
            return _current_view(s, p)


def tick(harness: str, conversation_id: str) -> dict:
    """Count the actual hook event even with empty/unbound shared memory; never reset on resume."""
    with _locked(harness, conversation_id) as p:
        s = _load(p, harness, conversation_id)
        s["prompt_count"] += 1
        n = s["prompt_count"] - s["reviewed_prompt_count"]
        _save(p, s)
        current = _current_view(s, p)
        return {**current, "due": bool(current["repair_pending"]) or n > 0 and n % HARD in (SOFT, 0),
                "hard": n > 0 and n % HARD == 0, "unreviewed_prompts": n}


def _verify_raw(rounds: list) -> None:
    """Stored rounds must still match their saved hashes. An original-turn ref has no
    vault copy to check — the reviewer judged the turn from the conversation itself."""
    raw_files = {}
    for r in rounds:
        if raw.is_native(r["ref"]):
            continue
        name, index = raw.parse_ref(r["ref"])
        if name not in raw_files:
            text = raw.read_exact(raw._raw_file(name))
            raw_files[name] = text, raw._round_spans(text)
        text, spans = raw_files[name]
        if index not in spans or core.sha256_bytes(
                text[slice(*spans[index])].rstrip("\n").encode("utf-8")) != r["hash"]:
            raise ValueError("raw snapshot changed; review is not acknowledged")


def _verify_native(s: dict, rounds: list) -> None:
    """An original turn has no vault copy: this device's transcript must still hold the
    words the snapshot tracked, or the review would close what its reviewer did not read."""
    native = [r for r in rounds if raw.is_native(r["ref"])]
    if not native:
        return
    source = _locate_transcript(s["harness"], s["conversation_id"], s.get("capture_path") or s["transcript_path"])
    if not (source and Path(source).is_file()):
        raise ValueError("the original transcript is not on this device; review is not acknowledged")
    parsed = transcripts.read(source, s["harness"], s["conversation_id"])
    current = {r["id"]: _turn_hashes(r, parsed.get("dialogue_v1")) for r in parsed["rounds"]}
    if any(r["hash"] not in current.get(r["id"], []) for r in native):
        raise ValueError("an original turn changed after this snapshot; capture again and review the current one")


def acknowledge(harness: str, conversation_id: str, through: str,
                outcome: str, reason: str, targets: list | None = None) -> dict:
    if outcome not in ("preserved", "summary", "no_value", "deferred") or not isinstance(reason, str) or not reason.strip():
        raise ValueError("outcome preserved|summary|no_value|deferred and nonempty reason required")
    with _locked(harness, conversation_id) as p, core.mutation_lock():
        s = _load(p, harness, conversation_id)
        snap = s["snapshots"].get(through)
        if _register_repair(s, through, _review_state_locked(s, through)):
            _save(p, s)
        repair = s.get("repair_pending", {}).get(through)
        if not snap or snap["count"] <= s["reviewed_count"] and not repair:
            raise ValueError("through is not an unreviewed snapshot of this conversation")
        refs = set(repair["refs"]) if repair else {
            r["ref"] for r in s["rounds"][s["reviewed_count"]:snap["count"]]}
        _verify_raw([r for r in s["rounds"] if r["ref"] in refs])
        if not repair and outcome != "deferred":
            # A repair rechecks receipts only; a deferral closes nothing.
            _verify_native(s, [r for r in s["rounds"] if r["ref"] in refs])
        receipts = []
        if outcome == "preserved":
            from . import distillation
            if not isinstance(targets, list) or not targets:
                raise ValueError("preserved requires completed distillation targets [{key:...}]")
            for target in targets:
                if not isinstance(target, dict) or not isinstance(target.get("key"), str):
                    raise ValueError("preserved target must name a distillation key")
                receipt = distillation._status_locked(target["key"])
                sources = receipt.get("sources", [])
                # A cited round binds through the original turn it cites (Mechanism §9 9항).
                bound = ({raw.canonical_ref(r["ref"]) for r in sources}
                         | {r["native"] for r in sources if r.get("native")})
                if receipt.get("status") != "complete" or not {raw.canonical_ref(ref) for ref in refs} & bound:
                    raise ValueError("target has no complete body/source/hub receipt for this snapshot")
                target_path = core.resolve_in_root(receipt.get("target", {}).get("path", ""))
                if target_path is None or not raw.graph.space_of(target_path)[0] == "scope":
                    raise ValueError("preserved integration target must be a durable Scope node")
                receipts.append(receipt)
        elif outcome == "summary":
            memory = scope_memory.read(s["session"])
            if not isinstance(targets, list) or not targets or not all(
                    isinstance(t, dict) and isinstance(t.get("text"), str) and t["text"].strip()
                    and t["text"] in memory["text"] for t in targets):
                raise ValueError("summary requires exact excerpts present in current shared memory")
            receipts = [{"scope": memory["scope"], "hash": memory["hash"], "targets": targets}]
        review = {"through": through, "outcome": outcome, "reason": reason.strip(),
                  "at": core.now_iso(), "refs": sorted(refs), "receipts": receipts,
                  "mechanical_preservation": outcome == "preserved", "semantic_verified": False}
        s["reviews"].append(review)
        if outcome != "deferred":
            s["reviewed_count"] = max(s["reviewed_count"], snap["count"])
            s["reviewed_prompt_count"] = max(s["reviewed_prompt_count"], snap["prompt_count"])
            s.get("repair_pending", {}).pop(through, None)
            if s["reviewed_count"] == len(s["rounds"]) and not s.get("repair_pending"):
                # Every tracked turn is reviewed, whoever reviewed it: the conversation's
                # user-turn and Stop counts start over (Mechanism §9-3 1항).
                s["reviewed_prompt_count"] = s["prompt_count"]
                if s.get("response_growth"):
                    s["response_growth"]["attempted_count"] = s["response_growth"]["count"]
        _save(p, s)
        return _view(s)


def _review_state_locked(s: dict, through: str) -> dict:
    snap = s["snapshots"].get(through)
    last = next((r for r in reversed(s["reviews"]) if r["through"] == through), {})
    result = {"harness": s["harness"], "conversation_id": s["conversation_id"], "through": through,
              "status": "pending", "outcome": last.get("outcome")}
    if snap and snap.get("repair_parts"):
        parts = [_review_state_locked(s, token) for token in snap["repair_parts"]]
        complete = all(part["status"] == "complete" for part in parts)
        return {**result, "status": "complete" if complete else "pending",
                "reason": None if complete else "bounded repair reviews remain pending",
                "parts": parts, "semantic_verified": False}
    repair = s.get("repair_pending", {}).get(through)
    if repair:
        return {**result, "reason": repair["reason"], "repair_pending": True}
    if not snap or not last or last.get("outcome") == "deferred" or s["reviewed_count"] < snap["count"]:
        return result
    try:
        _verify_raw([r for r in s["rounds"][:snap["count"]]
                     if not snap.get("repair_refs") or r["ref"] in snap["repair_refs"]])
        if last["outcome"] == "preserved":
            from . import distillation
            for receipt in last["receipts"]:
                current = distillation._status_locked(receipt["key"])
                if (current.get("status") != "complete" or current.get("sources") != receipt["sources"]
                        or any(current.get("target", {}).get(k) != receipt["target"].get(k)
                               for k in ("id", "path", "hash"))
                        or any(current.get("hub", {}).get(k) != receipt["hub"].get(k)
                               for k in ("id", "path"))):
                    return {**result, "reason": "preserved target/source/hub receipt changed"}
        elif last["outcome"] == "summary":
            memory = scope_memory.read(s["session"])
            for receipt in last["receipts"]:
                if not all(t["text"] in memory["text"] for t in receipt["targets"]):
                    return {**result, "reason": "reviewed summary excerpt no longer exists"}
    except (ValueError, OSError, write.WriteError) as exc:
        return {**result, "reason": str(exc)}
    return {**result, "status": "complete", "review": last, "semantic_verified": False}


def _register_repair(s: dict, through: str, result: dict) -> bool:
    """Keep an observed failed receipt pending until an explicit replacement ACK."""
    if s["snapshots"].get(through, {}).get("repair_parts"):
        changed = False
        for token in s["snapshots"][through]["repair_parts"]:
            changed = _register_repair(s, token, _review_state_locked(s, token)) or changed
        return changed
    prior = next((r for r in reversed(s["reviews"])
                  if r["through"] == through and r["outcome"] != "deferred"), None)
    if (result["status"] == "complete" or not result.get("reason") or not prior
            or through in s.get("repair_pending", {})):
        return False
    s.setdefault("repair_pending", {})[through] = {
        "reason": result["reason"], "refs": prior["refs"], "since": core.now_iso(),
        "review_count": len(s["reviews"])}
    return True


def _current_view(s: dict, path: Path) -> dict:
    # Only the latest completed receipt is discovered opportunistically. Older
    # failed final checks are explicitly registered by review_status; there is
    # no unbounded historical integrity scan on every prompt or capture.
    with core.mutation_lock():
        latest = next((r for r in reversed(s["reviews"]) if r["outcome"] != "deferred"), None)
        if latest:
            token = latest["through"]
            # A partition is still one original obligation. Check its sibling
            # receipts, rather than only the last acknowledged chunk.
            while parent := next((t for t, snap in s["snapshots"].items()
                                  if token in snap.get("repair_parts", [])), None):
                token = parent
            if _register_repair(s, token, _review_state_locked(s, token)):
                _save(path, s)
        return _view(s)


def _review_status_locked(harness: str, conversation_id: str, through: str) -> dict:
    # Pure while the caller owns mutation lock: never acquire the local lock or
    # write state here. Public review_status persists failures in local→mutation order.
    return _review_state_locked(_load(state_path(harness, conversation_id), harness, conversation_id), through)


def review_status(harness: str, conversation_id: str, through: str) -> dict:
    with _locked(harness, conversation_id) as p, core.mutation_lock():
        s = _load(p, harness, conversation_id)
        result = _review_state_locked(s, through)
        if _register_repair(s, through, result):
            _save(p, s)
            result["repair_pending"] = True
        return result


def _ended_source(s: dict) -> str | None:
    """This device's transcript of a conversation that has gone quiet. A worker without
    the conversation reads its unreviewed turns there; a conversation still running keeps
    its own review cadence, where the turns are already in context."""
    try:
        path = _locate_transcript(s["harness"], s["conversation_id"],
                                  s.get("capture_path") or s["transcript_path"])
        if path and time.time() - Path(path).stat().st_mtime >= ENDED_AFTER:
            return path
    except (OSError, ValueError):
        pass
    return None


def _known_pending(limit: int) -> tuple[list, int, list, list]:
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    probe = state_path("claude", "inventory")
    prefix = "-".join(probe.name.split("-")[:3]) + "-"
    paths = sorted(probe.parent.glob(prefix + "*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    states, errors, awaiting_native = [], [], []
    for p in paths:
        try:
            value = json.loads(p.read_text(encoding="utf-8"))
            with _locked(value["harness"], value["conversation_id"]) as locked_path:
                if p != locked_path:
                    raise ValueError("state filename identity mismatch")
                s = _load(p, value["harness"], value["conversation_id"])
                current = _current_view(s, p)
            changed, missing = False, False
            try:
                native_path = _locate_transcript(
                    s["harness"], s["conversation_id"], s.get("capture_path") or s.get("transcript_path"))
                if native_path:
                    changed = s.get("native_fingerprint") != transcripts.native_fingerprint(
                        native_path, s["harness"], s["conversation_id"])
                else:
                    changed, missing = True, True
            except FileNotFoundError:
                changed, missing = True, True
            except (OSError, ValueError) as exc:
                changed = True
                errors.append({"state": str(p), "phase": "native_lookup", "error": str(exc)})
                # Capture still fails closed, but stored raw remains reviewable.
            if missing and not (s["prompt_count"] or s["rounds"] or s.get("native_fingerprint")):
                # SessionStart may precede file creation, or no user turn ever
                # follows. Keep the cursor; do not spend the bounded work slots.
                awaiting_native.append({"harness": s["harness"], "conversation_id": s["conversation_id"],
                                        "state": "awaiting_native", "transcript_path": s["transcript_path"]})
                continue
            # A worker without the conversation takes stored rounds, receipts, and the
            # unreviewed turns of a conversation that ended on this device (Mechanism §9-4 3항).
            pending = current["pending_refs"]
            if (changed or current["repair_pending"] or any(not raw.is_native(ref) for ref in pending)
                    or pending and _ended_source(s)):
                states.append(s)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({"state": str(p), "error": str(exc)})
    return states[:limit], max(0, len(states) - limit), errors, awaiting_native


def list_pending(limit: int = 20) -> dict:
    states, remaining, errors, awaiting_native = _known_pending(limit)
    jobs = []
    for s in states:
        if not _view(s)["pending_refs"]:
            continue
        job = prompt(s["harness"], s["conversation_id"], offline=True)
        if not job["pending_refs"]:
            continue
        job["prompt"] = job.pop("text")
        jobs.append(job)
    return {"ok": not errors, "jobs": jobs, "remaining": remaining, "errors": errors,
            "awaiting_native": awaiting_native}


def catchup(limit: int = 20, *, max_rounds: int = MAX_REVIEW_ROUNDS) -> dict:
    """Bounded scheduler catch-up of known own vault states; never inject these in a normal session."""
    states, remaining, errors, awaiting_native = _known_pending(limit)
    captures, jobs = [], []
    for s in states:
        try:
            result = capture(s["harness"], s["conversation_id"],
                             s.get("capture_path") or s["transcript_path"], s["session"], s.get("space"))
            captures.append({k: result.get(k) for k in (
                "harness", "conversation_id", "ok", "appended", "capture_error", "capture_recovery")})
            if result["pending_refs"]:
                job = prompt(s["harness"], s["conversation_id"], include_organization=False,
                             max_rounds=max_rounds, offline=True)
                if job["pending_refs"]:
                    job["prompt"] = job.pop("text")
                    jobs.append(job)
        except Exception as exc:
            errors.append({"harness": s["harness"], "conversation_id": s["conversation_id"], "error": str(exc)})
    return {"ok": not errors and all(r["ok"] for r in captures), "jobs": jobs,
            "captures": captures, "remaining": remaining, "errors": errors,
            "awaiting_native": awaiting_native}


def prompt(harness: str, conversation_id: str, *, include_organization: bool = True,
           max_rounds: int = MAX_REVIEW_ROUNDS, organization_in_text: bool = True,
           offline: bool = False) -> dict:
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= MAX_REVIEW_ROUNDS:
        raise ValueError(f"max_rounds must be between 1 and {MAX_REVIEW_ROUNDS}")
    with _locked(harness, conversation_id) as p:
        s = _load(p, harness, conversation_id)
        st = _current_view(s, p)
        count = min(len(s["rounds"]), s["reviewed_count"] + max_rounds)
        if offline:
            # A worker without this conversation takes stored rounds, and original turns
            # only once the conversation has ended on this device (Mechanism §9-4 3항).
            ended = _ended_source(s)
            stored = s["reviewed_count"]
            while stored < count and (ended or not raw.is_native(s["rounds"][stored]["ref"])):
                stored += 1
            count = stored
        if s.get("repair_pending"):
            token = min(s["repair_pending"], key=lambda t: (s["snapshots"][t]["count"], t))
            repair = s["repair_pending"][token]
            if len(repair["refs"]) > max_rounds:
                # Split the obligation itself, not just its displayed refs. Each
                # part needs its own explicit ACK; the parent verifies all parts.
                parts = []
                for start in range(0, len(repair["refs"]), max_rounds):
                    refs = repair["refs"][start:start + max_rounds]
                    child = "sha256:" + hashlib.sha256(json.dumps(
                        [token, repair["review_count"], refs], sort_keys=True).encode()).hexdigest()
                    s["snapshots"][child] = {k: s["snapshots"][token][k]
                                              for k in ("count", "prompt_count")}
                    s["snapshots"][child]["repair_refs"] = refs
                    s["repair_pending"][child] = {**repair, "refs": refs}
                    parts.append(child)
                s["snapshots"][token]["repair_parts"] = parts
                del s["repair_pending"][token]
                token = parts[0]
                _save(p, s)
            st["through"] = token
            st["pending_refs"] = s["repair_pending"][token]["refs"]
            st["remaining_rounds"] = (len(s["rounds"]) - s["reviewed_count"]
                                      + sum(len(r["refs"]) for t, r in s["repair_pending"].items()
                                            if t != token))
            st["repair"] = s["repair_pending"][token]
        elif count > s["reviewed_count"]:
            selected = {**s, "rounds": s["rounds"][:count]}
            token = _snapshot(selected)
            # A bounded review may end inside the initial catch-up batch. Its
            # prompt counter does not claim newer unreviewed prompts as done.
            s["snapshots"].setdefault(token, {"count": count, "prompt_count": min(
                s["prompt_count"], s["reviewed_prompt_count"] + count - s["reviewed_count"])})
            _save(p, s)
            st["through"] = token
            st["pending_refs"] = [r["ref"] for r in s["rounds"][s["reviewed_count"]:count]]
            st["remaining_rounds"] = len(s["rounds"]) - count
        else:
            st["pending_refs"] = []  # an offline range that begins at an original turn is empty
        selected_refs = set(st["pending_refs"])
        st["raw_review"] = {"state": "none", "rounds": len(selected_refs), "error": None}
        if selected_refs:
            try:
                with core.mutation_lock():
                    selected_rounds = [r for r in s["rounds"] if r["ref"] in selected_refs]
                    if {r["ref"] for r in selected_rounds} != selected_refs:
                        raise ValueError("selected raw coordinates are missing from the saved cursor")
                    _verify_raw(selected_rounds)
                st["raw_review"]["state"] = ("native" if all(raw.is_native(ref) for ref in selected_refs)
                                             else "verified")
            except (ValueError, OSError, write.WriteError) as exc:
                st["raw_review"].update(state="unavailable", error=str(exc))
    # One snapshot identity for ordinary hooks and dedicated workers alike.
    # A later snapshot must not collide with an immutable distillation request.
    st["key"] = "scope-review-" + st["through"].removeprefix("sha256:") if st["pending_refs"] else None
    if st.get("repair"):
        st["key"] += f"-repair-{st['repair']['review_count']}"
    text = (f"[osk 대화별 통합 대기 — {harness}/{conversation_id}]\n"
            f"공유 scope 기억 session={json.dumps(st['session'], ensure_ascii=False)}와 "
            "이 대화의 검토 상태는 별개다. 공유 기억이 비어 있어도 검토한다.\n")
    if st["capture_error"]:
        text += f"포착 진단: {st['capture_error']} — 미완료로 남았다. 본 작업은 계속할 수 있다.\n"
    if st["capture_recovery"]["next_action"]:
        text += f"포착 재개: {st['capture_recovery']['next_action']}\n"
    if st["raw_review"]["state"] == "native" and not offline:
        text += (f"이번 검토 범위는 이 대화의 원본 턴 {st['raw_review']['rounds']}개다. 원문은 vault에 "
                 "옮기지 않았다(시행령 §2 2항) — 대화 문맥에서 판단한다.\n")
    elif st["raw_review"]["state"] == "verified":
        text += ("이번 검토 범위의 저장된 raw 좌표·해시는 일치한다. 원본 포착의 미완료와 구분해 "
                 "이 범위만 검토할 수 있다. 원본 소실을 no_value의 사유로 쓰거나 미포착 꼬리를 ACK하지 않는다.\n")
    elif st["raw_review"]["state"] == "unavailable":
        text += ("저장된 raw 검증 실패로 이번 범위의 증류·ACK를 보류한다. 원본 좌표·해시를 복구한 뒤 "
                 f"다시 검토한다: {st['raw_review']['error']}\n")
    if st["failed_rounds"] or st["interrupted_rounds"]:
        text += (f"원문에 실패 종료 {st['failed_rounds']}·종료 기록 없이 다음 턴으로 넘어간 부분 기록 "
                 f"{st['interrupted_rounds']}건이 포함된다. 관측 보존이며 작업 성공을 뜻하지 않는다.\n")
    if st["harness"] == "codex":
        text += ("native_trigger는 goal·heartbeat·다른 대화에서 전달된 메시지 또는 실패·중단 뒤 입력 없는 턴이다. 앞 입력은 문맥으로만 "
                 "보존하며 같은 의도의 재개라고 확정하지 않는다. 새 사용자 발화로 해석하지 말고, "
                 "명시 지시 증류와 자율 성장의 증거를 구분하라.\n")
    if st["inherited_rounds"] and st["harness"] == "codex":
        text += (f"fork 부모 대화의 과거 {st['inherited_rounds']}턴은 부모의 몫이다 — 이 대화의 검토 대상이 "
                 "아니고, 부모의 검토 대기도 그대로 남는다. 부모가 이 기기에서 추적되지 않으면 그 턴은 부모 "
                 "대화를 capture해 그쪽에서 검토한다.\n")
    elif st["inherited_rounds"]:
        text += (f"같은 scope의 부모 대화가 이미 추적한 과거 {st['inherited_rounds']}라운드는 이 대화의 "
                 "검토 대상이 아니다. 부모의 미검토 대기는 그대로 남는다.\n")
    if (st.get("coverage") or {}).get("ancestor_pending_tails"):
        text += ("fork 부모의 선언된 history 범위에 미완료 꼬리가 있다. 자식의 종료로 부모 턴을 "
                 "완료하거나 ACK하지 않는다. 부모 원본·검토 대기를 별도로 확인한다.\n")
    if st.get("repair"):
        text += ("이 snapshot은 이미 검토했지만 저장 영수증의 최종 확인이 실패해 복구 대기로 남았다. "
                 "새 대화의 완료 커서는 되감지 않았다. 아래 기존 출처와 영수증을 다시 확인하고 "
                 "같은 through로 명시적으로 재ACK하라. 허브 연결만 빠졌으면 기존 증류를 resume한다. "
                 "본문 정정이 필요하면 현재 노드를 재검토하고 별도 증류 key로 새 증거를 만든다. "
                 f"보류 사유: {st['repair']['reason']}\n")
    from . import organization
    jobs = []
    try:
        with core.mutation_lock():
            scope = write.resolve_session(st["session"]) if include_organization and st.get("session") else None
            jobs = organization.pending([scope], limit=1, record=True) if scope else []
    except (OSError, ValueError, write.WriteError) as exc:
        text += f"참조·조직 검토는 대기 중이다: {exc}. 아래 대화 통합은 계속한다.\n"
    st["organization_jobs"] = jobs
    organization_text = organization.prompt(jobs)
    if not organization_in_text:
        organization_text = ""  # the hook budgets organization_jobs as its own block (`osk.hook_text`)
    if not st["pending_refs"]:
        return {**st, "text": text + "검토할 완료 라운드가 아직 없다. 종료 꼬리는 같은 대화 재개 또는 명시 capture로 따라잡는다." + organization_text}
    command = core.cli_command("integration", "review", "--harness", harness,
                               "--conversation", conversation_id)
    from . import distillation
    try:
        discovered = distillation.discover(st["pending_refs"])
    except (ValueError, OSError, write.WriteError) as exc:
        discovered = {"proofs": [], "errors": [{"reason": str(exc)}], "truncated": False, "scanned": 0}
    st["previous_distillations"] = discovered["proofs"]
    st["proof_discovery"] = {k: discovered[k] for k in ("errors", "truncated", "scanned")}
    if discovered["proofs"]:
        text += ("기존 저장 증거가 있다. complete이면 본문을 다시 쓰지 말고 기존 key를 "
                 "preserved ACK의 targets에 재사용하라. 허브 연결만 pending이면 "
                 "update_node(name=proof.target.id, distill={resume:proof.key})를 부른 뒤 "
                 "같은 key로 ACK하라. body/summary/edges/anchors/settle/expect_hash는 보내지 않는다. "
                 "아래 증거의 실제 내용이 이번 검토 범위에 맞는지 먼저 확인하라.\n"
                 + json.dumps(discovered["proofs"], ensure_ascii=False) + "\n")
    if discovered["errors"] or discovered["truncated"]:
        text += ("기존 증류 증거 조회가 불완전하다. 기존 저장 여부를 확인하기 전 "
                 "중복 노드를 새로 만들지 말고 필요하면 deferred로 남겨라. 진단: "
                 + json.dumps(st["proof_discovery"], ensure_ascii=False) + "\n")
    text += (f"이번 검토 snapshot의 작업 키: {st['key']}\n"
             f"새 증류의 distill.key는 `{st['key']}:<대상별 고정 접미사>`로 정하라. "
             "기존 노드 ID 또는 새 노드 제목의 고정 slug를 접미사로 쓰고, 같은 대상의 "
             "재시도에서는 그대로 재사용한다. 여러 대상에 같은 key를 쓰지 않는다. "
             "새 snapshot은 새 작업 키를 쓴다. ACK만 유실됐다면 위 complete 증거의 "
             "기존 key를 targets에 재사용하며, 같은 본문을 새 key로 다시 쓰지 않는다.\n")
    stored = [ref for ref in st["pending_refs"] if not raw.is_native(ref)]
    text += "search로 기존 노드를 찾는다. " + write.CLAIM_GUIDANCE + "오래 쓸 지식만 Scope 노드로 옮긴다. "
    turns = len(st["pending_refs"]) - len(stored)
    if turns and offline and st.get("repair"):
        # A receipt repair needs no dialogue.
        text += (f"원본 턴 {turns}개의 원문은 읽지 않는다 — 기존 출처·허브 영수증만 다시 확인하고 "
                 "대화 내용을 새로 판단하지 않는다.\n")
    elif turns and offline:
        # The conversation ended before its own review: read only these turns from this
        # device's transcript, earlier context only on demand (Mechanism §9-4 3항). MCP
        # reads them; a sandboxed worker cannot run the CLI.
        text += (f"원본 턴 {turns}개는 검토 시점 전에 끝난 대화의 미검토 턴이다. 각 턴을 "
                 "read_cited(ref=<그 원본 턴>)로 이 기기의 원본 전사에서 그 턴만 읽는다. "
                 "턴마다 선별본(≤6000자)과 바로 앞 턴의 `previous`가 온다. 앞선 맥락은 판단에 꼭 필요할 때만 "
                 "`previous`로 한 턴씩 거슬러 읽고, 대화 전체를 읽지 않는다. 노드로 옮길 지식이 나온 턴만 "
                 f"cite_round(conversation=\"{harness}/{conversation_id}\", turn=<턴 ID>)로 인용하고, 받은 "
                 "round_ref를 distill.sources로 써 출처와 허브 Link를 완성한다.\n")
    elif turns:
        # The reviewer holds these turns in context. Citing is the only vault write of
        # dialogue, and only for evidence a node needs (Mechanism §9 9항).
        text += (f"원본 턴 {turns}개는 이 대화 문맥에 있다 — 다시 읽지 않고 "
                 "현재 scope_memory와 함께 후보를 찾는다. 노드로 옮길 지식이 나온 턴만 "
                 f"cite_round(conversation=\"{harness}/{conversation_id}\", quote=그 턴 사용자 발화의 짧은 일부)로 "
                 "인용하고, 받은 round_ref를 distill.sources로 써 출처와 허브 Link를 완성한다.\n")
    if stored:
        text += ("저장된 기록은 현재 scope_memory와 함께 read_cited(view=review) 선별본으로 본다. "
                 "보존할 주장이나 모순을 확인할 때만 query로 필요한 원료 근거를 좁혀 읽고, 원본 hash를 "
                 "출처에 쓴다. 선별본 생략은 무가치의 증거가 아니며, 판정 근거가 부족하면 deferred로 남긴다. "
                 "max_chars 증가·기록 전량 이어읽기·전사 전체 shell 출력으로 우회하지 않는다. "
                 "선택한 기록 출처와 허브 Link를 distill로 완성한다.\n" + "\n".join(stored) + "\n")
    text += "남길 지식이 없는 라운드까지 노드에 억지로 넣지 않는다.\n"
    text += (f"실행 명령({ 'PowerShell' if os.name == 'nt' else 'shell' }):\n{command}\n위 명령에 UTF-8 JSON을 stdin으로 전달하라: "
             + json.dumps({"through": st["through"], "outcome": "preserved|summary|no_value|deferred",
                           "reason": "검토 범위와 선택/생략 이유", "targets": [{"key": "완료한 distill key"}]}, ensure_ascii=False)
             + "\npreserved는 실제 노드·출처·허브 완료 영수증을 확인한다. summary는 현재 공유 기억의 "
             "정확한 발췌를 targets=[{text:...}]로 제출하며 노드 보존 성공으로 세지 않는다. "
             "no_value도 사유를 남기고, deferred는 대기를 유지한다. 기억 hash 변화만으로 완료되지 않는다. "
             "기계 검사는 저장·배선만 확인하며 의미 타당성은 원문과 따로 대조한다.")
    return {**st, "text": text + organization_text}


def _conversation(value: str) -> tuple[str, str]:
    """`<harness>/<conversation ID>` — the pair the hooks show the session."""
    harness, sep, sid = (value or "").strip().partition("/")
    if not sep:
        raise ValueError("conversation must be <harness>/<conversation ID>")
    _identity(harness, sid)
    return harness, sid


def cite(conversation: str, quote: str | None = None, turn: str | int | None = None,
         note: str | None = None, session: str | None = None, space: str | None = None,
         user: str | None = None) -> dict:
    """Keep one original turn as evidence (시행령 §2 2항 · Mechanism §9 9항). The engine
    copies the user's words from the harness transcript and keeps the agent's reply as
    location and hash; the caller names the turn and never resends its text."""
    harness, sid = _conversation(conversation)
    with _locked(harness, sid) as p:
        s = _load(p, harness, sid)
    session = session or s["session"]
    if not session:
        raise ValueError("this conversation is not tracked yet; its hooks or `integration capture` set its session")
    source = _locate_transcript(harness, sid, s.get("capture_path") or s["transcript_path"])
    if not (source and Path(source).is_file()):
        source = None
    if user is not None and source:
        raise ValueError("the original transcript is readable; name the turn by quote or turn")
    if user is None:
        if not source:
            raise ValueError("original transcript unavailable; pass user= to keep caller-supplied words")
        parsed = transcripts.read(source, harness, sid)
        shown = parsed.get("dialogue_v1") or {}
        turns = [{"id": turn_key(harness, r["id"]), "user": shown.get(r["id"], r)["user"],
                  "agent": shown.get(r["id"], r)["agent"], "owner": r.get("origin_conversation_id") or sid}
                 for r in parsed["rounds"]]
        tail = parsed.get("tail")
        if tail and tail.get("user") and turn_key(harness, tail["id"]) not in {t["id"] for t in turns}:
            turns.append({"id": turn_key(harness, tail["id"]), "user": tail["user"], "agent": None, "owner": sid})
        if isinstance(turn, str) and not re.fullmatch(r"-?\d+", turn.strip()):
            matched = [t for t in turns if t["id"] == turn_key(harness, turn.strip())]
        elif turn is not None:
            n = int(turn)
            if n >= 0:
                raise ValueError("a relative turn counts back from the latest: -1, -2, ...")
            matched = turns[n:n + 1 or None] if -n <= len(turns) else []
        elif quote and quote.strip():
            needle = " ".join(quote.split())
            matched = [t for t in turns if needle in " ".join(t["user"].split())]
        else:
            matched = turns[-1:]
        if len(matched) != 1:
            seen = [f"{t['id']}: {' '.join(t['user'].split())[:40]}" for t in (matched or turns)[-5:]]
            raise ValueError(f"{len(matched)} turns match; narrow by quote or turn. Candidates: {seen}")
        words, rid, agent, by = matched[0]["user"], matched[0]["id"], matched[0]["agent"], "engine"
        owner = matched[0]["owner"]
    else:
        if not user.strip():
            raise ValueError("user= needs the words to keep")
        words, agent, by, owner = user, None, "caller", sid
        rid = (turn_key(harness, turn.strip()) if isinstance(turn, str) and turn.strip() else
               "caller-" + core.sha256_bytes(user.encode("utf-8")).removeprefix("sha256:")[:16])
    meta = {"harness": harness, "conversation": owner, "turn": rid, "source": source,
            "agent_sha256": core.sha256_bytes(agent.encode("utf-8")) if agent else None, "user_by": by}
    record = s["record"]
    if owner != sid:
        # A fork parent's turn keeps its owner: the citation names the parent and joins
        # the parent's record, so the parent citing the same turn reuses that round.
        with _locked(harness, owner) as pp:
            record = _load(pp, harness, owner)["record"]
    result = raw.append_rounds(session, record, [{"user": words, "agent": (note or "").strip()}],
                               space or s["space"], cited=meta)
    out = {"ok": True, "round_ref": result["round_refs"][0], "path": result["path"],
           "index": result["indices"][0], "turn": rid, "reused": result.get("reused", False),
           "user_by": by, "agent_sha256": meta["agent_sha256"], "filtered": result["filtered"],
           **({"binding": result["binding"]} if "binding" in result else {})}
    if out["reused"]:
        # The record is append-only: report what it holds, and what this read saw apart.
        kept = result["cited"]
        out.update(user_by=kept.get("user_by"), agent_sha256=kept.get("agent_sha256"))
        seen = {k: v for k, v in (("user_by", by), ("agent_sha256", meta["agent_sha256"])) if kept.get(k) != v}
        if seen:
            out["observed"] = seen
        if (note or "").strip():
            out["note_saved"] = False
    return out


def read_turns(refs: list, max_chars: int = 6000, view: str = "review",
               query: str | None = None) -> dict:
    """Original turns from this device's transcript, for a reviewer without the conversation.
    Only the named turns are read, never the whole conversation; each names the turn
    before it as `previous`, for earlier context only when a judgment needs it
    (Mechanism §9-4 3항). Nothing is written."""
    from . import raw_view
    if view not in {"review", "full"} or not isinstance(refs, list) or not 1 <= len(refs) <= MAX_REVIEW_ROUNDS:
        raise ValueError(f"1..{MAX_REVIEW_ROUNDS} original-turn refs and view review|full are required")
    out, conversations = [], {}
    for ref in refs:
        parts = ref.split(":", 3) if raw.is_native(ref) else []
        if len(parts) != 4:
            raise ValueError("ref must be native:<harness>:<conversation ID>:<turn ID>")
        _, harness, sid, turn = parts
        if (harness, sid) not in conversations:
            with _locked(harness, sid) as p:
                s = _load(p, harness, sid)
            source = _locate_transcript(harness, sid, s.get("capture_path") or s["transcript_path"])
            if not (source and Path(source).is_file()):
                raise ValueError(f"the transcript of {harness}/{sid} is not on this device")
            parsed = transcripts.read(source, harness, sid)
            shown = parsed.get("dialogue_v1") or {}
            tracked = {turn_key(harness, r["id"]): r["hash"] for r in s["rounds"] if raw.is_native(r["ref"])}
            conversations[harness, sid] = tracked, [
                (turn_key(harness, r["id"]), shown.get(r["id"], r), _turn_hashes(r, parsed.get("dialogue_v1")),
                 r.get("origin_conversation_id") or sid) for r in parsed["rounds"]]
        tracked, turns = conversations[harness, sid]
        at = next((i for i, (key, *_) in enumerate(turns) if key == turn), None)
        if at is None:
            raise ValueError(f"{turn} is not a completed turn of {harness}/{sid}")
        _, pair, hashes, owner = turns[at]
        if owner != sid:
            raise ValueError(f"{turn} belongs to the fork parent; read {raw.native_ref(harness, owner, turn)}")
        chunk = raw._block(at + 1, pair["user"], pair["agent"]).rstrip("\n")
        item = {"ref": ref, "turn": turn, "hash": hashes[0], "position": at + 1, "turns": len(turns),
                "previous": raw.native_ref(harness, turns[at - 1][3], turns[at - 1][0]) if at else None}
        if turn in tracked and tracked[turn] not in hashes:
            # Changed since tracking: its review waits for the next capture's snapshot.
            item["changed"] = True
        if view == "review":
            item.update(raw_view.project(chunk, max_chars, query))
        else:
            cut = len(chunk) > max_chars
            item.update(view="full", chars=len(chunk), truncated=cut,
                        text=chunk[:max_chars] if cut else chunk)
        out.append(item)
    return {"ok": True, "turns": out}


class SubagentEvent(Exception):
    """Codex subagent hooks send the root session_id with the child's rollout. Not a
    ValueError: no fallback may record it in the root conversation's state."""


def hook_source(env: dict) -> tuple[str, str, str | None]:
    """Locate only the caller's native transcript, never another conversation's backlog."""
    sid = adapters.session_id(env)
    path = env.get("transcript_path")
    if not sid:
        raise ValueError("hook has no actual conversation ID; capture not acknowledged")
    harness = adapters.detect(env, sid, path)
    candidates = []
    if not path:
        for kind in ([harness] if harness else list(adapters.NAMES)):
            _identity(kind, sid)
            saved = _locate_transcript(kind, sid, status(kind, sid).get("transcript_path"))
            if saved:
                candidates.append((kind, saved))
        if len(candidates) == 1:
            harness, path = candidates[0]
        elif len(candidates) > 1:
            raise ValueError("multiple transcripts for this ID; provide explicit harness/transcript_path")
    if not harness:
        raise ValueError("native harness/transcript unavailable; provide harness and transcript_path")
    _identity(harness, sid)
    reason = adapters.get(harness).subagent(path, sid) if path and Path(path).is_file() else None
    if reason:
        raise SubagentEvent(reason)
    return harness, sid, path


def recent_transcript(harness: str) -> str | None:
    """이 vault가 가장 최근에 포착한 그 하네스 대화의 전사 — `doctor`가 판본을 읽는다.
    상태를 쓰지 않는다."""
    probe = state_path(harness, "inventory")
    prefix = "-".join(probe.name.split("-")[:3]) + "-"
    dated = []
    for p in probe.parent.glob(prefix + "*.json"):
        try:
            dated.append((p.stat().st_mtime_ns, p))
        except OSError:
            continue                     # 그 사이 사라진 상태
    for _, p in sorted(dated, reverse=True):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        path = s.get("transcript_path") if isinstance(s, dict) and s.get("harness") == harness else None
        if isinstance(path, str) and Path(path).is_file():
            return path
    return None


def hook_capture(env: dict, session: str) -> dict:
    harness, sid, path = hook_source(env)
    return capture(harness, sid, path, session, env.get("space"))
