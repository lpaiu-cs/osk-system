"""Durable, local conversation capture/review cursors; shared scope memory is not an ACK."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from . import core, raw, scope_memory, transcripts, write
from ._portalock import lock_exclusive, unlock

SOFT, HARD = 9, 15
MAX_REVIEW_ROUNDS = 15


def _identity(harness: str, conversation_id: str) -> str:
    if harness not in ("claude", "codex") or not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", conversation_id):
        raise ValueError("explicit claude/codex harness and actual conversation_id required")
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
        record_key = hashlib.sha256((harness + "\n" + sid).encode()).hexdigest()[:32]
        return {"version": 1, "root": str(core.ROOT.resolve()), "harness": harness,
                "conversation_id": sid, "session": None, "space": None, "transcript_path": None,
                "record": f"{harness}-{record_key}",
                "rounds": [], "reviewed_count": 0, "prompt_count": 0,
                "reviewed_prompt_count": 0, "snapshots": {}, "reviews": [],
                "capture_pending": False, "capture_error": None,
                "native_fingerprint": None, "coverage": None}
    s = json.loads(p.read_text(encoding="utf-8"))
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
            "coverage": s.get("coverage"),
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


def _inherited_rounds(s: dict, rounds: list, dialogue_v1: dict | None = None) -> list:
    """Reuse only the caller's copied prefix, with native ID and byte evidence."""
    if s["harness"] != "claude" or not s["space"]:
        return rounds
    scope = raw._scope_of_space(s["space"])
    path = raw.record_path(scope, s["record"])
    inherited = raw.inherited_prefix(path) or s.get("inherited")
    if not inherited and (path.exists() or s["rounds"]):
        return rounds  # Existing duplicate raw is immutable; do not rewrite its codec.
    cache = {}

    def matches(pair, source):
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
        agent = locator.sub(
            lambda m: m[1] + origins.get(m[3], m[2]) + ":" + m[3] + m[4]
            if m[2] == s["conversation_id"] and references["claude:" + m[2] + ":" + m[3]]
            else m[0], pair["agent"])
        from . import secrets
        expected = raw._block(number, raw.escape_numeric_h2(pair["user"]), raw.escape_numeric_h2(agent),
                              dialogue_id=pair["id"] if readable else None)
        return raw._round_body(block) == raw._round_body(secrets.filter_text(expected)[0])

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
                        "Scope/" + (write.resolve_session(owner["session"]) or ""))
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
    if harness == "claude":
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        matches = list((base / "projects").glob(f"*/{sid}.jsonl"))
    else:
        base = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        matches = []
        for folder, prefix in ((base / "sessions", "*/*/*/"), (base / "archived_sessions", "")):
            for suffix in (f"*-{sid}.jsonl", f"*-{sid}_*.jsonl"):
                matches.extend(folder.glob(prefix + suffix))
    matches = sorted({p.resolve() for p in matches if p.is_file()})
    if len(matches) > 1:
        raise ValueError("multiple transcripts for this ID; provide explicit harness/transcript_path")
    # transcripts.read checks the native identity before this path is saved.
    return str(matches[0]) if matches else saved


