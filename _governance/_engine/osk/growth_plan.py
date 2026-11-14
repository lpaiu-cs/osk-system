"""osk.growth_plan — 성장이 무엇을 다룰지 고른다 (Mechanism §9-4).

Scope 노드의 비교 묶음과 그것이 아직 지금 것인지, 재검토·정돈·퇴출 작업의 상태, 그리고 큐 사이에
차례를 나누는 선택. 기록부(`growth_ledger`)를 읽어 정하고, 계획을 적는 것은 실행(`growth.run`)이다.

Bounded Scope comparisons; the external agent owns meaning and graph writes. Receipts describe an exact
source set, never a permanently processed node. New sources therefore form new comparisons with older
sources. Only verified saved Domain content, or an explicit no-value decision, suppresses that comparison.
"""
from __future__ import annotations
import itertools
import heapq
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from . import core, distillation, evictions, graph, growth_ledger, organization, rechecks, placement
from .vault import Vault


BATCH_SIZE = 4                 # two batches fit in one eight-source comparison


MAX_DOMAINS = 8


MAX_LIMIT = 20


def node_index(vault: Vault) -> graph.Index:
    idx = graph.Index(vault)
    if idx.scan_errors or idx.broken or idx.dup_ids or idx.dup_stems:
        raise ValueError("growth needs a complete, unambiguous node inventory")
    return idx


def _snapshot(vault: Vault, idx: graph.Index, name: str) -> dict:
    path, kind = idx.nodes[name]
    node = idx.node(path)
    return {"id": node.id, "name": name,
            "path": core.posix_rel(path, vault.root),
            "hash": core.sha256_file(path),
            "summary": str(node.meta.get("summary", "")),
            "scope": kind[1] if kind[0] == "scope" else None}


def _references(idx: graph.Index, name: str) -> set[str]:
    node = idx.node(idx.nodes[name][0])
    refs = set()
    for ref in node.edges("derived-from") + node.wikilinks():
        if ref in idx.by_id:
            refs.add(ref)
        elif ref in idx.nodes:
            refs.add(idx.node(idx.nodes[ref][0]).id)
    return refs


def _key(sources: list[dict]) -> str:
    state = sorted((s["id"], s["hash"]) for s in sources)
    return core.sha256_bytes(json.dumps(state, separators=(",", ":")).encode())


def _source_current(vault: Vault, source: dict, idx: graph.Index) -> bool:
    hit = idx.by_id.get(source["id"])
    return bool(hit and hit[0].stem == source["name"]
                and hit[0].relative_to(vault.root).parts[:2] == Path(source["path"]).parts[:2]
                and core.sha256_file(hit[0]) == source["hash"])


def _proof(vault: Vault, key: str, receipt: dict | None = None) -> dict:
    if receipt is not None:
        return distillation.verify_receipt_locked(vault, receipt)
    return distillation.status_locked(vault, key)


def _current(vault: Vault, candidate: dict, idx: graph.Index, proof: dict | None = None) -> bool:
    if _key(candidate["sources"]) != candidate["key"]:
        return False
    if not all(_source_current(vault, s, idx) for s in candidate["sources"]):
        return False
    target = (proof or {}).get("target") or {}
    for domain in candidate["domains"]:
        if domain["id"] == target.get("id"):
            if (domain["path"] != target.get("path") or domain["name"] != target.get("name")
                    or domain["hash"] not in {target.get("before_hash"), target.get("hash")}):
                return False
        elif not _source_current(vault, domain, idx):
            return False
    return True


