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
— 압축·주입이 끼면 차이가 오염되므로, 통과율을 함께 낸다. 발화 비용
(`output_tokens`)은 **메시지의 값**이라 메시지당 한 번만 센다 — **osk만 있는**
메시지의 호출 k개에 1/k씩 나눠 붙여, 도구별 합과 전체 합이 같은 수를 가리킨다.
비-osk 도구가 섞인 메시지는 발화를 주지 않고 그 호출 수를 따로 낸다(아래 §발화).

대장(`_ledger/*.jsonl`) 행의 시각은 **`rid`(UUIDv7)에서** 읽는다 — §3 1항이 모든
행에 요구하는 유일한 시각 근거이고, `at`은 `settle`의 계약 필드가 아니다.

## 세 집계를 분리한다 — 고유 호출 · 경로 · 대화

같은 호출이 여러 파일에 남는다(재개가 이력을 복사한다). 셋을 따로 둔다:

- **건수**는 `tool_use_id`로 전역 고유하게 센다.
- **순서 분석**(에피소드·페이로드 귀속·재시도)은 **세션 자신의 경로** 위에서 한다 —
  세션 파일은 복사된 접두부와 자기 연속을 담은 하나의 선형 역사다. 세션들을 한
  선으로 평탄화하면 공통 접두를 공유하는 **형제 분기**(같은 원 세션에서 갈라진
  두 재개)의 호출이 시각순으로 섞여, A의 거부가 B의 증류 때문에 `distill`로
  판정된다(리뷰 지적). 공통 접두는 부모-자식 연속을 증명할 뿐 형제 사이의 순서를
  증명하지 않는다. 그래서 경로마다 따로 판정하고, 같은 거부(같은 `tool_use_id`)에서
  시작한 에피소드는 **가장 먼저 닫힌 경로의 것 하나만** 센다. 페이로드는 반대다 —
  분기점에서는 두 재개가 각각 요청을 보내 그 결과가 **각각 실려 각각 청구되므로**,
  후속마다 증분을 재고 게이트를 따로 걸어 통과한 것들을 **합한다**(실측: 분기점 50).
- **대화 수**는 `message.id`를 공유하는 세션들의 연결 성분으로 센다 — 재개·분기는
  모두 한 대화다(실측: 사본 537 중 411이 다른 sessionId 아래, message.id는 같다).
  **세션 id 수는 싣지 않는다** — 뒤에 오는 재개가 옛 메시지를 복사하며 그 수를
  바꾸는데, 세션이 언제 생겼는지는 자료에 없다(`window` 주석).

## 창은 먼저 자른다

`--since/--until`은 파생 상태(에피소드·페이로드·대장 나이)를 계산하기 **전에**
입력을 자른다. 창 뒤의 성공이 창 안 거부를 닫으면 같은 과거 창을 나중에 다시
돌렸을 때 결과가 바뀐다 — 창의 마지막 시점에 존재한 자료만으로 계산해야 같은
창은 언제 돌려도 같다(리뷰 지적). 자르는 기준은 **메시지의 시각 하나**다: 그것이
자료에 있는 유일한 실제 생성 시각이고 뒤에 무엇이 추가돼도 변하지 않는다.

## 계측 함정 셋 (재현하려면 반드시 피해야 한다 — #20)

1. `mcp__osk-system__`로 grep하면 오탐이 지배한다. 세션 시작에 실리는
   deferred tools 목록에 도구 이름이 전부 있다. **`tool_use` 블록만** 센다.
2. 한 과금 메시지가 블록마다 레코드로 쪼개져 `usage`를 공유한다(`apiBlockIndex`).
   **`message.id`로 접는다** — 안 접으면 건수가 부풀고 델타가 오염된다.
3. 세션 재개가 이전 이력을 새 파일로 복사한다 — 한 호출이 여러 파일에 남는다.
   **`tool_use_id`로 전역 중복 제거**하되 경로는 세션마다 보존한다.

