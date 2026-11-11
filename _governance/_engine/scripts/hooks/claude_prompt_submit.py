"""Claude/Codex UserPromptSubmit: capture own completed rounds and durable review cadence."""
import json
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    if os.environ.get("OSK_GROWTH_WORKER") == "1":
        return
    from claude_session_start import (_budget, capture_block, emit_context, first_call, normalize,
                                      note_run, open_vault, project_cwd, session_key)
    host = None
    try:
        vault = open_vault()
        try:
            env = json.load(sys.stdin)
            if not isinstance(env, dict):
                raise ValueError("hook input must be a JSON object")
        except Exception as exc:
            note_run(vault, "input", None, None)
            emit_context("input", f"[osk hook diagnostic - {type(exc).__name__}: {exc}; user work may continue.]")
            return
        env = normalize(env)
        key = session_key(vault, project_cwd(env))
        host = note_run(vault, "input", env, key)
        # A host may call its input event more than once per user turn (Antigravity's
        # PreInvocation fires before every model call); only the first is osk's input.
        if host is not None and not host.fires("input", env):
            return
        if not first_call(vault, "input", env, host):
            return
        body = capture_block(vault, env, key, budget=_budget(env, host))
    except Exception as exc:
        body = f"[osk hook diagnostic - {type(exc).__name__}: {exc}; user work may continue.]"
    if body:
        emit_context("input", body, host=host)


if __name__ == "__main__":
    main()
    from claude_session_start import finish
    finish()
