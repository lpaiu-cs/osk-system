"""osk.conversation_capture — 하네스의 전사를 대화의 라운드로 포착한다 (Mechanism §9-4).

전사 파일을 찾고, 어느 턴까지가 끝난 턴인지 정하고, 새 라운드를 인용 기록과 커서에 옮긴다. 대화의
착지와 일회성 작업 폴더의 판별, 상태를 바깥에 보이는 꼴(`state_view`·`status`), 훅이 부르는 입구도
여기 있다."""
from __future__ import annotations
import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from . import conversation_state, core, raw, secrets, transcripts, workspaces, write, transcript_files, locks
from . import harness as adapters
from .vault import Vault


SOFT, HARD = 9, 15


ENDED_AFTER = 12 * 3600  # seconds without a transcript change: the conversation ended (Mechanism §9-4 3항)


SOURCE_LOST_AFTER = 7 * 86400  # seconds a native transcript stays missing before its cursor closes as lost (#244)


def _capture_recovery(vault: Vault, s: dict) -> dict:
    """Advice only; it never relaxes capture identity or acknowledges stored raw."""
    if s.get("source_lost"):
        return {"state": "closed", "phase": "source_lost", "basis": "source_lookup",
                "next_action": "원본이 다시 나타나면 같은 대화를 capture해 이어 간다. 검토하지 못한 원본 턴은 "
                               "`source_lost.refs`에 남아 있다."}
    phase = s.get("capture_failure_phase", "unknown")
    if s.get("capture_error"):
        basis = "recorded_phase" if phase != "unknown" else "legacy_diagnostic"
        if phase == "unknown":
            error = s["capture_error"]
            if any(t in error for t in ("착지", "프로젝트 키", "프로젝트 키", "scope changed", "project changed", "scope is invalid")):
                phase = "landing"
            elif any(t in error for t in ("prefix", "접두부", "저장된 기록보다 짧다")):
                phase = "replay"
            elif any(t in error for t in ("identity", "session_meta", "sessionId", "history_base")):
                phase = "read"
            elif "FileNotFoundError" in error or "native transcript path unavailable" in error:
                phase = "source"
        if phase == "source":
            # A recorded miss is checked too: Codex compresses a cold rollout in place (#203).
            try:
                found = locate_transcript(vault, s["harness"], s["conversation_id"],
                                           s.get("capture_path") or s.get("transcript_path"))
                available = bool(found and Path(found).is_file())
                missing = not found or not Path(found).exists()
            except (OSError, ValueError):
                basis = "source_lookup_failed"
            else:
                basis = "source_lookup"
                if available:
                    return {"state": "retryable", "phase": phase, "basis": basis,
                            "next_action": "같은 ID의 원본 후보(Codex 압축본 포함)를 찾았다. 현재 엔진으로 capture하여 신원·접두부를 다시 검증한다."}
                if missing and not (s["rounds"] or s["prompt_count"] or s.get("native_fingerprint")):
                    phase = "awaiting_native"
        actions = {
            "awaiting_native": "같은 대화의 원본이 생성된 뒤 다시 capture한다.",
            "landing": "기존 대화의 scope를 유지하며 명시적인 프로젝트 키와 착지를 확인한다.",
            "source": "이 기기에 같은 대화 ID의 원본이 평문으로도 Codex 압축본(`.jsonl.zst`)으로도 없다. 원본을 복구하거나 올바른 전사 경로를 명시한다.",
            "dependency": "원본은 있으나 Codex 압축본(`.jsonl.zst`)을 풀 `zstandard`가 이 vault의 .venv에 없다. `_governance/_engine/requirements.txt`를 다시 설치한 뒤 같은 대화를 capture한다.",
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


def state_view(vault: Vault, s: dict) -> dict:
    rs, n = s["rounds"], s["reviewed_count"]
    repairs = s.get("repair_pending", {})
    repair_refs = {ref for item in repairs.values() for ref in item["refs"]}
    lost = set((s.get("source_lost") or {}).get("refs", []))   # 원본 없이 검토할 수 없는 턴(#244)
    return {"ok": not bool(s["capture_error"]), "harness": s["harness"],
            "conversation_id": s["conversation_id"], "session": s["session"],
            "record": s["record"], "transcript_path": s["transcript_path"],
            "captured_through": rs[-1]["id"] if rs else None,
            "reviewed_through": rs[n-1]["id"] if n else None,
            "captured_rounds": len(rs), "reviewed_rounds": n,
            "aborted_rounds": sum(r.get("completion") == "aborted" for r in rs),
            "failed_rounds": sum(r.get("completion") == "failed" for r in rs),
            "interrupted_rounds": sum(r.get("completion") == "interrupted" for r in rs),
            "pending": bool(s["capture_pending"] or any(r["ref"] not in lost for r in rs[n:]) or repairs),
            "repair_pending": repairs,
            "capture_pending": s["capture_pending"], "capture_error": s["capture_error"],
            "capture_recovery": _capture_recovery(vault, s),
            "coverage": s.get("coverage"), "excluded": s.get("excluded"),
            "source_lost": s.get("source_lost"),
            "space": s["space"], "workspace": s.get("workspace"),
            "inherited_rounds": len((s.get("inherited") or {}).get("rounds", [])),
            "pending_refs": [r["ref"] for i, r in enumerate(rs)
                             if i >= n and r["ref"] not in lost or r["ref"] in repair_refs],
            "through": conversation_state.snapshot(s),
            "prompt_count": s["prompt_count"], "reviewed_prompt_count": s["reviewed_prompt_count"],
            "response_growth": {k: v for k, v in s.get("response_growth", {}).items() if k != "seen"},
            "response_growth_stop": s.get("response_growth_stop"),
            "response_growth_route": s.get("response_growth_route"),
            "last_review": s["reviews"][-1] if s["reviews"] else None}


def status(vault: Vault, harness: str, conversation_id: str) -> dict:
    with conversation_state.state_lock(vault, harness, conversation_id) as p:
        return current_view(vault, conversation_state.load_state(vault, p, harness, conversation_id), p)


# Only the locators the parser writes into `native_result` fields; words that merely mention
# a locator are hashed as written.
_SESSION_LOCATOR = re.compile(r'("native_result":\s*")claude:[A-Za-z0-9_.-]+:')


def turn_hashes(pair: dict, dialogue_v1: dict | None = None) -> list[str]:
    """Local evidence of what was reviewed — the dialogue text never enters the vault.
    The first hash is the turn's own; a resumed copy also offers its original's row orders.
    A copy may rewrite result locators with its own conversation ID, so that part is
    left out: a copied turn hashes the same in parent and child. The user's words and any
    locator they mention are hashed as they are."""
    shown = (dialogue_v1 or {}).get(pair["id"], pair)
    digest = lambda agent: core.sha256_bytes(json.dumps(
        [shown["user"], _SESSION_LOCATOR.sub(r"\1claude:*:", agent)], ensure_ascii=False).encode("utf-8"))
    return [digest(a) for a in filter(None, (shown["agent"], *shown.get("agent_time_orders", ())))] or [digest("")]


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
            k["hash"] not in turn_hashes(r, dialogue_v1) for k, r in zip(known, parent))):
        raise ValueError("inherited native prefix changed; capture remains pending")
    if parent and known is None:
        s["inherited"] = {"rounds": [
            {"id": r["id"], "ref": raw.native_ref("codex", r["origin_conversation_id"], r["id"]),
             "hash": turn_hashes(r, dialogue_v1)[0]} for r in parent]}
    return rounds[len(parent):]


