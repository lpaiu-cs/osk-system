"""Upgrade-path matrix: a vault made and used by release S, updated by S's own
updater to a candidate release, keeps working under the candidate.

Why: v3.20.1 renamed the Space roots. Users ran the normal `osk.update`, it
reported success, and then the updated engine refused their vault (the updater
included). Every suite passed: no test made a vault with release X, updated it
with X's own updater, and then used it.

A cell is (starting release S, Space-root layout). Only layouts S can hold are
cells, and each vault gets its layout the way a real one did:

    `00_`  a fresh install of S (v3.21.0+ ships `00_Scope`)
    `= `   a fresh v3.20.0 install; for a later S, v3.20.0's own updater took it to S
    bare   a fresh v3.20.1 install (the hotfix shipped `Scope`); for a later S,
           v3.20.1's own updater took it to S

`= ` on v3.20.1 is not a cell: that engine refuses the vault, so it can be neither
used nor updated by it. It is what the known-bad candidate reproduces:
OSK_MATRIX_CELLS=v3.20.0/= OSK_MATRIX_CANDIDATE=v3.20.1=6f27819144d9… must fail.

Per cell, everything as S's users do it, with S's own code:
1. install per S's docs — clone, own branch, no public remote; from v3.22 also
   the baseline step (`osk.update --to S --apply`). Before it there is no
   baseline, so S's updater first stops on "engine drift" and says to rerun with
   --adopt, which the user does (then deletes the `.local-*` sidecars, as SETUP says);
2. content through S's MCP tool functions, S's user-only CLI (behind a terminal
   stand-in) and S's hooks: clusters in the three Spaces with links and
   derived-from (node and raw round), a raw record, scope memory with an
   eviction settled by a node and one left for tidy, a session binding derived
   from a work repository, a protected region with approvals and a recorded move,
   a Claude conversation captured by the Stop/UserPromptSubmit hooks, and the
   underscore clusters S's surface accepts though Mechanism §1 4항 rules them out;
3. snapshot every file, then update with S's own updater (the two-step gate
   where S has it);
4. check with the candidate: exit codes, version, every mapped file against the
   candidate's release.json (an oracle read from its manifest, not the updater),
   every other file byte-identical (ledgers only appended), the validator, MCP over
   stdio (overview, search, read_node by name and id, read_raw, scope memory,
   run_validators), the SessionStart hook keeping the key, binding and memory and
   carrying S's eviction to tidy, the conversation captured on across the update,
   the protected region's revert and approve, governance protection, and a second
   update to the same tag (candidate's updater) changing no file, a third a no-op.

Adding a release: put its commit in RELEASES, its cells in CELLS (and PR_CELLS if
it should run per PR), and list it in NO_BASELINE if its own docs record no
release baseline.

Environment:
    OSK_MATRIX=full                 every cell (default: PR_CELLS)
    OSK_MATRIX_CELLS=v3.20.0/=,...  explicit cells (layouts `=`, `bare`, `00_`)
    OSK_MATRIX_SOURCE=<repo|bundle> history holding the releases (default: this checkout)
    OSK_MATRIX_CANDIDATE=<tag>[=<sha>]
                                    an existing release in the source; `=<sha>` pins the
                                    commit (default: the source HEAD — its own release tag
                                    if HEAD is a release commit, else a throwaway release
                                    made with HEAD's osk.release inside a temp clone)
    OSK_MATRIX_JOBS=<n>             cells in parallel (default 4)
    OSK_MATRIX_REPORT=<path>        also write the results as JSON
    OSK_MATRIX_KEEP=<dir>           copy the cells' vaults there (debugging)

Exit 0 pass, 1 fail, 77 skipped (no release history, e.g. a shallow checkout);
the regression runner reports 77 as SKIP.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SKIP_EXIT = 77

# Releases by commit. A tag is not an identity: v3.20.1 was re-pointed at the
# hotfix after release, and clones that fetched it earlier still hold the old
# commit under the same name.
RELEASES = {
    "v3.20.0": "e62740d8ca1c0dcd14d9da6d98aaf989bf19ceb8",
    "v3.20.1": "6f27819144d974eefecc27035ba9791abeab407d",
    "v3.21.2": "56ca7dc3e045e27f702e6497b1ba7c9802186502",
    "v3.22.2": "bf32df027350b9dcfeafac43881d2ffd80d8e8d0",
}
LAYOUTS = {"=": "= ", "bare": "", "00_": "00_"}
# (start, layout) -> the release whose fresh install gave the vault that layout
# (None: a fresh install of start itself).
CELLS = {
    ("v3.20.0", "="): None,
    ("v3.20.1", "bare"): None,
    ("v3.21.2", "00_"): None,
    ("v3.21.2", "="): "v3.20.0",
    ("v3.21.2", "bare"): "v3.20.1",
    ("v3.22.2", "00_"): None,
    ("v3.22.2", "="): "v3.20.0",
    ("v3.22.2", "bare"): "v3.20.1",
}
# Per PR: the oldest updater (no gate, no baseline, legacy roots), the newest
# release on its shipped layout, and the newest release on the legacy layout
# (most early vaults). The release workflow runs every cell (OSK_MATRIX=full).
PR_CELLS = [("v3.20.0", "="), ("v3.22.2", "00_"), ("v3.22.2", "=")]
# How each release's own docs make a vault. Until v3.22 the README said "clone
# and switch to your own remote" and nothing recorded a release baseline, so the
# first update stops on "engine drift" and tells the user to rerun with --adopt.
# From v3.22 GETTING-STARTED step 2 records the baseline (`osk.update --to <tag>
# --apply`). Releases not listed here follow the baseline step.
NO_BASELINE = {"v3.20.0", "v3.20.1", "v3.21.2"}

KEY = "matrix-repo"          # the work repository whose hooks bind the session
INBOX = "inbox-repo"         # a session bound to an underscore scope
DRAFTER = "claude-opus-4-1"
SID = "0b4d1c2e-7f3a-4e59-8c21-6a9d3e5f1b77"   # a Claude conversation the hooks capture
STEP_TIMEOUT = 600


def _claude_round(n: int) -> list[dict]:
    """One finished Claude Code turn, as the native JSONL records it (the shape
    test_integration.py pins)."""
    def row(role, content, uid, stop=None, mid=None):
        return {"type": role, "sessionId": SID, "uuid": uid, "isSidechain": False,
                "message": {"role": role, "content": content, "id": mid, "stop_reason": stop}}
    return [row("user", f"matrix question {n}", f"user-{n}"),
            row("assistant", [{"type": "tool_use", "id": f"tool-{n}", "name": "probe",
                               "input": {"n": n}}], f"tool-{n}", "tool_use", f"m-tool-{n}"),
            row("user", [{"type": "tool_result", "tool_use_id": f"tool-{n}",
                          "content": "evidence"}], f"result-{n}"),
            row("assistant", [{"type": "text", "text": f"matrix answer {n}"}],
                f"answer-{n}", "end_turn", f"final-{n}")]


class CellFailure(Exception):
    pass


# ── processes ────────────────────────────────────────────────────────────────

def _env(extra: dict | None = None) -> dict:
    """The environment of a user's shell: no suite variables leak into the vault."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OSK_", "CODEX_", "CLAUDE")) and k != "PYTHONPATH"}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               GIT_AUTHOR_NAME="matrix", GIT_AUTHOR_EMAIL="matrix@example.invalid",
               GIT_COMMITTER_NAME="matrix", GIT_COMMITTER_EMAIL="matrix@example.invalid")
    env.update(extra or {})
    return env


