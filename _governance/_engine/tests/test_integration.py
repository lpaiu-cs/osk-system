"""Isolated native transcript, crash retry, and durable conversation review checks.

Run: python _governance/_engine/tests/test_integration.py
Native fixture envelopes mirror inspected Claude 2.x JSONL and Codex task_complete
rows. These checks prove parser/mechanical state boundaries, not semantic quality.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="osk-integration-test-")
# The runner's temp may be a non-canonical spelling (Windows 8.3 RUNNER~1, macOS
# /var -> /private/var). Hand the engine that raw spelling, but build every
# expected path from the root the engine canonicalised at its boundary.
os.environ["OSK_VAULT_ROOT"] = str(Path(TMP.name) / "vault")
sys.path.insert(0, str(ENGINE))
from osk import core, integration as it, raw, scope_memory, transcripts, validate, write
ROOT = core.ROOT
# `core.ROOT` is fixed when osk is first imported. Loaded after another suite in one
# process, this module would get that suite's vault — once the repository itself — and
# write into it. Stop before the first write.
if ROOT != Path(os.environ["OSK_VAULT_ROOT"]).resolve():
    raise RuntimeError(f"test_integration needs its own process: osk is already bound to {ROOT}")

validate.make_mini_vault(ROOT)
(ROOT / "00_Scope/Capture").mkdir()
scope_memory.replace("capture-tests", "", space="00_Scope/Capture")


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
    path = it.state_path(harness, sid)
    s = it._load(path, harness, sid)
    parsed = transcripts.read(str(transcript), harness, sid)
    shown = parsed["dialogue_v1"]
    record = ROOT / "00_Scope" / scope / "_raw/.records" / (s["record"] + ".txt")
    blocks = [raw._block(i, raw.escape_numeric_h2(shown[r["id"]]["user"]),
                         raw.escape_numeric_h2(shown[r["id"]]["agent"]), dialogue_id=r["id"])
              for i, r in enumerate(parsed["rounds"], 1)]
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes("\n".join(blocks).encode("utf-8"))
    rel = record.relative_to(ROOT).as_posix()
    s["rounds"] = [{"id": r["id"], "ref": f"{rel}#{i}", "completion": r["completion"],
                    "hash": core.sha256_bytes(block.rstrip("\n").encode("utf-8"))}
                   for i, (r, block) in enumerate(zip(parsed["rounds"], blocks), 1)]
    s["snapshots"] = {it._snapshot(s): {"count": len(s["rounds"]), "prompt_count": s["prompt_count"]}}
    it._save(path, s)
    return s, record


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sid = self._testMethodName
        self.path = Path(TMP.name) / f"{self.sid}.jsonl"

    def transcript(self, rows):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def capture(self, harness="claude"):
        return it.capture(harness, self.sid, str(self.path), "capture-tests")


    def test_codex_history_rejects_missing_foreign_cyclic_and_partial_sources(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME': home}):
            folder = Path(home) / 'archived_sessions'
            folder.mkdir()
            parent = folder / f'rollout-parent-{self.sid}.jsonl'
            child = folder / f'rollout-child-{self.sid}_continuation.jsonl'
            header = {'type': 'session_meta', 'payload': {'id': self.sid}}
            body = ''.join(json.dumps(r) + '\n' for r in [header] + codex_round(1)).encode()
            base = {'thread_id': self.sid, 'end_byte_offset': len(body)}
            def child_bytes(value):
                return ''.join(json.dumps(r) + '\n' for r in [
                    {'type':'session_meta', 'payload':{'id':self.sid, 'history_base':value}}] + codex_round(2)).encode()
            child.write_bytes(child_bytes(base))
            for mode in ('missing', 'foreign', 'cycle', 'partial', 'oversized', 'invalid', 'ambiguous'):
                with self.subTest(mode=mode):
                    parent.write_bytes(body)
                    child.write_bytes(child_bytes(base))
                    if mode == 'missing':
                        parent.unlink()
                    elif mode == 'foreign':
                        parent.write_bytes(body.replace(self.sid.encode(), b'other-conversation'))
                    elif mode == 'cycle':
                        child.write_bytes(child_bytes(dict(base, thread_id='continuation')))
                    elif mode == 'partial':
                        child.write_bytes(child_bytes(dict(base, end_byte_offset=len(body)-2)))
                    elif mode == 'oversized':
                        child.write_bytes(child_bytes(dict(base, end_byte_offset=len(body)+1)))
                    elif mode == 'invalid':
                        child.write_bytes(child_bytes(dict(base, thread_id='../foreign')))
                    else:
                        (folder / f'rollout-copy-{self.sid}.jsonl').write_bytes(body)
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        transcripts.read(str(child), 'codex', self.sid)

    def test_codex_compaction_only_turn_is_not_missing_dialogue(self):
        rows = [{'type':'session_meta', 'payload':{'id':self.sid}},
                {'type':'event_msg', 'payload':{'type':'task_started', 'turn_id':'compact'}},
                {'type':'compacted', 'payload':{'message':'internal summary'}},
                {'type':'event_msg', 'payload':{'type':'item_completed', 'thread_id':self.sid,
                    'turn_id':'compact', 'item':{'type':'ContextCompaction', 'id':'compact-item'}}},
                {'type':'event_msg', 'payload':{'type':'task_complete', 'turn_id':'compact', 'last_agent_message':None}}]
        self.transcript(rows + codex_round(1))
        captured = self.capture('codex')
        self.assertTrue(captured['ok'], captured)
        self.assertEqual(captured['captured_rounds'], 1)
        parsed = transcripts.read(str(self.path), 'codex', self.sid)
        self.assertEqual(parsed['diagnostics'], [])
        self.assertFalse(parsed['pending_tail'])
        # A real human turn with missing response still needs a diagnostic.
        rows.insert(2, {'type':'event_msg', 'payload':{'type':'user_message', 'message':'real question'}})
        self.transcript(rows)
        self.assertTrue(transcripts.read(str(self.path), 'codex', self.sid)['diagnostics'])


    def test_failed_codex_turn_and_inputless_retry_keep_observed_evidence(self):
        failed = codex_round(1)
        failed[-1]['payload'].update(last_agent_message=None, error={'message':'at capacity'})
        retry = codex_round(2)
        del retry[2:4]
        self.transcript([{'type':'session_meta','payload':{'id':self.sid}}] + failed + retry)
        parsed = transcripts.read(str(self.path), 'codex', self.sid)
        self.assertEqual(parsed['diagnostics'], [])
        self.assertEqual([r['completion'] for r in parsed['rounds']], ['failed','completed'])
        self.assertIn('at capacity', parsed['rounds'][0]['agent'])
        self.assertIn('inputless_after_terminal', parsed['rounds'][1]['user'])
        self.assertIn('turn-1', parsed['rounds'][1]['user'])
        self.assertIn('evidence result', parsed['rounds'][1]['agent'])

    def test_goal_and_silent_heartbeat_are_labeled_native_triggers(self):
        goal = codex_round(1)
        goal[2]['payload']['internal_chat_message_metadata_passthrough'] = {
            'turn_id':'turn-1', 'content_item_kinds':['goal.internal_context']}
        goal.pop(3)
        heartbeat = codex_round(2)
        heartbeat[2] = {'type':'response_item','payload':{
            'type':'function_call_output','name':'automation_update','namespace':'codex_app',
            'output':'<heartbeat>inspect current state</heartbeat>',
            'internal_chat_message_metadata_passthrough':{'turn_id':'turn-2'}}}
        heartbeat.pop(3)
        heartbeat[-3]['payload'].update(phase='final_answer',content=[{'type':'output_text','text':''}])
        heartbeat[-1]['payload']['last_agent_message'] = None
        rows = [{'type':'session_meta','payload':{'id':self.sid}}] + goal + heartbeat
        self.transcript(rows)
        parsed = transcripts.read(str(self.path), 'codex', self.sid)
        self.assertEqual(parsed['diagnostics'], [])
        self.assertEqual(len(parsed['rounds']), 2)
        self.assertIn('goal', parsed['rounds'][0]['user'])
        self.assertIn('heartbeat', parsed['rounds'][1]['user'])
        self.assertIn('task_complete', parsed['rounds'][1]['agent'])
        goal[2]['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'foreign'
        self.transcript(rows)
        with self.assertRaises(ValueError):
            transcripts.read(str(self.path), 'codex', self.sid)

    def test_superseded_turn_is_partial_and_open_tail_stays_pending(self):
        self.transcript([{'type':'session_meta','payload':{'id':self.sid}}]
                        + codex_round(1, finished=False) + codex_round(2, finished=False))
        parsed = transcripts.read(str(self.path), 'codex', self.sid)
        self.assertEqual([r['completion'] for r in parsed['rounds']], ['interrupted'])
        self.assertTrue(parsed['pending_tail'])
        self.assertIn('superseded', parsed['rounds'][0]['agent'])
        tail = codex_round(3, finished=False)
        del tail[2:4]
        self.transcript([{'type':'session_meta','payload':{'id':self.sid}}] + tail)
        self.assertTrue(transcripts.read(str(self.path), 'codex', self.sid)['pending_tail'])


    def test_claude_completion_and_tool_results(self):
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2, finished=False))
        st = self.capture()
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["captured_rounds"], 1)
        self.assertTrue(st["capture_pending"])
        self.assertFalse(raw.record_path("Capture", st["record"]).exists())  # tracking copies no dialogue
        [turn] = self.turns()
        text = turn["user"] + "\n" + turn["agent"]
        self.assertNotIn("evidence result", text)
        self.assertIn("tool_evidence_ref", text)
        self.assertIn('"calls": 1', text)
        self.assertIn("answer 1", text)
        self.assertNotIn("question 2", text)


    def test_end_turn_thinking_is_not_a_final_response(self):
        self.transcript(claude_round(self.sid, 1)[:-1])
        parsed = transcripts.read(str(self.path), "claude", self.sid)
        self.assertEqual(parsed["rounds"], [])
        self.assertTrue(parsed["pending_tail"])

    def test_codex_requires_matching_task_complete(self):
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1) + codex_round(2, finished=False))
        st = self.capture("codex")
        self.assertEqual(st["captured_rounds"], 1)
        self.assertTrue(st["capture_pending"])
        [turn] = self.turns("codex")
        text = turn["user"] + "\n" + turn["agent"]
        self.assertIn("tool_evidence_ref", text)
        self.assertNotIn("evidence result", text)
        self.assertEqual(text.count("question 1"), 1)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "wrong", "last_agent_message": "answer"}}) + "\n")
        self.assertFalse(self.capture("codex")["ok"])


    def test_codex_native_user_item_capture_and_replay(self):
        rows = codex_round(1)
        rows[3] = codex_user_item(self.sid, 1, "question 1")
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + rows)
        captured = self.capture("codex")
        self.assertEqual(captured["captured_rounds"], 1, captured)
        self.assertFalse(captured["capture_pending"], captured)
        [turn] = self.turns("codex")
        text = turn["user"] + "\n" + turn["agent"]
        self.assertEqual(text.count("question 1"), 1)
        self.assertNotIn("evidence result", text)
        self.assertIn("tool_evidence_ref", text)
        self.assertIn("answer 1", text)
        self.assertEqual(self.capture("codex")["appended"], 0)


    def test_codex_mixed_user_envelopes_preserve_distinct_inputs(self):
        header = [{"type": "session_meta", "payload": {"id": self.sid}}]
        rows = codex_round(1)
        legacy, native = rows[3], codex_user_item(self.sid, 1, "question 1")

        def parse(inputs):
            self.transcript(header + rows[:3] + inputs + rows[4:])
            return transcripts.read(str(self.path), "codex", self.sid)["rounds"][0]

        expected = parse([legacy])
        for inputs in ([legacy, native], [native, legacy]):
            actual = parse(inputs)
            self.assertEqual(actual["user"], expected["user"])
            self.assertEqual(actual["agent"], expected["agent"])
        distinct = codex_user_item(self.sid, 1, "independent correction")
        actual = parse([distinct, legacy, native])
        self.assertLess(actual["user"].index("independent correction"), actual["user"].index("question 1"))
        self.assertEqual(actual["user"].count("question 1"), 1)
        self.assertEqual(parse([legacy, legacy, native, native])["user"].count("question 1"), 2)
        rich = codex_user_item(self.sid, 1, "question 1")
        rich["payload"]["item"]["content"].append({"type": "image", "url": "https://example.invalid/image"})
        self.assertIn("https://example.invalid/image", parse([legacy, rich])["user"])


    def test_codex_native_identity_and_completion_boundaries(self):
        header = [{"type": "session_meta", "payload": {"id": self.sid}}]
        rows = codex_round(1)
        native = codex_user_item(self.sid, 1, "question 1")
        for field in ("thread_id", "turn_id"):
            wrong = json.loads(json.dumps(native))
            wrong["payload"][field] = "another-conversation-or-turn"
            self.transcript(header + rows[:3] + [wrong] + rows[4:])
            with self.assertRaisesRegex(ValueError, "user item identity mismatch"):
                transcripts.read(str(self.path), "codex", self.sid)
        self.transcript(header + rows[:3] + [native] + rows[4:-1])
        parsed = transcripts.read(str(self.path), "codex", self.sid)
        self.assertEqual(parsed["rounds"], [])
        self.assertTrue(parsed["pending_tail"])
        aborted = {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "turn-1"}}
        self.transcript(header + rows[:3] + [native, aborted])
        self.assertEqual(transcripts.read(str(self.path), "codex", self.sid)["rounds"][0]["completion"], "aborted")
        # response_item user also carries injected context; it is not a fallback prompt.
        self.transcript(header + rows[:3] + rows[4:])
        self.assertEqual(transcripts.read(str(self.path), "codex", self.sid)["rounds"], [])


    def test_resume_empty_memory_and_other_writer_never_ack(self):
        self.transcript(claude_round(self.sid, 1))
        st = self.capture()
        for _ in range(8):
            self.assertFalse(it.tick("claude", self.sid)["due"])
        self.assertTrue(it.tick("claude", self.sid)["due"])
        self.assertIn("공유 기억이 비어 있어도", it.prompt("claude", self.sid)["text"])
        cur = scope_memory.read("capture-tests")
        scope_memory.replace("other-writer", "other session writes shared memory", cur["hash"], "00_Scope/Capture")
        resumed = it.hook_capture({"harness": "claude", "session_id": self.sid, "transcript_path": str(self.path)}, "capture-tests")
        self.assertEqual(resumed["prompt_count"], 9)
        self.assertIsNone(resumed["reviewed_through"])
        self.assertTrue(resumed["pending"])
        other = it.status("claude", self.sid + "-other")
        self.assertFalse(other["pending"])
        self.assertNotIn(st["pending_refs"][0], it.prompt("claude", self.sid + "-other")["text"])

    def test_scope_routing_changes_are_recorded_and_same_scope_keys_resume(self):
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        write.bind_session(self.sid + "-renamed", "Capture")
        resumed = it.capture("claude", self.sid, str(self.path), self.sid + "-renamed")
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(first["pending_refs"], resumed["pending_refs"])
        write.alias_session(self.sid + "-alias", self.sid + "-renamed")
        self.assertTrue(it.capture("claude", self.sid, str(self.path), self.sid + "-alias")["ok"])
        refused = it.capture("claude", self.sid, str(self.path), self.sid + "-elsewhere", "00_Scope/W1")
        self.assertFalse(refused["ok"])
        self.assertTrue(it.status("claude", self.sid)["capture_pending"])
        self.assertIn("scope changed", it.status("claude", self.sid)["capture_error"])
        self.assertEqual(first["pending_refs"], refused["pending_refs"])
        self.assertTrue(it.capture("claude", self.sid, str(self.path), self.sid + "-renamed")["ok"])

    def test_capture_normalizes_saved_and_requested_scope(self):
        self.transcript(claude_round(self.sid, 1))
        first = it.capture("claude", self.sid, str(self.path), "capture-tests", "00_Scope/Capture/")
        self.assertTrue(first["ok"], first)
        # Also repair a successful cursor written with the previous spelling.
        path = it.state_path("claude", self.sid)
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["space"], "00_Scope/Capture")
        state["space"] = "00_Scope/Capture/"
        path.write_text(json.dumps(state), encoding="utf-8")
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        resumed = self.capture()
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["appended"], 1)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["space"], "00_Scope/Capture")

    def test_rejected_first_landing_does_not_pin_the_conversation(self):
        for n, space in enumerate(("00_Scope/W1", "00_Scope/Absent", "Capture")):
            sid = self.sid + str(n)
            self.transcript(claude_round(sid, 1))
            rejected = it.capture("claude", sid, str(self.path), "capture-tests", space)
            self.assertFalse(rejected["ok"], rejected)
            self.assertTrue(rejected["capture_pending"])
            self.assertEqual(rejected["captured_rounds"], 0)
            state = json.loads(it.state_path("claude", sid).read_text(encoding="utf-8"))
            self.assertIsNone(state["space"])
            self.assertFalse(raw.record_path("Capture", rejected["record"]).exists())
            resumed = it.capture("claude", sid, str(self.path), "capture-tests")
            self.assertTrue(resumed["ok"], resumed)
            self.assertEqual(resumed["appended"], 1)

    def test_missing_claude_native_file_persists_retry_before_harness_detection(self):
        base = Path(TMP.name) / "claude-home"
        native = base / "projects" / "project" / (self.sid + ".jsonl")
        env = {"session_id": self.sid, "transcript_path": str(native)}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(base)}, clear=False):
            failed = it.hook_capture(env, "capture-tests")
            self.assertFalse(failed["ok"])
            self.assertIn("FileNotFoundError", failed["capture_error"])
            self.assertTrue(it.state_path("claude", self.sid).exists())
            native.parent.mkdir(parents=True)
            native.write_text("".join(json.dumps(r) + "\n" for r in claude_round(self.sid, 1)), encoding="utf-8")
            recovered = it.hook_capture(env, "capture-tests")
            self.assertTrue(recovered["ok"], recovered)
            self.assertEqual(recovered["captured_rounds"], 1)


    def test_claude_copied_prefix_is_not_reused_across_scopes(self):
        parent = self.sid + "-parent"
        rows = claude_round(parent, 1)
        rows[0]["uuid"] = str(uuid.uuid4())
        self.transcript(rows)
        original = it.capture("claude", parent, str(self.path), "capture-tests")
        self.assertTrue(original["ok"], original)
        self.transcript([dict(r, sessionId=self.sid) for r in rows])
        copied = it.capture("claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        self.assertTrue(copied["ok"], copied)
        self.assertEqual((copied["captured_rounds"], copied["inherited_rounds"]), (1, 0))
        self.assertEqual(copied["pending_refs"], [raw.native_ref("claude", self.sid, rows[0]["uuid"])])


    def test_claude_copied_prefix_matches_streamed_tool_order(self):
        # Streaming execution appends a result between the parallel calls of one
        # message; a resumed copy rewrites them in message order and keeps the
        # rows' timestamps. The copy must still reuse the parent's raw.
        parent = self.sid + "-streamed"
        ids = lambda name: str(uuid.uuid5(uuid.NAMESPACE_URL, parent + name))

        def row(role, content, name, second, stop=None, message_id=None):
            return {"type": role, "sessionId": parent, "uuid": ids(name), "isSidechain": False,
                    "timestamp": f"2026-09-27T09:13:{second:02d}.000Z",
                    "message": {"role": role, "content": content, "id": message_id, "stop_reason": stop}}
        call = lambda n: [{"type": "tool_use", "id": f"call-{n}", "name": "probe", "input": {"n": n}}]
        result = lambda n: [{"type": "tool_result", "tool_use_id": f"call-{n}", "content": f"evidence {n}"}]
        q, c1, r1, c2, r2, end = (row("user", "question", "q", 1),
                                  row("assistant", call(1), "c1", 2, "tool_use", "m-1"),
                                  row("user", result(1), "r1", 3),
                                  row("assistant", call(2), "c2", 4, "tool_use", "m-1"),
                                  row("user", result(2), "r2", 5),
                                  row("assistant", [{"type": "text", "text": "answer"}], "end", 6, "end_turn", "m-2"))
        self.transcript([q, c1, r1, c2, r2, end])
        self.assertEqual(it.capture("claude", parent, str(self.path), "capture-tests")["captured_rounds"], 1)
        for child, changed in ((self.sid + "-copied", False), (self.sid + "-altered", True)):
            tail = claude_round(child, 2)
            for n, r in enumerate(tail):
                r["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, child + str(n)))
                r["timestamp"] = f"2026-09-27T09:14:{n:02d}.000Z"
            copied = json.loads(json.dumps([dict(r, sessionId=child) for r in (q, c1, c2, r1, r2, end)]))
            if changed:  # Order is forgiven, content is not.
                copied[3]["message"]["content"][0]["content"] = "other evidence"
            self.transcript(copied + tail)
            captured = it.capture("claude", child, str(self.path), "capture-tests")
            if changed:
                self.assertFalse(captured["ok"])
                self.assertIn("copied native round ID has different content", captured["capture_error"])
            else:
                self.assertTrue(captured["ok"], captured)
                self.assertEqual((captured["captured_rounds"], captured["inherited_rounds"]), (1, 1))
                self.assertEqual(it.capture("claude", child, str(self.path), "capture-tests")["appended"], 0)

    def test_claude_foreign_history_requires_actual_parent_chain(self):
        parent = claude_round("parent", 1)
        child = claude_round(self.sid, 2)
        child[0]["parentUuid"] = "unrelated"
        self.transcript(parent + child)
        self.assertFalse(self.capture()["ok"])
        child[0]["parentUuid"] = parent[-1]["uuid"]
        self.transcript(parent + child)
        self.assertTrue(self.capture()["ok"])
        self.transcript(parent + child + claude_round("unrelated", 3))
        self.assertFalse(self.capture()["ok"])

    def test_claude_metadata_only_fork_has_no_new_completed_round(self):
        self.transcript(claude_round("parent", 1) + [
            {"type": "custom-title", "sessionId": self.sid, "customTitle": "fork"}])
        result = self.capture()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["captured_rounds"], 0)

    def test_exact_snapshot_ack_leaves_newer_round_pending(self):
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        it.tick("claude", self.sid)
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        latest = self.capture()
        deferred = it.acknowledge("claude", self.sid, first["through"], "deferred", "need a source check")
        self.assertEqual(deferred["reviewed_rounds"], 0)
        ack = it.acknowledge("claude", self.sid, first["through"], "no_value", "first round is transient")
        self.assertEqual(ack["reviewed_rounds"], 1)
        self.assertEqual(ack["pending_refs"], latest["pending_refs"][1:])
        self.assertFalse(ack["last_review"]["mechanical_preservation"])
        with self.assertRaises(ValueError):
            it.acknowledge("claude", self.sid + "-other", latest["through"], "no_value", "cannot acknowledge another conversation")

    def test_summary_is_verified_and_not_node_success(self):
        self.transcript(claude_round(self.sid, 1))
        st = self.capture()
        with self.assertRaises(ValueError):
            it.acknowledge("claude", self.sid, st["through"], "summary", "summary only", [{"text": "missing excerpt"}])
        cur = scope_memory.read("capture-tests")
        scope_memory.replace("capture-tests", "an exact saved summary", cur["hash"])
        done = it.acknowledge("claude", self.sid, st["through"], "summary", "temporary summary is sufficient", [{"text": "exact saved summary"}])
        self.assertFalse(done["pending"])
        self.assertFalse(done["last_review"]["mechanical_preservation"])
        self.assertFalse(done["last_review"]["semantic_verified"])

    def test_capture_failure_is_pending_and_partial_tail_not_consumed(self):
        st = self.capture()
        self.assertFalse(st["ok"])
        self.assertTrue(st["pending"])
        self.transcript(claude_round(self.sid, 1))
        with self.path.open("ab") as f:
            f.write(b"{partial")
        st = self.capture()
        self.assertEqual(st["captured_rounds"], 1)
        self.assertTrue(st["capture_pending"])
        self.assertIn("incomplete JSONL", st["capture_error"])
        job = it.prompt("claude", self.sid, include_organization=False)
        self.assertEqual(job["raw_review"]["state"], "native")
        self.assertEqual(job["raw_review"]["rounds"], 1)


    def test_unstarted_missing_native_does_not_hide_active_missing_source(self):
        absent = self.capture()
        self.assertTrue(absent['pending'])
        self.assertEqual(absent["capture_recovery"]["state"], "awaiting_input")
        state_path = it.state_path('claude', self.sid)
        before = state_path.read_bytes()
        states, _, _, waiting = it._known_pending(100)
        self.assertNotIn(self.sid, [s['conversation_id'] for s in states])
        self.assertIn(self.sid, [s['conversation_id'] for s in waiting])
        self.assertEqual(state_path.read_bytes(), before)
        it.tick('claude', self.sid)
        states, _, _, waiting = it._known_pending(100)
        self.assertIn(self.sid, [s['conversation_id'] for s in states])
        self.assertNotIn(self.sid, [s['conversation_id'] for s in waiting])
        missing = self.capture()
        self.assertFalse(missing['ok'])
        self.assertEqual(missing["capture_recovery"]["state"], "action_required")
        self.transcript(claude_round(self.sid,1))
        self.assertEqual(self.capture()['appended'], 1)
        self.path.unlink()
        states, _, _, waiting = it._known_pending(100)
        self.assertIn(self.sid, [s['conversation_id'] for s in states])
        self.assertNotIn(self.sid, [s['conversation_id'] for s in waiting])

    def test_unstarted_native_arrival_is_discovered_without_prompt_tick(self):
        self.capture()
        self.transcript(claude_round(self.sid,1))
        states, _, _, waiting = it._known_pending(100)
        self.assertIn(self.sid, [s['conversation_id'] for s in states])
        self.assertNotIn(self.sid, [s['conversation_id'] for s in waiting])
        self.assertEqual(self.capture()['appended'], 1)

    def test_archived_codex_recovery_keeps_prefix_and_review_receipts(self):
        self.transcript([{'type': 'session_meta', 'payload': {'id': self.sid}}] + codex_round(1))
        first = self.capture('codex')
        it.acknowledge('codex', self.sid, first['through'], 'no_value', 'synthetic fixture')
        old = it._load(it.state_path('codex', self.sid), 'codex', self.sid)
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME': home}):
            archived = Path(home) / 'archived_sessions' / f'rollout-date-{self.sid}.jsonl'
            archived.parent.mkdir()
            self.path.replace(archived)
            with archived.open('a', encoding='utf-8') as f:
                f.write(''.join(json.dumps(r) + '\n' for r in codex_round(2)))
            recovered = it.hook_capture({'harness': 'codex', 'session_id': self.sid}, 'capture-tests')
            self.assertTrue(recovered['ok'], recovered)
            self.assertEqual(recovered['appended'], 1)
            self.assertEqual(recovered['transcript_path'], str(archived.resolve()))
            new = it._load(it.state_path('codex', self.sid), 'codex', self.sid)
            self.assertEqual(new['rounds'][:1], old['rounds'])
            self.assertEqual(new['reviews'], old['reviews'])
            self.assertEqual(new['reviewed_count'], 1)
            self.assertEqual(self.capture('codex')['appended'], 0)

    def test_native_relocation_rejects_ambiguity_and_wrong_identity(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME': home}):
            self.capture('codex')
            archived = Path(home) / 'archived_sessions' / f'rollout-date-{self.sid}.jsonl'
            resumed = Path(home) / 'sessions/2026/09/17' / f'rollout-date-{self.sid}_resume.jsonl'
            archived.parent.mkdir()
            resumed.parent.mkdir(parents=True)
            body = [{'type': 'session_meta', 'payload': {'id': self.sid}}] + codex_round(1)
            archived.write_text(''.join(json.dumps(r) + '\n' for r in body), encoding='utf-8')
            states, _, _, waiting = it._known_pending(100)
            self.assertIn(self.sid, [s['conversation_id'] for s in states])
            self.assertNotIn(self.sid, [s['conversation_id'] for s in waiting])
            resumed.write_bytes(archived.read_bytes())
            refused = self.capture('codex')
            self.assertIn('multiple transcripts', refused['capture_error'])
            self.assertEqual(refused['captured_rounds'], 0)
            archived.unlink()
            body[0]['payload']['id'] = 'another-conversation'
            resumed.write_text(''.join(json.dumps(r) + '\n' for r in body), encoding='utf-8')
            refused = self.capture('codex')
            self.assertFalse(refused['ok'])
            self.assertEqual(refused['transcript_path'], str(self.path.resolve()))
            self.assertEqual(refused['captured_rounds'], 0)
            body[0]['payload']['id'] = self.sid
            resumed.write_text(''.join(json.dumps(r) + '\n' for r in body), encoding='utf-8')
            self.assertEqual(self.capture('codex')['appended'], 1)
            archived.write_bytes(resumed.read_bytes())
            # An existing explicit source stays authoritative, even with two candidates.
            self.assertTrue(it.capture('codex', self.sid, str(resumed), 'capture-tests')['ok'])

    def test_native_identity_mismatch_and_changed_prefix_fail_closed(self):
        self.transcript(claude_round("not-this-conversation", 1))
        self.assertFalse(self.capture()["ok"])
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        self.assertTrue(first["ok"])
        # A rewound transcript no longer holds the tracked turn.
        rewound = claude_round(self.sid, 1)
        rewound[0]["uuid"] = "user-1-rewound"
        self.transcript(rewound)
        st = self.capture()
        self.assertFalse(st["ok"])
        self.assertIn("identity prefix changed", st["capture_error"])
        self.assertEqual(st["pending_refs"], first["pending_refs"])


    def test_rejected_explicit_native_path_keeps_the_verified_source(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                sid = self.sid + "-" + harness
                def rows(identity, count):
                    if harness == "claude":
                        return sum((claude_round(identity, n) for n in range(1, count + 1)), [])
                    return ([{"type": "session_meta", "payload": {"id": identity}}]
                            + sum((codex_round(n) for n in range(1, count + 1)), []))
                self.transcript(rows(sid, 1))
                first = it.capture(harness, sid, str(self.path), "capture-tests")
                it.acknowledge(harness, sid, first["through"], "no_value", "Completed fixture only.")
                state_path = it.state_path(harness, sid)
                before = it._load(state_path, harness, sid)
                other = self.path.with_suffix(".foreign.jsonl")
                other.write_text("".join(json.dumps(r) + "\n" for r in rows("foreign", 1)), encoding="utf-8")
                refused = it.capture(harness, sid, str(other), "capture-tests")
                self.assertFalse(refused["ok"], refused)
                self.assertEqual(refused["capture_recovery"]["phase"], "read")
                self.assertEqual(it._load(state_path, harness, sid)["transcript_path"], before["transcript_path"])
                self.transcript(rows(sid, 2))
                recovered = it.capture(harness, sid, None, "capture-tests")
                self.assertTrue(recovered["ok"], recovered)
                self.assertEqual(recovered["appended"], 1)
                after = it._load(state_path, harness, sid)
                self.assertEqual(after["rounds"][:1], before["rounds"])
                self.assertEqual(after["reviews"], before["reviews"])
                self.assertEqual(after["reviewed_count"], 1)
                self.assertEqual(it.review_status(harness, sid, first["through"])["status"], "complete")
                self.assertEqual(it.capture(harness, sid, None, "capture-tests")["appended"], 0)


    def test_changed_prefix_at_another_path_does_not_replace_the_verified_source(self):
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        it.acknowledge("claude", self.sid, first["through"], "no_value", "Completed fixture only.")
        state_path = it.state_path("claude", self.sid)
        before = it._load(state_path, "claude", self.sid)
        changed = claude_round(self.sid, 1) + claude_round(self.sid, 2)
        changed[0]["uuid"] = "user-1-rewritten"
        candidate = self.path.with_suffix(".changed.jsonl")
        candidate.write_text("".join(json.dumps(r) + "\n" for r in changed), encoding="utf-8")
        refused = it.capture("claude", self.sid, str(candidate), "capture-tests")
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["capture_recovery"]["phase"], "replay")
        after = it._load(state_path, "claude", self.sid)
        self.assertEqual(after["transcript_path"], before["transcript_path"])
        for key in ("rounds", "reviews", "reviewed_count", "snapshots", "native_fingerprint", "coverage"):
            self.assertEqual(after[key], before[key], key)
        recovered = it.capture("claude", self.sid, None, "capture-tests")
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["appended"], 0)


    def test_missing_native_and_unavailable_raw_have_separate_recovery_states(self):
        self.transcript(claude_round(self.sid, 1))
        self.capture()
        state, record = legacy_cursor("claude", self.sid, self.path)
        through = it._snapshot(state)
        raw_before = record.read_bytes()
        self.path.unlink()
        missing = it.capture("claude", self.sid, None, "capture-tests")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["capture_recovery"]["state"], "action_required")
        self.assertEqual(missing["capture_recovery"]["phase"], "source")
        job = it.prompt("claude", self.sid, include_organization=False)
        self.assertEqual(job["raw_review"], {"state": "verified", "rounds": 1, "error": None})
        self.assertEqual(job["through"], through)
        self.assertEqual(record.read_bytes(), raw_before)
        record.write_bytes(raw_before.replace(b"question 1", b"altered question"))
        blocked = it.prompt("claude", self.sid, include_organization=False)
        self.assertEqual(blocked["raw_review"]["state"], "unavailable")
        self.assertTrue(blocked["raw_review"]["error"])
        with self.assertRaises((ValueError, write.WriteError)):
            it.acknowledge("claude", self.sid, through, "no_value", "Fixture only.")
        self.assertEqual(it.status("claude", self.sid)["reviewed_rounds"], 0)
        record.write_bytes(raw_before)
        # This ACK judges only the verified stored round; it cannot close capture.
        reviewed = it.acknowledge("claude", self.sid, through, "no_value", "Stored fixture has no reusable claim.")
        self.assertTrue(reviewed["capture_pending"])
        self.assertTrue(reviewed["capture_error"])
        self.assertTrue(reviewed["pending"])
        self.assertEqual(it.prompt("claude", self.sid, include_organization=False)["raw_review"]["state"], "none")


    def test_relocated_capture_io_failure_keeps_candidate_until_cursor_recovery(self):
        # Catch up only this fault matrix, not every unrelated suite fixture.
        state_path_for = it.state_path
        state_dir = Path(TMP.name) / self.sid
        patcher = mock.patch.object(it, "state_path", side_effect=lambda harness, sid:
                                    state_dir / state_path_for(harness, sid).name)
        patcher.start()
        self.addCleanup(patcher.stop)
        for retry in ("pathless", "catchup"):
            with self.subTest(retry=retry):
                sid = f"{self.sid}-{retry}"
                self.transcript(claude_round(sid, 1))
                first = it.capture("claude", sid, str(self.path), "capture-tests")
                it.acknowledge("claude", sid, first["through"], "no_value", "Completed fixture only.")
                state_path = it.state_path("claude", sid)
                before = it._load(state_path, "claude", sid)
                candidate = self.path.with_name(sid + ".relocated.jsonl")
                candidate.write_text("".join(json.dumps(r) + "\n" for r in
                                            claude_round(sid, 1) + claude_round(sid, 2)), encoding="utf-8")
                # A transient read failure, explicit or pathless, keeps the candidate source.
                for attempt in (str(candidate), None):
                    with mock.patch.object(transcripts, "read", side_effect=PermissionError("Native temporarily locked.")):
                        failed = it.capture("claude", sid, attempt, "capture-tests")
                    self.assertFalse(failed["ok"], failed)
                    self.assertTrue(failed["capture_pending"])
                    self.assertEqual((failed["captured_rounds"], failed["reviewed_rounds"]), (1, 1))
                    failed_state = it._load(state_path, "claude", sid)
                    self.assertEqual(failed_state["transcript_path"], before["transcript_path"])
                    self.assertEqual(failed_state.get("capture_path"), str(candidate.resolve()))
                if retry == "pathless":
                    recovered = it.capture("claude", sid, None, "capture-tests")
                else:
                    caught = it.catchup(100)
                    recovered = next(c for c in caught["captures"] if c["conversation_id"] == sid)
                self.assertTrue(recovered["ok"], recovered)
                self.assertEqual(recovered["appended"], 1)
                after = it._load(state_path, "claude", sid)
                self.assertEqual(after["transcript_path"], str(candidate.resolve()))
                self.assertNotIn("capture_path", after)
                self.assertEqual(after["rounds"][:1], before["rounds"])
                self.assertEqual(after["reviews"], before["reviews"])
                self.assertEqual(after["snapshots"][first["through"]], before["snapshots"][first["through"]])
                self.assertEqual((len(after["rounds"]), after["reviewed_count"]), (2, 1))
                job = it.prompt("claude", sid, include_organization=False)
                self.assertEqual(job["pending_refs"], [after["rounds"][1]["ref"]])
                self.assertEqual(job["raw_review"], {"state": "native", "rounds": 1, "error": None})


    def test_unbound_conversation_can_choose_a_stable_session_without_binding_generic_key(self):
        self.transcript(claude_round(self.sid, 1))
        generic = self.sid + '-unbound'
        failed = it.capture('claude', self.sid, str(self.path), generic)
        self.assertFalse(failed['ok'])
        self.assertEqual(failed["capture_recovery"]["phase"], "landing")
        self.assertTrue(failed["capture_recovery"]["next_action"])
        captured = it.capture('claude', self.sid, str(self.path), 'capture-tests', '00_Scope/Capture')
        self.assertTrue(captured['ok'], captured)
        self.assertEqual(captured['captured_rounds'], 1)
        self.assertIsNone(write.resolve_session(generic))
        resumed = it.hook_capture({'harness': 'claude', 'session_id': self.sid}, generic)
        self.assertTrue(resumed['ok'], resumed)
        self.assertEqual(resumed['session'], 'capture-tests')
        self.assertIsNone(write.resolve_session(generic))
        other = it.capture('claude', self.sid + '-other', str(self.path), generic)
        self.assertFalse(other['ok'])
        self.assertIsNone(it._load(it.state_path('claude', self.sid + '-other'),
                                  'claude', self.sid + '-other')['space'])
        self.assertFalse(it.capture('claude', self.sid, str(self.path), generic, '00_Scope/W1')['ok'])

    def test_hook_process_resume_keeps_pending_and_diagnostics_are_visible(self):
        self.transcript(claude_round(self.sid, 1))
        self.capture()
        for _ in range(9):
            it.tick("claude", self.sid)
        cwd = Path(TMP.name) / "capture-tests"
        cwd.mkdir(exist_ok=True)
        payload = {"session_id": self.sid, "harness": "claude", "transcript_path": str(self.path), "source": "resume", "cwd": str(cwd)}
        result = subprocess.run([sys.executable, str(ENGINE / "scripts/hooks/claude_session_start.py")],
                                input=json.dumps(payload).encode(), capture_output=True, env=os.environ, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.sid, out)
        self.assertEqual(it.status("claude", self.sid)["prompt_count"], 9)
        broken = subprocess.run([sys.executable, str(ENGINE / "scripts/hooks/claude_prompt_submit.py")],
                                input=b"{broken", capture_output=True, env=os.environ, timeout=30)
        self.assertEqual(broken.returncode, 0)
        self.assertIn("diagnostic", json.loads(broken.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_aborted_turn_is_separate_terminal_evidence(self):
        rows = [{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1, finished=False)
        rows += [{"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "turn-1", "reason": "interrupted"}}] + codex_round(2)
        self.transcript(rows)
        st = self.capture("codex")
        self.assertTrue(st["ok"], st)
        self.assertFalse(st["capture_pending"])
        self.assertEqual(st["captured_rounds"], 2)
        self.assertEqual(st["aborted_rounds"], 1)
        first, second = (t["user"] + "\n" + t["agent"] for t in self.turns("codex"))
        self.assertIn("turn_aborted", first)
        self.assertIn("question 1", first)
        self.assertNotIn("question 1", second)
        self.assertFalse(self.capture("codex")["capture_pending"])
        unknown = codex_round(3, finished=False)
        del unknown[2:4]
        self.transcript([{"type":"session_meta","payload":{"id":self.sid}}] + unknown
                        + [{"type":"event_msg","payload":{"type":"turn_aborted","turn_id":"turn-3"}}])
        parsed = transcripts.read(str(self.path), 'codex', self.sid)
        self.assertTrue(parsed['pending_tail'])
        self.assertIn('missing input for aborted', parsed['diagnostics'][0])
        # Session initialization may abort before any user/agent activity.
        self.transcript([{"type":"session_meta","payload":{"id":self.sid}}, unknown[0],
                         {"type":"response_item","payload":{"type":"message","role":"user",
                          "content":[{"type":"input_text","text":"environment metadata"}],
                          "internal_chat_message_metadata_passthrough":{
                              "content_item_kinds":["environments.environment_context"],"turn_id":"turn-3"}}},
                         {"type":"event_msg","payload":{"type":"turn_aborted","turn_id":"turn-3"}}])
        parsed = transcripts.read(str(self.path), 'codex', self.sid)
        self.assertEqual((parsed['rounds'],parsed['diagnostics'],parsed['pending_tail']), ([],[],False))


    def test_all_tool_payloads_are_references_and_dialogue_stays(self):
        rows = claude_round(self.sid, 1)
        rows[1]["message"]["content"][0].update(name="Read", input={"file_path": "C:/private/source.txt"})
        rows[2]["message"]["content"][0]["content"] = "FILE_FULL_CONTENT_MUST_NOT_COPY"
        more = claude_round(self.sid, 2)
        more[1]["message"]["content"][0].update(name="Bash", input={"command": "mixed command"})
        more[2]["message"]["content"][0]["content"] = "MIXED_OUTPUT_MUST_NOT_COPY"
        self.transcript(rows + more + claude_round(self.sid, 3))
        st = self.capture()
        self.assertTrue(st["ok"], st)
        text = "\n".join(t["user"] + "\n" + t["agent"] for t in self.turns())
        self.assertNotIn("FILE_FULL_CONTENT_MUST_NOT_COPY", text)
        self.assertNotIn("MIXED_OUTPUT_MUST_NOT_COPY", text)
        self.assertNotIn("C:/private/source.txt", text)
        self.assertIn("Read", text)
        self.assertIn("native_result", text)
        self.assertIn("sha256", text)
        self.assertNotIn("evidence result", text)
        self.assertIn("answer 3", text)
        self.assertEqual(st["coverage"]["mode"], "tool-output-reference")


    def test_codex_structured_read_reference_and_abort_without_reply(self):
        rows = [{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1)
        rows[5]["payload"].update(name="mcp__osk__read_node", arguments=json.dumps({"name": "source-node"}))
        rows[6]["payload"]["output"] = "NODE_FULL_CONTENT_MUST_NOT_COPY"
        rows += [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-2"}},
                 {"type": "event_msg", "payload": {"type": "user_message", "message": "cancel before reply"}},
                 {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "turn-2"}}]
        self.transcript(rows)
        st = self.capture("codex")
        self.assertEqual(st["captured_rounds"], 2, st)
        first, second = self.turns("codex")
        text = first["user"] + "\n" + first["agent"]
        self.assertNotIn("NODE_FULL_CONTENT_MUST_NOT_COPY", text)
        self.assertIn("mcp__osk__read_node", text)
        self.assertIn("tool_evidence_ref", text)
        self.assertIn("turn_aborted", second["agent"])


    def test_stop_before_flush_catchup_without_resume_and_changed_after_ack(self):
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1, finished=False))
        pending = self.capture("codex")
        self.assertTrue(pending["capture_pending"])
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1))
        caught = it.catchup(100)
        capture = next(c for c in caught["captures"] if c["conversation_id"] == self.sid)
        self.assertEqual((capture["ok"], capture["appended"]), (True, 1))
        self.assertNotIn(self.sid, [j["conversation_id"] for j in caught["jobs"]])
        # The conversation itself reviews its turns at its next prompt.
        job = it.prompt("codex", self.sid, include_organization=False)
        self.assertEqual(len(job["pending_refs"]), 1)
        self.assertIn(sys.executable, job["text"])
        it.acknowledge("codex", self.sid, job["through"], "no_value", "bounded test round")
        self.assertEqual(it.review_status("codex", self.sid, job["through"])["status"], "complete")
        self.assertFalse(it.status("codex", self.sid)["pending"])
        # The session can append and exit before Stop; the fingerprint only says to look again.
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1) + codex_round(2))
        it.catchup(100)
        job2 = it.prompt("codex", self.sid, include_organization=False)
        self.assertEqual(len(job2["pending_refs"]), 1)
        self.assertNotEqual(job["through"], job2["through"])

    def test_changed_original_turn_never_closes_its_old_snapshot(self):
        rows = claude_round(self.sid, 1) + claude_round(self.sid, 2)
        second = len(claude_round(self.sid, 1))
        self.transcript(rows)
        first = self.capture()
        ref = raw.native_ref("claude", self.sid, "user-2")
        # The same turn ID now carries other words.
        rows[second]["message"]["content"] = "question 2, for every project"
        self.transcript(rows)
        [seen] = it.read_turns([ref])["turns"]
        self.assertTrue(seen["changed"], seen)
        with self.assertRaisesRegex(ValueError, "changed after this snapshot"):
            it.acknowledge("claude", self.sid, first["through"], "no_value", "judged the old words")
        again = self.capture()
        self.assertEqual((again["ok"], again["appended"], again["changed"]), (True, 0, 1))
        self.assertNotEqual(again["through"], first["through"])
        [seen] = it.read_turns([ref])["turns"]
        self.assertNotIn("changed", seen)
        self.assertTrue(seen["hash"])
        with self.assertRaisesRegex(ValueError, "not an unreviewed snapshot"):
            it.acknowledge("claude", self.sid, first["through"], "no_value", "judged the old words")
        done = it.acknowledge("claude", self.sid, again["through"], "no_value", "judged the current words")
        self.assertEqual((done["reviewed_rounds"], done["pending"]), (2, False))
        # A reviewed turn keeps the version its review judged.
        rows[second]["message"]["content"] = "question 2, changed after its review"
        self.transcript(rows)
        later = self.capture()
        self.assertEqual((later["ok"], later["changed"], later["reviewed_rounds"], later["pending"]),
                         (True, 0, 2, False))
        # Without the transcript an original turn cannot be closed; a deferral closes nothing.
        self.transcript(rows + claude_round(self.sid, 3))
        third = self.capture()
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "not on this device"):
            it.acknowledge("claude", self.sid, third["through"], "no_value", "unread")
        it.acknowledge("claude", self.sid, third["through"], "deferred", "the transcript is away")

    def test_scripted_codex_exec_run_is_not_tracked_or_reviewed(self):
        path = it.state_path("codex", self.sid)
        old = it._load(path, "codex", self.sid)
        # An earlier engine left an open-tail error on this run.
        old.update(session="capture-tests", capture_pending=True, capture_error="ValueError: old open tail")
        it._save(path, old)
        meta = {"type": "session_meta", "payload": {"id": self.sid, "originator": "codex_exec", "source": "exec"}}
        self.transcript([meta] + codex_round(1) + codex_round(2))
        found = self.capture("codex")
        self.assertEqual((found["ok"], found["captured_rounds"], found["pending"], found["excluded"]),
                         (True, 0, False, "codex_exec"))
        self.assertIsNone(found["capture_error"])
        ended = time.time() - it.ENDED_AFTER - 60
        os.utime(self.path, (ended, ended))
        self.assertNotIn(self.sid, [j["conversation_id"] for j in it.list_pending(100)["jobs"]])
        # An interactive Codex conversation is still tracked.
        meta["payload"].update(originator="codex_cli_rs", source="cli")
        other = f"{self.sid}-interactive"
        meta["payload"]["id"] = other
        talk = Path(TMP.name) / f"{other}.jsonl"
        talk.write_text("".join(json.dumps(r) + "\n" for r in [meta] + codex_round(1)), encoding="utf-8")
        tracked = it.capture("codex", other, str(talk), "capture-tests")
        self.assertEqual((tracked["ok"], tracked["captured_rounds"], tracked["excluded"]), (True, 1, None))

    def test_recited_turn_reports_the_stored_record_and_what_it_sees_now(self):
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2, finished=False))
        self.capture()
        first = it.cite(f"claude/{self.sid}", turn="user-2")
        self.assertIsNone(first["agent_sha256"])  # the open turn has no reply yet
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        again = it.cite(f"claude/{self.sid}", turn="user-2", note="a later gist")
        self.assertEqual((again["reused"], again["round_ref"]), (True, first["round_ref"]))
        # The record is append-only: the answer reports what it holds, this read apart.
        self.assertIsNone(again["agent_sha256"])
        self.assertTrue(again["observed"]["agent_sha256"])
        self.assertFalse(again["note_saved"])
        _, header = raw.find_cited(ROOT / again["path"], raw.native_ref("claude", self.sid, "user-2"))
        self.assertIsNone(header["agent_sha256"])
        # Words the caller supplied while the transcript was away stay the caller's.
        away = self.path.with_suffix(".away")
        self.path.rename(away)
        kept = it.cite(f"claude/{self.sid}", user="question 1", turn="user-1")
        self.assertEqual(kept["user_by"], "caller")
        away.rename(self.path)
        back = it.cite(f"claude/{self.sid}", turn="user-1")
        self.assertEqual((back["reused"], back["user_by"]), (True, "caller"))
        self.assertEqual(back["observed"]["user_by"], "engine")
        self.assertNotIn("note_saved", back)


    def test_review_manifests_are_bounded_and_find_root_specific_states(self):
        self.transcript([r for n in range(1, 19) for r in claude_round(self.sid, n)])
        self.capture()
        job = it.prompt("claude", self.sid, include_organization=False)
        self.assertEqual(len(job["pending_refs"]), 15)
        self.assertEqual(job["remaining_rounds"], 3)
        ack = it.acknowledge("claude", self.sid, job["through"], "no_value", "first fifteen reviewed")
        self.assertEqual(ack["reviewed_rounds"], 15)
        self.assertEqual(len(ack["pending_refs"]), 3)
        # .git directories and nongit fallback retain the identical root prefix.
        expected = it.state_path("claude", "inventory").name.split("-")[2]
        self.assertEqual(it.state_path("claude", self.sid).name.split("-")[2], expected)
        (ROOT / ".git").mkdir()
        try:
            git_sid = self.sid + "-git"
            git_path = Path(TMP.name) / (git_sid + ".jsonl")
            git_path.write_text("".join(json.dumps(r) + "\n" for r in claude_round(git_sid, 1)), encoding="utf-8")
            it.capture("claude", git_sid, str(git_path), "capture-tests")
            git_path.write_text("".join(json.dumps(r) + "\n" for r in
                                        claude_round(git_sid, 1) + claude_round(git_sid, 2)), encoding="utf-8")
            states, _, _, _ = it._known_pending(100)
            self.assertIn(git_sid, [s["conversation_id"] for s in states])
        finally:
            import shutil
            shutil.rmtree(ROOT / ".git")


    def test_unbound_incomplete_capture_retains_explicit_landing_for_catchup(self):
        self.transcript(claude_round(self.sid, 1, finished=False))
        pending = it.capture("claude", self.sid, str(self.path), self.sid, "00_Scope/Capture")
        self.assertTrue(pending["capture_pending"])
        self.assertEqual(pending["capture_recovery"]["state"], "pending")
        self.assertEqual(it.prompt("claude", self.sid, include_organization=False)["raw_review"]["state"], "none")
        self.assertIsNone(write.resolve_session(self.sid))
        self.transcript(claude_round(self.sid, 1))
        done = it.catchup(100)
        capture = next(c for c in done["captures"] if c["conversation_id"] == self.sid)
        self.assertEqual((capture["ok"], capture["appended"]), (True, 1))
        # Tracking writes nothing, so it binds nothing; the first citation lands the key.
        self.assertIsNone(write.resolve_session(self.sid))
        cited = it.cite(f"claude/{self.sid}", quote="question 1")
        self.assertTrue(cited["round_ref"].startswith("00_Scope/Capture/_cited/"), cited)
        self.assertEqual(write.resolve_session(self.sid), "Capture")


    def test_claude_hook_detects_identity_after_leading_metadata(self):
        self.transcript([{"type": "file-history-snapshot", "snapshot": {}},
                         {"type": "queue-operation", "operation": "enqueue"}]
                        + claude_round(self.sid, 1))
        st = it.hook_capture({"session_id": self.sid, "transcript_path": str(self.path)}, "capture-tests")
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["harness"], "claude")
        self.assertEqual(st["captured_rounds"], 1)

    def test_identical_text_distinct_native_turns_are_not_replay(self):
        rows = []
        for n in (1, 2, 3):
            pair = [{"type": "user", "sessionId": self.sid, "uuid": f"user-{n}",
                     "message": {"role": "user", "content": "same question"}},
                    {"type": "assistant", "sessionId": self.sid, "uuid": f"assistant-{n}",
                     "message": {"role": "assistant", "id": f"message-{n}", "stop_reason": "end_turn",
                                 "content": [{"type": "text", "text": "same answer"}]}}]
            rows += pair
            self.transcript(rows)
            st = self.capture()
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["appended"], 1)
            self.assertEqual(st["captured_rounds"], n)
        self.transcript(rows + pair)
        st = self.capture()
        self.assertEqual(st["appended"], 0)
        self.assertEqual(st["captured_rounds"], 3)
        parsed = transcripts.read(str(self.path), "claude", self.sid)
        raw.append_rounds("capture-tests", self.sid + "-manual", [parsed["rounds"][-1]])
        with self.assertRaises(write.WriteError):
            raw.append_rounds("capture-tests", self.sid + "-manual", [parsed["rounds"][-1]])

    def test_storage_boundary_retains_long_dialogue_not_native_bulk(self):
        long_text = "USER START " + "visible statement " * 2000 + "MIDPOINT CORRECTION " + "more words " * 2000 + " USER END"
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                sid = self.sid + harness
                path = Path(TMP.name) / (sid + ".jsonl")
                if harness == "claude":
                    rows = claude_round(sid, 1)
                    rows[0]["message"]["content"] = long_text
                else:
                    rows = [{"type":"session_meta", "payload":{"id":sid}}] + codex_round(1)
                    rows[4]["payload"]["message"] = long_text
                save = lambda: path.write_text("".join(json.dumps(r)+"\n" for r in rows), encoding="utf-8")
                tail = claude_round(sid, 2) if harness == "claude" else codex_round(2)
                rows += tail
                save()
                clean = transcripts.read(str(path), harness, sid)["dialogue_v1"]
                if harness == "claude":
                    tail[-1]["message"]["content"].insert(0, {"type":"thinking", "thinking":"OPAQUE_PAYLOAD" * 100000})
                    tail[-1]["message"]["transport_metadata"] = "TRANSPORT_BULK" * 100000
                    tail[1]["message"]["content"][0]["input"] = {"code":"TOOL_CODE_BULK" * 100000}
                else:
                    tail[-3]["payload"]["internal_chat_message_metadata_passthrough"] = {"noise":"TRANSPORT_BULK" * 100000}
                    rows.insert(-1, {"type":"response_item", "payload":{"type":"reasoning", "encrypted_content":"OPAQUE_PAYLOAD" * 100000}})
                    tail[4]["payload"]["arguments"] = "TOOL_CODE_BULK" * 100000
                save()
                noisy = transcripts.read(str(path), harness, sid)["dialogue_v1"]
                self.assertEqual([r["user"] for r in clean.values()], [r["user"] for r in noisy.values()])
                self.assertEqual(sum(len(r["agent"]) for r in clean.values()), sum(len(r["agent"]) for r in noisy.values()))
                self.assertNotEqual(list(clean.values())[-1]["agent"], list(noisy.values())[-1]["agent"],
                                    "changed tool payload must change its evidence hash")
                self.assertIn(long_text, noisy[next(iter(noisy))]["user"])
                st = it.capture(harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertEqual(st["captured_rounds"], 2)
                record = ROOT / it.cite(f"{harness}/{sid}", quote="MIDPOINT CORRECTION")["path"]
                it.cite(f"{harness}/{sid}", turn="-1")
                stored = record.read_bytes()
                for kept in (b"USER START", b"MIDPOINT CORRECTION", b"USER END", b"question 2"):
                    self.assertIn(kept, stored)
                for noise in (b"OPAQUE_PAYLOAD", b"TRANSPORT_BULK", b"TOOL_CODE_BULK", b"answer 2"):
                    self.assertNotIn(noise, stored)


    def test_actual_preserved_scope_node_ack_and_later_receipt_validation(self):
        from osk import contract, distillation as D
        self.transcript(claude_round(self.sid, 1))
        st = it.capture("claude", self.sid, str(self.path), self.sid, space="00_Scope/W1")
        ref = it.cite(f"claude/{self.sid}", quote="question 1")["round_ref"]
        spec = {"key": self.sid, "sources": [ref], "hub": "W1"}
        args = {"title": "A retained integration decision", "summary": "bounded retry",
                "body": "A retry is identified by native identity rather than repeated wording.",
                "drafter": "fable-5", "space": "00_Scope/W1"}
        created = D.create_node(spec, **args)
        self.assertEqual(created["distillation"]["status"], "complete")
        node = contract.parse(core.ROOT / created["path"])
        hub = contract.parse(core.ROOT / "00_Scope/W1/W1.md")
        self.assertIn(ref, write._stored_edges(node.meta["derived-from"]))
        self.assertIn(created["name"], hub.wikilinks())
        # The cited round binds the review of the original turn it cites.
        ack = it.acknowledge("claude", self.sid, st["through"], "preserved",
                             "The cited observation is retained in the existing project cluster.",
                             [{"key": spec["key"]}])
        self.assertFalse(ack["pending"])
        self.assertEqual(ack["reviewed_rounds"], 1)
        self.assertTrue(ack["last_review"]["mechanical_preservation"])
        self.assertFalse(ack["last_review"]["semantic_verified"])
        self.assertEqual(it.review_status("claude", self.sid, st["through"])["status"], "complete")
        sibling = D.create_node(dict(spec, key=self.sid + "-sibling"),
                                **dict(args, title="A sibling retained decision", body="Another reusable observation."))
        self.assertEqual(sibling["distillation"]["status"], "complete")
        self.assertEqual(it.review_status("claude", self.sid, st["through"])["status"], "complete")
        # After ACK only structure is observed: a body edit keeps the review closed.
        changed = write.update_node(created["id"], body="Changed after review.", expect_hash=created["new_hash"])
        after = it.review_status("claude", self.sid, st["through"])
        self.assertEqual(after["status"], "complete")
        self.assertNotIn("structure", after)
        # Dropping the source is reported, still without reopening the conversation's review.
        write.update_node(created["id"], remove_edges={"derived-from": ref}, expect_hash=changed["new_hash"])
        broken = it.review_status("claude", self.sid, st["through"])
        self.assertEqual(broken["status"], "complete")
        self.assertEqual(broken["structure"], [{"key": spec["key"], "reason": "target no longer cites its source"}])
        self.assertFalse(it.status("claude", self.sid)["pending"])


    def test_existing_saved_record_name_is_preserved(self):
        self.transcript(claude_round(self.sid, 1))
        path = it.state_path("claude", self.sid)
        state = it._load(path, "claude", self.sid)
        state["record"] = "legacy-record-name"
        it._save(path, state)
        st = self.capture()
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["record"], "legacy-record-name")
        cited = it.cite(f"claude/{self.sid}", turn="-1")
        self.assertIn("/.records/legacy-record-name.txt#1", cited["round_ref"])


    def test_raw_migration_preserves_pending_snapshot_and_legacy_receipt_match(self):
        from osk import distillation as D
        self.transcript(claude_round(self.sid, 1))
        it.capture("claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        state, physical = legacy_cursor("claude", self.sid, self.path, "W1")
        legacy = physical.parent.parent / (physical.stem + ".md")
        physical.rename(legacy)  # Simulate the pre-upgrade visible record and cursor.
        saved = legacy.read_bytes()
        old_ref = f"[[{legacy.relative_to(ROOT).as_posix()}#1]]"
        state_path = it.state_path("claude", self.sid)
        state["rounds"][0]["ref"] = old_ref
        token = it._snapshot(state)
        state["snapshots"] = {token: {"count": 1, "prompt_count": state["prompt_count"]}}
        it._save(state_path, state)
        replayed = it.capture("claude", self.sid, str(self.path), self.sid)
        self.assertTrue(replayed["ok"], replayed)
        self.assertEqual(replayed["through"], token)
        self.assertEqual(replayed["pending_refs"], [old_ref])
        self.assertEqual(replayed["appended"], 0)
        self.assertEqual(legacy.read_bytes(), saved)  # tracking never rewrites a record
        moved = raw.migrate(apply=True)
        self.assertIn(legacy.relative_to(ROOT).as_posix(), [f["from"] for f in moved["files"]])
        self.assertFalse(legacy.exists())
        self.assertEqual(physical.read_bytes(), saved)
        out = D.create_node({"key": self.sid, "sources": [old_ref], "hub": "W1"},
                            title=self.sid, summary="retained observation",
                            body="Observation retained with its original source.",
                            drafter="test-model", space="00_Scope/W1")
        self.assertEqual(out["distillation"]["status"], "complete")
        ack = it.acknowledge("claude", self.sid, token, "preserved", "fixture retained",
                             [{"key": self.sid}])
        self.assertTrue(ack["ok"], ack)


    def test_review_key_tracks_snapshot_and_recovers_saved_proof(self):
        from osk import distillation as D
        self.transcript(claude_round(self.sid, 1))
        it.capture("claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        first = it.prompt("claude", self.sid)
        self.assertEqual(first["key"], it.prompt("claude", self.sid)["key"])
        cited = it.cite(f"claude/{self.sid}", turn="-1")["round_ref"]
        spec = {"key": first["key"] + ":stable-target", "sources": [cited], "hub": "W1"}
        saved = D.create_node(spec, title="Snapshot-specific retained observation", summary="native observation",
                              body="The observation is preserved before its review acknowledgement arrives.",
                              drafter="fable-5", space="00_Scope/W1")
        self.assertEqual(saved["distillation"]["status"], "complete")
        retry = it.prompt("claude", self.sid)
        self.assertEqual(first["key"], retry["key"])
        proof = next(p for p in retry["previous_distillations"] if p["key"] == spec["key"])
        self.assertEqual(proof["status"], "complete")
        self.assertIn(spec["key"], retry["text"])
        self.assertIn("대상별 고정 접미사", retry["text"])
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        it.capture("claude", self.sid, str(self.path), self.sid)
        later = it.prompt("claude", self.sid)
        self.assertNotEqual(later["through"], first["through"])
        self.assertNotEqual(later["key"], first["key"])
        self.assertEqual(later["key"], it.prompt("claude", self.sid)["key"])
        self.assertTrue(any(p["key"] == spec["key"] for p in later["previous_distillations"]))


    def test_worker_batch_does_not_reduce_ordinary_cadence_or_ack_the_tail(self):
        self.transcript([row for n in range(20) for row in claude_round(self.sid, n)])
        original = self.capture()
        worker = it.prompt("claude", self.sid, include_organization=False, max_rounds=3)
        ordinary = it.prompt("claude", self.sid, include_organization=False)
        self.assertEqual(len(worker["pending_refs"]), 3)
        self.assertEqual(len(ordinary["pending_refs"]), 15)
        self.assertNotEqual(worker["through"], ordinary["through"])
        self.assertEqual(it.status("claude", self.sid)["pending_refs"], original["pending_refs"])
        it.acknowledge("claude", self.sid, worker["through"], "no_value", "Only fixture questions in selected first three rounds.")
        pending = it.status("claude", self.sid)["pending_refs"]
        self.assertEqual(pending, original["pending_refs"][3:])
        self.assertEqual(it.prompt("claude", self.sid, max_rounds=3)["pending_refs"], pending[:3])

    def _discovery_source(self):
        self.transcript(claude_round(self.sid, 1))
        st = it.capture("claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        return {**st, "cited": it.cite(f"claude/{self.sid}", turn="-1")["round_ref"]}


    def _discovery_save(self, st, key, title=None):
        from osk import distillation as D
        self.addCleanup(D._job_path(key).unlink, missing_ok=True)
        return D.create_node({"key": key, "sources": [st["cited"]], "hub": "W1"},
                             title=title or self.sid + "-node", summary="lost ACK recovery",
                             body="Keep the already retained decision and finish its missing acknowledgement.",
                             drafter="fable-5", space="00_Scope/W1")


    def test_saved_scope_without_ack_is_discovered_and_reused(self):
        from osk import graph
        st = self._discovery_source()
        key = self.sid + "-opaque-worker-key"
        saved = self._discovery_save(st, key)
        target = core.ROOT / saved["path"]
        before = target.read_bytes()
        own = it.prompt("claude", self.sid)
        proofs = own["previous_distillations"]
        self.assertEqual([proof["key"] for proof in proofs], [key])
        self.assertEqual(proofs[0]["status"], "complete")
        nodes_before = set(graph.Index().nodes)
        ack = it.acknowledge("claude", self.sid, own["through"], "preserved",
                             "Reuse the verified saved decision; the previous ACK was lost.",
                             [{"key": proofs[0]["key"]}])
        self.assertFalse(ack["pending"])
        self.assertEqual(it.review_status("claude", self.sid, own["through"])["status"], "complete")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(set(graph.Index().nodes), nodes_before)


    def test_discovery_excludes_other_conversation_and_root(self):
        import hashlib
        from osk import distillation as D
        own = self._discovery_source()
        key = self.sid + "-own"
        self._discovery_save(own, key)
        other_sid = self.sid + "-other"
        other_path = Path(TMP.name) / (other_sid + ".jsonl")
        other_path.write_text("".join(json.dumps(row) + "\n" for row in claude_round(other_sid, 1)), encoding="utf-8")
        other = it.capture("claude", other_sid, str(other_path), other_sid, "00_Scope/W1")
        other["cited"] = it.cite(f"claude/{other_sid}", turn="-1")["round_ref"]
        other_key = self.sid + "-unrelated"
        self._discovery_save(other, other_key, self.sid + "-other-node")
        # Model a journal in the same common git directory with a different ROOT.
        # Its valid body/source bytes would pass if canonical filename binding
        # were omitted. The key is never exposed to this conversation.
        foreign_key = self.sid + "-foreign-root"
        foreign = dict(D._load(key), key=foreign_key)
        digest = hashlib.sha256((str(ROOT / "other-root") + "\0" + foreign_key).encode()).hexdigest()
        own_path = D._job_path(foreign_key)
        own_digest = hashlib.sha256((str(core.ROOT) + "\0" + foreign_key).encode()).hexdigest()
        foreign_path = own_path.with_name(own_path.name.replace(own_digest, digest))
        self.assertNotEqual(foreign_path, D._job_path(foreign_key))
        self.addCleanup(foreign_path.unlink, missing_ok=True)
        write._atomic_write(foreign_path, json.dumps(foreign).encode())
        found = D.discover(own["pending_refs"])
        self.assertEqual([proof["key"] for proof in found["proofs"]], [key])
        self.assertFalse(found["errors"], found)
        self.assertNotIn(other_key, json.dumps(it.prompt("claude", self.sid)))
        self.assertNotIn(foreign_key, json.dumps(it.prompt("claude", self.sid)))


    def test_discovered_pending_hub_resumes_without_body_rewrite(self):
        from osk import distillation as D
        st = self._discovery_source()
        key = self.sid + "-interrupted"
        atomic = write._atomic_write
        hub = ROOT / "00_Scope/W1/W1.md"
        def fail_hub(path, data):
            if path == hub:
                raise OSError("injected lost hub write")
            return atomic(path, data)
        with mock.patch.object(write, "_atomic_write", side_effect=fail_hub):
            saved = self._discovery_save(st, key)
        self.assertTrue(saved["ok"])
        before = (ROOT / saved["path"]).read_bytes()
        prompt = it.prompt("claude", self.sid)
        proof = next(proof for proof in prompt["previous_distillations"] if proof["key"] == key)
        self.assertEqual(proof["status"], "pending")
        self.assertIn("distill={resume:proof.key}", prompt["text"])
        repaired = D.update_node({"resume": proof["key"]}, name=proof["target"]["id"])
        self.assertEqual(repaired["distillation"]["status"], "complete")
        ack = it.acknowledge("claude", self.sid, prompt["through"], "preserved",
                             "Recovered the saved target and its hub link.", [{"key": proof["key"]}])
        self.assertFalse(ack["pending"])
        self.assertEqual((ROOT / saved["path"]).read_bytes(), before)

    def test_discovery_errors_are_visible_and_stale_proof_is_not_reused(self):
        from osk import distillation as D
        st = self._discovery_source()
        key = self.sid + "-saved"
        saved = self._discovery_save(st, key)
        write.update_node(saved["id"], body="A later writer changed this decision.",
                          expect_hash=saved["new_hash"])
        damaged = D._job_path(self.sid + "-damaged")
        self.addCleanup(damaged.unlink, missing_ok=True)
        write._atomic_write(damaged, b"{bad-json")
        prompt = it.prompt("claude", self.sid)
        self.assertEqual(prompt["previous_distillations"], [])
        self.assertTrue(prompt["proof_discovery"]["errors"])
        self.assertIn("조회가 불완전", prompt["text"])
        self.assertTrue(prompt["pending"])


    def test_secret_dictionary_key_collision_does_not_drop_evidence(self):
        collision = {"ghp_" + "a" * 36: "first", "ghp_" + "b" * 36: "second"}
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                sid = self.sid + "-" + harness
                path = Path(TMP.name) / (sid + ".jsonl")
                if harness == "claude":
                    rows = claude_round(sid, 1)
                    rows[0]["message"]["content"] = [{"type":"text", "text":json.dumps(collision)}]
                    rows[1]["message"]["content"][0]["input"] = collision
                else:
                    rows = [{"type": "session_meta", "payload": {"id": sid}}] + codex_round(1)
                    rows[4]["payload"]["message"] = json.dumps(collision)
                    rows[5]["payload"]["arguments"] = json.dumps(collision)
                native = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
                path.write_bytes(native)
                st = it.capture(harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertFalse(st["capture_pending"])
                self.assertEqual(st["captured_rounds"], 1)
                cited = it.cite(f"{harness}/{sid}", turn="-1")
                saved = (ROOT / cited["path"]).read_bytes()
                self.assertEqual(saved.count(b"[FILTERED:github-token]"), 2)
                self.assertIn(b"first", saved)
                self.assertIn(b"second", saved)
                self.assertLess(saved.index(b"first"), saved.index(b"second"))
                for token in collision:
                    self.assertNotIn(token.encode(), saved)
                self.assertEqual(path.read_bytes(), native)


    def test_secret_in_duplicate_encoded_json_key_does_not_leak(self):
        tokens = ["ghp_" + letter * 36 for letter in "de"]
        encoded = (' { "same" : ' + json.dumps("before\n" + tokens[0])
                   + ', "same" : "' + tokens[1]
                   + '", "same" : "benign", "escaped" : "\\u0061" } ')
        expected = encoded
        for token in tokens:
            expected = expected.replace(token, "[FILTERED:github-token]")
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                sid = self.sid + "-" + harness
                path = Path(TMP.name) / (sid + ".jsonl")
                if harness == "claude":
                    rows = claude_round(sid, 1)
                    rows[0]["message"]["content"] = [{"type":"text", "text":encoded}]
                    rows[1]["message"]["content"][0]["input"] = {"encoded": encoded}
                else:
                    rows = [{"type": "session_meta", "payload": {"id": sid}}] + codex_round(1)
                    rows[4]["payload"]["message"] = encoded
                    rows[5]["payload"]["arguments"] = encoded
                native = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
                path.write_bytes(native)
                st = it.capture(harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertFalse(st["capture_pending"])
                self.assertEqual(st["captured_rounds"], 1)
                cited = it.cite(f"{harness}/{sid}", turn="-1")
                saved = (ROOT / cited["path"]).read_bytes()
                # Exact encoded content retains all duplicate pairs and whitespace.
                self.assertIn(json.dumps(expected).encode(), saved)
                self.assertEqual(saved.count(b"[FILTERED:github-token]"), 2)
                for token in tokens:
                    self.assertNotIn(token.encode(), saved)
                self.assertEqual(path.read_bytes(), native)


    def test_duplicate_json_dialogue_preserves_content_and_later_rounds(self):
        dialogue = ' { "status" : "old", "status" : "new", "escaped" : "\\u0061" } '
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                sid = self.sid + "-" + harness
                path = Path(TMP.name) / (sid + ".jsonl")
                if harness == "claude":
                    rows = claude_round(sid, 1)
                    rows[0]["message"]["content"] = dialogue
                    later = claude_round(sid, 2)
                else:
                    rows = [{"type": "session_meta", "payload": {"id": sid}}] + codex_round(1)
                    rows[4]["payload"]["message"] = dialogue
                    later = codex_round(2)
                native = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
                path.write_bytes(native)
                first = it.capture(harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(first["ok"], first)
                self.assertFalse(first["capture_pending"])
                self.assertEqual(first["appended"], 1)
                cited = it.cite(f"{harness}/{sid}", turn="-1")
                saved_first = (ROOT / cited["path"]).read_bytes()
                self.assertEqual(transcripts._dump(dialogue), dialogue)
                # raw._block already strips trailing whitespace from whole sections.
                expected = dialogue.rstrip() if harness == "claude" else json.dumps(dialogue)
                self.assertIn(expected.encode(), saved_first)
                self.assertEqual(path.read_bytes(), native)
                native += "".join(json.dumps(row) + "\n" for row in later).encode("utf-8")
                path.write_bytes(native)
                second = it.capture(harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(second["ok"], second)
                self.assertFalse(second["capture_pending"])
                self.assertEqual((second["captured_rounds"], second["appended"]), (2, 1))
                self.assertEqual(it.cite(f"{harness}/{sid}", turn="-1")["index"], 2)
                saved = (ROOT / cited["path"]).read_bytes()
                self.assertTrue(saved.startswith(saved_first))
                self.assertIn(b"question 2", saved)
                self.assertNotIn(b"answer 2", saved)
                self.assertEqual(path.read_bytes(), native)


    def test_json_literal_redaction_preserves_escapes_and_nested_secrets(self):
        token = "ghp_" + "f" * 36
        encoded = ('{"same":"\\ud800\\n\\u0067' + token[1:]
                   + '","same":"benign"}')
        nested = json.dumps({"encoded": encoded})
        filtered = transcripts._dump(nested)
        filtered.encode("utf-8")  # changed literals must not emit lone surrogates
        retained = json.loads(filtered)["encoded"]
        pairs = json.loads(retained, object_pairs_hook=list)
        self.assertEqual(pairs, [("same", "\ud800\n[FILTERED:github-token]"),
                                 ("same", "benign")])
        self.assertNotIn(token[1:], filtered)
        clean = " \t" + json.dumps(
            {"quoted": '"backslash\\ /"', "unicode": "\ud800 한글 😀"},
            ensure_ascii=True, indent=2).replace("/", "\\/") + "\n"
        self.assertEqual(transcripts._dump(clean), clean)

    def test_secret_filter_preserves_native_reference_hash_and_clean_json(self):
        import hashlib
        token = "ghp_" + "h" * 36
        original = {"text": "result\n" + token}
        reference = transcripts._result_content(
            {"name": "read_file", "input": {"path": "safe.txt"}}, original, "native:reference")
        rendered = json.loads(transcripts._dump(reference))
        expected = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(rendered["sha256"], expected)
        self.assertNotIn(token, json.dumps(rendered))
        unchanged = ' { "items" : [ "normal text", "ghp_short" ], "n": 3 } '
        self.assertEqual(transcripts._dump(unchanged), unchanged)

    def turns(self, harness="claude"):
        """The parsed dialogue — tracked by hash, copied into the vault only by `cite`."""
        return list(transcripts.read(str(self.path), harness, self.sid)["dialogue_v1"].values())

    def test_codex_paginated_history_keeps_receipts_and_byte_boundary(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME': home}):
            folder = Path(home) / 'sessions/2026/09/21'
            folder.mkdir(parents=True)
            parent = folder / f'rollout-first-{self.sid}.jsonl'
            child = folder / f'rollout-second-{self.sid}_continuation.jsonl'
            header = {'type': 'session_meta', 'payload': {'id': self.sid}}
            parent.write_text(''.join(json.dumps(r) + '\n' for r in [header] + codex_round(1)), encoding='utf-8')
            first = it.capture('codex', self.sid, str(parent), 'capture-tests')
            it.acknowledge('codex', self.sid, first['through'], 'no_value', 'synthetic fixture')
            before = it._load(it.state_path('codex', self.sid), 'codex', self.sid)
            offset = parent.stat().st_size
            # The declared history excludes later events in the ancestor file.
            with parent.open('a', encoding='utf-8') as f:
                f.write(''.join(json.dumps(r) + '\n' for r in codex_round(99)))
            child_header = {'type': 'session_meta', 'payload': {'id': self.sid,
                'history_base': {'thread_id': self.sid, 'end_byte_offset': offset}}}
            child.write_text(''.join(json.dumps(r) + '\n' for r in [child_header] + codex_round(2)), encoding='utf-8')
            child_name = '\\\\?\\' + str(child.resolve()) if os.name == 'nt' else str(child)
            captured = it.capture('codex', self.sid, child_name, 'capture-tests')
            self.assertTrue(captured['ok'], captured)
            self.assertEqual((captured['appended'], captured['captured_rounds'], captured['reviewed_rounds']), (1, 2, 1))
            after = it._load(it.state_path('codex', self.sid), 'codex', self.sid)
            self.assertEqual(after['rounds'][:1], before['rounds'])
            self.assertEqual(after['reviews'], before['reviews'])
            self.assertEqual(after['snapshots'][first['through']], before['snapshots'][first['through']])
            self.assertEqual([r['id'] for r in after['rounds']], ['turn-1', 'turn-2'])
            self.assertNotIn('question 99', json.dumps(transcripts.read(str(child), 'codex', self.sid)['dialogue_v1']))
            self.assertFalse(raw.record_path('Capture', captured['record']).exists())
            self.assertEqual(it.capture('codex', self.sid, str(child), 'capture-tests')['appended'], 0)
            # A lost local cursor tracks the same original turns again, under the same refs.
            refs = [r['ref'] for r in after['rounds']]
            it.state_path('codex', self.sid).unlink()
            rebuilt = it.capture('codex', self.sid, str(child), 'capture-tests')
            self.assertEqual((rebuilt['appended'], rebuilt['pending_refs']), (2, refs))

    def test_codex_missing_ancestor_keeps_tracked_turns_pending(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME':home}):
            parent = Path(home) / f'rollout-parent-{self.sid}.jsonl'
            child = Path(home) / f'rollout-child-{self.sid}_next.jsonl'
            header = {'type':'session_meta','payload':{'id':self.sid}}
            parent.write_text(''.join(json.dumps(r)+'\n' for r in [header] + codex_round(1)), encoding='utf-8')
            header['payload']['history_base'] = {'thread_id':self.sid,'end_byte_offset':parent.stat().st_size}
            child.write_text(''.join(json.dumps(r)+'\n' for r in [header] + codex_round(2)), encoding='utf-8')
            first = it.capture('codex', self.sid, str(child), 'capture-tests')
            self.assertTrue(first['ok'], first)
            parent.unlink()
            listed = it.list_pending(100)
            self.assertFalse(listed['ok'])
            self.assertTrue(any('history_base' in e['error'] for e in listed['errors']))
            # An offline worker has no copy of these turns; they wait for their own conversation.
            self.assertNotIn(self.sid, [j['conversation_id'] for j in listed['jobs']])
            caught = it.catchup(100)
            capture = next(c for c in caught['captures'] if c['conversation_id'] == self.sid)
            self.assertFalse(capture['ok'])
            self.assertIn('history_base', capture['capture_error'])
            self.assertNotIn(self.sid, [j['conversation_id'] for j in caught['jobs']])
            st = it.status('codex', self.sid)
            self.assertEqual((st['pending_refs'], st['reviewed_rounds']), (first['pending_refs'], 0))

    def test_codex_recovered_past_turn_follows_tracked_turns(self):
        native = codex_round(2)
        native[3] = codex_user_item(self.sid, 2, 'question 2')
        self.transcript([{'type':'session_meta','payload':{'id':self.sid}}]
                        + codex_round(1) + native + codex_round(3))
        # An older parser skipped turn-2; the cursor tracked the others in its order.
        older = transcripts.read(str(self.path), 'codex', self.sid)
        older['rounds'] = [r for r in older['rounds'] if r['id'] != 'turn-2']
        with mock.patch.object(transcripts, 'read', return_value=older):
            first = self.capture('codex')
        self.assertEqual(first['captured_rounds'], 2)
        it.acknowledge('codex', self.sid, first['through'], 'no_value', 'fixture turns only')
        old = it._load(it.state_path('codex', self.sid), 'codex', self.sid)
        captured = self.capture('codex')
        self.assertTrue(captured['ok'], captured)
        self.assertEqual((captured['appended'], captured['reviewed_rounds']), (1, 2))
        state = it._load(it.state_path('codex', self.sid), 'codex', self.sid)
        self.assertEqual(state['rounds'][:2], old['rounds'])
        self.assertEqual(state['reviews'], old['reviews'])
        self.assertEqual([r['id'] for r in state['rounds']], ['turn-1', 'turn-3', 'turn-2'])
        self.assertEqual(captured['pending_refs'], [raw.native_ref('codex', self.sid, 'turn-2')])
        # Without the local cursor, the turns are tracked again in transcript order.
        it.state_path('codex', self.sid).unlink()
        rebuilt = self.capture('codex')
        self.assertEqual((rebuilt['ok'], rebuilt['appended']), (True, 3))

    def test_cited_record_index_previews_skip_only_recorder_headers(self):
        rows = [{"type": "session_meta", "payload": {"id": self.sid}}]
        for n in (1, 2):
            turn = codex_round(n)
            turn[3] = codex_user_item(self.sid, n, f"question {n}")
            rows += turn
        self.transcript(rows)
        self.assertTrue(self.capture("codex")["ok"])
        refs = [it.cite(f"codex/{self.sid}", quote=f"question {n}")["round_ref"] for n in (1, 2)]
        name = raw.parse_ref(refs[0])[0]
        path = raw._raw_file(name)
        before = path.read_bytes()
        toc = raw.read_round(name)["index"]
        self.assertEqual(len(toc), 2)
        for n, item in enumerate(toc, 1):
            self.assertIn(f"question {n}", item["preview"])
            self.assertNotIn("osk-cited", item["preview"])
            recalled = raw.read_round(f"{name}#{n}")
            self.assertEqual(raw.cited_header(recalled["text"])["turn"], f"turn-{n}")
            self.assertEqual(item["chars"], recalled["chars"])
        self.assertEqual(path.read_bytes(), before)
        # A user can quote a recorder header. Skip only the header, not matching user
        # content or all HTML comments, in the cited and the older stored formats.
        for stamp in ("", raw._CODEX_V2 + "\n\n", raw._DIALOGUE_V1 + '"turn-1" -->\n\n',
                      raw._CITED + '{"conversation": "c", "harness": "codex", "turn": "t"} -->\n\n'):
            block = f"## 1\n\n{stamp}### user\n\n{raw._CODEX_V2}\n\n### agent\n\nreply\n"
            self.assertEqual(raw._preview(block), raw._CODEX_V2)
            crlf = f"## 1\n\n{stamp}### user\n\nCRLF question\n\n### agent\n\nreply\n".replace("\n", "\r\n")
            self.assertEqual(raw._preview(crlf), "CRLF question")

    def test_crash_during_capture_retries_without_duplicate(self):
        self.transcript(claude_round(self.sid, 1))
        with mock.patch.object(transcripts, "read", side_effect=SystemExit("simulated power loss mid-capture")):
            with self.assertRaises(SystemExit):
                self.capture()
        self.assertTrue(it.status("claude", self.sid)["pending"])
        st = self.capture()
        self.assertEqual((st["appended"], st["captured_rounds"]), (1, 1))
        self.assertEqual(self.capture()["appended"], 0)
        it.state_path("claude", self.sid).unlink()
        again = self.capture()
        self.assertEqual((again["appended"], again["pending_refs"]), (1, st["pending_refs"]))

    def test_claude_copied_prefix_is_not_reviewed_again_by_the_child(self):
        parent = self.sid + "-parent"
        rows = claude_round(parent, 1)
        rows[1]["message"]["content"][0]["name"] = "Bash"
        previous = None
        for row in rows:
            row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, parent + row["uuid"]))
            row["parentUuid"], previous = previous, row["uuid"]
        self.transcript(rows)
        original = it.capture("claude", parent, str(self.path), "capture-tests")
        self.assertEqual(original["captured_rounds"], 1)
        # The native desktop may preserve parent IDs or rewrite all IDs to the child.
        for rewrite in (False, True):
            child = self.sid + ("-rewrite" if rewrite else "-mixed")
            inherited = [dict(r, sessionId=child) if rewrite else r for r in rows]
            tail = claude_round(child, 3 if rewrite else 2)
            for row in tail:
                row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, child + row["uuid"]))
            tail[0]["parentUuid"] = rows[-1]["uuid"]
            self.transcript(inherited + tail)
            captured = it.capture("claude", child, str(self.path), "capture-tests")
            self.assertTrue(captured["ok"], captured)
            self.assertEqual((captured["captured_rounds"], captured["inherited_rounds"]), (1, 1))
            self.assertEqual(captured["reviewed_rounds"], 0)
            self.assertNotIn(original["pending_refs"][0], captured["pending_refs"])
            self.assertIn("과거 1라운드", it.prompt("claude", child)["text"])
            self.assertEqual(it.capture("claude", child, str(self.path), "capture-tests")["appended"], 0)
            it.state_path("claude", child).unlink()
            rebuilt = it.capture("claude", child, str(self.path), "capture-tests")
            self.assertTrue(rebuilt["ok"], rebuilt)
            self.assertEqual((rebuilt["appended"], rebuilt["inherited_rounds"]), (1, 1))
            changed = json.loads(json.dumps(inherited + tail))
            changed[0]["message"]["content"] = "changed copied question"
            self.transcript(changed)
            refused = it.capture("claude", child, str(self.path), "capture-tests")
            self.assertFalse(refused["ok"])
            self.assertIn("inherited native prefix changed", refused["capture_error"])
        self.assertEqual(it.status("claude", parent)["reviewed_rounds"], 0)
        # Without both local cursors the copy is the child's own history again.
        it.state_path("claude", parent).unlink()
        it.state_path("claude", child).unlink()
        self.transcript(inherited + tail)
        rebuilt = it.capture("claude", child, str(self.path), "capture-tests")
        self.assertEqual((rebuilt["appended"], rebuilt["inherited_rounds"]), (2, 0), rebuilt)

    def test_claude_fork_copy_matches_turns_whichever_session_owns_rows(self):
        grandparent, parent = self.sid + "-G", self.sid + "-P"
        rows, previous = [], None
        for n, sid, tool in ((1, grandparent, "Bash"), (2, parent, "Read")):
            part = claude_round(sid, n)
            part[1]["message"]["content"][0]["name"] = tool
            for row in part:
                row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, sid + row["uuid"]))
                row["parentUuid"], previous = previous, row["uuid"]
            rows += part
        # Ordinary dialogue may quote a reference-shaped JSON object verbatim.
        rows[4]["message"]["content"] = "literal example " + json.dumps({"content": {
            "coverage": "tool-output-reference",
            "native_result": "claude:" + grandparent + ":" + rows[2]["uuid"]}}, sort_keys=True)
        self.transcript(rows)
        self.assertFalse(it.state_path("claude", grandparent).exists())
        original = it.capture("claude", parent, str(self.path), "capture-tests")
        self.assertTrue(original["ok"], original)
        self.assertEqual(original["captured_rounds"], 2)
        # The copy rewrites every row, and so every result locator, to the child's ID.
        copied = [dict(r, sessionId=self.sid) for r in rows]
        tail = claude_round(self.sid, 3)
        for row in tail:
            row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, self.sid + row["uuid"]))
        tail[0]["parentUuid"] = previous
        self.transcript(copied + tail)
        result = self.capture()
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["appended"], result["inherited_rounds"]), (1, 2))
        self.assertEqual(result["reviewed_rounds"], 0)
        self.assertEqual(self.capture()["appended"], 0)
        changed = json.loads(json.dumps(copied + tail))
        changed[2]["message"]["content"][0]["content"] = "different result"
        self.transcript(changed)
        refused = self.capture()
        self.assertFalse(refused["ok"])
        self.assertIn("inherited native prefix changed", refused["capture_error"])

    def test_relocated_capture_crash_keeps_candidate_for_catchup(self):
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        it.acknowledge("claude", self.sid, first["through"], "no_value", "Completed fixture only.")
        state_path = it.state_path("claude", self.sid)
        before = it._load(state_path, "claude", self.sid)
        candidate = self.path.with_suffix(".relocated.jsonl")
        candidate.write_text("".join(json.dumps(r) + "\n" for r in
                                    claude_round(self.sid, 1) + claude_round(self.sid, 2)), encoding="utf-8")
        with mock.patch.object(transcripts, "read", side_effect=SystemExit("Power loss while reading the relocated source.")):
            with self.assertRaises(SystemExit):
                it.capture("claude", self.sid, str(candidate), "capture-tests")
        crashed = it._load(state_path, "claude", self.sid)
        self.assertEqual(crashed["transcript_path"], before["transcript_path"])
        self.assertEqual(crashed.get("capture_path"), str(candidate.resolve()))
        caught = it.catchup(100)
        capture = next(c for c in caught["captures"] if c["conversation_id"] == self.sid)
        self.assertEqual((capture["ok"], capture["appended"]), (True, 1))
        # The new turn waits for its own conversation; no offline worker reviews it.
        self.assertNotIn(self.sid, [j["conversation_id"] for j in caught["jobs"]])
        recovered = it._load(state_path, "claude", self.sid)
        self.assertEqual(recovered["transcript_path"], str(candidate.resolve()))
        self.assertNotIn("capture_path", recovered)
        self.assertEqual(recovered["rounds"][:1], before["rounds"])
        self.assertEqual(recovered["reviews"], before["reviews"])
        self.assertEqual((len(recovered["rounds"]), recovered["reviewed_count"]), (2, 1))
        self.assertEqual(it.capture("claude", self.sid, None, "capture-tests")["appended"], 0)

    def test_cited_round_filters_secrets_and_a_later_hand_edit_does_not_reopen_review(self):
        from osk import distillation as D, graph
        token = "ghp_" + "a" * 36
        rows = claude_round(self.sid, 1)
        rows[0]["message"]["content"] = "## 12\nsecret " + token
        self.transcript(rows)
        st = it.capture("claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        cited = it.cite(f"claude/{self.sid}", turn="-1", note="the question that carried a secret")
        stored = (ROOT / cited["path"]).read_text(encoding="utf-8")
        self.assertNotIn(token, stored)
        self.assertIn("[FILTERED:github-token]", stored)
        self.assertIn("\\## 12", stored)  # a numeric H2 in the words is escaped, not a round
        self.assertNotIn(token, it.state_path("claude", self.sid).read_text(encoding="utf-8"))
        source = D._source(cited["round_ref"], graph.Index())
        self.assertEqual(source["native"], st["pending_refs"][0])
        saved = D.create_node({"key": self.sid, "sources": [cited["round_ref"]], "hub": "W1"},
                              title=self.sid, summary="cited question",
                              body="The question is kept with its cited turn.", drafter="test-model",
                              space="00_Scope/W1")
        self.assertEqual(saved["distillation"]["status"], "complete")
        it.acknowledge("claude", self.sid, st["through"], "preserved", "Cited and kept.", [{"key": self.sid}])
        self.assertEqual(it.review_status("claude", self.sid, st["through"])["status"], "complete")
        # Records are append-only; ACK checked the round, and nothing rechecks it later. A real
        # correction is registered by its corrector (reopen), which a hand edit skips.
        path = ROOT / cited["path"]
        path.write_bytes(path.read_bytes().replace(b"secret", b"tampered"))
        self.assertEqual(it.review_status("claude", self.sid, st["through"])["status"], "complete")
        self.assertFalse(it.status("claude", self.sid)["pending"])

    def test_record_identity_survives_vault_copy(self):
        import shutil
        self.transcript(claude_round(self.sid, 1))
        code = """import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ['OSK_PROBE_ENGINE'])
