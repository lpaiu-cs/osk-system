#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""osk 턴 계기 — 하네스 트랜스크립트에서 표면의 비용과 효용을 잰다.

사료 `2026-09-02-anchor-batch-scope-memory`·`2026-09-02-eviction-ledger`가
"전부 `osk_turn_ledger.py`가 잰다"고 적은 그 계기다. 이슈 #20의 측정법을 도구로
굳힌 것이며, 판정 지표(통합 단독률·상한 초과 재시도·거부 에피소드 중 증류·
잘린 분량)와 §9-3의 지표(미처분 처분율·나이)를 같은 자리에서 낸다.

읽는 것은 **vault 밖** — `~/.claude/projects/*/*.jsonl`(Claude Code 트랜스크립트)
이고, 대장(`_ledger/evictions.jsonl`)은 있으면 함께 읽는다. vault의 노드·기억은
읽지 않는다. 출력은 집계뿐이다 — 어떤 본문도 싣지 않는다(트랜스크립트에는
사용자 대화가 있다).

## 과금 항등식

트랜스크립트가 요청마다 `usage` 전체를 남기므로 결과 페이로드의 비용을
추정이 아니라 **차이**로 잰다:

    prompt(n)   = input + cache_creation_input + cache_read_input
    payload(n)  = prompt(n+1) - prompt(n) - output(n)

즉 n번째 응답이 낸 도구 결과가 다음 요청의 프롬프트를 얼마나 키웠는가다. 이
값은 결과 문자수와 교차 검증하는 게이트(chars/token이 1.0~3.0)를 지나야 싣는다
— 압축·주입이 끼면 차이가 오염되므로, 통과율을 함께 낸다.

## 계측 함정 셋 (재현하려면 반드시 피해야 한다 — #20)

1. `mcp__osk-system__`로 grep하면 오탐이 지배한다. 세션 시작에 실리는
   deferred tools 목록에 도구 이름이 전부 있다. **`tool_use` 블록만** 센다.
2. 한 과금 메시지가 여러 레코드로 쪼개져 `usage`를 공유한다(`apiBlockIndex`).
   **`message.id`로 접는다** — 안 접으면 건수가 부풀고 델타가 오염된다.
3. 세션 재개가 이전 이력을 새 파일로 복사한다 — 한 호출이 여러 파일에 남는다.
   **`tool_use_id`로 전역 중복 제거**한다(실측: 899 고유 대 1,087 사본).

그리고 `is_error`는 실패를 못 잡는다 — osk는 거부를 MCP 오류가 아니라 성공
응답 본문의 `"ok": false`로 돌려준다. `ok`를 본다.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "mcp__osk-system__"
OVERFLOW_RE = re.compile(r"(\d+)자로 상한 (\d+)자를 (\d+)자 넘는다")
OK_RE = re.compile(r'"ok"\s*:\s*(true|false)')
CHARS_RE = re.compile(r'"chars"\s*:\s*(\d+)')
GATE = (1.0, 3.0)          # chars/token — 이 밖이면 델타가 오염된 것으로 보고 뺀다
DISTILL_TOOLS = {"create_node", "update_node"}


# ── 판독 ─────────────────────────────────────────────────────────────────

