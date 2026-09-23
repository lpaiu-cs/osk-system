"""Issue 17: real local Git transport + real update_node in a second process.

All mutation happens inside a newly created synthetic lab. Delay injection is
explicitly labelled; it is not a measurement of production network latency.
"""
import argparse
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from types import SimpleNamespace

from bench_index import emit, fixture


def git(root, *args):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                       text=True, encoding="utf-8", creationflags=0x08000000,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=90)
    if r.returncode:
        raise RuntimeError(f"git {args}: {r.stdout} {r.stderr}")
    return r.stdout.strip()


def configure(root):
    git(root, "config", "user.name", "OSK benchmark")
    git(root, "config", "user.email", "benchmark@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "core.hooksPath", str(root / ".git/no-hooks"))


def writer(a):
    os.environ["OSK_VAULT_ROOT"] = str(a.root.resolve())
    sys.path.insert(0, str(a.engine.resolve()))
    from osk import core, write, contract
    original_lock, original_unlock = core.lock_exclusive, core.unlock
    timing = {}

    def lock(f, *args, **kwargs):
        start = time.perf_counter()
        result = original_lock(f, *args, **kwargs)
        if Path(f.name) == core.mutation_lock_path():
            timing.update(wait_ms=(time.perf_counter()-start)*1000,
                          acquired_at=time.perf_counter())
        return result

    def unlock(f):
        if Path(f.name) == core.mutation_lock_path():
            timing["hold_ms"] = (time.perf_counter()-timing["acquired_at"])*1000
        return original_unlock(f)

    core.lock_exclusive, core.unlock = lock, unlock
    emit({"ready": True})
    for line in sys.stdin:
        cmd = json.loads(line)
        timing.clear()
        emit({"attempt_at": time.perf_counter()})
        start = time.perf_counter()
        op, seq = cmd.get("op", "update"), cmd["seq"]
        if op == "update":
            result = write.update_node("node-00020", summary=f"측정 {seq}")
            parsed = contract.parse(a.root / "00_Scope/Bench00/node-00020.md")
            assert parsed.meta["summary"] == f"측정 {seq}"
        elif op == "create":
            result = write.create_node(f"created-{seq}", "새 노드 측정", "[[Bench00]]",
                                       "agent", space="00_Scope/Bench00")
            assert (a.root / f"00_Scope/Bench00/created-{seq}.md").exists()
        else:
            raise ValueError(op)
        assert result["ok"], result
        emit({"op": op, "wall_ms": (time.perf_counter()-start)*1000, **timing})


def run(a):
    assert not a.root.exists(), "Use a new synthetic lab; never an existing repository"
    a.root.mkdir(parents=True)
    local, remote, peer = [a.root / x for x in ("local", "remote.git", "peer")]
    fixture(SimpleNamespace(root=local, nodes=a.nodes))
    (local / ".gitignore").write_text(".osk/\nperf-fixture.json\n", encoding="utf-8")
    git(a.root, "init", "--bare", "--initial-branch=main", str(remote))
    git(local, "init", "--initial-branch=main")
    configure(local)
    git(local, "add", "-A")
    git(local, "commit", "-m", "Synthetic baseline")
    git(local, "remote", "add", "origin", str(remote))
    git(local, "push", "-u", "origin", "main")
    clone_start = time.perf_counter()
    git(a.root, "clone", "--no-local", str(remote), str(peer))
    clone_ms = (time.perf_counter()-clone_start)*1000
    configure(peer)
    os.environ["OSK_VAULT_ROOT"] = str(local)
    sys.path.insert(0, str(a.engine.resolve()))
    import sync_daemon as sync
    import vault_sync
    a.output.parent.mkdir(parents=True, exist_ok=True)
    err = a.output.with_suffix(".writer.stderr").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, __file__, "writer", "--engine", str(a.engine),
                             "--root", str(local)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=err, text=True, encoding="utf-8", creationflags=0x08000000)
    err.close()
    assert json.loads(proc.stdout.readline())["ready"]
    seq = 0
    state = {}
    original_lock, original_unlock, original_git = sync.lock_exclusive, sync.unlock, vault_sync._git

    def lock(f, *args, **kwargs):
        start = time.perf_counter()
        original_lock(f, *args, **kwargs)
        state.update(sync_wait_ms=(time.perf_counter()-start)*1000, lock_at=time.perf_counter())

    def unlock(f):
        released = time.perf_counter()
        duration = (released-state["lock_at"])*1000
        state["sync_hold_ms"] = state.get("sync_hold_ms", 0) + duration
        state["release_at"] = released
        state.setdefault("lock_windows", []).append([state["lock_at"], released])
        return original_unlock(f)

    def send(op="update"):
        nonlocal seq
        seq += 1
        proc.stdin.write(json.dumps({"seq": seq, "op": op})+"\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("writer failed; see stderr")
        return json.loads(line)

    def receive():
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("writer failed; see stderr")
        return json.loads(line)

    def timed_git(root, args, timeout, **kwargs):
        if args[0] == "fetch" and not state.get("writer_started"):
            state["writer_started"] = send()
        started = time.perf_counter()
        delay = state.get("delay", 0) if args[0] in ("fetch", "push") else 0
        if delay:
            time.sleep(delay)
        race_setup_ms = 0
        if args[0] == "push" and state.get("reject_once"):
            state["reject_once"] = False
            # A real competing commit, not a mocked rejected status.
            race_start = time.perf_counter()
            (peer / "competing.txt").write_text(str(seq), encoding="utf-8")
            git(peer, "add", "-A")
            git(peer, "commit", "-m", "Competing remote commit")
            git(peer, "push", "origin", "main")
            race_setup_ms = (time.perf_counter()-race_start)*1000
        result = original_git(root, args, timeout, **kwargs)
        state.setdefault("git_calls", []).append({"command": args[0],
            "wall_ms": (time.perf_counter()-started)*1000,
            "injected_ms": delay*1000, "race_setup_ms": race_setup_ms,
            "returncode": result.returncode})
        return result

    sync.lock_exclusive, sync.unlock, vault_sync._git = lock, unlock, timed_git
    with a.output.open("w", encoding="utf-8") as out:
        def record(value):
            out.write(json.dumps(value)+"\n")
            out.flush()

        def tick(label, rep, delay=0, reject=False):
            state.clear()
            state.update(delay=delay, reject_once=reject)
            started = time.perf_counter()
            status = sync.once(local)
            wall_ms = (time.perf_counter()-started)*1000
            assert status == "ok", status
            result = receive()
            assert state.get("writer_started")
            assert result["acquired_at"] < state["lock_windows"][0][0], "fetch blocked the writer"
            result["acquire_after_sync_release_ms"] = (result["acquired_at"]-state["release_at"])*1000
            record({"phase": label, "rep": rep, "sync_wall_ms": wall_ms,
                    "writer": result, **state})
            # The fetch-time writer must have been included in this tick.
            assert git(local, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")
            assert f"측정 {seq}" in git(remote, "show", "main:00_Scope/Bench00/node-00020.md")

        try:
            record({"phase": "metadata", "nodes": a.nodes, "clone_ms": clone_ms,
                    "engine": str(a.engine), "python": sys.version,
                    "transport": "local bare Git; --no-local clone"})
            for op in ("update", "create"):
                for i in range(a.repeat):
                    send(op)
                    record({"phase": "uncontended", "rep": i, **receive()})
            # Publish fixture changes before the no-change sample.
            git(local, "add", "-A")
            git(local, "commit", "-m", "Baseline mutations")
            git(local, "push", "origin", "main")
            git(peer, "pull", "--rebase", "origin", "main")
            for i in range(a.repeat):
                tick("local-sync", i)
            for i in range(a.slow_repeats):
                tick("injected-network-delay", i, delay=a.delay)
            git(peer, "pull", "--rebase", "origin", "main")
            tick("real-push-retry", 0, delay=a.delay, reject=True)
            git(peer, "pull", "--rebase", "origin", "main")
            bulk = peer / "00_Scope/Bench00/_raw/.records/bulk"
            bulk.mkdir(parents=True)
            rng = random.Random(17)
            for i in range(16):
                (bulk / f"{i}.txt").write_bytes(rng.randbytes(1024*1024))
            git(peer, "add", "-A")
            git(peer, "commit", "-m", "16 MiB incoming synthetic payload")
            git(peer, "push", "origin", "main")
            tick("incoming-16mib", 0)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    emit({"output": str(a.output)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["run", "writer"])
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--engine", type=Path, required=True)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--nodes", type=int, default=1918)
    ap.add_argument("--repeat", type=int, default=30)
    ap.add_argument("--slow-repeats", type=int, default=5)
    ap.add_argument("--delay", type=float, default=1.2)
    args = ap.parse_args()
    {"run": run, "writer": writer}[args.mode](args)