def preservation(vault: Vault, candidate: dict, idx: graph.Index, target: str | None = None,
                  saved_proof: dict | None = None) -> dict:
    expected = {(s["id"], s["hash"]) for s in candidate["sources"]}
    keys = [candidate.get("distill_key", candidate["key"])] + [
        p["key"] for p in candidate.get("previous_distillations", [])]
    saved = {p["key"]: p for p in candidate.get("previous_distillations", [])
             if p.get("status") == "complete"}
    if saved_proof and saved_proof.get("key") in keys:
        saved[saved_proof["key"]] = saved_proof
    for key in keys:
        proof = _proof(vault, key, saved.get(key))
        if proof.get("status") != "complete":
            continue
        output = proof.get("target") or {}
        hit = idx.by_id.get(output.get("id"))
        if not hit or hit[1][0] != "domain" or (target and output.get("name") != target):
            continue
        actual = {(s.get("ref"), s.get("hash")) for s in proof.get("sources", [])}
        if actual and actual.issubset(expected) and _current(vault, candidate, idx, proof):
            return proof
    raise ValueError("no complete current Domain distillation covers a valid subset of this comparison")


def completed(vault: Vault, key: str, rows: list[dict], idx: graph.Index, par=None) -> dict | None:
    row = growth_ledger.latest_report(rows, key, "key", par)
    if not row or row.get("kind") != "review" or row.get("outcome") == "deferred":
        return None
    candidate = row.get("candidate")
    if not isinstance(candidate, dict) or not str(row.get("reason", "")).strip():
        return None
    try:
        if row["outcome"] == "preserved":
            preservation(vault, candidate, idx, row.get("target"), row.get("distillation"))
        elif not _current(vault, candidate, idx):
            return None
    except (ValueError, KeyError, OSError):
        return None
    return row


def _recheck_jobs(vault: Vault, idx: graph.Index, scope: str | None = None) -> list[dict]:
    """근거 재검토 후보를 작업으로 낸다(시행령 §7 2항). `scope`를 주면 그 scope의
    노드만 — Stop fork는 원 대화의 scope만 다룬다(Mechanism §9-4 3항)."""
    items, pending = rechecks.candidates(vault, idx)
    return [] if pending else [
        {"key": f"recheck:{i['id']}:{i['key']}", "node": i["node"], "target": i["target"],
         "why": i["why"], "cascade": i["cascade"], "next": i["next"], "id": i["id"],
         "target_key": i["key"], "node_state": i["node_state"], "target_state": i["target_state"],
         **({"previous_deferral": i["deferred"]} if "deferred" in i else {})}
        for i in items if "escalated" not in i and (scope is None or i["scope"] == scope)]


def _with_change(vault: Vault, jobs: list[dict], idx: graph.Index) -> list[dict]:
    """선택한 작업에만 마지막 점검 뒤의 변경분을 싣는다 — 후보 전부가 아니다."""
    for job in jobs:
        job["change"] = rechecks.change(vault, job["id"], job["target_key"], job["target"], idx)
    return jobs


def pick_rechecks(vault: Vault, idx: graph.Index, limit: int, scope: str | None = None,
                   rows: list[dict] | None = None) -> list[dict]:
    """고를 재검토 작업 — 시도한 적 없는 것부터, 그다음 가장 오래전에 시도한 것부터
    `limit`개. 정기 실행과 Stop fork가 같이 쓴다: 자르고 나서 돌리면 앞쪽이 계속
    열려 있을 때 뒤쪽이 영영 뽑히지 않는다."""
    rows = growth_ledger.ledger_records(vault) if rows is None else rows
    tried = {j["key"]: r["rid"] for r in rows if r.get("kind") == "plan"
             for j in r.get("recheck_jobs", [])}
    jobs = sorted(_recheck_jobs(vault, idx, scope), key=lambda j: (tried.get(j["key"], ""), j["key"]))
    return _with_change(vault, jobs[:limit], idx)


