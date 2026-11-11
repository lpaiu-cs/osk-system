"""osk.cli_parser — 명령의 문법.

무엇을 칠 수 있는지가 여기 있고, 그것이 하는 일은 `cli`에 있다. 검증기가 운용 안내문의 CLI 표를 이 파서와
대조하므로(`validate.cli_commands`) 명령의 실행보다 아래에 둔다."""
from __future__ import annotations
import argparse
from . import evictions
from . import harness as adapters


# `osk <이름> …`의 인자를 **파싱하지 않고 그대로** 넘기는 위임 명령.
DELEGATED = {"update": "정본 릴리스로 갱신 (osk.update로 위임)",
             "release": "[정본 전용] 정식 릴리스 선언 (osk.release로 위임)"}


# 하위 명령을 가진 명령은 제 함수에 문법을 둔다. 받는 것은 `osk`의 하위 명령 자리(`sub`)다.

def _raw(sub) -> None:
    p = sub.add_parser("raw", help="`_raw/` 기록의 숨김 저장 이관")
    rs = p.add_subparsers(dest="raw_cmd", required=True)
    q = rs.add_parser("migrate", help="기존 Markdown 원료의 숨김 .txt 이관 계획")
    q.add_argument("--apply", action="store_true", help="계획을 적용; 생략하면 읽기 전용")


def _integration(sub) -> None:
    p = sub.add_parser("integration", help="대화별 포착·통합 대기 (scope 공유 기억과 별개)")
    ins = p.add_subparsers(dest="integration_cmd", required=True)
    for name in ("list", "catchup"):
        q = ins.add_parser(name)
        q.add_argument("--limit", type=int, default=20)
    for name in ("capture", "status", "prompt", "review", "reopen"):
        q = ins.add_parser(name, help="저장·인용 라운드를 정정한 쪽이 그 라운드를 읽은 검토를 다시 연다"
                           if name == "reopen" else None)
        q.add_argument("--harness", choices=adapters.NAMES, required=True)
        q.add_argument("--conversation", required=True, help="실제 하네스 대화 ID")
        if name == "capture":
            q.add_argument("--transcript", required=True)
            q.add_argument("--session", required=True, help="프로젝트 키 — 훅이 작업 폴더에서 정한다(대화 ID가 아니다)")
            q.add_argument("--space", default=None)
        if name == "reopen":
            q.add_argument("--ref", action="append", required=True, help="정정한 저장·인용 라운드 좌표(반복 가능)")
            q.add_argument("--reason", required=True, help="정정 사유")


def _organization(sub) -> None:
    p = sub.add_parser("organization", help="Scope 참조·허브 정돈의 선택과 검증")
    osub = p.add_subparsers(dest="organization_cmd", required=True)
    q = osub.add_parser("plan")
    q.add_argument("--scope", required=True)
    q.add_argument("--preview", action="store_true")
    osub.add_parser("review")


def _growth(sub) -> None:
    p = sub.add_parser("growth", help="Scope→Domain 비교·검토·한정 실행")
    gs = p.add_subparsers(dest="growth_cmd", required=True)
    q = gs.add_parser("status", help="읽기 전용 성장 상태·기간별 증거 — 검토·복구·모델 실행 없음")
    q.add_argument("--since", help="이력 시작 ISO 시각(시간대 필수, 포함)")
    q.add_argument("--until", help="이력 끝 ISO 시각(시간대 필수, 제외; 기본 현재)")
    q.add_argument("--preflight", action="store_true", help="기존 fork doctor로 CLI·구독 인증도 조회(추론 없음)")
    for name in ("plan", "prompt", "run"):
        q = gs.add_parser(name)
        q.add_argument("--limit", type=int, default=3)
        if name == "run":
            q.add_argument("--command-file", required=True, action="append",
                           help="에이전트 argv 배열 JSON 파일 — --daily는 공급자마다 하나(Codex, 대신 돌 Claude)")
            q.add_argument("--timeout", type=int, default=600)
            q.add_argument("--daily", action="store_true",
                           help="하루 정기 실행: 주간 한도 조정, 묶음 실행, 묶음마다 한도 조회(--limit·--timeout 무시)")
            q.add_argument("--backlog", action="store_true",
                           help="--daily와 함께: 대화 검토 적체를 일괄로 — 하루 한도·벽시계 예산 없이 Scope 묶음만, "
                                "대기열이 빌 때까지(공급자는 주간 사용률 0.9에서 쉰다)")
            q.add_argument("--check", action="store_true", help="실행 파일만 확인; 모델·포착·대장 쓰기 없음")
            q.add_argument("--invocation", choices=("manual", "user_request", "scheduled", "unknown"),
                           default="unknown", help="명시한 기동 원인; 생략하면 추정하지 않는다")
    q = gs.add_parser("review")
    q.add_argument("key")
    q.add_argument("outcome", choices=("preserved", "no_value", "deferred"))
    q.add_argument("--target", default=None)
    q.add_argument("--reason", required=True)
    q.add_argument("--manifest", required=True, help="실행 전에 고정한 plan rid")
    q = gs.add_parser("checkpoint", help="완료한 개별 작업의 명시적 검토를 즉시 기록")
    q.add_argument("--file", required=True, help="UTF-8 osk_reviews JSON 파일")


