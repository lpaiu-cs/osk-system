"""osk.validate — 검증기 수트 (F.2).

구현 근거: 시행령 §11 — 파일 정본에서 재계산, 자동 집행은 검증기 활성화
후에만(현재 활성화된 자동 집행 없음 — 전 검증기는 요청 시 실행·보고 전용),
실패는 보류·보고, 수동 복구 경로 상존.

보고는 pass·fail·skipped 세 갈래다. **skipped는 pass가 아니다** — 선행
판독이 실패해 검사가 성립하지 않은 세그먼트이며, 빈 입력을 검사해 헛 PASS를
찍는 대신 보류로 남긴다.
"""
from __future__ import annotations
import json, re
from pathlib import Path

from .core import (ROOT, SIGNATURES, CANDIDATES, PINS, ROUTING, LEDGER,
                   VALIDATORS, CASE_RE, RID_RE, ID_RE, ledger_read,
                   ledger_damage, ledger_anchor_index, resolve_one)
from . import contract, graph, signatures, approvals, authority, secrets

# 사건 파일 머리의 고정 헤더 (Mechanism §4 4항). pre_sign은 구체제 필드로,
# 새 기록에는 두지 않으므로 필수에서 뺐다(기존 사건에는 사료로 남는다).
CASE_HEADER = ("case_no", "status", "parties", "docketed_at",
               "verdict", "verdict_at", "applied", "schema_version")
CASE_STATUS = ("docketed", "adjudicated")
CASE_VERDICT = ("기각", "수정", "존치")