def recheck_status(vault: Vault, job: dict, idx: graph.Index) -> dict:
    """그 쌍이 더는 후보가 아니면 완료다 — `rechecked`로 닫았거나 근거를 뺐다."""
    hit = idx.by_id.get(job["id"])
    if hit is None:
        return {"status": "complete", "reason": "node gone"}
    meta = idx.node(hit[0]).meta
    if job["target_key"] not in rechecks.pairs(idx, meta):
        # 근거를 뺐거나, 남아 있지만 대상 파일이 사라졌다(dangling으로 따로 보고된다).
        stored = meta.get("derived-from") or []
        kept = job["target"] in (stored if isinstance(stored, list) else [stored])
        return {"status": "complete", "reason": "basis dangling" if kept else "basis removed"}
    if job["target_key"] in rechecks.complete_keys(vault, idx, hit[0], meta):
        return {"status": "complete"}
    open_ = [i for i in rechecks.candidates(vault, idx)[0]
             if i["id"] == job["id"] and i["key"] == job["target_key"]]
    if open_ and "escalated" in open_[0]:
        return {"status": "complete", "reason": "escalated to the user"}
    return {"status": "pending"}


def plan_locked(vault: Vault, limit: int) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    idx, rows = node_index(vault), growth_ledger.ledger_records(vault)
    sources, domains, clusters, refs = {}, {}, {}, {}
    for name, (path, kind) in sorted(idx.nodes.items()):
        if kind[0] not in {"scope", "domain"} or placement.is_hub(path):
            continue
        snap = _snapshot(vault, idx, name)
        refs[snap["id"]] = _references(idx, name)
        if kind[0] == "scope":
            sources[snap["id"]] = snap
            clusters.setdefault(core.posix_rel(path.parent, vault.root), []).append(snap)
        else:
            domains[snap["id"]] = snap
    batches = [items[i:i + BATCH_SIZE] for items in clusters.values()
               for i in range(0, len(items), BATCH_SIZE)]

    def groups():
        # Existing graph relations get first look, without guessing semantic similarity.
        for domain_id in domains:
            linked = [s for sid, s in sources.items()
                      if sid in refs[domain_id] or domain_id in refs[sid]]
            for i in range(0, len(linked), 2 * BATCH_SIZE):
                if linked[i:i + 2 * BATCH_SIZE]:
                    yield "domain", linked[i:i + 2 * BATCH_SIZE]
        for batch in batches:
            yield "cluster", batch
        # ponytail: O(batches²) comparison inventory; add persisted cursors if the
        # scan becomes costly. Each model call still receives at most limit × 8 sources.
        for left, right in itertools.combinations(batches, 2):
            yield "comparison", left + right

    plans = [row for row in rows if row.get("kind") == "plan"]
    attempts = {c["key"]: row["rid"] for row in plans
                for c in row.get("candidates", [])}
    seen_versions = {(s["id"], s["hash"]) for row in plans
                     for c in row.get("candidates", []) for s in c["sources"]}
    recent = {c["key"] for c in plans[-1].get("candidates", [])} if plans else set()
    def fresh(candidate):
        return any((s["id"], s["hash"]) not in seen_versions for s in candidate["sources"])

    reviewed = {row.get("key") for row in rows if row.get("kind") == "review"}
    par = core.effective_parents(rows)
    rotation = None
    def pending():
        nonlocal rotation
        seen = set()
        for grouping, batch in groups():
            batch = sorted(batch, key=lambda s: s["id"])
            key = _key(batch)
            if key in seen:
                continue
            seen.add(key)
            if key in reviewed and completed(vault, key, rows, idx, par):
                continue
            candidate = {"key": key, "grouping": grouping, "sources": batch}
            # Do not immediately retry the last batch while alternatives remain.
            if rotation is None or (key in recent, fresh(candidate), attempts.get(key, "")) < (
                    rotation["key"] in recent, fresh(rotation), attempts.get(rotation["key"], "")):
                rotation = candidate
            yield candidate

    # Prefer unseen source versions. One slot keeps older comparisons moving;
    # with limit=1, alternate priority/rotation using recorded plans, never previews.
    # A plan is an attempt, not proof that its sources were read or distilled.
    candidates = heapq.nsmallest(limit, pending(), key=lambda c: (
        not fresh(c), attempts.get(c["key"], "")))
    if rotation and (limit > 1 or len(plans) % 2) and rotation not in candidates:
        candidates[-1] = rotation
    for candidate in candidates:
        batch = candidate["sources"]
        ids = {s["id"] for s in batch}
        related = [d for did, d in domains.items()
                   if refs[did] & ids or any(did in refs[sid] for sid in ids)]
        available = related + [d for d in domains.values() if d not in related]
        candidate.update(domains=available[:MAX_DOMAINS],
                         other_domains=max(0, len(available) - MAX_DOMAINS))
        last_review = growth_ledger.latest_report(rows, candidate["key"], "key", par)
        if last_review and last_review.get("outcome") == "deferred":
            candidate["previous_deferral"] = deferral(last_review)
        previous = [c["distill_key"] for row in rows if row.get("kind") == "plan"
                    for c in row.get("candidates", []) if c.get("key") == candidate["key"]
                    and c.get("distill_key")]
        candidate["previous_distillations"] = []
        for old_key in dict.fromkeys(previous[-3:]):
            old_proof = _proof(vault, old_key)
            if old_proof.get("target"):
                candidate["previous_distillations"].append(old_proof)
    organization_jobs = organization.pending(vault, limit=limit, idx=idx)
    attempts = {j["of"]: row["rid"] for row in plans for j in row.get("eviction_jobs", [])}
    eviction_rows = evictions.records(vault)
    settled = {r["of"] for r in eviction_rows if r["kind"] == "settle"}
    eviction_jobs = []
    for r in eviction_rows:
        if r["kind"] != "evict" or (r["rid"] not in attempts and (
                r["rid"] in settled or evictions.age_days(r) <= evictions.N_DAYS)):
            continue
        job = {"key": "eviction:" + r["rid"], "of": r["rid"], "scope": r["scope"],
               "text": r["text"], "age_days": evictions.age_days(r)}
        state = eviction_status(vault, job, idx, growth_rows=rows, eviction_rows=eviction_rows,
                                 growth_par=par)
        if state["status"] == "complete":
            continue
        prior = state.get("review")
        if prior and prior["outcome"] == "deferred":
            job["previous_deferral"] = deferral(prior)
        if state.get("settlement"):
            job["previous_settlement"] = {k: state["settlement"][k]
                                          for k in ("rid", "outcome", "target") if k in state["settlement"]}
        eviction_jobs.append(job)
    eviction_jobs.sort(key=lambda j: (attempts.get(j["of"], ""), j["of"]))
    eviction_jobs = eviction_jobs[:limit]
    recheck_jobs = pick_rechecks(vault, idx, limit, rows=rows)
    return {"candidates": candidates, "organization_jobs": organization_jobs, "eviction_jobs": eviction_jobs,
            "recheck_jobs": recheck_jobs, "source_count": len(sources),
            "cluster_count": len(clusters), "domain_count": len(domains),
            "limit": limit, "max_sources_per_candidate": 2 * BATCH_SIZE}


