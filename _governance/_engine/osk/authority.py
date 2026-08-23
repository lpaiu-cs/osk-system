"""osk.authority — 권한 검사.

구현 근거: 헌법 7조(위임 3요건·불명 보류), 시행령 §5(위임 Facet 전수 열거 +
승인본 확인 + 위임 절 + 3값 평가·fail-closed), Mechanism §7(위임 절 형식).

권한 검사는 위임 Facet의 **승인본 상태만** 읽는다(시행령 §5 2항). 위임의
성립은 그 노드가 위임 Facet의 승인본에 반영되어 있을 것을 요한다(헌법 7조
3항) — 위임 Facet은 상설 보호영역이다. 의미 검색·랭킹으로 권한을 추정하지
않는다(헌법 11조 3항).
"""
from __future__ import annotations
import re
from pathlib import Path

from .core import ROOT
from . import contract, approvals

DELEGATION_FACET = ROOT / "= Person" / "Delegation"
# 위임 Facet의 정본 region key — 권한 판정은 **이 정확한 region**의 승인본으로만
# 한다(하위 영역만 보호된 경우로 우회되지 않게).
DELEGATION_REGION = DELEGATION_FACET.relative_to(ROOT).as_posix()
CLAUSE_KEYS = ("대상", "범위", "조건", "종료")


