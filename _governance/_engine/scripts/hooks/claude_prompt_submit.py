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
    from claude_session_start import capture_block, emit_context, first_call, note_run, session_key
    try:
        env = json.load(sys.stdin)
        if not isinstance(env, dict):
            raise ValueError("hook input must be a JSON object")
    except Exception as exc:
        note_run("input", None, None)
        emit_context("input", f"[osk hook diagnostic - {type(exc).__name__}: {exc}; user work may continue.]")
        return
    host = None
    try:
        key = session_key(env.get("cwd") or os.getcwd())
        host = note_run("input", env, key)
        if not first_call("input", env, host):
            return
        body = capture_block(env, key)
    except Exception as exc:
        body = f"[osk hook diagnostic - {type(exc).__name__}: {exc}; user work may continue.]"
    if body:
        emit_context("input", body, host=host)


if __name__ == "__main__":
    main()