def run() -> dict:
    rep = {"pass": [], "fail": [], "skipped": []}

    def ok(name, errs):
        (rep["pass"].append(name) if not errs
         else rep["fail"].append({name: errs[:20]}))

    def skip(name, why):
        rep["skipped"].append({name: why})

    def guard(name, fn):
        """검증기는 어떤 입력에도 죽지 않는다(시행령 §11) — 예외도 실패로 보고."""
        try:
            ok(name, fn())
        except Exception as e:
            rep["fail"].append({name: [f"검사 자체 실패: {e}"]})

    idx = graph.Index()

    # 1. 노드 계약 (시행령 §1 · Mechanism §2) — 색인이 못 읽은 파일도 오류다
    broken = getattr(idx, "broken", None) or {}
    errs = [f"{stem}: 파싱 실패 {why}" for stem, why in sorted(broken.items())]
    ids = {}
    for stem, (p, kind) in idx.nodes.items():
        try:
            n = idx.node(p)
        except Exception as e:
            errs.append(f"{stem}: 파싱 실패 {e}")
            continue
        for e in contract.validate(n):
            errs.append(f"{stem}: {e}")
        if n.id in ids:
            errs.append(f"id 중복 {n.id}: {stem} & {ids[n.id]}")
        ids[n.id] = stem
    ok(f"노드 계약 ({len(idx.nodes) + len(broken)}개)", errs)

    # 2. 배치 (Mechanism §1)
    guard("공간 배치·`_` 규칙", graph.layout_violations)

    # 3. 참조 위상 (헌법 8조 3항)
    guard("참조 위상", lambda: graph.topology_check(idx))
    ok("동명 노드 중복", [f"{s}: {v}" for s, v in idx.dup_stems.items()])
    try:
        rep["warnings"] = {"dangling_refs": graph.dangling_refs(idx)}
    except Exception as e:
        rep["warnings"] = {"dangling_refs": []}
        skip("미해석 참조 경고", f"산출 실패: {e}")

    # 4. 승인 기록부 (시행령 §6 · Mechanism §3) — 보호영역 현황.
    #    판독 실패는 플래그로 남긴다 — 빈 recs를 검사한 헛 PASS를 막는다.
    errs, arecs, appr_ok = [], [], True
    try:
        arecs = approvals.records()
    except Exception as e:
        errs.append(str(e))
        appr_ok = False
    if appr_ok:
        try:
            rep["protected_regions"] = {
                r: approvals.state(r) for r in approvals.protected_regions()}
        except Exception as e:
            errs.append(str(e))
    ok(f"승인 기록부 ({len(arecs)}행)", errs)

    # 5. 대장 판독 (Mechanism §3 1항 공통). update.jsonl도 이 규율을 따르는
    #    대장이다. signatures.jsonl은 구체제 사료로 판독만 한다(무결 검사 대상).
    errs, ledgers = [], ([(approvals.APPROVALS, arecs)] if appr_ok else [])
    for p in [SIGNATURES, CANDIDATES, PINS, ROUTING, VALIDATORS,
              approvals.MOVES,
              LEDGER / "migration" / "events.jsonl", LEDGER / "rechecks.jsonl",
              LEDGER / "update.jsonl"]:
        try:
            ledgers.append((p, ledger_read(p)))
        except Exception as e:
            errs.append(str(e))
    ok("대장 JSON 무결", errs)

    # 6. 대장 구조 손상 — rid 부재·형식 위반·중복 (Mechanism §3 7항 · §3 2항).
    #    중복 rid는 기록의 동일성을 깨뜨려 판정을 뒤집으므로 전 대장에 건다.
    dmg = []
    for p, rs in ledgers:
        dmg += ledger_damage(rs, p.name)
    ok(f"대장 rid 유일·형식 ({len(ledgers)}개 대장)", dmg)
    if not appr_ok:
        skip("승인 대장 rid 유일·형식", "승인 대장 판독 실패 — 검사 불성립")

    # 7. 위임 성립 요건 (시행령 §5) — 대장 손상에도 죽지 않는다
    errs = []
    try:
        dels = authority.enumerate_delegations()
        for d in dels:
            if not d["effective"]:
                errs.append(f"{d['title']}: 절={d['valid_clause']} 승인본={d['approved']}")
        rep["delegations"] = len(dels)
    except Exception as e:
        errs.append(f"위임 검사 실패: {e}")
    ok("위임 3요건", errs)

    # 8. 비밀값 필터 fixture (Mechanism §9 2항)
    ok("비밀값 필터 양성/음성", secrets.self_test())

    # 9. 보호영역 생애 fixture — 매 검증 실행 (회귀 방지)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        guard("보호영역 생애 fixture", lambda: fixture_approval_lifecycle(td))

    # 10. 승인 기록부 정합성 (인과 극대 유일·승인본 해석 — 시행령 §6 7항)
    if appr_ok:
        guard("승인 기록부 정합(stale·승인본 해석)", approvals.integrity)
    else:
        skip("승인 기록부 정합", "승인 대장 판독 실패 — 판정 보류")

    # 11. 대장 스키마 — 기록 동일성·필수 필드는 **전 구간**, parents 계약만
    #     앵커(첫 parents 기록) 이후. 유산 구간을 무검증으로 두면 앵커 위에
    #     끼운 위조 행이 검증을 통째로 우회한다. 대상은 승인 기록부다.
    if appr_ok:
        errs = []
        anchor = ledger_anchor_index(arecs)
        known = set()
        for i, r in enumerate(arecs):
            where = f"행{i+1}"
            rid = r.get("rid")
            if not re.match(RID_RE, str(rid)):
                errs.append(f"{where}: rid 형식 위반 {rid}")
            for k in ("kind", "region", "at"):
                if k not in r:
                    errs.append(f"{where}: 필수 필드 누락 {k}")
            if r.get("kind") not in approvals.KINDS:
                errs.append(f"{where}: 미정의 kind {r.get('kind')}")
            if anchor is not None and i >= anchor:
                if not isinstance(r.get("parents"), list) or (not r["parents"] and i != 0):
                    errs.append(f"{where}: parents 부재")   # 빈 parents는 파일 첫 기록만 허용
                else:
                    for pp in r["parents"]:
                        if not isinstance(pp, str):
                            errs.append(f"{where}: parents 원소가 문자열이 아님 {pp!r}")
                        elif pp not in known:
                            errs.append(f"{where}: 미지의 parent {pp}")
            if isinstance(rid, str):
                known.add(rid)
        after = 0 if anchor is None else len(arecs) - anchor
        ok(f"승인 대장 스키마(전 {len(arecs)}행 · parents는 앵커 이후 {after}행)", errs)
    else:
        skip("승인 대장 스키마", "승인 대장 판독 실패 — 검사 불성립")

    # 12. 사건 파일 헤더 (Mechanism §4 3항) — 파싱 실패를 여기서 직접 보고한다
    #     (topology_check의 'conflicts 대상 부적격'으로 오보되지 않게).
    errs = []
    cdir = LEDGER / "case"
    cases = sorted(cdir.glob("CASE-*.md")) if cdir.is_dir() else []
    for f in cases:
        if not re.match(CASE_RE, f.stem):
            errs.append(f"{f.name}: 사건 번호 형식 위반")
        c = signatures.parse_case(f)
        if c is None:
            errs.append(f"{f.name}: 헤더 파싱 실패")
            continue
        for k in CASE_HEADER:
            if k not in c:
                errs.append(f"{f.name}: 필수 필드 누락 {k}")
        if str(c.get("case_no")) != f.stem:
            errs.append(f"{f.name}: case_no≠파일명 ({c.get('case_no')})")
        if str(c.get("status")) not in CASE_STATUS:
            errs.append(f"{f.name}: 미정의 status {c.get('status')}")
        v = c.get("verdict")
        if v is not None and v != "" and str(v) not in CASE_VERDICT:
            errs.append(f"{f.name}: 미정의 verdict {v}")
        if "parties" in c and not isinstance(c["parties"], list):
            errs.append(f"{f.name}: parties가 목록이 아님")
    ok(f"사건 파일 헤더 ({len(cases)}건)", errs)

    # 13. _raw 비밀값 (Mechanism §9 1항 — '기록 시 치환'의 집행 지점).
    #     보고에는 경로와 패턴 이름만 싣는다 — 비밀값 자체는 절대 싣지 않는다.
    errs = []
    # `_wm`도 함께 훑는다 — 통로에는 필터가 걸려 있지만(osk/wm.py), 통로 밖
    # 유입(수동 편집·구 엔진 기기에서의 동기화·나중에 추가된 패턴)은 그 필터를
    # 지나지 않는다. §9 1항이 검증기를 집행 지점으로 두는 이유가 그것이다.
    for d in sorted([*(ROOT / "= Scope").rglob("_raw"),
                     *(ROOT / "= Scope").rglob("_scope_memory")]):
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                errs.append(f"{p.relative_to(ROOT)}: 판독 실패 {e}")
                continue
            _, hits = secrets.filter_text(text)
            if hits:
                errs.append(f"{p.relative_to(ROOT)}: {sorted(set(hits))}")
    ok("_raw·_scope_memory 비밀값 미기록", errs)

    # 14. 정합성 검사 — 충돌 후보 (헌법 12조 1·2항). 기계 판정이 가능한 유형만
    #     검출해 **보고**한다. 상정·각하는 사용자 전속이므로 대장에 자동으로
    #     기록하지 않는다(헌법 12조 3항 · 시행령 §11 3항 — 자동 집행 없음).
    guard("정합성 검사(충돌 후보 없음)", lambda: conflict_candidates(idx))

    # 15. 외부 표면 (Mechanism §6-2). 규범이 선언한 도구 목록이 정본이고,
    #     구현이 그와 동치인지 + 권위 대장에 손대지 않는지를 대조한다.
    if declared_tools() is None:
        skip("외부 표면 계약", "Mechanism §6-2 도구 목록 부재 — 검사 불성립")
    else:
        guard("외부 표면 계약(도구 목록·권위 비노출)", surface_violations)
        guard("표면 린트(스키마 건전·가르침)", surface_lint)
        if _last_surface_cost:
            rep["surface_cost"] = dict(_last_surface_cost)

    # 16. 군집 허브 노드 (헌법 3조 8항 · 시행령 §3 6항 · Mechanism §6-1). 검사는 언제나
    #     수행해 보고하고, verdict 산입은 활성화 뒤에만 한다(시행령 §11
    #     2항·3항) — 활성화 순간 무엇이 FAIL이 될지 사용자가 미리 본다.
    try:
        co = cluster_overview_report(idx)
        rep["cluster_overview"] = co
        co_active = validator_active("cluster-overview")
        rep["cluster_overview_active"] = co_active
        co_errs = []
        for c, st in sorted(co.items()):
            if not st["overview"]:
                co_errs.append(f"{c}: 동명 허브 노드 없음")
            elif st["unreachable"]:
                co_errs.append(f"{c}: 허브 미도달 {st['unreachable']}개")
        if co_active:
            ok("군집 허브 노드", co_errs)
        elif co_errs:
            skip("군집 허브 노드", f"비활성 — 보고만 (위반 {len(co_errs)}건)")
        else:
            ok("군집 허브 노드", [])
    except Exception as e:
        rep["fail"].append({"군집 허브 노드": [f"검사 자체 실패: {e}"]})

    rep["verdict"] = "PASS" if not rep["fail"] else "FAIL"
    return rep


