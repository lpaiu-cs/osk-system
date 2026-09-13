"""Bounded, resumable node -> provenance -> hub completion.

Receipts prove persisted bytes and references, never semantic correctness.
The local journal is written before either graph write. All graph changes still
use write's ordinary validator/render/CAS path under one mutation lock.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import contract, graph, raw, secrets, write
from .core import ROOT, local_lock_path, posix_rel, resolve_in_root, sha256_bytes, sha256_file


def _job_path(key: str) -> Path:
    if not isinstance(key, str) or not key.strip() or len(key) > 256:
        raise write.WriteError("distill.key must be a nonempty stable key (<=256 characters)")
    digest = hashlib.sha256((str(ROOT) + "\0" + key).encode()).hexdigest()
    return local_lock_path("osk-distillation-" + digest + ".json").with_suffix(".json")


def _load(key: str) -> dict | None:
    path = _job_path(key)
    if not path.exists():
        return None
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
        if job["key"] != key or job["version"] != 1:
            raise ValueError("job identity mismatch")
        return job
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise write.WriteError("distillation journal is unreadable; pending", [str(e)]) from e


def _save(job: dict) -> None:
    # This is operational state outside the tracked graph, not a node write.
    write._atomic_write(_job_path(job["key"]),
                        json.dumps(job, ensure_ascii=False, sort_keys=True).encode())


def _unwrap(ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip():
        raise write.WriteError("distill source must be a nonempty reference")
    ref = ref.strip()
    return ref[2:-2].strip() if ref.startswith("[[") and ref.endswith("]]") else ref


def _source(ref: str, idx) -> dict:
    value = _unwrap(ref)
    if "#" in value:
        path, number = raw.parse_ref(value)
        if number is None:
            raise write.WriteError("raw source requires an exact numeric round anchor")
        p = raw._raw_file(path)
        text = raw.read_exact(p)
        raw._next_index(text)  # duplicate/damaged rounds are not resolvable evidence
        span = raw._round_spans(text).get(number)
        if span is None:
            raise write.WriteError("source round does not exist")
        # Trailing round separators are not evidence; later append must not
        # invalidate the unchanged round. Hash the full stored round, untruncated.
        data = text[span[0]:span[1]].rstrip("\n").encode()
        return {"ref": f"[[{posix_rel(p, ROOT)}#{number}]]",
                "path": posix_rel(p, ROOT), "hash": sha256_bytes(data)}
    p = write._live_locate(value, idx)
    if p is None or not p.is_file() or graph.space_of(p)[0] not in ("scope", "domain"):
        raise write.WriteError("source must resolve to a Scope/Domain node or exact raw round")
    node = contract.parse(p)
    if not write._norm_body(node.body):
        raise write.WriteError("source node has no retained body")
    return {"ref": node.id, "path": posix_rel(p, ROOT), "hash": sha256_file(p),
            "id": node.id, "name": p.stem}


def _sources(items, idx) -> list[dict]:
    if not isinstance(items, list) or not items or len(items) > 64:
        raise write.WriteError("distill.sources requires 1..64 references")
    found = {}
    for item in items:
        if isinstance(item, dict):
            if set(item) != {"ref", "hash"} or not isinstance(item["hash"], str):
                raise write.WriteError("source snapshot requires exactly ref and hash")
            ref, expected = item["ref"], item["hash"]
        else:
            ref, expected = item, None
        source = _source(ref, idx)
        if expected is not None and source["hash"] != expected:
            raise write.WriteError("source changed since selection; pending")
        found[source["ref"]] = source
    return list(found.values())


def _hub(name: str, idx) -> dict:
    p = write._live_locate(name, idx)
    if p is None or not p.is_file() or not graph.is_hub(p):
        raise write.WriteError("distill.hub must name an existing hub")
    if graph.space_of(p)[0] not in ("scope", "domain"):
        raise write.WriteError("distillation cannot write protected or other spaces")
    node = contract.parse(p)
    return {"name": p.stem, "id": node.id, "path": posix_rel(p, ROOT),
            "before_hash": sha256_file(p), "hash": sha256_file(p)}


def _check_sources(job: dict, idx) -> None:
    for old in job["sources"]:
        current = _source(old["ref"], idx)
        if current["hash"] != old["hash"] or current["path"] != old["path"]:
            raise write.WriteError("source changed or moved; pending")


def _receipt(job: dict, reason: str | None = None) -> dict:
    out = {"key": job["key"], "status": "pending" if reason else "complete",
           "sources": job["sources"], "target": job["target"], "hub": job["hub"]}
    if reason:
        out["reason"] = reason
    return out


def _verify(job: dict) -> dict:
    try:
        idx = graph.Index()
        write._require_complete(idx)
        _check_sources(job, idx)
        target, hub = job["target"], job["hub"]
        p, hp = resolve_in_root(target["path"]), resolve_in_root(hub["path"])
        if p is None or hp is None or not p.is_file() or not hp.is_file():
            raise write.WriteError("target or hub missing; pending")
        if graph.space_of(p)[0] not in ("scope", "domain") or p.parent != hp.parent:
            raise write.WriteError("target left selected hub; pending")
        node, hn = contract.parse(p), contract.parse(hp)
        if node.id != target["id"] or sha256_file(p) != target["hash"]:
            raise write.WriteError("target bytes changed; pending")
        if not write._norm_body(node.body):
            raise write.WriteError("target has no retained body; pending")
        if hn.id != hub["id"] or not graph.is_hub(hp):
            raise write.WriteError("hub identity changed; pending")
        # Node.edges() intentionally drops raw anchors. Retain exact stored refs.
        actual = set()
        for ref in write._stored_edges(node.meta.get("derived-from")):
            try:
                actual.add(_source(ref, idx)["ref"])
            except (write.WriteError, OSError, ValueError, TypeError):
                # An unrelated pre-existing citation need not be a valid new
                # distillation input. Only the requested evidence is certified.
                continue
        if not {s["ref"] for s in job["sources"]}.issubset(actual):
            raise write.WriteError("target provenance is incomplete; pending")
        if target["name"] not in hn.wikilinks():
            raise write.WriteError("selected hub does not link to target; pending")
        receipt = _receipt(job)
        receipt["hub"] = dict(hub, hash=sha256_file(hp))
        return receipt
    except (write.WriteError, OSError, ValueError, KeyError, TypeError) as e:
        return _receipt(job, str(e))



def _verify_receipt_locked(receipt: dict) -> dict:
    """Recheck a synchronized receipt without requiring a local recovery journal."""
    if (not isinstance(receipt, dict)
            or not all(k in receipt for k in ("key", "sources", "target", "hub"))
            or not isinstance(receipt["sources"], list) or not receipt["sources"]
            or not isinstance(receipt["target"], dict) or not isinstance(receipt["hub"], dict)):
        raise write.WriteError("incomplete distillation receipt")
    return _verify(receipt)


def verify_receipt(receipt: dict) -> dict:
    """Read-only structural verification of a saved receipt on any vault replica."""
    with write._Lock():
        return _verify_receipt_locked(receipt)


def _status_locked(key: str) -> dict:
    """Caller holds the shared mutation lock; read and verify, never repair."""
    job = _load(key)
    return _verify(job) if job else {"key": key, "status": "pending", "reason": "unknown job"}


def status(key: str) -> dict:
    """Recheck actual source/target/hub bytes, not a cached success flag."""
    with write._Lock():
        return _status_locked(key)



def discover(raw_refs: list[str], limit: int = 8, scan_limit: int = 256) -> dict:
    """Find reusable local proofs for this bounded raw snapshot, never another root."""
    if (not isinstance(raw_refs, list) or not raw_refs or len(raw_refs) > 64
            or not 1 <= limit <= 32 or not 1 <= scan_limit <= 1024):
        raise write.WriteError("bounded raw references and discovery limits required")
    with write._Lock():
        idx = graph.Index()
        write._require_complete(idx)
        wanted = {_source(ref, idx)["ref"] for ref in raw_refs}
        if any("#" not in ref for ref in wanted):
            raise write.WriteError("proof discovery accepts exact raw round references only")
        # Preserve local_lock_path's vault-specific fallback suffix; in a shared
        # git directory, canonical _job_path(key) below still excludes other ROOTs.
        probe = _job_path("inventory")
        digest = hashlib.sha256((str(ROOT) + "\0inventory").encode()).hexdigest()
        pattern = probe.name.replace(digest, "*")
        paths = sorted(probe.parent.glob(pattern),
                       key=lambda path: (-path.stat().st_mtime_ns, path.name))
        proofs, errors, scanned = [], [], 0
        for path in paths[:scan_limit]:
            scanned += 1
            try:
                if path.stat().st_size > 256 * 1024:
                    raise ValueError("journal exceeds the 256 KiB discovery bound")
                job = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(job, dict) or not isinstance(job.get("key"), str):
                    raise ValueError("journal has no stable key")
                if _job_path(job["key"]) != path:
                    continue
                if job.get("version") != 1:
                    raise ValueError("unsupported journal version")
                if not wanted.intersection(source["ref"] for source in job["sources"]):
                    continue
                target = resolve_in_root(job["target"]["path"])
                if target is None or graph.space_of(target)[0] != "scope":
                    continue
                proof = _verify(job)
                if (proof["status"] == "complete"
                        or proof.get("reason") == "selected hub does not link to target; pending"):
                    proofs.append(proof)
                else:
                    errors.append({"key": job["key"], "reason": proof.get("reason", "unverified proof")})
            except (write.WriteError, ValueError, OSError, KeyError, TypeError) as exc:
                errors.append({"journal": path.name, "reason": str(exc)})
        return {"proofs": proofs[:limit], "errors": errors,
                "scanned": scanned, "truncated": len(paths) > scan_limit or len(proofs) > limit}


def _wire_hub(job: dict) -> dict:
    """Caller holds mutation lock. Repair only a journaled, still-exact output."""
    idx = graph.Index()
    write._require_complete(idx)
    _check_sources(job, idx)
    target, hub = job["target"], job["hub"]
    p, hp = resolve_in_root(target["path"]), resolve_in_root(hub["path"])
    if p is None or not p.is_file() or sha256_file(p) != target["hash"]:
        raise write.WriteError("expected target bytes absent; original request or review is required")
    if hp is None or not hp.is_file() or p.parent != hp.parent:
        raise write.WriteError("selected hub missing or target moved; pending")
    tn, hn = contract.parse(p), contract.parse(hp)
    if (tn.id != target["id"] or graph.space_of(p)[0] not in ("scope", "domain")
            or not write._norm_body(tn.body)):
        raise write.WriteError("target identity/body changed; pending")
    if hn.id != hub["id"] or not graph.is_hub(hp):
        raise write.WriteError("hub identity changed; pending")
    if target["name"] not in hn.wikilinks():
        def prepare_hub(path, data):
            job["hub"]["hash"] = sha256_bytes(data)
            _save(job)
        write._update_node_locked(
            hub["id"], body=hn.body.rstrip() + "\n\n- [[" + target["name"] + "]]\n",
            expect_hash=sha256_file(hp), _before_write=prepare_hub)
    return _verify(job)


def resume(key: str, *, name: str | None = None) -> dict:
    """Resume hub wiring without reconstructing or changing the retained body."""
    with write._Lock():
        job = _load(key)
        if job is None:
            return {"ok": False, "distillation": {"key": key, "status": "pending",
                    "reason": "unknown job; original request is required"}}
        target = job["target"]
        if name is not None and name not in (target["id"], target["name"]):
            raise write.WriteError("resume name does not match the journaled target")
        try:
            receipt = _wire_hub(job)
        except Exception as exc:
            receipt = _receipt(job, str(exc))
        path = resolve_in_root(target["path"])
        saved = bool(path and path.is_file() and sha256_file(path) == target["hash"])
        return {"ok": saved, "resumed": True, "node_preserved": saved,
                "name": target["name"], "id": target["id"], "path": target["path"],
                "new_hash": target["hash"] if saved else None, "distillation": receipt}


def _execute(operation: str, distill: dict, request: dict) -> dict:
    if not isinstance(distill, dict) or set(distill) != {"key", "sources", "hub"}:
        raise write.WriteError("distill requires exactly key, sources, hub")
    key = distill["key"]
    _job_path(key)
    if request.get("settle") is not None:
        raise write.WriteError("distill and eviction settle are separate completion operations")
    # Bind the key to the complete caller request, before secret filtering.
    binding = sha256_bytes(json.dumps(
        [operation, distill, request], ensure_ascii=False, sort_keys=True).encode())
    request = dict(request)
    filtered = set()
    for field in ("body", "summary", "new_text"):
        if isinstance(request.get(field), str):
            request[field], hits = secrets.filter_text(request[field])
            filtered.update(hits)
    with write._Lock():
        job = _load(key)
        if job and job["binding"] != binding:
            raise write.WriteError("distill.key is already bound to a different request")
        idx = graph.Index()
        write._require_complete(idx)
        if not job:
            sources = _sources(distill["sources"], idx)
            hub = _hub(distill["hub"], idx)
            if operation == "create":
                supplied_body = request.get("body")
                target_path = None
                selected_hash = None
            else:
                target_path = write._live_locate(request["name"], idx)
                if target_path is None or not target_path.is_file():
                    raise write.WriteError("distillation target does not exist")
                if graph.space_of(target_path)[0] not in ("scope", "domain"):
                    raise write.WriteError("distillation target must be a Scope/Domain node")
                old = contract.parse(target_path)
                selected_hash = sha256_file(target_path)
                supplied_body = request.get("body")
                if request.get("old_text") is not None:
                    supplied_body = request.get("new_text")
                if supplied_body is not None and request.get("body") is not None:
                    if write._norm_body(supplied_body) == write._norm_body(old.body):
                        raise write.WriteError("distillation requires a changed retained body")
            if not isinstance(supplied_body, str) or not write._norm_body(supplied_body):
                raise write.WriteError("distillation requires retained body content")
        else:
            sources, hub = job["sources"], job["hub"]
            selected_hash = job["target"]["before_hash"]

        def prepare(path, data):
            nonlocal job
            n = contract.parse_bytes(path, data)
            current_hash = sha256_file(path) if path.exists() else None
            if current_hash != selected_hash:
                raise write.WriteError("target changed before write; pending")
            if operation == "update" and write._norm_body(n.body) == write._norm_body(contract.parse(path).body):
                raise write.WriteError("distillation requires a changed retained body")
            if graph.space_of(path)[0] not in ("scope", "domain"):
                raise write.WriteError("distillation target must be a Scope/Domain node")
            if path.parent != resolve_in_root(hub["path"]).parent or path.stem == hub["name"]:
                raise write.WriteError("target must be a knowledge node under the selected hub")
            if any(s["path"] in (posix_rel(path, ROOT), hub["path"]) for s in sources):
                raise write.WriteError("distillation cannot cite its target or wiring hub as input")
            if not write._norm_body(n.body):
                raise write.WriteError("distillation has no retained body after validation")
            stored = set()
            for ref in write._stored_edges(n.meta.get("derived-from")):
                try:
                    stored.add(_source(ref, idx)["ref"])
                except (write.WriteError, OSError, ValueError, TypeError):
                    continue
            if not {source["ref"] for source in sources}.issubset(stored):
                raise write.WriteError("required provenance was removed; no node written")
            planned = {"name": path.stem, "id": n.id, "path": posix_rel(path, ROOT),
                       "before_hash": sha256_file(path) if path.exists() else None,
                       "hash": sha256_bytes(data)}
            if job:
                if job["target"] != planned:
                    raise write.WriteError("prepared target changed; pending")
            else:
                job = {"version": 1, "key": key, "binding": binding,
                       "sources": sources, "hub": hub, "target": planned,
                       "identity": {"id": n.id, "created": n.meta["created"]},
                       "stamp": n.meta["updated"]}
                _check_sources(job, graph.Index())
                _save(job)  # identity and exact expected bytes BEFORE graph write

        try:
            if job:
                _check_sources(job, idx)
                target = job["target"]
                path = resolve_in_root(target["path"])
                current = sha256_file(path) if path and path.is_file() else None
                if current not in (target["before_hash"], target["hash"]):
                    raise write.WriteError("target changed concurrently; pending")
                already_written = current == target["hash"]
            else:
                already_written = False
            if not already_written:
                edge_arg = "edges" if operation == "create" else "add_edges"
                edges = dict(request.get(edge_arg) or {})
                edges["derived-from"] = (write._as_list(edges.get("derived-from", []))
                                         + [s["ref"] for s in sources])
                request[edge_arg] = edges
                if operation == "create":
                    result = write._create_node_locked(
                        **request, _before_write=prepare,
                        _identity=job["identity"] if job else None)
                else:
                    result = write._update_node_locked(
                        **request, _before_write=prepare, _stamp=job["stamp"] if job else None)
                if job is None:
                    raise write.WriteError("no retained body was written")
            else:
                result = {"ok": True, "name": target["name"], "id": target["id"],
                          "path": target["path"], "new_hash": target["hash"], "resumed": True}
            receipt = _wire_hub(job)
        except Exception as e:
            if job is None:
                raise
            receipt = _verify(job)
            if receipt["status"] != "complete":
                receipt["reason"] = str(e)
            p = resolve_in_root(job["target"]["path"])
            saved = bool(p and p.is_file() and sha256_file(p) == job["target"]["hash"])
            result = {"ok": saved, "name": job["target"]["name"], "id": job["target"]["id"],
                      "path": job["target"]["path"], "node_preserved": saved,
                      "new_hash": job["target"]["hash"] if saved else None}
        result["distillation"] = receipt
        if filtered:
            result["filtered"] = sorted(filtered)
        return result


def create_node(distill: dict, **kwargs) -> dict:
    return _execute("create", distill, kwargs)


def update_node(distill: dict, **kwargs) -> dict:
    if isinstance(distill, dict) and set(distill) == {"resume"}:
        if not isinstance(kwargs.get("name"), str) or any(
                value is not None for key, value in kwargs.items() if key != "name"):
            raise write.WriteError("resume accepts only name and distill={resume:key}; no mutations")
        return resume(distill["resume"], name=kwargs["name"])
    return _execute("update", distill, kwargs)
