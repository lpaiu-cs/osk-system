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

from .core import CASE_RE, RID_RE, ID_RE
from .ledger import ledger_read, ledger_damage, ledger_anchor_index
from . import contract, graph, signatures, approvals, authority, secrets, evictions, raw, placement, topology
from .vault import Vault
from . import activated_rules, surface_checks

# 사건 파일 머리의 고정 헤더 (Mechanism §4 4항). pre_sign은 구체제 필드로,
# 새 기록에는 두지 않으므로 필수에서 뺐다(기존 사건에는 사료로 남는다).
CASE_HEADER = ("case_no", "status", "parties", "docketed_at",
               "verdict", "verdict_at", "applied", "schema_version")
CASE_STATUS = ("docketed", "adjudicated")
CASE_VERDICT = ("기각", "수정", "존치")


def run(vault: Vault, read_tools) -> dict:
    """수트 전체. `read_tools`는 표면이 내놓는 도구의 목록을 읽는 함수다 — 표면 린트(15)가 부른다."""
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

    idx = graph.Index(vault)

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
            other, q = ids[n.id]
            link = (" — 같은 파일로 가는 링크다: 링크를 지워 한 자리만 남긴다"
                    if graph.same_file(p, q) else "")
            errs.append(f"id 중복 {n.id}: {stem} & {other}{link}")
        ids[n.id] = (stem, p)
    ok(f"노드 계약 ({len(idx.nodes) + len(broken)}개)", errs)

    # 2. 배치 (Mechanism §1)
    guard("공간 배치·`_` 규칙", lambda: placement.layout_violations(vault))
    guard("raw 저장 경계", lambda: raw.storage_violations(vault))

    # 3. 참조 위상 (헌법 8조 3항)
    guard("참조 위상", lambda: topology.topology_check(vault, idx))
    ok("동명 노드 중복", [f"{s}: {v}" for s, v in idx.dup_stems.items()])
    records = None                        # 참조 경고와 근거 절 검사(17)가 함께 쓰는 기록 색인
    try:
        records = raw.Records(vault)
        rep["warnings"] = topology.reference_report(vault, idx, records)
    except Exception as e:
        rep["warnings"] = {"dangling_refs": [], "duplicate_edges": []}
        skip("미해석 참조 경고", f"산출 실패: {e}")

    # 근거 재검토 후보 (시행령 §7 2항) — 표시일 뿐 verdict 밖이다.
    try:
        from . import rechecks
        rc = rechecks.report(vault, idx)
        if rc:
            rep["warnings"]["rechecks"] = rc
    except Exception as e:
        skip("근거 재검토 후보", f"산출 실패: {e}")

    # 4. 승인 기록부 (시행령 §6 · Mechanism §3) — 보호영역 현황.
    #    판독 실패는 플래그로 남긴다 — 빈 recs를 검사한 헛 PASS를 막는다.
    errs, arecs, appr_ok = [], [], True
    try:
        arecs = approvals.records(vault)
    except Exception as e:
        errs.append(str(e))
        appr_ok = False
    if appr_ok:
        try:
            rep["protected_regions"] = {
                r: approvals.state(vault, r) for r in approvals.protected_regions(vault)}
            gw = approvals.governance_warning(vault, arecs)   # 경고일 뿐 verdict 밖
            if gw:
                rep["warnings"]["governance_unprotected"] = gw
        except Exception as e:
            errs.append(str(e))
    ok(f"승인 기록부 ({len(arecs)}행)", errs)

    # 5. 대장 판독 (Mechanism §3 1항 공통). update.jsonl도 이 규율을 따르는
    #    대장이다. signatures.jsonl은 구체제 사료로 판독만 한다(무결 검사 대상).
    errs, ledgers = [], ([(approvals.ledger_path(vault), arecs)] if appr_ok else [])
    for p in [vault.signatures, vault.candidates, vault.pins, vault.routing, vault.validators, vault.roots,
              approvals.moves_path(vault), vault.evictions,
              vault.ledger / "migration" / "events.jsonl", vault.ledger / "rechecks.jsonl",
              vault.ledger / "update.jsonl", vault.ledger / "growth.jsonl", vault.ledger / "release.jsonl"]:
        try:
            ledgers.append((p, ledger_read(p)))
        except Exception as e:
            errs.append(str(e))
    ok("대장 JSON 무결", errs)

    # 6. 대장 구조 손상 — rid 부재·형식 위반·중복 (Mechanism §3 8항 · §3 2항).
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
        dels = authority.enumerate_delegations(vault)
        for d in dels:
            if not d["effective"]:
                errs.append(f"{d['title']}: 절={d['valid_clause']} 승인본={d['approved']}")
        rep["delegations"] = len(dels)
    except Exception as e:
        errs.append(f"위임 검사 실패: {e}")
    ok("위임 3요건", errs)

    # 8. 비밀값 필터 fixture (Mechanism §9 2항)
    ok("비밀값 필터 양성/음성", secrets.self_test())

    # 8b. 구현이 §9 1항의 표를 그대로 담는가 (§9 2항). 표를 못 읽으면 검사가
    #     성립하지 않으므로 생략한다 — 표면 계약(§6-2)과 같은 규율이다.
    if declared_secret_patterns(vault) is None:
        skip("비밀값 패턴 동치", "Mechanism §9 1항 패턴 표 부재 — 검사 불성립")
    else:
        ok("비밀값 패턴이 규범과 동치", secret_pattern_drift(vault))

    # 9. 보호영역 생애 fixture — 매 검증 실행 (회귀 방지)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        guard("보호영역 생애 fixture", lambda: fixture_approval_lifecycle(vault, td))

    # 10. 승인 기록부 정합성 (인과 극대 유일·승인본 해석 — 시행령 §6 7항)
    if appr_ok:
        guard("승인 기록부 정합(stale·승인본 해석)", lambda: approvals.integrity(vault))
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

    # 11b. 퇴출 기록부 스키마 (Mechanism §9-2 12항) — 같은 규율에 kind별 필수
    #      필드와 `settle`→`evict` 참조를 더한다. 판독 실패는 5번이 이미 보고했다.
    erecs = next((rs for p, rs in ledgers if p == vault.evictions), None)
    if erecs is not None:
        ok(f"퇴출 기록부 스키마({len(erecs)}행)", evictions.schema_errors(erecs))
    else:
        skip("퇴출 기록부 스키마", "퇴출 기록부 판독 실패 — 검사 불성립")

    # 12. 사건 파일 헤더 (Mechanism §4 3항) — 파싱 실패를 여기서 직접 보고한다
    #     (topology_check의 'conflicts 대상 부적격'으로 오보되지 않게).
    errs = []
    cdir = vault.ledger / "case"
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
    for d in sorted([*(vault.root / vault.scope).rglob("_cited"), *(vault.root / vault.scope).rglob("_raw"),
                     *(vault.root / vault.scope).rglob("_scope_memory"), vault.root / vault.person / "_cited",
                     vault.root / "_cited"]):
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                errs.append(f"{p.relative_to(vault.root)}: 판독 실패 {e}")
                continue
            _, hits = secrets.filter_text(text)
            if hits:
                errs.append(f"{p.relative_to(vault.root)}: {sorted(set(hits))}")
    ok("인용 기록·_scope_memory 비밀값 미기록", errs)

    # 14. 정합성 검사 — 충돌 후보 (헌법 12조 1·2항). 기계 판정이 가능한 유형만
    #     검출해 **보고**한다. 상정·각하는 사용자 전속이므로 대장에 자동으로
    #     기록하지 않는다(헌법 12조 3항 · 시행령 §11 3항 — 자동 집행 없음).
    guard("정합성 검사(충돌 후보 없음)", lambda: conflict_candidates(idx))

    # 15. 외부 표면 (Mechanism §6-2). 규범이 선언한 도구 목록이 정본이고,
    #     구현이 그와 동치인지 + 권위 대장에 손대지 않는지를 대조한다.
    if surface_checks.declared_tools(vault) is None:
        skip("외부 표면 계약", "Mechanism §6-2 도구 목록 부재 — 검사 불성립")
    else:
        guard("외부 표면 계약(도구 목록·권위 비노출)", lambda: surface_checks.surface_violations(vault))
        guard("표면 린트(스키마 건전·가르침)", lambda: surface_checks.surface_lint(vault, read_tools))
        if surface_checks.last_surface_cost:
            rep["surface_cost"] = dict(surface_checks.last_surface_cost)

    # 15b. 운용 안내문(docs/SETUP.md) ↔ 규범·파서. 문서 표류는 조용하다 —
    #      검증기가 문서를 보지 않으면 감사로만 드러나고, 그 사이 새 세션이
    #      안내문을 먼저 읽고 시작한다(감사 2026-09-02: 도구 수·이름·CLI 표).
    #      인스턴스에 `docs/`가 없으면 빈 목록이라 자연히 통과한다.
    guard("운용 안내문 대조(도구 목록·CLI 표)", lambda: surface_checks.setup_doc_drift(vault))

    # 16. 군집 허브 노드 (헌법 3조 8항 · 시행령 §3 6항 · Mechanism §6-1). 검사는 언제나
    #     수행해 보고하고, verdict 산입은 활성화 뒤에만 한다(시행령 §11
    #     2항·3항) — 활성화 순간 무엇이 FAIL이 될지 사용자가 미리 본다.
    try:
        co = activated_rules.cluster_overview_report(vault, idx)
        rep["cluster_overview"] = co
        co_active = activated_rules.validator_active(vault, "cluster-overview")
        rep["cluster_overview_active"] = co_active
        co_errs = activated_rules.cluster_overview_errors(co)
        if co_active:
            ok("군집 허브 노드", co_errs)
        elif co_errs:
            skip("군집 허브 노드", f"비활성 — 보고만 (위반 {len(co_errs)}건)")
        else:
            ok("군집 허브 노드", [])
    except Exception as e:
        rep["fail"].append({"군집 허브 노드": [f"검사 자체 실패: {e}"]})

    # 17. 노드 끝 근거 절 (헌법 9조 2항 · 시행령 §7 2항 · Mechanism §6-1 4항). 군집 허브
    #     노드처럼 언제나 검사해 보고하고, verdict 산입은 활성화 뒤에만 한다.
    try:
        from . import roots
        external = roots.Roots(vault)
        ev = activated_rules.evidence_section_report(vault, idx, external, records)
        # `derived-from`에 남은 비노드는 이행 전 노드마다 걸리므로 노드 수로만 싣는다 — 줄의
        # 위반이 그 수백 줄에 묻히지 않게 한다. verdict 산입(활성화 뒤)은 둘 다 센다.
        left = [n for n, es in ev.items() if any(e.startswith(activated_rules.LEFTOVER) for e in es)]
        rep["evidence_section"] = {n: shown for n, es in ev.items()
                                   if (shown := [e for e in es if not e.startswith(activated_rules.LEFTOVER)])}
        rep["evidence_section_leftovers"] = len(left)
        # 이 기기에 없는 외부 루트 — 위반이 아니라 보류이며 루트마다 한 번 알린다
        rep["evidence_section_held"] = external.held
        ev_active = activated_rules.validator_active(vault, "evidence-section")
        rep["evidence_section_active"] = ev_active
        ev_errs = [f"{n}: {e}" for n, es in sorted(ev.items()) for e in es]
        if ev_active:
            ok("근거 절", ev_errs)
        elif ev_errs:
            skip("근거 절", f"비활성 — 보고만 (위반 {len(ev_errs)}건)")
        else:
            ok("근거 절", [])
    except Exception as e:
        rep["fail"].append({"근거 절": [f"검사 자체 실패: {e}"]})

    rep["verdict"] = "PASS" if not rep["fail"] else "FAIL"
    return rep


