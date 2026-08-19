"""osk.cli — 사용자·에이전트 공용 명령.

보호영역의 지정·해제·승인·반려는 사용자 전속 행위다(헌법 10조 1~2항) —
CLI는 대화형 확인을 강제하고, 에이전트는 이 명령을 사용자 지시 없이 실행하지
않는다. 승인/반려는 MCP 표면에 노출하지 않는다(Mechanism §6-2 2항).
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

from .core import ROOT
from . import graph, approvals, authority, validate, search


def _confirm(prompt: str) -> None:
    """사용자 전속 행위의 대화형 확인 (헌법 10조 1~2항).
    표준입력이 단말이 아니면 응답이 사용자의 것임을 확인할 수 없으므로
    묻지 않고 중단한다 — 파이프·리다이렉션으로 무인 승인이 성립하지 않게
    한다. 우회 플래그는 두지 않는다(fail-closed)."""
    if not sys.stdin.isatty():
        sys.exit("중단 — 지정·해제·승인·반려는 대화형 단말에서 사용자가 직접 "
                 "확인해야 한다 (헌법 10조 1~2항)")
    if input(prompt).lower() != "y":
        sys.exit("중단")


# `osk <이름> …`의 인자를 **파싱하지 않고 그대로** 넘기는 위임 명령.
DELEGATED = {"update": "정본 릴리스로 갱신 (osk.update로 위임)",
             "release": "[정본 전용] 정식 릴리스 선언 (osk.release로 위임)"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # 위임 명령은 파서에 넣기 전에 가른다. `argparse.REMAINDER`로 받으면 잔여의
    # 첫 토큰이 `-`로 시작할 때 상위 파서가 그것을 자기 옵션으로 먼저 해석해
    # `osk update --apply`가 "unrecognized arguments: --apply"로 죽는다(실측).
    # 위임의 계약은 "해석하지 않고 넘긴다"이므로, 해석하는 자리를 아예 지난다 —
    # `--help`도 그대로 넘어가 위임 대상 자신의 사용법이 나온다.
    if argv and argv[0] in DELEGATED:
        rest = argv[1:]
        if argv[0] == "update":
            from . import update as _u
            return _u.main(rest)
        from . import release as _r
        return _r.main(rest)

    ap = argparse.ArgumentParser(prog="osk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="검증기 수트 전체 실행")
    sub.add_parser("status", help="체계 현황")
    p = sub.add_parser("search", help="작업 검색")
    p.add_argument("query"); p.add_argument("-k", type=int, default=8)
    p = sub.add_parser("view", help="열람 검색")
    p.add_argument("query"); p.add_argument("-k", type=int, default=8)
    p = sub.add_parser("check", help="권한 사전 검사")
    p.add_argument("action")
    p = sub.add_parser("protect", help="[사용자 전속] 보호영역 지정")
    p.add_argument("region"); p.add_argument("--reason", default="")
    p = sub.add_parser("unprotect", help="[사용자 전속] 보호영역 해제")
    p.add_argument("region"); p.add_argument("--reason", default="")
    p = sub.add_parser("approve", help="[사용자 전속] 변경집합 승인")
    p.add_argument("region"); p.add_argument("--reason", default="")
    p = sub.add_parser("revert", help="[사용자 전속] 변경집합 반려(원상 복원)")
    p.add_argument("region"); p.add_argument("--reason", default="")
    for name, helptext in DELEGATED.items():     # `osk --help` 목록에만 쓰인다
        sub.add_parser(name, help=helptext)      # — 실제 파싱은 위에서 지났다
    a = ap.parse_args(argv)

    if a.cmd == "validate":
        validate.main()
    elif a.cmd == "status":
        idx = graph.Index()
        regions = approvals.protected_regions()
        print(json.dumps({
            "nodes": len(idx.nodes),
            "protected_regions": {r: approvals.state(r) for r in regions},
            "delegations": [d["title"] for d in authority.enumerate_delegations()
                            if d["effective"]],
            "root": str(ROOT),
        }, ensure_ascii=False, indent=2))
    elif a.cmd in ("search", "view"):
        s = search.Searcher()
        rows = s.work_search(a.query, a.k) if a.cmd == "search" \
            else s.view_search(a.query, a.k)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif a.cmd == "check":
        print(json.dumps(authority.check(a.action), ensure_ascii=False, indent=2))
    elif a.cmd == "protect":
        st = approvals.state(a.region)
        print(f"보호영역 지정: {a.region}\n현재 상태: {st}")
        _confirm("지정 시점의 작업본이 초기 승인본이 됩니다. 지정합니까? [y/N] ")
        rec = approvals.protect(a.region, a.reason)
        print("지정 등재:", rec["rid"], "| 승인본:", rec["accepted"])
    elif a.cmd == "unprotect":
        _confirm(f"{a.region} 보호를 해제합니까? (clean 상태에서만) [y/N] ")
        rec = approvals.unprotect(a.region, a.reason)
        print("해제 등재:", rec["rid"])
    elif a.cmd == "approve":
        st = approvals.state(a.region)
        if st != "pending":
            sys.exit(f"승인할 변경집합이 없다 — 상태: {st}")
        # 양측 CAS의 두 예상값을 확인 프롬프트 **전에** 고정한다 — 프롬프트
        # 사이에 작업본이 바뀌면 approve가 거부한다(검토한 것만 승인).
        base = approvals.approved_hash(a.region)
        work = approvals.working_tree_hash(a.region)
        print(f"승인 대상: {a.region}\n승인본→작업본: {base} → {work}")
        _confirm("검토한 이 변경집합을 승인본으로 받아들입니까? [y/N] ")
        rec = approvals.approve(a.region, base, expect_work=work, reason=a.reason)
        print("승인 등재:", rec["rid"], "| 새 승인본:", rec["accepted"])
    elif a.cmd == "revert":
        st = approvals.state(a.region)
        if st != "pending":
            sys.exit(f"반려할 변경집합이 없다 — 상태: {st}")
        print(f"반려 대상: {a.region} — 작업본을 승인본으로 원상 복원합니다")
        _confirm("에이전트의 변경을 버리고 승인본으로 되돌립니까? [y/N] ")
        rec = approvals.revert(a.region, a.reason)
        print("반려 등재:", rec["rid"], "| 복원 승인본:", rec["base"])
    # update·release는 파서 앞에서 위임된다 — 여기에 분기를 두면 죽은 코드다.


if __name__ == "__main__":
    main()