def validator_active(rule: str) -> bool:
    """검증기 규칙의 활성 여부 (Mechanism §6-1). 규칙별 인과 극대가 현재
    상태이고, 대장이 없거나 미확정(분기)이면 비활성이다 — fail-open이 아니라
    보고-전용으로 남는 쪽이 안전하다(시행령 §11 2항: 집행은 활성화 뒤에만)."""
    try:
        recs = ledger_read(VALIDATORS)
    except Exception:
        return False
    r = resolve_one(recs, rule, "rule")
    return bool(r and r.get("kind") == "activate")


def cluster_overview_report(idx: "graph.Index") -> dict:
    """군집 허브 노드 검사 (헌법 3조 8항 · 시행령 §3 6항) — 군집별 (a) 동명
    허브 노드 존재, (b) 허브에서 **Link의 방향**(헌법 8조 4항)을 따라
    출발했을 때 전 구성 노드의 도달 가능성. 도달은 허브가 가리키는 것으로
    성립하고, 노드가 허브를 가리키는 것으로는 성립하지 않는다.

    간선은 **본문 Link의 노드 대상만**이며 **군집 안으로 한정**한다 —
    Predicate Edge는 근거·사건의 관계라 세지 않고(`_outgoing_refs` 참조),
    군집 밖 대상은 위상 규칙의 몫이며, 비노드 대상(원자료·대장)은 노드
    그래프가 아니다. Workbench 구획은 자체 계약을 따르므로 제외한다(§6-1 3항).

    이 검사는 최상위 허브만 알고 하위 허브를 구별하지 않는다 — 시행령 §3
    7항의 "항해의 내부 노드는 허브뿐"과 "각 노드는 하나의 갈래에 속한다"는
    검사하지 않으며, 그 판정에는 하위 허브의 기계 식별이 선행한다.
    순회는 방문 집합 위에서만 전진하므로 상호·자기 참조의 순환에서도 종료가
    보장된다."""
    clusters: dict = {}
    for stem, (p, _k) in idx.nodes.items():
        rel = p.relative_to(ROOT)
        if "Workbench" in rel.parts or rel.parts[0] == "_governance":
            continue
        clusters.setdefault(p.parent, set()).add(stem)
    out = {}
    for cdir, members in clusters.items():
        key = str(cdir.relative_to(ROOT)).replace("\\", "/")
        ov = cdir.name
        if ov not in members:
            out[key] = {"overview": False, "nodes": len(members),
                        "unreachable": len(members)}
            continue
        seen, queue = {ov}, [ov]
        while queue:
            for t in _outgoing_refs(idx, queue.pop(), members) - seen:
                seen.add(t)
                queue.append(t)
        unreach = sorted(members - seen)
        st = {"overview": True, "nodes": len(members),
              "unreachable": len(unreach)}
        if unreach:
            st["orphans"] = unreach[:20]
        out[key] = st
    return out


