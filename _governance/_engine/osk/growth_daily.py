"""정기 성장 실행의 하루 — 묶음 실행, 한도 조회, 주간 한도 조정(#175 2026-10-18 결정).

  · 하루 한도 L을 묶음으로 나눠 돈다. Scope 통합 작업은 묶음 하나에 혼자(9라운드, 900초) 두되, 같은
    scope의 작은 꼬리는 모두 9턴까지 함께 묶어 한 작업으로 센다(#306 D5). 나머지 큐는 3개씩(600초)
    묶는다. Scope 통합이 첫 차례를 갖는다.
  · 포착은 하루 시작에 한 번, 공급자를 고르기 전에 한다 — 모두 소진인 날에도 한다. Codex 한도는
    묶음마다 조회한다(모델을 부르지 않는다). Codex가 소진이면 Claude 워커가 대신 돈다. 공급자는 주간
    사용률이 0.9에 닿으면 쉰다 — 사용자의 몫을 남긴다(#306 D3). 공급자별 예산은 두지 않는다.
  · 진척 없던 작업(보류 포함)은 그날 다시 고르지 않고 다음 작업으로 잇는다. 작업자 이상(비정상 종료·
    시한 초과·결과 묶음 거절)이면 그 큐만 그날 멈추고, 다른 큐는 차례를 받는다 — 대화 검토가 멈춰도
    재검토 묶음은 돈다(#306 D2). 고를 작업이 없으면 그날 멈춘다. 벽시계 예산(1.5시간)을 넘기면 새
    묶음을 띄우지 않는다. 같은 날 다시 불려도 L과 벽시계 예산은 그날 앞선 실행까지 더해 지킨다.
  · 하루 실행마다 멈춘 까닭과 묶음 수를 기기 기록(`DAYS`)에 남긴다. 묶음을 하나도 돌지 못한 날이
    이어지면 세션 시작이 알린다(`idle`, #306 D6).
  · L은 주마다 다시 정한다(`adjust`). 그 판단 근거는 그 주 첫 묶음의 계획(`daily.adjustment`)에
    남는다. 묶음마다 토큰과 캐시 적중은 실행 기록(`usage`)에 남는다.
  · 대화 검토 적체는 `backlog`가 추정하고(`osk doctor --vault`가 보인다), `daily(backlog=True)`가
    하루 한도·벽시계 예산 없이 Scope 묶음만 대기열이 빌 때까지 돈다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time

from . import (core, growth, integration, growth_status, raw, transcripts, conversation_capture, conversation_review,
               growth_ledger, worker_process)
from .vault import Vault
from . import worker_usage

DAY_SECONDS = 90 * 60                    # 하루 벽시계 예산; 작업 스케줄러 제한은 이것 + 30분이다
SCOPE_ROUNDS = growth.SCOPE_ROUNDS_PER_JOB
SCOPE_TIMEOUT = 900                      # 9라운드 작업은 대화별 검토 실측으로 최대 9.8분이다
LIGHT_SIZE, LIGHT_TIMEOUT = 3, 600
LIGHT = tuple(q for q in growth_ledger.QUEUES if q != "scope_jobs")
BUSY_WAIT = 600                          # 잠금을 쥔 대화별 검토는 작업자 시한(600초) 안에 끝난다
WEEK = 7                                 # D — 소진일에도 Claude로 대신 돌므로 매일 돈다
REST_AT = 0.9                            # 공급자의 주간 사용률이 이 이상이면 쉰다(#306 D3)
OLD_SHARE = 4                            # 오래된 대기는 4주에 나눠 갚는다
LIMIT_FLOOR, LIMIT_BEFORE = 6, 3         # L의 하한, 첫 조정 전의 L(기존 --limit 3)
KEEP_WITHIN = 3                          # 새 L과 지금 L의 차가 이보다 작으면 그대로 둔다
# Scope 묶음 단가의 첫 추정(작업당 12만~15만)의 위쪽 — 적체 추정에 쓴다. 실측이 있으면 그것을 쓴다.
SCOPE_UNIT_COST = 150_000
BACKLOG_COMMAND = ("`osk growth run --daily --backlog --command-file .osk/growth-command.json`(Claude 워커가 "
                   "있으면 `--command-file .osk/growth-command-claude.json`을 더한다)로 일괄로 돈다 — 하루 한도 "
                   "없이 대기열이 빌 때까지 Scope 묶음만 돌고, 공급자는 주간 사용률 0.9에서 쉰다")
DAYS = Path(".osk/growth/days.jsonl")    # 하루 실행의 멈춘 까닭과 묶음 수 — 기기 소유(Mechanism §9-4 5항)


def _epoch(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scan(path: str, harness: str, sid: str, since: float, wanted: set, times: dict) -> int:
    """한 대화의 `since` 뒤 작업 토큰(캐시 읽기 제외). 검토 대기 중인 Claude 턴의 시각도 함께 모은다."""
    tokens, messages = 0, {}
    for line in transcripts.native_lines(path, harness, sid):
        if harness == "codex" and b'"token_count"' not in line:
            continue
        if harness == "claude" and b'"usage"' not in line and not any(u.encode() in line for u in wanted):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        at = _epoch(row.get("timestamp"))
        if row.get("uuid") in wanted and at is not None:
            times[row["uuid"]] = at
        if at is None or at < since:
            continue
        payload, message = row.get("payload") or {}, row.get("message") or {}
        last = (payload.get("info") or {}).get("last_token_usage") if payload.get("type") == "token_count" else None
        if harness == "codex" and isinstance(last, dict):
            tokens += last.get("input_tokens", 0) - last.get("cached_input_tokens", 0) + last.get("output_tokens", 0)
        elif harness == "claude" and row.get("type") == "assistant" and isinstance(message.get("usage"), dict):
            messages[message.get("id") or row.get("uuid")] = message["usage"]
    return tokens + sum(u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                        + u.get("output_tokens", 0) for u in messages.values())


def _round_at(harness: str, ident: str, times: dict) -> float | None:
    if harness == "codex":
        turn = ident.rsplit(":", 1)[-1]
        return int(turn.replace("-", "")[:12], 16) / 1000 if re.match(core.RID_RE, turn) else None
    return times.get(ident.split(":", 1)[0])


def _measure(vault: Vault, since: float) -> dict:
    """이 vault가 추적하는 대화의 원본 전사에서 `since` 뒤의 공급자별 작업 토큰, 그리고 검토할 수
    있는 대기 라운드를 `since` 앞뒤로 갈라 센다. 작업은 대화마다 9라운드씩이다. 원본이 없는 대화의
    원본 라운드는 검토할 수 없어 세지 않는다."""
    work = dict.fromkeys(worker_usage.PROVIDERS, 0)
    inflow = dict.fromkeys(("new_rounds", "new_jobs", "old_rounds", "old_jobs", "unread"), 0)
    for s in growth_status.conversation_states(vault):
        harness, sid = s["harness"], s["conversation_id"]
        if harness not in work:
            continue
        try:
            path = conversation_capture.locate_transcript(vault, harness, sid, s.get("capture_path") or s["transcript_path"])
            recent = bool(path) and Path(path).stat().st_mtime >= since
        except (OSError, ValueError):
            path, recent = None, False
        by_ref = {r["ref"]: r for r in s["rounds"]}
        pending = [by_ref[ref] for ref in dict.fromkeys(conversation_capture.state_view(vault, s)["pending_refs"])
                   if ref in by_ref and (path or not raw.is_native(ref))]
        times = {}
        if recent:
            try:
                work[harness] += _scan(path, harness, sid, since,
                                       {r["id"].split(":", 1)[0] for r in pending} if harness == "claude" else set(),
                                       times)
            except (OSError, ValueError):
                inflow["unread"] += 1
        # 이번 주에 고치지 않은 전사의 턴은 모두 그 전의 것이다.
        new = sum(1 for r in pending if recent and (_round_at(harness, r["id"], times) or 0) >= since)
        old = len(pending) - new
        inflow["new_rounds"] += new
        inflow["old_rounds"] += old
        inflow["new_jobs"] += -(-new // SCOPE_ROUNDS)
        inflow["old_jobs"] += -(-old // SCOPE_ROUNDS)
    return {"work": work, "inflow": inflow}


def _at(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(row["at"])
    except (KeyError, TypeError, ValueError):
        return None


def _scope_unit(rows: list[dict]) -> tuple[int, int]:
    """Scope 묶음 하나의 토큰 — 최근 실측한 정기 Scope 묶음(많아야 20개)의 평균, 없으면 첫 추정.
    (토큰, 실측한 묶음 수)."""
    plans = {r.get("rid"): r for r in rows if r.get("kind") == "plan"}
    got = [r["usage"]["tokens"] for r in rows if r.get("kind") == "run" and (r.get("usage") or {}).get("measured")
           and ((plans.get(r.get("manifest")) or {}).get("daily") or {}).get("kind") == "scope"][-20:]
    return (round(sum(got) / len(got)) if got else SCOPE_UNIT_COST), len(got)


def backlog(vault: Vault) -> dict:
    """대화 검토 적체(#306) — 정기 실행이 지금 검토할 수 있는 대화와 턴(끝난 대화의 원본 턴과 저장
    라운드), 같은 scope의 작은 꼬리를 9턴까지 묶는 Scope 묶음 수의 추정(남는 꼬리를 큰 것부터 채운다),
    예상 토큰(Scope 묶음 단가 × 묶음 수). 대화 상태를 쓰지 않고 읽기만 한다."""
    unit, measured = _scope_unit(growth_ledger.ledger_records(vault))
    scopes: dict = {}
    conversations = turns = 0
    for s in growth_status.conversation_states(vault):
        refs = conversation_capture.state_view(vault, s)["pending_refs"]
        ended = bool(refs) and conversation_capture.ended_source(vault, s)
        n = sum(1 for ref in refs if ended or not raw.is_native(ref))
        if n:
            conversations, turns = conversations + 1, turns + n
            scopes.setdefault(conversation_review.review_scope(vault, s), []).append(n)
    batches = 0
    for scope, sizes in scopes.items():
        batches += sum(n // SCOPE_ROUNDS for n in sizes)
        tails, bins = sorted((n % SCOPE_ROUNDS for n in sizes if n % SCOPE_ROUNDS), reverse=True), []
        for tail in tails:
            room = next((i for i, b in enumerate(bins) if scope and b + tail <= SCOPE_ROUNDS), None)
            if room is None:
                bins.append(tail)
            else:
                bins[room] += tail
        batches += len(bins)
    return {"conversations": conversations, "turns": turns, "batches": batches, "unit_tokens": unit,
            "measured_batches": measured, "tokens": batches * unit}


def _adjustments(rows: list[dict]) -> list[dict]:
    return [r["daily"]["adjustment"] for r in rows
            if r.get("kind") == "plan" and isinstance((r.get("daily") or {}).get("adjustment"), dict)]


def adjust(vault: Vault, rows: list[dict], providers: list[str], now: datetime | None = None) -> dict:
    """주간 한도 조정. N은 지난 7일 유입 가운데 정기 실행 몫(9라운드 작업 수)과 7일보다 오래된 대기
    작업의 4분의 1이다. 하루 Scope 작업 S = ⌈N÷7⌉, 하루 한도는 L = max(6, S+3)이다(+3은 가벼운 큐
    묶음 하나). 공급자별 예산으로 자르지 않는다 — 공급자는 주간 사용률 0.9에서 쉰다(`_choose`, #306 D3).
    늘릴 때는 바로 늘리고, 줄일 때는 절반까지만 줄이며, 차가 3보다 작으면 그대로 둔다. 공급자별 작업
    토큰(`work`)은 판단 근거로만 남는다."""
    now = now or datetime.now(timezone.utc).astimezone()
    since = now - timedelta(days=WEEK)
    measured = _measure(vault, since.timestamp())
    inflow = measured["inflow"]
    n = inflow["new_jobs"] + -(-inflow["old_jobs"] // OLD_SHARE)
    s = -(-n // WEEK)
    target = max(LIMIT_FLOOR, s + LIGHT_SIZE)
    previous = (_adjustments(rows) or [{"limit": LIMIT_BEFORE}])[-1]["limit"]
    limit = (previous if abs(target - previous) < KEEP_WITHIN else
             target if target > previous else max(target, -(-previous // 2)))
    return {"at": now.isoformat(timespec="seconds"), "N": n, "S": s, "target": target, "previous": previous,
            "limit": limit, "inflow": inflow, "work": {p: measured["work"][p] for p in providers}}


def idle(vault: Vault, today: str | None = None) -> dict | None:
    """정기 실행이 묶음을 하나도 돌지 못한 날이 오늘까지 이어지면 그 날들과 마지막 까닭(`DAYS`). 기록이
    없는 날도 센다 — 정기 실행이 시작되지 않은 날이다(`missing`). 오늘은 기록이 있을 때만 센다(아직 돌 때가
    아닐 수 있다). 고를 작업이 없던 날(`empty`)은 쉰 것이 아니라 끊는다. 적체 일괄 실행(`backlog`)은 정기
    실행이 아니라 판정에 넣지 않는다. 이어지지 않으면 None."""
    try:
        rows = [json.loads(line) for line in (vault.root / DAYS).read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, ValueError):
        return None
    days: dict[str, dict] = {}
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("day"), str) and not r.get("backlog"):
            d = days.setdefault(r["day"], {"batches": 0, "states": []})
            d["batches"] += r.get("batches") or 0
            d["states"].append(r.get("state"))
    if not days:
        return None
    day = datetime.fromisoformat(today or datetime.now(timezone.utc).astimezone().date().isoformat()).date()
    if day.isoformat() not in days:
        day -= timedelta(days=1)
    streak = []
    while day.isoformat() >= min(days):
        d = days.get(day.isoformat())
        if d and (d["batches"] or "empty" in d["states"]):
            break
        streak.append(day.isoformat())
        day -= timedelta(days=1)
    if not streak:
        return None
    last = days.get(streak[0])
    return {"days": len(streak), "first": streak[-1], "last": streak[0],
            "state": last["states"][-1] if last else "missing"}


def _counted(kind: str, run: dict) -> int:
    """L에 세는 작업 수 — Scope 묶음은 작은 꼬리를 함께 묶어도 하나다(#306 D5)."""
    return min(1, run.get("selected", 0)) if kind == "scope" else run.get("selected", 0)


def _next_kind(done: dict, left: set, limit: int) -> str | None:
    """Scope 통합이 첫 차례, 이어 가벼운 큐 묶음 하나, 그다음 Scope가 L−3까지, 남은 자리는 가벼운 큐."""
    scope = "scope" in left and done["scope"] < limit - LIGHT_SIZE
    if scope and (not done["scope"] or done["light"] >= LIGHT_SIZE or "light" not in left):
        return "scope"
    return "light" if "light" in left else "scope" if scope else None


def claude_week(vault: Vault, rows: list[dict], now: float | None = None) -> float | None:
    """Claude의 7일 사용률(0~1) — 가장 최근 Claude 실행(정기 묶음·대화 검토 fork) 출력의 한도 알림
    (`rate_limit_event`의 `unifiedWindows.seven_day`)에서 읽는다. Claude에는 모델을 부르지 않는 조회가
    없다. 그 창이 이미 새로 열렸거나 읽은 값이 없으면 None이다. 최근 Claude 실행 20개까지만 본다."""
    now = time.time() if now is None else now
    seen = 0
    for r in reversed(rows):
        if r.get("kind") != "run" or r.get("provider") != "claude" or not r.get("output"):
            continue
        seen += 1
        try:
            events = worker_usage.output_events(vault.root / r["output"] / "stdout.txt")
        except OSError:
            events = []
        for e in reversed(events):
            week = ((e.get("rate_limit_info") or {}).get("unifiedWindows") or {}).get("seven_day") \
                if e.get("type") == "rate_limit_event" else None
            if isinstance(week, dict) and isinstance(week.get("utilization"), (int, float)):
                return float(week["utilization"]) if (week.get("resetsAt") or 0) > now else None
        if seen >= 20:
            break
    return None


def _choose(vault: Vault, commands: dict, off: dict) -> tuple[str | None, dict | None, dict]:
    """이 묶음의 공급자 — 선호 순서대로, 그날 막힌 공급자(`off`)·소진(신호 없음 포함)·주간 사용률이
    0.9에 닿은 공급자(`rest`)를 건너뛴다. Codex는 묶음마다 다시 조회하고, Claude는 최근 실행의 한도
    알림을 읽는다(`claude_week`). (공급자, Codex 조회, 건너뛴 까닭)."""
    limits, skipped = None, {}
    for who in [p for p in worker_usage.PROVIDERS if p in commands] + [p for p in commands if p not in worker_usage.PROVIDERS]:
        if who in off:
            skipped[who] = off[who]
            continue
        if who == "codex":
            # 실행기가 띄울 바이너리로 묻는다 — Desktop이 갱신되면 명령 파일의 옛 경로는 지워진다.
            # 쓸 수 없는 명령은 묻지 않는다: 그 묶음이 `invalid_command`로 알린다.
            checked = worker_process.check_command(vault, commands[who])
            if checked["ok"] and (limits := worker_usage.codex_limits(vault, checked["executable"]))["exhausted"]:
                skipped[who] = "exhausted" if limits["signal"] else "no_signal"
                continue
            if checked["ok"] and (limits.get("weekly") or 0) >= REST_AT * 100:
                skipped[who] = "rest"
                continue
        elif who == "claude" and (claude_week(vault, growth_ledger.ledger_records(vault)) or 0) >= REST_AT:
            skipped[who] = "rest"
            continue
        return who, limits, skipped
    return None, limits, skipped


def _batch(vault: Vault, command: list[str], kind: str, size: int, **args) -> dict:
    """묶음 하나. 다른 실행이 잠금을 쥐었으면(`busy`) 기다렸다가 한 번 다시 시도한다."""
    for wait in (True, False):
        result = growth.run(vault, command, limit=size, timeout=SCOPE_TIMEOUT if kind == "scope" else LIGHT_TIMEOUT,
                            queues=("scope_jobs",) if kind == "scope" else LIGHT, scope_rounds=SCOPE_ROUNDS,
                            scope_bundle=SCOPE_ROUNDS if kind == "scope" else 0, **args)
        if result.get("state") != "busy" or not wait:
            return result
        time.sleep(BUSY_WAIT)
    return result


def daily(vault: Vault, commands: list[list[str]], *, invocation: str = "scheduled", backlog: bool = False) -> dict:
    """하루 정기 성장 실행. 명령은 공급자마다 하나다(Codex, 그리고 대신 돌 Claude 워커). `backlog`는 대화
    검토 적체를 일괄로 돈다 — 주간 조정·하루 한도·벽시계 예산 없이 Scope 묶음만, 대기열이 비거나 작업자
    이상이 나거나 모든 공급자가 쉴 때까지."""
    started = time.monotonic()
    by: dict = {}
    for command in commands:
        who = worker_usage.provider(command) or "other"      # 한도도 예산도 모르는 명령
        if who in by:
            raise ValueError(f"two growth commands for one provider ({who})")
        by[who] = command
    errors = []
    # 포착은 모델을 부르지 않는다 — 공급자가 모두 소진인 날에도, 주간 측정보다 먼저 한 번 한다.
    # 실패는 묶음을 막지 않지만 그날의 실패다. 이미 적힌 같은 오류(원본이 지워진 대화 등)는 대화
    # 상태와 `integration status`에 남아 있다 — 날마다 실패로 세면 새 실패가 묻힌다.
    try:
        captured = integration.capture_known(vault)
    except Exception as exc:
        captured = {"captures": [], "errors": [{"error": f"{type(exc).__name__}: {exc}"}]}
    failed = captured["errors"] + [c for c in captured["captures"] if c.get("new_error")]
    if failed:
        errors.append({"capture": failed})
    rows = growth_ledger.ledger_records(vault)
    now = datetime.now(timezone.utc).astimezone()
    day = now.date().isoformat()
    last = (_adjustments(rows) or [None])[-1]
    # 주는 날짜로 센다. 조정 시각은 포착을 마친 뒤에 적혀 날마다 몇 분씩 달라서, 시각으로 재면 같은
    # 시각의 매일 실행이 7일째에 몇 분 모자라 하루씩 밀린다(#258).
    fresh = not backlog and (last is None or (now.date() - datetime.fromisoformat(last["at"]).astimezone(
        now.tzinfo).date()).days >= WEEK)
    adjustment = last
    if fresh:
        try:
            adjustment = adjust(vault, rows, [p for p in worker_usage.PROVIDERS if p in by], now)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # 재지 못한 주는 남기지 않는다 — 다음 실행이 다시 잰다. 그동안은 지난 L(없으면 하한)로 돈다.
            errors.append({"adjustment": f"{type(exc).__name__}: {exc}"})
            fresh = False
    adjustment = adjustment or {"at": now.isoformat(timespec="seconds"), "limit": LIMIT_FLOOR}
    recorded = fresh                 # 새로 정했으면 첫 묶음의 계획에 판단 근거를 싣는다
    limit = adjustment["limit"]
    plans = {r["rid"]: r for r in rows if r.get("kind") == "plan"}
    today = [(plans[r["manifest"]], r) for r in rows if r.get("kind") == "run" and r.get("manifest") in plans
             and (plans[r["manifest"]].get("daily") or {}).get("day") == day]
    exclude = {k for _, r in today for k in (r.get("progress") or {}).get("none", [])}
    # 같은 날 다시 불려도(수동 실행·재시작) L과 벽시계 예산은 그날 전체의 것이다. 한도에 거절된
    # 묶음은 다른 공급자가 다시 돌았으니 작업 수에 넣지 않는다 — 쓴 시간은 넣는다. 적체 일괄 실행의
    # 묶음은 L과 예산에 넣지 않는다 — 넣으면 그날 정기 실행의 재검토·정돈 묶음이 차례를 잃는다(#312 리뷰).
    # 그날 진척 없던 작업을 다시 고르지 않는 것(`exclude`)에는 넣는다.
    done, earlier = {"scope": 0, "light": 0}, 0.0
    for plan, run in today:
        if plan["daily"].get("backlog"):
            continue
        if plan["daily"].get("kind") in done and run.get("state") not in growth_ledger.WAITS:
            done[plan["daily"]["kind"]] += _counted(plan["daily"]["kind"], run)
        if (begun := _at(plan)) and (ended := _at(run)):
            earlier += max(0.0, (ended - begun).total_seconds())
    left, off, faults = {"scope", "light"}, {}, set()
    batches, stop, skipped = [], None, {}
    while stop is None:
        kind = ("scope" if "scope" in left else None) if backlog else _next_kind(done, left, limit)
        if not backlog and time.monotonic() - started + earlier >= DAY_SECONDS:
            stop = "time_budget"
        elif not backlog and done["scope"] + done["light"] >= limit:
            stop = "daily_limit"
        elif kind is None:
            stop = "worker_fault" if faults else "empty"
        if stop:
            break
        who, limits, skipped = _choose(vault, by, off)
        if who is None:
            # 소진·한도 거절은 대기다(#173). 사용률 0.9에서 쉬는 것도 실패가 아니다.
            stop = ("limited" if {"exhausted", "no_signal", "limited"} & set(skipped.values()) else
                    "rest" if "rest" in skipped.values() else "unavailable")
            break
        info = {"day": day, "batch": len(batches) + 1, "kind": kind, "provider": who, "limit": limit,
                "limits": limits, "skipped": skipped, **({"backlog": True} if backlog else {})}
        if recorded:
            info["adjustment"] = adjustment
        result = _batch(vault, by[who], kind, 1 if kind == "scope" else min(LIGHT_SIZE, limit - sum(done.values())),
                        capture=False, exclude=frozenset(exclude), daily=info, invocation=invocation)
        state = result.get("state")
        if result.get("manifest"):
            recorded = False         # 판단 근거는 이 계획에 남았다
            batches.append({k: result.get(k) for k in ("manifest", "state", "provider", "selected", "progress",
                                                      "usage", "returncode", "error")} | {"kind": kind})
        if state == "busy":
            stop = "busy"
        elif state in {"skipped", "capture_pending"}:
            left.discard(kind)
        elif state in {"limited", "unavailable", "invalid_command"}:
            off[who] = state         # 그날은 다른 공급자로 같은 묶음을 다시 띄운다
            if state != "limited":
                errors.append({"provider": who, "state": state,
                               "error": result.get("error") or result.get("violations")})
        else:
            done[kind] += _counted(kind, result)
            exclude |= set(result["progress"]["none"])
            if growth_ledger.run_failed(result):
                errors.append({"manifest": result["manifest"], "state": state, "error": result.get("error")})
                faults.add(kind)
                left.discard(kind)   # 작업자 이상 — 이 큐만 그날 멈춘다
    try:
        path = vault.root / DAYS
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"day": day, "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                                "state": stop, "batches": len(batches), **({"backlog": True} if backlog else {})},
                               ensure_ascii=False) + "\n")
    except OSError as exc:
        errors.append({"days": f"{type(exc).__name__}: {exc}"})
    # `done`과 `day_seconds`는 그날 앞선 실행까지 더한 값이다.
    return {"ok": not errors, "state": stop, "day": day, "limit": limit, "done": done,
            "elapsed_seconds": round(time.monotonic() - started),
            "day_seconds": round(time.monotonic() - started + earlier), "skipped": skipped,
            **({"backlog": True} if backlog else {}), "adjustment": adjustment if fresh else {"at": adjustment["at"], "limit": limit},
            "capture": {"ok": not failed, "appended": sum(1 for c in captured["captures"] if c.get("appended")),
                        "failed": len(failed),
                        "known_errors": sum(1 for c in captured["captures"] if not c["ok"] and not c["new_error"])},
            "batches": batches, "errors": errors}
