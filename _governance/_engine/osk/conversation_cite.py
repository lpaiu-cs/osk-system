"""osk.conversation_cite — 원본 턴을 근거로 인용하고 다시 읽는다 (Mechanism §9 9항).

대화의 한 턴을 인용 기록의 라운드로 남기고(`cite`), 검토할 턴과 인용한 턴을 원본에서 읽어 온다."""
from __future__ import annotations
import re
from pathlib import Path
from . import conversation_capture, conversation_state, core, evidence, raw, raw_view, secrets, transcripts
from .vault import Vault


def _conversation(vault: Vault, value: str) -> tuple[str, str]:
    """`<harness>/<conversation ID>` — the pair the hooks show the session."""
    harness, sep, sid = (value or "").strip().partition("/")
    if not sep:
        raise ValueError("conversation must be <harness>/<conversation ID>")
    conversation_state.conversation_key(vault, harness, sid)
    return harness, sid


def cite(vault: Vault, conversation: str, quote: str | None = None, turn: str | int | None = None,
         note: str | None = None, session: str | None = None, space: str | None = None,
         user: str | None = None) -> dict:
    """Keep one original turn as evidence (시행령 §2 2항 · Mechanism §9 9항). The engine
    copies the user's words from the harness transcript and keeps the agent's reply as
    location and hash; the caller names the turn and never resends its text."""
    harness, sid = _conversation(vault, conversation)
    # 착지 확인·기록·고정을 이 대화의 잠금 하나 안에서 한다 — 같은 대화의 첫 인용 둘이 서로 다른
    # scope로 갈리지 않는다. 잠금 순서는 검토 ACK와 같다(대화 잠금 → 변경 잠금).
    with conversation_state.state_lock(vault, harness, sid) as p:
        return _cite(vault, p, conversation_state.load_state(vault, p, harness, sid), harness, sid, quote, turn, note, session, space, user)