from osk import core, integration as it, raw, validate
if not (core.ROOT / '00_Scope/W1').is_dir():
    validate.make_mini_vault(core.ROOT)
sid = os.environ['OSK_PROBE_SID']
st = it.capture('claude', sid, os.environ['OSK_PROBE_TRANSCRIPT'], 'copy-session', '00_Scope/W1')
assert st['ok'], st
cited = it.cite('claude/' + sid, turn='-1')
print(json.dumps({'record': st['record'], 'refs': st['pending_refs'], 'appended': st['appended'],
                  'state_path': str(it.state_path('claude', sid)), 'cited': cited['round_ref'],
                  'reused': cited['reused']}))
"""
        with tempfile.TemporaryDirectory(prefix="osk-vault-copy-test-") as folder:
            first, second = Path(folder) / "original", Path(folder) / "copy"
            first.mkdir()
            results = []
            for root in (first, second):
                env = {**os.environ, "OSK_VAULT_ROOT": str(root), "OSK_PROBE_ENGINE": str(ENGINE),
                       "OSK_PROBE_SID": self.sid, "OSK_PROBE_TRANSCRIPT": str(self.path)}
                result = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                results.append(json.loads(result.stdout))
                if root == first:
                    shutil.copytree(first, second)
            self.assertEqual(results[0]["record"], results[1]["record"])
            self.assertEqual(results[0]["refs"], results[1]["refs"])
            self.assertNotEqual(results[0]["state_path"], results[1]["state_path"])
            # The local cursor stays behind; the copied record already holds the citation.
            self.assertEqual([r["appended"] for r in results], [1, 1])
            self.assertEqual(results[0]["cited"], results[1]["cited"])
            self.assertEqual([r["reused"] for r in results], [False, True])

    def test_cite_keeps_user_words_and_points_at_the_original_turn(self):
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2)
                        + claude_round(self.sid, 3, finished=False))
        st = self.capture()
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["pending_refs"], [raw.native_ref("claude", self.sid, f"user-{n}") for n in (1, 2)])
        self.assertFalse(raw.record_path("Capture", st["record"]).exists())
        conversation = f"claude/{self.sid}"
        cited = it.cite(conversation, quote="  question\n1 ")
        self.assertTrue(cited["round_ref"].startswith("00_Scope/Capture/_cited/.records/"), cited)
        self.assertEqual((cited["turn"], cited["user_by"], cited["reused"]), ("user-1", "engine", False))
        text = raw.read_round(cited["round_ref"])["text"]
        self.assertIn("question 1", text)
        self.assertNotIn("answer 1", text)  # the reply stays in the transcript, kept as its hash
        header = raw.cited_header(text)
        self.assertEqual((header["harness"], header["conversation"], header["turn"]), ("claude", self.sid, "user-1"))
        self.assertEqual(header["agent_sha256"], cited["agent_sha256"])
        self.assertEqual(raw.cited_native(text), st["pending_refs"][0])
        # The same turn, named again either way, keeps its one coordinate.
        again = it.cite(conversation, turn="user-1")
        self.assertEqual((again["round_ref"], again["reused"]), (cited["round_ref"], True))
        # The open turn is citable before it finishes and agrees with its finished round.
        open_turn = it.cite(conversation, turn="-1", note="decision under discussion")
        self.assertEqual((open_turn["turn"], open_turn["agent_sha256"]), ("user-3", None))
        self.assertIn("decision under discussion", raw.read_round(open_turn["round_ref"])["text"])
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2) + claude_round(self.sid, 3))
        self.capture()
        self.assertEqual(it.cite(conversation, quote="question 3")["round_ref"], open_turn["round_ref"])
        with self.assertRaisesRegex(ValueError, "3 turns match"):
            it.cite(conversation, quote="question")
        with self.assertRaisesRegex(ValueError, "relative turn"):
            it.cite(conversation, turn="2")
        with self.assertRaisesRegex(ValueError, "transcript is readable"):
            it.cite(conversation, user="words supplied by the caller")
        with self.assertRaisesRegex(ValueError, "harness"):
            it.cite("claude:" + self.sid, quote="question 1")

    def test_cite_without_transcript_marks_caller_supplied_words(self):
        self.transcript(claude_round(self.sid, 1))
        self.capture()
        self.path.unlink()
        conversation = f"claude/{self.sid}"
        with self.assertRaisesRegex(ValueError, "pass user="):
            it.cite(conversation, quote="question 1")
        cited = it.cite(conversation, user="question 1, as the agent recalls it", turn="user-1:final-1")
        self.assertEqual((cited["user_by"], cited["turn"], cited["agent_sha256"]), ("caller", "user-1", None))
        self.assertEqual(raw.cited_header(raw.read_round(cited["round_ref"])["text"])["user_by"], "caller")
        self.assertTrue(it.cite(conversation, user="anything", turn="user-1")["reused"])

    def test_offline_review_takes_stored_rounds_and_leaves_turns_to_their_conversation(self):
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        self.capture()
        state, record = legacy_cursor("claude", self.sid, self.path)
        stored = [r["ref"] for r in state["rounds"]]
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2) + claude_round(self.sid, 3))
        st = self.capture()
        turn = raw.native_ref("claude", self.sid, "user-3")
        self.assertEqual((st["appended"], st["pending_refs"]), (1, stored + [turn]))
        # The conversation itself sees both: stored rounds to read, its own turn to cite.
        online = it.prompt("claude", self.sid, include_organization=False)
        self.assertEqual(online["pending_refs"], st["pending_refs"])
        self.assertIn("read_cited", online["text"])
        self.assertIn(f'cite_round(conversation="claude/{self.sid}"', online["text"])
        # An offline worker has only the stored rounds.
        job = next(j for j in it.list_pending(100)["jobs"] if j["conversation_id"] == self.sid)
        self.assertEqual(job["pending_refs"], stored)
        self.assertEqual(job["raw_review"]["state"], "verified")
        self.assertNotIn("cite_round", job["prompt"])
        it.acknowledge("claude", self.sid, job["through"], "no_value", "stored fixture rounds only")
        self.assertNotIn(self.sid, [j["conversation_id"] for j in it.list_pending(100)["jobs"]])
        rest = it.prompt("claude", self.sid, include_organization=False)
        self.assertEqual((rest["pending_refs"], rest["raw_review"]["state"]), ([turn], "native"))
        # A same-name record in `_raw/` stays the record and takes the new citation.
        cited = it.cite(f"claude/{self.sid}", turn="-1")
        self.assertEqual(cited["path"], record.relative_to(ROOT).as_posix())
        self.assertEqual(cited["index"], 3)

    def test_native_secret_strings_never_reach_cursor_or_cited_record(self):
        tokens = ["ghp_" + letter * 36 for letter in "uakv"]
        fence = chr(96) * 3
        block = lambda token: fence + "text\n" + token + "\n" + fence
        nested = {"items": [{"\n" + tokens[2]: block(tokens[3])}], "benign": "ghp_short"}
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                sid = self.sid + "-" + harness
                path = Path(TMP.name) / (sid + ".jsonl")
                if harness == "claude":
                    rows = claude_round(sid, 1)
                    rows[0]["message"]["content"] = [{"type": "text", "text": block(tokens[0])}]
                    rows[1]["message"]["content"][0]["input"] = nested
                    rows[2]["message"]["content"][0]["content"] = {"result": [block(tokens[3])]}
                    rows[-1]["message"]["content"][0]["text"] = block(tokens[1])
                else:
                    rows = [{"type": "session_meta", "payload": {"id": sid}}] + codex_round(1)
                    rows[4]["payload"]["message"] = block(tokens[0])
                    rows[5]["payload"]["arguments"] = json.dumps(nested)
                    rows[6]["payload"]["output"] = json.dumps({"result": [block(tokens[3])]})
                    rows[7]["payload"]["content"][0]["text"] = block(tokens[1])
                native = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
                path.write_bytes(native)
                st = it.capture(harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertEqual(st["captured_rounds"], 1)
                with mock.patch.object(raw.secrets, "write_raw", wraps=raw.secrets.write_raw) as sink:
                    cited = it.cite(f"{harness}/{sid}", turn="-1", note="the reply is kept as its hash")
                self.assertEqual(sink.call_count, 1)  # mandatory final filter still runs
                saved = (ROOT / cited["path"]).read_bytes()
                cursor = it.state_path(harness, sid).read_bytes()
                for token in tokens:
                    self.assertNotIn(token.encode(), saved)
                    self.assertNotIn(token.encode(), cursor)
                self.assertEqual(saved.count(b"[FILTERED:github-token]"), 1)  # the user's words only
                self.assertTrue(cited["agent_sha256"])
                self.assertEqual(path.read_bytes(), native)  # native evidence is not edited
                again = it.cite(f"{harness}/{sid}", turn="-1")
                self.assertTrue(again["reused"])
                self.assertEqual((ROOT / cited["path"]).read_bytes(), saved)

    def test_batch_reads_only_the_unreviewed_turns_of_an_ended_conversation(self):
        import mcp_server
        # Inventory only this test's conversations.
        state_path_for = it.state_path
        state_dir = Path(TMP.name) / self.sid
        patcher = mock.patch.object(it, "state_path", side_effect=lambda harness, sid:
                                    state_dir / state_path_for(harness, sid).name)
        patcher.start()
        self.addCleanup(patcher.stop)
        talks = {}
        for name, count in (("done", 2), ("short", 5), ("running", 3)):
            sid = f"{self.sid}-{name}"
            path = Path(TMP.name) / f"{sid}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for n in range(1, count + 1)
                                    for r in claude_round(sid, n)), encoding="utf-8")
            st = it.capture("claude", sid, str(path), "capture-tests")
            self.assertTrue(st["ok"], st)
            talks[name] = sid, path, st
        sid, path, st = talks["done"]
        it.acknowledge("claude", sid, st["through"], "no_value", "fixture turns only")
        sid, path, st = talks["short"]
        first = it.prompt("claude", sid, include_organization=False, max_rounds=2)
        it.acknowledge("claude", sid, first["through"], "no_value", "first two fixture turns")
        ended = time.time() - it.ENDED_AFTER - 60
        for name in ("done", "short"):
            os.utime(talks[name][1], (ended, ended))
        jobs = {j["conversation_id"]: j for j in it.list_pending(100)["jobs"]}
        # A fully reviewed conversation is not read; a running one keeps its own cadence.
        self.assertEqual(set(jobs), {sid})
        job = jobs[sid]
        self.assertEqual(job["pending_refs"], [raw.native_ref("claude", sid, f"user-{n}") for n in (3, 4, 5)])
        # A sandboxed worker reads them through MCP, not the CLI.
        self.assertIn("read_cited(ref=", job["prompt"])
        self.assertNotIn("read_command", job)
        read = it.read_turns(job["pending_refs"])["turns"]
        self.assertEqual([t["position"] for t in read], [3, 4, 5])
        self.assertTrue(all(f"question {t['position']}" in t["text"] for t in read), read)
        self.assertNotIn("question 2", json.dumps(read))
        self.assertEqual(read[0]["previous"], raw.native_ref("claude", sid, "user-2"))
        # Earlier context only on demand, one turn back at a time.
        [back] = it.read_turns([read[0]["previous"]])["turns"]
        self.assertEqual((back["position"], back["previous"]), (2, raw.native_ref("claude", sid, "user-1")))
        out = mcp_server.read_cited(job["pending_refs"][0], view="full")
        self.assertEqual((out["ok"], out["position"]), (True, 3), out)
        self.assertIn("answer 3", out["text"])
        with self.assertRaisesRegex(ValueError, "not a completed turn"):
            it.read_turns([raw.native_ref("claude", sid, "user-9")])
        done = it.acknowledge("claude", sid, job["through"], "no_value", "ended fixture turns")
        self.assertEqual((done["reviewed_rounds"], done["pending"]), (5, False))
        self.assertNotIn(sid, [j["conversation_id"] for j in it.list_pending(100)["jobs"]])

    def test_complete_review_restarts_the_conversation_cadence(self):
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2) + claude_round(self.sid, 3))
        self.capture()
        for _ in range(6):
            it.tick("claude", self.sid)
        path = it.state_path("claude", self.sid)
        state = it._load(path, "claude", self.sid)
        state["response_growth"] = {"counter": "finals", "seen": ["final"], "count": 6,
                                    "attempted_count": 0, "history_baselined": True}
        it._save(path, state)
        part = it.prompt("claude", self.sid, include_organization=False, max_rounds=2)
        it.acknowledge("claude", self.sid, part["through"], "no_value", "first two fixture turns")
        state = it._load(path, "claude", self.sid)
        # A partial review leaves both counts running.
        self.assertEqual((state["reviewed_prompt_count"], state["response_growth"]["attempted_count"]), (2, 0))
        rest = it.prompt("claude", self.sid, include_organization=False)
        it.acknowledge("claude", self.sid, rest["through"], "no_value", "last fixture turn")
        state = it._load(path, "claude", self.sid)
        self.assertEqual((state["reviewed_prompt_count"], state["response_growth"]["attempted_count"]), (6, 6))
        self.assertEqual(it.tick("claude", self.sid)["unreviewed_prompts"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