그리고 `is_error`는 실패를 못 잡는다 — osk는 거부를 MCP 오류가 아니라 성공
응답 본문의 `"ok": false`로 돌려준다. `ok`를 본다.
"""
from __future__ import annotations
import argparse
import json
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
INF = float("inf")


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


def _row_time(r: dict) -> float | None:
    """대장 행의 시각 — **`rid`에서 읽는다.**

    Mechanism §3 1항이 모든 `_ledger/` 행에 UUIDv7 `rid`를 요구하고, 그 앞 48비트가
    Unix ms이며 **잠금 안에서 단조 생성**된다. `at`으로 자르면 안 되는 이유: `at`은
    `evict`의 계약 필드일 뿐 `settle`의 계약(§9-2 12항)은 `of`·`outcome`·`target`
    이라, 계약에 맞는 `settle`이 시각 없이 들어와 **과거 창의 처분 상태를 바꾼다**
    (리뷰 지적). rid는 모든 행에 있고 뒤에 오는 기록이 앞의 rid를 바꾸지 못한다.
    형식이 어긋난 행(손상)만 `at`으로 물러선다(실측: 실 대장 22/22행이 rid에서
    시각을 내고 파일 순서로 단조였다)."""
    h = str(r.get("rid", "")).replace("-", "")
    if len(h) == 32 and h[12] == "7":
        try:
            return int(h[:12], 16) / 1000.0
        except ValueError:
            pass
    return _ts(r.get("at")) if r.get("at") else None


def _failed(r: dict | None) -> bool:
    """실패 판정 — `"ok": false`(osk의 거부) 또는 MCP 오류. 한 곳에서만 정한다:
    같은 식을 두 곳에 두면 한쪽만 고쳐져 집계가 갈린다."""
    return bool(r and (r["ok"] is False or r["is_error"]))


def read_corpus(projects: Path, exclude: list[str]) -> dict:
    """트랜스크립트 전수를 읽어 (과금 메시지, 도구 호출, 결과)를 접는다.

    반환:
      msgs:    {message.id: {ts, usage, sessions:set, tool_uses:[tid…], blocks:set}}
               — 같은 message.id는 어느 세션·파일에서 오든 **하나**다(함정 2·3)
      uses:    {tool_use_id: {name, msg, ts, first_file, first_session, input_len}}
      results: {tool_use_id: {text_len, ok, is_error, overflow, chars}}
      files:   읽은 파일 수
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
                    mid = m["id"]
                    ent = msgs.get(mid)
                    if ent is None:                 # 함정 2·3 — 첫 레코드의 usage만
                        ent = msgs[mid] = {"ts": _ts(r.get("timestamp")), "usage": m["usage"],
                                           "sessions": set(), "tool_uses": [], "blocks": set()}
                    ent["sessions"].add(sess)
                    if isinstance(c, list):
                        for blk in c:
                            if blk.get("type") != "tool_use":   # 함정 1 — text 블록은 호출이 아니다
                                continue
                            # 같은 메시지의 같은 블록을 사본이 다시 보여 준다 — 한 번만 센다.
                            bid = blk.get("id")
                            if bid in ent["blocks"]:
                                continue
                            ent["blocks"].add(bid)
                            name = str(blk.get("name", ""))
                            if not name.startswith(PREFIX):
                                continue
                            if bid in uses:                      # 함정 3 — 다른 message.id 아래의 사본
                                continue
                            uses[bid] = {"name": name[len(PREFIX):], "msg": mid, "ts": ent["ts"],
                                         "first_file": f.name, "first_session": sess, "project": proj,
                                         "input_len": len(json.dumps(blk.get("input", {}), ensure_ascii=False))}
                            ent["tool_uses"].append(bid)
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


# ── 세션·대화·창 ─────────────────────────────────────────────────────────

def conversations(corpus: dict) -> dict:
    """세션 → 대화 id. 같은 `message.id`를 공유하는 세션은 한 대화다(재개·분기).
    **세기 위한 것**이지 순서의 근거가 아니다 — 순서는 `session_paths`가 준다.

    이 셈은 뒤에 오는 사본에 흔들리지 않는다: 나중의 재개는 원 세션의 메시지를
    복사하므로 이미 있는 성분에 합쳐질 뿐 성분 수를 바꾸지 않고, 창 뒤에 시작한
    새 대화의 메시지는 시각으로 걸러진다. 세션 id 수는 그렇지 않아 싣지 않는다
    (`window` 주석)."""
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for ent in corpus["msgs"].values():
        ss = sorted(ent["sessions"])
        for s in ss:
            find(s)
        for a, b in zip(ss, ss[1:]):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)      # 결정적 — 이름이 작은 쪽이 대표
    return {s: find(s) for s in list(parent)}