def _scope_cursors(vault: Vault, s: dict):
    """같은 scope의 다른 Claude 커서 — 복제된 앞부분의 주인을 찾을 때만 읽는다."""
    probe = conversation_state.state_path(vault, "claude", s["conversation_id"])
    prefix = "-".join(probe.name.split("-")[:3]) + "-"
    for candidate in sorted(probe.parent.glob(prefix + "*.json")):
        if candidate == probe:
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if data.get("harness") != "claude" or data.get("root") != s["root"]:
                continue
            owner = conversation_state.load_state(vault, candidate, "claude", data["conversation_id"])
            if candidate != conversation_state.state_path(vault, "claude", owner["conversation_id"]):
                continue
            # 부모의 착지는 부모의 작업 공간 판별로 정한다 — 이 프로세스의 작업 폴더로 정하지 않는다
            bound = write.resolve_session(vault, owner["session"]) if owner["session"] and not one_off(owner) else None
            landing = owner["space"] or (f"{vault.scope}/{bound}" if bound else None)
            # 착지 없는 대화는 어느 커서와도 견준다 — 이어받는 것은 원본 턴뿐이고, 부모가 착지했으면
            # 그 착지를 잇는다(`_inherited_rounds`). 착지한 대화는 같은 scope의 커서와만 견준다.
            if s["space"] is None or landing == s["space"]:
                yield owner, landing
        except (OSError, ValueError, KeyError):
            continue


