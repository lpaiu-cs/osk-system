"""osk.cli — 사용자·에이전트 공용 명령.

보호영역의 지정·해제·승인·반려는 사용자 전속 행위다(헌법 10조 1~2항) —
CLI는 대화형 확인을 강제하고, 에이전트는 이 명령을 사용자 지시 없이 실행하지
않는다. 승인/반려는 MCP 표면에 노출하지 않는다(Mechanism §6-2 2항).
"""
from __future__ import annotations
import argparse, json, sys

from .core import ROOT, StaleEngineError
from . import graph, approvals, authority, raw, scope_memory, validate, search, write


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


def _print_changeset(region: str) -> None:
    """헌법 10조 2항 — 사용자는 **차이를 검토하여** 승인·반려한다. 해시 두 개는
    검토가 아니므로 무엇이 생기고 사라지고 바뀌는지를 파일 단위로 낸다."""
    cs = approvals.changeset(region)
    if cs is None:
        print("  (차이를 판정할 수 없다 — 승인본 미해석)")
        return
    moved = {m["to"] for m in cs.get("moves", [])} \
        | {m["from"] for m in cs.get("moves", [])}
    for label, key in (("추가", "added"), ("삭제", "removed"), ("수정", "modified")):
        rows = [r for r in cs[key] if r not in moved]
        if not rows:
            continue
        print(f"  {label} {len(rows)}건")
        for r in rows[:20]:
            print(f"    {r}")
        if len(rows) > 20:
            print(f"    … 외 {len(rows) - 20}건")
    for m in cs.get("moves", []):
        print(f"  이동  {m['from']} → {m['to']}")
    eol = cs.get("eol_only") or []
    if eol:
        print(f"  그중 {len(eol)}건은 **줄바꿈만** 다릅니다 — 내용은 그대로입니다"
              f"(EOL 고정 이행). `git diff`가 빈 출력인 이유가 이것입니다.")
        for r in eol[:10]:
            print(f"    ~ {r}")
        if len(eol) > 10:
            print(f"    … 외 {len(eol) - 10}건")
    legacy = cs.get("legacy_excluded") or []
    if legacy:
        print(f"  개정 전 승인본 — 제외 구획 항목 {len(legacy)}건이 승인본에만 "
              f"남아 있습니다(§3 4항): {', '.join(legacy[:3])}")
        print("  이 승인으로 새 형상의 승인본이 서고, 그 항목은 이후 변경집합에 "
              "들지 않습니다.")
    # 파생 표시(`legacy_excluded`·`eol_only`)는 "차이 없음" 판정에 세지 않는다 —
    # 앞의 것은 승인본 쪽 사정이고, 뒤의 것은 `modified`의 부분집합이다.
    if not any(v for k, v in cs.items()
               if k not in ("legacy_excluded", "eol_only")):
        print("  (파일 단위 차이 없음)")


