"""Stop hands completion's capture to a detached worker and, when configured, starts a
background review.

Neither reads the transcript in the hook (#192): the review supervisor waits briefly for the
native final marker, the hooks' worker captures what changed. Daily catchup retries missed
tails. No blocking decision or forced continuation of the parent turn.
"""
import json
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _notice(host, message: str) -> None:
    """User-visible diagnostic only: no decision:block and a success exit. Without a
    known host (or engine), the envelope both A hosts accept."""
    try:
        output = host.hook_notice("stop", message) if host else {"systemMessage": message}
    except Exception:
        output = {"systemMessage": message}
    # 평문 호스트(Kiro)는 종료 훅의 출력을 어디에도 싣지 않는다 — 어댑터가 빈 문자열을 준다.
    data = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    try:
        from claude_session_start import emit_raw
        emit_raw(data)
    except Exception:
        sys.stdout.buffer.write(data.encode("utf-8"))


def main() -> None:
    if os.environ.get("OSK_GROWTH_WORKER") == "1":
        return
    host = None
    try:
        from claude_session_start import first_call, normalize, note_run, project_cwd, session_key
        try:
            env = json.load(sys.stdin)
            if not isinstance(env, dict):
                raise ValueError("hook input must be a JSON object")
        except Exception:
            note_run("stop", None, None)
            raise
        from osk import integration, response_growth
        env = normalize(env)
        key = session_key(project_cwd(env))
        host = note_run("stop", env, key)
        if not first_call("stop", env, host):
            return
        try:
            if response_growth.launch(env, key):
                return
            response_growth.defer(env, key, probe_due=False)
            harness, sid, _ = integration.hook_source(env)
            seen = integration.status(harness, sid)
        except integration.SubagentEvent:
            return  # The root conversation's own Stop owns its state.
        # The worker's result lands after this hook: say the last capture's standing failure,
        # once as the input hook does — a new conversation's missing file is no failure.
        standing = seen["capture_error"] if seen["capture_recovery"]["state"] != "awaiting_input" else None
        if standing and integration.notice_once(harness, sid, standing):
            raise ValueError(standing)
    except Exception as exc:
        _notice(host, f"[osk capture diagnostic - {type(exc).__name__}: {exc}; "
                      "user work may continue, capture remains pending.]")


if __name__ == "__main__":
    main()
    from claude_session_start import finish
    finish()
