"""osk.evictions — 퇴출 기록부 (Mechanism §9-2 12항)와 정돈 실행 (§9-3).

scope 기억의 상한 초과 거부 **직후의 첫 성공한 쓰기**가 저장본에서 덜어 낸
구간을 `evict`로 남기고, 그 처분을 `settle`로 남긴다. 엔진은 무엇도 자르거나
요약하지 않는다 — **호출자가 스스로 뺀 것을 적을 뿐**이고 저장본은 호출자가
보낸 그대로다. 거부와 무관한 평소의 정리는 기록하지 않는다 — 그것은 자리값
못하는 엔트리를 지우는 정상이고, 상한에 밀려 **무엇이든** 잘라 내는 것과
다르다.

왜 기록하는가: 거부문은 (1) 정리 → (2) 증류의 순서를 주는데 정리는 언제나
성공하므로 (2)에 닿는 일이 없었다(14일 실측: 정리 93% · 증류 7%). 처방은
"자를 수 없게"가 아니라 **"잘린 것이 사라지지 않게"**다 — 근거와 기준선은 사료
`2026-09-02-eviction-ledger`에 있다.

처분은 세션의 시작에서 한다(§9-3 1항). 별도 스케줄러가 아니라 SessionStart
훅이 오래된 것부터 K개를 싣고 첫 도구 호출에 처분을 함께 실으라고 지시한다.
벽이 아니다 — 본 작업이 먼저면 넘어가도 되고 항목은 대장에 남는다. 건너뛴
것은 `osk status`가 보이고, N일을 넘으면 훅이 "밀렸다"를 앞세우며, 그래도
밀리면 `osk tidy prompt`가 전용 세션의 프롬프트를 낸다(3항).

두 기록 모두 §3 1항의 공통 규율(rid·parents·union merge)을 따르고 어느 행도
지우지 않는다 — 처분 뒤에도 "무엇이 잘려 어디로 갔는가"가 남는다.
"""
from __future__ import annotations
import re
import time
from datetime import datetime

from .core import (EVICTIONS, RID_RE, ledger_read, ledger_damage, ledger_append,
                   ledger_anchor_index, mutation_lock, _rid_parts, _rid_key)
from . import graph

KINDS = ("evict", "settle")
OUTCOMES = ("node", "merged", "discarded")
K = 3            # §9-3 1항 — 세션 시작에 싣는 미처분 항목 수 (조문 개정으로 조정)
N_DAYS = 14      # §9-3 3항 — 이 나이를 넘으면 "정돈이 밀렸다"를 앞세운다
_DAY_MS = 86_400_000


# ── 덜어 낸 구간 ────────────────────────────────────────────────────────

def _lines(s: str) -> list[str]:
    return [ln.rstrip() for ln in s.split("\n") if ln.strip()]


def removed_lines(old: str, new: str) -> str:
    """`old`의 줄 중 `new`에 남지 않은 줄 — 순서를 지키고 같은 줄은 한 번.

    **두 쓰기 방식 모두 저장본의 전후로 잰다** — 전체 치환이든 앵커 일괄이든
    "저장본에서 덜어 낸 구간"(§9-2 12항)은 치환 전 정본과 치환 뒤 정본의
    차이이고, 원시 `edits`의 `old_text`가 아니다. 앵커 일괄은 앞 연산이 만든
    문자열을 뒤 연산의 앵커로 쓸 수 있어, `old_text`를 모으면 **저장된 적 없는
    중간 문자열**이 퇴출된 것처럼 적히고 그 문자열은 비밀값 필터를 지나지
    않았다(리뷰 P1). 저장본은 필터를 지난 것만 담는다.

    줄이 단위다. scope 기억은 줄 단위 엔트리이고, 고쳐 쓴 줄은 옛 줄 그대로
    남으므로 처분에서 폐기로 판정된다 — 잘린 것을 놓치는 쪽보다 그 편이 싸다."""
    keep = set(_lines(new))
    out: list[str] = []
    for ln in _lines(old):
        if ln not in keep and ln not in out:
            out.append(ln)
    return "\n".join(out)


# ── 대장 ─────────────────────────────────────────────────────────────────