def declared_secret_patterns(vault: Vault) -> dict[str, str] | None:
    """Mechanism §9 1항의 패턴 표 — **규범이 필터의 정본이다.**

    `declared_tools`와 같은 규율이다. 이 표가 조문에 있다는 것은 코드만 고치면
    규범과 갈린다는 뜻인데, 갈렸다는 사실을 알려 주는 자리가 없었다 — 그래서
    경계 결함(`\\b`가 한글 조사 직결 토큰을 놓친다)이 조문과 코드 양쪽에 함께
    오래 남았다. 정본을 읽어 대조하면 다음에는 한쪽만 고칠 수 없다.

    표를 못 읽으면 `None`이다 — 호출부가 그것을 거부로 다룬다(fail-closed)."""
    import re as _re
    try:
        text = (vault.root / "_governance/Mechanism.md").read_text(encoding="utf-8")
    except OSError:
        return None
    # 종결자는 `§9-2`**또는 문서 끝**이다 — 규범 원본에서는 §9-2가 뒤따르지만,
    # 발췌를 심는 시험 vault에서는 §9가 마지막일 수 있다. 하나만 보면 그런
    # vault에서 표가 통째로 안 읽혀 "정본 부재"가 된다.
    sec = _re.search(r"^## §9 .*?(?=^## §9-2|\Z)", text, _re.M | _re.S)
    if not sec:
        return None
    out = {}
    for line in sec.group(0).split("\n"):
        m = _re.match(r"^\s*\|\s*([a-z0-9][a-z0-9-]*)\s*\|\s*`(.+?)`\s*\|\s*$",
                      line)
        if m:
            out[m.group(1)] = m.group(2)
    return out or None


