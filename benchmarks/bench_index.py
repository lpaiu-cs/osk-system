"""Issue 16: actual MCP handler paths, isolated process caches, Windows working set.

Cold means empty application caches, NOT a flushed OS disk cache. No live node
writes: mutations require a synthetic fixture made by this script. Results
contain timings/counts only, never node bodies. Python 3.11 + engine dependencies.
"""
import argparse
import ctypes
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import weakref


def memory():
    # PROCESS_MEMORY_COUNTERS_EX; working set is RSS, not tracemalloc allocations.
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in (
                "peak_ws", "ws", "peak_paged", "paged", "peak_nonpaged",
                "nonpaged", "pagefile", "peak_pagefile", "private")]
    p = Counters()
    p.cb = ctypes.sizeof(p)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    fn = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not fn(kernel.GetCurrentProcess(), ctypes.byref(p), p.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {k + "_mib": getattr(p, v) / 2**20 for k, v in (
        ("rss", "ws"), ("peak_rss", "peak_ws"), ("private", "private"))}


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def worker(a):
    os.environ["OSK_VAULT_ROOT"] = str(a.root.resolve())
    sys.path.insert(0, str(a.engine.resolve()))
    start, cpu = time.perf_counter(), time.process_time()
    import mcp_server as server
    from osk import contract, graph, search
    imported = {"import_ms": (time.perf_counter()-start)*1000,
                "import_cpu_ms": (time.process_time()-cpu)*1000, **memory()}
    target = a.target or next((a.root / "= Scope").glob("osk-system/*.md")).stem
    counts = {}
    original_index, original_search, original_parse = (
        graph.Index.__init__, search.Searcher.__init__, contract.parse)
    original_fp = server._vault_fingerprint

    def index(self, *args, **kwargs):
        counts["index_builds"] += 1
        return original_index(self, *args, **kwargs)

    def searcher(self, *args, **kwargs):
        counts["search_builds"] += 1
        return original_search(self, *args, **kwargs)

    def parse(*args, **kwargs):
        counts["parsed_files"] += 1
        return original_parse(*args, **kwargs)

    def fingerprint():
        started = time.perf_counter()
        result = original_fp()
        counts["fingerprint_ms"] += (time.perf_counter()-started)*1000
        counts["racy"] = result[1]
        return result

    graph.Index.__init__, search.Searcher.__init__ = index, searcher
    contract.parse, server._vault_fingerprint = parse, fingerprint
    emit({"ready": True, **imported})
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["op"] == "stop":
            break
        if cmd.get("cold"):
            previous = weakref.ref(server._index) if server._index is not None else None
            server._index = server._searcher = server._fingerprint = None
            gc.collect()
            assert previous is None or previous() is None, "cold sample retained the previous Index"
        if cmd.get("at"):
            time.sleep(max(0, cmd["at"] - time.perf_counter()))
        counts.update(index_builds=0, search_builds=0, parsed_files=0,
                      fingerprint_ms=0, racy=False)
        start, cpu = time.perf_counter(), time.process_time()
        if cmd["op"] == "read_node":
            value = server.read_node(target)
            assert "error" not in value, value.get("error")
        elif cmd["op"] == "overview":
            value = server.overview()
            assert value["nodes"] > 0
        elif cmd["op"] == "search":
            value = server.search(target, 8)
            assert value and value[0]["title"] == target
        elif cmd["op"] == "signature":
            value = fingerprint()
        else:
            raise ValueError(cmd)
        wall, cpu_ms = (time.perf_counter()-start)*1000, (time.process_time()-cpu)*1000
        # Payload sizing and memory interrogation are outside the handler timer.
        result = {"op": cmd["op"], "wall_ms": wall, "cpu_ms": cpu_ms,
                  **counts, **memory(), "margin_ms": graph.racy_margin_ns()/1e6,
                  "payload_bytes": len(json.dumps(value, ensure_ascii=False).encode())}
        if server._index is not None:
            idx = server._index
            result.update(cached_parses=len(idx.parsed),
                          indexed_files=len(idx._entries), complete=idx.complete)
            del idx  # Do not keep the previous cold sample alive across gc.collect().
        if cmd["op"] == "signature":
            result["signature"] = value[0]
        emit(result)


def fixture(a):
    a.root.mkdir(parents=True, exist_ok=False)
    sizes = []
    for i in range(a.nodes):
        group = f"Bench{i % 20:02d}"
        name = group if i < 20 else f"node-{i:05d}"
        p = a.root / "= Scope" / group / (name + ".md")
        p.parent.mkdir(parents=True, exist_ok=True)
        # Fixed size, bilingual, non-identical vocabulary, 2 links per node.
        neighbor = i + 20 if i + 20 < a.nodes else i % 20
        neighbor_name = f"node-{neighbor:05d}" if neighbor >= 20 else group
        body = (f"# {name}\n\n측정과 검증을 위한 합성 지식. cache invalidation transaction boundary.\n"
                + "\n".join(f"판단 근거 {j}: 관측한 결과를 검증하고 기존 노드를 갱신한다. "
                            f"token{i % 311}_{j} repeated evidence state{(i+j)%97}."
                            for j in range(12))
                + f"\n\n[[{group}]] [[{neighbor_name}]]\n")
        content = (f"---\nid: 260922-benc-{i:08x}\n"
                   "created: 2026-09-22 09:00 (KST)\nupdated: 2026-09-22 09:00 (KST)\n"
                   "author: agent\ndrafter: agent\nsummary: 색인 성능 합성 표본\n---\n\n" + body)
        p.write_text(content, encoding="utf-8")
        sizes.append(len(content.encode()))
    raw = a.root / "= Scope/Bench00/_raw/.records/sample.txt"
    raw.parent.mkdir(parents=True)
    raw.write_text("# 1\n\n사용자: 합성 원료\n에이전트: 관측 결과\n", encoding="utf-8")
    ledger = a.root / "= Scope/Workbench/_ledger/bench.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{}\n", encoding="utf-8")
    info = {"synthetic": True, "generator": 2, "nodes": a.nodes, "bytes": sum(sizes),
            "min_bytes": min(sizes), "max_bytes": max(sizes),
            "target": "node-00020"}
    (a.root / "perf-fixture.json").write_text(json.dumps(info), encoding="utf-8")
    emit(info)


