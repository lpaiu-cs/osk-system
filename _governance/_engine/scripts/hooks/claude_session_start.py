"""Claude Code SessionStart 훅 — scope 기억 주입 + 정돈 주입.

등록(사용자 settings.json → hooks.SessionStart → command):
    <인스턴스>/.venv/Scripts/python.exe <인스턴스>/_governance/_engine/scripts/hooks/claude_session_start.py

stdin으로 하네스가 주는 JSON({cwd, session_id, source, …})을 받고, stdout이
그대로 세션 문맥에 주입된다. 지시("CLAUDE.md에 쓰라")는 읽혀도 눈앞에 없으면
쓰이지 않는다는 것이 실측이라, 보여주는 일은 훅이 맡는다 — 보이지 않는 것은
통합되지 않는다.

세션 키는 cwd가 속한 git 저장소의 **본 저장소 디렉터리 이름**이다. 워크트리
안에서도 본 저장소 이름으로 접힌다(`git-common-dir`의 부모) — 워크트리 이름은
세션마다 달라 키가 되지 못한다. 결속이 없으면 빈 출력 — 주입할 것이 없는 것은
오류가 아니다.

**정돈도 같은 길로 싣는다**(Mechanism §9-3 1항). 세션이 곧 주기다 — 별도
스케줄러 없이, 결속이 선 세션이 시작되면 그 scope의 미처분 퇴출 항목 중 오래된
것부터 K개와 Workbench의 경유 노드를 함께 실어 첫 도구 호출에 처분을 함께
실으라고 지시한다. 벽이 아니다. 가장 오래된 항목이 N일을 넘으면 "밀렸다"를
**맨 앞**에 세운다(3항). 문안은 `osk.evictions`가 만든다 — 전용 세션의
프롬프트(`osk tidy prompt`)와 같은 말을 쓰기 위해서다.

부수 임무: 케이던스 카운터 리셋. 재개(resume)가 시계를 이어받으면 긴 대화일수록
증류가 덜 일어나는 것이 아니라 — 이 체계의 결정은 반대다: 재개 직후에는 증류할
새것이 없으므로 카운터는 세션 로컬이고 재개는 0에서 시작한다.

어떤 실패도 세션 시작을 막지 않는다(전부 삼키고 빈 출력). 기억과 정돈은 서로
독립이다 — 한쪽의 실패가 다른 쪽의 주입을 막지 않는다.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]        # …/_governance/_engine
sys.path.insert(0, str(ENGINE))

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def session_key(cwd: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            gd = Path(r.stdout.strip())
            if not gd.is_absolute():
                gd = Path(cwd) / gd
            return gd.resolve().parent.name
    except Exception:
        pass
    return Path(cwd).name


def _memory_block(scope_memory, key: str) -> str:
    """scope 기억 전문 — 비었으면 빈 문자열."""
    st = scope_memory.read(key)
    text = (st.get("text") or "").strip()
    if not text:
        return ""
    # 문구는 **현행 계약**을 가르쳐야 한다. 구판은 "약 10 user 턴마다 …
    # 전체 치환"이라 적었는데, v3.10.0의 케이던스는 9·15턴이고 쓰기의 기본은
    # `edits` 앵커 일괄이다 — 세션의 첫 지시가 1,500자 전문 재발화를 유도해
    # 개정이 없애려던 행동을 그대로 불렀다.
    #
    # 세션 키도 싣는다. 도구의 `session`은 이 값이어야 하는데 훅만 알고
    # 호출자는 몰라서 매번 지어냈고, 그 결속은 append-only로 영구히 쌓였다.
    return (
        f"[osk scope 기억 — = Scope/{st['scope']} · "
        f"{st['chars']}/{st['limit']}자 · 여유 {st['limit'] - st['chars']}자]\n"
        f"모든 세션·기기가 공유하는 기억이다 — 세션 한정 상태를 적지 말 것.\n"
        f"`scope_memory`를 부를 때 `session=\"{key}\"`를 그대로 쓴다 — "
        f"세션이 바뀌어도 같은 값이어야 이 기억으로 돌아온다.\n"
        f"약 9 user 턴마다 이 세션의 배울 점을 통합하라 — `edits`로 "
        f"`[{{old_text, new_text}}, …]`를 **다음 도구 호출에 함께** 실어라. "
        f"앵커는 아래 전문에서 그대로 복사하고(공백·줄바꿈까지, 마지막 줄엔 "
        f"개행이 없다) 해시는 필요 없다. 전문을 통째로 갈 때만 `text`와 "
        f"아래 `hash`를 쓴다.\n"
        f"hash: {st['hash']}\n---\n{text}")


def main() -> None:
    try:
        env = json.load(sys.stdin)
    except Exception:
        env = {}
    cwd = env.get("cwd") or os.getcwd()

    # 케이던스 카운터 리셋 — 카운터는 세션·기기 로컬이다.
    sid = env.get("session_id") or ""
    if sid:
        try:
            (Path(tempfile.gettempdir()) / "osk-cadence" / f"{sid}.count"
             ).unlink(missing_ok=True)
        except OSError:
            pass

    try:
        from osk import scope_memory, write, evictions
        key = session_key(cwd)
        scope = write.resolve_session(key)
        if not scope:
            return                                   # 결속 없음 — 주입할 것 없음
        mem = ""
        try:
            mem = _memory_block(scope_memory, key)
        except Exception:
            mem = ""                                 # 기억 판독 실패가 정돈을 막지 않는다
        banner = block = ""
        try:
            banner, block = evictions.hook_block(scope, sys.executable, str(ENGINE))
        except Exception:
            pass                                     # 대장 손상은 검증기·status가 말한다
        # 순서가 조문이다(§9-3 3항) — 밀림 경고가 맨 앞, 기억, 정돈 블록.
        out = "\n\n".join(p for p in (banner, mem, block) if p)
        if not out:
            return
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        return                                       # 주입 실패가 세션을 막지 않는다


if __name__ == "__main__":
    main()