def _run(cmd, cwd=None, env=None, input=None, timeout=STEP_TIMEOUT):
    return subprocess.run(cmd, cwd=cwd, env=env or _env(), input=input,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _git(*args, cwd=None) -> str:
    r = _run(["git", *args], cwd=cwd)
    if r.returncode != 0:
        raise CellFailure(f"git {' '.join(args)}: {r.stderr.strip()[-400:]}")
    return r.stdout.strip()


def _json_tail(text: str):
    """The last JSON document a command printed (osk prints one, pretty)."""
    text = text.strip()
    for i in [m.start() for m in re.finditer(r"(?m)^[{\[]", text)]:
        try:
            return json.loads(text[i:])
        except ValueError:
            continue
    return None


def _py(vault: Path, *args, input=None, env=None):
    """`python -m …` in the vault root with that vault's engine, as SETUP says."""
    return _run([sys.executable, *args], cwd=vault, input=input,
                env=_env({"PYTHONPATH": "_governance/_engine", **(env or {})}))


def _drive(vault: Path, mode: str, cfg: dict) -> dict:
    """Run a driver of this file inside the vault's own engine."""
    r = _py(vault, str(Path(__file__).resolve()), "--drive", mode, json.dumps(cfg))
    got = [ln[len("@@DRIVE@@"):] for ln in r.stdout.splitlines() if ln.startswith("@@DRIVE@@")]
    if r.returncode != 0 or not got:
        raise CellFailure(f"driver {mode} (exit {r.returncode}): "
                          f"{(r.stdout[-1500:] + r.stderr[-2500:]).strip()}")
    return json.loads(got[-1])


# ── drivers (run in a vault's own engine; never import osk at module level) ──

def _say(obj) -> None:
    print("@@DRIVE@@" + json.dumps(obj, ensure_ascii=False), flush=True)


class _Terminal(io.StringIO):
    """The user at a terminal answering `y`: the CLI refuses a non-terminal stdin."""
    def isatty(self):
        return True


def _cli(*argv) -> None:
    from osk import cli
    saved = sys.stdin
    sys.stdin = _Terminal("y\n" * 4)
    try:
        cli.main(list(argv))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise RuntimeError(f"osk {' '.join(argv)}: {e.code}")
    finally:
        sys.stdin = saved


def _load_hook():
    import importlib.util
    path = Path.cwd() / "_governance/_engine/scripts/hooks/claude_session_start.py"
    spec = importlib.util.spec_from_file_location("osk_session_start", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive_content(cfg: dict) -> dict:
    """Use the vault the way an agent and its user do, with this engine only."""
    sys.path.insert(0, str(Path.cwd() / "_governance/_engine"))
    import mcp_server as m
    from osk import approvals
    S, D, P = cfg["roots"]
    key = cfg["key"]
    out: dict = {}

    def ok(name, *a, **kw):
        r = getattr(m, name)(*a, **kw)
        if not (isinstance(r, dict) and r.get("ok")):
            raise RuntimeError(f"{name}{a}{kw} -> {r}")
        return r

    def new_cluster(**kw):
        # The new-cluster gate refuses once; the agent confirms with the user
        # and sends the same request again.
        r = m.create_node(**kw)
        return r if r.get("ok") else ok("create_node", **kw)

    def state(region):
        return approvals.state(region)

    if cfg["phase"] == "A":
        out["key"] = _load_hook().session_key(cfg["work"])
        if out["key"] != key:
            raise RuntimeError(f"session key {out['key']!r}, expected {key!r}")
        new_cluster(title="W1", summary="Matrix work scope hub",
                    body="Work scope of the upgrade matrix. [[Alpha]] [[Beta]]",
                    drafter=DRAFTER, session=key, space=f"{S}/W1")
        raw1 = ok("append_raw", key, "matrix-log", "How do we keep vaults alive?",
                  "Update them with their own updater and check everything.")
        out["round1"] = raw1["round_ref"]
        ok("create_node", title="Alpha", summary="Alpha keeps vaults alive",
           body="Alpha: an update must keep every vault usable. See [[Beta]].",
           drafter=DRAFTER, session=key, edges={"derived-from": raw1["round_ref"]})
        ok("create_node", title="Beta", summary="Beta follows Alpha",
           body="Beta follows [[Alpha]] with a concrete check.",
           drafter=DRAFTER, session=key, edges={"derived-from": "Alpha"})
        new_cluster(title="Topic", summary="Matrix domain hub",
                    body="Domain hub. [[Delta]]", drafter=DRAFTER, space=f"{D}/Topic")
        ok("create_node", title="Delta", summary="Delta is a domain fact",
           body="Delta: a domain fact.", drafter=DRAFTER, space=f"{D}/Topic")
        new_cluster(title="Pat", summary="Matrix person hub",
                    body="Pat reviews the matrix.", drafter=DRAFTER, space=f"{P}/Pat")
        new_cluster(title="Topic2", summary="Second domain hub",
                    body="Second domain hub. [[Epsilon]]", drafter=DRAFTER,
                    space=f"{D}/Topic2")
        ok("create_node", title="Epsilon", summary="Epsilon will move",
           body="Epsilon: reviewed by [[Pat]].", drafter=DRAFTER,
           space=f"{D}/Topic2", edges={"derived-from": "Delta"})
        ok("scope_memory", key, text="- Matrix memory: keep vaults working.\n"
                                     "- Update with the vault's own updater.\n"
                                     "- Scratch note to evict.")
        ok("record_candidate", "duplication", ["Alpha", "Beta"], "matrix candidate")
        # Underscore directories in node spaces: Mechanism §1 4항 says no nodes
        # there, but what S's surface accepts, S's users can have. A scope with
        # its own session, a sub-cluster and a Domain cluster.
        def accepted(**kw):                      # the new-cluster gate refuses once
            return bool(m.create_node(**kw).get("ok") or m.create_node(**kw).get("ok"))
        out["legacy"] = [t for t, space, session in (
            ("_inbox", f"{S}/_inbox", INBOX), ("_drafts", f"{S}/W1/_drafts", None),
            ("_archive", f"{D}/_archive", None))
            if accepted(title=t, summary="Underscore cluster hub", body="Kept aside.",
                        drafter=DRAFTER, session=session, space=space)]
        if "_inbox" in out["legacy"]:
            ok("create_node", title="Idea", summary="An inbox idea", body="Idea: sort later.",
               drafter=DRAFTER, session=INBOX)
            ok("scope_memory", INBOX, text="- Inbox memory.")
            out["legacy"].append("Idea")
        region = f"{D}/Topic"
        _cli("protect", region, "--reason", "matrix region")
        ok("update_node", "Delta", old_text="a domain fact",
           new_text="an approved domain fact")
        out["pending_after_edit"] = state(region)
        _cli("approve", region, "--reason", "matrix approval")
        moved = ok("move_nodes", ["Epsilon"], region)
        for link in moved.get("hub_links", []):
            for name in link.get("remove", []):
                ok("update_node", link["hub"], old_text=f" [[{name}]]", new_text="")
            for name in link.get("add", []):
                ok("update_node", link["hub"], old_text="[[Delta]]",
                   new_text=f"[[Delta]] [[{name}]]")
        out["pending_after_move"] = state(region)
        _cli("approve", region, "--reason", "matrix move approval")
        out["region_A"] = state(region)
    else:
        # The memory limit refuses a write; the next write that trims is recorded
        # as an eviction (Mechanism §9-2 12항). A node settles the first; the
        # scratch line stays unsettled for the candidate's tidy.
        def overflow():
            r = m.scope_memory(key, edits=[{"old_text": "- Scratch note to evict.",
                                            "new_text": "- Scratch note to evict.\n- " + "x" * 1600}])
            if r.get("ok"):
                raise RuntimeError("the scope memory limit accepted 1600 more characters")
        overflow()
        ev = ok("scope_memory", key, edits=[{"old_text": "keep vaults working.",
                                             "new_text": "keep vaults working after updates."}])
        ok("create_node", title="Zeta", summary="Zeta came later",
           body="Zeta belongs to [[W1]]: keep vaults working.", drafter=DRAFTER, session=key,
           edges={"derived-from": "Alpha"}, settle=ev["evicted"])
        ok("update_node", "W1", old_text="[[Beta]]", new_text="[[Beta]] [[Zeta]]")
        raw2 = ok("append_raw", key, "matrix-log", "And after an update?",
                  "The same checks, with the new engine.")
        out["round2"] = raw2["round_ref"]
        overflow()
        ok("scope_memory", key, edits=[{"old_text": "\n- Scratch note to evict.", "new_text": ""}])
        region = f"{D}/Topic"
        ok("update_node", "Delta", old_text="an approved domain fact",
           new_text="an approved domain fact, confirmed twice")
        out["pending_B"] = state(region)
        _cli("approve", region, "--reason", "matrix approval B")
        ok("update_node", "Alpha", add_edges={"derived-from": "Topic"})
        out["region_B"] = state(region)
    return out


def _drive_mcp(cfg: dict) -> dict:
    """The candidate's MCP server over stdio, started the way .mcp.json starts it."""
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path.cwd() / "_governance/_engine/mcp_server.py")],
            env=_env(), cwd=str(Path.cwd()))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                out = {"tools": sorted(t.name for t in (await s.list_tools()).tools)}

                async def call(tool, args):
                    res = await s.call_tool(tool, args)
                    items = [json.loads(c.text) for c in res.content]
                    return items[0] if len(items) == 1 else items

                out["overview"] = await call("overview", {"session": cfg["key"]})
                # A list result arrives as one content item per element.
                hits = await call("search", {"query": "vault usable", "k": 5})
                out["search"] = hits if isinstance(hits, list) else [hits]
                out["read"] = {n: await call("read_node", {"name": n}) for n in cfg["names"]}
                out["read_id"] = await call("read_node", {"name": out["read"]["Alpha"].get("id", "?")})
                out["raw"] = {ref: await call("read_raw", {"ref": ref, "view": "full"})
                              for ref in cfg["rounds"]}
                records = (await call("read_raw", {"space": cfg["space"]})).get("records") or []
                talk = [r["path"] for r in records if r.get("record") != "matrix-log"]
                out["conversation"] = {"records": records} if len(talk) != 1 else {
                    str(n): await call("read_raw", {"ref": f"{talk[0]}#{n}", "view": "full"})
                    for n in (1, 2, 3)}
                out["memory"] = await call("scope_memory", {"session": cfg["key"]})
                out["legacy"] = {n: await call("read_node", {"name": n}) for n in cfg["legacy"]}
                if "_inbox" in cfg["legacy"]:
                    out["inbox_memory"] = await call("scope_memory", {"session": INBOX})
                out["validators"] = await call("run_validators", {})
                return out
    return asyncio.run(asyncio.wait_for(run(), timeout=300))


