"""osk.scope_memory — scope 기억.

**모든 세션과 기기가 같은 것을 본다.** 이름이 그것을 말해야 한다 — 구명
working_memory는 인지과학 은유(한 마음의 사유물)라 "내 세션의 스크래치패드"로
읽혔고, 실측으로 세션 한정 상태("push 대기"류)가 섞여 들어왔다. scope의 공유
기억이므로 세션 한정 상태는 적지 않는다.

구현 근거: Workbench 계약 2.1(작업 상태 — 노드가 아닌 임시 데이터, 작업 종료 시
보존 판별), 헌법 9조 3항("일회적 작업 상태는 Workbench 계약에 맡기고" + 보존
판별의 기준), 시행령 §11 1항(파일이 정본).

**상한은 저장 용량의 제한이 아니라 승격의 문턱이다.** 상한이 없으면 scope 기억이
자라고, 자라는 scope 기억은 노드를 만들 이유를 없앤다 — 필요한 것이 이미 거기 다
있기 때문이다. 그러면 지식은 영원히 주변부에 머물고 헌법 2조("연결을 통해
중심부로 자라난다")가 작동하지 않는다. 넘치는 순간이 곧 "이건 이 세션의 것이
아니다"라는 신호이고, 그 신호가 노드화를 부른다.

그래서 초과는 **거부한다.** 자동 절단·자동 요약을 두면 그 신호가 조용히 소비되어
아무 일도 일어나지 않는다. 에러 자체가 통합 트리거이므로 별도 요약 파이프라인이
없고, 통합할 때마다 자리값 못하는 엔트리가 퇴출되므로 decay 스케줄러도 없다.
"""
from __future__ import annotations
import unicodedata
from pathlib import Path

from .core import ROOT, mutation_lock, posix_rel, sha256_bytes
from . import evictions, graph, secrets, write

# 계수는 mechanism이 정한다(시행령 서문). 한 세션의 배울 점을 적기에는 충분하고
# 두 세션어치를 쌓아두기에는 모자란 크기여야 압력이 선다.
LIMIT = 1500

# 연속 상한 초과 거부의 상한 (§9-2 4항). 넘으면 재시도 지시를 거두고 "접고
# 가라"를 싣는다 — 실패한 기억 갱신이 본 작업을 막아서는 안 된다. 세션 키
# 단위로, 이 프로세스 안에서만 센다(엔진은 하네스 턴을 보지 못한다). 성공이
# 지운다. Hermes의 _MAX_CONSOLIDATION_FAILURES_PER_TURN과 같은 자리다.
OVERFLOW_STOP = 3
_OVERFLOW_RUNS: dict[str, int] = {}

# 퇴출 대기 표식 (§9-2 12항) — 상한 초과 거부가 세우고 **첫 성공한 쓰기**가
# 지운다. 연속 계수와 다른 표식이다: 계수는 읽기도 지우지만(읽기는 새 시도의
# 시작), 이 표식은 읽기에 살아남아야 한다 — 거부 → 읽기 → 잘라서 통과가 바로
# "상한에 밀려 자른" 경로이고, 그 사이의 읽기는 무엇을 자를지 보는 일이다.
_PENDING_EVICT: dict[str, bool] = {}

_CONFINE = ("scope 기억은 scope당 하나이므로 한 세션의 것을 다른 scope로 "
            "번지게 하지 않는다 —")

# 이 문장이 응답에 늘 실린다. 호출자의 기본 성향은 "지우면 안 된다"라서, 퇴출이
# 정상임을 말해 주지 않으면 쌓기만 하고 상한이 압력이 아니라 벽이 된다.
# 근거는 **있으면** 배선한다. 무조건 요구하면 승격마다 대화를 raw에 남기게
# 되고, 승격은 10턴마다 압박이 걸리므로 결국 대화 대부분이 raw에 쌓인다 —
# 헌법 4조 3항을 "인용·검증에 필요한 범위"로 고쳐 벗어난 전량 포착으로 우회
# 복귀하는 셈이다. 규범도 그렇게 읽힌다: 의무는 **증류**에 붙고(헌법 9조 1항)
# 증류에는 원료가 전제된다. Workbench 계약 3.1도 `_raw/` 인용은 쌓을 것이
# 아니라 "근거 노드로 증류하여 대체"할 것으로 본다.
EVICTION_NOTE = ("엔트리는 자리를 다툰다. 자리값을 못하는 엔트리는 지운다 — "
                 "지우는 것이 정상이며 유실이 아니다. 정리로도 모자라면 그때 "
                 "기존 노드를 갱신하거나 새 노드로 증류하고, **근거가 있으면** "
                 "배선한다. 순서가 반대면 자리값 못하는 것까지 노드가 된다.")


