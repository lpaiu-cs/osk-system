"""osk.worker_usage — 작업자의 공급자와 그 출력이 말하는 것 (Mechanism §9-4 5항).

명령이 어느 공급자의 것인지(`provider`), 공급자의 한도 창이 어떤지(`codex_limits`), 작업자가 끝내지 못한
까닭이 한도 거절인지(`limited`), 출력에 적힌 요청들의 캐시 적중이 어땠는지(`usage`). 하루의 예산과 묶음은
`growth_daily`가 정한다."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import threading
from . import harness as adapters, worker_preflight, worker_process
from .vault import Vault


PROVIDERS = ("codex", "claude")          # 선호 순서 — Codex 예산을 넘는 몫은 Claude가 맡는다


MISS_BELOW = 0.9                         # 적중률(캐시 읽기 ÷ 입력)이 이보다 낮은 요청이 미스다


NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def provider(command: list[str]) -> str | None:
    """명령의 공급자 — 실행 파일 이름(`codex`·`claude`)이다. 그 밖의 명령은 한도도 예산도 모른다."""
    name = Path(command[0]).stem.lower() if command else ""
    return name if name in PROVIDERS else None


def codex_limits(vault: Vault, executable: str, timeout: float = 30) -> dict:
    """`codex app-server`의 `account/rateLimits/read` — 모델을 부르지 않는다(codex-cli 0.154 확인).
    소진은 크레딧까지 바닥난 상태다: `rateLimitReachedType`이나 `spendControlReached`가 서 있거나,
    포함 사용량도 크레딧도 없다. 주간 창이 100%여도 크레딧이 있으면 소진이 아니다. 답하지 못하면
    신호 없음이고 소진과 같이 다룬다."""
    requests = [{"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "osk", "version": "1"}}},
                {"method": "initialized"},
                {"id": 1, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}}]
    try:
        proc = subprocess.Popen([executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=vault.root, env=worker_preflight.subscription_env(),
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
    worker_process.stop_tree(proc)
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


def output_events(output: Path) -> list[dict]:
    rows = []
    for line in output.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def limited(vault: Vault, output: Path, command: list[str]) -> bool:
    """끝내지 못한 작업자가 한도에 막혔는가 — 실패가 아니라 대기다(#173). Claude는 조직 설정으로
    초과 사용이 꺼져 있어 100%에서 거절하고, 그 거절을 `rate_limit_event`(status `rejected`)로 낸다.
    Codex는 크레딧까지 바닥나야 거절하므로, 끝난 뒤 다시 조회해 소진이면 거절로 본다. 조회가 답하지
    못하면 거절이 아니다 — 실행 전 선택은 신호 없음을 소진과 같이 다루지만, 이미 실패한 작업의 까닭으로
    확정하면 작업자 오류가 대기로 묻힌다."""
    who = provider(command)
    try:
        if who == "claude":
            return any(e.get("type") == "rate_limit_event"
                       and (e.get("rate_limit_info") or {}).get("status") == "rejected" for e in output_events(output))
    except OSError:
        return False
    if who != "codex":
        return False
    limits = codex_limits(vault, command[0])
    return limits["signal"] and limits["exhausted"]


def _claude_requests(output: Path) -> tuple[list, dict | None]:
    """요청마다 (입력 전체, 캐시 읽기, 출력) — 한 메시지의 부분들은 같은 id로 따로 나온다. 합계는
    `result.usage`다: `result.modelUsage`는 이어받은 원 대화의 누계를 담는다."""
    messages, total = {}, None
    for e in output_events(output):
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
    tid = next((e.get("thread_id") for e in output_events(output) if e.get("type") == "thread.started"), None)
    paths = adapters.get("codex").transcripts(tid) if isinstance(tid, str) and tid.isprintable() and tid else []
    if len(paths) != 1:
        return [], None
    rows = output_events(paths[0])
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
