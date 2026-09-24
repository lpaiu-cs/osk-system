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


def main() -> None:
    if os.environ.get("OSK_GROWTH_WORKER") == "1":
        return
    try:
        from claude_session_start import session_key
        from osk import integration, response_growth
        env = json.load(sys.stdin)
        if not isinstance(env, dict):
            raise ValueError("hook input must be a JSON object")
        key = session_key(env.get("cwd") or os.getcwd())
        try:
            if response_growth.launch(env, key):
                return
            result = integration.hook_capture(env, key)
        except integration.SubagentEvent:
            return  # The root conversation's own Stop owns its state.
        if result["capture_error"]:
            raise ValueError(result["capture_error"])
    except Exception as exc:
        # Common hook diagnostic output, no decision:block and success exit.
        sys.stdout.buffer.write(json.dumps({"systemMessage":
            f"[osk capture diagnostic - {type(exc).__name__}: {exc}; user work may continue, capture remains pending.]"
        }, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