def capture(harness: str, conversation_id: str, transcript_path: str | None,
            session: str, space: str | None = None) -> dict:
    with _locked(harness, conversation_id) as p:
        s = _load(p, harness, conversation_id)
        s["capture_pending"], s["capture_error"] = True, None
        _save(p, s)  # Persist intent before reading or appending; failures remain pending.
        try:
            pinned = s["space"]
            if not pinned and s["rounds"]:
                name, _ = raw.parse_ref(s["rounds"][0]["ref"])
                pinned = "/".join(raw._raw_file(name).relative_to(core.ROOT).parts[:2])
            if pinned:
                scope = raw._scope_of_space(pinned)
                if not scope:
                    raise ValueError("saved conversation scope is invalid; capture remains pending")
                pinned = "Scope/" + scope
            destination, bound = write.resolve_landing(session, space, raw._CONFINE)
            requested = "Scope/" + destination if destination else None
            if pinned and not requested and s["session"]:
                # A generic cwd key must not bind unrelated conversations.
                # Resume this native ID using its explicitly chosen session.
                session = s["session"]
                destination, bound = write.resolve_landing(session, pinned, raw._CONFINE)
                requested = "Scope/" + destination
            if (pinned and requested and pinned != requested
                    or s["session"] and s["session"] != session
                    and not (bound and requested == pinned or not pinned and requested)):
                raise ValueError("conversation scope changed; existing capture was not moved")
            s["session"] = session
            s["space"] = pinned or requested
            if transcript_path:
                s["transcript_path"] = str(Path(transcript_path).resolve())
            _save(p, s)
            native_path = _locate_transcript(harness, conversation_id, s["transcript_path"])
            if not native_path:
                raise ValueError("native transcript path unavailable; use integration capture with --transcript")
            parsed = transcripts.read(native_path, harness, conversation_id)
            s["transcript_path"] = native_path
            s["coverage"] = parsed["coverage"]
            _save(p, s)  # Reference-only coverage is visible before raw capture.
            # ponytail: serialize capture until its cursor is saved; use per-scope
            # locks if capture throughput matters. Raw owns the mutation lock.
            with _locked_path(core.local_lock_path("osk-capture-prefix.lock")):
                rounds = _inherited_rounds(s, parsed["rounds"], parsed.get("dialogue_v1"))
                if harness == "codex" and parsed.get("codex_v2") is not None and s["space"]:
                    # Old raw coordinates/ACKs are immutable. Newly supported
                    # historical turns append after them, with native IDs in raw.
                    path = raw.record_path(raw._scope_of_space(s["space"]), s["record"])
                    order = raw.codex_capture_order(path, parsed["codex_v1"], parsed["codex_v2"],
                                                    tuple(r["id"] for r in s["rounds"]))
                    by_id = {r["id"]: r for r in rounds}
                    if len(by_id) != len(rounds) or any(rid not in by_id for rid in order):
                        raise ValueError("native round identity prefix changed; existing raw was not altered")
                    known = set(order)
                    rounds = [by_id[rid] for rid in order] + [r for r in rounds if r["id"] not in known]
                _save(p, s)  # Keep prefix ownership across a crash before raw append.
                ids = [r["id"] for r in rounds]
                if len(ids) != len(set(ids)) or ids[:len(s["rounds"])] != [r["id"] for r in s["rounds"]]:
                    raise ValueError("native round identity prefix changed; existing raw was not altered")
                if rounds:
                    result = raw.append_rounds(session, s["record"], rounds, s.get("space"),
                                               replay_prefix=True, codex_v1=parsed.get("codex_v1"),
                                               codex_v2=parsed.get("codex_v2"),
                                               dialogue_v1=parsed.get("dialogue_v1"),
                                               inherited=s.get("inherited"))
                    s["coverage"]["codex_v1_rounds"] = result.get("codex_v1_rounds", [])
                    stored = raw.read_exact(raw._raw_file(result["path"]))
                    spans = raw._round_spans(stored)
                    # Existing snapshots bind these strings: moving raw must not
                    # change an old review token or pretend the round was reviewed.
                    prior_refs = {r["id"]: r["ref"] for r in s["rounds"]}
                    s["rounds"] = [{"id": r["id"], "ref": prior_refs.get(r["id"], ref), "completion": r["completion"],
                                    "hash": core.sha256_bytes(stored[slice(*spans[i])].rstrip("\n").encode("utf-8"))}
                                   for i, r, ref in zip(result["indices"], rounds, result["round_refs"])]
                    token = _snapshot(s)
                    s["snapshots"].setdefault(token, {"count": len(rounds), "prompt_count": s["prompt_count"]})
                else:
                    result = {"appended": 0}
                s["capture_pending"] = parsed["pending_tail"]
                s["capture_error"] = "; ".join(parsed["diagnostics"]) or None
                s["native_fingerprint"] = parsed["native_fingerprint"]
                _save(p, s)
                return {**_current_view(s, p), "appended": result.get("appended", 0)}
        except Exception as exc:
            s["capture_pending"], s["capture_error"] = True, f"{type(exc).__name__}: {exc}"
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
    raw_files = {}
    for r in rounds:
        name, index = raw.parse_ref(r["ref"])
        if name not in raw_files:
            text = raw.read_exact(raw._raw_file(name))
            raw_files[name] = text, raw._round_spans(text)
        text, spans = raw_files[name]
        if index not in spans or core.sha256_bytes(
                text[slice(*spans[index])].rstrip("\n").encode("utf-8")) != r["hash"]:
            raise ValueError("raw snapshot changed; review is not acknowledged")


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
        receipts = []
        if outcome == "preserved":
            from . import distillation
            if not isinstance(targets, list) or not targets:
                raise ValueError("preserved requires completed distillation targets [{key:...}]")
            for target in targets:
                if not isinstance(target, dict) or not isinstance(target.get("key"), str):
                    raise ValueError("preserved target must name a distillation key")
                receipt = distillation._status_locked(target["key"])
                if receipt.get("status") != "complete" or not {raw.canonical_ref(ref) for ref in refs}.intersection(
                        raw.canonical_ref(r["ref"]) for r in receipt.get("sources", [])):
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
                native_path = _locate_transcript(s["harness"], s["conversation_id"], s.get("transcript_path"))
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
            if current["pending"] or changed:
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
        job = prompt(s["harness"], s["conversation_id"])
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
            result = capture(s["harness"], s["conversation_id"], s["transcript_path"], s["session"], s.get("space"))
            captures.append({k: result.get(k) for k in ("harness", "conversation_id", "ok", "appended", "capture_error")})
            if result["pending_refs"]:
                job = prompt(s["harness"], s["conversation_id"], include_organization=False,
                             max_rounds=max_rounds)
                job["prompt"] = job.pop("text")
                jobs.append(job)
        except Exception as exc:
            errors.append({"harness": s["harness"], "conversation_id": s["conversation_id"], "error": str(exc)})
    return {"ok": not errors and all(r["ok"] for r in captures), "jobs": jobs,
            "captures": captures, "remaining": remaining, "errors": errors,
            "awaiting_native": awaiting_native}


