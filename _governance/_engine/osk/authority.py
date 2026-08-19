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


def enumerate_delegations() -> list[dict]:
    """위임 Facet 전수 열거. 각 항목: 성립 3요건(배치·유효 위임 절·승인본
    반영) 평가를 포함한다.

    `approved`는 그 위임 노드가 위임 Facet의 승인본에 그대로 반영돼 있는가다
    — 위임 Facet이 보호영역이 아니거나(미보호), 노드가 승인본에 없거나 승인
    이후 작업본이 달라졌으면(pending) False다(fail-closed). 파싱 불가 파일은
    위임으로 세지 않고 `broken`으로 표시한다 — 그 파일 하나가 권한 검사를
    죽이지 않는다(시행령 §11)."""
    out = []
    if not DELEGATION_FACET.exists():
        return out
    for p in sorted(DELEGATION_FACET.glob("*.md")):
        try:
            n = contract.parse(p)
        except Exception as e:
            out.append({
                "path": str(p.relative_to(ROOT)), "node": None,
                "title": p.stem, "clause": None, "broken": str(e),
                "valid_clause": False, "approved": False, "effective": False,
            })
            continue
        clause = parse_clause(n.body)
        # 승인본 반영 = **위임 Facet 자체**(DELEGATION_REGION)가 보호 중이고 그
        # 승인본에 이 노드가 그 해시로 들어 있음. Facet이 미보호면 미성립이다 —
        # 하위 디렉터리만 protect해 우회할 수 없다(헌법 7조 3항, fail-closed).
        approved = approvals.file_in_region_baseline(DELEGATION_REGION, p)
        out.append({
            "path": str(p.relative_to(ROOT)), "node": n.id,
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