def _runs_key(session: str) -> str:
    """연속 거부 계수의 키 — **정본 세션 키**로 접는다.

    구판은 호출자가 보낸 원문으로 키잡았다. 별칭(구 저장소 이름)으로 들어온
    같은 세션이 다른 계수를 갖게 되어, `OVERFLOW_STOP`이 세 번째에 서지 않거나
    엉뚱한 자리에서 섰다."""
    try:
        return write.canonical_session(session) or session
    except Exception:
        return session              # 대장 판독 실패는 계수의 문제가 아니다


def sm_dir() -> Path:
    return ROOT / "= Scope" / "Workbench" / "_scope_memory"


def sm_path(scope: str) -> Path:
    """`= Scope/Workbench/_scope_memory/<scope>.md`. scope의 지도가 scope 밖에 사는 것은
    scope 디렉토리를 노드만으로 깔끔히 두기 위해서이고, 접근이 어차피 엔진을
    지나므로 물리 자리가 사용성을 좌우하지 않는다."""
    return sm_dir() / f"{scope}.md"


def canon(text: str) -> str:
    """scope 기억의 **정본 형태** — NFC 정규화 + 앞뒤 공백 제거.

    길이·해시·전문이 전부 이 형태 위에서 돈다. 정규화가 없으면 같은 글이
    기기에 따라 두 배로 세어진다(macOS 유래 NFD 한글은 코드포인트가 두 배다) —
    상한이 기기 의존이 되면 그것은 상한이 아니다. 저장은 여기에 개행 하나를
    붙이지만 그 개행은 계수에 들지 않는다 — 호출자가 보내지 않은 글자를 세면
    "몇 자를 지워야 하는가"의 답이 틀린다."""
    return unicodedata.normalize("NFC", text).strip()


def _read(p: Path) -> str:
    """저장본을 **바이트 그대로** 읽어 정본 형태로 접는다.

    `read_text`의 universal newlines를 쓰면 읽기와 쓰기가 다른 문자열을 본다 —
    쓰기(`_atomic_write`)는 `\\r`을 그대로 저장하는데 읽기만 접으므로, 성공
    응답의 `hash`와 다음 CAS가 재는 `hash`가 갈린다. 그러면 자기가 방금 쓴
    해시로 다시 부른 호출자가 "다른 기기의 통합이 들어왔다"는 거짓 진단을
    받는다(실측 재현 — PowerShell 파이프가 CRLF를 내므로 `sm write` 훅 경로가
    그대로 밟는다)."""
    return canon(p.read_bytes().decode("utf-8")) if p.is_file() else ""


def _state(scope: str, text: str, *, full: bool = True, **extra) -> dict:
    """**잔여는 늘, 전문은 읽기와 거부에만** 싣는다(§9-2 5항).

    잔여가 매 응답에 실려야 호출자가 넘치기 전에 정리한다 — 호출자는 글자를 셀
    수 없다. 14일 실측에서 상한 초과폭의 중앙값은 49자였고 33%가 25자 이내였다.

    전문은 다르다. 성공한 쓰기의 전문은 호출자가 **방금 보낸 바이트 그대로**라
    정보가 아니고, "무엇이 들어 있는지"는 세션 시작 주입이 이미 세션당 한 번
    싣는다(`scripts/hooks/claude_session_start.py`). 같은 실측에서 이 에코가
    154,955토큰 — osk 도구 비용 전체의 10.4% — 을 썼다.

    거부에는 싣는다. 시도가 반려됐으므로 호출자는 실제 저장 상태를 모른다.
    특히 해시 불일치는 다른 기기의 통합이 들어왔다는 뜻이라(§9-2 8항) 전문
    없이는 그 위에서 다시 통합할 수 없다."""
    text = canon(text)
    st = {"scope": scope, "path": posix_rel(sm_path(scope), ROOT)}
    if full:
        st["text"] = text
    st.update({"chars": len(text), "limit": LIMIT,
               "remaining": LIMIT - len(text),
               "hash": sha256_bytes(text.encode("utf-8")),
               "eviction": EVICTION_NOTE, **extra})
    return st