def secret_pattern_drift(vault: Vault) -> list[str]:
    """구현(`secrets.PATTERNS`)과 조문(§9 1항 표)의 차이 — 빈 목록이면 동치."""
    declared = declared_secret_patterns(vault)
    if declared is None:
        return ["Mechanism §9 1항의 패턴 표를 읽지 못했다 — 필터의 정본 부재"]
    errs = []
    for name in sorted(set(declared) - set(secrets.PATTERNS)):
        errs.append(f"규범에 선언된 패턴이 구현에 없다: {name}")
    for name in sorted(set(secrets.PATTERNS) - set(declared)):
        errs.append(f"선언되지 않은 패턴이 구현에 있다: {name}")
    for name in sorted(set(declared) & set(secrets.PATTERNS)):
        if declared[name] != secrets.PATTERNS[name]:
            errs.append(f"패턴이 규범과 다르다: {name}\n"
                        f"    규범: {declared[name]}\n"
                        f"    구현: {secrets.PATTERNS[name]}")
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


def make_mini_vault(vault: Vault, dst) -> None:
    """fixture·회귀 시험용 최소 vault 골격."""
    from pathlib import Path
    dst = Path(dst)
    for d in [(vault.scope + '/' + 'W1'), (vault.scope + '/' + 'Workbench/_ledger/case'),
              (vault.scope + '/' + 'Workbench/transit'), vault.domain,
              (vault.person + '/' + 'Delegation'), (vault.person + '/' + 'Module'), "_sources"]:
        (dst / d).mkdir(parents=True, exist_ok=True)
    # W1 허브 노드 — 첫-노드 규칙(시행령 §3 6항) 아래에서 시험들이 W1에
    # 자유로 노드를 만들 수 있으려면 허브가 먼저 서 있어야 한다.
    idx_md = dst / (vault.scope + '/' + 'W1/W1.md')
    if not idx_md.exists():
        idx_md.write_text(
            '---\nid: "260801-zzzz-w1ix"\ncreated: "2026-08-01 00:00 (KST)"\n'
            'updated: "2026-08-01 00:00 (KST)"\nauthor: "agent"\n'
            'drafter: "sonnet-5"\nsummary: "W1 군집 허브 — 시험 골격"\n---\n'
            "\n# W1\n\n시험 군집의 허브 노드.\n", encoding="utf-8")


def fixture_approval_lifecycle(vault: Vault, tmp_root) -> list[str]:
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
    make_mini_vault(vault, mini)
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


def main(read_tools):
    vault = Vault.open()
    rep = run(vault, read_tools)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    raise SystemExit(0 if rep["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    from . import cli                # 이 파일이 진입점일 때만 — 도구의 목록은 표면이 내놓는다
    main(cli.published_tools)
