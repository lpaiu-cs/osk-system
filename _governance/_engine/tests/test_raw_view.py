"""Bounded selection/search and source identity; run directly, no live vault."""
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="osk-raw-view-") as folder:
        os.environ["OSK_VAULT_ROOT"] = folder
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from osk import core, distillation, graph, raw, raw_view, validate
        validate.make_mini_vault(folder)
        import mcp_server

        user = json.dumps({"content": [{"type": "text", "text": "Keep the cache rule and its limitation."}]})
        events = [
            {"type": "reasoning", "encrypted_content": "opaque-secret" * 100000},
            {"type": "function_call", "arguments": "routine args " * 50000},
            {"type": "function_call_output", "output": "padding " * 20000 + "CACHE_LIMIT was 16; measured on cold boot."},
            {"type": "function_call_output", "output": json.dumps({"encrypted_content": "opaque-secret", "result": "CACHE_LIMIT corrected to 8 after retest."})},
            {"type": "function_call", "call_id": "remote-check-77", "arguments": "read remote checksum"},
            {"type": "function_call_output", "call_id": "remote-check-77", "output": "abcdef0123456789"},
            {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"text": "Retesting the cache."}]},
            {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"text": "Keep cache rule; confirm CACHE_LIMIT in the retest evidence."}]},
        ]
        agent = "\n\n".join(json.dumps(x) for x in events)
        first = raw.append_round("reading-test", "evidence", user, agent, "Scope/W1")
        ref = first["round_ref"]
        path = core.ROOT / first["path"]
        before = path.read_bytes()
        view = mcp_server.read_raw(ref, max_chars=100000)
        assert view["ok"] and view["view"] == "review", view
        assert view["chars"] > 1000000 and len(view["text"]) <= 6000
        assert "Keep the cache rule" in view["text"] and "confirm CACHE_LIMIT" in view["text"]
        assert "opaque-secret" not in view["text"] and "routine args" not in view["text"]
        assert "was 16" not in view["text"] and view["selection_only"]
        hits = mcp_server.read_raw(ref, query="CACHE_LIMIT")
        assert "was 16" in hits["text"] and "corrected to 8" in hits["text"], hits
        assert "opaque-secret" not in hits["text"] and len(hits["text"]) <= 6000
        command = mcp_server.read_raw(ref, query="remote checksum")
        assert "call remote-check-77" in command["text"]
        assert "abcdef0123456789" in mcp_server.read_raw(ref, query="remote-check-77")["text"]
        assert hits["hash"] == view["hash"] == distillation._source(ref, graph.Index())["hash"]
        assert path.read_bytes() == before, "reading must not rewrite immutable evidence"
        assert "opaque-secret" in raw.read_round(ref, 3000000)["text"], "legacy full view changed"
        raw.append_round("reading-test", "evidence", "next", "next answer", "Scope/W1")
        assert raw.read_round(ref, view="review")["hash"] == view["hash"], "append changed source identity"
        for bad in (dict(query=""), dict(query="x" * 201), dict(view="full", query="cache")):
            assert not mcp_server.read_raw(ref, **bad)["ok"], bad
        assert not mcp_server.read_raw(first["path"], query="cache")["ok"]

        # Claude content arrays and unlabelled final answers remain useful;
        # thinking never becomes visible evidence, including inside search results.
        claude = ('### user\nActual question\n### agent\n### assistant\n'
                  + json.dumps([{"type": "thinking", "thinking": "private thought"},
                                {"type": "text", "text": "Last visible conclusion"}])
                  + '\n### user\n' + json.dumps([{"type": "tool_result", "content": "counterexample FOUND"}]))
        visible = raw_view.project(claude)
        assert "Actual question" in visible["text"] and "Last visible conclusion" in visible["text"]
        assert "private thought" not in visible["text"] and "counterexample" not in visible["text"]
        assert "counterexample FOUND" in raw_view.project(claude, query="counterexample found")["text"]
        assert raw_view.project(claude, query="private thought")["selected_events"] == 0
        native = "### user\n" + json.dumps({"native_trigger": "heartbeat", "previous_input": "old request"})
        triggered = raw_view.project(native)["text"]
        assert "native_trigger_context" in triggered and "· user]" not in triggered
        # Long plain material cannot masquerade as complete; explicit queries find
        # evidence in its middle without an instruction to page over all of it.
        plain = "begin " + "padding " * 10000 + "MIDDLE_FINDING measured" + "padding " * 10000 + " end"
        clipped = raw_view.project(plain, 200)
        assert clipped["truncated"] and len(clipped["text"]) <= 200
        assert "MIDDLE_FINDING measured" in raw_view.project(plain, query="MIDDLE_FINDING")["text"]
        assert not raw_view.project(plain, query="absent")["text"]

        # Plain dialogue can start with JSON-looking tokens. Never split a
        # number/boolean/object prefix from the condition it qualifies.
        for statement, query in (
                ("16 workers are sufficient.", "16 workers"),
                ("0.05 seconds is the measured ceiling.", "0.05 ceiling"),
                ("-2 is the lower bound.", "-2 bound"),
                ("true means enabled.", "true enabled"),
                ('"16 workers" is a quotation.', "16 workers quotation"),
                ('{"workers": 16}', "workers 16"),
                ('[{"workers": 16}]', "workers 16"),
                ('{"type": "message", "content": "example"} is literal prose.', "message literal prose")):
            chunk = "### user\n\n" + statement + "\n\n### agent\n\n### assistant\n\n" + statement
            for result in (raw_view.project(chunk), raw_view.project(chunk, query=query)):
                assert result["selected_events"] == 2 and statement in result["text"], result

        reference = {"type": "tool_evidence_ref", "native_result": "claude:user1:answer1",
                     "hash_scheme": "dialogue-tool-manifest-v1", "sha256": "a" * 64,
                     "calls": 1, "results": 1, "tools": ["Read"]}
        claude_ref = '### user\n\nQuestion\n\n### agent\n\n### assistant\n\n' + json.dumps([
            {"type": "text", "text": "Read result needs verification."}]) + "\n\n" + json.dumps(reference)
        shown = raw_view.project(claude_ref)
        assert "native_evidence_reference" in shown["text"] and "claude:user1:answer1" in shown["text"], shown
        assert "Read result needs verification." in shown["text"]
        print("raw reading checks passed: bounded view/search, correction, opaque omission, unchanged provenance")


if __name__ == "__main__":
    main()
