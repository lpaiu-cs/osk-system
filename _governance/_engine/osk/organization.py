"""Persistent review of reference roles and the organization of a touched Scope.

Knowledge files themselves retain unresolved work. A version-bound review only
suppresses the exact inspected state; no new model runner or graph store exists.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from . import core, contract, graph, write


def _state_path() -> Path:
    return core.local_lock_path("osk-organization.json")


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


def record_move(plans: list, hub_links: list) -> str:
    """Caller holds mutation lock; record intent before the first filesystem move."""
    items = [{"id": n.id, "name": src.stem, "from": core.posix_rel(src, core.ROOT),
              "to": core.posix_rel(dst, core.ROOT), "hash": core.sha256_file(src)}
             for src, dst, n in plans]
    key = core.sha256_bytes(json.dumps(items, sort_keys=True).encode())
    state = _load()
    state.setdefault("moves", {})[key] = {"items": items, "hub_links": hub_links}
    _save(state)
    return key


def pending_moves(scope: str, idx) -> list:
    result = []
    for key, move in _load().get("moves", {}).items():
        items = []
        for item in move["items"]:
            if Path(item["from"]).parts[:2] != ("= Scope", scope):
                continue
            path = write._live_locate(item["id"], idx)
            current = core.posix_rel(path, core.ROOT) if path else None
            if current != item["to"]:
                items.append(dict(item, current=current,
                                  intact=bool(path and core.sha256_file(path) == item["hash"])))
        if items:
            result.append({"key": key, "remaining": items, "hub_links": move["hub_links"]})
    return result


def finish_move(key: str) -> None:
    state = _load()
    state.get("moves", {}).pop(key, None)
    _save(state)


def _index():
    idx = graph.Index()
    if idx.scan_errors or idx.broken or idx.dup_ids or idx.dup_stems:
        raise ValueError("organization needs a complete, unambiguous inventory")
    return idx


def _scope_path(scope: str) -> Path:
    if not isinstance(scope, str) or not scope or "/" in scope or "\\" in scope:
        raise ValueError("scope must be one existing top-level Scope name")
    p = core.resolve_in_root("= Scope/" + scope)
    if p is None or not p.is_dir() or scope == "Workbench":
        raise ValueError("organization requires an existing Scope")
    return p


def snapshot(scope: str, idx=None) -> dict:
    """Read bytes once per node; do not combine cached bodies with current hashes."""
    base = _scope_path(scope)
    idx = _index() if idx is None else idx
    nodes, parsed, references = [], {}, []
    for name, (path, kind) in sorted(idx.nodes.items()):
        if kind[0] != "scope" or kind[1] != scope:
            continue
        data = path.read_bytes()
        node = contract.parse_bytes(path, data)
        parsed[path] = node
        nodes.append({"id": node.id, "name": name, "path": core.posix_rel(path, core.ROOT),
                      "hash": core.sha256_bytes(data), "summary": str(node.meta.get("summary", "")),
                      "hub": graph.is_hub(path), "has_body": bool(write._norm_body(node.body))})
        references.extend(graph.reference_review(node, idx))
    links = {}
    for p, node in parsed.items():
        links[p] = {write._live_locate(ref, idx) for ref in node.wikilinks()}
    clusters, issues = [], []
    for directory in sorted({p.parent for p in parsed}):
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
            "nodes": nodes, "clusters": clusters, "issues": issues, "references": references,
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
                not _unresolved(current, row.get("intentional", [])))


def pending(scopes=None, limit: int = 3, *, idx=None, record: bool = False) -> list[dict]:
    """Caller holds the vault lock. Pending references survive missing local state."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise ValueError("organization limit must be 1..20")
    idx = _index() if idx is None else idx
    state, jobs = _load(), []
    scopes = sorted(set(scopes if scopes is not None else
                        [k[1] for _, k in idx.nodes.values() if k[0] == "scope"] +
                        [p["scope"] for p in state["plans"].values()]))
    attempts = {p["scope"]: p.get("last_attempt", "") for p in state["plans"].values()}
    scopes.sort(key=lambda name: (attempts.get(name, ""), name))
    # ponytail: Scope inventories are read on maintenance, never on every write.
    # Use persisted change hints only if this bounded readout becomes costly.
    for scope in scopes:
        prior = next((p for p in state["plans"].values() if p["scope"] == scope), None)
        reviewed = state["reviews"].get(scope, {})
        unfinished = prior and not (reviewed.get("key") == prior["key"] and reviewed.get("outcome") == "complete")
        if not prior and not any(k[0] == "scope" and k[1] == scope for _, k in idx.nodes.values()):
            continue  # A raw-only capture has no knowledge organization to review yet.
        current = snapshot(scope, idx)
        missing_ids = sorted({n["id"] for n in prior["nodes"]} - {n["id"] for n in current["nodes"]}) if prior else []
        if missing_ids:
            current["missing_ids"] = missing_ids
        if (not prior and all(n["hub"] for n in current["nodes"]) and not current["references"]
                and not current["issues"] and not current["pending_moves"]):
            continue
        if _complete(scope, state, current):
            continue
        current["reason"] = "references or changed knowledge need organization review"
        current["review_command"] = "python -m osk.cli organization review (UTF-8 JSON stdin)"
        if unfinished or missing_ids:
            current["key"] = prior["key"]
        jobs.append(current)
        if record:
            # Keep one unfinished selection per Scope across writes/restarts.
            if not (unfinished or missing_ids):
                state["plans"] = {k: p for k, p in state["plans"].items() if p["scope"] != scope}
            state["plans"].setdefault(current["key"], current)
            state["plans"][current["key"]]["last_attempt"] = core.now_kst()
        if len(jobs) == limit:
            break
    if record and jobs:
        _save(state)
    return jobs