def _inherited_rounds(vault: Vault, s: dict, rounds: list, dialogue_v1: dict | None = None) -> list:
    """Reuse only the caller's copied prefix, with native ID and byte evidence."""
    if s["harness"] == "codex":
        return _fork_parent_turns(s, rounds, dialogue_v1)
    if s["harness"] != "claude":
        return rounds
    path = raw.locate_record(vault, s["record"])        # 루트 `_cited/` 또는 옛 자리 — 착지와 무관하다
    inherited = (raw.inherited_prefix(path) if path else None) or s.get("inherited")
    if not inherited and (path and path.exists() or s["rounds"]):
        return rounds  # A conversation already tracking its own turns keeps them.
    cache = {}

    def matches(pair, source):
        if raw.is_native(source["ref"]):
            # A parent tracked by original turn: same turn ID and the same reviewed text.
            return source["hash"] in turn_hashes(pair, dialogue_v1)
        name, number = raw.parse_ref(source["ref"])
        if "/".join(raw.raw_file(vault, name).relative_to(vault.root).parts[:2]) != s["space"]:
            raise ValueError("inherited source crossed scope; capture remains pending")
        if name not in cache:
            text = raw.read_exact(raw.raw_file(vault, name))
            cache[name] = text, raw.round_spans(text)
        text, spans = cache[name]
        block = text[slice(*spans[number])]
        if core.sha256_bytes(block.rstrip("\n").encode()) != source["hash"]:
            raise ValueError("inherited raw changed; capture remains pending")
        readable = raw.round_body(block).startswith(raw.DIALOGUE_V1)
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
        # The copy may hold the tool evidence in message order; the original
        # captured append order, which the rows' timestamps still give.
        for text in filter(None, (pair["agent"], *pair.get("agent_time_orders", ()))):
            agent = locator.sub(
                lambda m: m[1] + origins.get(m[3], m[2]) + ":" + m[3] + m[4]
                if m[2] == s["conversation_id"] and references["claude:" + m[2] + ":" + m[3]]
                else m[0], text)
            expected = raw.round_block(number, raw.escape_numeric_h2(pair["user"]), raw.escape_numeric_h2(agent),
                                  dialogue_id=pair["id"] if readable else None)
            if raw.round_body(block) == raw.round_body(secrets.filter_text(expected)[0]):
                return True
        return False

    if inherited is None:
        known, landings = {}, {}
        if rounds and re.fullmatch(r"[0-9a-fA-F-]{36}", rounds[0]["id"].split(":")[0]):
            # ponytail: one local-state scan on first capture; index native IDs if this grows costly.
            for owner, landing in _scope_cursors(vault, s):
                for source in owner["rounds"]:
                    if s["space"] or raw.is_native(source["ref"]):   # 저장 라운드는 그 scope의 것이다
                        known.setdefault(source["id"], []).append(source)
                        landings[source["id"], source["ref"]] = landing
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
        if shared and not s["space"]:
            # 재개 사본은 같은 대화가 이어진 것이다 — 부모가 고정한 착지를 잇는다
            s["space"] = next((landings[r["id"], r["ref"]] for r in shared if landings.get((r["id"], r["ref"]))), None)
    if inherited:
        prefix = inherited["rounds"]
        if (len(rounds) < len(prefix)
                or any(pair["id"] != source["id"] or not matches(pair, source)
                       for pair, source in zip(rounds, prefix))):
            raise ValueError("inherited native prefix changed; capture remains pending")
        s["inherited"] = inherited
        return rounds[len(prefix):]
    return rounds


def locate_transcript(vault: Vault, harness: str, sid: str, saved: str | None = None) -> str | None:
    """Recover a moved native file by exact ID; ambiguity requires an explicit path."""
    conversation_state.conversation_key(vault, harness, sid)
    if saved and (found := transcript_files.on_disk(saved)):
        return str(found.resolve())
    matches = sorted({p.resolve() for p in adapters.get(harness).transcripts(sid) if p.is_file()})
    if len(matches) > 1:
        raise ValueError("multiple transcripts for this ID; provide explicit harness/transcript_path")
    # transcripts.read checks the native identity before this path is saved.
    return str(matches[0]) if matches else saved


def _workspace(vault: Vault, s: dict, path: str | None, cwd: str | None) -> bool:
    """이 대화의 작업 폴더가 일회성 작업 폴더인가(Mechanism §6-2 6항) — 처음 알 수 있을 때 한 번
    판별해 대화에 고정한다. 원본 전사가 적은 작업 폴더를 먼저 쓰고, 없으면 훅의 작업 폴더를 쓴다.
    판별은 그 대화의 것이다 — 같은 이름의 키를 쓰는 다른 세션에 번지지 않는다."""
    if "workspace" not in s:
        seen = transcript_files.workspace(path, s["harness"]) if path and Path(path).is_file() else None
        where = (seen or {}).get("cwd") or cwd
        if where:
            s["workspace"] = (workspaces.judge(vault, s["harness"], where, (seen or {}).get("version"))
                              or {"one_off": False})
    return one_off(s)


def one_off(s: dict) -> bool:
    return bool((s.get("workspace") or {}).get("one_off"))


