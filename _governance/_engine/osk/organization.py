"""Persistent review of reference roles and the organization of a touched Scope.

Knowledge files themselves retain unresolved work. A version-bound review only
suppresses the exact inspected state; no new model runner or graph store exists.
"""
from __future__ import annotations

from bisect import bisect_right
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

from . import core, contract, graph, write

REVIEW_CHARS = 4000  # Same bounded range returned by read_node.
REVIEW_UNITS = 3


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
    parts = scope.split("/") if scope.startswith("Domain/") else ["Scope", scope]
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
        units.extend(_units(node))
        nodes.append({"id": node.id, "name": name, "path": core.posix_rel(path, core.ROOT),
                      "hash": core.sha256_bytes(data), "summary": str(node.meta.get("summary", "")),
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


def pending(scopes=None, limit: int = 3, *, idx=None, record: bool = False) -> list[dict]:
    """Caller holds the vault lock. Pending references survive missing local state."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise ValueError("organization limit must be 1..20")
    idx = _index() if idx is None else idx
    state, jobs = _load(), []
    scopes = sorted(set(scopes if scopes is not None else
                        [k[1] if k[0] == "scope" else "Domain/" + k[1]
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
                                (k[0] == "domain" and "Domain/" + k[1] == scope)
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
        current["review_units"] = remaining[:REVIEW_UNITS]
        current["coverage"] = {"remaining": len(remaining), "total": len(current.pop("units"))}
        current["reason"] = "references or changed knowledge need organization review"
        current["review_command"] = "python -m osk.cli organization review (UTF-8 JSON stdin)"
        if unfinished or missing_ids:
            current["key"] = prior["key"]
        if reviewed.get("outcome") == "deferred":
            current["previous_deferral"] = {
                k: reviewed[k] for k in ("key", "reason", "after", "at")}
            current["previous_deferral"]["snapshot_changed"] = reviewed["after"] != current["snapshot"]
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
        return jobs[0] if jobs else {"scope": scope, "status": "complete"}


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
        if any(i["unit"] not in allowed & live for i in checked):
            raise ValueError("unselected or changed review unit; inspect a fresh plan")
        if checked and after != current["snapshot"]:
            raise ValueError("organization changed after inspection; pending")
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
        state["reviews"][scope] = row
        if outcome == "complete":
            state["moves"] = {k: m for k, m in state.get("moves", {}).items()
                              if any(Path(i["from"]).parts[:2] != _scope_path(scope).relative_to(core.ROOT).parts
                                     for i in m["items"])}
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


def prompt(jobs: list[dict], *, inventory: bool = True) -> str:
    if not jobs:
        return ""
    code = (f"import os,runpy,sys;os.environ['OSK_VAULT_ROOT']={str(core.ROOT)!r};"
            f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r});"
            "runpy.run_module('osk.cli',run_name='__main__')")
    argv = [sys.executable, "-c", code, "organization"]
    command = ("& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
               if os.name == "nt" else shlex.join(argv))
    return ("\n[osk 참조·조직 검토]\n"
            "저장 완료와 참조·조직 완료는 다르다. 아래 선택된 군집의 요약·크기·참조 목록을 먼저 보고 "
            "이번 작업의 판정 대상은 review_units의 최대 3개 구간(각 4000자 이하)이다. read_node(name=id, view=view)로 읽는다. "
            "반환된 view_hash가 선택 노드의 'view:'+hash와 다르면 새 plan에서 범위를 확인한 뒤 읽는다. "
            "이는 군집 전체의 검토가 아니다. 조건이나 출처가 구간 밖이면 필요한 근거 구간만 표적 조회한다. "
            "이전 deferred의 질문과 필요한 근거부터 이어서 확인한다. 예산 안에 판단하지 못하면 checked에 넣지 말고 "
            "필요한 절과 질문을 deferred에 남긴다. 긴 본문 전문을 반복해서 읽지 않는다. "
            "previous_deferral.snapshot_changed가 참이면 이전 판단을 현재 완료로 간주하지 말고 대상 ID의 현행 내용을 확인한다. "
            + write.CLAIM_GUIDANCE +
            "개수만으로 나누지 말고 기존 입구를 재사용하라. "
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
            "checked는 선택한 구간의 현행 본문에 결속된다. 수정한 구간은 새 plan으로 다시 읽고 다음 작업에서 검토한다. "
            "coverage.remaining이 이번 checked 수보다 크면 전체 완료가 아니므로 deferred로 부분 진척을 저장한다. "
            "처음 선택한 key와 최신 after=snapshot, scope, outcome=complete|deferred, reason, checked, "
            "intentional=[{id,relation:Link,ref,reason}]를 organization review의 JSON stdin으로 낸다. "
            "정돈이 불필요하면 현재 구조가 맞는 이유를 기록하고, 미완료면 deferred로 남긴다.\n"
            + "실행 명령 접두부: " + command + "\n"
            + (json.dumps(readout(jobs), ensure_ascii=False) if inventory else "목록은 아래 organization_jobs에 있다.\n"))
