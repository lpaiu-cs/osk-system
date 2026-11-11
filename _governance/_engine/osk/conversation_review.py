"""osk.conversation_review — 대화 검토를 닫는다 (Mechanism §9-4).

검토한 범위가 저장된 라운드·원본 전사와 같은지 확인한 뒤 커서를 올리고(`acknowledge`), 닫은 검토를
다시 연다(`reopen`). 공유 scope 기억에 쓴 것은 검토를 닫은 것이 아니다 — 닫는 것은 이 모듈뿐이다."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from . import (conversation_capture, conversation_state, core, distillation, graph, placement, raw, scope_memory,
               transcripts, write)
from .vault import Vault


def _stored_hashes(vault: Vault, refs) -> dict:
    """Each stored round's hash as it is in the vault now (None if the round is gone)."""
    raw_files, out = {}, {}
    for ref in refs:
        name, index = raw.parse_ref(ref)
        if name not in raw_files:
            text = raw.read_exact(raw.raw_file(vault, name))
            raw_files[name] = text, raw.round_spans(text)
        text, spans = raw_files[name]
        out[ref] = (core.sha256_bytes(text[slice(*spans[index])].rstrip("\n").encode("utf-8"))
                    if index in spans else None)
    return out


def verify_raw(vault: Vault, rounds: list) -> None:
    """Stored rounds must still match their saved hashes. An original-turn ref has no
    vault copy to check — the reviewer judged the turn from the conversation itself."""
    stored = [r for r in rounds if not raw.is_native(r["ref"])]
    current = _stored_hashes(vault, [r["ref"] for r in stored])
    if any(current[r["ref"]] != r["hash"] for r in stored):
        raise ValueError("raw snapshot changed; review is not acknowledged")


def _verify_native(vault: Vault, s: dict, rounds: list) -> dict:
    """An original turn has no vault copy: this device's transcript must still hold the
    words the snapshot tracked, or the review would close what its reviewer did not read.
    Returns each turn's user words hash, which a citation of that turn must match."""
    native = [r for r in rounds if raw.is_native(r["ref"])]
    if not native:
        return {}
    source = conversation_capture.locate_transcript(vault, s["harness"], s["conversation_id"], s.get("capture_path") or s["transcript_path"])
    if not (source and Path(source).is_file()):
        raise ValueError("the original transcript is not on this device; review is not acknowledged")
    parsed = transcripts.read(source, s["harness"], s["conversation_id"], rules=conversation_capture.turn_rules(s))
    shown = parsed.get("dialogue_v1") or {}
    current = {r["id"]: conversation_capture.turn_hashes(r, shown) for r in parsed["rounds"]}
    if any(r["hash"] not in current.get(r["id"], []) for r in native):
        raise ValueError("an original turn changed after this snapshot; capture again and review the current one")
    words = {r["id"]: core.sha256_bytes(shown.get(r["id"], r)["user"].encode("utf-8")) for r in parsed["rounds"]}
    return {r["ref"]: words.get(r["id"]) for r in native}