def capture(vault: Vault, harness: str, conversation_id: str, transcript_path: str | None,
            session: str, space: str | None = None, cwd: str | None = None) -> dict:
    """Track this conversation's completed turns for review. The dialogue stays in the
    harness transcript: the cursor keeps original-turn refs and hashes, never vault text
    (헌법 4조 3항 · 시행령 §2 2항). Evidence enters `_cited/` only through `cite`.
    Tracking does not wait for a landing: citing a turn and writing a node need one, and
    the review of an unlanded conversation decides it (Mechanism §9-4 3항).

    The transcript is parsed outside the conversation's state lock — a large one takes
    seconds, and the hooks' own short reads of the state (tick, route) must not wait on
    it (#192). Captures of one conversation still take turns (`.capture.lock`), and only
    capture writes the fields it reads back after the parse."""
    p = conversation_state.state_path(vault, harness, conversation_id)
    with conversation_state.locked_path(p.with_name(p.stem + ".capture.json")):
        found = _capture_source(vault, harness, conversation_id, transcript_path, session, space, cwd)
        if isinstance(found, dict):
            return found
        native_path, rules, s = found
        try:
            if rules and "turns_since" not in s and not s["rounds"] and not s.get("inherited"):
                rules.update(_copied_since(vault, s, native_path))
            parsed = transcripts.read(native_path, harness, conversation_id, rules=rules)
        except Exception as exc:
            with conversation_state.state_lock(vault, harness, conversation_id) as p:
                return _capture_failed(vault, p, conversation_state.load_state(vault, p, harness, conversation_id), exc, "read")
        with conversation_state.state_lock(vault, harness, conversation_id) as p:
            s = conversation_state.load_state(vault, p, harness, conversation_id)
            try:
                return _capture_apply(vault, p, s, parsed, native_path, rules)
            except Exception as exc:
                return _capture_failed(vault, p, s, exc, "replay")


def _capture_failed(vault: Vault, p: Path, s: dict, exc: Exception, phase: str) -> dict:
    if isinstance(exc, FileNotFoundError) and phase in {"source", "read"}:
        phase = ("source" if s["rounds"] or s["prompt_count"] or s.get("native_fingerprint")
                 else "awaiting_native")
    if isinstance(exc, transcript_files.ZstdMissing):
        phase = "dependency"
    s["capture_pending"], s["capture_error"] = True, f"{type(exc).__name__}: {exc}"
    s["capture_failure_phase"] = phase
    # Only a validation refusal discards the candidate source; an I/O failure
    # keeps it for the next pathless retry.
    if phase != "landing" and isinstance(exc, ValueError):
        s.pop("capture_path", None)
    conversation_state.save_state(p, s)
    return current_view(vault, s, p)


def _land(vault: Vault, s: dict, transcript_path: str | None, session: str, space: str | None,
          cwd: str | None) -> str | None:
    """The landing step of `capture`: pin the conversation's work folder judgment, project
    key, landing and first transcript path in `s`, reading at most the transcript's head.
    Returns the path to look for; a refused landing raises ValueError."""
    # 일회성 작업 폴더의 대화는 그 키에 선 결속을 따르지 않는다 — 착지는 대화에 고정한 것뿐이다
    follow = not _workspace(vault, s, (str(Path(transcript_path).resolve()) if transcript_path else None)
                            or s.get("capture_path") or s["transcript_path"], cwd)
    pinned = s["space"]
    if pinned:
        scope = raw.scope_of_space(vault, pinned)
        if not scope:
            raise ValueError("saved conversation scope is invalid; capture remains pending")
        pinned = (vault.scope + '/') + scope
    # 착지는 검토의 문맥(허브 트리·조직 검토)이지 인용 기록의 자리가 아니다 — 기록은 루트
    # `_cited/` 한 자리다(헌법 4조 3항). 명시한 space가 먼저, 다음이 고정된 착지, 그 다음이
    # 프로젝트의 주된 scope다. 한 대화가 여러 scope의 활동을 품을 수 있으니 바뀌어도 거부하지 않는다.
    destination, primary = write.resolve_landing(vault, session if follow else None, space, "")
    requested = (vault.scope + '/') + destination if destination else None
    if pinned and not requested and s["session"]:
        # 흔한 작업 폴더 키가 무관한 대화를 제 결속으로 끌지 않게 — 착지가 고정된 대화는
        # 제가 고른 키로 이어진다
        session = s["session"]
        destination, primary = write.resolve_landing(vault, session if follow else None, pinned, "")
        requested = (vault.scope + '/') + destination
    if (s["session"] and s["session"] != session
            and not (primary and requested == pinned or not pinned and requested)):
        # 키가 바뀐 재포착은 같은 scope로 이어질 때만 받는다 — 흔한 폴더 이름의 키가
        # 무관한 대화를 제 결속으로 끌어가지 않게 한다
        raise ValueError("conversation scope changed; existing capture was not moved")
    s["session"] = session
    s["space"] = requested if space else (pinned or requested)
    requested_path = (str(Path(transcript_path).resolve()) if transcript_path else
                      s.get("capture_path") or s["transcript_path"])
    if not s["transcript_path"]:
        s["transcript_path"] = requested_path  # First-capture intent survives an absent/unflushed file.
    return requested_path