def _fork(sub) -> None:
    p = sub.add_parser("fork", help="구독 fork 준비 점검 (읽기 전용)")
    fs = p.add_subparsers(dest="fork_cmd", required=True)
    q = fs.add_parser("doctor", help="route()의 판정과 근거 — 상태·설정을 쓰지 않는다")
    q.add_argument("--harness", choices=adapters.fork_names(), default=None, help="기본: 모두")
    q.add_argument("--session", default=None, help="실제 하네스 대화 ID")
    q.add_argument("--transcript", default=None, help="전사 경로 (기본: 훅과 같은 방식으로 찾는다)")
    q.add_argument("--json", action="store_true")


def _sm(sub) -> None:
    # `wm`도 기계 경로다 — SessionStart 훅이 `show`를 불러 전문을 주입한다.
    p = sub.add_parser("sm", help="scope 기억 (훅 경로)")
    ws = p.add_subparsers(dest="sm_cmd", required=True)
    q = ws.add_parser("show", help="전문 출력 — 훅이 그대로 문맥에 주입한다")
    q.add_argument("--session", required=True, help="프로젝트 키(훅이 작업 폴더에서 정한다)")
    q.add_argument("--space", default=None)
    q.add_argument("--json", action="store_true", help="상태 전체를 JSON으로")
    q = ws.add_parser("write", help="전체 치환 — 본문은 stdin")
    q.add_argument("--session", required=True, help="프로젝트 키(훅이 작업 폴더에서 정한다)")
    q.add_argument("--expect-hash", dest="expect_hash", default=None)
    q.add_argument("--space", default=None)


def _routing(sub) -> None:
    p = sub.add_parser("routing", help="프로젝트 결속 — 결속 집합과 주된 scope (Mechanism §6-2 6항)")
    rt = p.add_subparsers(dest="routing_cmd", required=True)
    q = rt.add_parser("show", help="프로젝트의 결속 집합·주된 scope — 생략하면 검토 대상 전부")
    q.add_argument("--project", default=None, help="프로젝트 키(훅이 작업 폴더에서 정한 값)")
    for name, helptext in (("bind", "결속에 scope를 더한다 — 주된 scope는 정하지 않는다"),
                           ("unbind", "결속에서 scope를 뺀다"),
                           ("primary", "[사용자 전속] 주된 scope 지정 — 그 프로젝트 자체의 활동이 속한 scope")):
        q = rt.add_parser(name, help=helptext)
        q.add_argument("--project", required=True, help="프로젝트 키")
        q.add_argument("--scope", required=True, help="scope 이름")
        q.add_argument("--reason", default="")


def _tidy(sub) -> None:
    # `tidy`도 기계 경로다 — 훅이 지시한 처분을 에이전트가 `settle`로 적는다.
    p = sub.add_parser("tidy", help="정돈 — 미처분 퇴출 항목 (Mechanism §9-3)")
    ts = p.add_subparsers(dest="tidy_cmd", required=True)
    q = ts.add_parser("list", help="scope별 미처분 evict 목록과 현황")
    q.add_argument("--scope", default=None)
    q = ts.add_parser("prompt", help="전용 정돈 세션의 프롬프트 — 새 세션에 붙여 넣는다")
    q.add_argument("--scope", default=None, help="기본: 가장 오래 밀린 scope")
    q = ts.add_parser("settle", help="처분 기록 — 노드로 증류·기존 노드에 통합·폐기")
    q.add_argument("of", help="대상 evict의 rid")
    q.add_argument("outcome", choices=evictions.OUTCOMES)
    q.add_argument("--target", action="append", default=None,
                   help="노드 제목 (폐기면 없음, 여럿이면 되풀이)")
    q.add_argument("--reason", default=None, help="처분의 판단 이유")
    q = ts.add_parser("discard", help="경유 노드 폐기 — 유입 참조가 없을 때, 사용자 확인 뒤")
    q.add_argument("title", help="Workbench 경유 노드 제목")
    q.add_argument("--confirm", default=None, help="확인받은 판의 해시 (없으면 계획만 낸다)")