def _drive_user(cfg: dict) -> dict:
    """After the update: the agent edits a protected node, the user reverts, then
    edits again and approves — with the candidate's MCP functions and CLI."""
    sys.path.insert(0, str(Path.cwd() / "_governance/_engine"))
    import mcp_server as m
    from osk import approvals
    region, path = cfg["region"], Path.cwd() / cfg["node"]
    out = {"before": approvals.state(region)}
    approved = path.read_bytes()
    r = m.update_node("Delta", old_text="confirmed twice", new_text="edited after the update")
    out["edit"] = bool(r.get("ok")) or r
    out["pending"] = approvals.state(region)
    _cli("revert", region, "--reason", "matrix revert")
    out["reverted"] = approvals.state(region)
    out["restored"] = path.read_bytes() == approved
    r = m.update_node("Delta", old_text="confirmed twice", new_text="confirmed after the update")
    out["edit2"] = bool(r.get("ok")) or r
    _cli("approve", region, "--reason", "matrix approval after update")
    out["approved"] = approvals.state(region)
    return out


DRIVERS = {"content": _drive_content, "mcp": _drive_mcp, "user": _drive_user}


# ── source, candidate, vaults ────────────────────────────────────────────────

class Upstream:
    """A private clone of the source history that serves releases to vaults.

    Tags are set here from RELEASES (and the candidate), so a stale or moved tag
    in the source never decides what a cell installs. Nothing is written to the
    source repository."""

    def __init__(self, source: str, work: Path):
        self.path = work / "upstream"
        _git("clone", "-q", "--no-checkout", source, str(self.path))
        self.head = next(sha for sha, _, ref in (
            ln.partition("\t") for ln in _git("ls-remote", source, "HEAD", cwd=work).splitlines())
            if ref == "HEAD")
        self.moved = {}
        for tag, sha in RELEASES.items():
            if not self.has(sha):
                continue
            was = _run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
                       cwd=self.path).stdout.strip()
            if was and was != sha:
                self.moved[tag] = was
            _git("tag", "-f", tag, sha, cwd=self.path)

    def has(self, sha: str) -> bool:
        return _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=self.path).returncode == 0

    def release_json(self, tag: str) -> dict:
        return json.loads(_git("show", f"{tag}:release.json", cwd=self.path))

    def pin(self, tag: str, sha: str) -> None:
        if not self.has(sha):
            raise CellFailure(f"{tag}: commit {sha} is not in the source history")
        _git("tag", "-f", tag, sha, cwd=self.path)

    def head_candidate(self) -> tuple[str, str]:
        """The source HEAD as the candidate: its own release tag when HEAD is a
        release commit (the release workflow), else a throwaway release of HEAD
        made here with HEAD's own osk.release, under a version above every tag.
        The source repository is never touched."""
        if not self.has(self.head):
            raise CellFailure(f"source HEAD {self.head} is not in the clone")
        tags = _git("tag", "-l", "v*", cwd=self.path).split()
        for tag in _git("tag", "--points-at", self.head, cwd=self.path).split():
            if re.fullmatch(r"v\d+\.\d+\.\d+", tag) and self.release_json(tag)["version"] == tag:
                return tag, f"release {tag} at the source HEAD"
        top = max((int(m[1]) for t in tags if (m := re.fullmatch(r"v(\d+)\.\d+\.\d+", t))),
                  default=0)
        version = f"v{top + 1}.0.0"
        _git("checkout", "-q", "-B", "osk-matrix-candidate", self.head, cwd=self.path)
        r = _py(self.path, "-m", "osk.release", "--version", version, "--apply")
        rep = _json_tail(r.stdout) or {}
        if r.returncode != 0 or not rep.get("tagged"):
            raise CellFailure(f"throwaway release of {self.head[:12]} failed: "
                              f"{(r.stdout[-800:] + r.stderr[-1500:]).strip()}")
        return version, f"throwaway release {version} of {self.head[:12]} ({rep['files']} files)"