def _capture_source(vault: Vault, harness: str, conversation_id: str, transcript_path: str | None,
                    session: str, space: str | None, cwd: str | None):
    """The landing and source steps of `capture`, under the state lock — `(native_path,
    rules, state)` to read, or the view of a refusal."""
    with conversation_state.state_lock(vault, harness, conversation_id) as p:
        s = conversation_state.load_state(vault, p, harness, conversation_id)
        s["capture_pending"], s["capture_error"] = True, None
        s.pop("capture_failure_phase", None)
        conversation_state.save_state(p, s)  # Persist intent before reading; failures remain pending.
        phase = "landing"
        try:
            requested_path = _land(vault, s, transcript_path, session, space, cwd)
            conversation_state.save_state(p, s)
            phase = "source"
            native_path = locate_transcript(vault, harness, conversation_id, requested_path)
            present = bool(native_path) and Path(native_path).exists()
            if s.get("source_lost") and not present:
                # 원본 소실로 닫은 커서는 다시 실패로 세지 않는다(#244)
                s["capture_pending"] = False
                conversation_state.save_state(p, s)
                return current_view(vault, s, p)
            if not native_path:
                if not (s["rounds"] or s["prompt_count"] or s.get("native_fingerprint")):
                    phase = "awaiting_native"
                raise ValueError("native transcript path unavailable; use integration capture with --transcript")
            if present:
                source_clock(s, missing=False)   # 원본이 돌아왔다 — 닫음을 풀고 이어 간다
            # Retain the attempted source across a crash, separately from the last
            # accepted source. Only a validation rejection clears this intent below.
            s["capture_path"] = native_path
            conversation_state.save_state(p, s)
            phase = "read"
            return native_path, turn_rules(s), s
        except Exception as exc:
            return _capture_failed(vault, p, s, exc, phase)


def _capture_apply(vault: Vault, p: Path, s: dict, parsed: dict, native_path: str, rules: dict | None) -> dict:
    """The read transcript into the cursor, under the state lock (`capture`)."""
    harness, conversation_id = s["harness"], s["conversation_id"]
    if parsed.get("originator") == "codex_exec":
        # A scripted `codex exec` run — osk's own scheduled runs included — is not a
        # conversation to learn from: its turns are neither tracked nor reviewed
        # (Mechanism §9-4 3항). Rounds an earlier engine stored keep their review.
        s.update(excluded="codex_exec", capture_pending=False, capture_error=None,
                 transcript_path=native_path, native_fingerprint=parsed["native_fingerprint"])
        s.pop("capture_path", None)
        conversation_state.save_state(p, s)
        return {**current_view(vault, s, p), "appended": 0, "changed": 0}
    coverage = parsed["coverage"]
    if not s["rounds"]:
        s["coverage"] = coverage
    conversation_state.save_state(p, s)
    # ponytail: serialize tracking until its cursor is saved; use per-scope
    # locks if capture throughput matters.
    with conversation_state.locked_path(locks.local_lock_path("osk-capture-prefix.lock", vault.root)):
        rounds = _inherited_rounds(vault, s, parsed["rounds"], parsed.get("dialogue_v1"))
        kept, gone = s["rounds"], []
        if harness == "codex" or parsed.get("reverted"):
            by_id = {r["id"]: r for r in rounds}
            if len(by_id) != len(rounds):
                raise ValueError("native round identity prefix changed; the review cursor was not moved")
            # Codex `thread/revert` keeps an earlier page only up to a turn (#208), and a Claude
            # rewind leaves its dropped branch behind (#235): an unreviewed turn either took out
            # leaves the cursor as if it had never been tracked. Any other missing turn is a
            # changed prefix.
            gone = [i for i, r in enumerate(kept) if r["id"] not in by_id]
            if any(kept[i]["id"] not in parsed.get("reverted", ()) for i in gone):
                raise ValueError("native round identity prefix changed; the review cursor was not moved")
            if gone and gone[0] < s["reviewed_count"]:
                raise ValueError("Codex reverted a reviewed turn (thread/revert); the review cursor was not moved"
                                 if harness == "codex" else
                                 "Claude rewound past a reviewed turn; the review cursor was not moved")
            kept = [r for r in kept if r["id"] in by_id]
            # A recovered past turn follows the turns already tracked: keep the saved order.
            known = {r["id"] for r in kept}
            rounds = [by_id[r["id"]] for r in kept] + [r for r in rounds if r["id"] not in known]
        if s.get("rewound"):
            # Turns a rewind into a copy left behind (`_fork_rewound`) stay out until the
            # conversation goes on past them; then they come back to review with the new turns.
            later = [r["id"] for r in rounds[len(kept):]]
            if later == s["rewound"]["rounds"][:len(later)]:
                rounds = rounds[:len(kept)]
            else:
                del s["rewound"]
        ids = [r["id"] for r in rounds]
        if len(ids) != len(set(ids)) or ids[:len(kept)] != [r["id"] for r in kept]:
            raise ValueError("native round identity prefix changed; the review cursor was not moved")
        # Existing snapshots bind the saved entries: keep them, add the new turns.
        # An unreviewed original turn whose words changed under its ID is tracked
        # as it reads now, and snapshots taken before the change are dropped: no
        # review closes words the reviewer did not see. A reviewed turn keeps the
        # version its review judged. A reverted turn drops its snapshots the same way.
        prior = {r["id"]: r for r in kept}
        tracked, changed = [], []
        for i, r in enumerate(rounds):
            hashes = turn_hashes(r, parsed.get("dialogue_v1"))
            entry = prior.get(r["id"])
            if (entry and raw.is_native(entry["ref"]) and entry["hash"] not in hashes
                    and i >= s["reviewed_count"]):
                entry = {**entry, "hash": hashes[0]}
                changed.append(i)
            tracked.append(entry or {
                "id": r["id"], "ref": raw.native_ref(harness, conversation_id, conversation_state.turn_key(harness, r["id"])),
                "completion": r["completion"], "hash": hashes[0], **({"cut": r["cut"]} if r.get("cut") else {})})
        s["rounds"] = tracked
        if rules:
            s.setdefault("turns_since", rules["since"])
            s.setdefault("prompts_since", rules["prompts_since"])
            if "harness_since" in rules:
                s.setdefault("harness_since", rules["harness_since"])
        if changed or gone:
            first = min(changed[:1] + gone[:1])
            s["snapshots"] = {t: v for t, v in s["snapshots"].items()
                              if v.get("repair_refs") or v["count"] <= first}
        appended = len(s["rounds"]) - len(prior)
        if (appended or changed or gone) and s["rounds"]:
            token = conversation_state.snapshot(s)
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
        conversation_state.save_state(p, s)
        return {**current_view(vault, s, p), "appended": appended, "changed": len(changed), "reverted": len(gone)}