def _roots(sub) -> None:
    p = sub.add_parser("roots", help="외부 루트 — 선언은 모든 기기에, 위치는 이 기기에 (Mechanism §1 6항)")
    ro = p.add_subparsers(dest="roots_cmd", required=True)
    ro.add_parser("show", help="별칭마다 선언 상태와 이 기기의 위치")
    q = ro.add_parser("declare", help="[사용자 전속] 별칭 선언 — 다시 선언하면 설명·원본을 고친다")
    q.add_argument("alias")
    q.add_argument("--note", required=True, help="설명")
    q.add_argument("--origin", default=None, help="공유 원본의 주소")
    q = ro.add_parser("retire", help="[사용자 전속] 별칭 해제")
    q.add_argument("alias")
    q.add_argument("--note", default="", help="해제 이유")
    q = ro.add_parser("mount", help="[사용자 전속] 이 기기의 위치")
    q.add_argument("alias")
    q.add_argument("path")
    q = ro.add_parser("unmount", help="[사용자 전속] 이 기기의 위치를 지운다")
    q.add_argument("alias")


def _evidence(sub) -> None:
    p = sub.add_parser("evidence", help="근거 절 (헌법 제8조 6항)")
    ev = p.add_subparsers(dest="evidence_cmd", required=True)
    q = ev.add_parser("migrate", help="비노드 derived-from을 근거 절의 지지 줄로 옮기는 이행 계획 (v5.4)")
    q.add_argument("--apply", action="store_true", help="계획대로 옮기고 이행 기록을 남긴다")


def build_parser() -> argparse.ArgumentParser:
    """`osk`의 명령 정본 — 파서 구성만 떼어 둔다. 명령을 더하는 순서가 `osk --help`의 순서다.

    검증기가 안내문(`docs/SETUP.md`)의 CLI 표를 이것과 대조한다. 구판은
    소스 문자열을 정규식으로 훑었는데, 정의 형태가 조금만 달라도 조용히
    빠졌다 — `DELEGATED`의 두 항목은 들여쓰기가 달라 **하나도 걸리지
    않았다**(실측). 명령이 무엇인지 아는 것은 파서이므로 파서에게 묻는다.
    """
    ap = argparse.ArgumentParser(prog="osk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="검증기 수트 전체 실행")
    sub.add_parser("status", help="체계 현황")
    sub.add_parser("rechecks", help="근거 재검토 후보 전체 (시행령 §7 2항)")
    p = sub.add_parser("search", help="작업 검색")
    p.add_argument("query"); p.add_argument("-k", type=int, default=8)
    p = sub.add_parser("view", help="열람 검색")
    p.add_argument("query"); p.add_argument("-k", type=int, default=8)
    p = sub.add_parser("check", help="권한 사전 검사")
    p.add_argument("action")
    _raw(sub)
    _integration(sub)
    _organization(sub)
    _growth(sub)
    _fork(sub)

    p = sub.add_parser("doctor", help="이 기기의 하네스 연결과 vault 이행 점검 — MCP·훅 등록, 훅 실행, "
                                      "전달, 판본, fork, 남은 이행 (읽기 전용)")
    p.add_argument("--harness", choices=adapters.NAMES, default=None, help="기본: 모두")
    p.add_argument("--json", action="store_true")
    p.add_argument("--vault", action="store_true",
                   help="연결 대신 vault 내용 점검 — 남은 이행, 검증기 위반, 정기 검토의 잔재, 대화 검토 적체")
    p.add_argument("--migrate", action="store_true",
                   help="남은 이행을 계획하고(종료코드 2), 확인된 다음 실행에서 순서대로 적용해 이행 기록을 남긴다")
    _sm(sub)
    _routing(sub)
    _tidy(sub)

    p = sub.add_parser("validators",
                       help="검증기 활성화 현황, 해제·재활성화는 [사용자 전속] (Mechanism §6-1)")
    p.add_argument("--activate", metavar="RULE")
    p.add_argument("--deactivate", metavar="RULE")
    p.add_argument("--reason", default="")
    _roots(sub)
    _evidence(sub)
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