def parse_clause(body: str) -> dict | None:
    """`## 위임` 절의 4항목. 형식 미충족이면 None — 권한의 근거가 되지 않는다."""
    m = re.search(r"^## 위임\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    if not m:
        return None
    out = {}
    for key in CLAUSE_KEYS:
        km = re.search(rf"^- {key}\s*[::]\s*(.+?)(?=^- |\Z)", m.group(1), re.M | re.S)
        if not km:
            return None
        out[key] = km.group(1).strip()
    return out


def covering_regions() -> list[str]:
    """위임 Facet을 덮는 보호영역 — Facet 자신과 그 **상위** 구획들.

    헌법 10조 1항: "상위 구획의 보호는 그 하위 전체에 미치며, 에이전트는 하위에
    보호의 예외를 만들 수 없다." 그래서 사용자가 Facet 대신 `= Person`을
    지정했어도 위임은 성립한다. 반대로 Facet **하위**만 지정한 것은 덮지 못한다
    — 하위 구획만 protect해 Facet의 미보호를 우회할 수 없다(헌법 7조 3항)."""
    reg = DELEGATION_REGION
    return [r for r in approvals.protected_regions()
            if reg == r.rstrip("/") or reg.startswith(r.rstrip("/") + "/")]


def _baseline_nodes() -> list[Path] | None:
    """승인본에 담긴 위임 Facet 안의 노드 파일 전수 — 덮는 보호영역이 없으면
    None(미보호).

    시행령 §5 2항은 "**승인본의** 위임 노드를 전수 열거"라고 한다 — 작업본을
    훑으면 승인본에 있으나 지금 지워진 노드를 놓치고, 하위 디렉터리의 노드도
    빠진다. 그래서 열거의 출처는 승인본 manifest다."""
    for region in sorted(covering_regions(), key=len, reverse=True):
        tree = approvals.approved_hash(region)
        table = approvals._tree_table_for_region(region, tree) if tree else None
        if table is None:
            continue
        pre = DELEGATION_REGION + "/"
        return [p for p in (approvals.resolve_in_root(rel) for rel in sorted(table)
                            if rel.startswith(pre) and rel.endswith(".md"))
                if p is not None]
    return None


def enumerate_delegations() -> list[dict]:
    """위임 Facet 전수 열거. 각 항목: 성립 3요건(배치·유효 위임 절·승인본
    반영) 평가를 포함한다.

    열거는 **승인본**에서 한다(시행령 §5 2항). `approved`는 그 노드가 덮는
    보호영역의 승인본에 그 해시로 들어 있는가다 — 덮는 영역이 없거나(미보호),
    승인 이후 작업본이 달라졌으면(pending) False다(fail-closed). 파싱 불가
    파일은 위임으로 세지 않고 `broken`으로 표시한다 — 그 파일 하나가 권한
    검사를 죽이지 않는다(시행령 §11)."""
    out = []
    nodes = _baseline_nodes()
    if nodes is None:                      # 미보호 — 성립한 위임이 없다
        if not DELEGATION_FACET.exists():
            return out
        nodes = sorted(DELEGATION_FACET.glob("*.md"))   # 보고용(전부 미성립)
    for p in nodes:
        # 군집 색인 노드(헌법 3조 8항)는 위임이 아니다 — Delegation Facet도
        # 노드 군집이라 동명 색인을 두는데, 그것을 위임으로 열거하면 절 형식
        # 검사가 영구 실패한다(v3.3.0 실측: 색인 승인 직후 위임 3요건 FAIL).
        # 색인에 위임 절을 위장시키는 것도, Facet만 색인 예외로 하는 것도
        # 답이 아니다 — 열거가 거른다(시행령 §5 1항).
        if p == DELEGATION_FACET / f"{DELEGATION_FACET.name}.md":
            continue
        rel = str(p.relative_to(ROOT))
        if not p.is_file():                # 승인본에는 있으나 작업본에서 사라짐
            out.append({"path": rel, "node": None, "title": p.stem,
                        "clause": None, "broken": "작업본에 없음",
                        "valid_clause": False, "approved": False,
                        "effective": False})
            continue
        try:
            n = contract.parse(p)
        except Exception as e:
            out.append({
                "path": rel, "node": None,
                "title": p.stem, "clause": None, "broken": str(e),
                "valid_clause": False, "approved": False, "effective": False,
            })
            continue
        clause = parse_clause(n.body)
        approved = any(approvals.file_in_region_baseline(r, p)
                       for r in covering_regions())
        out.append({
            "path": rel, "node": n.id,
            "title": p.stem, "clause": clause,
            "valid_clause": clause is not None,
            "approved": approved,
            "effective": clause is not None and approved,
        })
    return out


def evaluate(delegation: dict, action: str) -> tuple[str, str]:
    """위임 하나를 행위에 대해 **적용·비적용·불명**으로 평가한다
    (시행령 §5 2항). 반환: (평가, 사유).

    v0 의미론: 성립하지 않은 위임만 기계적으로 '비적용'으로 확정할 수 있다.
    성립한 위임의 범위·조건·종료를 기계 평가하는 적용 봉투 평가기는 아직
    없으므로, 성립한 위임은 대상 문자열이 스치든 아니든 전부 '불명'이다 —
    불명을 비적용으로 대체하지 않는 것이 이 함수의 요점이다(헌법 7조 9항).
    문자열 일치는 사용자가 원문을 읽을 때의 안내일 뿐 평가값이 아니다."""
    if not delegation["valid_clause"]:
        return "비적용", "위임 절 형식 미충족 — 권한의 근거가 아니다"
    if not delegation["approved"]:
        return "비적용", "위임 Facet 승인본에 반영되지 않음 — 위임 미성립"
    return "불명", "적용 봉투(범위·조건·종료)의 기계 평가 미구현"


def check(action: str) -> dict:
    """행동 계획 전 검사 (시행령 §5 2항). 위임 Facet을 전수 열거해 각 위임을
    적용·비적용·불명으로 평가하고, 하나라도 불명이 남으면 그 행위는 보류다.

    v0 의미론: 적용 봉투 평가기가 없으므로 성립한 위임은 전부 불명이고,
    따라서 **판정은 언제나 보류(hold)다** — 이 검사는 proceed를 내지 않는다.
    불명은 비적용으로 대체하지 않는다(헌법 7조 9항)."""
    dels = enumerate_delegations()
    evaluated = []
    for d in dels:
        verdict, why = evaluate(d, action)
        evaluated.append({"title": d["title"], "node": d["node"],
                          "evaluation": verdict, "reason": why,
                          "clause": d["clause"]})
    unknown = [e for e in evaluated if e["evaluation"] == "불명"]
    applied = [e for e in evaluated if e["evaluation"] == "적용"]
    hints = [e["title"] for e in evaluated
             if e["evaluation"] == "불명" and action and e["clause"]
             and (action in e["clause"]["대상"] or e["clause"]["대상"] in action)]
    return {
        "action": action,
        "verdict": "hold" if (unknown or not applied) else "proceed",
        "evaluations": evaluated,
        "unknown": [e["title"] for e in unknown],
        "candidates": hints,      # 불명 중 대상 문자열이 스치는 것 — 안내용
        "delegations": [d["title"] for d in dels],
        "note": "봉투 평가기 구현 전까지 성립한 위임은 전부 불명이므로 이 검사는 "
                "proceed를 내지 않는다. 행동의 근거는 사용자의 현재 직접 지시"
                "(헌법 7조 3항) 또는 위임 절 원문을 읽고 범위·조건을 확인한 "
                "사용자 판단뿐이다.",
    }