def tick(vault: Vault, harness: str, conversation_id: str) -> dict:
    """Count the actual hook event even with empty/unbound shared memory; never reset on resume."""
    with conversation_state.state_lock(vault, harness, conversation_id) as p:
        s = conversation_state.load_state(vault, p, harness, conversation_id)
        s["prompt_count"] += 1
        n = s["prompt_count"] - s["reviewed_prompt_count"]
        conversation_state.save_state(p, s)
        current = current_view(vault, s, p)
        return {**current, "due": bool(current["repair_pending"]) or n > 0 and n % HARD in (SOFT, 0),
                "hard": n > 0 and n % HARD == 0, "unreviewed_prompts": n}


# 4.2 이전에는 ACK 뒤 영수증과 요약 발췌를 상시 다시 확인하고, 바뀌면 검토를 다시 열었다.
# 그 사유로 열린 복구 대기만 처음 볼 때 닫는다. 출처 정정으로 연 것(사유가 다르다)은 남긴다.
_RECHECK_REASONS = {"preserved target/source/hub receipt changed",
                    "reviewed summary excerpt no longer exists"}


def current_view(vault: Vault, s: dict, path: Path) -> dict:
    old = [token for token, repair in s.get("repair_pending", {}).items()
           if repair.get("reason") in _RECHECK_REASONS]
    for token in old:
        del s["repair_pending"][token]
    if old:
        conversation_state.save_state(path, s)
    return state_view(vault, s)


def turn_rules(s: dict) -> dict | None:
    """Claude의 새 턴 규칙(턴 도중 메시지, 멈춘 미완료 턴)은 이 엔진이 처음 추적한 뒤의
    턴부터다. 새 프롬프트가 도구 호출로 끝난 턴을 닫는 규칙(#218)도 그 규칙을 처음 쓴 엔진이
    추적한 뒤부터다(`prompts_since`). 하네스가 프롬프트로 보낸 알림과 되감기로 버린 갈래를
    가리는 규칙(#275·#235)도 그렇다(`harness_since`). 그 앞에 저장된 라운드는 저장된 경계와
    표현 그대로 다시 읽힌다. 모든 판독이 같은 규칙을 써야 저장된 경계가 바뀌지 않는다."""
    if s["harness"] != "claude":
        return None
    tracked = (s.get("inherited") or {}).get("rounds", []) + s["rounds"]
    last = tracked[-1]["id"] if tracked else None
    return {"since": s.get("turns_since", last), "prompts_since": s.get("prompts_since", last),
            "harness_since": s.get("harness_since", last),
            "cuts": [r["cut"] for r in tracked if r.get("cut")], "now": time.time()}