def _snapshot(vault: Path) -> dict[str, bytes]:
    out = {}
    for p in vault.rglob("*"):
        rel = p.relative_to(vault).as_posix()
        if (rel.split("/", 1)[0] in (".git", ".osk") or "__pycache__" in p.parts
                or not p.is_file()):
            continue
        out[rel] = p.read_bytes()
    return out


def _gate(vault: Path) -> bool:
    """Does this vault's updater stop at a confirmation checkpoint on the first --apply?"""
    return "approval_required" in (vault / "_governance/_engine/osk/update.py").read_text(
        encoding="utf-8")


def _apply(vault: Path, tag: str, gated: bool, *flags) -> list[dict]:
    """One `osk.update --to <tag> --apply`, repeated once when it stops for the
    user's confirmation (the user confirms in between)."""
    runs = []
    for _ in range(2 if gated else 1):
        r = _py(vault, "-m", "osk.update", "--to", tag, "--apply", *flags)
        runs.append({"exit": r.returncode, "report": _json_tail(r.stdout) or {},
                     "stderr": r.stderr.strip()[-2000:]})
        if r.returncode != 2:
            break
    return runs


def _update(vault: Path, tag: str, steps: list) -> dict:
    """Update the vault with its own updater, as that updater's user does: the
    two-step gate where the updater has one, and --adopt when the updater itself
    says the vault has no baseline yet. After an adopt, SETUP tells the user to
    review the `.local-<version>` sidecars and delete them."""
    gated = _gate(vault)
    runs = _apply(vault, tag, gated)
    adopt = runs[-1]["exit"] == 1 and "--adopt" in runs[-1]["stderr"]
    if adopt:
        runs += _apply(vault, tag, gated, "--adopt")
    codes = [x["exit"] for x in runs]
    want = [1] * adopt + ([2, 0] if gated else [0])
    rep = runs[-1]["report"]
    steps.append({"update": tag, "gated": gated, "adopt": adopt, "exits": codes})
    if codes != want or not rep.get("applied"):
        raise CellFailure(f"update to {tag}: exits {codes} (expected {want}); "
                          f"{runs[-1]['stderr'] or json.dumps(rep, ensure_ascii=False)[:1500]}")
    if gated and not runs[adopt]["report"].get("approval_required"):
        raise CellFailure(f"update to {tag}: the first --apply did not stop for confirmation")
    if adopt:
        for side in rep.get("sidecars") or []:
            if side.endswith(f".local-{tag}"):
                (vault / side).unlink()
    return {"runs": runs, "report": rep, "adopt": adopt}


