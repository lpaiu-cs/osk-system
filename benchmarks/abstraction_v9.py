"""Reproducible v9/v10 fixture runs through the real engine, using subscription Codex CLI.

Private fixtures/results stay outside the source repository. Never deploy to the live vault.
Run with the vault venv: abstraction_v9.py run --fixtures DIR --output DIR --vault DIR
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "_governance/_engine"
MODELS = ("gpt-5.6-sol", "gpt-6.1-terra")


def usage(path):
    totals = {}
    turns = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("type") == "turn.completed" and isinstance(row.get("usage"), dict):
            turns += 1
            for k, v in row["usage"].items():
                if isinstance(v, int):
                    totals[k] = totals.get(k, 0) + v
    return {"measured": bool(turns), "turns": turns, **totals}


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout


def prepare(fixture, out, version="v9"):
    from osk import abstraction, contract, core, graph, growth, rechecks
    fx = json.loads(fixture.read_text(encoding="utf-8"))
    idx = graph.Index()
    path = core.ROOT / fx["path"]
    target = contract.parse(path)
    node_name = fx["dependents"][0]
    node_path = idx.locate(node_name)[0]
    node = contract.parse(node_path)
    pair = next((k, ts, ref) for k, (ts, ref) in rechecks.pairs(idx, node.meta).items()
                if k.split("#")[0] == target.id)
    key, ts, ref = pair
    next_nodes = [name for name, _, _, ps, _ in rechecks._citing(idx, {})
                  if any(k.split("#")[0] == node.id for k in ps)]
    job = {"key": f"recheck:{node.id}:{key}", "node": node_name, "target": ref,
           "id": node.id, "target_key": key, "target_state": ts,
           "node_state": rechecks.state(node_path.read_bytes()), "cascade": False, "next": next_nodes}
    before = git(core.ROOT, "show", f"{fx['parent']}:{fx['path']}")
    after = path.read_text(encoding="utf-8")
    from osk import evidence
    added = set(fx["added_coords"])
    commit = {"commit": fx["sha"][:12], "at": fx["at"], "diff": fx["diff"],
              "evidence_added": [line for line in after.splitlines()
                                 if (m := evidence.LINE.match(line)) and m[2] in added],
              "rounds": [], "before_body": contract.parse_bytes(path, before.encode()).body,
              "after_body": target.body}
    abstraction.prepare(job, [commit], idx=idx, version=version)
    planned = {"prompt_version": version, "candidates": [], "scope_jobs": [],
               "organization_jobs": [], "eviction_jobs": [], "recheck_jobs": [job],
               "work_order": [{"queue": "recheck_jobs", "index": 0}]}
    with core.mutation_lock():
        record = core.ledger_append(growth.LEDGER, {"kind": "plan", **planned})
    planned["manifest"] = record["rid"]
    dump(out / "planned.json", planned)
    (out / "prompt.txt").write_text(growth.prompt(planned), encoding="utf-8")
    (out / "before.md").write_text(after, encoding="utf-8")
    (out / "historical-before.md").write_text(before, encoding="utf-8")
    print(json.dumps({"sources": len(job["abstraction"]["cited"]), "manifest": planned["manifest"]}))


def check(out):
    from osk import graph, growth
    planned = json.loads((out / "planned.json").read_text(encoding="utf-8"))
    try:
        applied = growth._apply_final_reviews(out / "events.jsonl", planned)
        statuses = {j["key"]: growth._recheck_status(j, graph.Index()) for j in planned["recheck_jobs"]}
        result = {"applied": applied, "statuses": statuses,
                  "complete": not applied["errors"] and all(s["status"] == "complete" for s in statuses.values())}
    except (ValueError, OSError) as exc:
        result = {"complete": False, "error": str(exc)}
    dump(out / "verification.json", result)
    print(json.dumps(result, ensure_ascii=False))


def preflight(fixtures, args):
    sb = (args.output / 'preflight-vault').resolve()
    if sb.exists():
        raise ValueError(f'preflight directory already exists: {sb}')
    subprocess.run(['git','clone','-q','--shared','--no-checkout',str(args.vault),str(sb)],check=True)
    env = {k:v for k,v in os.environ.items() if not k.startswith(('CLAUDE','OSK_'))}
    env.update(OSK_VAULT_ROOT=sb.as_posix(),PYTHONPATH=str(ENGINE),PYTHONUTF8='1',OSK_GROWTH_WORKER='1')
    results = []
    for fixture in fixtures:
        fx = json.loads(fixture.read_text(encoding='utf-8'))
        git(sb,'checkout','-q','--detach',fx['sha'])
        out = args.output / 'preflight' / fx['id']
        out.mkdir(parents=True,exist_ok=True)
        result = subprocess.run([sys.executable,str(Path(__file__).resolve()),'prepare','--fixture',str(fixture),'--output',str(out),'--prompt-version',args.prompt_version],
                                cwd=sb,env=env,capture_output=True,text=True,encoding='utf-8')
        (out/'prepare.log').write_text(result.stdout+result.stderr,encoding='utf-8')
        results.append({'id':fx['id'],'rc':result.returncode})
        # prepare appends only a local plan; restore tracked files before the next historical checkout.
        git(sb,'reset','--hard',fx['sha'])
        print('preflight',fx['id'],result.returncode,flush=True)
    dump(args.output/'preflight.json',results)
    if sb.parent==args.output.resolve() and sb.name=='preflight-vault' and sb!=args.vault.resolve():
        shutil.rmtree(sb)
    if any(r['rc'] for r in results):
        raise RuntimeError('fixture preparation failed; inspect preflight logs')


def one(fixture, model, rep, args):
    fx = json.loads(fixture.read_text(encoding="utf-8"))
    name = f"{fx['id']}_{model}_{rep}"
    out = args.output / "runs" / name
    if (out / "done.json").exists():
        previous = json.loads((out / "planned.json").read_text(encoding="utf-8"))
        if previous.get("prompt_version") != args.prompt_version:
            raise ValueError("output contains another prompt version; choose a fresh output directory")
        return
    out.mkdir(parents=True, exist_ok=True)
    sandboxes = (args.output / "sandboxes").resolve()
    sb = (sandboxes / name).resolve()
    if not sb.is_relative_to(sandboxes) or sb == sandboxes or sb == args.vault.resolve():
        raise ValueError("sandbox path escaped output")
    if sb.exists():
        raise ValueError(f"unfinished sandbox preserved: {sb}")
    sandboxes.mkdir(exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(args.vault), str(sb)], check=True)
    git(sb, "checkout", "-q", "--detach", fx["sha"])
    git(sb, "config", "user.name", "osk-v9-experiment")
    git(sb, "config", "user.email", "osk-v9@invalid")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "OSK_"))}
    env.update(OSK_VAULT_ROOT=sb.as_posix(), PYTHONPATH=str(ENGINE), PYTHONUTF8="1", OSK_GROWTH_WORKER="1")
    j = subprocess.run([sys.executable, str(Path(__file__).resolve()), "prepare", "--fixture", str(fixture),
                        "--output", str(out), "--prompt-version", args.prompt_version], env=env, cwd=sb, capture_output=True, text=True, encoding="utf-8")
    (out / "prepare.log").write_text(j.stdout + j.stderr, encoding="utf-8")
    if j.returncode:
        raise RuntimeError(j.stderr)
    mcp = ('mcp_servers={"osk-system"={command="%s",args=["%s/mcp_server.py"],required=true,'
           'default_tools_approval_mode="approve",env={OSK_VAULT_ROOT="%s",PYTHONPATH="%s",PYTHONUTF8="1",OSK_GROWTH_WORKER="1"}}}'
           % (Path(sys.executable).as_posix(), ENGINE.as_posix(), sb.as_posix(), ENGINE.as_posix()))
    cli = os.environ.get("CODEX_APP_RUNTIME_CLI_PATH") or shutil.which("codex")
    cmd = [cli, "exec", "--ignore-user-config", "--json", "--skip-git-repo-check",
           "--sandbox", "read-only", "--disable", "hooks", "-c", mcp,
           "-c", 'forced_login_method="chatgpt"', "-c", 'approval_policy="on-request"',
           "-c", 'project_doc_max_bytes=0', "-c", 'model_reasoning_effort="medium"',
           "-c", 'developer_instructions="Perform only the supplied OSK review using OSK MCP. Return its requested JSON. Do not delegate."',
           "--model", model, "-o", str(out / "last.txt"), "-"]
    if not args.retain_trace:
        cmd.insert(2, "--ephemeral")
    dump(out / "argv.json", cmd)
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()
    from osk import growth
    with (out / "events.jsonl").open("wb") as ev, (out / "stderr.txt").open("wb") as er:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=ev, stderr=er, env=env, cwd=sb,
                                creationflags=0x08000200 if os.name == "nt" else 0,
                                start_new_session=os.name != "nt")
        try:
            proc.communicate((out / "prompt.txt").read_bytes(), timeout=args.timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            growth._stop_tree(proc)
            rc = "timeout"
    seconds = round(time.monotonic() - t0, 3)
    from abstraction_trace import collect_trace
    trace = (collect_trace(out, Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))), started)
             if args.retain_trace else {"status": "disabled", "compaction_count": None})
    if args.retain_trace and trace["status"] != "captured":
        print(name, "WARNING: compaction evidence", trace["status"], file=sys.stderr, flush=True)
    if rc == 0:
        checked = subprocess.run([sys.executable, str(Path(__file__).resolve()), "check", "--output", str(out)],
                                 env=env, cwd=sb, capture_output=True, text=True, encoding="utf-8")
        (out / "check.log").write_text(checked.stdout + checked.stderr, encoding="utf-8")
        if checked.returncode:
            raise RuntimeError(checked.stderr)
    else:
        dump(out / "verification.json", {"complete": False, "error": f"worker exit {rc}"})
    target = sb / fx["path"]
    (out / "after.md").write_text(target.read_text(encoding="utf-8") if target.exists() else "", encoding="utf-8")
    (out / "vault.diff").write_text(git(sb, "-c", "core.quotepath=off", "diff", fx["sha"]), encoding="utf-8")
    measured_usage = usage(out / "events.jsonl")
    dump(out / "done.json", {"model": model, "reasoning_effort": "medium", "repeat": rep,
                              "prompt_version": args.prompt_version,
                              "started_at": started, "rc": rc, "seconds": seconds, "usage": measured_usage,
                              "compaction_trace": trace["status"], "compaction_count": trace["compaction_count"],
                              "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                              "prompt_sha256": hashlib.sha256((out / "prompt.txt").read_bytes()).hexdigest()})
    # The resolved sandbox was checked above; never remove an input or a live vault.
    if sb.is_relative_to(sandboxes) and sb != sandboxes:
        shutil.rmtree(sb)
    verification = json.loads((out / "verification.json").read_text(encoding="utf-8"))
    print(name, "rc", rc, "complete", verification["complete"], seconds, "s", flush=True)
    if rc != 0:
        raise RuntimeError(f"worker failed; batch stopped for inspection: {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("run", "prepare", "check", "preflight"))
    p.add_argument("--fixtures", type=Path)
    p.add_argument("--fixture", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--vault", type=Path)
    p.add_argument("--ids", nargs="*")
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--retain-trace", action="store_true", help="Opt in to persisted CLI rollout/compaction logs; default stays ephemeral")
    p.add_argument("--prompt-version", choices=("v9", "v10", "v11"), default="v9")
    args = p.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ENGINE))
    if args.mode == "prepare":
        prepare(args.fixture.resolve(), args.output, args.prompt_version)
    elif args.mode == "check":
        check(args.output)
    else:
        fixtures = [f.resolve() for f in sorted(args.fixtures.glob("F*.json"), key=lambda f:int(f.stem[1:])) if not args.ids or f.stem in args.ids]
        if args.mode == 'preflight':
            preflight(fixtures,args)
            return
        for rep in range(1, args.repeats + 1):
            for i, f in enumerate(fixtures):
                for model in (MODELS if (i + rep) % 2 else MODELS[::-1]):
                    one(f, model, rep, args)


if __name__ == "__main__":
    main()
