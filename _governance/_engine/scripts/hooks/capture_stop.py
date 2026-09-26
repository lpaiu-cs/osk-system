"""Stop captures completion and, when configured, starts a background review.

The detached helper waits briefly for the native final marker. Daily catchup retries
missed tails. No blocking decision or forced continuation of the parent turn.
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
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))


def main() -> None:
    if os.environ.get("OSK_GROWTH_WORKER") == "1":
        return
    host = None
    try:
        from claude_session_start import note_run, session_key
        try:
            env = json.load(sys.stdin)
            if not isinstance(env, dict):
                raise ValueError("hook input must be a JSON object")
        except Exception:
            note_run("stop", None, None)
            raise
        from osk import integration, response_growth
        key = session_key(env.get("cwd") or os.getcwd())
        host = note_run("stop", env, key)
        try:
            if response_growth.launch(env, key):
                return
            result = integration.hook_capture(env, key)
        except integration.SubagentEvent:
            return  # The root conversation's own Stop owns its state.
        if result["capture_error"]:
            raise ValueError(result["capture_error"])
    except Exception as exc:
        _notice(host, f"[osk capture diagnostic - {type(exc).__name__}: {exc}; "
                      "user work may continue, capture remains pending.]")


if __name__ == "__main__":
    main()
