"""Opt-in input-hook experiment (#245). Only candidate IDs and sample metadata leave the worker."""
from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import time
from pathlib import Path

from . import conversation_state, core, harness, routing
from ._portalock import lock_exclusive
from .harness import base
from .vault import Vault


def _save(path: Path, row: dict) -> None:
    core.atomic_write(path, json.dumps(row, ensure_ascii=False).encode("utf-8"))


def submit(vault: Vault, session: str, sid: str, transcript: str | None, turn: int, prompt: str) -> None:
    prompt = base.GOAL.sub("", base.INJECTED.sub("", prompt)).strip()
    if not prompt:
        return
    record = conversation_state.state_path(vault, "claude", sid).with_suffix(".recall-shadow") / f"{turn}.json"
    requested = time.time_ns()
    row = {"turn": turn, "requested_ns": requested, "status": "pending"}
    try:
        _save(record, row)
        boundary = None
        if transcript and harness.get("claude").claims(sid, transcript):
            try:
                stat = Path(transcript).stat()
                boundary = [stat.st_dev, stat.st_ino, stat.st_size]
            except OSError:
                pass       # The first prompt can arrive before the transcript exists.
        payload = {"session": session, "sid": sid, "turn": turn, "requested_ns": requested,
                   "prompt": prompt[:2000], "transcript": transcript, "boundary": boundary}
        core.spawn_worker(vault, "osk.recall_probe", stdin=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except OSError as exc:
        # An optional measurement cannot replace the normal input-hook response.
        try:
            _save(record, {**row, "status": "failed", "error": type(exc).__name__})
        except OSError:
            pass


def _previous_reply(path: str, boundary: list, sid: str) -> str:
    # Read only bytes present at submit, even if a later turn finished before this worker ran.
    with Path(path).open("rb") as f:
        stat = os.fstat(f.fileno())
        if [stat.st_dev, stat.st_ino] != boundary[:2] or stat.st_size < boundary[2]:
            return ""
        start = max(0, boundary[2] - 131072)
        f.seek(start)
        lines = f.read(boundary[2] - start).split(b"\n")
    for line in reversed(lines[1:] if start else lines):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        reply = _assistant_text(row, sid)
        if reply:
            return reply[-500:]
    return ""


def _assistant_text(row, sid: str) -> str:
    if not isinstance(row, dict) or row.get("type") != "assistant" or row.get("isSidechain") or row.get("sessionId") != sid:
        return ""
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "\n".join(b["text"] for b in content
                            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return content.strip() if isinstance(content, str) else ""


def _candidates(vault: Vault, job: dict) -> dict:
    from .search import Searcher   # Only the detached process loads the search engine and NumPy.

    prior = ""
    if job["boundary"] and harness.get("claude").claims(job["sid"], job["transcript"]):
        try:
            prior = _previous_reply(job["transcript"], job["boundary"], job["sid"])
        except OSError:
            pass
    searcher = Searcher(vault)
    if not searcher.idx.complete or searcher.broken:
        return {"status": "incomplete_index"}
    local = routing.bindings(vault, job["session"])
    query = job["prompt"]
    plain = [r["id"] for r in searcher.work_search(query, 8, local)]
    paired = (query + "\n" + prior)[:2000]
    with_reply = [r["id"] for r in searcher.work_search(paired, 8, local)] if prior and paired != query else None
    return {"status": "measured", "prompt_ids": plain, "prompt_reply_ids": with_reply}


def observe(vault: Vault, job: dict) -> None:
    cursor = conversation_state.state_path(vault, "claude", job["sid"])
    turn = int(job["turn"])
    record = cursor.with_suffix(".recall-shadow") / f"{turn}.json"
    started = time.time_ns()
    clock = time.perf_counter()
    try:
        with core.local_lock_path("osk-recall-shadow.lock", vault.root).open("a+b") as lock:
            try:
                lock_exclusive(lock, blocking=False)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                result = {"status": "busy"}
            else:
                result = _candidates(vault, job)
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
        result = {"status": "failed", "error": type(exc).__name__}
    _save(record, {"turn": turn, "requested_ns": job["requested_ns"], "started_ns": started,
                   "elapsed_ms": round((time.perf_counter() - clock) * 1000), **result})


def report(vault: Vault, sid: str) -> list[dict]:
    folder = conversation_state.state_path(vault, "claude", sid).with_suffix(".recall-shadow")
    files = (p for p in folder.glob("*.json") if p.stem.isdecimal())
    return [json.loads(p.read_bytes()) for p in sorted(files, key=lambda p: int(p.stem))]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", metavar="CLAUDE_SESSION_ID", help="show this conversation's local samples")
    args = parser.parse_args()
    if args.report:
        print(json.dumps(report(Vault.open(), args.report), indent=2))
    elif os.environ.get("OSK_RECALL_SHADOW") == "1":
        request = json.load(sys.stdin.buffer)
        observe(Vault.open(), request)
    else:
        parser.error("use --report CLAUDE_SESSION_ID to inspect a measurement")