def plan(vault: Vault, limit: int = 3) -> dict:
    """Read-only bounded inventory of changed/unreviewed comparison sets."""
    with core.mutation_lock(vault):
        return plan_locked(vault, limit)


def scope_tried(rows: list[dict], now: datetime | None = None):
    """Scope 대화를 지금 자리(검토한 라운드 수·복구 대기)에서 고른 횟수 — 정기 실행의 시도만 센다(#306
    D4). 대화 검토 fork의 시도, 결과가 없거나 대기(한도 거절·잠금)였던 실행은 작업자가 검토하지 못한 것이라
    세지 않는다. 작업자가 실패한 실행(`run_failed`)은 최근 7일 것만 센다 — 그 대화가 실패를 부르면 다른
    대화에 차례를 넘기되(#136 M4), 오래전 한 번의 실패로 대기열 맨 뒤에 묶이지 않는다. 진척하면 자리가
    바뀌어 다시 0이다."""
    since = (now or datetime.now(timezone.utc)) - timedelta(days=STALLED_DAYS)
    runs = {r.get("manifest"): r for r in rows if r.get("kind") == "run"}

    def counted(plan: dict) -> bool:
        run = runs.get(plan.get("rid"))
        if plan.get("work_context", "daily") != "daily" or not run or run.get("state") in growth_ledger.WAITS:
            return False
        try:
            return not growth_ledger.run_failed(run) or datetime.fromisoformat(run["at"]) >= since
        except (KeyError, TypeError, ValueError):
            return False

    seen = Counter((j.get("harness"), j.get("conversation_id"), j.get("reviewed_rounds"),
                    tuple(sorted(j.get("repair_pending") or {})))
                   for r in rows if r.get("kind") == "plan" and counted(r) for j in r.get("scope_jobs", []))
    return lambda s: seen[(s["harness"], s["conversation_id"], s["reviewed_count"],
                           tuple(sorted(s.get("repair_pending") or {})))]