def _outgoing_refs(idx: "graph.Index", stem: str, members: set) -> set:
    """`stem` 노드가 **본문 Link로** 가리키는 군집 구성원 (헌법 8조 4항).

    Predicate Edge는 세지 않는다. 도달이 `derived-from`을 세면 고아를 지우는
    가장 싼 길이 근거를 하나 더 다는 것이 되어, 검증기의 압력이 증거 계층으로
    샌다 — 근거는 조건부인데(헌법 9조 1항) 도달은 필수이므로, 필수를 조건부
    위에 얹지 않는다(Mechanism §6-1 3항).

    이름형·경로형 Link 모두 마지막 조각으로 접는다. 구성원이 아닌 대상 —
    군집 밖 노드·비노드 — 은 마지막 교집합에서 떨어진다."""
    n = idx.node(idx.nodes[stem][0])
    near = set()
    for t in set(n.wikilinks()):
        base = t.rsplit("/", 1)[-1].strip()
        near.add(base[:-3] if base.endswith(".md") else base)
    near.discard(stem)
    return near & members


# 상주 표면 비용의 상한 — 초과는 회귀다. 이 blob(도구의 이름·설명·스키마)은
# 클라이언트가 접속할 때 문맥에 들어가 세션 내내 모든 요청에 실려 다닌다.
# 그 성장이 조용히 일어나지 못하게 하는 **래칫**이 이 상수다: 설명이 자라든
# (가르침 — 글로 줄일 수 있다) 스키마가 자라든(인자 수·타입 복잡도 — 글로 못
# 줄인다) 여기를 올려야 하고, 올릴 때는 사유를 남긴다. 분해는 검증기 보고의
# `surface_cost`가 매 실행 싣는다 — 다음 상향 때 어느 쪽이 자랐는지 그 자리에서
# 보인다.
#   5000 → 5600: append_raw 노출 (`_raw/` 기록 통로의 표면 연결).
#   5600 → 6100: read_raw 노출 (기록의 명시 회상 — Mechanism §9 8항).
#   6100 → 6300: §6-2 7항 감사가 적발한 가르침 결손 보강 — `space`의 실제
#     형식, 결속 후 생략, 재호출 비안전성, 절단의 비대칭. 감사에서 이것들이
#     없어 실제로 막혔다(예산이 막는 것은 비대이지 필요한 가르침이 아니다).
#   6300 → 7000: working_memory(현 scope_memory) 노출 (Mechanism §9-2).
#   7000 → 7400: §6-2 7항 감사가 적발한 결손 셋 — `space`의 실제 범위,
#     비밀값이 성공하면서 치환된다는 것, `text:""`의 의미와 되돌리는 법.
#     함께 `overview`가 `clusters`를 '그대로 space에 넣으라'고 하던 것을
#     바로잡았다 — 한 도구의 설명이 다른 도구를 반박하고 있었다.
#
# (2026-08-22: 예전 머리 문구는 "막는 것은 설명의 비대이지 도구 수가 아니다"라
#  했으나 실측상 blob의 절반 이상이 스키마였다 — 이름과 실제가 어긋나 위
#  문구로 바로잡았다. 기계는 그대로다: 매 상향이 사유와 함께 남는 의도된
#  결정이 되는 것, 그것이 이 상수의 일이다.)
#   7400 → 7500: working_memory → scope_memory 개명 + 공유성 가르침(모든
#     세션과 기기가 같은 것을 본다 — 세션 한정 상태 금지). 실측으로 세션
#     한정 상태가 유입돼 개명했고, 그 이유가 설명에 실려야 재발을 막는다.
SCHEMA_BUDGET = 7500