def _install(up: Upstream, dest: Path, release: str) -> Path:
    """Clone the release onto an own `main` without the public remote, and point
    the updater at the private upstream (`.osk/config.json`, as SETUP documents
    for an offline source). The baseline step, where the docs have one, is the
    caller's."""
    vault = dest / "vault"
    _git("clone", "-q", "--no-checkout", str(up.path), str(vault))
    _git("checkout", "-q", "-B", "main", RELEASES[release], cwd=vault)
    _git("remote", "remove", "origin", cwd=vault)
    (vault / ".osk").mkdir()
    (vault / ".osk/config.json").write_text(json.dumps(
        {"upstream": {"source": "git", "url": str(up.path), "pin": None}}), encoding="utf-8")
    return vault


def _validate(vault: Path) -> dict:
    r = _py(vault, "-m", "osk.cli", "validate")
    rep = _json_tail(r.stdout)
    if not isinstance(rep, dict):
        raise CellFailure(f"validate did not run (exit {r.returncode}): "
                          f"{(r.stdout[-500:] + r.stderr[-2500:]).strip()}")
    return rep


def _split(rep: dict, dirs=()) -> tuple[list, list]:
    """Validator failures as (the rest, the messages about `dirs`)."""
    rest, about = [], []
    for group in rep.get("fail") or []:
        for name, msgs in group.items():
            mine = [m for m in msgs if any(f"/{d}/" in str(m).replace("\\", "/") for d in dirs)]
            about += mine
            if len(mine) < len(msgs):
                rest.append({name: [m for m in msgs if m not in mine]})
    return rest, about


def _warnings(rep: dict) -> list[str]:
    w = rep.get("warnings") or {}
    return [f"{k}: {v if isinstance(v, str) else len(v)}" for k, v in sorted(w.items()) if v]


def _status(vault: Path) -> dict:
    r = _py(vault, "-m", "osk.cli", "status")
    rep = _json_tail(r.stdout)
    if not isinstance(rep, dict):
        raise CellFailure(f"status (exit {r.returncode}): {r.stderr.strip()[-2000:]}")
    return rep


def _roots(layout: str) -> list[str]:
    return [LAYOUTS[layout] + k for k in ("Scope", "Domain", "Person")]


def _mapped(up: "Upstream", tag: str) -> dict[str, str]:
    """Instance path -> attested hash of every file the release's own manifest
    maps into an instance (MAP minus DENY and KEEP) — an oracle kept apart from
    the updater's implementation."""
    files = up.release_json(tag)["files"]
    man = _git("show", f"{tag}:_governance/_engine/scripts/publish-manifest.txt", cwd=up.path)
    maps, deny, keep = [], [], set()
    for line in man.splitlines():
        kind, _, rest = line.split("#", 1)[0].strip().partition(" ")
        if kind == "MAP":
            src, _, dst = rest.partition("->")
            maps.append((src.strip(), dst.strip()))
        elif kind == "DENY":
            deny.append(rest.strip())
        elif kind == "KEEP":
            keep.add(rest.strip())
    out = {}
    for path, digest in files.items():
        if path in keep or any(d in path or path.endswith(d.rstrip("/")) for d in deny):
            continue
        for src, dst in maps:
            if path == src or (src.endswith("/") and path.startswith(src)):
                out[dst + path[len(src):] if src.endswith("/") else dst] = digest
                break
    return out


def _hook(vault: Path, cell: Path, script: str, **payload) -> str:
    """One hook invocation the way the harness makes it: the vault's hook script,
    the project directory as cwd, the event JSON on stdin."""
    r = _run([sys.executable, str(vault / "_governance/_engine/scripts/hooks" / script)],
             cwd=cell / "work" / KEY, input=json.dumps(payload),
             env=_env({"CLAUDE_CONFIG_DIR": str(cell / "claude")}))
    if r.returncode != 0:
        raise CellFailure(f"{script} exit {r.returncode}: {r.stderr.strip()[-1500:]}")
    return r.stdout


def _diagnostics(out: str) -> list[str]:
    """What a hook reported as its own failure (it never fails the session)."""
    return re.findall(r"\[osk[^\]]*(?:진단|diagnostic)[^\]]*\]", out)