def record_evict(scope: str, session: str, text: str) -> dict:
    """거부 직후의 첫 성공한 쓰기가 덜어 낸 구간. 잠금 순서는 호출부가 쥔
    mutation → 대장이다(`ledger_append`가 대장 잠금을 잡는다)."""
    return ledger_append(EVICTIONS, {"kind": "evict", "scope": scope,
                                     "session": session, "text": text})


def records() -> list[dict]:
    """전 기록. 구조 손상이면 판정을 세우지 않는다(§3 2항 — fail-closed)."""
    recs = ledger_read(EVICTIONS)
    dmg = ledger_damage(recs, EVICTIONS)
    if dmg:
        raise ValueError("퇴출 기록부 손상 — 수동 복구 절차 필요 (Mechanism §3 8항): "
                         + "; ".join(dmg[:3]))
    return recs


def row_ms(r: dict) -> int | None:
    """기록의 시각 — rid(UUIDv7)의 앞 48비트가 정본이고 `at`은 예비다.
    계기(`osk_turn_ledger.py`)와 같은 판정이라 둘이 같은 나이를 낸다."""
    rid = str(r.get("rid", ""))
    if re.match(RID_RE, rid):
        return _rid_parts(rid)[0]
    at = r.get("at")
    if at:
        try:
            return int(datetime.fromisoformat(str(at)).timestamp() * 1000)
        except ValueError:
            return None
    return None


def unsettled(scope: str | None = None,
              recs: list[dict] | None = None) -> list[dict]:
    """미처분 `evict` — 오래된 것부터. 처분은 그 rid를 `of`로 가리키는 `settle`이
    하나라도 있는 것이다(둘이면 다기기 동시 처분이고 손상이 아니다 — §9-3 4항)."""
    recs = records() if recs is None else recs
    done = {r.get("of") for r in recs if r.get("kind") == "settle"}
    rows = [r for r in recs
            if r.get("kind") == "evict" and r.get("rid") not in done
            and (scope is None or r.get("scope") == scope)]
    rows.sort(key=lambda r: _rid_key(r["rid"]))
    return rows