# 마지막 표면 린트의 상주 비용 분해 — surface_lint가 채우고 run이 보고에 싣는다.
_last_surface_cost: dict | None = None


def surface_lint() -> list[str]:
    """발행 스키마의 **결정론적 불변식**. 이번 감사가 도출한 것들이며, 이 검사가
    잡는 것은 엔진의 옳음이 아니라 **표면의 가르침**이 낡는 일이다(10차 ④).

    실제 수행 시뮬레이션은 여기 넣지 않는다 — 비결정론적이고, 그 자리는 시험이
    아니라 §6-2 7항의 개정 관문이다."""
    import json as _json
    errs = []
    try:
        # 등록부를 직접 읽는다 — `list_tools()`는 코루틴이라 서버 자신의 이벤트
        # 루프 안에서 `asyncio.run`으로 부를 수 없다. 이 검사는 `run_validators`
        # 도구로도 불리므로 동기 경로여야 한다.
        import mcp_server as srv
        reg = srv.mcp._tool_manager._tools
        tools = [type("T", (), {"name": n, "description": t.description,
                                "inputSchema": t.parameters})
                 for n, t in sorted(reg.items())]
    except Exception as e:
        return [f"표면 판독 실패: {e}"]
    blob = _json.dumps([{"name": t.name, "description": t.description,
                         "inputSchema": t.inputSchema} for t in tools],
                       ensure_ascii=False)
    # 측정을 남긴다 — 보고(run)가 이 값을 싣는다. 판정과 보고가 딴 자로 재면
    # 조용히 갈라지므로, 재는 자리는 여기 하나다.
    global _last_surface_cost
    _last_surface_cost = {
        "total": len(blob), "budget": SCHEMA_BUDGET,
        "headroom": SCHEMA_BUDGET - len(blob),
        "description": sum(len(t.description or "") for t in tools),
        "schema": sum(len(_json.dumps(t.inputSchema, ensure_ascii=False))
                      for t in tools)}
    if len(blob) > SCHEMA_BUDGET:
        errs.append(f"상주 스키마 예산 초과: {len(blob)}자 > {SCHEMA_BUDGET}")
    for t in tools:
        s = t.inputSchema or {}
        props = s.get("properties") or {}
        req = set(s.get("required") or [])
        # ⓐ required ⊆ properties — 순진한 title 가지치기가 만든 자기모순의 고정
        if not req <= set(props):
            errs.append(f"{t.name}: required가 properties를 벗어난다: "
                        f"{sorted(req - set(props))}")
        # ⓑ pydantic 자동 title 주석 부재 (상주 예산의 낭비)
        for k, v in props.items():
            if isinstance(v, dict) and "title" in v:
                errs.append(f"{t.name}.{k}: 자동 생성 title 주석 잔존")
        # ⓒ 필수 인자는 설명이나 스키마 제약에 등장해야 한다 — 침묵 필수 금지
        for k in sorted(req):
            v = props.get(k) or {}
            constrained = any(x in v for x in
                              ("enum", "pattern", "minimum", "maximum",
                               "minLength", "maxLength", "minItems"))
            if k not in (t.description or "") and not constrained:
                errs.append(f"{t.name}.{k}: 필수인데 설명에도 스키마 제약에도 없다")
    # ⓓ search 결과 필드 계약 — `updated`는 실어야 하고(시기 필터), 서명
    #    폐지로 `signed`는 표면에서 사라져야 한다(그 필드로 권한 추정 금지)
    names = {t.name for t in tools}
    if "search" in names:
        try:
            import osk.search as _s
            src = (Path(_s.__file__).read_text(encoding="utf-8"))
            if '"updated"' not in src:
                errs.append('search 결과 계약 누락: "updated"')
            if 'r["signed"]' in src or '"signed":' in src:
                errs.append('search 결과에 폐지된 signed 필드 잔존')
        except Exception as e:
            errs.append(f"search 계약 판독 실패: {e}")
    # ⓔ 군집 거부가 유효 목록을 싣는가
    try:
        import osk.write as _w
        if "_cluster_names()" not in Path(_w.__file__).read_text(encoding="utf-8"):
            errs.append("군집 거부 문구가 유효 목록을 싣지 않는다")
    except Exception as e:
        errs.append(f"거부 문구 판독 실패: {e}")
    # ⓕ 설명에 박힌 수치는 낡는다 — working_memory 설명의 상한 수치는 계수
    #    (wm.LIMIT)와 같아야 한다. 계수를 개정하면 이 검사가 설명의 낡음을 잡는다.
    if "scope_memory" in names:
        try:
            import osk.scope_memory as _sm
            wmt = next(t for t in tools if t.name == "scope_memory")
            if str(_sm.LIMIT) not in (wmt.description or ""):
                errs.append(f"scope_memory 설명의 상한 수치가 계수({_sm.LIMIT}자)와 다르다")
        except Exception as e:
            errs.append(f"상한 수치 대조 실패: {e}")
    return errs


