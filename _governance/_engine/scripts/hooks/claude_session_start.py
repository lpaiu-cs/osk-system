"""Claude Code / Codex SessionStart 훅 — scope 기억·복구·정돈 주입.

등록(사용자 settings.json → hooks.SessionStart → command):
    <인스턴스>/.venv/Scripts/python.exe <인스턴스>/_governance/_engine/scripts/hooks/claude_session_start.py

stdin으로 하네스가 주는 JSON({cwd, session_id, source, …})을 받고, stdout의
hookSpecificOutput.additionalContext가 세션 문맥에 주입된다. 지시("CLAUDE.md에
쓰라")는 읽혀도 눈앞에 없으면 쓰이지 않는다는 것이 실측이라, 보여주는 일은
훅이 맡는다 — 보이지 않는 것은
통합되지 않는다.

세션 키는 cwd가 속한 git 저장소의 **본 저장소 디렉터리 이름**이다. 워크트리
안에서도 본 저장소 이름으로 접힌다(`git-common-dir`의 부모) — 워크트리 이름은
세션마다 달라 키가 되지 못한다. 결속이 없어도 `overview`로 착지를 확인하도록
안내한다. 아직 없는 착지를 훅이 대신 정하지 않는다.

**정돈도 같은 길로 싣는다**(Mechanism §9-3 1항). 세션이 곧 주기다 — 별도
스케줄러 없이, 결속이 선 세션이 시작되면 그 scope의 미처분 퇴출 항목 중 오래된
것부터 K개와 Workbench의 경유 노드를 함께 실어 첫 도구 호출에 처분을 함께
실으라고 지시한다. 벽이 아니다. 가장 오래된 항목이 N일을 넘으면 "밀렸다"를
**맨 앞**에 세운다(3항). 문안은 `osk.evictions`가 만든다 — 전용 세션의
프롬프트(`osk tidy prompt`)와 같은 말을 쓰기 위해서다.

자기 대화의 완료 전사를 포착하고 durable 검토 대기를 이어받는다. 재개는
카운터·대기를 지우지 않는다. 어떤 실패도 세션 시작을 막지 않지만 진단은 싣는다.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]        # …/_governance/_engine
sys.path.insert(0, str(ENGINE))

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def emit_context(event: str, text: str) -> None:
    """두 하네스의 JSON 계약 — `[osk …]` 평문은 Codex에서 JSON으로 오인된다."""
    output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


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
    # 케이던스는 실행 방식별 안내가 맡는다. 이 공유 블록은 저장 경계와
    # `edits` 계약만 가르쳐 Stop 실행기에 user 턴 재촉을 겹쳐 싣지 않는다.
    #
    # 세션 키도 싣는다. 도구의 `session`은 이 값이어야 하는데 훅만 알고
    # 호출자는 몰라서 매번 지어냈고, 그 결속은 append-only로 영구히 쌓였다.
    return (
        f"[osk scope 기억 — = Scope/{st['scope']} · "
        f"{st['chars']}/{st['limit']}자 · 여유 {st['limit'] - st['chars']}자]\n"
        f"모든 세션·기기가 공유하는 기억이다 — 세션 한정 상태를 적지 말 것.\n"
        f"{st['session_note']}\n"
        f"대화 검토 시에는 자기 대화의 raw와 현재 공유 기억을 함께 검토하라. "
        f"오래 쓸 지식은 search로 찾은 기존 Scope 노드 갱신을 우선하고 출처·허브를 "
        f"완성한다. 요약에 머물 내용은 그 다음 scope 기억에 반영하고, 남길 것이 "
        f"없으면 사유를 남긴다. 요약 수정은 `edits`로 "
        f"`[{{old_text, new_text}}, …]`를 **다음 도구 호출에 함께** 실어라. "
        f"앵커는 아래 전문에서 그대로 복사하고(공백·줄바꿈까지, 마지막 줄엔 "
        f"개행이 없다) 해시는 필요 없다. 전문을 통째로 갈 때만 `text`와 "
        f"아래 `hash`를 쓴다.\n"
        f"hash: {st['hash']}\n---\n{text}")


def _bootstrap(key: str, *, bound: bool) -> str:
    arg = json.dumps(key, ensure_ascii=False)
    return (f"[osk 세션 시작 — session={arg}]\n"
            f"이 세션에서 `overview(session={arg})`를 한 번 불러 군집과 열린 사건을 "
            "확인하라. 기억을 묻는 질문에는 `search`를 먼저 쓴다. "
            + ("아래 scope 기억을 통합의 출발점으로 삼는다."
               if bound else "아직 scope 결속이 없다. 착지를 추측하지 말고 overview의 "
               "군집에서 해당 프로젝트를 확인한 뒤 scope_memory를 읽어라."))


def capture_block(env: dict, key: str, *, startup: bool = False) -> str:
    from osk import integration, response_growth
    try:
        captured = integration.hook_capture(env, key)
        harness, sid = captured["harness"], captured["conversation_id"]
        background = response_growth.initialize(env)
        if background is not None:
            failures = [r for r in (captured.get('response_growth_stop'), background.get('last_result'))
                        if r and not r.get('ok', True) and r.get('state') != 'running']
            error = captured['capture_error'] or '; '.join(r.get('error') or r['state'] for r in failures)
            if error:
                return f"[osk 백그라운드 검토 대기 — {error}; 본 작업은 계속한다. 완료로 처리하지 않았다.]"
            return ("[osk 대화 검토 — 최종 답변 Stop 9회마다 원대화와 같은 하네스·모델의 "
                    "구독 fork가 자기 대화를 검토한다. 실행 결과는 integration status에서 확인한다.]"
                    if startup else "")
        if startup:
            return integration.prompt(harness, sid)["text"] if captured["pending"] else ""
        cadence = integration.tick(harness, sid)
        if not cadence["due"] and not captured["capture_error"]:
            return ""
        lead = (f"[osk 케이던스 — user 턴 {cadence['unreviewed_prompts']}] "
                + ("이번엔 단독 턴이어도 된다. " if cadence["hard"] else
                   "다음 도구 호출에 함께 실어 검토하라 — 검토만을 위한 턴을 따로 쓰지 마라. "))
        parts = [lead, integration.prompt(harness, sid)["text"]]
    except Exception as exc:
        parts = [f"[osk 포착·통합 진단 — {type(exc).__name__}: {exc}. 본 작업은 계속한다; 대기를 완료로 처리하지 않았다.]"]
    # Native capture failures must not hide an independently readable scope's
    # recovery instructions. SessionStart already emits shared memory below.
    if not startup:
        try:
            from osk import scope_memory, write
            if write.resolve_session(key):
                parts.extend([scope_memory.recovery_block(key), _memory_block(scope_memory, key)])
        except Exception as exc:
            parts.append(f"[osk 공유 기억 판독 진단 — {type(exc).__name__}: {exc}]")
    return "\n\n".join(p for p in parts if p)


def main() -> None:
    if os.environ.get("OSK_GROWTH_WORKER") == "1":
        return  # maintenance evidence belongs to its run, not a new integration queue
    try:
        env = json.load(sys.stdin)
        if not isinstance(env, dict):
            raise ValueError("hook input must be a JSON object")
    except Exception as exc:
        emit_context("SessionStart", f"[osk 훅 입력 판독 진단 — {type(exc).__name__}: {exc}; 본 작업은 계속한다.]")
        return
    cwd = env.get("cwd") or os.getcwd()

    try:
        from osk import scope_memory, write, evictions
        key = session_key(cwd)
        captured = capture_block(env, key, startup=True)
        scope = write.resolve_session(key)
        bootstrap = _bootstrap(key, bound=bool(scope))
        recovery = ""
        try:
            recovery = scope_memory.recovery_block(key)
        except Exception:
            recovery = "[osk scope 복구 표식을 읽지 못했다 — CLI status로 확인하라]"
        if not scope:
            emit_context("SessionStart", "\n\n".join(p for p in (bootstrap, recovery, captured) if p))
            return
        mem = ""
        try:
            mem = _memory_block(scope_memory, key)
        except Exception as exc:
            mem = f"[osk 공유 기억 판독 진단 — {type(exc).__name__}: {exc}]"
        banner = block = ""
        try:
            banner, block = evictions.hook_block(scope, sys.executable, str(ENGINE))
        except Exception as exc:
            block = f"[osk 정돈 판독 진단 — {type(exc).__name__}: {exc}]"
        # 순서가 조문이다(§9-3 3항) — 밀림 경고가 맨 앞, 기억, 정돈 블록.
        out = "\n\n".join(p for p in (banner, bootstrap, recovery, mem, captured, block) if p)
        if not out:
            return
        emit_context("SessionStart", out)
    except Exception as exc:
        emit_context("SessionStart", f"[osk 세션 시작 진단 — {type(exc).__name__}: {exc}; 본 작업은 계속한다.]")


if __name__ == "__main__":
    main()
