"""Claude Code UserPromptSubmit 훅 — 케이던스 (9턴 동승 · 15턴 단독).

등록(사용자 settings.json → hooks.UserPromptSubmit → command):
    <인스턴스>/.venv/Scripts/python.exe <인스턴스>/_governance/_engine/scripts/hooks/claude_prompt_submit.py

user 턴마다 카운터를 올린다. **9턴**에 scope 기억의 지금 전문·해시·여유를
주입하고 "다음 도구 호출에 **함께** 실어라"를 지시한다 — 통합만을 위한 턴을
따로 쓰지 않게. **15턴**까지 기억이 갱신되지 않았으면 다시 주입하되 이번엔
단독 턴을 허용하고, 카운터를 처음으로 돌린다. 기억이 갱신되면(해시가 바뀌면)
그 자리에서 카운터를 처음으로 돌린다 — "9턴마다"는 마지막 통합으로부터다.

왜 전문을 주입하는가: 세션 시작 훅만 주입하고 케이던스 훅은 문장만 줬더니,
호출자가 읽기 턴을 쓰거나 세션 시작 때의 낡은 해시로 불일치를 냈다(실측 — 읽기
23·불일치 7). 주입 한 번(~1,100토큰)이 턴 하나(문맥 재독 ~50k 단위)보다
싸다. 왜 동승인가: 통합 호출 235건이 전부 단독 턴이었다 — 문장 하나("지금")가
턴을 만들고 있었다.

카운터와 마지막 해시는 **세션·기기 로컬**이다(임시 디렉터리, session_id 단위).
vault에 남기면 동기화되어 공유된다 — 카운터는 지식이 아니다. SessionStart 훅이
세션 시작마다 지우므로 재개는 0에서 시작한다.

어떤 실패도 프롬프트 제출을 막지 않는다(전부 삼키고 빈 출력).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]        # …/_governance/_engine
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SOFT = 9         # 이 턴에 주입하고 "다음 도구 호출에 함께"를 지시한다
HARD = 15        # 여기까지 갱신이 없으면 단독 턴을 허용하고 처음으로 돌린다

_EDITS_HOWTO = (
    "`edits`로 `[{old_text,new_text},…]` — 자리값 못하는 항목을 빼는 연산과 새것을 "
    "넣는 연산을 **한 호출에** 실어라. 앵커는 아래 전문에서 그대로 복사하고(공백·"
    "줄바꿈까지), 상한은 순결과에만 걸린다. 해시는 필요 없다. 세션 한정 상태는 적지 "
    "않는다. 배울 것이 없으면 넘어가도 된다 — 케이던스가 강제하는 것은 시점이지 "
    "기록이 아니다.")


def _read_int(p: Path) -> int:
    try:
        return int(p.read_text(encoding="ascii").strip() or 0)
    except Exception:
        return 0


def _write(p: Path, s: str) -> None:
    try:
        p.write_text(s, encoding="utf-8")
    except OSError:
        pass


def _emit(s: str) -> None:
    sys.stdout.buffer.write(s.encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> None:
    try:
        env = json.load(sys.stdin)
    except Exception:
        return
    sid = env.get("session_id")
    if not sid:
        return
    cwd = env.get("cwd") or os.getcwd()

    d = Path(tempfile.gettempdir()) / "osk-cadence"
    f_count = d / f"{sid}.count"
    f_hash = d / f"{sid}.hash"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # 기억의 지금 상태 — 결속이 없거나 비었으면 세기만 한다(주입할 것이 없다).
    st = None
    try:
        from claude_session_start import session_key
        from osk import scope_memory, write
        key = session_key(cwd)
        if write.resolve_session(key):
            st = scope_memory.read(key)
            if not (st.get("text") or "").strip():
                st = None
    except Exception:
        st = None

    n = _read_int(f_count) + 1
    if st is not None:
        last = ""
        try:
            last = f_hash.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        if last and last != st["hash"]:
            # 통합이 일어났다(이 세션이든 다른 기기든). 계수를 처음으로.
            n = 1
        _write(f_hash, st["hash"])
    _write(f_count, str(n))

    if st is None or n not in (SOFT, HARD):
        return

    scope, chars, limit = st["scope"], st["chars"], st["limit"]
    head = (f"[osk 케이던스 — user 턴 {n}] 아래는 `= Scope/{scope}` 기억의 지금 "
            f"전문이다 — {chars}/{limit}자 · **여유 {limit - chars}자** · "
            f"hash {st['hash']}.\n")
    if n == SOFT:
        body = (head +
                "이 세션에서 배운 것을 **다음 도구 호출에 함께 실어** `scope_memory`로 "
                "통합하라 — 통합만을 위한 턴을 따로 쓰지 마라. " + _EDITS_HOWTO)
    else:
        body = (head +
                f"{SOFT}턴부터 통합이 실리지 않았다. 지금 `scope_memory`로 통합하라 — "
                f"이번엔 단독 턴이어도 된다. " + _EDITS_HOWTO)
        _write(f_count, "0")            # 15턴 뒤엔 처음부터 — 매 턴 재촉하지 않는다
    _emit(body + "\n---\n" + st["text"])


if __name__ == "__main__":
    main()