def _copied_since(vault: Vault, s: dict, native_path: str) -> dict:
    """처음 포착하는 Claude 사본(resume·fork)의 새 규칙 경계 — 턴 규칙(`since`), 프롬프트
    규칙(`prompts_since`), 하네스 행 규칙(`harness_since`) 각각. 복제된 앞부분은 그 턴을 추적한 같은 scope 커서가 쓴 경계를
    따른다 — 새 규칙으로 읽으면 부모가 옛 규칙으로 추적한 턴과 경계·표현이 갈라진다. 그
    커서들의 경계 가운데 이 전사에서 가장 뒤에 오는 것을 쓴다. 부모가 갈래 지점 뒤로 이어
    추적했으면 그 경계는 사본에 없다 — 복제된 앞부분은 모두 그 앞이니, 경계까지 추적한
    라운드 가운데 사본에 있는 마지막 것이 경계다. 처음부터 새 규칙을 쓴 커서(경계 None)는 그대로다.
    부모가 저장한 절단 지점은 넘기지 않는다 — 사본에서도 행 시각의 12시간 공백이 같은
    자리를 나눈다. 프롬프트 경계는 부모가 턴 규칙으로 읽은 라운드다 — 유휴 닫기가 나눈
    라운드는 규칙 없는 판독에 없으므로, 찾은 턴 규칙으로 다시 읽어 찾는다(#233 리뷰)."""
    best = {"since": None, "prompts_since": None, "harness_since": None}
    rounds = transcripts.read(native_path, "claude", s["conversation_id"])["rounds"]
    if not rounds or not re.fullmatch(r"[0-9a-fA-F-]{36}", rounds[0]["id"].split(":")[0]):
        return best   # 복제는 원래 행 id(UUID)로만 알아본다 — _inherited_rounds와 같은 기준
    cursors = [([r["id"] for r in (owner.get("inherited") or {}).get("rounds", []) + owner["rounds"]], owner)
               for owner, _landing in _scope_cursors(vault, s)]

    def latest(rounds: list, key: str) -> str | None:
        order, found = {r["id"]: i for i, r in enumerate(rounds)}, None
        for ids, owner in cursors:
            bound = owner.get(key, ids[-1] if ids else None)
            if bound in ids:
                bound = next((i for i in reversed(ids[:ids.index(bound) + 1]) if i in order), None)
            if bound in order and (found is None or order[bound] > order[found]):
                found = bound
        return found

    best["since"] = latest(rounds, "turns_since")
    ruled = transcripts.read(native_path, "claude", s["conversation_id"],
                             rules={"since": best["since"], "cuts": [], "now": time.time()})["rounds"]
    best["prompts_since"] = latest(ruled, "prompts_since")
    ruled = transcripts.read(native_path, "claude", s["conversation_id"],
                             rules={"since": best["since"], "prompts_since": best["prompts_since"],
                                    "cuts": [], "now": time.time()})["rounds"]
    best["harness_since"] = latest(ruled, "harness_since")
    return best


def source_clock(s: dict, missing: bool) -> bool:
    """원본이 없는 동안의 시계(#244). 처음 못 찾은 때를 적고, SOURCE_LOST_AFTER 동안 계속 없으면
    원본 소실로 닫는다 — 하루 묶음이 다시 포착하지 않고, 포착 오류·대기와 검토 대기에서 빠진다.
    검토하지 못한 원본 턴은 지우지 않고 그 처분(`source_lost.refs`)에 남긴다. 인용 기록으로 남은
    턴은 원본 없이도 검토할 수 있어 대기에 둔다. 원본을 찾으면 시계와 닫음을 푼다. 바꿨으면 True."""
    if not missing:
        changed = "source_missing_since" in s or "source_lost" in s
        s.pop("source_missing_since", None)
        s.pop("source_lost", None)
        return changed
    if s.get("source_lost"):
        return False
    now = time.time()
    if "source_missing_since" not in s:
        s["source_missing_since"] = now
        return True
    if now - s["source_missing_since"] < SOURCE_LOST_AFTER:
        return False
    s["source_lost"] = {"since": s["source_missing_since"], "closed": now,
                        "refs": [r["ref"] for r in s["rounds"][s["reviewed_count"]:] if raw.is_native(r["ref"])]}
    s.update(capture_pending=False, capture_error=None)
    s.pop("capture_failure_phase", None)
    s.pop("capture_path", None)
    return True


def ended_source(vault: Vault, s: dict) -> str | None:
    """This device's transcript of a conversation that has gone quiet. A worker without
    the conversation reads its unreviewed turns there; a conversation still running keeps
    its own review cadence, where the turns are already in context."""
    try:
        path = locate_transcript(vault, s["harness"], s["conversation_id"],
                                  s.get("capture_path") or s["transcript_path"])
        if path and time.time() - Path(path).stat().st_mtime >= ENDED_AFTER:
            return path
    except (OSError, ValueError):
        pass
    return None