STALLED_PICKS, STALLED_DAYS = 2, 7


def _job_place(job: dict) -> tuple:
    """정체를 셀 작업의 자리. 재검토는 key와 인용 노드·근거의 상태다 — key는 참조 관계뿐이고, 두 상태가
    바뀌면 엔진이 그 쌍을 다시 고른다. 정돈은 key다 — 마치지 않은 검토의 key를 이어 쓰는데
    (`organization.pending`), snapshot은 scope 노드의 해시를 모두 담아 그 scope의 어느 쓰기에도 바뀐다.
    실제 원장에서는 고를 때마다 달라 정체를 하나도 세지 못했다(#268). 정체로 빠진 정돈도 하루 묶음은
    그대로 고른다."""
    return job["key"], job.get("node_state"), job.get("target_state")


def stalled(rows: list[dict], now: datetime | None = None) -> set:
    """지난 7일 동안 진척 없이 두 번 이상 끝난 작업의 자리(`_job_place`) — 진척하면 다시 센다. 자리는
    그 실행의 계획에 실린 작업에서 읽는다(#259). 대기(한도 거절·잠금)와 실패한 실행(`run_failed`)은
    작업자가 검토하지 못한 것이라 진척 없음으로 세지 않는다."""
    since = (now or datetime.now(timezone.utc)) - timedelta(days=STALLED_DAYS)
    plans = {r.get("rid"): r for r in rows if r.get("kind") == "plan"}
    misses = Counter()
    for r in rows:
        if r.get("kind") != "run" or not isinstance(r.get("progress"), dict):
            continue
        try:
            if datetime.fromisoformat(r["at"]) < since:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        jobs = {j["key"]: j for queue in growth_ledger.QUEUES for j in (plans.get(r.get("manifest")) or {}).get(queue, [])
                if isinstance(j, dict) and "key" in j}
        for key in r["progress"].get("made", []):
            misses.pop(_job_place(jobs.get(key) or {"key": key}), None)
        if r.get("state") not in growth_ledger.WAITS and not growth_ledger.run_failed(r):
            misses.update(_job_place(jobs.get(key) or {"key": key}) for key in r["progress"].get("none", []))
    return {place for place, n in misses.items() if n >= STALLED_PICKS}


def skip_stalled(jobs: list[dict], stalled: set, limit: int, planned: dict) -> list[dict]:
    """대화별 검토가 고를 작업 — 거듭 진척 없던 자리를 빼고 그 뒤의 후보로 채워 `limit`개. 뺀 key는
    계획에 남긴다(#259, #175 결정 2). 하루 묶음은 그날 진척 없던 작업을 뺀다(`growth_daily`)."""
    skipped = [j["key"] for j in jobs if _job_place(j) in stalled]
    if skipped:
        planned.setdefault("stalled", []).extend(skipped)
    return [j for j in jobs if _job_place(j) not in stalled][:limit]