def _landing(session: str, space: str | None) -> tuple[str, str | None]:
    """`(scope, bound)` — 첫 쓰기가 결속을 세워야 하므로 결속 여부도 함께 낸다.

    거부에도 전문과 잔여를 싣는다(§9-2 5항). 결속을 아는 거부 — 교차 scope 같은
    경우 — 는 낼 수 있는데 안 내면, 호출자가 고쳐 보내려고 다시 읽어야 한다."""
    try:
        scope, bound = write.resolve_landing(session, space, _CONFINE)
    except write.WriteError as e:
        b = write.resolve_session(session)
        if b:
            raise write.WriteError(str(e), e.violations,
                                   **_state(b, _read(sm_path(b)))) from None
        raise
    if not scope:
        raise write.WriteError(
            "착지 미정 — 아무것도 하지 않았다",
            [f"세션 `{session}`의 scope 결속이 없다. `space`를 주면 그 자리의 "
             f"scope 기억을 쓴다. 가능한 space: {graph.space_list()}"])
    if scope not in graph.scope_names():
        raise write.WriteError(
            "결속이 가리키는 scope가 없다 — 아무것도 하지 않았다",
            [f"세션 `{session}`은 `= Scope/{scope}`에 결속돼 있으나 그 scope가 "
             f"없다. 가능한 space: {graph.space_list()}"])
    return scope, bound


def read(session: str, space: str | None = None) -> dict:
    """그 scope의 scope 기억 전문. 결속이 없고 `space`도 없으면 거부한다."""
    scope, _ = _landing(session, space)     # 읽기는 결속을 세우지 않는다
    # 성공한 읽기도 **연속**을 끊는다. §9-2 4항의 상한은 "연속 3회 거부"인데,
    # 구판은 성공한 쓰기만 계수를 지워서 사이에 낀 읽기·앵커 불일치·해시
    # 불일치가 연속으로 세어졌다. 읽기는 호출자가 저장본을 다시 본 순간이니
    # 그 다음 시도는 새 시도다. (계수는 프로세스 안에만 산다 — 이 초기화가
    # 없으면 서버를 대화 사이에 유지하는 클라이언트에서 지난 대화의 거부가
    # 다음 대화의 첫 초과를 곧장 "3회째"로 만든다.)
    _OVERFLOW_RUNS.pop(_runs_key(session), None)
    return {"ok": True, **_state(scope, _read(sm_path(scope)))}


