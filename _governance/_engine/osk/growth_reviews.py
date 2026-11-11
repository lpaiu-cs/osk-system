"""osk.growth_reviews — 작업자의 판정을 받아 적는다 (Mechanism §9-4).

작업자의 최종 결과 묶음을 공급자 출력에서 읽고(`_final_packet`), 형식과 선택 여부를 확인한 뒤
(`_validate_packet`), 항목마다 제 기록부에 적는다(`_apply_reviews`). 항목 하나의 잘못은 그 항목만
받지 않는다."""
from __future__ import annotations
import json
from pathlib import Path
import re
from . import conversation_review, evictions, graph, growth_ledger, growth_plan, organization, rechecks, ledger, locks
from .vault import Vault


def review(vault: Vault, key: str, outcome: str, target: str | None = None, reason: str = "",
           *, manifest: str) -> dict:
    """Append a decision for a recorded, still-current comparison manifest."""
    if outcome not in {"preserved", "no_value", "deferred"}:
        raise ValueError("outcome must be preserved, no_value or deferred")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("an explicit reason is required")
    if not isinstance(manifest, str) or not manifest:
        raise ValueError("the exact growth manifest is required")
    if outcome != "preserved" and target is not None:
        raise ValueError("only preserved decisions have a target")
    with locks.mutation_lock(vault):
        rows, idx = growth_ledger.ledger_records(vault), growth_plan.node_index(vault)
        matches = [(r, c) for r in rows if r.get("kind") == "plan"
                   and r.get("rid") == manifest
                   for c in r.get("candidates", []) if c.get("key") == key]
        if not matches:
            raise ValueError("candidate was not recorded by a growth run")
        selected, candidate = matches[-1]
        proof = None
        if outcome == "preserved":
            proof = growth_plan.preservation(vault, candidate, idx, target)
            target = proof["target"]["name"]
        # no_value·deferred는 비교 당시의 판단으로 받는다. 출처나 비교 맥락이 그 뒤 바뀌었으면
        # _completed가 완료로 세지 않아 새 비교로 다시 고른다 — 보고를 버릴 까닭이 없다.
        record = {"kind": "review", "key": key, "manifest": selected["rid"],
                  "candidate": candidate, "outcome": outcome, "target": target,
                  "reason": reason.strip(), "distillation": proof}
        if proof:
            cited = {s["ref"] for s in proof["sources"]}
            record["omitted_sources"] = [s["id"] for s in candidate["sources"] if s["id"] not in cited]
        return ledger.ledger_append(growth_ledger.ledger_path(vault), record)