class Conversation:
    """A Claude Code conversation in the work repository, captured into the vault
    by whatever hooks the vault has at the time."""

    def __init__(self, vault: Path, cell: Path):
        self.vault, self.cell, self.rounds = vault, cell, 0
        self.path = cell / "claude/projects/matrix" / f"{SID}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.said: list[str] = []

    def _event(self, script, **extra) -> str:
        out = _hook(self.vault, self.cell, script, session_id=SID,
                    transcript_path=str(self.path), cwd=str(self.cell / "work" / KEY), **extra)
        self.said += _diagnostics(out)
        return out

    def start(self, source: str) -> str:
        out = self._event("claude_session_start.py", hook_event_name="SessionStart", source=source)
        return ((_json_tail(out) or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")

    def turn(self) -> None:
        self._event("claude_prompt_submit.py", hook_event_name="UserPromptSubmit",
                    prompt=f"matrix question {self.rounds + 1}")
        self.rounds += 1
        with self.path.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in _claude_round(self.rounds)))
        self._event("capture_stop.py", hook_event_name="Stop", stop_hook_active=False)


# ── one cell ─────────────────────────────────────────────────────────────────

def run_cell(up: Upstream, cand: str, work: Path, start: str, layout: str) -> dict:
    t0 = time.monotonic()
    via = CELLS[(start, layout)]
    res = {"cell": f"{start}/{layout}", "via": via, "steps": [], "checks": {}, "facts": {}}
    checks, facts = res["checks"], res["facts"]

    def check(label, cond, detail=""):
        checks[label] = True if cond else (detail or False)

    cell = work / f"{start}-{layout.replace('=', 'eq')}"
    try:
        S, D, P = _roots(layout)
        region = f"{D}/Topic"
        repo = cell / "work" / KEY
        repo.mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=repo)
        _git("commit", "-q", "--allow-empty", "-m", "work", cwd=repo)

        # 1. The vault as S's users have it.
        first = via or start
        vault = _install(up, cell, first)
        shipped = sorted(p.name for p in vault.iterdir()
                         if p.name.endswith(("Scope", "Domain", "Person")))
        if shipped != sorted([S, D, P]):
            raise CellFailure(f"{first} ships {shipped}, not the {layout!r} layout")
        if first not in NO_BASELINE:
            _update(vault, first, res["steps"])                  # GETTING-STARTED step 2
        conv = Conversation(vault, cell)
        cfg = {"roots": [S, D, P], "key": KEY, "work": str(repo)}
        a = _drive(vault, "content", {**cfg, "phase": "A"})
        conv.start("startup")
        conv.turn()
        if via:
            _update(vault, start, res["steps"])                  # the via release's updater
        b = _drive(vault, "content", {**cfg, "phase": "B"})
        conv.turn()
        facts["content"] = {**a, **b}
        check("S: region clean after its approvals", a["region_A"] == b["region_B"] == "clean",
              f"{a['region_A']}/{b['region_B']}")
        check("S: its hooks captured the conversation quietly", not conv.said, conv.said)
        v = _validate(vault)
        check("S: its own validator passes the vault", v.get("verdict") == "PASS", v.get("fail"))
        facts["legacy_accepted_by_S"] = a["legacy"]
        before = _snapshot(vault)

        # 2. S's own updater to the candidate.
        upd = _update(vault, cand, res["steps"])
        rep = upd["report"]
        facts["update"] = {k: rep.get(k) for k in (
            "current", "version", "applied_files", "removed", "skel_created",
            "governance_accepted", "governance_protected")}
        facts["update"]["sidecars"] = len(rep.get("sidecars") or [])
        check("update: the report names the candidate", rep.get("version") == cand, rep.get("version"))
        check("update: no conflict or skeleton; sidecars only from --adopt",
              not (rep.get("conflict") or rep.get("skel_created")
                   or rep.get("sidecars") and not upd["adopt"]),
              {k: rep.get(k) for k in ("conflict", "skel_created", "sidecars")})

        # 3. The data, before any candidate process touches the vault.
        after = _snapshot(vault)
        mapped = _mapped(up, cand)
        framework = set(up.release_json(start)["files"]) | set(mapped)
        changed, grown = [], []
        for rel, data in before.items():
            new = after.get(rel)
            if rel in framework or new == data:
                continue
            if "/_ledger/" in f"/{rel}" and rel.endswith(".jsonl") and new and new.startswith(data):
                grown.append(rel)
            else:
                changed.append(rel if new is not None else f"{rel} (deleted)")
        journal = f"{S}/Workbench/_ledger/update.jsonl"       # the updater's own; an adopt starts it
        added = sorted(r for r in after if r not in before and r not in framework | {journal})
        facts["data"] = {"files": sum(r not in framework for r in before),
                         "ledgers_grown": sorted(grown)}
        check("data: every file byte-identical, ledgers only appended", not changed, changed[:10])
        check("data: no new file outside the framework", not added, added[:10])
        bad = sorted(p for p, h in mapped.items() if not (vault / p).is_file()
                     or "sha256:" + hashlib.sha256((vault / p).read_bytes()).hexdigest() != h)
        check("engine: every mapped file matches the candidate release.json", not bad, bad[:10])
        gone = sorted(p for p in set(_mapped(up, start)) - set(mapped) if (vault / p).exists())
        check("engine: files the candidate dropped are removed", not gone, gone[:10])

        # 4. The candidate engine in the upgraded vault.
        r = _py(vault, "-m", "osk.update", "--to", cand)
        report = _json_tail(r.stdout) or {}
        if r.returncode != 0 or not report:
            raise CellFailure(f"candidate engine cannot run in this vault (osk.update exit "
                              f"{r.returncode}): {r.stderr.strip()[-1500:]}")
        check("version: current is the candidate", report.get("current") == cand,
              report.get("current"))
        check("version: nothing left to apply", not any(report.get(k) for k in (
            "add", "update", "remove", "rebaseline", "conflict", "engine_drift")),
              {k: report.get(k) for k in ("add", "update", "remove", "rebaseline", "conflict")})
        # What S's surface put under `_` directories is judged in its own checks.
        aside = [n for n in a["legacy"] if n.startswith("_")]
        v = _validate(vault)
        facts["validate"] = {"verdict": v.get("verdict"), "warnings": _warnings(v)}
        rest, rejected = _split(v, aside)
        check("validator: no failure (legacy placements judged below)", not rest, rest)

        ctx = conv.start("resume")
        conv.turn()
        facts["hook"] = ctx[:200]
        check("hooks: SessionStart keeps the session key", f'session="{KEY}"' in ctx, ctx[:500])
        check("hooks: SessionStart injects the bound scope memory",
              "keep vaults working after updates." in ctx and "아직 scope 결속이 없다" not in ctx,
              ctx[:800])
        check("hooks: capture continues without diagnostics", not conv.said, conv.said)
        check("hooks: SessionStart carries S's unsettled eviction to tidy",
              "Scratch note to evict" in ctx, ctx[-800:])

        names = ["W1", "Alpha", "Beta", "Zeta", "Topic", "Delta", "Epsilon", "Pat", "Topic2"]
        mc = _drive(vault, "mcp", {"key": KEY, "names": names, "space": f"{S}/W1",
                                   "rounds": [a["round1"], b["round2"]],
                                   "legacy": a["legacy"]})
        # v4 enforces Mechanism §1 4항: `_` directories are not node places. The files
        # stay byte-identical (the data check above); the release notes carry the move.
        if a["legacy"]:
            unread = {n for n, r in mc["legacy"].items() if "error" in r}
            named = {d for d in aside
                     if any(f"/{d}/" in str(m).replace("\\", "/") for m in rejected)}
            check("legacy: v4 does not read nodes under `_` directories; the validator names each",
                  unread == set(a["legacy"]) and named == set(aside),
                  {"read_node": sorted(unread), "validator": rejected})
        if "_inbox" in a["legacy"]:
            check("legacy: the session bound to `_inbox` is refused, naming the missing scope",
                  "_inbox" in json.dumps(mc["inbox_memory"], ensure_ascii=False)
                  and "Inbox memory." not in (mc["inbox_memory"].get("text") or ""),
                  mc["inbox_memory"])
        ov = mc["overview"]
        check("mcp: the server starts and lists its tools", len(mc["tools"]) >= 12, mc["tools"])
        check("mcp: overview sees every node, nothing broken",
              ov.get("nodes", 0) >= len(names) and not ov.get("broken"), ov)
        check("mcp: the session binding resolves", ov.get("session_scope") == "W1",
              ov.get("session_scope"))
        check("mcp: search finds the content",
              any(x.get("title") == "Alpha" for x in mc["search"] if isinstance(x, dict)),
              mc["search"])
        unread = {n: r for n, r in mc["read"].items() if "error" in r
                  or "sha256:" + hashlib.sha256((vault / r["path"]).read_bytes()).hexdigest()
                  != r.get("hash")}
        check("mcp: read_node reads every node at its bytes", not unread, unread)
        check("mcp: read_node by id", mc["read_id"].get("name") == "Alpha", mc["read_id"])
        raw_bad = {ref: r for ref, r in mc["raw"].items() if r.get("ok") is False or "error" in r}
        check("mcp: raw rounds S wrote still read", not raw_bad, raw_bad)
        talk = mc["conversation"]
        check("mcp: the hook-captured record reads, S's rounds and the candidate's",
              [f"matrix answer {n}" in json.dumps(talk.get(str(n)), ensure_ascii=False)
               for n in (1, 2, 3)] == [True] * 3, talk)
        check("mcp: scope memory reads what S wrote",
              "keep vaults working after updates." in (mc["memory"].get("text") or ""),
              mc["memory"])
        check("mcp: run_validators agrees with the CLI validator",
              mc["validators"].get("verdict") == v.get("verdict")
              and not _split(mc["validators"], aside)[0], mc["validators"].get("fail"))

        st = _status(vault)
        facts["status"] = {k: st.get(k) for k in ("protected_regions", "evictions", "warnings")}
        check("region: clean after the update", st.get("protected_regions", {}).get(region) == "clean",
              st.get("protected_regions"))
        check("evictions: S's settled one stays settled, the other is still pending",
              (st.get("evictions") or {}).get("W1", {}).get("unsettled") == 1, st.get("evictions"))
        user = _drive(vault, "user", {"region": region, "node": f"{D}/Topic/Delta.md"})
        facts["revert_approve"] = user
        check("region: revert restores the approved bytes",
              user["pending"] == "pending" and user["reverted"] == "clean" and user["restored"], user)
        check("region: approve accepts a new change", user["approved"] == "clean", user)

        # 5. The same tag again, now with the candidate's own updater.
        again = _update(vault, cand, res["steps"])["report"]
        facts["second_update"] = {k: again.get(k) for k in (
            "applied_files", "removed", "governance_protected", "governance_accepted")}
        check("second update: changes no file", again.get("applied_files") == 0
              and not again.get("removed") and not again.get("sidecars"), facts["second_update"])
        st = _status(vault)
        facts["governance"] = st.get("protected_regions", {}).get("_governance")
        check("governance: protected and clean at the end",
              facts["governance"] == "clean" and not st.get("warnings"),
              {"regions": st.get("protected_regions"), "warnings": st.get("warnings")})
        r = _py(vault, "-m", "osk.update", "--to", cand)
        third = _json_tail(r.stdout) or {}
        check("third update: nothing to do", r.returncode == 0 and third.get("current") == cand
              and not any(third.get(k) for k in ("add", "update", "remove", "rebaseline"))
              and "protect" not in (third.get("governance") or {}),
              {k: third.get(k) for k in ("current", "add", "update", "remove", "rebaseline",
                                         "governance")})
        v = _validate(vault)
        rest = _split(v, aside)[0]
        check("validator: no failure at the end", not rest, rest)
    except CellFailure as e:
        res["error"] = str(e)
    except subprocess.TimeoutExpired as e:
        res["error"] = f"timeout after {e.timeout}s: {e.cmd}"
    except Exception as e:                        # the harness itself; keep the other cells
        import traceback
        res["error"] = "".join(traceback.format_exception(e)[-4:])
    res["ok"] = "error" not in res and all(v is True for v in checks.values())
    res["seconds"] = round(time.monotonic() - t0, 1)
    return res


