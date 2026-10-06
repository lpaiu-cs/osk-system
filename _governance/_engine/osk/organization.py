"""Persistent review of reference roles and the organization of a touched Scope.

Knowledge files themselves retain unresolved work. A version-bound review only
suppresses the exact inspected state; no new model runner or graph store exists.
"""
from __future__ import annotations
from .core import DOMAIN, SCOPE

from bisect import bisect_right
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import core, contract, graph, write

REVIEW_CHARS = 4000  # Same bounded range returned by read_node.
REVIEW_UNITS = 3
# 계획 뒤 검토자가 고친 구간은 다른 검토자에게 넘긴다. update_node는 검토자 신원을
# 싣지 않으므로 시간으로 가른다 — 정기 실행은 하루 간격이고 한 세션은 반나절 안이다.
# ponytail: 시간 냉각은 신원의 근사다. 쓰기가 검토자를 싣게 되면 신원으로 가른다.
HANDOFF_SECONDS = 12 * 3600
# 문단 경계가 없는 긴 문단에서 쓸 차선의 경계 — 줄바꿈이나 문장 끝.
_SOFT_BREAK = re.compile(r"\n|[.!?。](?=\s)")


def _units(node) -> list[dict]:
    """Stable paragraph-bounded excerpts; appending does not invalidate earlier pages."""
    body, result, start, occurrences = node.body, [], 0, {}
    breaks = [m.end() for m in re.finditer(r"\n\n+|\n(?=#{1,6} )", body)]
    while start < len(body):
        end = min(start + REVIEW_CHARS, len(body))
        if end < len(body):
            cut = bisect_right(breaks, end)
            if cut and breaks[cut - 1] > start:
                end = breaks[cut - 1]
            else:
                # 창 안에 문단 경계가 없다 — 문장 중간에서 자르지 않도록 마지막 줄바꿈이나
                # 문장 끝에서 자르고, 그것도 없을 때만 REVIEW_CHARS에서 끊는다.
                soft = [m.end() for m in _SOFT_BREAK.finditer(body, start, end)]
                if soft and soft[-1] > start:
                    end = soft[-1]
        text = body[start:end]
        if text.strip():
            # The summary belongs to the first range. Updating it must not
            # invalidate every already-reviewed historical section.
            context = str(node.meta.get("summary", "")) + "\0" if not result else ""
            digest = core.sha256_bytes((context + text).encode())
            count = occurrences.get(digest, 0)
            occurrences[digest] = count + 1
            result.append({"unit": f"{node.id}:{digest}:{count}", "id": node.id,
                           "name": node.path.stem, "view": f"{start}:{end}", "chars": len(text)})
        start = end
    return result


def _remaining(current: dict, state: dict) -> list[dict]:
    covered = state.get("coverage", {}).get(current["scope"], {})
    return [u for u in current["units"] if u["unit"] not in covered]


def _state_path() -> Path:
    # Worktrees share the mutation lock, but selections belong to this checkout.
    root = os.path.normcase(str(core.ROOT.resolve()))
    key = hashlib.sha256(root.encode()).hexdigest()[:16]
    return core.local_lock_path(f"osk-organization-{key}.json")