# Mechanism §6-2 2항 — 표면에서 금지된 권위 심벌 (보호영역 권위·구체제 서명)
FORBIDDEN_CALLS = ("protect", "unprotect", "approve", "revert",
                   "sign", "unsign", "restore_for_dismissal")
AUTHORITY_LEDGERS = ("APPROVALS", "SIGNATURES", "PINS")


def declared_tools() -> list[str] | None:
    """Mechanism §6-2 4항의 도구 목록 — 규범이 표면의 정본이다."""
    import re as _re
    try:
        text = (ROOT / "_governance/Mechanism.md").read_text(encoding="utf-8")
    except OSError:
        return None
    m = _re.search(r"^## §6-2 .*?```\n(.*?)```", text, _re.M | _re.S)
    return sorted(m.group(1).split()) if m else None


# 표면이 거치는 쓰기 통로 — 금지 심벌 검사를 여기까지 건다. 코드를 옮겨
# 검사를 비켜가는 표류를 막기 위해서다(6차 판정). `osk/raw.py`는 append_raw의
# 통로이므로 같은 이유로 여기 든다.
SURFACE_MODULES = ("mcp_server.py", "osk/write.py", "osk/raw.py")


def surface_violations(engine_dir=None) -> list[str]:
    """MCP 표면 검사 (Mechanism §6-2). 정적 도달성 분석은 파이썬에서 취약하므로
    결정론적인 두 가지만 본다: ⅰ)선언 목록과 구현 도구의 동치 ⅱ)금지 심벌
    (보호영역·구체제 서명 권위 호출·권위 대장 append)의 부재. 금지선은 **권위 대장**이지
    대장 일반이 아니다 — 충돌 후보 기록은 헌법 12조 2항이 명하는 바다.

    검사 대상은 경로 사본이 아니라 **지금 도는 엔진**이다(시험은 engine_dir로
    사본을 준다)."""
    import ast
    from pathlib import Path as _Path
    errs = []
    declared = declared_tools()
    if declared is None:
        return ["Mechanism §6-2의 도구 목록을 읽지 못했다 — 표면의 정본 부재"]
    base = _Path(engine_dir) if engine_dir else _Path(__file__).resolve().parent.parent
    src = base / "mcp_server.py"
    trees = {}
    for rel in SURFACE_MODULES:
        f = base / rel
        try:
            trees[rel] = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except (OSError, SyntaxError) as e:
            return [f"표면 판독 실패: {rel} — {e}"]
    tree = trees["mcp_server.py"]

    def _is_tool(fn):
        return any(
            (isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool")
            or getattr(d, "attr", None) == "tool"
            for d in fn.decorator_list)

    impl = sorted(n.name for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and _is_tool(n))
    for extra in sorted(set(impl) - set(declared)):
        errs.append(f"선언되지 않은 도구가 표면에 있다: {extra}")
    for missing in sorted(set(declared) - set(impl)):
        errs.append(f"선언된 도구가 구현에 없다: {missing}")

    for rel, t in trees.items():
        for n in ast.walk(t):
            if not isinstance(n, ast.Call):
                continue
            name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if name in FORBIDDEN_CALLS:
                errs.append(f"표면이 권위 심벌을 호출한다: {rel}:{n.lineno} {name}")
            if name == "ledger_append" and n.args:
                tgt = (getattr(n.args[0], "attr", None)
                       or getattr(n.args[0], "id", None))
                if tgt in AUTHORITY_LEDGERS:
                    errs.append(f"표면이 권위 대장에 기록한다: {rel}:{n.lineno} {tgt}")
    return errs


def conflict_candidates(idx: "graph.Index") -> list[str]:
    """기계 판정이 가능한 충돌 유형 (Mechanism §4 3항의 초기 목록 중):

    - `duplication`  — 같은 이름의 독립 노드(동명 stem).

    contradiction·competition 등 의미 판단이 필요한 유형은 여기서 다루지
    않는다 — 기계가 판정할 수 없는 것을 판정한 척하지 않는다.
    (lineage-fork는 계보 술어 `replaces` 폐지로 함께 사라졌다 — 개정은
    같은 `id`의 제자리 갱신이므로 분기 자체가 성립하지 않는다.)"""
    out = []
    for stem, paths in sorted(idx.dup_stems.items()):
        out.append(f"duplication: {stem} — {paths}")
    return out


def make_mini_vault(dst) -> None:
    """fixture·회귀 시험용 최소 vault 골격."""
    from pathlib import Path
    dst = Path(dst)
    for d in ["= Scope/W1", "= Scope/Workbench/_ledger/case",
              "= Scope/Workbench/transit", "= Domain",
              "= Person/Delegation", "= Person/Module", "_sources"]:
        (dst / d).mkdir(parents=True, exist_ok=True)
    # W1 허브 노드 — 첫-노드 규칙(시행령 §3 6항) 아래에서 시험들이 W1에
    # 자유로 노드를 만들 수 있으려면 허브가 먼저 서 있어야 한다.
    idx_md = dst / "= Scope/W1/W1.md"
    if not idx_md.exists():
        idx_md.write_text(
            '---\nid: "260801-zzzz-w1ix"\ncreated: "2026-08-01 00:00 (KST)"\n'
            'updated: "2026-08-01 00:00 (KST)"\nauthor: "agent"\n'
            'drafter: "sonnet-5"\nsummary: "W1 군집 허브 — 시험 골격"\n---\n'
            "\n# W1\n\n시험 군집의 허브 노드.\n", encoding="utf-8")


def fixture_approval_lifecycle(tmp_root) -> list[str]:
    """보호영역 생애 fixture — **격리 subprocess**에서 실행한다: OSK_VAULT_ROOT가
    임시 mini-vault를 가리키는 별도 프로세스이므로 서버 프로세스의 전역 상태를
    일절 건드리지 않는다. protect→pending→approve(양측 CAS)→revert→unprotect의
    생애와 fail-closed 경계를 소진한다.

    stdin은 끊는다 — 이 수트가 `run_validators`로 불릴 때 부모는 **stdio
    파이프 위에 선 표면 프로세스**다.
    그 stdin을 물려주면 자식은 부모의 미결 read 뒤에 줄을 서서 인터프리터
    초기화조차 마치지 못하고(Windows 익명 파이프는 동기 객체다), 시한 120초를
    다 태운 뒤 `TimeoutExpired`가 올라온다. 실측된 실패다 — 표면으로 부른
    검증기가 매번 120초 만에 통째로 무효가 됐다."""
    import json as _json
    import os as _os
    import subprocess as _sp
    import sys as _sys
    from pathlib import Path
    mini = Path(tmp_root) / "mini-vault"
    make_mini_vault(mini)
    script = Path(__file__).parent / "_fixture_approvals.py"
    env = dict(_os.environ, OSK_VAULT_ROOT=str(mini))
    r = _sp.run([_sys.executable, str(script)], capture_output=True,
                stdin=_sp.DEVNULL, text=True, env=env, timeout=120)
    if r.returncode != 0:
        return [f"fixture 프로세스 실패: {r.stderr.strip()[-400:]}"]
    try:
        return _json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return [f"fixture 출력 파싱 실패: {r.stdout[-200:]!r}"]


def main():
    rep = run()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    raise SystemExit(0 if rep["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