def _text_of(content) -> str:
    """tool_result의 content — str이거나 [{type:'text', text}] 목록."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    return ""


def _ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def read_corpus(projects: Path, exclude: list[str]) -> dict:
    """트랜스크립트 전수를 읽어 (과금 메시지, 도구 호출, 결과)를 접는다.

    반환:
      msgs:  {(session, message.id): {ts, usage, tool_uses:[id…], n_tools}}
      uses:  {tool_use_id: {name, session, msg, ts, file, project, input_len}}
      results: {tool_use_id: {text_len, ok, is_error, overflow:(n,limit,over)|None, chars}}
    """
    msgs: dict = {}
    uses: dict = {}
    results: dict = {}
    files_scanned = 0
    for f in sorted(projects.glob("*/*.jsonl")):
        proj = f.parent.name
        if any(x in proj for x in exclude):
            continue
        files_scanned += 1
        try:
            fh = f.open(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                # 값싼 사전 걸러내기 — 과금 메시지·도구 결과·osk 호출만 판독한다
                if '"usage"' not in line and "tool_result" not in line and PREFIX not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                sess = r.get("sessionId") or f.stem
                m = r.get("message") or {}
                c = m.get("content")
                if r.get("type") == "assistant" and isinstance(m.get("usage"), dict) and m.get("id"):
                    key = (sess, m["id"])
                    ent = msgs.get(key)
                    if ent is None:                 # 함정 2 — 첫 레코드의 usage만
                        ent = msgs[key] = {"ts": _ts(r.get("timestamp")),
                                           "usage": m["usage"], "tool_uses": [],
                                           "n_tools": 0, "blocks": set()}
                    if isinstance(c, list):
                        for blk in c:
                            if blk.get("type") != "tool_use":   # 함정 1 — text 블록은 호출이 아니다
                                continue
                            # 같은 메시지의 같은 블록을 재개 사본이 다시 보여 준다 —
                            # 두 번 세면 도구 하나인 메시지가 둘로 보여 단독 판정과
                            # 페이로드 귀속이 깨진다(첫 판의 실측).
                            bid = blk.get("id")
                            if bid in ent["blocks"]:
                                continue
                            ent["blocks"].add(bid)
                            ent["n_tools"] += 1
                            name = str(blk.get("name", ""))
                            if not name.startswith(PREFIX):
                                continue
                            tid = blk.get("id")
                            if tid in uses:                      # 함정 3 — 재개 사본
                                continue
                            uses[tid] = {"name": name[len(PREFIX):], "session": sess,
                                         "msg": key, "ts": ent["ts"], "file": f.name,
                                         "project": proj,
                                         "input_len": len(json.dumps(blk.get("input", {}), ensure_ascii=False))}
                            ent["tool_uses"].append(tid)
                elif isinstance(c, list):
                    for blk in c:
                        if blk.get("type") != "tool_result":
                            continue
                        tid = blk.get("tool_use_id")
                        if tid in results or tid is None:
                            continue
                        txt = _text_of(blk.get("content"))
                        okm = OK_RE.search(txt)
                        ov = OVERFLOW_RE.search(txt)
                        chm = CHARS_RE.search(txt)
                        results[tid] = {"text_len": len(txt),
                                        "ok": (okm.group(1) == "true") if okm else None,
                                        "is_error": bool(blk.get("is_error")),
                                        "overflow": tuple(int(g) for g in ov.groups()) if ov else None,
                                        "chars": int(chm.group(1)) if chm else None}
    return {"msgs": msgs, "uses": uses, "results": results, "files": files_scanned}


# ── 과금 항등식 ─────────────────────────────────────────────────────────────

def _prompt(u: dict) -> int:
    return int(u.get("input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0) \
        + int(u.get("cache_read_input_tokens", 0) or 0)


def attribute_costs(corpus: dict) -> None:
    """각 osk 호출에 발화 비용(output_tokens)과 페이로드 비용(다음 프롬프트의
    증분)을 붙인다. 페이로드는 그 메시지의 도구 호출이 **하나**일 때만 그 호출에
    귀속한다 — 여럿이면 어느 결과가 프롬프트를 키웠는지 가를 수 없다."""
    by_sess: dict = defaultdict(list)
    for key, ent in corpus["msgs"].items():
        by_sess[key[0]].append((ent["ts"], key, ent))
    for sess, lst in by_sess.items():
        lst.sort(key=lambda x: x[0])
        for i, (_ts_, key, ent) in enumerate(lst):
            u = ent["usage"]
            out = int(u.get("output_tokens", 0) or 0)
            nxt = lst[i + 1][2]["usage"] if i + 1 < len(lst) else None
            payload = (_prompt(nxt) - _prompt(u) - out) if nxt else None
            for tid in ent["tool_uses"]:
                use = corpus["uses"][tid]
                use["output_tokens"] = out
                use["alone"] = ent["n_tools"] == 1
                res = corpus["results"].get(tid)
                if payload is not None and ent["n_tools"] == 1 and res and res["text_len"] > 0:
                    ratio = res["text_len"] / payload if payload > 0 else None
                    use["payload_tokens"] = payload
                    use["ratio"] = ratio
                    use["gated"] = bool(ratio and GATE[0] <= ratio <= GATE[1])
                else:
                    use["payload_tokens"] = None
                    use["ratio"] = None
                    use["gated"] = False


# ── scope 기억 에피소드 ───────────────────────────────────────────────────

def episodes(corpus: dict) -> list[dict]:
    """세션별 scope_memory 호출 순서에서 '첫 상한 초과 거부 ~ 다음 성공'을 한
    에피소드로 묶고, 그 사이에 증류(create_node/update_node 성공)가 있었는지
    본다. 세션이 끝날 때까지 성공이 없으면 미해결이다."""
    by_sess: dict = defaultdict(list)
    for tid, use in corpus["uses"].items():
        by_sess[use["session"]].append((use["ts"], tid))
    out = []
    for sess, calls in by_sess.items():
        calls.sort()
        cur = None
        for ts, tid in calls:
            use, res = corpus["uses"][tid], corpus["results"].get(tid)
            if res is None:
                continue
            name = use["name"]
            if name == "scope_memory" and res["overflow"]:
                if cur is None:
                    cur = {"session": sess, "start": ts, "rejections": 0,
                           "attempted": res["overflow"][0], "distill": 0,
                           "search": 0, "tokens": 0, "over": res["overflow"][2]}
                cur["rejections"] += 1
                cur["tokens"] += (use.get("payload_tokens") or 0) + (use.get("output_tokens") or 0)
            elif cur is not None:
                if name in DISTILL_TOOLS and res["ok"]:
                    cur["distill"] += 1
                elif name == "search":
                    cur["search"] += 1
                elif name == "scope_memory" and res["ok"]:
                    cur["end"] = ts
                    cur["accepted"] = res["chars"]
                    cur["trimmed"] = (cur["attempted"] - res["chars"]) if res["chars"] is not None else None
                    cur["outcome"] = "distill" if cur["distill"] else "trim"
                    out.append(cur)
                    cur = None
        if cur is not None:
            cur["outcome"] = "unresolved"
            out.append(cur)
    return out


# ── 퇴출 기록부 (§9-2 12항 · §9-3) ─────────────────────────────────────────

def evictions(ledger: Path | None, now: float) -> dict | None:
    if not ledger or not ledger.is_file():
        return None
    evicts, settled = {}, set()
    with ledger.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "evict" and r.get("rid"):
                evicts[r["rid"]] = _ts(r.get("at")) or 0.0
            elif r.get("kind") == "settle" and r.get("of"):
                settled.add(r["of"])
    open_ = {k: v for k, v in evicts.items() if k not in settled}
    ages = sorted((now - v) / 86400 for v in open_.values() if v)
    return {"evict": len(evicts), "settled": len(settled & set(evicts)),
            "open": len(open_),
            "settle_rate": (len(settled & set(evicts)) / len(evicts)) if evicts else None,
            "oldest_days": max(ages) if ages else None,
            "age_p50_days": statistics.median(ages) if ages else None}


# ── 집계 ─────────────────────────────────────────────────────────────────

def _failed(r: dict | None) -> bool:
    """실패 판정 — `"ok": false`(osk의 거부) 또는 MCP 오류. 한 곳에서만 정한다:
    같은 식을 두 곳에 두면 한쪽만 고쳐져 집계가 갈린다."""
    return bool(r and (r["ok"] is False or r["is_error"]))


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(corpus: dict, since: float, until: float, ledger: Path | None) -> dict:
    uses = {t: u for t, u in corpus["uses"].items() if since <= u["ts"] <= until}
    res = corpus["results"]
    sessions = {u["session"] for u in uses.values()}
    per_tool: dict = defaultdict(lambda: {"calls": 0, "fail": 0, "out_tokens": [],
                                          "payload_tokens": [], "ratios": [], "gated": 0, "alone": 0})
    total_tokens = fail_tokens = 0
    for tid, u in uses.items():
        r = res.get(tid)
        t = per_tool[u["name"]]
        t["calls"] += 1
        failed = _failed(r)
        t["fail"] += failed
        t["out_tokens"].append(u.get("output_tokens") or 0)
        t["alone"] += bool(u.get("alone"))
        cost = (u.get("output_tokens") or 0) + (u.get("payload_tokens") or 0 if u.get("gated") else 0)
        total_tokens += cost
        fail_tokens += cost if failed else 0
        if u.get("gated"):
            t["gated"] += 1
            t["payload_tokens"].append(u["payload_tokens"])
            t["ratios"].append(u["ratio"])
    tools = {}
    for name, t in sorted(per_tool.items(), key=lambda x: -x[1]["calls"]):
        tools[name] = {"calls": t["calls"], "fail": t["fail"],
                       "out_tokens_mean": round(statistics.mean(t["out_tokens"]), 1) if t["out_tokens"] else None,
                       "payload_tokens_mean": round(statistics.mean(t["payload_tokens"]), 1) if t["payload_tokens"] else None,
                       "gate_pass": t["gated"],
                       "chars_per_token": (round(min(t["ratios"]), 2), round(max(t["ratios"]), 2)) if t["ratios"] else None,
                       "alone_rate": round(t["alone"] / t["calls"], 3) if t["calls"] else None}
    # scope 기억
    sm = [(t, u) for t, u in uses.items() if u["name"] == "scope_memory"]
    overs = [res[t]["overflow"] for t, _u in sm if res.get(t) and res[t]["overflow"]]
    over_amt = [o[2] for o in overs]
    over_tokens = sum(((u.get("payload_tokens") or 0) if u.get("gated") else 0) + (u.get("output_tokens") or 0)
                      for t, u in sm if res.get(t) and res[t]["overflow"])
    eps = [e for e in episodes(corpus) if since <= e["start"] <= until]
    resolved = [e for e in eps if e["outcome"] != "unresolved"]
    multi = [e for e in eps if e["rejections"] >= 2]
    trimmed = [e["trimmed"] for e in resolved if e.get("trimmed") is not None and e["outcome"] == "trim"]
    fails = sum(1 for t in uses if _failed(res.get(t)))
    retry_after_fail = 0
    by_sess: dict = defaultdict(list)
    for t, u in uses.items():
        by_sess[u["session"]].append((u["ts"], t))
    for lst in by_sess.values():
        lst.sort()
        for i in range(len(lst) - 1):
            a, b = lst[i][1], lst[i + 1][1]
            if _failed(res.get(a)) and uses[a]["name"] == uses[b]["name"]:
                retry_after_fail += 1
    return {
        "window": [datetime.fromtimestamp(since, timezone.utc).date().isoformat(),
                   datetime.fromtimestamp(until, timezone.utc).date().isoformat()],
        "files": corpus["files"], "sessions": len(sessions), "calls": len(uses),
        "failures": fails, "failure_rate": round(fails / len(uses), 3) if uses else None,
        "retry_after_fail": retry_after_fail,
        "tokens_total": total_tokens, "tokens_failed": fail_tokens,
        "tools": tools,
        "scope_memory": {
            "calls": len(sm),
            "alone_rate": round(sum(1 for _t, u in sm if u.get("alone")) / len(sm), 3) if sm else None,
            "overflow_rejections": len(overs), "overflow_tokens": over_tokens,
            "overflow_share_of_cost": round(over_tokens / total_tokens, 3) if total_tokens else None,
            "over_p25": _pct(over_amt, .25), "over_p50": _pct(over_amt, .5), "over_p75": _pct(over_amt, .75),
            "over_max": max(over_amt) if over_amt else None,
            "over_le100_share": round(sum(1 for x in over_amt if x <= 100) / len(over_amt), 3) if over_amt else None,
            "episodes": len(eps),
            "episodes_trim": sum(1 for e in resolved if e["outcome"] == "trim"),
            "episodes_distill": sum(1 for e in resolved if e["outcome"] == "distill"),
            "episodes_unresolved": len(eps) - len(resolved),
            "distill_rate": round(sum(1 for e in resolved if e["outcome"] == "distill") / len(resolved), 3) if resolved else None,
            "multi_rejection_episodes": len(multi),
            "multi_rejection_with_distill": sum(1 for e in multi if e["distill"]),
            "multi_rejection_with_search": sum(1 for e in multi if e["search"]),
            "trimmed_chars_total": sum(trimmed), "trimmed_chars_median": statistics.median(trimmed) if trimmed else None,
        },
        "evictions": evictions(ledger, until),
    }


def render(s: dict) -> str:
    L = []
    w = s["window"]
    L.append(f"osk 턴 계기 — {w[0]} ~ {w[1]} · 파일 {s['files']} · 세션 {s['sessions']} · 호출 {s['calls']}")
    L.append(f"실패 {s['failures']} ({(s['failure_rate'] or 0)*100:.1f}%) · 실패 직후 같은 도구 재호출 {s['retry_after_fail']}"
             f" · 토큰 합 {s['tokens_total']:,} (실패분 {s['tokens_failed']:,})"
             f" — 합은 발화 전부 + 게이트 통과 페이로드")
    L.append("")
    L.append(f"{'도구':<18}{'호출':>6}{'실패':>6}{'발화tok':>9}{'페이로드tok':>11}{'게이트':>7}{'chars/tok':>12}{'단독률':>8}")
    for name, t in s["tools"].items():
        cpt = f"{t['chars_per_token'][0]}~{t['chars_per_token'][1]}" if t["chars_per_token"] else "-"
        L.append(f"{name:<18}{t['calls']:>6}{t['fail']:>6}{(t['out_tokens_mean'] or 0):>9.0f}"
                 f"{(t['payload_tokens_mean'] or 0):>11.0f}{t['gate_pass']:>7}{cpt:>12}{(t['alone_rate'] or 0):>8.2f}")
    m = s["scope_memory"]
    L.append("")
    L.append("scope 기억")
    L.append(f"  호출 {m['calls']} · 통합 단독률 {(m['alone_rate'] or 0)*100:.0f}%")
    L.append(f"  상한 초과 거부 {m['overflow_rejections']} · {m['overflow_tokens']:,}tok"
             f" (비용의 {(m['overflow_share_of_cost'] or 0)*100:.1f}%)"
             f" · 초과폭 p25/p50/p75 {m['over_p25']}/{m['over_p50']}/{m['over_p75']} · ≤100자 {(m['over_le100_share'] or 0)*100:.0f}%")
    L.append(f"  에피소드 {m['episodes']}: 잘라서 통과 {m['episodes_trim']} · 증류 {m['episodes_distill']}"
             f" · 미해결 {m['episodes_unresolved']} → 증류율 {(m['distill_rate'] or 0)*100:.0f}%")
    L.append(f"  2회 이상 거부 {m['multi_rejection_episodes']} (그중 증류 {m['multi_rejection_with_distill']}"
             f", search 호출 {m['multi_rejection_with_search']})")
    L.append(f"  증류 없이 잘린 분량 {m['trimmed_chars_total']:,}자 (에피소드 중앙값 {m['trimmed_chars_median']})")
    e = s["evictions"]
    L.append("")
    if e is None:
        L.append("퇴출 기록부: 없음 (§9-2 12항 — 엔진이 붙으면 여기 실린다)")
    else:
        L.append(f"퇴출 기록부: evict {e['evict']} · 처분 {e['settled']} ({(e['settle_rate'] or 0)*100:.0f}%)"
                 f" · 미처분 {e['open']} · 가장 오래된 {e['oldest_days'] and round(e['oldest_days'],1)}일 · 나이 중앙값 {e['age_p50_days'] and round(e['age_p50_days'],1)}일")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="osk 턴 계기 — 트랜스크립트에서 표면 비용·효용 집계(본문은 싣지 않는다)")
    ap.add_argument("--projects", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--since", help="YYYY-MM-DD (UTC, 포함)")
    ap.add_argument("--until", help="YYYY-MM-DD (UTC, 포함)")
    ap.add_argument("--exclude-project", action="append", default=[],
                    help="프로젝트 디렉터리 이름에 이 문자열이 들면 제외 — 감사 세션의 자기 오염을 뺄 때")
    ap.add_argument("--ledger", help="_ledger/evictions.jsonl 경로 (있으면 §9-3 지표)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    since = _ts(a.since + "T00:00:00Z") if a.since else 0.0
    until = _ts(a.until + "T23:59:59Z") if a.until else datetime.now(timezone.utc).timestamp()
    corpus = read_corpus(Path(a.projects), a.exclude_project)
    attribute_costs(corpus)
    s = summarize(corpus, since, until, Path(a.ledger) if a.ledger else None)
    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print(render(s))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