def acknowledge(vault: Vault, harness: str, conversation_id: str, through: str,
                outcome: str, reason: str, targets: list | None = None,
                primary_scope: str | None = None) -> dict:
    """`primary_scope`는 이 검토의 소속 판정이 **그 프로젝트 자체의 활동**이 속한 scope로 확인한
    것이다 — 결정 묶음의 한 항목이며 실행기(이 함수)가 주된 scope 지정 기록으로 적는다(Mechanism
    §6-2 6항 · §9-4 3항). 일회성 작업 폴더의 대화에는 프로젝트가 없어 받지 않는다."""
    if outcome not in ("preserved", "summary", "no_value", "deferred") or not isinstance(reason, str) or not reason.strip():
        raise ValueError("outcome preserved|summary|no_value|deferred and nonempty reason required")
    if primary_scope is not None and (not isinstance(primary_scope, str) or not primary_scope.strip()):
        raise ValueError("primary_scope must be a scope name")
    with conversation_state.state_lock(vault, harness, conversation_id) as p, core.mutation_lock(vault):
        s = conversation_state.load_state(vault, p, harness, conversation_id)
        if primary_scope is not None:
            if conversation_capture.one_off(s) or not s["session"]:
                raise ValueError("a one-off workspace conversation has no project to designate a primary scope for")
            primary_scope = raw.scope_of_space(vault, primary_scope) or primary_scope.strip()
            if primary_scope not in raw.scope_names(vault):
                raise ValueError(f"primary_scope is not a scope: {primary_scope}")
        snap = s["snapshots"].get(through)
        repair = s.get("repair_pending", {}).get(through)
        refs = set(conversation_state.review_refs(s, through))
        verify_raw(vault, [r for r in s["rounds"] if r["ref"] in refs])
        words = {}
        if not repair and outcome != "deferred":
            # A reopened review rereads only its corrected stored rounds; a deferral closes nothing.
            words = _verify_native(vault, s, [r for r in s["rounds"] if r["ref"] in refs])
        receipts = []
        if outcome == "preserved":
            if not isinstance(targets, list) or not targets:
                raise ValueError("preserved requires completed distillation targets [{key:...}]")
            for target in targets:
                if not isinstance(target, dict) or not isinstance(target.get("key"), str):
                    raise ValueError("preserved target must name a distillation key")
                receipt = distillation.status_locked(vault, target["key"])
                sources = receipt.get("sources", [])
                # A cited round binds through the original turn it cites (Mechanism §9 9항), and
                # only while the turn holds the words it cited.
                bound = ({raw.canonical_ref(vault, r["ref"]) for r in sources}
                         | {r["native"] for r in sources if r.get("native") and r.get("native_user")
                            in (None, words.get(r["native"], r.get("native_user")))})
                # Each refusal says which condition failed and what the target cites (#219).
                if receipt.get("status") != "complete":
                    raise ValueError(f"target {target['key']} has no complete body/source/hub receipt: "
                                     f"{receipt.get('reason') or receipt.get('status')}")
                covered = {raw.canonical_ref(vault, ref) for ref in refs}
                if not covered & bound:
                    short = conversation_state.short_ids(s)
                    changed = [short(r["native"]) for r in sources if r.get("native") in covered]
                    if changed:
                        raise ValueError(f"target {target['key']} cites {', '.join(changed)} of this review, but the "
                                         "turn's words changed after it was cited; cite the turn again and "
                                         "distill from the new citation")
                    cited = [short(r["native"]) if r.get("native") else r["ref"] for r in sources]
                    raise ValueError(f"target {target['key']} cites no turn of this review ({conversation_state.range_text(s, refs)}); "
                                     f"it cites {', '.join(cited) or 'nothing'}. A later turn's distillation "
                                     "belongs to that turn's review: judge this range on its own")
                target_path = core.resolve_in_root(vault, receipt.get("target", {}).get("path", ""))
                kind = placement.space_of(vault, target_path)[0] if target_path is not None else None
                if kind not in ("scope", "person"):
                    raise ValueError("preserved integration target must be a durable Scope or Person node")
                receipts.append(receipt)
        elif outcome == "summary":
            # 일회성 작업 폴더의 대화는 그 키의 결속이 아니라 대화에 고정한 착지의 기억을 본다
            # 대화에 고정한 착지가 있으면 그 scope의 기억이다 — 프로젝트의 주된 scope와 다를 수 있다
            memory = scope_memory.read(vault, None if conversation_capture.one_off(s) else s["session"], s.get("space"))
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
        if primary_scope is not None and write.bindings(vault, s["session"])["primary"] != primary_scope:
            # 검토 저장 **앞에** 적는다 — 대장 쓰기가 실패하면 검토도 남지 않아 같은 through로
            # 재시도할 수 있다. 지정이 먼저 서고 저장이 실패해도 재시도는 같은 판정이라 건너뛴다.
            write.set_primary(vault, s["session"], primary_scope, "대화 검토의 소속 판정")
        conversation_state.save_state(p, s)
        return conversation_capture.state_view(vault, s)


def _review_state_locked(vault: Vault, s: dict, through: str) -> dict:
    snap = s["snapshots"].get(through)
    last = next((r for r in reversed(s["reviews"]) if r["through"] == through), {})
    result = {"harness": s["harness"], "conversation_id": s["conversation_id"], "through": through,
              "status": "pending", "outcome": last.get("outcome")}
    if snap and snap.get("repair_parts"):
        parts = [_review_state_locked(vault, s, token) for token in snap["repair_parts"]]
        complete = all(part["status"] == "complete" for part in parts)
        return {**result, "status": "complete" if complete else "pending",
                "reason": None if complete else "bounded repair reviews remain pending",
                "parts": parts, "semantic_verified": False}
    repair = s.get("repair_pending", {}).get(through)
    if repair:
        return {**result, "reason": repair["reason"], "repair_pending": True}
    if not snap or not last or last.get("outcome") == "deferred" or s["reviewed_count"] < snap["count"]:
        return result
    # ACK가 출처·대상·출처 링크·허브를 확인했다. 그 뒤에는 구조 신호만 보고 검토를 다시 열지
    # 않는다 — 문단 편집·허브 이동·scope 기억 정리는 보존을 무르지 않는다. 출처 라운드는
    # append-only라 바뀌지 않고, 정정은 정정하는 쪽이 reopen으로 등록한다.
    out = {**result, "status": "complete", "review": last, "semantic_verified": False}
    if last["outcome"] == "preserved":
        idx = graph.Index(vault)
        broken = [{"key": r["key"], "reason": why} for r in last["receipts"]
                  if (why := distillation.structure(vault, r, idx))]
        if broken:
            out["structure"] = broken
    return out