def replace(session: str, text: str | None = None,
            expect_hash: str | None = None, space: str | None = None,
            edits: list | None = None) -> dict:
    """scope 기억을 쓴다 — **전체 치환**(`text`) 또는 **앵커 일괄**(`edits`).

    앵커 일괄은 `[{old_text, new_text}, …]`이며 해시를 요구하지 않는다 — 앵커가
    "고칠 자리를 봤다"는 증거다(§6-2 4항이 부분 변경을 그렇게 가른다). 각
    앵커는 **그 시점의 작업본**에 정확히 한 번 나와야 하고, 목록은 전부 아니면
    전무로 적용되며, 상한은 중간 상태가 아니라 **순결과**에만 건다. 그래야
    비우는 연산과 채우는 연산이 한 호출에 실려, 넘칠 때마다 전문을 다시 보내고
    문맥 전체를 다시 읽히는 왕복이 사라진다(§9-2 4항). 부분 연산을 금했던
    근거 — 상한에 닿는 쪽이 엔진이 되어 무엇을 버릴지 모른다 — 는 그대로다:
    버리는 결정은 호출자가 같은 호출 안에서 하고, 엔진은 어떤 경우에도 스스로
    버리지 않는다. 파일은 파싱하지 않는다 — 원소는 호출자가 앵커로 그때 정한다.

    `expect_hash`는 기존 내용이 있을 때 필수다(Mechanism §6-2 4항의 규율) —
    데몬의 주기 pull이 다른 기기의 통합을 끌어오므로 보지 않은 상태를 덮는 일이
    실제로 일어난다. 그 불일치가 곧 §9-2 8항의 통합 신호다.

    **이 계층은 git을 부르지 않는다.** 동기화의 계약(`ensure_branch → commit →
    pull → push`)은 데몬이 소유한다. 그 순서를 여기서 복제하면 브랜치 고정이
    빠져 남의 feature 브랜치를 rebase하고, `git add -A`가 진행 중이던 작업까지
    쓸어 담아 push한다(리뷰에서 실측). 네트워크 I/O를 `mutation_lock` 안에서
    하는 것도 표면 전체를 최대 수 분 세운다."""
    # 형태 검사는 잠금 **밖**에서 — 틀린 요청이 잠금을 잡을 이유가 없다.
    if (text is None) == (edits is None):
        raise write.WriteError(
            "`text`(전체 치환)와 `edits`(앵커 일괄) 중 하나만 — 쓰지 않았다",
            ["둘 다 없으면 쓸 것이 없고, 둘 다 있으면 어느 쪽이 이겼는지 응답으로 "
             "구분되지 않는다. 읽기는 둘 다 없이 부른다."])
    if edits is not None:
        if not isinstance(edits, list) or not edits:
            raise write.WriteError(
                "`edits`가 비어 있다 — 쓰지 않았다",
                ["`[{old_text, new_text}, …]` 목록을 보내라. 지울 때는 `new_text`에 "
                 "빈 문자열을 준다."])
        for i, e in enumerate(edits, 1):
            if not isinstance(e, dict) or "old_text" not in e or "new_text" not in e:
                raise write.WriteError(
                    f"`edits[{i}]`의 형태가 틀렸다 — 쓰지 않았다",
                    ["각 항목은 `{old_text, new_text}`다. 한쪽만으로는 무엇을 "
                     "무엇으로 바꾸는지 정해지지 않는다."])
            if not all(isinstance(e[k], str) for k in ("old_text", "new_text")):
                # 문자열이 아니면 아래 `count`·`replace`가 원시 `TypeError`를
                # 내고, 그 예외 문구가 그대로 위반 목록에 실려 호출자는 무엇을
                # 고쳐야 할지 모른다. 계약 위반은 계약의 말로 낸다.
                raise write.WriteError(
                    f"`edits[{i}]`의 값이 문자열이 아니다 — 쓰지 않았다",
                    ["`old_text`·`new_text`는 본문의 조각(문자열)이다. 지울 "
                     "때는 `new_text`에 빈 문자열을 준다."])
            # 앵커는 저장본과 **같은 정본 형태**로 맞춘다 — 저장본은 NFC인데
            # (§9-2 9항) 앵커만 NFD로 오면(macOS 유래) 눈에 같은 글자가 맞지
            # 않아 "앵커가 없다"가 된다.
            e["old_text"] = unicodedata.normalize("NFC", e["old_text"])
            e["new_text"] = unicodedata.normalize("NFC", e["new_text"])
            if not e["old_text"]:
                raise write.WriteError(
                    f"`edits[{i}]`의 앵커가 비어 있다 — 쓰지 않았다",
                    ["빈 앵커는 모든 자리에 맞아 고칠 자리를 가리키지 못한다. "
                     "바꿀 대목을 그대로 넣어라."])
            if e["old_text"] == e["new_text"]:
                raise write.WriteError(
                    f"`edits[{i}]`는 바뀌는 것이 없다 — 쓰지 않았다",
                    ["`old_text`와 `new_text`가 같다."])
    errs = write.ephemeral_session_errors(session)
    if errs:
        raise write.WriteError("세션 키 부적격 — 쓰지 않았다", errs)
    with mutation_lock():
        scope, bound = _landing(session, space)
        p = sm_path(scope)
        cur = _read(p)

        if cur and expect_hash is None and edits is None:
            raise write.WriteError(
                "expect_hash 없음 — 쓰지 않았다",
                ["기존 내용이 있다. 지금 상태를 읽고 그 `hash`를 `expect_hash`로 "
                 "함께 보내라 — 보지 않은 상태를 덮지 않기 위해서다."],
                **_state(scope, cur))
        if cur and expect_hash is not None and expect_hash != sha256_bytes(cur.encode("utf-8")):
            raise write.WriteError(
                "상태가 어긋났다 — 쓰지 않았다",
                ["`expect_hash`가 현재 상태와 다르다. 다른 기기의 통합이 "
                 "들어왔을 수 있다 — 아래 전문 위에서 다시 통합하라."],
                **_state(scope, cur))

        # 비밀값 필터의 적용 지점이 여기다. 전사는 vault 밖이라 필터가 닿지
        # 않지만, scope 기억은 vault 안이고 에이전트가 쓴다 — 요약에 섞이면
        # 그대로 커밋된다. 상한은 **치환 뒤** 길이로 잰다(치환문이 원본보다
        # 길어질 수 있고, 저장되는 것이 세어져야 한다).
        if edits is not None:
            # 앵커 일괄 (§9-2 4항). 각 앵커는 **그 시점의 작업본**에 정확히 한 번 —
            # 앞 연산이 만든 자리를 뒤 연산이 앵커로 쓸 수 있다. 전부 아니면
            # 전무: 하나라도 안 맞으면 아무것도 쓰지 않는다. 그때 저장본은
            # 호출자가 모르는 것이 돼 있다는 뜻이므로 전문을 싣는다(5항 — 해시
            # 불일치와 같은 자리). 유일성이 안전 계약의 전부다 — 여러 곳에
            # 맞으면 어디를 고칠지 호출자가 정한 바가 없다.
            if not cur:
                raise write.WriteError(
                    "기억이 비어 있다 — 쓰지 않았다",
                    ["`edits`는 기존 본문을 고친다. 첫 쓰기는 `text`로 하라."],
                    **_state(scope, cur))
            work = cur
            for i, e in enumerate(edits, 1):
                n = work.count(e["old_text"])
                if n != 1:
                    raise write.WriteError(
                        (f"`edits[{i}]` 앵커가 없다 — 쓰지 않았다" if n == 0 else
                         f"`edits[{i}]` 앵커가 {n}곳 맞는다 — 쓰지 않았다"),
                        ([f"`old_text`가 지금 저장본에 나오지 않는다. 저장본이 그 "
                          f"사이 바뀌었을 수 있다 — 아래 전문에서 그대로 복사해 "
                          f"넣어라. 공백·줄바꿈까지 일치해야 한다."]
                         if n == 0 else
                         ["어느 자리를 고칠지 정해지지 않는다. 앞뒤 줄을 함께 "
                          "넣어 앵커를 **유일하게** 만들어라."]),
                        **_state(scope, cur))
                work = work.replace(e["old_text"], e["new_text"], 1)
            text = work
        filtered, hits = secrets.filter_text(text)
        # 옵시디언 태그 방어 (Mechanism §8 7항) — scope 기억도 vault의 md라
        # 옵시디언이 렌더한다. 상한은 변환 **뒤** 길이로 잰다(저장되는 것이
        # 세어져야 한다 — 비밀값 치환과 같은 이유).
        body = canon(write._space_numeric_tags(filtered))

        if body.startswith("---"):
            # `---`로 시작하면 색인이 frontmatter로 읽어 이 파일을 노드형으로
            # 본다 — 표면 도구 하나가 vault를 검증기 FAIL 상태로 만든다.
            raise write.WriteError(
                "`---`로 시작할 수 없다 — 쓰지 않았다",
                ["scope 기억은 노드가 아니므로 frontmatter를 두지 않는다"
                 "(Mechanism §9-2 1항). 선두의 `---`는 색인이 frontmatter로 "
                 "읽어 검증기를 깨뜨린다 — 다른 줄로 시작하라."],
                **_state(scope, cur))

        if len(body) > LIMIT:
            key = _runs_key(session)
            runs = _OVERFLOW_RUNS.get(key, 0) + 1
            _OVERFLOW_RUNS[key] = runs
            _PENDING_EVICT[key] = True      # 다음 성공이 덜어 낸 것을 적는다
            # §9-2 4항은 3회째에 "**재시도 지시를 거두고** … 다음 통합으로
            # 넘긴다"고 정한다. 구판은 거두지 않고 "접고 가라"를 덧붙이기만 해서
            # 상반된 두 지시가 한 응답에 실렸다 — 거두는 것이 조문이다.
            stop = runs >= OVERFLOW_STOP
            retry = ("" if stop else
                     ("빼는 연산과 넣는 연산을 **한 `edits`에** 실어 다시 보내라 — "
                      "상한은 순결과에만 걸린다." if edits is not None else
                      "그 뒤 남은 것으로 다시 보내라. 넘칠 때마다 전문을 다시 보내지 "
                      "말고 `edits`로 빼고 넣어라 — 한 호출로 끝난다."))
            v = [f"{len(body)}자로 상한 {LIMIT}자를 {len(body) - LIMIT}자 "
                 f"넘는다. **순서대로** 하라 — (1) scope 기억에서 자리값 못하는 "
                 f"엔트리를 먼저 정리하라. (2) 그래도 모자라면 갱신할 기존 노드를 "
                 f"`search`로 먼저 찾고, 남길 값어치가 "
                 f"있는 것을 **기존 노드에 갱신**하거나 새 노드로 증류하라"
                 f"(착지는 `= Scope/{scope}`). **근거가 있으면** "
                 f"배선한다 — 기존 노드에서 온 것이면 그 `id`, 이 대화에서 처음 "
                 f"알게 됐고 **나중에 다툴 만한 주장**이면 `append_raw`로 그 라운드를 "
                 f"남기고 `round_ref`를 건다. 원료 없이 지금 처음 적는 것이면 근거는 "
                 f"비우고 본문에 언제·어디서인지를 남긴다. scope 기억은 근거가 아니라 "
                 f"경유지다. {retry}"]
            if stop:
                # 재시도 지시를 거둔다(§9-2 4항) — 실패한 기억 갱신이 본 작업을
                # 막아서는 안 된다. 기억은 그대로이니 신호를 소비하는 것이 아니라
                # 미루는 것이다.
                v.append(f"연속 {runs}회째 거부다 — 이번 통합은 **접고** 본 작업으로 "
                         f"돌아가라. 다시 보내지 마라. 기억은 그대로다. 다음 통합 "
                         f"전에 결론 난 것을 노드로 증류해 자리를 만들어라.")
            # (2)는 그 자리의 의무가 아니다(§9-2 6항) — 잘라 낸 것은 12항이
            # 기록하고 §9-3의 정돈이 처분한다. 이 말이 없으면 호출자는 (2)를
            # 지금 못 하는 것을 유실로 여겨 자르지 못하고 벽에 선다.
            v.append("잘라 낸 것은 사라지지 않는다 — 이 거부 뒤 첫 성공한 쓰기가 "
                     "덜어 낸 줄은 퇴출 기록부에 남고, 다음 세션 시작이 처분을 "
                     "싣는다(§9-2 12항·§9-3). (2)는 지금의 의무가 아니다.")
            raise write.WriteError(
                "상한 초과 — 쓰지 않았다", v,
                # 상한 초과에는 전문을 싣지 않는다(§9-2 5항). 이 거부는 **디스크를
                # 바꾸지 않았고**, 호출자가 다듬을 것은 저장본이 아니라 방금 보낸
                # 자기 초안이다. 해시 불일치와 다른 점이 그것이다 — 거기서는 다른
                # 기기의 통합이 들어와 저장본이 호출자가 모르는 것이 돼 있다.
                # `hash`·`remaining`·넘긴 자수는 그대로 온다.
                **_state(scope, cur, full=False, rejected_chars=len(body)))

        # 퇴출 기록 (§9-2 12항) — 상한 초과 거부 직후의 첫 성공한 쓰기가 덜어
        # 낸 구간. 엔진은 자르지 않는다: 호출자가 뺀 것을 적을 뿐이다. 거부와
        # 무관한 정리는 표식이 없어 적히지 않는다. **파일보다 먼저** 적는다 —
        # 파일이 먼저면 대장 손상 때 잘린 것이 기록 없이 사라지고, 그것이 이
        # 조문이 막는 유일한 손실이다. 대장이 거부하면 아무것도 쓰지 않는다.
        key = _runs_key(session)
        removed = ""
        if _PENDING_EVICT.get(key):
            removed = (evictions.removed_by_edits(edits) if edits is not None
                       else evictions.removed_lines(cur, body))
        ev = None
        if removed:
            try:
                ev = evictions.record_evict(scope, key, removed)
            except ValueError as e:
                raise write.WriteError(
                    "퇴출 기록부에 적지 못했다 — 쓰지 않았다",
                    [f"퇴출 기록부에 적지 못했다 — {e}. 상한에 밀려 잘라 낸 것은 "
                     f"기록 없이 사라질 수 없다(§9-2 12항) — 대장을 복구한 뒤 "
                     f"다시 보내라. 저장본은 그대로다."],
                    **_state(scope, cur, full=False)) from None
        # 결속을 **쓰기 전에** 세운다. 뒤에 두면 대장이 손상됐을 때 파일은
        # 남고 결속은 안 서서, 표면이 "아무것도 쓰지 않았다"고 보고하는데도
        # 호출자가 방금 쓴 것에 닿지 못하는 상태가 된다(부분 성공 금지).
        if not bound:
            write.bind_session(session, scope, "첫 scope 기억 쓰기에서 확정")
        p.parent.mkdir(parents=True, exist_ok=True)
        write._atomic_write(p, (body + chr(10)).encode("utf-8") if body else b"")
        _OVERFLOW_RUNS.pop(key, None)       # 성공이 연속 계수를 지운다
        _PENDING_EVICT.pop(key, None)       # 덜어 낸 것이 없었어도 표식은 끝난다

        # 크게 줄어든 쓰기에는 **사라진 전문**을 함께 돌려준다. 전체 치환이라
        # 직전 상태가 남지 않고 표면에 복구 수단도 없는데, 자리가 모자라 다급한
        # 호출자가 가장 손대기 쉬운 것이 `text:""`다 — 가장 위험한 버튼이 가장
        # 가까운 순간에 놓여 있다. 금지하는 대신(비울 수 없는 scope 기억은 작업
        # 기억이 아니다) 같은 턴 안에서 되돌릴 수 있게 한다.
        extra = {"filtered": sorted(set(hits))}
        if ev:
            # 잘린 것이 어디 남았는지 — 처분은 다음 세션 시작이 싣는다(§9-3 1항)
            extra["evicted"] = ev["rid"]
            extra["evicted_chars"] = len(removed)
        # 성공이 전문을 싣지 않으므로(§9-2 5항) 치환된 결과가 보이지 않는다.
        # 상주 스키마는 여유가 4자뿐이라 표면에 못 적는다 — 실제로 치환이 일어난
        # 때만 싣는다. 안 적으면 호출자의 기억과 저장본이 갈린 채로 다음 통합이
        # 그 위에 얹힌다.
        if hits:
            extra["filtered_note"] = (
                "비밀값이 치환됐다 — 저장본이 보낸 것과 다르다. 다음 통합 전에 "
                "`text` 없이 한 번 읽어 저장본 위에서 이어라.")
        if cur and len(body) * 2 < len(cur):
            extra["replaced_text"] = cur
            extra["note"] = (
                f"이 쓰기로 {len(cur) - len(body)}자가 사라졌다. 의도한 정리라면 "
                f"그대로 두고, 실수였으면 `replaced_text`를 그대로 다시 보내라 — "
                f"직전 상태는 여기 말고 어디에도 남지 않는다.")
        # 성공에는 전문을 싣지 않는다(§9-2 5항) — 호출자가 방금 보낸 것이다.
        # `hash`는 남는다: 다음 쓰기가 재읽기 없이 연쇄한다.
        return {"ok": True, **_state(scope, body, full=False, **extra)}