def strict_json(text: str):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def invalid(value):
        raise ValueError("non-finite JSON number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


_TRAILING = {"system", "rate_limit_event"}     # provider events that carry no answer


def _final_packet(output: Path) -> dict:
    """Read provider final messages, never JSON found inside tool output or prose."""
    if output.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("provider output exceeds the 8 MiB review bound")
    text = output.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("no structured final review packet")
    try:
        whole = strict_json(text)
    except ValueError:
        whole = None
    if isinstance(whole, dict) and set(whole) == {"osk_reviews"}:
        return whole
    events = [strict_json(line) for line in text.split("\n") if line.strip()]
    if not events or not all(isinstance(event, dict) for event in events):
        raise ValueError("unknown provider final-output format")
    # Claude Code appends session metadata after its final result: task notifications and
    # summaries (`system`) and rate-limit notices. The answer is the last event that is not
    # such metadata; another assistant turn after a result still means no final result.
    final = next((event for event in reversed(events) if event.get("type") not in _TRAILING), {})
    if final.get("type") == "turn.completed":
        starts = [i for i, event in enumerate(events) if event.get("type") == "turn.started"]
        turn = events[starts[-1]:] if starts else []
        items = [event.get("item", {}) for event in turn if event.get("type") == "item.completed"]
        if (not items or not isinstance(items[-1], dict) or items[-1].get("type") != "agent_message"
                or any(event.get("type") in {"error", "turn.failed"} for event in turn)):
            raise ValueError("Codex has no successful final agent message")
        final_text = items[-1].get("text")
    elif final.get("type") == "result" and final.get("subtype") == "success" and final.get("is_error") is False:
        final_text = final.get("result")
    else:
        raise ValueError("provider output has no successful final result")
    if not isinstance(final_text, str):
        raise ValueError("provider final message is not text")
    packet = _message_packet(final_text)
    if not isinstance(packet, dict) or set(packet) != {"osk_reviews"}:
        raise ValueError("final message must contain only osk_reviews")
    return packet


def _message_packet(text: str):
    """최종 메시지의 결과 묶음 — 메시지 전체이거나, 아니면 행 머리에서 시작해 메시지 끝까지 가는 끝
    블록 하나가 통째로 결과 묶음이어야 한다(#306 D1). 설명 한 줄 뒤에 붙인 묶음은 읽고, 산문 가운데의
    JSON과 코드 펜스 안의 JSON은 읽지 않는다. 엄격 판독(`strict_json`)은 같다."""
    try:
        return strict_json(text)
    except ValueError as whole:
        for m in re.finditer(r"(?m)^\{", text):
            # 앞에 연 펜스가 홀수면 펜스 안이다 — 닫는 표시가 빠진 펜스도 결과로 읽지 않는다(#313 리뷰)
            if m.start() == 0 or len(re.findall(r"(?m)^ {0,3}(?:```|~~~)", text[:m.start()])) % 2:
                continue
            try:
                packet = strict_json(text[m.start():])
            except ValueError:
                continue
            if isinstance(packet, dict) and set(packet) == {"osk_reviews"}:
                return packet
        raise whole


_QUEUE_IDS = {"domain": ("key",), "scope": ("harness", "conversation_id", "through"),
              "organization": ("key",), "eviction": ("of",), "recheck": ("key",)}


def _validate_packet(packet: dict, planned: dict) -> dict:
    """묶음 자체(형식·manifest)가 틀리면 전체를 거절한다. 항목 하나의 잘못은 그 항목만 받지
    않고 사유를 `errors`로 돌려준다 — 선의의 실수 하나로 같은 묶음의 다른 판정을 버리지
    않는다(헌법 1조 3항). 같은 항목이 겹치면 어느 판정인지 정할 수 없어 모두 받지 않는다.
    판정의 뜻이 분명한데 붙은 군더더기(보류의 targets 등)는 떼고 받으며 `notes`로 알린다."""
    reviews = packet.get("osk_reviews")
    if not isinstance(reviews, dict) or not {"manifest", "domain", "scope"} <= set(reviews) <= {"manifest", *_QUEUE_IDS}:
        raise ValueError("review packet needs exactly manifest, domain and scope")
    if reviews["manifest"] != planned["manifest"]:
        raise ValueError("review packet manifest does not match this run")
    if any(not isinstance(reviews.get(q, []), list) for q in _QUEUE_IDS):
        raise ValueError("each review queue must be a list")
    out = {"manifest": reviews["manifest"], "errors": [], "notes": []}
    for queue in _QUEUE_IDS:
        entries = reviews.get(queue, [])
        ids = [_entry_id(queue, e) for e in entries]
        out[queue] = []
        for entry, ident in zip(entries, ids):
            try:
                if ident is not None and ids.count(ident) > 1:
                    raise ValueError("the same item is reviewed more than once in this packet")
                out[queue].append(_check_entry(queue, entry, planned, out["notes"]))
            except ValueError as exc:
                out["errors"].append(f"{queue} review {ident or '?'}: {exc}")
    return out


def _entry_id(queue: str, entry) -> str | None:
    fields = _QUEUE_IDS[queue]
    if not isinstance(entry, dict) or not all(isinstance(entry.get(f), str) for f in fields):
        return None
    return ":".join(entry[f] for f in fields)


def _check_entry(queue: str, entry, planned: dict, notes: list) -> dict:
    """한 항목의 계약. 맞으면 (군더더기를 뗀) 항목을 돌려주고, 아니면 ValueError."""
    ident = _entry_id(queue, entry)
    if queue in ("domain", "scope"):
        allowed = ({c["key"] for c in planned["candidates"]} if queue == "domain" else
                   {":".join((j["harness"], j["conversation_id"], j["through"])) for j in planned["scope_jobs"]})
        required = ({"key", "outcome", "reason"} if queue == "domain" else
                    {"harness", "conversation_id", "through", "outcome", "reason"})
        optional = {"target"} if queue == "domain" else {"targets"}
        if not isinstance(entry, dict) or not required <= set(entry) <= required | optional:
            raise ValueError(f"invalid {queue} review fields")
        if any(not isinstance(entry[k], str) or not entry[k].strip() for k in required):
            raise ValueError(f"{queue} review fields must be nonempty strings")
        if ident not in allowed:
            raise ValueError(f"unselected {queue} review")
        if entry["outcome"] not in {"preserved", "no_value", "deferred"} | ({"summary"} if queue == "scope" else set()):
            raise ValueError(f"invalid {queue} review outcome")
        entry = dict(entry)
        if queue == "domain":
            if "target" in entry and entry["outcome"] != "preserved":
                entry.pop("target")
                notes.append(f"domain review {ident}: only a preserved decision names a target; the target was dropped")
            elif "target" in entry and (not isinstance(entry["target"], str) or not entry["target"].strip()):
                raise ValueError("a preserved Domain review names its target title")
        elif entry["outcome"] in {"preserved", "summary"}:
            field = "key" if entry["outcome"] == "preserved" else "text"
            targets = entry.get("targets")
            if not isinstance(targets, list) or not targets or not all(
                    isinstance(t, dict) and set(t) == {field}
                    and isinstance(t[field], str) and t[field].strip() for t in targets):
                raise ValueError("Scope review needs exact target keys or saved summary excerpts")
        elif "targets" in entry:
            entry.pop("targets")
            notes.append(f"scope review {ident}: no_value and deferred close no target; the targets were dropped")
        return entry
    if queue == "organization":
        jobs = {j["key"]: j for j in planned.get("organization_jobs", [])}
        fields = {"key", "scope", "outcome", "reason"}
        if not isinstance(entry, dict) or not fields <= set(entry) <= fields | {"after", "intentional", "checked"}:
            raise ValueError("invalid organization review fields")
        if any(not isinstance(entry[k], str) or not entry[k].strip() for k in fields):
            raise ValueError("organization review fields must be nonempty strings")
        if entry["key"] not in jobs or jobs[entry["key"]]["scope"] != entry["scope"]:
            raise ValueError("unselected organization review")
        if entry["outcome"] not in {"complete", "deferred"}:
            raise ValueError("invalid organization outcome")
        checked = entry.get("checked", [])
        if not isinstance(checked, list) or any(not isinstance(i, dict) or not isinstance(i.get("unit"), str)
                                                for i in checked):
            raise ValueError("organization checked units must be objects with a unit")
        units = {u["unit"] for u in jobs[entry["key"]].get("review_units", [])}
        kept = [i for i in checked if i["unit"] in units]
        if len(kept) != len(checked):
            notes.append(f"organization review {ident}: {len(checked) - len(kept)} checked unit(s) outside "
                         "this manifest were dropped")
        return {**entry, "checked": kept} if "checked" in entry else entry
    if queue == "eviction":
        fields = {"of", "outcome", "reason"}
        if (not isinstance(entry, dict) or not fields <= set(entry) <= fields | {"target"}
                or any(not isinstance(entry[k], str) or not entry[k].strip() for k in fields)):
            raise ValueError("invalid eviction review fields")
        if entry["of"] not in {j["of"] for j in planned.get("eviction_jobs", [])}:
            raise ValueError("unselected eviction review")
        if entry["outcome"] not in {"node", "merged", "discarded", "deferred"}:
            raise ValueError("invalid eviction outcome")
        if entry["outcome"] in {"node", "merged"}:
            if not isinstance(entry.get("target"), str) or not entry["target"].strip():
                raise ValueError("preserved eviction requires a target title")
        elif "target" in entry:
            entry = {k: v for k, v in entry.items() if k != "target"}
            notes.append(f"eviction review {ident}: discarded and deferred name no target; the target was dropped")
        return entry
    if not isinstance(entry, dict) or entry.get("outcome") not in {"escalated", "deferred"}:
        raise ValueError("a recheck review only escalates or defers; a check closes through update_node")
    fields = {"key", "outcome", "reason"} | ({"proposal"} if entry["outcome"] == "escalated" else set())
    if entry["outcome"] == "deferred" and "proposal" in entry:
        entry = {k: v for k, v in entry.items() if k != "proposal"}
        notes.append(f"recheck review {ident}: a deferral carries no proposal; the proposal was dropped")
    if set(entry) != fields or any(not isinstance(entry[k], str) or not entry[k].strip() for k in fields):
        raise ValueError("invalid recheck review fields")
    if entry["key"] not in {j["key"] for j in planned.get("recheck_jobs", [])}:
        raise ValueError("unselected recheck review")
    return entry


def apply_final_reviews(vault: Vault, output: Path, planned: dict) -> dict:
    try:
        reviews = _validate_packet(_final_packet(output), planned)
    except (ValueError, OSError, TypeError, RecursionError) as exc:
        return {"state": "rejected", "errors": [str(exc)]}
    return _apply_reviews(vault, reviews, planned)


def checkpoint(vault: Vault, packet: dict) -> dict:
    """Apply explicit partial decisions now; never harvest arbitrary worker output."""
    if not isinstance(packet, dict) or set(packet) != {"osk_reviews"}:
        raise ValueError("checkpoint needs one osk_reviews object")
    manifest = packet["osk_reviews"].get("manifest") if isinstance(packet["osk_reviews"], dict) else None
    with locks.mutation_lock(vault):
        row = next((r for r in growth_ledger.ledger_records(vault) if r["kind"] == "plan" and r["rid"] == manifest), None)
    if row is None:
        raise ValueError("checkpoint requires a recorded growth manifest")
    planned = {**row, "manifest": manifest}
    reviews = _validate_packet(packet, planned)
    result = _apply_reviews(vault, reviews, planned)
    return {**result, "ok": not result["errors"]}


def _apply_reviews(vault: Vault, reviews: dict, planned: dict) -> dict:
    result = {"state": "applied", "domain": {}, "scope": {}, "organization": {}, "eviction": {},
              "errors": list(reviews.get("errors", [])), "notes": list(reviews.get("notes", []))}
    for entry in reviews["domain"]:
        key = entry["key"]
        try:
            with locks.mutation_lock(vault):
                done = growth_plan.completed(vault, key, growth_ledger.ledger_records(vault), growth_plan.node_index(vault))
            if done:
                result["domain"][key] = "already_complete"
            else:
                review(vault, **entry, manifest=planned["manifest"])
                result["domain"][key] = "recorded"
        except (ValueError, OSError) as exc:
            result["errors"].append(f"Domain {key}: {exc}")
    for entry in reviews["scope"]:
        key = ":".join(entry[k] for k in ("harness", "conversation_id", "through"))
        try:
            done = conversation_review.review_status(vault, entry["harness"], entry["conversation_id"], entry["through"])
            if done["status"] == "complete":
                result["scope"][key] = "already_complete"
            else:
                conversation_review.acknowledge(vault, **entry)
                result["scope"][key] = "recorded"
        except (ValueError, OSError) as exc:
            result["errors"].append(f"Scope {key}: {exc}")
    selected = {j["key"]: j for j in planned.get("organization_jobs", [])}
    for entry in reviews.get("organization", []):
        try:
            with locks.mutation_lock(vault):
                done = organization.status(vault, selected[entry["key"]])
                # A worker without the CLI cannot read a fresh snapshot, so it is held to the
                # one its plan gave it. Unit keys cover bodies, not references: any change in the
                # scope since the plan — another session's edges or the worker's own writes —
                # keeps the job from completing what was not reviewed. Units it checked that
                # are still unchanged stay as deferred progress, and the units its own writes
                # changed go to another reviewer (organization.review).
                entry = {"after": selected[entry["key"]]["snapshot"], **entry}
            if done["status"] != "complete":
                row = organization.review(vault, **entry)
                if row.get("skipped"):
                    result["notes"].append(f"Organization {entry['key']}: {len(row['skipped'])} checked unit(s) "
                                           "changed after reading and were not recorded")
            result["organization"][entry["key"]] = "recorded"
        except (ValueError, KeyError, OSError) as exc:
            result["errors"].append(f"Organization {entry['key']}: {exc}")
    selected_evictions = {j["of"]: j for j in planned.get("eviction_jobs", [])}
    for entry in reviews.get("eviction", []):
        try:
            with locks.mutation_lock(vault):
                done = growth_plan.eviction_status(vault, selected_evictions[entry["of"]])
                if done.get("reason"):
                    raise ValueError(done["reason"])
                record = {"kind": "eviction_review", "manifest": planned["manifest"], **entry}
                if entry["outcome"] != "deferred":
                    last = done.get("settlement")
                    if last and any(last.get(k) != entry.get(k) for k in ("outcome", "target")):
                        raise ValueError("eviction already has a different disposition")
                    if not last:
                        last = evictions.settle_locked(vault, **entry)
                    record["settlement"] = last["rid"]
                previous = done.get("review") or {}
                if isinstance(previous.get("manifest"), str) and (
                        ledger.rid_key(previous["manifest"]) > ledger.rid_key(planned["manifest"])):
                    result["notes"].append(f"Eviction {entry['of']}: a newer selection already reviewed it; "
                                           "this report is kept as history and does not change it")
                if any(previous.get(k) != v for k, v in record.items()):
                    ledger.ledger_append(growth_ledger.ledger_path(vault), record)
                result["eviction"][entry["of"]] = "recorded"
        except (ValueError, KeyError, OSError) as exc:
            result["errors"].append(f"Eviction {entry['of']}: {exc}")
    selected_rechecks = {j["key"]: j for j in planned.get("recheck_jobs", [])}
    for entry in reviews.get("recheck", []):
        job = selected_rechecks[entry["key"]]
        try:
            with locks.mutation_lock(vault):
                now = [i for i in rechecks.candidates(vault, growth_plan.node_index(vault))[0]
                       if i["id"] == job["id"] and i["key"] == job["target_key"]]
                if not now:
                    result["notes"].append(f"Recheck {entry['key']}: already closed; nothing to record")
                    result.setdefault("recheck", {})[entry["key"]] = "already_closed"
                    continue
                if (now[0]["node_state"], now[0]["target_state"]) != (job["node_state"], job["target_state"]):
                    # 보고는 선택 때의 두 상태에 대한 것으로 남는다. 올림이면 지금 상태와 달라
                    # 표시가 붙지 않아 그 쌍이 다시 선택되고, 보류면 다음 작업이 바뀐 사실을 함께 받는다.
                    result["notes"].append(f"Recheck {entry['key']}: the node or target changed since "
                                           "selection; the report is kept for the selected states")
                if "escalated" not in now[0]:
                    ledger.ledger_append(growth_ledger.ledger_path(vault), {
                        "kind": "recheck_review", "manifest": planned["manifest"], **entry,
                        "node": job["node"], "target": job["target"],
                        "node_state": job["node_state"], "target_state": job["target_state"]})
            result.setdefault("recheck", {})[entry["key"]] = "recorded"
        except (ValueError, OSError) as exc:
            result["errors"].append(f"Recheck {entry['key']}: {exc}")
    if result["errors"]:
        result["state"] = "incomplete"
    return result


def needs_review(vault: Vault, planned: dict, rows: list[dict], idx: graph.Index) -> bool:
    """선택한 작업 가운데 아직 열린 것이 있으면 작업자의 최종 결과 묶음을 적용한다 —
    다섯 큐 모두다. 재검토만 남은 실행도 올림과 보류를 그 묶음으로 보고한다."""
    return (any(not growth_plan.completed(vault, c["key"], rows, idx) for c in planned["candidates"])
            or any(conversation_review.review_status_locked(vault,
                j["harness"], j["conversation_id"], j["through"])["status"] != "complete"
                for j in planned["scope_jobs"])
            or any(organization.status(vault, j, idx)["status"] != "complete"
                   for j in planned["organization_jobs"])
            or any(growth_plan.eviction_status(vault, j, idx)["status"] != "complete" for j in planned["eviction_jobs"])
            or any(growth_plan.recheck_status(vault, j, idx)["status"] != "complete"
                   for j in planned.get("recheck_jobs", [])))
