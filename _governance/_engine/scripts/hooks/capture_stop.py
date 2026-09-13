"""Stop only requests capture; native completion still decides what may enter raw.

The scheduler's integration catchup retries a tail whose task_complete was not
yet flushed when Stop ran. No blocking decision or forced model continuation.
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
        from osk import integration
        env = json.load(sys.stdin)
        if not isinstance(env, dict):
            raise ValueError("hook input must be a JSON object")
        result = integration.hook_capture(env, session_key(env.get("cwd") or os.getcwd()))
        if result["capture_error"]:
            raise ValueError(result["capture_error"])
    except Exception as exc:
        # Common hook diagnostic output, no decision:block and success exit.
        sys.stdout.buffer.write(json.dumps({"systemMessage":
            f"[osk capture diagnostic - {type(exc).__name__}: {exc}; user work may continue, capture remains pending.]"
        }, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
