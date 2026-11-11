"""osk.growth_ledger — 성장 기록부와 그 행이 말하는 것 (Mechanism §9-4).

정기 실행과 대화 검토 fork가 남기는 계획·실행·판정의 기록을 읽는다: 손상 검사, 한 항목의 지금
보고, 실행이 실패였는가, 최신 정기 실행의 상태. 무엇을 고르고 어떻게 돌릴지는 위의 모듈이 정한다."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import core
from .vault import Vault


def ledger_path(vault: Vault) -> Path:
    """성장 기록부."""
    return vault.ledger / "growth.jsonl"


QUEUES = ("candidates", "scope_jobs", "organization_jobs", "eviction_jobs", "recheck_jobs")


# 실패가 아니라 대기다(#173): 다른 실행이 잠금을 쥐었거나(`busy`) 한도에 막혔다(`limited`).
WAITS = {"busy", "limited"}


def ledger_records(vault: Vault) -> list[dict]:
    rows = core.ledger_read(ledger_path(vault))
    errors = core.ledger_damage(rows, ledger_path(vault))
    for row in rows:
        if row.get("kind") not in {"plan", "review", "run", "eviction_review", "recheck_review"}:
            errors.append("unknown growth record kind")
        if row.get("kind") == "recheck_review" and row.get("outcome") not in {"escalated", "deferred"}:
            errors.append("unknown recheck review outcome")
        if row.get("kind") == "review" and row.get("outcome") not in {
                "preserved", "no_value", "deferred"}:
            errors.append("unknown growth review outcome")
        if row.get("kind") == "eviction_review" and row.get("outcome") not in {
                "node", "merged", "discarded", "deferred"}:
            errors.append("unknown eviction review outcome")
    if errors:
        raise ValueError("growth ledger damaged: " + "; ".join(errors[:5]))
    return rows


def latest_report(rows: list[dict], value: str, field: str, par=None) -> dict | None:
    """한 항목의 지금 보고. 앞 선택(manifest)을 인과로 이은 선택이 있으면 앞 선택의 보고는
    기록으로만 남는다 — 옛 선택의 보고가 늦게 붙어 인과 극대가 되어도 새 선택의 보고를
    되돌리지 않는다. 서로 잇지 않은 선택(동기화 전의 두 기기)은 함께 남고, 그 보고가
    갈리면 정하지 않는다(Mechanism §3 1항). 같은 보고를 두 기기가 따로 적었으면(이어 적기
    등) 갈린 것이 아니므로 하나로 본다. 같은 선택 안에서는 인과상 나중 기록이 정한다.
    `par`는 같은 `rows`의 `effective_parents`다 — 여러 항목을 판정할 때 한 번만 잰다."""
    par = core.effective_parents(rows) if par is None else par
    plans = {r["manifest"] for r in rows
             if r.get(field) == value and isinstance(r.get("manifest"), str)}
    anc = {m: core.ancestors(m, par) for m in plans}
    kept = {m for m in plans if not any(m in anc[o] for o in plans if o != m)}
    top = core.causal_maxima(rows, value, par, field, candidate=(
        (lambda r: r.get("manifest") in kept) if plans else None))
    said = lambda r: {k: v for k, v in r.items() if k not in ("rid", "parents", "at")}
    return top[0] if top and all(said(r) == said(top[0]) for r in top) else None


def daily_status(vault: Vault, days: int = 3) -> dict:
    """최신 정기 실행 — `ran`은 최근 `days`일 안에 돌았는가, `active`는 그날 재검토를 맡았는가(Mechanism
    §4-1 4항), `last`는 최신 계획의 시각과 상태(결과가 없으면 None)다. 맡았다는 것은 그날 재검토 큐를 채운
    묶음(Scope 통합 묶음이 아닌 것)이 실패 없이 결과를 냈고, 재검토 후보가 있었다면 그중 재검토 작업을 고른 묶음이
    있으며, 고른 재검토 작업을 모두 닫거나 보류로 판정했다는 뜻이다(#301). 다른 작업이 자리를 다 써서
    재검토를 하나도 고르지 못한 날은 맡지 않은 것이다(#312 리뷰). 대기(잠금·한도) 묶음은 다른 공급자가
    다시 돌았으므로 보지 않는다. 정돈처럼 여러 번에 나눠 끝나는 작업이 남은 것은 재검토를 대화 검토 fork에
    넘길 까닭이 아니다. 적체 일괄 실행(`daily.backlog`)은 정기 실행이 아니다. 실패하거나 결과가 없는 새
    계획은 과거 성공으로 덮지 않는다."""
    now = datetime.now(timezone.utc)
    rows = ledger_records(vault)
    plans = [r for r in rows if r.get("kind") == "plan" and r.get("work_context", "daily") == "daily"
             and not (r.get("daily") or {}).get("backlog")]
    runs = {r.get("manifest"): r for r in rows if r.get("kind") == "run"}
    try:
        latest = plans[-1]
        result = runs.get(latest["rid"])
        at = (result or latest)["at"]
        ran = now - timedelta(days=days) <= datetime.fromisoformat(at) <= now
        day = (latest.get("daily") or {}).get("day")
        same = [p for p in plans if day and (p.get("daily") or {}).get("day") == day] or [latest]
        judged = [p for p in same if (p.get("daily") or {}).get("kind") != "scope"
                  and (runs.get(p["rid"]) or {}).get("state") not in WAITS]
        queued = any(len(p.get("recheck_jobs") or []) + ((p.get("queued_not_selected") or {}).get("recheck_jobs") or 0)
                     for p in judged)
        picked = [p for p in judged if p.get("recheck_jobs")]
        healthy = lambda r: bool(r) and (r.get("ok") is True or not run_failed(r))
        active = (ran and bool(judged) and all(healthy(runs.get(p["rid"])) for p in judged) and (picked or not queued)
                  and all(_rechecks_done(p, runs.get(p["rid"]), rows) for p in picked))
        return {"ran": ran, "active": bool(active),
                "last": {"at": at, "state": result.get("state") if result else None}}
    except (IndexError, KeyError, TypeError, ValueError):
        return {"ran": False, "active": False, "last": None}


def _rechecks_done(plan: dict, result: dict | None, rows: list[dict]) -> bool:
    """그 묶음이 고른 재검토 작업을 모두 닫거나 보류로 판정했는가. 실패한 실행은 판정하지 못한 것이다."""
    if not result:
        return False
    if result.get("ok") is True:
        return True
    if run_failed(result):
        return False
    deferred = {r.get("key") for r in rows if r.get("kind") == "recheck_review"
                and r.get("manifest") == plan["rid"] and r.get("outcome") == "deferred"}
    outcomes = result.get("recheck_outcomes") or {}
    return all((outcomes.get(j["key"]) or {}).get("status") == "complete" or j["key"] in deferred
               for j in plan.get("recheck_jobs", []))


def daily_active(vault: Vault, days: int = 3) -> bool:
    """최신 정기 실행일이 최근 `days`일 안에 재검토를 맡았는가(`daily_status`)."""
    return daily_status(vault, days)["active"]


def run_failed(result: dict) -> bool:
    """실행이 실패했는가 — 작업이 남은 것(`incomplete`)과 가른다. `ok`는 고른 작업을 모두
    끝냈다는 뜻이라, 여러 번에 나눠 끝나는 정돈이 남아도 거짓이다(Mechanism §9-4 4항).
    실패는 돌지 못했거나(잠김·준비 실패), 작업자가 정상 종료하지 않았거나, 결과 묶음을
    적용하지 못한 것이다. 잠금 대기(`busy`)와 한도 거절(`limited`)은 실패가 아니라 대기다(#173)."""
    if result.get("state") in WAITS:
        return False
    if result.get("kind") != "run":
        return not result.get("ok", False)
    final = result.get("final_reviews") or {}
    return (result.get("returncode") != 0 or bool(result.get("error"))
            or final.get("state") in {"rejected", "not_applied"} or bool(final.get("errors")))
