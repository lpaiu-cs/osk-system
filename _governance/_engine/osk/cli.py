"""osk.cli — 사용자·에이전트 공용 명령.

서명(sign)·해제(unsign)는 사용자 전속 행위다(헌법 10조 3항) — CLI는 대화형
확인을 강제하고, 에이전트는 이 명령을 사용자 지시 없이 실행하지 않는다.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

from .core import ROOT, resolve_in_root
from . import contract, graph, signatures, authority, validate, search


def _confirm(prompt: str) -> None:
    """사용자 전속 행위의 대화형 확인 (헌법 10조 3항).
    표준입력이 단말이 아니면 응답이 사용자의 것임을 확인할 수 없으므로
    묻지 않고 중단한다 — 파이프·리다이렉션으로 무인 서명이 성립하지 않게
    한다. 우회 플래그는 두지 않는다(fail-closed)."""
    if not sys.stdin.isatty():
        sys.exit("중단 — 서명·해제는 대화형 단말에서 사용자가 직접 확인해야 한다 "
                 "(헌법 10조 3항)")
    if input(prompt).lower() != "y":
        sys.exit("중단")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="osk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="검증기 수트 전체 실행")
    p = sub.add_parser("status", help="체계 현황")
    p = sub.add_parser("search", help="작업 검색")
    p.add_argument("query"); p.add_argument("-k", type=int, default=8)
    p = sub.add_parser("view", help="열람 검색(미서명 후보 표시)")
    p.add_argument("query"); p.add_argument("-k", type=int, default=8)
    p = sub.add_parser("check", help="권한 사전 검사")
    p.add_argument("action")
    p = sub.add_parser("sign", help="[사용자 전속] 노드 서명")
    p.add_argument("path"); p.add_argument("--reason", required=True)
    p = sub.add_parser("unsign", help="[사용자 전속] 서명 해제")
    p.add_argument("node_id"); p.add_argument("--reason", required=True)
    sub.add_parser("update", help="정본 릴리스로 갱신 (osk.update로 위임)"
                   ).add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("release", help="[정본 전용] 정식 릴리스 선언 (osk.release로 위임)"
                   ).add_argument("rest", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)

    if a.cmd == "validate":
        validate.main()
    elif a.cmd == "status":
        idx = graph.Index()
        current = set()
        for _s, (pp, _k) in idx.nodes.items():
            try:
                current.add(idx.node(pp).id)
            except Exception:
                pass
        latest = {n: r for n, r in signatures.latest_by_node().items()
                  if n in current}
        signed = sum(1 for nid in latest if signatures.status(nid) == "signed")
        print(json.dumps({
            "nodes": len(idx.nodes),
            "signed": f"{signed}/{len(latest)}",
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
    elif a.cmd == "sign":
        p = resolve_in_root(Path(a.path).resolve())   # 인자 경로도 vault 안으로 봉쇄
        if p is None or not p.is_file():
            sys.exit(f"vault 밖이거나 없는 경로 — 서명 불가: {a.path}")
        n = contract.parse(p)
        errs = contract.validate(n)
        if errs:
            print("계약 위반 — 서명 불가:", errs); sys.exit(1)
        print(f"서명 대상: {p}\nid: {n.id}\nsummary: {n.meta.get('summary')}")
        _confirm("서명은 사용자 전속 행위입니다. 본인이 상태를 확인했습니까? [y/N] ")
        rec = signatures.sign(p, a.reason, n.id)
        print("서명 등재:", rec["rid"])
    elif a.cmd == "unsign":
        _confirm(f"{a.node_id} 서명을 해제합니까? [y/N] ")
        rec = signatures.unsign(a.node_id, a.reason)
        print("해제 등재:", rec["rid"])
    elif a.cmd == "update":
        from . import update as _u
        _u.main(a.rest)
    elif a.cmd == "release":
        from . import release as _r
        _r.main(a.rest)


if __name__ == "__main__":
    main()