def _cite(vault: Vault, p: Path, s: dict, harness: str, sid: str, quote, turn, note, session, space, user) -> dict:
    session = session or s["session"]
    if not session:
        raise ValueError("this conversation is not tracked yet; its hooks or `integration capture` set its session")
    # 인용 기록은 루트 `_cited/` 한 자리다(헌법 4조 3항) — 인용에 착지도 결속도 필요 없다. 대화의
    # 착지(`s["space"]`)는 검토의 문맥일 뿐이고, `space`는 호환 인자다.
    source = conversation_capture.locate_transcript(vault, harness, sid, s.get("capture_path") or s["transcript_path"])
    if not (source and Path(source).is_file()):
        source = None
    if user is not None and source:
        raise ValueError("the original transcript is readable; name the turn by quote or turn")
    if user is None:
        if not source:
            raise ValueError("original transcript unavailable; pass user= to keep caller-supplied words")
        parsed = transcripts.read(source, harness, sid, rules=conversation_capture.turn_rules(s))
        shown = parsed.get("dialogue_v1") or {}
        turns = [{"id": conversation_state.turn_key(harness, r["id"]), "user": shown.get(r["id"], r)["user"],
                  "agent": shown.get(r["id"], r)["agent"], "owner": r.get("origin_conversation_id") or sid}
                 for r in parsed["rounds"]]
        tail = parsed.get("tail")
        if tail and tail.get("user") and conversation_state.turn_key(harness, tail["id"]) not in {t["id"] for t in turns}:
            turns.append({"id": conversation_state.turn_key(harness, tail["id"]), "user": tail["user"], "agent": None, "owner": sid})
        # Only "-N" counts back from the latest. Any other string is a turn ID — Antigravity's are
        # step numbers ("2"), and the ID read_cited returns must name the same turn here.
        n = (turn if isinstance(turn, int) and not isinstance(turn, bool) else
             int(turn) if isinstance(turn, str) and re.fullmatch(r"-\d+", turn.strip()) else None)
        # "-N" and the default latest count the user's turns: a round a harness notice started
        # has no words of theirs to cite (#275).
        spoken = [t for t in turns if t["user"].strip()]
        if n is not None:
            if n >= 0:
                raise ValueError("a relative turn counts back from the latest: -1, -2, ...")
            matched = spoken[n:n + 1 or None] if -n <= len(spoken) else []
        elif isinstance(turn, str) and turn.strip():
            key = conversation_state.turn_key(harness, turn.strip())
            matched = [t for t in turns if t["id"] == key]
            if not matched and len(key) >= conversation_state.SHORT_ID:
                # A review range names its turns by a prefix unique among the tracked turns
                # (`conversation_state.short_ids`, #219), so it resolves among those alone — not a fork parent's turn
                # or the open tail. An exact ID wins, so a short step number ("2") never matches "20".
                tracked = {conversation_state.turn_key(harness, r["id"]) for r in s["rounds"]}
                matched = [t for t in turns if t["id"] in tracked and t["id"].startswith(key)]
        elif quote and quote.strip():
            needle = " ".join(quote.split())
            matched = [t for t in turns if needle in " ".join(t["user"].split())]
        else:
            matched = spoken[-1:]
        if len(matched) != 1:
            seen = [f"{t['id']}: {' '.join(t['user'].split())[:40]}" for t in (matched or turns)[-5:]]
            raise ValueError(f"{len(matched)} turns match; narrow by quote or turn. Candidates: {seen}")
        words, rid, agent, by = matched[0]["user"], matched[0]["id"], matched[0]["agent"], "engine"
        owner = matched[0]["owner"]
    else:
        if not user.strip():
            raise ValueError("user= needs the words to keep")
        words, agent, by, owner = user, None, "caller", sid
        rid = (conversation_state.turn_key(harness, turn.strip()) if isinstance(turn, str) and turn.strip() else
               "caller-" + core.sha256_bytes(user.encode("utf-8")).removeprefix("sha256:")[:16])
    meta = {"harness": harness, "conversation": owner, "turn": rid, "source": source,
            "agent_sha256": core.sha256_bytes(agent.encode("utf-8")) if agent else None, "user_by": by}
    if by == "engine":
        # The words the engine copied: the same turn ID with other words is other evidence. A
        # caller's recollection is not the turn's words and does not decide that.
        meta["user_sha256"] = core.sha256_bytes(words.encode("utf-8"))
    record = s["record"]
    if owner != sid:
        # A fork parent's turn keeps its owner: the citation names the parent and joins
        # the parent's record, so the parent citing the same turn reuses that round.
        with conversation_state.state_lock(vault, harness, owner) as pp:
            parent = conversation_state.load_state(vault, pp, harness, owner)
        record = parent["record"]
    result = raw.append_rounds(vault, session, record, [{"user": words, "agent": (note or "").strip()}],
                               cited=meta)
    out = {"ok": True, "round_ref": result["round_refs"][0], "path": result["path"],
           "index": result["indices"][0],
           # 근거 줄에 쓸 좌표 — 기록 이름 해시의 앞자리와 라운드(Mechanism §8 9항)
           "coordinate": evidence.coordinate(vault, result["path"], result["indices"][0]),
           "turn": rid, "reused": result.get("reused", False),
           "user_by": by, "agent_sha256": meta["agent_sha256"], "filtered": result["filtered"],
           **({"binding": result["binding"]} if "binding" in result else {})}
    if result.get("supersedes"):
        out["supersedes"] = result["supersedes"]   # the turn's words changed since that citation
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


REVIEW_SIDES = (1500, 1500)  # 묶음 읽기의 턴당 발화·응답 상한 — 15턴이면 45k자 안팎(10-22 사용자 결정, #219)


def read_review(vault: Vault, conversation: str, through: str, query: str | None = None) -> dict:
    """The turns one review covers, in one call: each turn's words and response within
    REVIEW_SIDES, so a reviewer whose context lost them (compaction) reads only that range.
    Nothing is written."""
    harness, sid = _conversation(vault, conversation)
    with conversation_state.state_lock(vault, harness, sid) as p:
        s = conversation_state.load_state(vault, p, harness, sid)
    refs = conversation_state.review_refs(s, through)
    if len(refs) > conversation_state.MAX_REVIEW_ROUNDS:
        # A capture snapshot spans every unreviewed turn; a review reads the bounded one it was given.
        raise ValueError(f"this snapshot covers {len(refs)} turns; read the through the review prompt gave "
                         f"(at most {conversation_state.MAX_REVIEW_ROUNDS})")
    native = [ref for ref in refs if raw.is_native(ref)]
    seen = {t["ref"]: t for t in read_turns(vault, native, view="review", query=query, sides=REVIEW_SIDES)["turns"]} if native else {}
    short = conversation_state.short_ids(s)
    turns = [{"short": short(ref), **(seen.get(ref) or raw.read_round(vault, ref, view="review", query=query,
                                                                      sides=REVIEW_SIDES))} for ref in refs]
    return {"ok": True, "through": through, "range": conversation_state.range_text(s, refs), "turns": turns}