def select_work(planned: dict, limit: int, rows: list[dict]) -> None:
    """Share the run budget across queues; recorded attempts drive fair rotation."""
    last = {key: -1 for key in growth_ledger.QUEUES}
    last_first = dict(last)
    for key in growth_ledger.QUEUES:
        planned.setdefault(key, [])
    for number, row in enumerate(rows):
        if (row.get("kind") == "plan" and
                row.get("work_context", "daily") == planned.get("work_context", "daily")):
            for key in growth_ledger.QUEUES:
                if row.get(key):
                    last[key] = number
            if row.get("work_order"):
                last_first[row["work_order"][0]["queue"]] = number
    order = sorted(growth_ledger.QUEUES, key=last.get)
    # 첫 차례가 가장 오래된 큐를 먼저 뽑는다 — 한도가 큐 수보다 작아도 첫 차례가
    # 큐마다 한 번씩 돈다(첫 작업 뒤 시간이 다 된 워커에도 굶는 큐가 없다).
    lead = min((key for key in growth_ledger.QUEUES if planned[key]), key=last_first.get, default=None)
    if lead is not None:
        order.remove(lead)
        order.insert(0, lead)
    selected = {key: [] for key in growth_ledger.QUEUES}
    work_order = []
    for _ in range(limit):
        for key in order:
            if len(selected[key]) < len(planned[key]):
                selected[key].append(planned[key][len(selected[key])])
                work_order.append({"queue": key, "index": len(selected[key]) - 1})
                order.remove(key)
                order.append(key)
                break
        else:
            break
    if work_order:
        # Selection recency shares the budget; first-turn recency protects queues
        # from workers that time out before reaching their later selected jobs.
        first = min(range(len(work_order)), key=lambda i: last_first[work_order[i]["queue"]])
        work_order.insert(0, work_order.pop(first))
    planned["queued_not_selected"] = {key: len(planned[key]) - len(selected[key]) for key in growth_ledger.QUEUES}
    planned.update(selected)
    planned["work_order"] = work_order


def deferral(review: dict) -> dict:
    reason = review.get("reason", "")
    return {"through": review.get("through"), "reason": reason[:1200],
            "reason_truncated": len(reason) > 1200}


def eviction_status(vault: Vault, job: dict, idx=None, *, growth_rows=None, eviction_rows=None,
                     growth_par=None) -> dict:
    rows = evictions.records(vault) if eviction_rows is None else eviction_rows
    growth_rows = growth_ledger.ledger_records(vault) if growth_rows is None else growth_rows
    original = next((r for r in rows if r["rid"] == job["of"] and r["kind"] == "evict"), None)
    if not original or any(original[k] != job[k] for k in ("scope", "text")):
        return {"status": "pending", "reason": "selected eviction source changed"}
    review = growth_ledger.latest_report(growth_rows, job["of"], "of", growth_par)
    result = {"status": "pending", "review": review, "semantic_verified": False}
    settled = [r for r in rows if r["kind"] == "settle" and r["of"] == job["of"]]
    if not settled:
        return result
    last = settled[-1]
    result["settlement"] = last
    try:
        if last["outcome"] != "discarded":
            evictions.require_target(last.get("target", ""), idx or node_index(vault))
    except ValueError as exc:
        return {**result, "reason": str(exc)}
    reviewed_job = next((j for r in growth_rows if review and r["kind"] == "plan"
                         and r["rid"] == review["manifest"] for j in r.get("eviction_jobs", [])
                         if j["of"] == job["of"]), {})
    if (review and review["outcome"] != "deferred" and review.get("settlement") == last["rid"]
            and all(review.get(k) == last.get(k) for k in ("outcome", "target"))
            and all(reviewed_job.get(k) == job[k] for k in ("scope", "text"))):
        result["status"] = "complete"
    return result