def session_paths(corpus: dict) -> dict:
    """세션 → 그 세션이 담은 메시지 id의 시각순 목록. 복사된 접두부까지 포함한
    **그 세션의 선형 역사**다. 형제 분기는 접두부만 공유하고 그 뒤는 각자다."""
    paths: dict = defaultdict(list)
    for mid, ent in corpus["msgs"].items():
        for sess in ent["sessions"]:
            paths[sess].append((ent["ts"], mid))
    return {s: [m for _t, m in sorted(v)] for s, v in paths.items()}


def window(corpus: dict, until: float) -> dict:
    """창의 끝 시점에 존재한 자료만 남긴다 — 파생 상태는 이 위에서 계산한다.

    자르는 기준은 **메시지의 시각 하나**다. 그것이 자료에 있는 유일한 실제
    생성 시각이고, 뒤에 무엇이 추가돼도 변하지 않는다. 세션의 '탄생'으로도
    잘라 봤으나 물렸다: 세션이 언제 생겼는지를 적은 자리가 트랜스크립트에
    없어(계보 필드 부재, `cost-state.startTime`은 184 세션 중 1) 코퍼스에서
    추정할 수밖에 없는데, "그 세션에만 있는 메시지"라는 추정은 나중의 재개가
    그 메시지를 복사하는 순간 무너진다(실측: 고유 메시지가 없는 세션 31/184).
    그러면 **자기 시각으로는 창 안인 메시지가 통째로 사라져** 호출·토큰·
    에피소드까지 과거 실행과 달라진다 — 세션 수 하나를 지키려다 자료를 잃는다.

    그래서 세션으로는 자르지 않고, 대신 **불안정한 세션 id 수를 싣지 않는다**.
    창 뒤의 재개가 옛 메시지를 복사해도 메시지 집합은 그대로이고(같은
    `message.id`), 늘어나는 것은 그 메시지의 `sessions` 집합뿐이다. 경로가
    하나 늘지만 그것은 부모 경로의 접두이므로 에피소드(같은 거부는 먼저 닫힌
    것 하나)·재시도(쌍의 집합)·호출(tool_use_id)에 아무것도 더하지 않는다."""
    msgs = {k: v for k, v in corpus["msgs"].items() if v["ts"] <= until}
    uses = {t: dict(u) for t, u in corpus["uses"].items() if u["msg"] in msgs}
    results = {t: r for t, r in corpus["results"].items() if t in uses}
    return {"msgs": msgs, "uses": uses, "results": results, "files": corpus["files"]}


# ── 과금 항등식 ─────────────────────────────────────────────────────────────

