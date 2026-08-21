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
                   CASE_RE, RID_RE, ledger_read, ledger_damage,
                   ledger_anchor_index)
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
    for p in [SIGNATURES, CANDIDATES, PINS, ROUTING, approvals.MOVES,
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
        ok("보호영역 생애 fixture", fixture_approval_lifecycle(td))

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
    for d in sorted((ROOT / "= Scope").rglob("_raw")):
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
    ok("_raw 비밀값 미기록", errs)

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

    rep["verdict"] = "PASS" if not rep["fail"] else "FAIL"
    return rep


# 상주 스키마 문자수 상한 — 초과는 회귀다. 이 예산이 막는 것은 **설명의
# 비대**이지 도구 수가 아니다. 표면이 도구 하나만큼 자라는 것은 §6-2 7항의
# 개정이 결정하는 일이므로, 그때는 이 상수도 함께 올린다 — 올린 사유를 여기
# 남겨 다음 사람이 "왜 올랐나"를 코드 밖에서 묻지 않게 한다.
#   5000 → 5600: append_raw 노출 (`_raw/` 기록 통로의 표면 연결).
#   5600 → 6100: read_raw 노출 (기록의 명시 회상 — Mechanism §9 7항).
#   6100 → 6300: §6-2 7항 감사가 적발한 가르침 결손 보강 — `space`의 실제
#     형식, 결속 후 생략, 재호출 비안전성, 절단의 비대칭. 감사에서 이것들이
#     없어 실제로 막혔다(예산이 막는 것은 비대이지 필요한 가르침이 아니다).
SCHEMA_BUDGET = 6300


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


def fixture_approval_lifecycle(tmp_root) -> list[str]:
    """보호영역 생애 fixture — **격리 subprocess**에서 실행한다: OSK_VAULT_ROOT가
    임시 mini-vault를 가리키는 별도 프로세스이므로 서버 프로세스의 전역 상태를
    일절 건드리지 않는다. protect→pending→approve(양측 CAS)→revert→unprotect의
    생애와 fail-closed 경계를 소진한다."""
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
                text=True, env=env, timeout=120)
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