def turn_at(s: dict, native_path: str | None, refs: list[str] | None = None) -> float | None:
    """다음에 검토할 턴의 원래 시각(epoch 초). 복구 대기면 그 대상 가운데 가장 이른 턴이다.
    `refs`를 주면 그 턴들 가운데 가장 이른 시각이고, 한 턴이라도 모르면 None이다 — 증거 조회가
    그보다 늦은 시각에서 멈추면 그 턴의 영수증을 놓친다(#227).
    Codex 턴 id는 UUIDv7이라 id에서 읽고, Claude는 원본 전사에서 그 사용자 발화 행의 시각을
    읽는다. 알 수 없으면 None."""
    if refs is not None:
        wanted = set(refs)
        ids = [r["id"] for r in s["rounds"] if r["ref"] in wanted]
        if len(ids) != len(wanted):
            return None
    else:
        repair = {ref for r in (s.get("repair_pending") or {}).values() for ref in r.get("refs", [])}
        ids = ([r["id"] for r in s["rounds"] if r["ref"] in repair] if repair else
               [s["rounds"][s["reviewed_count"]]["id"]] if s["reviewed_count"] < len(s["rounds"]) else [])
    if not ids:
        return None
    if s["harness"] == "codex":
        turns = [i.rsplit(":", 1)[-1] for i in ids]
        if not all(re.match(core.RID_RE, t) for t in turns):
            return None
        return min(int(t.replace("-", "")[:12], 16) / 1000 for t in turns)
    if s["harness"] != "claude" or not native_path:
        return None
    want, found = {i.split(":", 1)[0] for i in ids}, []
    try:
        for line in transcript_files.native_lines(native_path, s["harness"], s["conversation_id"]):
            for uuid in [u for u in want if u.encode() in line]:
                row = json.loads(line)
                if row.get("uuid") == uuid and isinstance(row.get("timestamp"), str):
                    found.append(datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp())
                    want.discard(uuid)
            if not want:
                break
    except (OSError, ValueError):
        return None
    if refs is not None and want:
        return None
    return min(found) if found else None


class SubagentEvent(Exception):
    """Codex subagent hooks send the root session_id with the child's rollout. Not a
    ValueError: no fallback may record it in the root conversation's state."""


def hook_source(vault: Vault, env: dict) -> tuple[str, str, str | None]:
    """Locate only the caller's native transcript, never another conversation's backlog."""
    sid = adapters.session_id(env)
    path = env.get("transcript_path")
    if not sid:
        raise ValueError("hook has no actual conversation ID; capture not acknowledged")
    harness = adapters.detect(env, sid, path)
    candidates = []
    if not path:
        for kind in ([harness] if harness else list(adapters.NAMES)):
            conversation_state.conversation_key(vault, kind, sid)
            saved = locate_transcript(vault, kind, sid, status(vault, kind, sid).get("transcript_path"))
            if saved:
                candidates.append((kind, saved))
        if len(candidates) == 1:
            harness, path = candidates[0]
        elif len(candidates) > 1:
            raise ValueError("multiple transcripts for this ID; provide explicit harness/transcript_path")
    if not harness:
        raise ValueError("native harness/transcript unavailable; provide harness and transcript_path")
    conversation_state.conversation_key(vault, harness, sid)
    reason = adapters.get(harness).subagent(path, sid) if path and Path(path).is_file() else None
    if reason:
        raise SubagentEvent(reason)
    return harness, sid, path


def recent_transcript(vault: Vault, harness: str) -> str | None:
    """이 vault가 가장 최근에 포착한 그 하네스 대화의 전사 — `doctor`가 판본을 읽는다.
    상태를 쓰지 않는다."""
    probe = conversation_state.state_path(vault, harness, "inventory")
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


def hook_capture(vault: Vault, env: dict, session: str) -> dict:
    harness, sid, path = hook_source(vault, env)
    return capture(vault, harness, sid, path, session, env.get("space"), cwd=env.get("cwd"))


def hook_land(vault: Vault, env: dict, session: str) -> dict:
    """SessionStart's part of `capture`: pin the conversation's landing (`_land`) so its first
    turn cites and reviews under it, without parsing the transcript — a resumed or compacted
    one takes seconds (#192); the hooks' worker captures. A refused landing is kept as capture
    keeps it. Returns the saved view: the turns as of the last capture."""
    harness, sid, path = hook_source(vault, env)
    with conversation_state.state_lock(vault, harness, sid) as p:
        s = conversation_state.load_state(vault, p, harness, sid)
        try:
            _land(vault, s, path, session, env.get("space"), env.get("cwd"))
        except Exception as exc:
            return _capture_failed(vault, p, s, exc, "landing")
        conversation_state.save_state(p, s)
        return current_view(vault, s, p)


def capture_due(vault: Vault, harness: str, conversation_id: str, transcript_path: str | None) -> bool:
    """Is there anything new for `capture`? A stat, never a read — the hooks hand a capture
    to their worker only then (#192). A new conversation's named transcript that does not exist
    yet (before its first turn is written) has nothing to capture: the worker would count it
    missing after this prompt's tick, and a later hook's worker captures it. A tracked
    conversation's file that left the named path is the worker's to find by ID
    (`locate_transcript`) or to record missing."""
    with conversation_state.state_lock(vault, harness, conversation_id) as p:
        s = conversation_state.load_state(vault, p, harness, conversation_id)
    if transcript_path and not os.path.exists(transcript_path):
        return bool(s["rounds"] or s.get("native_fingerprint"))
    if s["capture_pending"] or s["capture_error"] or not s.get("native_fingerprint") or not transcript_path:
        return True
    try:
        return transcript_files.native_fingerprint(transcript_path, harness, conversation_id) != s["native_fingerprint"]
    except (OSError, ValueError):
        return True