def read_turns(vault: Vault, refs: list, max_chars: int = 6000, view: str = "review",
               query: str | None = None, *, sides: tuple[int, int] | None = None) -> dict:
    """Original turns from this device's transcript, for a reviewer without the conversation.
    Only the named turns are read, never the whole conversation; each names the turn
    before it as `previous`, for earlier context only when a judgment needs it
    (Mechanism §9-4 3항). Nothing is written."""
    if view not in {"review", "full"} or not isinstance(refs, list) or not 1 <= len(refs) <= conversation_state.MAX_REVIEW_ROUNDS:
        raise ValueError(f"1..{conversation_state.MAX_REVIEW_ROUNDS} original-turn refs and view review|full are required")
    if sides is not None and view != "review":
        raise ValueError("sides apply only to view=review")
    out, conversations = [], {}
    for ref in refs:
        parts = ref.split(":", 3) if raw.is_native(ref) else []
        if len(parts) != 4:
            raise ValueError("ref must be native:<harness>:<conversation ID>:<turn ID>")
        _, harness, sid, turn = parts
        if (harness, sid) not in conversations:
            with conversation_state.state_lock(vault, harness, sid) as p:
                s = conversation_state.load_state(vault, p, harness, sid)
            source = conversation_capture.locate_transcript(vault, harness, sid, s.get("capture_path") or s["transcript_path"])
            if not (source and Path(source).is_file()):
                raise ValueError(f"the transcript of {harness}/{sid} is not on this device")
            parsed = transcripts.read(source, harness, sid, rules=conversation_capture.turn_rules(s))
            shown = parsed.get("dialogue_v1") or {}
            tracked = {conversation_state.turn_key(harness, r["id"]): r["hash"] for r in s["rounds"] if raw.is_native(r["ref"])}
            conversations[harness, sid] = tracked, [
                (conversation_state.turn_key(harness, r["id"]), shown.get(r["id"], r), conversation_capture.turn_hashes(r, parsed.get("dialogue_v1")),
                 r.get("origin_conversation_id") or sid) for r in parsed["rounds"]]
        tracked, turns = conversations[harness, sid]
        at = next((i for i, (key, *_) in enumerate(turns) if key == turn), None)
        if at is None:
            raise ValueError(f"{turn} is not a completed turn of {harness}/{sid}")
        _, pair, hashes, owner = turns[at]
        if owner != sid:
            raise ValueError(f"{turn} belongs to the fork parent; read {raw.native_ref(harness, owner, turn)}")
        # The hash binds the original; the text leaving through MCP is filtered first, as a citation
        # is — not every parser redacts (Antigravity words and replies, Kiro string replies).
        chunk, hits = secrets.filter_text(raw.round_block(at + 1, pair["user"], pair["agent"]).rstrip("\n"))
        item = {"ref": ref, "turn": turn, "hash": hashes[0], "position": at + 1, "turns": len(turns),
                "previous": raw.native_ref(harness, turns[at - 1][3], turns[at - 1][0]) if at else None}
        if hits:
            item["filtered"] = sorted(set(hits))
        if turn in tracked and tracked[turn] not in hashes:
            # Changed since tracking: its review waits for the next capture's snapshot.
            item["changed"] = True
        if view == "review":
            item.update(raw_view.project(chunk, max_chars, query, sides=sides))
        else:
            cut = len(chunk) > max_chars
            item.update(view="full", chars=len(chunk), truncated=cut,
                        text=chunk[:max_chars] if cut else chunk)
        out.append(item)
    return {"ok": True, "turns": out}