# ── the matrix ───────────────────────────────────────────────────────────────

def _source() -> str | None:
    src = os.environ.get("OSK_MATRIX_SOURCE")
    if src:
        return src
    r = _run(["git", "-C", str(ENGINE), "rev-parse", "--show-toplevel"])
    return r.stdout.strip() if r.returncode == 0 else None


def _cells() -> list[tuple[str, str]]:
    raw = os.environ.get("OSK_MATRIX_CELLS")
    if raw:
        cells = [tuple(c.strip().split("/", 1)) for c in raw.split(",") if c.strip()]
        unknown = [c for c in cells if c not in CELLS]
        if unknown:
            raise SystemExit(f"unknown cells {unknown}; known: {sorted(CELLS)}")
        return cells
    return sorted(CELLS) if os.environ.get("OSK_MATRIX") == "full" else PR_CELLS


def main() -> int:
    t0 = time.monotonic()
    src = _source()
    if not src:
        print("SKIP: no source history (not a git checkout and OSK_MATRIX_SOURCE unset)")
        return SKIP_EXIT
    cells = _cells()
    # Read-only git objects on Windows: the cleanup resets permissions itself.
    with tempfile.TemporaryDirectory(prefix="osk-matrix-", ignore_cleanup_errors=True) as td:
        work = Path(os.path.realpath(td))
        up = Upstream(src, work)
        need = {r for c in cells for r in (c[0], CELLS[c] or c[0])}
        missing = sorted(r for r in need if not up.has(RELEASES[r]))
        if missing:
            print(f"SKIP: the source history lacks {missing} "
                  f"({', '.join(RELEASES[r][:12] for r in missing)}) — a shallow checkout? "
                  f"CI needs actions/checkout fetch-depth: 0")
            return SKIP_EXIT
        spec = os.environ.get("OSK_MATRIX_CANDIDATE")
        if spec:
            cand, _, sha = spec.partition("=")
            if sha:
                up.pin(cand, sha)
            elif not up.has(cand):
                print(f"FAIL: candidate {cand} is not in the source")
                return 1
            how = f"{cand} from the source" + (f" pinned at {sha[:12]}" if sha else "")
        else:
            cand, how = up.head_candidate()
        prep = time.monotonic() - t0
        print(f"candidate: {how}; source: {src}; prepared in {prep:.1f}s")
        for tag, was in sorted(up.moved.items()):
            print(f"note: the source's {tag} points at {was[:12]}; the matrix uses the "
                  f"release {RELEASES[tag][:12]}")
        jobs = max(1, int(os.environ.get("OSK_MATRIX_JOBS") or 4))
        with concurrent.futures.ThreadPoolExecutor(min(jobs, len(cells))) as pool:
            results = list(pool.map(lambda c: run_cell(up, cand, work, *c), cells))
        keep = os.environ.get("OSK_MATRIX_KEEP")
        if keep:                                  # debugging: the vaults as the cells left them
            shutil.copytree(work, keep, ignore=shutil.ignore_patterns("upstream"),
                            dirs_exist_ok=True)
    failed = [r for r in results if not r["ok"]]
    for r in results:
        print(f"\n{'PASS' if r['ok'] else 'FAIL'} {r['cell']}"
              f"{' (via ' + r['via'] + ')' if r['via'] else ''} — {r['seconds']}s  "
              f"updates: {[s['exits'] for s in r['steps']]}")
        if r.get("error"):
            print(f"  error: {r['error']}")
        for label, v in r["checks"].items():
            print(f"  {'ok ' if v is True else 'BAD'} {label}"
                  + ("" if v is True else f": {json.dumps(v, ensure_ascii=False, default=str)[:1500]}"))
        for k, v in r["facts"].items():
            if k != "content":
                print(f"  · {k}: {json.dumps(v, ensure_ascii=False, default=str)[:600]}")
    # Last, and short: a runner that keeps only the tail still gets every cell.
    total = time.monotonic() - t0
    print(f"\nupgrade matrix → {cand} ({how}):")
    for r in results:
        bad = [k for k, v in r["checks"].items() if v is not True]
        print(f"  {'PASS' if r['ok'] else 'FAIL'} {r['cell']:<14} updates {[s['exits'] for s in r['steps']]}"
              f"  checks {len(r['checks']) - len(bad)}/{len(r['checks'])}"
              + (f"\n       error: {r['error'].strip().splitlines()[-1][:300]}"
                 if r.get("error") else "")
              + "".join(f"\n       BAD {k}" for k in bad))
    print(f"upgrade matrix: {len(results) - len(failed)}/{len(results)} cells pass "
          f"to {cand} in {total:.1f}s")
    out = os.environ.get("OSK_MATRIX_REPORT")
    if out:
        Path(out).write_text(json.dumps({"candidate": cand, "how": how, "seconds": total,
                                         "cells": results}, ensure_ascii=False, indent=1,
                                        default=str), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--drive"]:
        _say(DRIVERS[sys.argv[2]](json.loads(sys.argv[3])))
        sys.exit(0)
    sys.exit(main())