def run(a):
    info = None
    marker = a.root / "perf-fixture.json"
    if marker.exists():
        info = json.loads(marker.read_text(encoding="utf-8"))
    if a.mutate:
        assert info and info.get("synthetic") is True, "Only synthetic fixtures may be changed"
    target = info["target"] if info else a.target
    procs = []
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("w", encoding="utf-8") as out:
        def record(obj):
            out.write(json.dumps(obj) + "\n")
            out.flush()

        def batch(op, label, rep=0, cold=False):
            cmd = json.dumps({"op": op, "cold": cold, "at": time.perf_counter()+.08})+"\n"
            for p in procs:
                p.stdin.write(cmd)
                p.stdin.flush()
            rows = []
            for i, p in enumerate(procs):
                line = p.stdout.readline()
                if not line:
                    raise RuntimeError(f"worker {i} exited: {p.poll()}; see stderr file")
                row = json.loads(line)
                rows.append(row)
                record({"phase": label, "rep": rep, "worker": i, **row})
            return rows

        try:
            for i in range(a.workers):
                err = a.output.with_suffix(f".worker{i}.stderr").open("w", encoding="utf-8")
                cmd = [sys.executable, __file__, "worker", "--engine", str(a.engine),
                       "--root", str(a.root)]
                if target:
                    cmd += ["--target", target]
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=err, text=True, encoding="utf-8",
                                     creationflags=0x08000000)
                err.close()
                procs.append(p)
            for i, p in enumerate(procs):
                line = p.stdout.readline()
                if not line:
                    raise RuntimeError(f"worker {i} failed to start")
                record({"phase": "startup", "worker": i, **json.loads(line)})
            ops = ["search"] if a.search_only else ["read_node", "overview", "search"]
            for op in ops:
                for j in range(a.repeat):
                    batch(op, "cold", j, cold=True)
                batch(op, "warmup")
                for j in range(a.warm):
                    batch(op, "warm", j)
            before = batch("signature", "signature-before")
            for j in range(a.warm):
                batch("signature", "signature", j)
            if a.mutate:
                changes = [("node", "search"), ("raw", "search"), ("ledger", "search")]
                if not a.search_only:
                    changes.insert(0, ("node", "read_node"))
                paths = {"node": a.root / "= Scope/Bench00/node-00020.md",
                         "raw": a.root / "= Scope/Bench00/_raw/.records/sample.txt",
                         "ledger": a.root / "= Scope/Workbench/_ledger/bench.jsonl"}
                for kind, op in changes:
                    p = paths[kind]
                    original = p.read_bytes()
                    try:
                        for j in range(a.repeat):
                            ready = batch("search", "prime")
                            p.write_bytes(original + (b"\n" * (j+1)))
                            time.sleep(max(r["margin_ms"] for r in ready)/1000 + .01)
                            rows = batch(op, "after-" + kind, j)
                            assert all(not r["racy"] and r["complete"] for r in rows)
                    finally:
                        p.write_bytes(original)
                        time.sleep(.03)
            after = batch("signature", "signature-after")
            record({"phase": "metadata", "fixture": info, "workers": a.workers,
                    "python": sys.version, "engine": str(a.engine),
                    "readonly_signature_unchanged": not a.mutate and before[0]["signature"] == after[0]["signature"]})
        finally:
            for p in procs:
                if p.poll() is None:
                    p.stdin.close()
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
    emit({"output": str(a.output), "workers": a.workers, "fixture": info})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["fixture", "worker", "run"])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--engine", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--nodes", type=int, default=5000)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--warm", type=int, default=25)
    ap.add_argument("--target")
    ap.add_argument("--mutate", action="store_true")
    ap.add_argument("--search-only", action="store_true")
    args = ap.parse_args()
    {"fixture": fixture, "worker": worker, "run": run}[args.mode](args)
