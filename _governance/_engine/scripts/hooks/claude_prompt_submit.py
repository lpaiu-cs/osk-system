"""Claude Code UserPromptSubmit 훅 — 10턴 케이던스.

등록(사용자 settings.json → hooks.UserPromptSubmit → command):
    <인스턴스>/.venv/Scripts/python.exe <인스턴스>/_governance/_engine/scripts/hooks/claude_prompt_submit.py

user 턴마다 카운터를 올리고, 10의 배수 턴에 증류 지시를 stdout으로 낸다
(UserPromptSubmit의 stdout은 문맥에 주입된다). 케이던스가 강제하는 것은
시점이지 품질이 아니다 — 본 루프의 선의에 시점 판단까지 맡기지 않는 것이
이 훅의 전부다.

카운터는 **세션·기기 로컬**이다(임시 디렉터리, session_id 단위). vault에
남기면 동기화되어 공유된다 — 카운터는 지식이 아니다. SessionStart 훅이
세션 시작마다 지우므로 재개는 0에서 시작한다.

어떤 실패도 프롬프트 제출을 막지 않는다(전부 삼키고 빈 출력).
"""
import json
import sys
import tempfile
from pathlib import Path

CADENCE = 10


def main() -> None:
    try:
        env = json.load(sys.stdin)
    except Exception:
        return
    sid = env.get("session_id")
    if not sid:
        return
    d = Path(tempfile.gettempdir()) / "osk-cadence"
    f = d / f"{sid}.count"
    try:
        d.mkdir(parents=True, exist_ok=True)
        n = int(f.read_text(encoding="ascii").strip() or 0) + 1
    except Exception:
        n = 1
    try:
        f.write_text(str(n), encoding="ascii")
    except OSError:
        pass
    if n and n % CADENCE == 0:
        msg = (f"[osk 케이던스 — user 턴 {n}] 이 세션에서 배운 것을 지금 "
               f"`scope_memory`로 증류하라 — 먼저 읽고, 자리값 못하는 엔트리를 "
               f"지우고, 전체 치환으로 통합하라. 세션 한정 상태는 적지 않는다. "
               f"배울 것이 없으면 넘어가도 된다 — 케이던스가 강제하는 것은 "
               f"시점이지 기록이 아니다.")
        sys.stdout.buffer.write(msg.encode("utf-8"))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
