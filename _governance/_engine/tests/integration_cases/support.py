"""대화 포착과 검토의 수트의 바탕 — 임시 저장소를 세우고, 갈래들이 함께 쓰는 도우미를 둔다.

이 모듈을 읽는 것이 곧 격리다. 갈래 파일과 `tests/test_integration.py`은 엔진보다 이 모듈을 먼저 읽는다."""
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
TMP = tempfile.TemporaryDirectory(prefix="osk-integration-test-")
# The runner's temp may be a non-canonical spelling (Windows 8.3 RUNNER~1, macOS
# /var -> /private/var). Hand the engine that raw spelling, but build every
# expected path from the root the engine canonicalised at its boundary.
os.environ["OSK_VAULT_ROOT"] = str(Path(TMP.name) / "vault")
sys.path.insert(0, str(ENGINE))
from osk import core, raw, scope_memory, transcripts, validate, write, conversation_state  # noqa: E402
from osk.vault import Vault  # noqa: E402
VAULT = Vault.open()
ROOT = VAULT.root
# `VAULT.root` is fixed when osk is first imported. Loaded after another suite in one
# process, this module would get that suite's vault — once the repository itself — and
# write into it. Stop before the first write.
if ROOT != Path(os.environ["OSK_VAULT_ROOT"]).resolve():
    raise RuntimeError(f"test_integration needs its own process: osk is already bound to {ROOT}")

validate.make_mini_vault(VAULT, ROOT)
(ROOT / "00_Scope/Capture").mkdir()
scope_memory.replace(VAULT, "capture-tests", "", space="00_Scope/Capture")
write.set_primary(VAULT, "capture-tests", "Capture", "fixture")      # 쓰기는 결속만 더한다 — 주된 scope는 지정으로(Mechanism §6-2 6항)


def claude_round(sid, n, *, finished=True):
    def row(role, content, uid, stop=None, message_id=None):
        return {"type": role, "sessionId": sid, "uuid": uid, "isSidechain": False,
                "message": {"role": role, "content": content, "id": message_id,
                            "stop_reason": stop}}
    rs = [row("user", f"question {n}", f"user-{n}"),
          row("assistant", [{"type": "tool_use", "id": f"tool-{n}", "name": "probe", "input": {"n": n}}], f"tool-{n}", "tool_use", f"m-tool-{n}"),
          row("user", [{"type": "tool_result", "tool_use_id": f"tool-{n}", "content": "evidence result"}], f"result-{n}")]
    if finished:
        rs += [row("assistant", [{"type": "thinking", "thinking": "fixture reasoning"}], f"think-{n}", "end_turn", f"final-{n}"),
               row("assistant", [{"type": "text", "text": f"answer {n}"}], f"answer-{n}", "end_turn", f"final-{n}")]
    return rs


def claude_row(sid, role, content, uid, stop=None, message_id=None):
    return {"type": role, "sessionId": sid, "uuid": uid, "isSidechain": False,
            "message": {"role": role, "content": content, "id": message_id, "stop_reason": stop}}


def stamped(rows, base, step=1):
    """Claude's ISO row timestamps, `step` seconds apart from the epoch second `base`."""
    return [{**r, "timestamp": datetime.fromtimestamp(base + i * step, timezone.utc)
             .strftime("%Y-%m-%dT%H:%M:%S.000Z")} for i, r in enumerate(rows)]


def evidenced(body, *refs):
    """A body ending with one 근거 line per cited round — a distillation checks its round
    sources against these lines (Constitution art. 8 §6)."""
    lines = [f"- 2026-10-31 · `{raw.coordinate(VAULT, raw.raw_file(VAULT, raw.parse_ref(ref)[0]), raw.parse_ref(ref)[1])}`"
             " · 지지 — the cited turn" for ref in refs]
    return body + "\n\n## 근거\n\n" + "\n".join(lines)


def queued(sid, uid, words, *, kind="human", mode="prompt", reason="absorbed_mid_turn"):
    """A message sent while the turn ran: Claude Code's attachment and its queue removal."""
    rows = [{"type": "attachment", "sessionId": sid, "uuid": uid, "isSidechain": False,
             "attachment": {"type": "queued_command", "prompt": words, "source_uuid": f"cmd-{uid}",
                            "commandMode": mode, "origin": {"kind": kind}}}]
    if reason:
        rows.append({"type": "queue-operation", "operation": "remove", "reason": reason,
                     "commandUuid": f"cmd-{uid}", "sessionId": sid})
    return rows


def codex_round(n, *, finished=True):
    def row(typ, **payload):
        return {"type": typ, "payload": payload}
    rs = [row("event_msg", type="task_started", turn_id=f"turn-{n}"),
          row("turn_context", turn_id=f"turn-{n}"),
          row("response_item", type="message", role="user", content=[{"type": "input_text", "text": f"question {n}"}]),
          row("event_msg", type="user_message", message=f"question {n}", images=[]),
          row("response_item", type="function_call", name="probe", arguments="{}", call_id=f"tool-{n}"),
          row("response_item", type="function_call_output", call_id=f"tool-{n}", output="evidence result"),
          row("response_item", type="message", role="assistant", content=[{"type": "output_text", "text": f"answer {n}"}]),
          row("event_msg", type="agent_message", phase="final_answer", message=f"answer {n}")]
    if finished:
        rs.append(row("event_msg", type="task_complete", turn_id=f"turn-{n}", last_agent_message=f"answer {n}"))
    return rs


def codex_user_item(sid, n, text):
    return {"type": "event_msg", "payload": {
        "type": "item_completed", "thread_id": sid, "turn_id": f"turn-{n}",
        "item": {"type": "UserMessage", "id": f"user-{n}",
                 "content": [{"type": "text", "text": text}]}}}


def legacy_cursor(harness, sid, transcript, scope="Capture"):
    """Rewrite a fresh cursor as the full-capture engine (<= v4.1) left it: each completed
    round stored in `_raw/` and tracked by its block hash. Upgraded instances keep these."""
    path = conversation_state.state_path(VAULT, harness, sid)
    s = conversation_state.load_state(VAULT, path, harness, sid)
    parsed = transcripts.read(str(transcript), harness, sid)
    shown = parsed["dialogue_v1"]
    record = ROOT / "00_Scope" / scope / "_raw/.records" / (s["record"] + ".txt")
    blocks = [raw.round_block(i, raw.escape_numeric_h2(shown[r["id"]]["user"]),
                         raw.escape_numeric_h2(shown[r["id"]]["agent"]), dialogue_id=r["id"])
              for i, r in enumerate(parsed["rounds"], 1)]
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes("\n".join(blocks).encode("utf-8"))
    rel = record.relative_to(ROOT).as_posix()
    s["rounds"] = [{"id": r["id"], "ref": f"{rel}#{i}", "completion": r["completion"],
                    "hash": core.sha256_bytes(block.rstrip("\n").encode("utf-8"))}
                   for i, (r, block) in enumerate(zip(parsed["rounds"], blocks), 1)]
    s["snapshots"] = {conversation_state.snapshot(s): {"count": len(s["rounds"]), "prompt_count": s["prompt_count"]}}
    conversation_state.save_state(path, s)
    return s, record