def _register_repair(s: dict, through: str, reason: str, version: str,
                     corrected: list, targets: list) -> bool:
    """Reopen an acknowledged review until an explicit replacement ACK. The repair keeps the
    corrected coordinates and the nodes whose receipts cited them: they are what a reviewer
    rereads, and a cited round is not among the review's own refs. A review already reopened by
    an earlier correction moves to a new token for this version — an ACK of the earlier bytes
    names the old token, which no longer closes anything — and keeps both corrections."""
    snap = s["snapshots"].get(through, {})
    if snap.get("repair_parts"):
        return any([_register_repair(s, token, reason, version, corrected, targets)
                    for token in snap["repair_parts"]])
    pending = s.setdefault("repair_pending", {})
    if through in pending:
        old = pending[through]
        if old.get("version") == version:
            return False
        child = "sha256:" + hashlib.sha256(json.dumps([through, version]).encode()).hexdigest()
        s["snapshots"][child] = {"count": snap["count"], "prompt_count": snap["prompt_count"],
                                 "repair_refs": old["refs"]}
        snap["repair_parts"] = [child]
        kept = {t["key"]: t for t in old.get("targets", []) + targets}
        pending[child] = {**pending.pop(through), "reason": reason, "since": core.now_iso(),
                          "review_count": len(s["reviews"]), "version": version,
                          "corrected": sorted(set(old.get("corrected", [])) | set(corrected)),
                          "targets": [kept[k] for k in sorted(kept)]}
        return True
    prior = next((r for r in reversed(s["reviews"])
                  if r["through"] == through and r["outcome"] != "deferred"), None)
    if not prior:
        return False
    pending[through] = {"reason": reason, "refs": prior["refs"], "since": core.now_iso(),
                        "review_count": len(s["reviews"]), "version": version,
                        "corrected": corrected, "targets": targets}
    return True


def reopen(vault: Vault, harness: str, conversation_id: str, refs: list, reason: str) -> dict:
    """Stored and cited records are append-only (헌법 4조 4항). A correction is the exception,
    and the corrector registers it here. A corrected round this conversation tracks takes its
    current bytes; every acknowledged review that read it — as its own round, or through a
    receipt citing it — is reviewed again. No later check looks for any of this."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reopen requires the correction's reason")
    with conversation_state.state_lock(vault, harness, conversation_id) as p, core.mutation_lock(vault):
        s = conversation_state.load_state(vault, p, harness, conversation_id)
        wanted = {raw.canonical_ref(vault, ref) for ref in refs or []}
        if not wanted or any(raw.is_native(ref) for ref in wanted):
            raise ValueError("reopen takes corrected stored or cited rounds; an original turn is not corrected here")
        current = _stored_hashes(vault, sorted(wanted))
        if None in current.values():
            raise ValueError("a corrected round is missing from its record")

        hits = {}   # through -> (corrected refs it read, receipts that cited them, by key)
        for v in s["reviews"]:
            if v["outcome"] == "deferred":
                continue
            # Old cursors keep their original coordinates: compare both sides canonically.
            read = wanted & {raw.canonical_ref(vault, ref) for ref in v["refs"]}
            cited = {}
            for receipt in v.get("receipts", []):
                hit = wanted & {raw.canonical_ref(vault, src["ref"]) for src in receipt.get("sources", [])}
                if hit:
                    read |= hit
                    cited[receipt["key"]] = {"key": receipt["key"], "id": receipt["target"]["id"],
                                             "name": receipt["target"].get("name"),
                                             "path": receipt["target"]["path"]}
            if read:
                seen, targets = hits.setdefault(v["through"], (set(), {}))
                seen |= read
                targets.update(cited)
        if not hits:
            raise ValueError("no acknowledged review of this conversation read these rounds")
        for r in s["rounds"]:
            if raw.canonical_ref(vault, r["ref"]) in wanted:
                r["hash"] = current[raw.canonical_ref(vault, r["ref"])]
        version = core.sha256_bytes(json.dumps(sorted(current.items())).encode("utf-8"))
        for token, (read, targets) in sorted(hits.items()):
            _register_repair(s, token, reason.strip(), version, sorted(read),
                             [targets[k] for k in sorted(targets)])
        conversation_state.save_state(p, s)
        return conversation_capture.state_view(vault, s)


def review_status_locked(vault: Vault, harness: str, conversation_id: str, through: str) -> dict:
    # Pure while the caller owns mutation lock: never acquire the local lock or write state.
    return _review_state_locked(vault, conversation_state.load_state(vault, conversation_state.state_path(vault, harness, conversation_id), harness, conversation_id), through)


def review_status(vault: Vault, harness: str, conversation_id: str, through: str) -> dict:
    with conversation_state.state_lock(vault, harness, conversation_id) as p, core.mutation_lock(vault):
        return _review_state_locked(vault, conversation_state.load_state(vault, p, harness, conversation_id), through)


def review_scope(vault: Vault, s: dict) -> str | None:
    """대화 검토 문맥의 scope — 고정한 착지, 없으면 결속의 주된 scope부터(§9-3 1항). `prompt`의 허브
    트리 scope와 같다."""
    if s.get("space"):
        return raw.scope_of_space(vault, s["space"])
    try:
        scopes = write.bound_scopes(vault, s["session"]) if s.get("session") and not conversation_capture.one_off(s) else []
    except (OSError, ValueError, write.WriteError):
        scopes = []
    return scopes[0] if scopes else None
