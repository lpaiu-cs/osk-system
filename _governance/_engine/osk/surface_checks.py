"""osk.surface_checks — 내놓는 표면의 검사 (Mechanism §6-2).

발행 스키마의 불변식(`surface_lint`), 규범이 선언한 도구 목록과 구현의 동치·권위 심벌의 부재
(`surface_violations`), 운용 안내문과 규범·파서의 대조(`setup_doc_drift`). 수트(`validate.run`)가 부른다."""
from __future__ import annotations
import ast
from pathlib import Path
from types import SimpleNamespace
from . import cli_parser
from .vault import Vault


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
#   7500 → 7700: `overview`의 `include` — 훅이 호스트 한도(Claude Code 1만 자·Kiro
#     3천 자) 때문에 접은 블록을 돌려받는 통로(Mechanism §9-3 1항, 2026-09-27
#     개정). 대부분 스키마(블록 이름 열거 +136)다. 정확한 호출은 훅의 접은 줄이
#     알려 주므로 설명은 한 문장(+52)으로 둔다.
#   7700 유지(2026-10-04): append_raw·read_raw → cite_round·read_cited(시행령 §2 2항
#     개정). 인용은 턴 좌표만 받고 발화는 엔진이 옮기므로 라운드 본문 인자가 빠졌다.
#     read_cited는 원본 턴 좌표도 받는다(Mechanism §9 8항) — 샌드박스 안의 정기
#     실행이 CLI 없이 끝난 대화의 턴을 읽는 길이다. cite_round·search 설명을 줄여 맞췄다.
#   7700 → 7800: `update_node`의 `rechecked`(Mechanism §4-1 2항 개정, 2026-10-06). 근거를
#     다시 넣는 배선과 재검토 완료를 갈랐다 — 근거를 다른 까닭으로 다시 넣은 호출이 점검을
#     닫지 않는다. 스키마만 자란다(+92). 쓰는 법은 후보와 함께 오는 `rechecks.close`가
#     가르치므로 설명에는 싣지 않는다.
#   7800 → 7900: `cite_round`의 `space`(시행령 §3 9항·Mechanism §9-4 3항, 2026-10-12). 소속
#     scope 없는 대화의 첫 인용이 그 대화의 착지를 정한다. 스키마만 자란다(+60). 쓰는 법은
#     소속 없는 대화의 검토 지시(`integration.unlanded`)가 가르치므로 설명에는 싣지 않는다.
#   7900 → 8000: `read_cited`의 `conversation`·`through`(#219·#220, 2026-10-22). 밀린 검토의 범위를
#     한 번에 읽는다. 스키마만 자란다(+129). 쓰는 법과 `cite_round`의 짧은 턴 ID는 대화 검토
#     안내(`integration.prompt`)가 가르치므로 설명에는 싣지 않는다.
#   8000 유지(2026-10-23): `search`의 `session`(#170, 시행령 §8 3항 locality). 스키마만 자란다(+62).
#     올리기 전에 군더더기를 심의했다 — 도구 설명이 docstring의 들여쓰기 공백(38줄 × 4칸)까지
#     싣고 있었다. 그것을 걷어(-152, `mcp_server._apply_prune`) 맞췄고, 설명의 내용은 그대로다.
#     쓰는 법은 세션 시작 문구가 가르친다.
SCHEMA_BUDGET = 8000


# 마지막 표면 린트의 상주 비용 분해 — surface_lint가 채우고 run이 보고에 싣는다.
last_surface_cost: dict | None = None