def _prompt(u: dict) -> int:
    return int(u.get("input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0) \
        + int(u.get("cache_read_input_tokens", 0) or 0)


def attribute_costs(corpus: dict) -> None:
    """각 osk 호출에 발화 비용과 페이로드 비용을 붙인다.

    - 발화(`output_tokens`)는 메시지의 값이다 — 그 메시지의 osk 호출 k개에 1/k씩
      나눈다(`out_share`). 도구별 합과 전체 합이 같은 수를 가리키게 하기 위해서다.
      **osk만 있는 메시지에서만** 나눈다: 한 메시지에 `scope_memory`와 Bash가 함께
      있으면 메시지 전체의 발화가 osk 몫으로 들어가 분모·분자가 다 부푼다(실측:
      혼합 메시지 50/728·호출 56/905·발화 119,988tok). #20의 손측정도 osk만 있는
      메시지에서 발화를 읽었다. 혼합 메시지의 호출은 발화를 얻지 못하며 그 수를
      `speech_unattributed_calls`로 함께 낸다 — 덜 세는 것을 숨기지 않는다.
    - 페이로드(다음 프롬프트의 증분)는 그 메시지의 도구 호출이 **하나**일 때만 그
      호출에 귀속한다 — 여럿이면 어느 결과가 프롬프트를 키웠는지 가를 수 없다.

    후속은 **하나가 아니다.** 분기점에서는 두 재개가 각각 요청을 보내고, 두 요청의
    프롬프트에 그 결과가 **각각 실려 각각 청구된다**. 가장 이른 후속 하나만 재면
    다시 실린 몫이 통째로 빠진다(실측: 분기점 50). 그래서 후속마다 증분을 재고
    게이트를 따로 걸어 **통과한 것들의 합**을 페이로드로 둔다 — 두 재개 시점의
    문맥이 달라 증분도 게이트 통과 여부도 다를 수 있으므로 하나로 갈음하지 않는다.
    창 끝의 마지막 메시지는 후속이 없어 페이로드를 얻지 못한다."""
    conv = conversations(corpus)
    succ: dict = defaultdict(set)
    for path in session_paths(corpus).values():
        for a, b in zip(path, path[1:]):
            succ[a].add(b)
    msgs = corpus["msgs"]
    for mid, ent in msgs.items():
        ent["conversation"] = min(conv.get(s, s) for s in ent["sessions"]) if ent["sessions"] else mid
        u = ent["usage"]
        out = int(u.get("output_tokens", 0) or 0)
        deltas = [_prompt(msgs[n]["usage"]) - _prompt(u) - out
                  for n in sorted(succ.get(mid, ()), key=lambda x: (msgs[x]["ts"], x))]
        k = len(ent["tool_uses"])
        n_tools = len(ent["blocks"])
        all_osk = k == n_tools                     # 비-osk 도구가 섞이지 않았다
        for tid in ent["tool_uses"]:
            use = corpus["uses"][tid]
            use["conversation"] = ent["conversation"]
            use["output_tokens"] = out
            use["out_share"] = (out / k) if (all_osk and k) else None
            use["alone"] = n_tools == 1
            res = corpus["results"].get(tid)
            ratios = []
            if deltas and n_tools == 1 and res and res["text_len"] > 0:
                ratios = [(d, res["text_len"] / d) for d in deltas if d > 0]
            passed = [(d, r) for d, r in ratios if GATE[0] <= r <= GATE[1]]
            use["successors"] = len(deltas)
            use["payload_tokens"] = sum(d for d, _r in passed) if passed else None
            use["ratios"] = [r for _d, r in passed]
            use["ratio"] = passed[0][1] if passed else None
            use["gated"] = bool(passed)


def _cost(u: dict) -> float:
    return (u.get("out_share") or 0.0) + ((u.get("payload_tokens") or 0) if u.get("gated") else 0)


# ── scope 기억 에피소드 ───────────────────────────────────────────────────

def episodes(corpus: dict) -> list[dict]:
    """경로별로 '첫 상한 초과 거부 ~ 다음 성공'을 한 에피소드로 묶고, 그 사이에
    증류(create_node/update_node 성공)가 있었는지 본다. 같은 거부에서 시작한
    에피소드가 여러 경로(재개·분기)에 나타나면 **가장 먼저 닫힌 것 하나**만 센다 —
    끝까지 닫히지 않은 경로만 있으면 미해결 하나다."""
    msgs, uses, results = corpus["msgs"], corpus["uses"], corpus["results"]
    best: dict = {}
    for sess, path in session_paths(corpus).items():
        cur = None
        for mid in path:
            for tid in msgs[mid]["tool_uses"]:
                use, res = uses[tid], results.get(tid)
                if res is None:
                    continue
                name = use["name"]
                if name == "scope_memory" and res["overflow"]:
                    if cur is None:
                        cur = {"start": use["ts"], "start_tid": tid, "rejections": 0,
                               "attempted": res["overflow"][0], "distill": 0,
                               "search": 0, "tokens": 0.0, "over": res["overflow"][2]}
                    cur["rejections"] += 1
                    cur["tokens"] += _cost(use)
                elif cur is not None:
                    if name in DISTILL_TOOLS and res["ok"]:
                        cur["distill"] += 1
                    elif name == "search":
                        cur["search"] += 1
                    elif name == "scope_memory" and res["ok"]:
                        cur["end"] = use["ts"]
                        cur["accepted"] = res["chars"]
                        cur["trimmed"] = (cur["attempted"] - res["chars"]) if res["chars"] is not None else None
                        cur["outcome"] = "distill" if cur["distill"] else "trim"
                        _keep_earliest(best, cur)
                        cur = None
        if cur is not None:
            cur["outcome"] = "unresolved"
            cur["end"] = INF
            _keep_earliest(best, cur)
    return sorted(best.values(), key=lambda e: e["start"])


def _keep_earliest(best: dict, ep: dict) -> None:
    key = ep["start_tid"]
    if key not in best or ep["end"] < best[key]["end"]:
        best[key] = ep


# ── 퇴출 기록부 (§9-2 12항 · §9-3) ─────────────────────────────────────────

def evictions(ledger: Path | None, until: float) -> dict | None:
    """창 끝(`until`) 시점의 대장 상태. `at`이 창 뒤인 행은 없는 것으로 본다 —
    나이는 창 끝에서 재며 음수가 되지 않는다."""
    if not ledger or not ledger.is_file():
        return None
    evicts, settled = {}, set()
    with ledger.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            at = _row_time(r)
            if at is not None and at > until:
                continue
            if r.get("kind") == "evict" and r.get("rid"):
                evicts[r["rid"]] = at or 0.0
            elif r.get("kind") == "settle" and r.get("of"):
                settled.add(r["of"])
    open_ = {k: v for k, v in evicts.items() if k not in settled}
    ages = sorted((until - v) / 86400 for v in open_.values() if v)
    done = len(settled & set(evicts))
    return {"evict": len(evicts), "settled": done, "open": len(open_),
            "settle_rate": (done / len(evicts)) if evicts else None,
            "oldest_days": max(ages) if ages else None,
            "age_p50_days": statistics.median(ages) if ages else None}


# ── 집계 ─────────────────────────────────────────────────────────────────

def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(full: dict, since: float, until: float, ledger: Path | None) -> dict:
    """창을 먼저 자르고, 그 위에서 귀속·에피소드·대장 상태를 계산한다."""
    corpus = window(full, until)
    attribute_costs(corpus)
    res, msgs = corpus["results"], corpus["msgs"]
    uses = {t: u for t, u in corpus["uses"].items() if since <= u["ts"]}
    convs_seen = {u.get("conversation") for u in uses.values()}
    per_tool: dict = defaultdict(lambda: {"calls": 0, "fail": 0, "out": [], "payload": [],
                                          "ratios": [], "gated": 0, "alone": 0})
    total = failed_cost = 0.0
    unattributed = 0
    for tid, u in uses.items():
        r = res.get(tid)
        t = per_tool[u["name"]]
        t["calls"] += 1
        f = _failed(r)
        t["fail"] += f
        if u.get("out_share") is not None:
            t["out"].append(u["out_share"])
        else:
            unattributed += 1
        t["alone"] += bool(u.get("alone"))
        c = _cost(u)
        total += c
        failed_cost += c if f else 0.0
        if u.get("gated"):
            t["gated"] += 1
            t["payload"].append(u["payload_tokens"])
            t["ratios"].extend(u["ratios"])
    tools = {}
    for name, t in sorted(per_tool.items(), key=lambda x: -x[1]["calls"]):
        tools[name] = {"calls": t["calls"], "fail": t["fail"],
                       "out_share_mean": round(statistics.mean(t["out"]), 1) if t["out"] else None,
                       "payload_tokens_mean": round(statistics.mean(t["payload"]), 1) if t["payload"] else None,
                       "gate_pass": t["gated"],
                       "chars_per_token": (round(min(t["ratios"]), 2), round(max(t["ratios"]), 2)) if t["ratios"] else None,
                       "alone_rate": round(t["alone"] / t["calls"], 3) if t["calls"] else None}
    sm = [(t, u) for t, u in uses.items() if u["name"] == "scope_memory"]
    overs = [res[t]["overflow"] for t, _u in sm if res.get(t) and res[t]["overflow"]]
    over_amt = [o[2] for o in overs]
    over_tokens = sum(_cost(u) for t, u in sm if res.get(t) and res[t]["overflow"])
    eps = [e for e in episodes(corpus) if since <= e["start"]]
    resolved = [e for e in eps if e["outcome"] != "unresolved"]
    multi = [e for e in eps if e["rejections"] >= 2]
    trimmed = [e["trimmed"] for e in resolved if e.get("trimmed") is not None and e["outcome"] == "trim"]
    fails = sum(1 for t in uses if _failed(res.get(t)))
    # 실패 직후 같은 도구 재호출 — 경로마다 인접 쌍을 보되 같은 쌍은 한 번만 센다
    pairs: set = set()
    for path in session_paths(corpus).values():
        seq = [tid for mid in path for tid in msgs[mid]["tool_uses"] if tid in uses]
        for a, b in zip(seq, seq[1:]):
            if _failed(res.get(a)) and uses[a]["name"] == uses[b]["name"]:
                pairs.add((a, b))
    return {
        "window": [datetime.fromtimestamp(since, timezone.utc).date().isoformat(),
                   datetime.fromtimestamp(until, timezone.utc).date().isoformat()],
        "files": corpus["files"], "conversations": len(convs_seen),
        "calls": len(uses), "failures": fails,
        "speech_unattributed_calls": unattributed,
        "failure_rate": round(fails / len(uses), 3) if uses else None,
        "retry_after_fail": len(pairs),
        "tokens_total": round(total), "tokens_failed": round(failed_cost),
        "tools": tools,
        "scope_memory": {
            "calls": len(sm),
            "alone_rate": round(sum(1 for _t, u in sm if u.get("alone")) / len(sm), 3) if sm else None,
            "overflow_rejections": len(overs), "overflow_tokens": round(over_tokens),
            "overflow_share_of_cost": round(over_tokens / total, 3) if total else None,
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
    L.append(f"osk 턴 계기 — {w[0]} ~ {w[1]} · 파일 {s['files']} · 대화 {s['conversations']} · 호출 {s['calls']}")
    L.append(f"실패 {s['failures']} ({(s['failure_rate'] or 0)*100:.1f}%) · 실패 직후 같은 도구 재호출 {s['retry_after_fail']}"
             f" · 토큰 합 {s['tokens_total']:,} (실패분 {s['tokens_failed']:,})"
             f" — 합은 발화(osk만 있는 메시지, 메시지당 한 번) + 게이트 통과 페이로드"
             f"; 혼합 메시지라 발화를 못 준 호출 {s['speech_unattributed_calls']}")
    L.append("")
    L.append(f"{'도구':<18}{'호출':>6}{'실패':>6}{'발화tok/호출':>13}{'페이로드tok':>11}{'게이트':>7}{'chars/tok':>12}{'단독률':>8}")
    for name, t in s["tools"].items():
        cpt = f"{t['chars_per_token'][0]}~{t['chars_per_token'][1]}" if t["chars_per_token"] else "-"
        L.append(f"{name:<18}{t['calls']:>6}{t['fail']:>6}{(t['out_share_mean'] or 0):>13.0f}"
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
        L.append(f"퇴출 기록부(창 끝 기준): evict {e['evict']} · 처분 {e['settled']} ({(e['settle_rate'] or 0)*100:.0f}%)"
                 f" · 미처분 {e['open']} · 가장 오래된 {e['oldest_days'] and round(e['oldest_days'],1)}일 · 나이 중앙값 {e['age_p50_days'] and round(e['age_p50_days'],1)}일")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="osk 턴 계기 — 트랜스크립트에서 표면 비용·효용 집계(본문은 싣지 않는다)")
    ap.add_argument("--projects", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--since", help="YYYY-MM-DD (UTC, 포함)")
    ap.add_argument("--until", help="YYYY-MM-DD (UTC, 포함) — 파생 상태는 이 시점 자료만으로 계산한다")
    ap.add_argument("--exclude-project", action="append", default=[],
                    help="프로젝트 디렉터리 이름에 이 문자열이 들면 제외 — 감사 세션의 자기 오염을 뺄 때")
    ap.add_argument("--ledger", help="_ledger/evictions.jsonl 경로 (있으면 §9-3 지표)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    since = _ts(a.since + "T00:00:00Z") if a.since else 0.0
    until = _ts(a.until + "T23:59:59Z") if a.until else datetime.now(timezone.utc).timestamp()
    corpus = read_corpus(Path(a.projects), a.exclude_project)
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