def prompt(harness: str, conversation_id: str, *, include_organization: bool = True,
           max_rounds: int = MAX_REVIEW_ROUNDS) -> dict:
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= MAX_REVIEW_ROUNDS:
        raise ValueError(f"max_rounds must be between 1 and {MAX_REVIEW_ROUNDS}")
    with _locked(harness, conversation_id) as p:
        s = _load(p, harness, conversation_id)
        st = _current_view(s, p)
        count = min(len(s["rounds"]), s["reviewed_count"] + max_rounds)
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
    if st["failed_rounds"] or st["interrupted_rounds"]:
        text += (f"원문에 실패 종료 {st['failed_rounds']}·종료 기록 없이 다음 턴으로 넘어간 부분 기록 "
                 f"{st['interrupted_rounds']}건이 포함된다. 관측 보존이며 작업 성공을 뜻하지 않는다.\n")
    if st["harness"] == "codex":
        text += ("native_trigger는 goal·heartbeat 또는 실패·중단 뒤 입력 없는 턴이다. 앞 입력은 문맥으로만 "
                 "보존하며 같은 의도의 재개라고 확정하지 않는다. 새 사용자 발화로 해석하지 말고, "
                 "명시 지시 증류와 자율 성장의 증거를 구분하라. 복구된 과거 턴은 "
                 "기존 좌표 뒤에 추가될 수 있으므로 raw 번호만으로 발생 시각을 추론하지 않는다.\n")
    if st["inherited_rounds"]:
        text += (f"같은 scope에 이미 보존된 과거 {st['inherited_rounds']}라운드는 원래 raw를 참조한다. "
                 "새 포착·이 대화의 검토 완료로 세지 않는다. 부모의 미검토 대기는 그대로 남는다.\n")
    if st.get("repair"):
        text += ("이 snapshot은 이미 검토했지만 저장 영수증의 최종 확인이 실패해 복구 대기로 남았다. "
                 "새 대화의 완료 커서는 되감지 않았다. 아래 기존 출처와 영수증을 다시 확인하고 "
                 "같은 through로 명시적으로 재ACK하라. 허브 연결만 빠졌으면 기존 증류를 resume한다. "
                 "본문 정정이 필요하면 현재 노드를 재검토하고 별도 증류 key로 새 증거를 만든다. "
                 f"보류 사유: {st['repair']['reason']}\n")
    if (st.get("coverage") or {}).get("mode") == "tool-output-reference":
        text += "포착 범위: 파일 읽기·혼합 명령 결과는 native 위치와 hash 참조로 보존했다. 상세 증거를 다시 읽으려면 원래 전사 보관이 필요하다.\n"
    if (st.get("coverage") or {}).get("codex_v1_rounds"):
        text += "포착 범위: 과거 Codex 라운드는 당시 user_message 형식 그대로 보존했다. 옛 포착기가 생략한 native 입력의 상세는 원래 전사를 확인하라.\n"
    from . import organization
    jobs = []
    try:
        with core.mutation_lock():
            scope = write.resolve_session(st["session"]) if include_organization and st.get("session") else None
            jobs = organization.pending([scope], limit=1, record=True) if scope else []
    except (OSError, ValueError, write.WriteError) as exc:
        text += f"참조·조직 검토는 대기 중이다: {exc}. 아래 raw 통합은 계속한다.\n"
    st["organization_jobs"] = jobs
    organization_text = organization.prompt(jobs)
    if not st["pending_refs"]:
        return {**st, "text": text + "검토할 완료 raw 라운드가 아직 없다. 종료 꼬리는 같은 대화 재개 또는 명시 capture로 따라잡는다." + organization_text}
    code = (f"import os,runpy,sys;os.environ['OSK_VAULT_ROOT']={str(core.ROOT)!r};"
            f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r});"
            "runpy.run_module('osk.cli',run_name='__main__')")
    argv = [sys.executable, "-c", code, "integration", "review", "--harness", harness, "--conversation", conversation_id]
    command = ("& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
               if os.name == "nt" else shlex.join(argv))
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
    text += ("현재 scope_memory와 아래 raw의 read_raw(view=review) 선별본으로 후보를 찾는다. "
             "이미 이 대화에서 확인한 근거를 전량 재독하지 않는다. 보존할 주장이나 모순을 "
             "확인할 때만 query로 필요한 원료 근거를 좁혀 읽는다. 원본 hash를 출처에 쓴다. "
             "선별본 생략은 무가치의 증거가 아니며, 판정 근거가 부족하면 deferred로 남긴다. "
             "max_chars 증가·raw 전량 이어읽기·전사 전체 shell 출력으로 우회하지 않는다. "
             "search로 기존 노드를 찾는다. " + write.CLAIM_GUIDANCE +
             "오래 쓸 지식만 Scope 노드로 옮기며 선택한 raw 출처와 허브 Link를 distill로 완성한다. "
             "남길 지식이 없는 라운드까지 노드에 억지로 넣지 않는다.\n"
             + "\n".join(st["pending_refs"]) + "\n"
             + f"실행 명령({ 'PowerShell' if os.name == 'nt' else 'shell' }):\n{command}\n위 명령에 UTF-8 JSON을 stdin으로 전달하라: "
             + json.dumps({"through": st["through"], "outcome": "preserved|summary|no_value|deferred",
                           "reason": "검토 범위와 선택/생략 이유", "targets": [{"key": "완료한 distill key"}]}, ensure_ascii=False)
             + "\npreserved는 실제 노드·출처·허브 완료 영수증을 확인한다. summary는 현재 공유 기억의 "
             "정확한 발췌를 targets=[{text:...}]로 제출하며 노드 보존 성공으로 세지 않는다. "
             "no_value도 사유를 남기고, deferred는 대기를 유지한다. 기억 hash 변화만으로 완료되지 않는다. "
             "기계 검사는 저장·배선만 확인하며 의미 타당성은 raw와 따로 대조한다.")
    return {**st, "text": text + organization_text}


def hook_source(env: dict) -> tuple[str, str, str | None]:
    """Locate only the caller's native transcript, never another conversation's backlog."""
    sid = env.get("session_id") or env.get("conversation_id") or os.environ.get("CODEX_THREAD_ID")
    harness = env.get("harness") or os.environ.get("OSK_HARNESS")
    path = env.get("transcript_path")
    if not harness and os.environ.get("CODEX_THREAD_ID") == sid:
        harness = "codex"
    if not harness and path and sid and Path(path).stem == sid:
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"
        if Path(path).resolve().is_relative_to(base.resolve()):
            # A fresh Claude file may not exist until after SessionStart.
            harness = "claude"
    if not harness and path:
        with Path(path).open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("type") == "session_meta":
                    harness = "codex"
                    break
                if row.get("sessionId"):
                    harness = "claude"
                    break
    if not sid:
        raise ValueError("hook has no actual conversation ID; capture not acknowledged")
    candidates = []
    if not path:
        for kind in ([harness] if harness else ["claude", "codex"]):
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
    return harness, sid, path


def hook_capture(env: dict, session: str) -> dict:
    harness, sid, path = hook_source(env)
    return capture(harness, sid, path, session, env.get("space"))