def surface_lint(vault: Vault, read_tools) -> list[str]:
    """발행 스키마의 **결정론적 불변식**. 이번 감사가 도출한 것들이며, 이 검사가
    잡는 것은 엔진의 옳음이 아니라 **표면의 가르침**이 낡는 일이다(10차 ④).

    실제 수행 시뮬레이션은 여기 넣지 않는다 — 비결정론적이고, 그 자리는 시험이
    아니라 §6-2 7항의 개정 관문이다.

    `read_tools`는 도구마다 `name`·`description`·`inputSchema`를 내놓는 함수다
    (`mcp_server.published_tools`). 표면이 건넨다 — 검증기는 표면을 읽지 않는다. 읽다가 난 실패는
    이 검사의 실패다."""
    import json as _json
    errs = []
    try:
        tools = [SimpleNamespace(**tool) for tool in read_tools()]
    except Exception as e:
        return [f"표면 판독 실패: {e}"]
    blob = _json.dumps([{"name": t.name, "description": t.description,
                         "inputSchema": t.inputSchema} for t in tools],
                       ensure_ascii=False)
    # 측정을 남긴다 — 보고(run)가 이 값을 싣는다. 판정과 보고가 딴 자로 재면
    # 조용히 갈라지므로, 재는 자리는 여기 하나다.
    global last_surface_cost
    last_surface_cost = {
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
    # ⓒ-2 **등록부 대 선언 목록** — `surface_violations`의 AST 검사는 `@mcp.tool()`
    #    데코레이터만 보므로 `mcp.add_tool(fn, name=…)`으로 등재한 도구를 놓친다
    #    (FastMCP에 실재하는 API다). 여기는 이미 **런타임 등록부**를 읽고 있으니
    #    같은 자리에서 대조하면 정적 분석의 사각이 닫힌다 — 무엇이 선언됐는지가
    #    곧 클라이언트의 능력이라는 §6-2 1항의 전제가 그렇게 지켜진다.
    declared = declared_tools(vault)
    if declared is not None:      # 정본 부재는 `surface_violations`가 이미 보고한다
        reg_names = {t.name for t in tools}
        for extra in sorted(reg_names - set(declared)):
            errs.append(f"선언되지 않은 도구가 등록부에 있다: {extra}")
        for missing in sorted(set(declared) - reg_names):
            errs.append(f"선언된 도구가 등록부에 없다: {missing}")
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
        import osk.cluster_gate as _g
        if "cluster_names(vault)" not in Path(_g.__file__).read_text(encoding="utf-8"):
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


# 권위 대장을 가리키는 식 — 저장소의 대장 자리(`vault.pins`)이거나 승인 기록부의 자리(`approvals.ledger_path(vault)`).
AUTHORITY_LEDGERS = ("signatures", "pins", "roots")   # roots — Mechanism §1 6항


def _approvals_ledger_names(tree) -> set[str]:
    """이 파일이 승인 기록부의 자리를 부르는 이름들 — `approvals.ledger_path`, 별칭, 이름으로 가져온 것."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            from_approvals = (n.module or "").split(".")[-1] == "approvals"
            for a in n.names:
                if a.name == "approvals":
                    names.add(f"{a.asname or a.name}.ledger_path")
                elif from_approvals and a.name == "ledger_path":
                    names.add(a.asname or a.name)
        elif isinstance(n, ast.Import):
            names.update(f"{a.asname or a.name}.ledger_path" for a in n.names if a.name.split(".")[-1] == "approvals")
    return names


def _authority_ledger(arg, approvals: set[str]) -> str | None:
    """`ledger_append`의 첫 인자가 권위 대장을 가리키면 그 이름."""
    if isinstance(arg, ast.Attribute) and arg.attr in AUTHORITY_LEDGERS:
        return arg.attr
    if isinstance(arg, ast.Call):
        callee = ast.unparse(arg.func)             # 다른 모듈을 거친 것도 본다: `write.approvals.ledger_path(vault)`
        if callee in approvals or (callee.rsplit(".", 1)[-1] == "ledger_path" and "approvals" in callee):
            return "approvals"
    return None


def declared_tools(vault: Vault) -> list[str] | None:
    """Mechanism §6-2 4항의 도구 목록 — 규범이 표면의 정본이다."""
    import re as _re
    try:
        text = (vault.root / "_governance/Mechanism.md").read_text(encoding="utf-8")
    except OSError:
        return None
    m = _re.search(r"^## §6-2 .*?```\n(.*?)```", text, _re.M | _re.S)
    return sorted(m.group(1).split()) if m else None


def cli_commands() -> tuple[set[str], set[str]]:
    """CLI 명령을 **파서 자신**에게 묻는다 — (전체 경로, 잎).

    소스 문자열을 정규식으로 훑는 방식은 정의 형태가 조금만 달라도 조용히
    빠진다: `DELEGATED`의 `update`·`release`는 들여쓰기 때문에 **하나도 걸리지
    않았고**, 그래서 안내문에서 그 두 행을 지워도 검사가 통과했다(리뷰 지적,
    실측). 명령이 무엇인지 아는 것은 파서이므로 파서에서 만든다.

    잎을 따로 돌려주는 이유: `raw`·`sm`은 그 자체로 칠 수 있는 명령이 아니라
    하위 명령을 요구하는 묶음이다. 안내문이 적어야 하는 것은 사용자가 실제로
    치는 `raw migrate` 쪽이고, 묶음 이름을 적는 것은 잘못이 아니다."""
    import argparse as _ap
    allc: set[str] = set()
    leaves: set[str] = set()

    def walk(parser, prefix=""):
        subs = [a for a in parser._actions
                if isinstance(a, _ap._SubParsersAction)]
        if not subs and prefix:
            leaves.add(prefix)
            return
        for act in subs:
            for name, sp in act.choices.items():
                path = (prefix + " " + name).strip()
                allc.add(path)
                walk(sp, path)

    walk(cli_parser.build_parser())
    return allc, leaves


def setup_doc_drift(vault: Vault) -> list[str]:
    """운용 안내문(`docs/SETUP.md`)이 규범·구현과 어긋나는가.

    표류는 코드에서만 나는 것이 아니다. 감사 2026-09-02이 찾은 한 무리는
    **안내문이 없어진 제도를 가르치는 것**이었다 — 도구를 열한 종이라 세고,
    있지도 않은 `move_node`를 적었으며, CLI 표에 `validators`가 빠져 있었다.
    그 무리는 조용하다: 검증기가 문서를 보지 않으므로 감사로만 드러나고, 그
    사이 새 세션은 안내문을 먼저 읽고 시작한다.

    §9 1항의 패턴표를 `secrets.PATTERNS`와 대조하는 것과 같은 규율이다 —
    **같은 것을 두 곳에 적으면 갈라지므로, 갈라진 것을 검사가 말하게 한다.**
    `surface_lint`가 이미 규범↔등록부를 잇고 있으니 여기서 안내문↔규범·파서를
    이으면 셋이 한 삼각형으로 묶인다.

    안내문을 읽지 못하면 빈 목록을 돌려준다 — 인스턴스에는 `docs/`가 없을 수
    있고, 없는 것을 결함으로 세면 그 vault는 영영 FAIL이다(§6-2 검사와 같은
    '검사 불성립' 관례)."""
    import re as _re
    try:
        text = (vault.root / "docs/SETUP.md").read_text(encoding="utf-8")
    except OSError:
        return []
    errs, declared = [], declared_tools(vault)

    # ⓐ 도구 목록 — 정본은 Mechanism §6-2 7항이고 안내문은 그 사본이다.
    #    목록이 적힌 **문단만** 본다. 절 전체를 훑으면 본문에 backtick으로 적힌
    #    경로·인자 이름까지 '도구'로 잡혀 오탐이 된다.
    if declared:
        m = _re.search(r"정본은 Mechanism §6-2 7항이다[^\n]*\n(.*?)\n[ \t]*\n",
                       text, _re.S)
        blob = m.group(1) if m else ""
        named = set(_re.findall(r"`([a-z_]+)`", blob))
        if not named:
            errs.append("안내문에서 도구 목록 문단을 찾지 못했다 — 검사 불성립")
        else:
            for missing in sorted(set(declared) - named):
                errs.append(
                    f"안내문의 도구 목록에 없다: {missing} (Mechanism §6-2 7항)")
            for stale in sorted(named - set(declared)):
                errs.append(f"안내문이 규범에 없는 도구를 적는다: {stale}")
        # ⓑ 개수 표기 — "도구는 열둘이며" 같은 문장이 목록과 함께 늙는다.
        #    문단을 못 찾았으면 위에서 이미 말했다 — 같은 사실로 두 번 울지 않는다.
        at = m.start() if m else None
        head = text[max(0, at - 200):at + 200] if at is not None else ""
        n = len(declared)
        want = _NUM_KO.get(n)
        # 숫자꼴도 인정하되 **단위를 달고 있을 때만** 본다 — 맨 숫자를 찾으면
        # 곁의 조항 번호(§6-2 7항)가 우연히 걸린다.
        if (at is not None and want
                and not (want in head or f"{n}종" in head
                         or f"{n}개" in head)):
            errs.append(f"안내문의 도구 수 표기가 규범과 다르다 — 선언 {n}종")

    # ⓒ CLI 표 — 사용자가 칠 수 있는 명령이 실제 파서와 **양방향으로** 같아야
    #    한다. 빠진 것만 보면 반대쪽 표류를 놓친다: 파서에서 명령을 없앴는데
    #    안내문 행이 남으면 사용자는 안내문을 따라 존재하지 않는 명령을 친다 —
    #    이 검사가 막으려는 '없어진 제도를 안내문이 계속 가르치는' 유형이다.
    #
    #    **찾지 못한 것을 통과로 세지 않는다.** 표를 못 찾으면 조용히 넘어가던
    #    구판은, 안내문을 다시 쓰다 표 머리글이 바뀌는 순간 이 보장을 통째로
    #    잃었다(리뷰 지적) — 그 뒤로는 명령을 더하든 지우든 아무 말이 없다.
    #    위 도구 목록 갈래가 '문단을 찾지 못했다'를 오류로 내는 것과 같게 둔다.
    try:
        allc, leaves = cli_commands()
    except Exception as e:
        errs.append(f"CLI 명령을 파서에서 만들지 못했다 — 검사 불성립: {e}")
        allc, leaves = set(), set()
    if leaves:
        tbl = _re.search(r"^\| 명령 \| 하는 일 \|$(.*?)\n[ \t]*\n", text,
                         _re.M | _re.S)
        if not tbl:
            errs.append("안내문에서 CLI 표를 찾지 못했다 — 검사 불성립"
                        "(표 머리글은 `| 명령 | 하는 일 |`이어야 한다)")
        else:
            doc: set[str] = set()
            for row in tbl.group(1).splitlines():
                cells = row.split("|")
                if len(cells) < 3:
                    continue
                head = cells[1].strip()
                if not head or set(head) <= set("-: "):
                    continue                     # 구분선
                # **첫 칸만** 본다 — 설명 칸의 backtick(`_raw/` 등)까지 훑으면
                # 명령이 아닌 것이 명령으로 잡혀 거짓 경보가 난다.
                doc |= set(_re.findall(r"`([a-z][a-z-]*(?: [a-z]+)?)`", head))
            for missing in sorted(leaves - doc):
                errs.append(f"안내문의 CLI 표에 없는 명령이다: {missing}")
            for stale in sorted(doc - allc):
                errs.append(f"안내문의 CLI 표에 없어진 명령이 남아 있다: {stale}")
    return errs


# 안내문은 도구 수를 한국어 고유수사로 적는다("도구는 열둘이며").
_NUM_KO = dict(enumerate(
    ["하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉", "열",
     "열하나", "열둘", "열셋", "열넷", "열다섯", "열여섯", "열일곱",
     "열여덟", "열아홉", "스물"], start=1))


# 표면이 거치는 쓰기 통로 — 금지 심벌 검사를 여기까지 건다. 코드를 옮겨
# 검사를 비켜가는 표류를 막기 위해서다(6차 판정). `osk/integration.py`·`osk/raw.py`는
# cite_round의 통로이므로 같은 이유로 여기 든다.
# `write`·`integration`에서 갈라져 나온 모듈도 같은 통로다 — 가를 때마다 이 목록이 따라가야 검사가
# 눈멀지 않는다.
SURFACE_MODULES = ("mcp_server.py", "osk/write.py", "osk/nodefile.py", "osk/cluster_gate.py",
                   "osk/node_checks.py", "osk/node_moves.py", "osk/raw.py", "osk/integration.py",
                   "osk/conversation_state.py", "osk/conversation_capture.py", "osk/conversation_review.py",
                   "osk/conversation_cite.py", "osk/scope_memory.py", "osk/distillation.py", "osk/routing.py")


def surface_violations(vault: Vault, engine_dir=None) -> list[str]:
    """MCP 표면 검사 (Mechanism §6-2). 정적 도달성 분석은 파이썬에서 취약하므로
    결정론적인 두 가지만 본다: ⅰ)선언 목록과 구현 도구의 동치 ⅱ)금지 심벌
    (보호영역·구체제 서명 권위 호출·권위 대장 append)의 부재. 금지선은 **권위 대장**이지
    대장 일반이 아니다 — 충돌 후보 기록은 헌법 12조 2항이 명하는 바다.

    검사 대상은 경로 사본이 아니라 **지금 도는 엔진**이다(시험은 engine_dir로
    사본을 준다)."""
    from pathlib import Path as _Path
    errs = []
    declared = declared_tools(vault)
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
        approvals = _approvals_ledger_names(t)
        for n in ast.walk(t):
            if not isinstance(n, ast.Call):
                continue
            name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if name in FORBIDDEN_CALLS:
                errs.append(f"표면이 권위 심벌을 호출한다: {rel}:{n.lineno} {name}")
            if name == "ledger_append" and n.args:
                tgt = _authority_ledger(n.args[0], approvals)
                if tgt:
                    errs.append(f"표면이 권위 대장에 기록한다: {rel}:{n.lineno} {tgt}")
    return errs