def age_days(r: dict, now_ms: int | None = None) -> int:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    ms = row_ms(r)
    return 0 if ms is None else max(0, (now_ms - ms) // _DAY_MS)


def status(now_ms: int | None = None) -> dict[str, dict]:
    """scope별 미처분 수·가장 오래된 나이·밀림 여부 (§9-3 3항). 미처분이 있는
    scope만 싣는다."""
    out: dict[str, dict] = {}
    for r in unsettled():
        s = out.setdefault(r["scope"], {"unsettled": 0, "oldest_days": 0})
        s["unsettled"] += 1
        s["oldest_days"] = max(s["oldest_days"], age_days(r, now_ms))
    for s in out.values():
        s["overdue"] = s["oldest_days"] > N_DAYS
    return out


def settle(of: str, outcome: str, target: str | None = None) -> dict:
    """처분 기록. `outcome`은 node·merged·discarded, `target`은 노드 제목이며
    폐기에는 없다. 노드가 서 있지 않으면 적지 않는다 — 처분은 한 일의 기록이지
    하겠다는 약속이 아니다. `of`는 잠금 안에서 다시 확인한다."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome은 {'·'.join(OUTCOMES)} 중 하나다 — `{outcome}`은 아니다")
    target = (target or "").strip() or None
    if outcome == "discarded" and target:
        raise ValueError("폐기(discarded)에는 target이 없다 — 어디로도 가지 않았다")
    if outcome != "discarded" and not target:
        raise ValueError(f"{outcome}에는 target(노드 제목)이 필요하다 — "
                         f"어디로 갔는지가 처분의 내용이다")

    def expect(recs: list[dict]) -> str | None:
        if not any(r.get("kind") == "evict" and r.get("rid") == of for r in recs):
            return (f"`{of}`는 퇴출 기록부의 evict가 아니다 — settle은 있는 evict만 "
                    f"가리킨다(`osk tidy list`)")
        return None

    rec: dict = {"kind": "settle", "of": of, "outcome": outcome}
    if target:
        rec["target"] = target
    # 노드 확인은 **변경 잠금 안**에서, 그리고 후보 파일이 아니라 **판독되는
    # 비모호 노드**로 한다(리뷰 P2). 파손 파일 하나가 그 이름의 임자가 되면
    # 증류된 적 없는 조각이 정돈 큐에서 영구히 빠진다. 잠금 순서는 다른
    # 모듈과 같다 — 변경 잠금 → 대장 잠금.
    with mutation_lock():
        if target:
            r = graph.Index().resolve(target)
            if r[0] == "ambiguous":
                raise ValueError(f"`{target}`는 동명이 둘 이상이라 어느 노드인지 정해지지 "
                                 f"않는다 — 처분을 적지 않았다")
            if r[0] != "node":
                raise ValueError(f"노드 `{target}`이 없다(파손 파일은 노드가 아니다) — "
                                 f"처분은 노드가 선 뒤에 적는다. 제목은 파일 이름 "
                                 f"그대로, 경로 없이")
        return ledger_append(EVICTIONS, rec, expect=expect)


def schema_errors(recs: list[dict]) -> list[str]:
    """검증기용 — 기록 동일성·kind별 필수 필드는 전 구간, parents 계약은 앵커
    이후(승인 기록부와 같은 규율). `settle`의 `of`는 어느 행이든 evict여야
    하고, `target`은 폐기에는 없고 그 밖에는 있어야 한다."""
    errs: list[str] = []
    anchor = ledger_anchor_index(recs)
    known: set[str] = set()
    evicts = {r.get("rid") for r in recs if r.get("kind") == "evict"}
    for i, r in enumerate(recs):
        where = f"행{i+1}"
        rid = r.get("rid")
        if not re.match(RID_RE, str(rid)):
            errs.append(f"{where}: rid 형식 위반 {rid}")
        kind = r.get("kind")
        if kind not in KINDS:
            errs.append(f"{where}: 미정의 kind {kind}")
        need = (("scope", "session", "text", "at") if kind == "evict"
                else ("of", "outcome", "at"))
        for k in need:
            if k not in r:
                errs.append(f"{where}: 필수 필드 누락 {k}")
        if kind == "settle":
            if r.get("outcome") not in OUTCOMES:
                errs.append(f"{where}: 미정의 outcome {r.get('outcome')}")
            elif (r["outcome"] == "discarded") == bool(r.get("target")):
                errs.append(f"{where}: target은 폐기에는 없고 그 밖에는 있어야 한다")
            if r.get("of") not in evicts:
                errs.append(f"{where}: 미지의 evict {r.get('of')}")
        if anchor is not None and i >= anchor:
            if not isinstance(r.get("parents"), list) or (not r["parents"] and i != 0):
                errs.append(f"{where}: parents 부재")
            else:
                for pp in r["parents"]:
                    if not isinstance(pp, str):
                        errs.append(f"{where}: parents 원소가 문자열이 아님 {pp!r}")
                    elif pp not in known:
                        errs.append(f"{where}: 미지의 parent {pp}")
        if isinstance(rid, str):
            known.add(rid)
    return errs


# ── 정돈 문안 — 훅과 전용 세션이 같은 말을 쓴다 ──────────────────────────

def transit_titles() -> list[str]:
    """Workbench의 경유 노드 제목 — 정돈의 다른 대상(Workbench 계약 §3)."""
    idx = graph.Index()
    return sorted(stem for stem, (_p, kind) in idx.names.items()
                  if kind and kind[0] == "workbench-transit")


def _item(r: dict, now_ms: int | None) -> str:
    body = str(r.get("text", "")).strip().replace("\n", "\n  ")
    return (f"- `{r['rid']}` · {age_days(r, now_ms)}일 전 · "
            f"세션 `{r.get('session')}`\n  {body}")


def _exits(scope: str, python: str, engine: str) -> str:
    return (f"출구는 셋이다(§9-3 2항) — 노드로 증류(`create_node`, 착지 `= Scope/{scope}`; "
            f"여러 scope에 재사용되면 Domain) · 기존 노드에 통합(`search`로 찾아 "
            f"`update_node`) · 폐기. 어느 쪽이든 **settle을 적어야 처분이다**:\n"
            f"  PYTHONPATH={engine} {python} -m osk.cli tidy settle <rid> node|merged "
            f"--target \"<노드 제목>\"\n"
            f"  PYTHONPATH={engine} {python} -m osk.cli tidy settle <rid> discarded\n"
            f"증류·통합이 보호영역의 노드를 바꾸면 그 차이는 변경집합으로 남아 승인을 "
            f"받는다 — 정돈이 승인을 대신하지 않는다.")


def hook_block(scope: str, python: str, engine: str,
               now_ms: int | None = None) -> tuple[str, str]:
    """SessionStart 훅의 두 조각 — (밀림 경고, 정돈 블록). 미처분이 없으면 둘 다
    빈 문자열이다. 경고는 N일을 넘었을 때만 있고 **주입문의 맨 앞**에 선다."""
    rows = unsettled(scope)
    if not rows:
        return "", ""
    oldest = age_days(rows[0], now_ms)
    banner = ""
    if oldest > N_DAYS:
        banner = (f"[osk 정돈이 밀렸다 — = Scope/{scope} 미처분 {len(rows)}건, 가장 "
                  f"오래된 것 {oldest}일 (기준 {N_DAYS}일)] 아래 항목을 먼저 처분하라. "
                  f"그래도 밀리면 `osk tidy prompt`가 전용 세션의 프롬프트를 낸다.")
    shown = rows[:K]
    lines = [
        f"[osk 정돈 — = Scope/{scope} 미처분 퇴출 {len(rows)}건 중 오래된 {len(shown)}건]",
        "scope 기억에서 상한에 밀려 잘려 나간 조각이다(Mechanism §9-2 12항). "
        "**첫 도구 호출에 처분을 함께 실어라** — 벽이 아니다: 본 작업이 먼저면 "
        "넘어가도 되고, 항목은 대장에 남는다.",
    ]
    lines += [_item(r, now_ms) for r in shown]
    if len(rows) > len(shown):
        lines.append(f"- … 외 {len(rows) - len(shown)}건 (`osk tidy list`)")
    transit = transit_titles()
    if transit:
        lines.append("Workbench 경유 노드(정돈 대상 — Workbench 계약 §3): "
                     + " · ".join(f"[[{t}]]" for t in transit))
    lines.append(_exits(scope, python, engine))
    return banner, "\n".join(lines)


def tidy_prompt(scope: str | None, python: str, engine: str,
                now_ms: int | None = None) -> str:
    """전용 정돈 세션의 프롬프트 (§9-3 3항) — 사용자가 새 세션에 붙여 넣거나
    스케줄러가 `claude -p`로 띄운다. 한 세션은 한 scope다: scope를 안 주면
    가장 오래 밀린 scope를 고른다. 세션 키는 그 scope의 정본 키다."""
    st = status(now_ms)
    if not st:
        return "미처분 퇴출 항목이 없다 — 정돈할 것이 없다."
    if scope is None:
        scope = max(st, key=lambda s: (st[s]["oldest_days"], st[s]["unsettled"], s))
    elif scope not in st:
        return (f"= Scope/{scope}에는 미처분 항목이 없다. 있는 scope: "
                + ", ".join(sorted(st)))
    rows = unsettled(scope)
    session = rows[-1].get("session")
    lines = [
        f"이 세션은 `= Scope/{scope}`의 **전용 정돈 세션**이다(Mechanism §9-3 3항). "
        f"본 작업은 없다 — 아래 미처분 퇴출 {len(rows)}건을 전부 처분하고 끝낸다.",
        f"osk 도구를 부를 때 `session=\"{session}\"`을 쓴다 — 이 scope의 정본 키다. "
        f"승인은 우회하지 않는다.",
        "",
    ]
    lines += [_item(r, now_ms) for r in rows]
    transit = transit_titles()
    if transit:
        lines += ["", "Workbench 경유 노드(정돈 대상 — Workbench 계약 §3): "
                  + " · ".join(f"[[{t}]]" for t in transit)]
    lines += ["", _exits(scope, python, engine)]
    return "\n".join(lines)