def _load() -> dict:
    p = _state_path()
    if not p.exists():
        return {"version": 1, "plans": {}, "reviews": {}}
    state = json.loads(p.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not all(isinstance(state.get(k), dict) for k in ("plans", "reviews")):
        raise ValueError("organization state is damaged; pending")
    return state


def _save(state: dict) -> None:
    write._atomic_write(_state_path(), json.dumps(state, ensure_ascii=False, sort_keys=True).encode())


def _retire_arrived_moves(state: dict, idx) -> None:
    """Persist arrival, so a later legitimate placement cannot revive old intent."""
    for key, move in list(state.get("moves", {}).items()):
        remaining = []
        for item in move["items"]:
            # The destination is known; an ID lookup would parse the whole vault.
            path = core.resolve_in_root(item["to"])
            if path is None or not path.is_file() or idx.node(path).id != item["id"]:
                remaining.append(item)
        if remaining:
            move["items"] = remaining
        else:
            del state["moves"][key]


def record_move(plans: list, hub_links: list, idx) -> str:
    """Caller holds mutation lock; record intent before the first filesystem move."""
    items = [{"id": n.id, "name": src.stem, "from": core.posix_rel(src, core.ROOT),
              "to": core.posix_rel(dst, core.ROOT), "hash": core.sha256_file(src)}
             for src, dst, n in plans]
    key = core.sha256_bytes(json.dumps(items, sort_keys=True).encode())
    state = _load()
    # Also recover arrival if a previous process stopped before its checkpoint.
    _retire_arrived_moves(state, idx)
    state.setdefault("moves", {})[key] = {"items": items, "hub_links": hub_links}
    _save(state)
    return key


def pending_moves(scope: str, idx) -> list:
    result = []
    for key, move in _load().get("moves", {}).items():
        items = []
        for item in move["items"]:
            if Path(item["from"]).parts[:2] != _scope_path(scope).relative_to(core.ROOT).parts:
                continue
            path = write._live_locate(item["id"], idx)
            current = core.posix_rel(path, core.ROOT) if path else None
            if current != item["to"]:
                items.append(dict(item, current=current,
                                  intact=bool(path and core.sha256_file(path) == item["hash"])))
        if items:
            result.append({"key": key, "remaining": items, "hub_links": move["hub_links"]})
    return result


def checkpoint_moves(idx) -> None:
    # ponytail: rewrite the small journal per arrival; batch/append progress if large moves make this costly.
    state = _load()
    _retire_arrived_moves(state, idx)
    _save(state)


def _index():
    idx = graph.Index()
    if idx.scan_errors or idx.broken or idx.dup_ids or idx.dup_stems:
        raise ValueError("organization needs a complete, unambiguous inventory")
    return idx


def _scope_path(scope: str) -> Path:
    if not isinstance(scope, str) or not scope or "\\" in scope:
        raise ValueError("scope must name one existing Scope or Domain cluster")
    parts = scope.split("/") if scope.startswith((DOMAIN + '/')) else [SCOPE, scope]
    if len(parts) != 2 or not parts[1] or "/" in parts[1] or parts[1] in {".", "..", "Workbench"}:
        raise ValueError("organization requires an existing top-level Scope or Domain cluster")
    p = core.resolve_in_root(Path(*parts))
    if p is None or not p.is_dir():
        raise ValueError("organization requires an existing Scope or Domain cluster")
    return p


def snapshot(scope: str, idx=None) -> dict:
    """Read bytes once per node; do not combine cached bodies with current hashes."""
    base = _scope_path(scope)
    idx = _index() if idx is None else idx
    nodes, parsed, references, units = [], {}, [], []
    for name, (path, kind) in sorted(idx.nodes.items()):
        if not path.is_relative_to(base):
            continue
        data = path.read_bytes()
        node = contract.parse_bytes(path, data)
        parsed[path] = node
        digest = core.sha256_bytes(data)
        # read_node(view=...)가 이 바이트에서 돌려줄 view_hash다. unit 속 sha256은 구간
        # 내용 키라 view_hash와 같을 수 없다 — 작업자가 그것과 비교해 결정을 버렸다(10-04).
        units.extend({**u, "expect_view_hash": "view:" + digest} for u in _units(node))
        nodes.append({"id": node.id, "name": name, "path": core.posix_rel(path, core.ROOT),
                      "hash": digest, "summary": str(node.meta.get("summary", "")),
                      "hub": graph.is_hub(path), "has_body": bool(write._norm_body(node.body)),
                      "body_chars": len(node.body), **write.size_feedback(path, node.body)})
        references.extend(graph.reference_review(node, idx))
    links = {}
    for p, node in parsed.items():
        links[p] = {write._live_locate(ref, idx) for ref in node.wikilinks()}
    clusters, issues = [], []
    directories = {d for p in parsed for d in p.parents if d.is_relative_to(base)}
    for directory in sorted(directories):
        hub = directory / (directory.name + ".md")
        local = {p for p in parsed if p.parent == directory and p != hub}
        children = {p for p in parsed if graph.is_hub(p) and p.parent.parent == directory}
        wanted = local | children
        linked = links.get(hub, set())
        missing = sorted(p.stem for p in wanted - linked)
        bypass = sorted(p.stem for p in linked if p in parsed and p != hub and p not in wanted)
        item = {"path": core.posix_rel(directory, core.ROOT),
                "hub": hub.stem if hub in parsed else None,
                "local_nodes": len(local), "child_hubs": sorted(p.stem for p in children),
                "missing_links": missing, "branch_bypass": bypass}
        clusters.append(item)
        if hub not in parsed or missing or bypass:
            issues.append(item)
    version = core.sha256_bytes(json.dumps(
        sorted((n["id"], n["path"], n["hash"]) for n in nodes), separators=(",", ":")).encode())
    return {"scope": scope, "key": "organization:" + version, "snapshot": version,
            "nodes": nodes, "units": units, "clusters": clusters, "issues": issues, "references": references,
            "pending_moves": pending_moves(scope, idx)}


def hub_tree(base: Path, idx=None) -> str:
    """그 scope의 허브 트리(Mechanism §9-4 3항) — 허브가 있는 군집마다 군집 경로·직속 노드 수·
    허브 요약을 한 줄로 싣고, 들여쓰기로 상하를 보인다. 노드는 수만 센다. 허브 없는 폴더는
    군집이 아니므로(자리가 될 수 없다) 싣지 않는다."""
    idx = _index() if idx is None else idx
    members: dict[Path, list[Path]] = {}
    for _name, (path, _kind) in idx.nodes.items():
        if path.is_relative_to(base):
            members.setdefault(path.parent, []).append(path)
    lines = []
    for d in sorted(members, key=lambda p: p.relative_to(base).parts):
        # 허브 판정은 graph.is_hub 그대로다 — 확장자의 대소문자를 가리지 않는다(Mechanism §1 4항)
        hub = next((p for p in members[d] if graph.is_hub(p)), None)
        if hub is None:
            continue
        try:
            summary = str(idx.node(hub).meta.get("summary", ""))
        except Exception:
            summary = ""
        lines.append("  " * len(d.relative_to(base).parts)
                     + f"- {core.posix_rel(d, core.ROOT)} · 직속 노드 {len(members[d]) - 1} — {summary}")
    shown, size = [], 0
    for line in lines:
        # ponytail: 실측 최대 허브 9개·852자다. 넘치면 깊은 순서를 따라 자르고 남은 수만 알린다.
        if size + len(line) > HUB_TREE_CAP:
            shown.append(f"… 허브 {len(lines) - len(shown)}개 더")
            break
        shown.append(line)
        size += len(line) + 1
    return "\n".join(shown)


def _ref_key(item: dict) -> tuple:
    return item["id"], item["relation"], item["ref"]


def _unresolved(current: dict, intentional: list) -> list:
    accepted = {_ref_key(item) for item in intentional}
    return [item for item in current["references"]
            if not (item["relation"] == "Link" and _ref_key(item) in accepted)]


def _complete(scope: str, state: dict, current: dict) -> bool:
    row = state["reviews"].get(scope)
    return bool(row and row.get("outcome") == "complete" and
                row.get("after") == current["snapshot"] and not current["issues"] and not current["pending_moves"] and
                not _remaining(current, state) and
                not _unresolved(current, row.get("intentional", [])))


def _cooling(state: dict, scope: str) -> set:
    """넘김 중인 구간 — 계획 뒤 검토자가 고친 노드의 구간으로, 아직 냉각 시간 안이다."""
    now = time.time()
    return {unit for unit, at in state.get("handoff", {}).get(scope, {}).items()
            if isinstance(at, (int, float)) and now - at < HANDOFF_SECONDS}


def pending(scopes=None, limit: int = 3, *, idx=None, record: bool = False) -> list[dict]:
    """Caller holds the vault lock. Pending references survive missing local state."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise ValueError("organization limit must be 1..20")
    idx = _index() if idx is None else idx
    state, jobs = _load(), []
    scopes = sorted(set(scopes if scopes is not None else
                        [k[1] if k[0] == "scope" else (DOMAIN + '/') + k[1]
                         for _, k in idx.nodes.values() if k[0] in {"scope", "domain"}] +
                        [p["scope"] for p in state["plans"].values()]))
    attempts = {p["scope"]: p.get("last_attempt", "") for p in state["plans"].values()}
    scopes.sort(key=lambda name: (attempts.get(name, ""), name))
    # ponytail: Scope inventories are read on maintenance, never on every write.
    # Use persisted change hints only if this bounded readout becomes costly.
    for scope in scopes:
        prior = next((p for p in state["plans"].values() if p["scope"] == scope), None)
        reviewed = state["reviews"].get(scope, {})
        unfinished = prior and not (reviewed.get("key") == prior["key"] and reviewed.get("outcome") == "complete")
        if not prior and not any((k[0] == "scope" and k[1] == scope) or
                                (k[0] == "domain" and (DOMAIN + '/') + k[1] == scope)
                                for _, k in idx.nodes.values()):
            continue  # A raw-only capture has no knowledge organization to review yet.
        current = snapshot(scope, idx)
        missing_ids = sorted({n["id"] for n in prior["nodes"]} - {n["id"] for n in current["nodes"]}) if prior else []
        if missing_ids:
            current["missing_ids"] = missing_ids
        if (not prior and all(n["hub"] for n in current["nodes"]) and not current["references"]
                and not current["issues"] and not current["pending_moves"]
                and not any(n.get("organization_advice") for n in current["nodes"])):
            continue
        if _complete(scope, state, current):
            continue
        remaining = _remaining(current, state)
        sizes = {n["id"]: n["body_chars"] for n in current["nodes"]}
        remaining.sort(key=lambda u: -sizes[u["id"]])
        cooling = _cooling(state, scope)
        offered = [u for u in remaining if u["unit"] not in cooling]
        if (remaining and not offered and not current["issues"] and not current["references"]
                and not current["pending_moves"]):
            continue  # 남은 구간이 모두 넘김 중이다 — 고친 검토자가 아닌 다음 검토자의 몫
        current["review_units"] = offered[:REVIEW_UNITS]
        current["coverage"] = {"remaining": len(remaining), "total": len(current.pop("units"))}
        if len(offered) < len(remaining):
            current["coverage"]["handoff"] = len(remaining) - len(offered)
        current["reason"] = "references or changed knowledge need organization review"
        current["review_command"] = "python -m osk.cli organization review (UTF-8 JSON stdin)"
        if unfinished or missing_ids:
            current["key"] = prior["key"]
        if reviewed.get("outcome") == "deferred":
            current["previous_deferral"] = {
                k: reviewed[k] for k in ("key", "reason", "after", "at")}
            current["previous_deferral"]["snapshot_changed"] = reviewed["after"] != current["snapshot"]
        current["hub_tree"] = hub_tree(_scope_path(scope), idx)
        jobs.append(current)
        if len(jobs) == limit:
            break
    if record:
        record_attempts(jobs)
    return jobs


def record_attempts(jobs: list[dict]) -> None:
    """Caller holds the vault lock; register only the jobs actually selected."""
    if not jobs:
        return
    state = _load()
    for current in jobs:
        scope, key = current["scope"], current["key"]
        # The selected key retains the original unfinished snapshot across edits.
        state["plans"] = {k: p for k, p in state["plans"].items() if p["scope"] != scope or k == key}
        state["plans"].setdefault(key, dict(current))
        # Preserve every identity selected during an unfinished review, including
        # destinations created by an earlier batch of the same review.
        state["plans"][key]["nodes"] = list({n["id"]: n for n in
            state["plans"][key]["nodes"] + current["nodes"]}.values())
        state["plans"][key]["review_units"] = current["review_units"]
        state["plans"][key]["last_attempt"] = core.now_kst()
    _save(state)


def plan(scope: str, *, record: bool = True) -> dict:
    with core.mutation_lock():
        _scope_path(scope)
        jobs = pending([scope], limit=1, record=record)
        if jobs:
            return jobs[0]
        # 남은 구간이 모두 넘김 중이라 고르지 않은 것은 완료가 아니다 — 다음 검토자의 차례다
        state = _load()
        waiting = [u for u in _remaining(snapshot(scope), state) if u["unit"] in _cooling(state, scope)]
        if waiting:
            return {"scope": scope, "status": "handoff", "handoff": len(waiting),
                    "reason": "remaining ranges were corrected by their last reviewer; "
                              "another reviewer takes them after the cooling period"}
        return {"scope": scope, "status": "complete"}


def review(key: str, scope: str, outcome: str, reason: str, after: str = "",
           intentional: list | None = None, checked: list | None = None) -> dict:
    """Check saved state, not a claim that a plan was executed."""
    if outcome not in {"complete", "deferred"} or not isinstance(reason, str) or not reason.strip():
        raise ValueError("organization outcome and concrete reason required")
    intentional = [] if intentional is None else intentional
    checked = [] if checked is None else checked
    if (not isinstance(checked, list) or len(checked) > REVIEW_UNITS or any(
            not isinstance(i, dict) or set(i) != {"unit", "reason"} or
            not all(isinstance(v, str) and v.strip() for v in i.values()) for i in checked)
            or len({i["unit"] for i in checked}) != len(checked)):
        raise ValueError("checked must contain at most three distinct units and their claim/conditions assessment")
    if not isinstance(intentional, list) or any(
            not isinstance(i, dict) or set(i) != {"id", "relation", "ref", "reason"} or
            i["relation"] != "Link" or not all(isinstance(v, str) and v.strip() for v in i.values())
            for i in intentional):
        raise ValueError("only individual exploratory/source Links may have an explicit retention reason")
    with core.mutation_lock():
        state = _load()
        selected = state["plans"].get(key)
        if not selected or selected["scope"] != scope:
            raise ValueError("organization review has no selected scope snapshot")
        current = snapshot(scope)
        allowed = {u["unit"] for u in selected.get("review_units", [])}
        live = {u["unit"] for u in current["units"]}
        skipped = [i["unit"] for i in checked if i["unit"] not in allowed & live]
        if skipped and outcome == "complete":
            raise ValueError("unselected or changed review unit; inspect a fresh plan")
        # 구간 키가 본문을 묶으므로, 바뀌지 않은 구간의 판단은 그 뒤 scope가 바뀌어도(이
        # 검토자의 쓰기 포함) 보류 진척으로 남긴다. 바뀌었거나 고르지 않은 구간은 기록하지
        # 않고 돌려준다. 완료만 판독한 snapshot과 모든 구간을 요구한다(아래).
        checked = [i for i in checked if i["unit"] not in skipped]
        coverage = state.setdefault("coverage", {}).setdefault(scope, {})
        for item in checked:
            coverage[item["unit"]] = {"reason": item["reason"].strip(), "at": core.now_kst()}
        state["coverage"][scope] = {k: v for k, v in coverage.items() if k in live}
        if outcome == "complete":
            if after != current["snapshot"]:
                raise ValueError("organization changed after inspection; pending")
            old_ids = {n["id"] for n in selected["nodes"]}
            if not old_ids <= {n["id"] for n in current["nodes"]}:
                raise ValueError("selected knowledge disappeared or crossed Scope; pending")
            if any(not n["hub"] and not n["has_body"] for n in current["nodes"]):
                raise ValueError("knowledge body is empty; pending")
            available = {_ref_key(i) for i in current["references"]}
            if any(_ref_key(i) not in available for i in intentional):
                raise ValueError("intentional Link is not a current review item")
            if current["issues"] or current["pending_moves"] or _unresolved(current, intentional):
                raise ValueError("reference or local hub wiring remains unfinished")
            if _remaining(current, state):
                raise ValueError("unreviewed content remains; checkpoint checked units as deferred")
        row = {"key": key, "scope": scope, "outcome": outcome, "reason": reason.strip(),
               "after": current["snapshot"], "intentional": intentional, "checked": checked,
               "remaining_units": len(_remaining(current, state)), "at": core.now_kst()}
        if skipped:
            row["skipped"] = skipped
        state["reviews"][scope] = row
        if outcome == "complete":
            state["moves"] = {k: m for k, m in state.get("moves", {}).items()
                              if any(Path(i["from"]).parts[:2] != _scope_path(scope).relative_to(core.ROOT).parts
                                     for i in m["items"])}
        # 계획 뒤 바뀌거나 새로 생긴 노드는 이 검토자가 고친 것이다 — 그 구간은 자기
        # 정정을 스스로 검토하지 않도록 냉각 시간 동안 다른 검토자에게 넘긴다(넘김).
        planned = {n["id"]: n["hash"] for n in selected["nodes"]}
        edited = {n["id"] for n in current["nodes"] if planned.get(n["id"]) != n["hash"]}
        covered = state["coverage"][scope]
        handoff = {u: at for u, at in state.get("handoff", {}).get(scope, {}).items()
                   if u in live and u not in covered}
        now = time.time()
        for u in current["units"]:
            if u["id"] in edited and u["unit"] not in covered:
                handoff.setdefault(u["unit"], now)
        state.setdefault("handoff", {})[scope] = handoff
        _save(state)
        return row


def status(job: dict, idx=None) -> dict:
    current = snapshot(job["scope"], idx)
    state = _load()
    row = state["reviews"].get(job["scope"], {})
    good = row.get("key") == job["key"] and _complete(job["scope"], state, current)
    return {"key": job["key"], "scope": job["scope"],
            "status": "complete" if good else "pending", "snapshot": current["snapshot"],
            "remaining_units": len(_remaining(current, state))}


def readout(jobs: list[dict]) -> list[dict]:
    """The full identity inventory stays in the manifest, not in every prompt."""
    result = []
    for job in jobs:
        selected = {u["id"] for u in job.get("review_units", [])}
        nodes = [n for n in job["nodes"] if n["id"] in selected or n["hub"]]
        result.append({**job, "nodes": nodes, "other_nodes": len(job["nodes"]) - len(nodes)})
    return result


def guidance() -> str:
    """The whole rule text — growth prompts and `plan` carry it; hooks carry `HOOK_GUIDANCE`."""
    return ("저장 완료와 참조·조직 완료는 다르다. 아래 선택된 군집의 요약·크기·참조 목록을 먼저 보고 "
            "이번 작업의 판정 대상은 review_units의 최대 3개 구간(각 4000자 이하)이다. read_node(name=id, view=view)로 읽는다. "
            "반환된 view_hash가 그 구간의 expect_view_hash와 다르면 새 plan에서 범위를 확인한 뒤 읽는다"
            "(unit 속 sha256은 구간 내용 키라 비교 대상이 아니다). "
            "이는 군집 전체의 검토가 아니다. 조건이나 출처가 구간 밖이면 필요한 근거 구간만 표적 조회한다. "
            "이전 deferred의 질문과 필요한 근거부터 이어서 확인한다. 예산 안에 판단하지 못하면 checked에 넣지 말고 "
            "필요한 절과 질문을 deferred에 남긴다. 긴 본문 전문을 반복해서 읽지 않는다. "
            "previous_deferral.snapshot_changed가 참이면 이전 판단을 현재 완료로 간주하지 말고 대상 ID의 현행 내용을 확인한다. "
            + write.CLAIM_GUIDANCE +
            "허브의 분화(시행령 §3 7항)는 이 검토가 맡는다 — hub_tree가 그 scope의 허브와 허브마다 직속 노드 "
            "수를 보인다. 분화 시에는 현재 구조 유지·기존 입구 통합·분화 중 본문에 맞는 선택을 하며 "
            "수량만으로 분화하지 않는다. "
            "organization_advice가 있으면 실행 일지 누적과 허브의 본문 중복을 점검한다. "
            "허브는 현재 탐색 지도이며 단계별 보고서가 아니다. 결론·적용 조건·출처를 유지하고 "
            "기존 결론을 고치되 매번 진행 기록을 덧붙이지 않는다. 레포의 실행 상세는 레포 문서를 인용한다. "
            "예산 안에 판단하지 못한 내용은 삭제하거나 완료라 하지 말고, 다음 노드 ID·절·질문을 deferred에 남긴다. "
            "같은 최상위 군집 내부의 비고정 배치는 수행하고 변경을 보고한다. pin·보호영역·최상위 경계는 유지한다. "
            "오래 쓸 결론은 노드, 전사·레포 문서는 원료다. 원료 Markdown을 지식 노드나 허브로 승격하지 말라. "
            "외부 레포 문서는 확인한 URL로, vault 원료는 루트 기준 경로와 정확한 라운드로 인용한다. "
            "미해석 PE는 실제 근거로 수리하고, 탐색 질문 Link는 개별 사유로 유지할 수 있다. "
            "분화 시 실제 하위 군집·허브·구성원을 만들고 hub_links 양쪽을 반영한다. "
            "중단 시 같은 ID의 현재 위치부터 확인해 남은 작업을 이어라. "
            "완료 전에 organization plan --scope <scope> --preview로 최신 snapshot을 읽고, "
            "검토한 각 구간의 주장·적용 조건·분화 또는 유지 이유를 checked=[{unit,reason}]에 남긴다. "
            "checked는 선택한 구간의 현행 본문에 결속된다. 계획 뒤 고치거나 만든 노드의 구간은 review 때 "
            "넘김으로 기록돼 12시간 동안 계획에 오지 않는다(coverage.handoff) — 자기 정정은 다른 검토자가 본다. "
            "coverage.remaining이 이번 checked 수보다 크면 전체 완료가 아니므로 deferred로 부분 진척을 저장한다. "
            "처음 선택한 key와 최신 after=snapshot, scope, outcome=complete|deferred, reason, checked, "
            "intentional=[{id,relation:Link,ref,reason}]를 organization review의 JSON stdin으로 낸다. "
            "정돈이 불필요하면 현재 구조가 맞는 이유를 기록하고, 미완료면 deferred로 남긴다.")


def prompt(jobs: list[dict], *, inventory: bool = True) -> str:
    if not jobs:
        return ""
    command = core.cli_command("organization")
    return ("\n[osk 참조·조직 검토]\n" + guidance() + "\n"
            + "실행 명령 접두부: " + command + "\n"
            + (json.dumps(readout(jobs), ensure_ascii=False) if inventory else "목록은 아래 organization_jobs에 있다.\n"))


# 훅에 싣는 몫 — 착수와 완료의 조건, 판정이 결속되는 값. 나머지 규칙과 군집·노드 목록은
# 작업 직전에 `plan`이 준다(2026-09-27 독립 검토: 규칙은 문서가 아니라 쓰는 순간에).
HOOK_GUIDANCE = (
    "저장 완료와 참조·조직 완료는 다르다. 이번 대상은 아래 review_units의 구간뿐이다(군집 전체가 아니다). "
    "`read_node(name=id, view=view)`로 읽고 주장·적용 조건이 현행과 맞는지 판정한다. 돌려받은 view_hash가 "
    "그 구간의 expect_view_hash와 다르면(unit 속 sha256은 구간 내용 키라 비교하지 않는다) 선택 뒤 본문이 "
    "바뀐 것이니 새 plan으로 범위를 다시 확인한 뒤 읽는다. "
    "previous_deferral.snapshot_changed가 참이면 이전 판단을 이어받지 말고 "
    "현행을 확인한다. 틀린 곳은 그 자리에서 고치되 정정 전후의 판단과 출처를 보존하고, 고친 구간은 다음 "
    "검토로 넘긴다. 판단하지 못한 내용은 지우거나 완료라 하지 않는다. pin·보호영역·최상위 경계를 유지하고 "
    "원료를 노드·허브로 승격하지 않는다. 허브의 분화(시행령 §3 7항)는 이 검토가 맡는다 — hub_tree가 그 "
    "scope의 허브와 허브마다 직속 노드 수를 보인다. 분화 시에는 현재 구조 유지·기존 입구 통합·분화 중 본문에 "
    "맞는 선택을 하며 수량만으로 분화하지 않는다. 제출 전에 접두부 + `plan --scope <scope> --preview`로 최신 "
    "snapshot을 읽어 after에 넣는다. coverage.remaining이 checked 수보다 크면 outcome=deferred로 내고 "
    "reason에 다음 대상(노드 ID·절·질문)을 남긴다. 접두부 + `review`에 JSON stdin으로 "
    "{key, after, scope, outcome: complete|deferred, reason, checked: [{unit, reason}], "
    "intentional: [{id, relation: Link, ref, reason}]}을 낸다. 중단되면 같은 key로 plan부터 다시 읽어 "
    "잇는다. 전체 규칙은 plan 응답의 guidance에 있다.")
_FLAGGED = (" references·issues가 있으면 실제 근거로 수리하고, 탐색 질문 Link만 개별 사유와 함께 "
            "intentional에 남긴다. PE 미해석은 이 예외로 닫지 않는다.")
_LIST_CAP = 10
HUB_TREE_CAP = 3000


def hook_readout(job: dict) -> dict:
    """훅에 싣는 계획 — 판정이 결속되는 값과 채워진 목록만, 필드 이름은 `plan`과 같다."""
    out = {k: job[k] for k in ("scope", "key", "snapshot", "coverage", "hub_tree") if k in job}
    # 구간의 view는 선택 당시 파일의 위치다. read_node의 view_hash와 맞춰 볼 expect_view_hash는
    # 남긴다 — 없으면 다른 세션의 삽입으로 밀린 구간을 읽고도 제출이 통과한다.
    out["review_units"] = [{k: u[k] for k in ("unit", "id", "name", "view", "chars", "expect_view_hash")
                            if k in u} for u in job.get("review_units", [])]
    prior = job.get("previous_deferral")
    if prior:
        out["previous_deferral"] = {k: prior[k] for k in ("snapshot_changed", "at", "reason") if k in prior}
    broken = [c for c in job.get("clusters", []) if c.get("missing_links") or c.get("branch_bypass")]
    for name, items in (("issues", job.get("issues")), ("references", job.get("references")),
                        ("pending_moves", job.get("pending_moves")), ("missing_ids", job.get("missing_ids")),
                        ("clusters", broken)):
        if items:
            out[name] = items[:_LIST_CAP]
            if len(items) > _LIST_CAP:
                out[name + "_total"] = len(items)
    return out


def hook_prompt(jobs: list[dict]) -> str:
    """조직 검토의 훅 블록. 빈 목록이면 빈 문자열이다."""
    if not jobs:
        return ""
    head = " · ".join(f"scope {j['scope']} 남은 구간 {j['coverage']['remaining']}/{j['coverage']['total']}"
                      for j in jobs)
    flagged = any(j.get("references") or j.get("issues") for j in jobs)
    return (f"[osk 참조·조직 검토 — {head}]\n" + HOOK_GUIDANCE + (_FLAGGED if flagged else "") + "\n"
            + "실행 명령 접두부: " + core.cli_command("organization") + "\n"
            + json.dumps([hook_readout(j) for j in jobs], ensure_ascii=False))
