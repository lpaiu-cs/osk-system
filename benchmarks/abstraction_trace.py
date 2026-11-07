"""Preserve CLI rollout evidence; audit identical successful reads, not semantic errors."""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil


def rows(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue  # A killed worker can leave one incomplete trailing event.


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_trace(out, home, started):
    events = list(rows(out / "events.jsonl"))
    tid = next((r.get("thread_id") for r in events if r.get("type") == "thread.started"), None)
    candidates = []
    first = datetime.fromisoformat(started).date() - timedelta(days=1)
    last = datetime.now().date() + timedelta(days=1)
    while first <= last and tid:
        candidates.extend((home / "sessions" / first.strftime("%Y/%m/%d")).glob(f"*{tid}*.jsonl"))
        first += timedelta(days=1)
    status = {"thread_id": tid, "status": "missing", "compaction_count": None}
    if len(candidates) == 1:
        shutil.copyfile(candidates[0], out / "rollout.jsonl")
        trace = list(rows(out / "rollout.jsonl"))
        compact = [{"line": i + 1, "timestamp": r.get("timestamp"), "type": r.get("type"),
                    "event_type": r.get("payload", {}).get("type")}
                   for i, r in enumerate(trace) if r.get("type") == "compacted"
                   or r.get("payload", {}).get("type") == "context_compacted"]
        # Keep per-call token/window telemetry; cumulative usage cannot establish context pressure.
        usage = [{"line": i + 1, "timestamp": r.get("timestamp"), "info": r["payload"].get("info")}
                 for i, r in enumerate(trace) if r.get("payload", {}).get("type") == "token_count"]
        count = sum(r["type"] == "compacted" for r in compact)
        status.update(status="captured", compaction_count=count or len(compact),
                      events=compact, token_usage=usage)
    elif len(candidates) > 1:
        status["status"] = "ambiguous"
    save(out / "compaction.json", status)
    return status


def audit_run(out):
    seen, calls, repeats = {}, [], []
    prompt = (out / "prompt.txt").read_text(encoding="utf-8")
    # A transparent text-volume proxy, NOT a token count or a context-limit measurement.
    volume = len(prompt.encode("utf-8"))
    epoch = 0
    for event in rows(out / "events.jsonl"):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        if item.get("type") != "mcp_tool_call":
            volume += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            continue
        tool, args, result = item.get("tool"), item.get("arguments", {}), item.get("result")
        serial = json.dumps(result, ensure_ascii=False, sort_keys=True)
        bad = bool(item.get("error")) or (isinstance(result, dict) and result.get("isError"))
        n = len(calls)
        key = (tool, json.dumps(args, ensure_ascii=False, sort_keys=True))
        digest = hashlib.sha256(serial.encode()).hexdigest()
        if tool in {"read_node", "read_cited"} and not bad:
            prev = seen.get(key)
            if prev and prev["digest"] == digest:
                repeats.append({"item": item.get("id"), "previous_item": prev["item"],
                                "tool": tool, "arguments": args,
                                "consecutive": prev["call"] == n - 1,
                                "no_intervening_write": prev["epoch"] == epoch,
                                "response_utf8_bytes": len(serial.encode()),
                                "visible_utf8_bytes_before": volume})
            seen[key] = {"digest": digest, "item": item.get("id"), "call": n, "epoch": epoch}
        # Conservative: any intervening write can justify a refreshed read, even failed writes.
        if tool not in {"read_node", "read_cited", "overview", "search", "query", "verify_abstraction"}:
            epoch += 1
        calls.append(tool)
        volume += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
    done = json.loads((out / "done.json").read_text(encoding="utf-8"))
    return {"run": out.name, "model": done["model"], "calls": len(calls),
            "read_calls": sum(t in {"read_node", "read_cited"} for t in calls),
            "prompt_chars": len(prompt), "identical_repeats": repeats}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("study", type=Path)
    p.add_argument("output", type=Path)
    a = p.parse_args()
    data = [audit_run(d) for d in sorted((a.study / "runs").iterdir()) if (d / "done.json").exists()]
    totals = {}
    for model in sorted({d["model"] for d in data}):
        group = [d for d in data if d["model"] == model]
        totals[model] = {"runs": len(group), "read_calls": sum(d["read_calls"] for d in group)}
        for label, pred in [("identical", lambda r: True),
                            ("no_write", lambda r: r["no_intervening_write"]),
                            ("consecutive", lambda r: r["consecutive"])]:
            hits = [(d, [r for r in d["identical_repeats"] if pred(r)]) for d in group]
            totals[model][label] = {"runs": sum(bool(rs) for _, rs in hits),
                                    "calls": sum(len(rs) for _, rs in hits),
                                    "ids": [d["run"].split("_")[0] for d, rs in hits if rs]}
    save(a.output, {"definitions": "Same tool, exact arguments, identical successful result. No-write excludes any intervening mutation; consecutive means adjacent tool calls. Neither proves wasted reasoning or absent compaction.",
                    "summary": totals, "runs": data})
    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