def plan(scope: str, *, record: bool = True) -> dict:
    with core.mutation_lock():
        _scope_path(scope)
        jobs = pending([scope], limit=1, record=record)
        return jobs[0] if jobs else {"scope": scope, "status": "complete"}


def review(key: str, scope: str, outcome: str, reason: str, after: str = "",
           intentional: list | None = None) -> dict:
    """Check saved state, not a claim that a plan was executed."""
    if outcome not in {"complete", "deferred"} or not isinstance(reason, str) or not reason.strip():
        raise ValueError("organization outcome and concrete reason required")
    intentional = [] if intentional is None else intentional
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
        row = {"key": key, "scope": scope, "outcome": outcome, "reason": reason.strip(),
               "after": current["snapshot"], "intentional": intentional, "at": core.now_kst()}
        state["reviews"][scope] = row
        if outcome == "complete":
            state["moves"] = {k: m for k, m in state.get("moves", {}).items()
                              if any(Path(i["from"]).parts[:2] != ("= Scope", scope)
                                     for i in m["items"])}
        _save(state)
        return row


def status(job: dict, idx=None) -> dict:
    current = snapshot(job["scope"], idx)
    state = _load()
    row = state["reviews"].get(job["scope"], {})
    good = row.get("key") == job["key"] and _complete(job["scope"], state, current)
    return {"key": job["key"], "scope": job["scope"],
            "status": "complete" if good else "pending", "snapshot": current["snapshot"]}


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
            "저장 완료와 참조·조직 완료는 다르다. 아래 변경 Scope의 기존 본문과 허브를 읽고 "
            "한 절차인지 독립된 주제들인지 판단하라. 개수만으로 나누지 말고 기존 입구를 재사용하라. "
            "같은 Scope 내부의 비고정 배치는 수행하고 변경을 보고한다. pin·보호영역·최상위 경계는 유지한다. "
            "오래 쓸 결론은 노드, 전사·레포 문서는 원료다. 원료 Markdown을 지식 노드나 허브로 승격하지 말라. "
            "외부 레포 문서는 확인한 URL로, vault 원료는 루트 기준 경로와 정확한 라운드로 인용한다. "
            "미해석 PE는 실제 근거로 수리하고, 탐색 질문 Link는 개별 사유로 유지할 수 있다. "
            "분화 시 실제 하위 군집·허브·구성원을 만들고 hub_links 양쪽을 반영한다. "
            "중단 시 같은 ID의 현재 위치부터 확인해 남은 작업을 이어라. "
            "완료 전에 organization plan --scope <scope> --preview로 최신 snapshot을 읽고, "
            "처음 선택한 key와 최신 after=snapshot, scope, outcome=complete|deferred, reason, "
            "intentional=[{id,relation:Link,ref,reason}]를 organization review의 JSON stdin으로 낸다. "
            "정돈이 불필요하면 현재 구조가 맞는 이유를 기록하고, 미완료면 deferred로 남긴다.\n"
            + "실행 명령 접두부: " + command + "\n"
            + (json.dumps(jobs, ensure_ascii=False) if inventory else "목록은 아래 organization_jobs에 있다.\n"))