def _emit(obj) -> None:
    """결과를 **UTF-8 바이트로** 낸다. 이 경로의 소비자는 사람이 아니라 훅이고,
    Windows 콘솔의 기본 코드페이지를 타면 한글이 든 위반 메시지에서
    `UnicodeEncodeError`로 죽는다 — 기록은 남았는데 보고가 죽는 꼴이 된다."""
    sys.stdout.buffer.write(
        (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _raw_stdin():
    """훅이 보내는 봉투를 읽는다. **바이트로 읽어 UTF-8로 푼다** — 콘솔
    인코딩을 타면 기록이 그 기기의 코드페이지에 인질이 되는데, `_raw/`는
    append-only라 나중에 고칠 수도 없다."""
    if sys.stdin.isatty():
        sys.exit('stdin으로 JSON 봉투를 넘겨라 (훅·파이프). 형식: '
                 '{"rounds": [{"user": "…", "agent": "…"}, …]}')
    data = sys.stdin.buffer.read()
    if not data.strip():
        sys.exit("빈 stdin — 이을 라운드가 없다")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        sys.exit(f"stdin 판독 실패 — {type(e).__name__}: {e}")


def _raw_rounds(env) -> list:
    """봉투에서 라운드 목록을 꺼낸다. 배열 그대로 · `rounds` 키 · 라운드 하나를
    모두 받는다 — 한 라운드만 보내는 흔한 경우에 감싸기를 강요하지 않는다."""
    if isinstance(env, list):
        return env
    if isinstance(env, dict):
        if isinstance(env.get("rounds"), list):
            return env["rounds"]
        if "user" in env or "agent" in env:
            return [env]
    return []


def _raw_cmd(a) -> None:
    """`osk raw` — 하네스 훅이 실제 대화 바이트를 넣는 경로.

    표면의 `append_raw`는 에이전트가 **서술한** 라운드를 받는다. 헌법 4조
    3항이 명하는 것은 전량 포착이므로, 전사를 그대로 나를 수 있는 경로가
    따로 필요하다 — 같은 통로·같은 계약을 쓰고 입력만 기계에서 온다."""
    if a.raw_cmd == "status":
        try:
            _emit(raw.record_state(a.session, a.record, a.space))
        except (write.WriteError, StaleEngineError) as e:
            _emit({"ok": False, "violations": e.violations})
            sys.exit(1)
        return
    env = _raw_stdin()
    meta = env if isinstance(env, dict) else {}
    # 플래그가 봉투를 이긴다 — 봉투는 전사 생성기가, 플래그는 그것을 거는
    # 사람이 쓴다. 어느 자리로 왔는지 모호하면 거는 쪽의 뜻을 따른다.
    session = a.session or meta.get("session")
    record = a.record or meta.get("record")
    space = a.space or meta.get("space")
    missing = [k for k, v in (("session", session), ("record", record)) if not v]
    if missing:
        sys.exit(f"필수 값 없음: {', '.join(missing)} — 플래그나 봉투로 준다")
    try:
        _emit(raw.append_rounds(session, record, _raw_rounds(env), space))
    except (write.WriteError, StaleEngineError) as e:
        _emit({"ok": False, "violations": e.violations})
        sys.exit(1)


def _sm_cmd(a) -> None:
    """`osk sm` — scope 기억. `show`는 SessionStart 훅이 부르는 자리다.

    **`show`의 기본 출력은 JSON이 아니라 전문 그대로다.** 훅은 이 값을 문맥에
    그대로 넣으므로, 감싸는 껍데기가 있으면 훅마다 벗기는 코드를 쓰게 된다.
    결속이 없으면 빈 출력이고 주입할 것도 없다 — 그것은 오류가 아니다."""
    if a.sm_cmd == "show":
        # 훅이 세션 시작마다 부르는 자리다. 결속이 아직 없는 새 저장소가
        # **첫 호출의 정상 상태**이므로, 거기서 위반 JSON을 내면 훅이 그것을
        # 문맥에 넣거나 종료코드를 보고 조용히 건너뛴다 — 둘 다 나쁘다.
        # 착지를 못 정하면 주입할 것이 없을 뿐이니 빈 출력으로 넘긴다.
        try:
            st = scope_memory.read(a.session, a.space)
        except (write.WriteError, StaleEngineError) as e:
            if a.json:
                _emit({"ok": False, "violations": e.violations, **e.extra})
                sys.exit(1)
            return                      # 빈 stdout · 종료코드 0
        if a.json:
            _emit(st)
        else:
            sys.stdout.buffer.write(st["text"].encode("utf-8"))
            sys.stdout.buffer.flush()
        return
    try:
        _emit(scope_memory.replace(a.session, _stdin_text(), a.expect_hash, a.space))
    except (write.WriteError, StaleEngineError) as e:
        _emit({"ok": False, "violations": e.violations, **e.extra})
        sys.exit(1)


def _stdin_text() -> str:
    """본문은 **바이트로 읽어 UTF-8로 푼다** — 콘솔 인코딩을 타면 그 기기에서
    쓴 작업 기억만 다른 바이트가 되고, 상한도 해시도 어긋난다."""
    if sys.stdin.isatty():
        sys.exit("본문을 stdin으로 넘겨라 (훅·파이프).")
    try:
        return sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError as e:
        sys.exit(f"stdin 판독 실패 — {e}")


def build_parser() -> argparse.ArgumentParser:
    """`osk`의 명령 정본 — 파서 구성만 떼어 둔다.

    검증기가 안내문(`docs/SETUP.md`)의 CLI 표를 이것과 대조한다. 구판은
    소스 문자열을 정규식으로 훑었는데, 정의 형태가 조금만 달라도 조용히
    빠졌다 — `DELEGATED`의 두 항목은 들여쓰기가 달라 **하나도 걸리지
    않았다**(실측). 명령이 무엇인지 아는 것은 파서이므로 파서에게 묻는다.
    """
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
    # `raw`는 하네스 훅이 부르는 **기계 경로**다 — 대화형 확인을 걸지 않는다.
    # 사용자 전속 행위가 아니라 표면의 `append_raw`와 같은 행위이고, 거는
    # 순간 훅에서 쓸 수 없어 자동 포착이 성립하지 않는다.
    p = sub.add_parser("raw", help="`_raw/` 세션 기록 (훅 경로)")
    rs = p.add_subparsers(dest="raw_cmd", required=True)
    q = rs.add_parser("append", help="라운드 append — 본문은 stdin JSON")
    q.add_argument("--session"); q.add_argument("--record")
    q.add_argument("--space", default=None)
    q = rs.add_parser("status", help="기록의 현재 라운드 수 — 중복 방지용")
    q.add_argument("--session", required=True)
    q.add_argument("--record", required=True)
    q.add_argument("--space", default=None)

    # `wm`도 기계 경로다 — SessionStart 훅이 `show`를 불러 전문을 주입한다.
    p = sub.add_parser("sm", help="scope 기억 (훅 경로)")
    ws = p.add_subparsers(dest="sm_cmd", required=True)
    q = ws.add_parser("show", help="전문 출력 — 훅이 그대로 문맥에 주입한다")
    q.add_argument("--session", required=True)
    q.add_argument("--space", default=None)
    q.add_argument("--json", action="store_true", help="상태 전체를 JSON으로")
    q = ws.add_parser("write", help="전체 치환 — 본문은 stdin")
    q.add_argument("--session", required=True)
    q.add_argument("--expect-hash", dest="expect_hash", default=None)
    q.add_argument("--space", default=None)

    p = sub.add_parser("validators",
                       help="[사용자 전속] 검증기 활성화 현황·전환 (Mechanism §6-1)")
    p.add_argument("--activate", metavar="RULE")
    p.add_argument("--deactivate", metavar="RULE")
    p.add_argument("--reason", default="")
    p = sub.add_parser("protect", help="[사용자 전속] 보호영역 지정")
    p.add_argument("region"); p.add_argument("--reason", default="")
    p = sub.add_parser("unprotect", help="[사용자 전속] 보호영역 해제")
    p.add_argument("region"); p.add_argument("--reason", default="")
    p = sub.add_parser("approve", help="[사용자 전속] 변경집합 승인")
    p.add_argument("region"); p.add_argument("--reason", default="")
    p = sub.add_parser("revert", help="[사용자 전속] 변경집합 반려(원상 복원)")
    p.add_argument("region"); p.add_argument("--reason", default="")
    p = sub.add_parser("store-reconcile",
                       help="내용 주소 저장소를 파일 이름 기준으로 판독·이행")
    p.add_argument("--apply", action="store_true",
                   help="판독만 하지 않고 색인·작업 트리를 실제로 맞춘다")
    for name, helptext in DELEGATED.items():     # `osk --help` 목록에만 쓰인다
        sub.add_parser(name, help=helptext)      # — 실제 파싱은 위에서 지났다
    return ap


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

    ap = build_parser()
    a = ap.parse_args(argv)

    if a.cmd == "validate":
        validate.main()
    elif a.cmd == "validators":
        # 활성화·해제는 사용자 전속이다(시행령 §11 3항) — 표면(MCP)에는 없다.
        known = ["cluster-overview"]
        rule = a.activate or a.deactivate
        if rule:
            if a.activate and a.deactivate:
                sys.exit("중단 — --activate와 --deactivate는 함께 줄 수 없다")
            if rule not in known:
                sys.exit(f"모르는 규칙: {rule} — 가능한 규칙: {', '.join(known)}")
            kind = "activate" if a.activate else "deactivate"
            cur = "활성" if validate.validator_active(rule) else "비활성"
            print(f"규칙 {rule}: 현재 {cur}")
            if kind == "activate":
                _confirm(f"{rule}을(를) 활성화하면 위반이 검증기 verdict에 "
                         f"산입됩니다(시행령 §11 2항). 활성화합니까? [y/N] ")
            else:
                _confirm(f"{rule}을(를) 해제하면 같은 검사가 보고 전용으로 "
                         f"돌아갑니다. 해제합니까? [y/N] ")
            from .core import VALIDATORS, ledger_append
            rec = ledger_append(VALIDATORS, {
                "kind": kind, "rule": rule, "reason": a.reason})
            print("기록 등재:", rec["rid"])
        else:
            for r in known:
                print(f"{r}: "
                      f"{'활성' if validate.validator_active(r) else '비활성'}")
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
    elif a.cmd == "raw":
        return _raw_cmd(a)
    elif a.cmd == "sm":
        return _sm_cmd(a)
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
        if st == "stale":
            # 봉합 승인 — 승인 기록이 갈렸다(다기기 병합). 사용자가 갈래를 보고
            # 현재 작업본을 새 승인본으로 삼는다(Mechanism §3 5항).
            forks = approvals.divergence(a.region)
            work = approvals.working_tree_hash(a.region)
            print(f"영역이 stale입니다 — 승인 기록이 {len(forks)}갈래로 갈렸습니다.")
            for f in forks:
                print(f"  갈래 {f.get('rid')} {f.get('kind')} "
                      f"accepted={f.get('accepted')} at={f.get('at')}")
            print(f"현재 작업본: {work}")
            _confirm("이 작업본을 새 승인본으로 삼아 갈래를 봉합합니까? [y/N] ")
            # 검토한 갈래 집합을 프롬프트 **전에** 고정해 넘긴다 — 프롬프트
            # 사이 동기화로 새 갈래가 오면 봉합이 거부된다(본 적 없는 갈래를
            # 함께 봉합하지 않는다).
            rec = approvals.approve(a.region, None, expect_work=work,
                                    reason=a.reason or "분기 봉합",
                                    seal_heads=[f["rid"] for f in forks])
            print("봉합 승인 등재:", rec["rid"], "| 새 승인본:", rec["accepted"])
            return
        if st != "pending":
            sys.exit(f"승인할 변경집합이 없다 — 상태: {st}")
        # 양측 CAS의 두 예상값을 확인 프롬프트 **전에** 고정한다 — 프롬프트
        # 사이에 작업본이 바뀌면 approve가 거부한다(검토한 것만 승인).
        base = approvals.approved_hash(a.region)
        work = approvals.working_tree_hash(a.region)
        print(f"승인 대상: {a.region}\n승인본→작업본: {base} → {work}")
        _print_changeset(a.region)
        _confirm("검토한 이 변경집합을 승인본으로 받아들입니까? [y/N] ")
        rec = approvals.approve(a.region, base, expect_work=work, reason=a.reason)
        print("승인 등재:", rec["rid"], "| 새 승인본:", rec["accepted"])
    elif a.cmd == "revert":
        st = approvals.state(a.region)
        if st != "pending":
            sys.exit(f"반려할 변경집합이 없다 — 상태: {st}")
        # 반려도 파괴적이므로 승인과 **같은 결속**을 쓴다 — 버릴 변경집합
        # (승인본→작업본)을 프롬프트 **전에** 고정한다. 프롬프트 사이에
        # 에이전트가 더 쓴 변경이 '사용자가 승인한 반려'에 묶여 사라지지 않게.
        base = approvals.approved_hash(a.region)
        work = approvals.working_tree_hash(a.region)
        print(f"반려 대상: {a.region} — 작업본을 승인본으로 원상 복원합니다")
        print(f"버릴 변경집합(작업본→승인본): {work} → {base}")
        _print_changeset(a.region)
        _confirm("에이전트의 변경을 버리고 승인본으로 되돌립니까? [y/N] ")
        rec = approvals.revert(a.region, base, expect_work=work, reason=a.reason)
        print("반려 등재:", rec["rid"], "| 복원 승인본:", rec["base"])
    elif a.cmd == "store-reconcile":
        # EOL 이행의 저장소 몫. `-text`를 얻은 구획은 어느 쪽을 무조건 택해도
        # 틀린다(작업 트리를 stage하면 옳던 blob이 깨지고, 통째로 빼면 깨진
        # blob이 작업 트리를 덮는다) — 내용 주소에는 파일 이름이라는 심판이
        # 있으므로 그것으로 가른다.
        import subprocess as _sp
        _root = str(ROOT)

        def _index_bytes(rel):
            r = _sp.run(["git", "-C", _root, "cat-file", "blob", ":" + rel],
                        capture_output=True)
            return r.stdout if r.returncode == 0 else None

        def _apply(kind, rel):
            if kind == "stage":
                _sp.run(["git", "-C", _root, "add", "--", rel], check=True)
            else:
                _sp.run(["git", "-C", _root, "checkout", "--", rel], check=True)

        res = approvals.reconcile_store(
            _index_bytes, _apply if a.apply else None)
        print(f"객체 {res['checked']}건 판독 — 색인을 고칠 것 {len(res['stage'])}, "
              f"작업 트리를 고칠 것 {len(res['restore'])}, "
              f"판정 불능 {len(res['broken'])}")
        for rel in res["broken"][:20]:
            print("  판정 불능(양쪽 다 이름과 다름):", rel)
        if not res["ok"]:
            sys.exit("중단 — 어느 쪽도 이름의 digest와 맞지 않는 객체가 있다. "
                     "추측으로 고르지 않았고 아무것도 바꾸지 않았다. 그 객체를 "
                     "참조하는 승인본은 해제 후 재지정이 회복 경로다.")
        if not a.apply:
            print("판독만 했다 — 이행하려면 --apply")
        elif res["stage"] or res["restore"]:
            print("이행했다. 색인 변경은 이어지는 commit에 담긴다.")
    # update·release는 파서 앞에서 위임된다 — 여기에 분기를 두면 죽은 코드다.


if __name__ == "__main__":
    main()
