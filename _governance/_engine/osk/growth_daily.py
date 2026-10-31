"""정기 성장 실행의 하루 — 묶음 실행, 한도 조회, 주간 한도 조정(#175 2026-10-18 결정).

  · 하루 한도 L을 묶음으로 나눠 돈다. Scope 통합 작업은 묶음 하나에 혼자(9라운드, 900초) 두고,
    나머지 큐는 3개씩(600초) 묶는다. Scope 통합이 첫 차례를 갖는다.
  · 포착은 하루 시작에 한 번, 공급자를 고르기 전에 한다 — 모두 소진인 날에도 한다. Codex 한도는
    묶음마다 조회한다(모델을 부르지 않는다). Codex가 소진이면 Claude 워커가 대신 돈다.
  · 고를 작업이 없거나 묶음이 진척 없이 끝나면 그날 멈춘다. 진척 없던 작업은 그날 다시 고르지
    않는다. 벽시계 예산(1.5시간)을 넘기면 새 묶음을 띄우지 않는다. 같은 날 다시 불려도 L과
    벽시계 예산은 그날 앞선 실행까지 더해 지킨다.
  · L과 공급자별 예산은 주마다 다시 정한다(`adjust`). 그 판단 근거는 그 주 첫 묶음의 계획
    (`daily.adjustment`)에 남는다. 묶음마다 토큰과 캐시 적중은 실행 기록(`usage`)에 남는다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
import threading
import time

from . import core, growth, integration

PROVIDERS = ("codex", "claude")          # 선호 순서 — Codex 예산을 넘는 몫은 Claude가 맡는다
DAY_SECONDS = 90 * 60                    # 하루 벽시계 예산; 작업 스케줄러 제한은 이것 + 30분이다
SCOPE_ROUNDS = growth.SCOPE_ROUNDS_PER_JOB
SCOPE_TIMEOUT = 900                      # 9라운드 작업은 대화별 검토 실측으로 최대 9.8분이다
LIGHT_SIZE, LIGHT_TIMEOUT = 3, 600
LIGHT = tuple(q for q in growth._QUEUES if q != "scope_jobs")
BUSY_WAIT = 600                          # 잠금을 쥔 대화별 검토는 작업자 시한(600초) 안에 끝난다
MISS_BELOW = 0.9                         # 적중률(캐시 읽기 ÷ 입력)이 이보다 낮은 요청이 미스다
WEEK = 7                                 # D — 소진일에도 Claude로 대신 돌므로 매일 돈다
RATIO, RATIO_BACKLOG = 20, 10            # 작업:검토 — 7일보다 오래된 대기가 있으면 10:1까지
OLD_SHARE = 4                            # 오래된 대기는 4주에 나눠 갚는다
LIMIT_FLOOR, LIMIT_BEFORE = 6, 3         # L의 하한, 첫 조정 전의 L(기존 --limit 3)
KEEP_WITHIN = 3                          # 새 L과 지금 L의 차가 이보다 작으면 그대로 둔다
# Scope 작업 단가의 첫 추정(작업당 12만~15만)의 위쪽 — 지난주 Scope 묶음 실측이 있으면 그것을 쓴다.
SCOPE_UNIT_COST = 150_000
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def provider(command: list[str]) -> str | None:
    """명령의 공급자 — 실행 파일 이름(`codex`·`claude`)이다. 그 밖의 명령은 한도도 예산도 모른다."""
    name = Path(command[0]).stem.lower() if command else ""
    return name if name in PROVIDERS else None


def codex_limits(executable: str, timeout: float = 30) -> dict:
    """`codex app-server`의 `account/rateLimits/read` — 모델을 부르지 않는다(codex-cli 0.154 확인).
    소진은 크레딧까지 바닥난 상태다: `rateLimitReachedType`이나 `spendControlReached`가 서 있거나,
    포함 사용량도 크레딧도 없다. 주간 창이 100%여도 크레딧이 있으면 소진이 아니다. 답하지 못하면
    신호 없음이고 소진과 같이 다룬다."""
    from . import response_growth
    requests = [{"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "osk", "version": "1"}}},
                {"method": "initialized"},
                {"id": 1, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}}]
    try:
        proc = subprocess.Popen([executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=core.ROOT, env=response_growth.subscription_env(),
                                start_new_session=os.name != "nt", creationflags=NO_WINDOW)
    except OSError as exc:
        return {"signal": False, "exhausted": True, "error": str(exc)}
    answer: dict = {}

    def read():
        for line in proc.stdout:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("id") == 1:
                answer.update(row)
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        proc.stdin.write(("\n".join(json.dumps(r) for r in requests) + "\n").encode("utf-8"))
        proc.stdin.flush()
    except OSError:
        pass
    reader.join(timeout)
    growth._stop_tree(proc)
    result = answer.get("result")
    snap = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(snap, dict):
        return {"signal": False, "exhausted": True,
                "error": str((answer.get("error") or {}).get("message") or "no rate-limit answer")}
    credits = snap.get("credits") if isinstance(snap.get("credits"), dict) else {}
    windows = {w.get("windowDurationMins"): w.get("usedPercent")
               for w in (snap.get("primary"), snap.get("secondary")) if isinstance(w, dict)}
    exhausted = bool(snap.get("rateLimitReachedType") or snap.get("spendControlReached")
                     or result.get("ordinaryUsageAllowed") is not True
                     and not (credits.get("hasCredits") or credits.get("unlimited")))
    return {"signal": True, "exhausted": exhausted, "weekly": windows.get(10080), "five_hour": windows.get(300),
            "ordinary": result.get("ordinaryUsageAllowed"), "credits": credits.get("balance"),
            "has_credits": credits.get("hasCredits"), "reached": snap.get("rateLimitReachedType"),
            "spend_control": snap.get("spendControlReached")}


def _events(output: Path) -> list[dict]:
    rows = []
    for line in output.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def limited(output: Path, command: list[str]) -> bool:
    """끝내지 못한 작업자가 한도에 막혔는가 — 실패가 아니라 대기다(#173). Claude는 조직 설정으로
    초과 사용이 꺼져 있어 100%에서 거절하고, 그 거절을 `rate_limit_event`(status `rejected`)로 낸다.
    Codex는 크레딧까지 바닥나야 거절하므로, 끝난 뒤 다시 조회해 소진이면 거절로 본다. 조회가 답하지
    못하면 거절이 아니다 — 실행 전 선택은 신호 없음을 소진과 같이 다루지만, 이미 실패한 작업의 까닭으로
    확정하면 작업자 오류가 대기로 묻힌다."""
    who = provider(command)
    try:
        if who == "claude":
            return any(e.get("type") == "rate_limit_event"
                       and (e.get("rate_limit_info") or {}).get("status") == "rejected" for e in _events(output))
    except OSError:
        return False
    if who != "codex":
        return False
    limits = codex_limits(command[0])
    return limits["signal"] and limits["exhausted"]


def _claude_requests(output: Path) -> tuple[list, dict | None]:
    """요청마다 (입력 전체, 캐시 읽기, 출력) — 한 메시지의 부분들은 같은 id로 따로 나온다. 합계는
    `result.usage`다: `result.modelUsage`는 이어받은 원 대화의 누계를 담는다."""
    messages, total = {}, None
    for e in _events(output):
        message = e.get("message") or {}
        u = message.get("usage")
        if e.get("type") == "assistant" and isinstance(u, dict) and message.get("id"):
            messages[message["id"]] = u
        elif e.get("type") == "result" and isinstance(e.get("usage"), dict):
            total = e["usage"]
    shape = lambda u: (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                       + u.get("cache_read_input_tokens", 0), u.get("cache_read_input_tokens", 0),
                       u.get("output_tokens", 0))
    return [shape(u) for u in messages.values()], shape(total) if total else None


def _codex_requests(output: Path) -> tuple[list, None]:
    """요청마다 (입력 전체, 캐시 읽기, 출력) — 작업자 자신의 세션 기록(`thread.started`의 id)에서
    읽는다. fork는 원 대화의 행을 이어받으므로 자기 `session_meta` 뒤의 행만 센다."""
    from . import harness as adapters
    tid = next((e.get("thread_id") for e in _events(output) if e.get("type") == "thread.started"), None)
    paths = adapters.get("codex").transcripts(tid) if isinstance(tid, str) and tid.isprintable() and tid else []
    if len(paths) != 1:
        return [], None
    rows = _events(paths[0])
    start = next((r.get("timestamp", "") for r in rows if r.get("type") == "session_meta"), "")
    out = []
    for r in rows:
        p = r.get("payload") or {}
        u = (p.get("info") or {}).get("last_token_usage") if p.get("type") == "token_count" else None
        if r.get("type") == "event_msg" and isinstance(u, dict) and r.get("timestamp", "") >= start:
            out.append((u.get("input_tokens", 0), u.get("cached_input_tokens", 0), u.get("output_tokens", 0)))
    return out, None


def usage(output: Path, command: list[str], *, cold_first: bool) -> dict:
    """한 실행의 토큰과 캐시 적중. 토큰은 캐시 읽기를 뺀 값(캐시 아닌 입력 + 캐시 쓰기 + 출력)이다.
    미스는 적중률 0.9 미만인 요청이다. 정기 묶음은 `codex exec`를 새로 띄워 첫 요청이 구조적으로
    식어 있다 — 그 냉시작은 따로 적고 판정에서 뺀다(`cold_first`). 대화별 검토의 첫 요청은 원
    대화의 캐시를 이어받아야 하므로 판정에 넣는다."""
    who = provider(command)
    try:
        requests, total = (_codex_requests(output) if who == "codex" else
                           _claude_requests(output) if who == "claude" else ([], None))
    except (OSError, ValueError, TypeError) as exc:
        return {"measured": False, "reason": str(exc)}
    if not requests and not total:
        return {"measured": False, "reason": "no per-request usage"}
    whole, cached, out = total or tuple(map(sum, zip(*requests)))
    hit = lambda r: r[1] / r[0] if r[0] else 1.0
    result = {"measured": True, "tokens": whole - cached + out, "cache_read": cached, "requests": len(requests),
              "misses": [[i, round(hit(r), 3)] for i, r in enumerate(requests)
                         if (i or not cold_first) and hit(r) < MISS_BELOW]}
    if cold_first and requests:
        first = requests[0]
        result["cold_start"] = {"tokens": first[0] - first[1] + first[2], "hit": round(hit(first), 3)}
    return result


def _epoch(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scan(path: str, harness: str, sid: str, since: float, wanted: set, times: dict) -> int:
    """한 대화의 `since` 뒤 작업 토큰(캐시 읽기 제외). 검토 대기 중인 Claude 턴의 시각도 함께 모은다."""
    from . import transcripts
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


def _measure(since: float) -> dict:
    """이 vault가 추적하는 대화의 원본 전사에서 `since` 뒤의 공급자별 작업 토큰, 그리고 검토할 수
    있는 대기 라운드를 `since` 앞뒤로 갈라 센다. 작업은 대화마다 9라운드씩이다. 원본이 없는 대화의
    원본 라운드는 검토할 수 없어 세지 않는다."""
    from . import growth_status, raw
    work = dict.fromkeys(PROVIDERS, 0)
    inflow = dict.fromkeys(("new_rounds", "new_jobs", "old_rounds", "old_jobs", "unread"), 0)
    for s in growth_status._states():
        harness, sid = s["harness"], s["conversation_id"]
        if harness not in work:
            continue
        try:
            path = integration._locate_transcript(harness, sid, s.get("capture_path") or s["transcript_path"])
            recent = bool(path) and Path(path).stat().st_mtime >= since
        except (OSError, ValueError):
            path, recent = None, False
        by_ref = {r["ref"]: r for r in s["rounds"]}
        pending = [by_ref[ref] for ref in dict.fromkeys(integration._view(s)["pending_refs"])
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


def _spent(rows: list[dict], since: datetime) -> tuple[dict, list[int]]:
    """`since` 뒤 실행의 공급자별 토큰 — 대화별 검토(`review`)와 정기 묶음(`daily`), 그리고 Scope
    묶음 하나하나의 토큰(단가). 이 변경 전의 실행은 이 기기에 남은 출력에서 잰다."""
    plans = {r["rid"]: r for r in rows if r.get("kind") == "plan"}
    spent = {p: {"review": 0, "daily": 0, "unmeasured": 0} for p in PROVIDERS}
    scope = []
    for r in rows:
        at = _at(r)
        if r.get("kind") != "run" or at is None or at < since:
            continue
        plan = plans.get(r.get("manifest")) or {}
        path = ("review" if str(plan.get("work_context", "")).startswith("stop:") else
                "daily" if plan.get("daily") else None)
        who, measured = r.get("provider"), r.get("usage")
        if path and "provider" not in r and r.get("output"):
            try:
                command = json.loads((core.ROOT / r["output"] / "argv.json").read_text(encoding="utf-8"))
                who = provider(command)
                measured = usage(core.ROOT / r["output"] / "stdout.txt", command, cold_first=path == "daily")
            except (OSError, ValueError, TypeError):
                pass
        if not path or who not in spent:
            continue
        if (measured or {}).get("measured"):
            spent[who][path] += measured["tokens"]
            if path == "daily" and plan["daily"].get("kind") == "scope":
                scope.append(measured["tokens"])
        else:
            spent[who]["unmeasured"] += 1
    return spent, scope


def _adjustments(rows: list[dict]) -> list[dict]:
    return [r["daily"]["adjustment"] for r in rows
            if r.get("kind") == "plan" and isinstance((r.get("daily") or {}).get("adjustment"), dict)]


def adjust(rows: list[dict], providers: list[str], now: datetime | None = None) -> dict:
    """주간 한도 조정. N은 지난 7일 유입 가운데 정기 실행 몫(9라운드 작업 수)과 7일보다 오래된 대기
    작업의 4분의 1이다. 하루 Scope 작업 S = ⌈N÷7⌉을 공급자별 예산으로 자르고, 하루 한도는
    L = max(6, S+3)이다(+3은 가벼운 큐 묶음 하나). 예산은 공급자마다 "지난주 작업 ÷ 20 − 지난주 대화별
    검토"이며, 오래된 대기가 있으면 ÷10까지 쓴다. 늘릴 때는 바로 늘리고, 줄일 때는 절반까지만
    줄이며, 차가 3보다 작으면 그대로 둔다."""
    now = now or datetime.now(timezone.utc).astimezone()
    since = now - timedelta(days=WEEK)
    measured = _measure(since.timestamp())
    spent, scope = _spent(rows, since)
    inflow = measured["inflow"]
    ratio = RATIO_BACKLOG if inflow["old_jobs"] else RATIO
    unit = round(sum(scope) / len(scope)) if scope else SCOPE_UNIT_COST
    budgets = {p: {"work": measured["work"][p], "reviews": spent[p]["review"],
                   "unmeasured_reviews": spent[p]["unmeasured"],
                   "budget": measured["work"][p] // ratio - spent[p]["review"]} for p in providers}
    n = inflow["new_jobs"] + -(-inflow["old_jobs"] // OLD_SHARE)
    s = -(-n // WEEK)
    affordable = sum(max(0, b["budget"]) for b in budgets.values()) // unit // WEEK
    target = max(LIMIT_FLOOR, min(s, affordable) + LIGHT_SIZE)
    previous = (_adjustments(rows) or [{"limit": LIMIT_BEFORE}])[-1]["limit"]
    limit = (previous if abs(target - previous) < KEEP_WITHIN else
             target if target > previous else max(target, -(-previous // 2)))
    return {"at": now.isoformat(timespec="seconds"), "N": n, "S": s, "S_affordable": affordable,
            "target": target, "previous": previous, "limit": limit, "ratio": ratio,
            "unit_cost": {"tokens": unit, "measured_batches": len(scope)}, "inflow": inflow, "providers": budgets}


def _next_kind(done: dict, left: set, limit: int) -> str | None:
    """Scope 통합이 첫 차례, 이어 가벼운 큐 묶음 하나, 그다음 Scope가 L−3까지, 남은 자리는 가벼운 큐."""
    scope = "scope" in left and done["scope"] < limit - LIGHT_SIZE
    if scope and (not done["scope"] or done["light"] >= LIGHT_SIZE or "light" not in left):
        return "scope"
    return "light" if "light" in left else "scope" if scope else None


def _choose(commands: dict, budgets: dict, off: dict) -> tuple[str | None, dict | None, dict]:
    """이 묶음의 공급자 — 선호 순서대로, 그날 막힌 공급자(`off`)·주간 예산을 다 쓴 공급자·소진(신호
    없음 포함)을 건너뛴다. Codex는 묶음마다 다시 조회한다. (공급자, Codex 조회, 건너뛴 까닭)."""
    limits, skipped = None, {}
    for who in [p for p in PROVIDERS if p in commands] + [p for p in commands if p not in PROVIDERS]:
        if who in off:
            skipped[who] = off[who]
            continue
        if budgets.get(who, 1) <= 0:
            skipped[who] = "budget"
            continue
        if who == "codex":
            # 실행기가 띄울 바이너리로 묻는다 — Desktop이 갱신되면 명령 파일의 옛 경로는 지워진다.
            # 쓸 수 없는 명령은 묻지 않는다: 그 묶음이 `invalid_command`로 알린다.
            checked = growth.check_command(commands[who])
            if checked["ok"] and (limits := codex_limits(checked["executable"]))["exhausted"]:
                skipped[who] = "exhausted" if limits["signal"] else "no_signal"
                continue
        return who, limits, skipped
    return None, limits, skipped


def _batch(command: list[str], kind: str, size: int, **args) -> dict:
    """묶음 하나. 다른 실행이 잠금을 쥐었으면(`busy`) 기다렸다가 한 번 다시 시도한다."""
    for wait in (True, False):
        result = growth.run(command, limit=size, timeout=SCOPE_TIMEOUT if kind == "scope" else LIGHT_TIMEOUT,
                            queues=("scope_jobs",) if kind == "scope" else LIGHT, scope_rounds=SCOPE_ROUNDS,
                            **args)
        if result.get("state") != "busy" or not wait:
            return result
        time.sleep(BUSY_WAIT)
    return result


def daily(commands: list[list[str]], *, invocation: str = "scheduled") -> dict:
    """하루 정기 성장 실행. 명령은 공급자마다 하나다(Codex, 그리고 대신 돌 Claude 워커)."""
    started = time.monotonic()
    by: dict = {}
    for command in commands:
        who = provider(command) or "other"      # 한도도 예산도 모르는 명령
        if who in by:
            raise ValueError(f"two growth commands for one provider ({who})")
        by[who] = command
    errors = []
    # 포착은 모델을 부르지 않는다 — 공급자가 모두 소진인 날에도, 주간 측정보다 먼저 한 번 한다.
    # 실패는 묶음을 막지 않지만 그날의 실패다. 이미 적힌 같은 오류(원본이 지워진 대화 등)는 대화
    # 상태와 `integration status`에 남아 있다 — 날마다 실패로 세면 새 실패가 묻힌다.
    try:
        captured = integration.capture_known()
    except Exception as exc:
        captured = {"captures": [], "errors": [{"error": f"{type(exc).__name__}: {exc}"}]}
    failed = captured["errors"] + [c for c in captured["captures"] if c.get("new_error")]
    if failed:
        errors.append({"capture": failed})
    rows = growth._records()
    now = datetime.now(timezone.utc).astimezone()
    day = now.date().isoformat()
    last = (_adjustments(rows) or [None])[-1]
    # 주는 날짜로 센다. 조정 시각은 포착을 마친 뒤에 적혀 날마다 몇 분씩 달라서, 시각으로 재면 같은
    # 시각의 매일 실행이 7일째에 몇 분 모자라 하루씩 밀린다(#258).
    fresh = last is None or (now.date() - datetime.fromisoformat(last["at"]).astimezone(now.tzinfo).date()).days >= WEEK
    adjustment = last
    if fresh:
        try:
            adjustment = adjust(rows, [p for p in PROVIDERS if p in by], now)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # 재지 못한 주는 남기지 않는다 — 다음 실행이 다시 잰다. 그동안은 지난 L(없으면 하한)로 돈다.
            errors.append({"adjustment": f"{type(exc).__name__}: {exc}"})
            fresh = False
    adjustment = adjustment or {"at": now.isoformat(timespec="seconds"), "limit": LIMIT_FLOOR, "providers": {}}
    recorded = fresh                 # 새로 정했으면 첫 묶음의 계획에 판단 근거를 싣는다
    limit = adjustment["limit"]
    spent = _spent(rows, datetime.fromisoformat(adjustment["at"]))[0]
    budgets = {p: b["budget"] - spent[p]["daily"] for p, b in adjustment.get("providers", {}).items()}
    plans = {r["rid"]: r for r in rows if r.get("kind") == "plan"}
    today = [(plans[r["manifest"]], r) for r in rows if r.get("kind") == "run" and r.get("manifest") in plans
             and (plans[r["manifest"]].get("daily") or {}).get("day") == day]
    exclude = {k for _, r in today for k in (r.get("progress") or {}).get("none", [])}
    # 같은 날 다시 불려도(수동 실행·재시작) L과 벽시계 예산은 그날 전체의 것이다. 한도에 거절된
    # 묶음은 다른 공급자가 다시 돌았으니 작업 수에 넣지 않는다 — 쓴 시간은 넣는다.
    done, earlier = {"scope": 0, "light": 0}, 0.0
    for plan, run in today:
        if plan["daily"].get("kind") in done and run.get("state") not in growth.WAITS:
            done[plan["daily"]["kind"]] += run.get("selected", 0)
        if (begun := _at(plan)) and (ended := _at(run)):
            earlier += max(0.0, (ended - begun).total_seconds())
    left, off = {"scope", "light"}, {}
    batches, stop, skipped = [], None, {}
    while stop is None:
        kind = _next_kind(done, left, limit)
        if time.monotonic() - started + earlier >= DAY_SECONDS:
            stop = "time_budget"
        elif done["scope"] + done["light"] >= limit:
            stop = "daily_limit"
        elif kind is None:
            stop = "empty"
        if stop:
            break
        who, limits, skipped = _choose(by, budgets, off)
        if who is None:
            # 소진·한도 거절은 대기다(#173). 예산만 다 쓴 것도 실패가 아니다.
            stop = ("limited" if {"exhausted", "no_signal", "limited"} & set(skipped.values()) else
                    "budget" if "budget" in skipped.values() else "unavailable")
            break
        info = {"day": day, "batch": len(batches) + 1, "kind": kind, "provider": who, "limit": limit,
                "limits": limits, "skipped": skipped, "budget_left": budgets.get(who)}
        if recorded:
            info["adjustment"] = adjustment
        result = _batch(by[who], kind, 1 if kind == "scope" else min(LIGHT_SIZE, limit - sum(done.values())),
                        capture=False, exclude=frozenset(exclude), daily=info, invocation=invocation)
        state = result.get("state")
        if result.get("manifest"):
            recorded = False         # 판단 근거는 이 계획에 남았다
            batches.append({k: result.get(k) for k in ("manifest", "state", "provider", "selected", "progress",
                                                      "usage", "returncode", "error")} | {"kind": kind})
            if who in budgets and (result.get("usage") or {}).get("measured"):
                budgets[who] -= result["usage"]["tokens"]
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
            done[kind] += result.get("selected", 0)
            exclude |= set(result["progress"]["none"])
            if growth.run_failed(result):
                errors.append({"manifest": result["manifest"], "state": state, "error": result.get("error")})
            if not result["progress"]["made"]:
                stop = "no_progress"
    # `done`과 `day_seconds`는 그날 앞선 실행까지 더한 값이다.
    return {"ok": not errors, "state": stop, "day": day, "limit": limit, "done": done,
            "elapsed_seconds": round(time.monotonic() - started),
            "day_seconds": round(time.monotonic() - started + earlier), "budget_left": budgets,
            "skipped": skipped, "adjustment": adjustment if fresh else {"at": adjustment["at"], "limit": limit},
            "capture": {"ok": not failed, "appended": sum(1 for c in captured["captures"] if c.get("appended")),
                        "failed": len(failed),
                        "known_errors": sum(1 for c in captured["captures"] if not c["ok"] and not c["new_error"])},
            "batches": batches, "errors": errors}
