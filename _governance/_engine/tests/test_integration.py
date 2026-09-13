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
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="osk-integration-test-")
ROOT = Path(TMP.name) / "vault"
os.environ["OSK_VAULT_ROOT"] = str(ROOT)
sys.path.insert(0, str(ENGINE))
from osk import core, integration as it, raw, scope_memory, transcripts, validate, write

validate.make_mini_vault(ROOT)
(ROOT / "= Scope/Capture").mkdir()
scope_memory.replace("capture-tests", "", space="= Scope/Capture")


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


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sid = self._testMethodName
        self.path = Path(TMP.name) / f"{self.sid}.jsonl"

    def transcript(self, rows):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def capture(self, harness="claude"):
        return it.capture(harness, self.sid, str(self.path), "capture-tests")

    def test_claude_completion_and_tool_results(self):
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2, finished=False))
        st = self.capture()
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["captured_rounds"], 1)
        self.assertTrue(st["capture_pending"])
        text = raw.read_round(st["pending_refs"][0])["text"]
        self.assertIn("evidence result", text)
        self.assertIn("tool_use", text)
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
        text = raw.read_round(st["pending_refs"][0])["text"]
        self.assertIn("function_call_output", text)
        self.assertIn("evidence result", text)
        self.assertEqual(text.count("question 1"), 1)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "wrong", "last_agent_message": "answer"}}) + "\n")
        self.assertFalse(self.capture("codex")["ok"])

    def test_crash_after_raw_append_retries_without_duplicate(self):
        self.transcript(claude_round(self.sid, 1))
        original = raw.append_rounds

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise SystemExit("simulated power loss after raw append")

        with mock.patch.object(raw, "append_rounds", crash):
            with self.assertRaises(SystemExit):
                self.capture()
        self.assertTrue(it.status("claude", self.sid)["pending"])
        st = self.capture()
        self.assertEqual(st["appended"], 0)
        self.assertEqual(st["captured_rounds"], 1)
        self.assertEqual(raw.record_state("capture-tests", st["record"])["rounds"], 1)
        it.state_path("claude", self.sid).unlink()
        self.assertEqual(self.capture()["appended"], 0)

    def test_resume_empty_memory_and_other_writer_never_ack(self):
        self.transcript(claude_round(self.sid, 1))
        st = self.capture()
        for _ in range(8):
            self.assertFalse(it.tick("claude", self.sid)["due"])
        self.assertTrue(it.tick("claude", self.sid)["due"])
        self.assertIn("공유 기억이 비어 있어도", it.prompt("claude", self.sid)["text"])
        cur = scope_memory.read("capture-tests")
        scope_memory.replace("other-writer", "other session writes shared memory", cur["hash"], "= Scope/Capture")
        resumed = it.hook_capture({"harness": "claude", "session_id": self.sid, "transcript_path": str(self.path)}, "capture-tests")
        self.assertEqual(resumed["prompt_count"], 9)
        self.assertIsNone(resumed["reviewed_through"])
        self.assertTrue(resumed["pending"])
        other = it.status("claude", self.sid + "-other")
        self.assertFalse(other["pending"])
        self.assertNotIn(st["pending_refs"][0], it.prompt("claude", self.sid + "-other")["text"])

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

    def test_native_identity_mismatch_and_changed_prefix_fail_closed(self):
        self.transcript(claude_round("not-this-conversation", 1))
        self.assertFalse(self.capture()["ok"])
        self.transcript(claude_round(self.sid, 1))
        self.assertTrue(self.capture()["ok"])
        changed = claude_round(self.sid, 1)
        changed[0]["message"]["content"] = "rewritten old question"
        self.transcript(changed)
        st = self.capture()
        self.assertFalse(st["ok"])
        self.assertEqual(raw.record_state("capture-tests", st["record"])["rounds"], 1)

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
        first = raw.read_round(st["pending_refs"][0])["text"]
        second = raw.read_round(st["pending_refs"][1])["text"]
        self.assertIn("turn_aborted", first)
        self.assertIn("question 1", first)
        self.assertNotIn("question 1", second)
        self.assertFalse(self.capture("codex")["capture_pending"])

    def test_file_reads_and_mixed_exec_are_references_but_probe_output_stays(self):
        rows = claude_round(self.sid, 1)
        rows[1]["message"]["content"][0].update(name="Read", input={"file_path": "C:/private/source.txt"})
        rows[2]["message"]["content"][0]["content"] = "FILE_FULL_CONTENT_MUST_NOT_COPY"
        more = claude_round(self.sid, 2)
        more[1]["message"]["content"][0].update(name="Bash", input={"command": "mixed command"})
        more[2]["message"]["content"][0]["content"] = "MIXED_OUTPUT_MUST_NOT_COPY"
        self.transcript(rows + more + claude_round(self.sid, 3))
        st = self.capture()
        text = "\n".join(raw.read_round(ref)["text"] for ref in st["pending_refs"])
        self.assertTrue(st["ok"], st)
        self.assertNotIn("FILE_FULL_CONTENT_MUST_NOT_COPY", text)
        self.assertNotIn("MIXED_OUTPUT_MUST_NOT_COPY", text)
        self.assertIn("C:/private/source.txt", text)
        self.assertIn("native_result", text)
        self.assertIn("sha256", text)
        self.assertIn("evidence result", text)
        self.assertEqual(st["coverage"]["mode"], "tool-output-reference")
        self.assertIn("전사 보관", it.prompt("claude", self.sid)["text"])

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
        text = raw.read_round(st["pending_refs"][0])["text"]
        self.assertNotIn("NODE_FULL_CONTENT_MUST_NOT_COPY", text)
        self.assertIn("source-node", text)
        self.assertIn("turn_aborted", raw.read_round(st["pending_refs"][1])["text"])

    def test_stop_before_flush_catchup_without_resume_and_changed_after_ack(self):
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1, finished=False))
        pending = self.capture("codex")
        self.assertTrue(pending["capture_pending"])
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1))
        caught = it.catchup(100)
        job = next(j for j in caught["jobs"] if j["conversation_id"] == self.sid)
        self.assertEqual(len(job["pending_refs"]), 1)
        self.assertIn(sys.executable, job["prompt"])
        it.acknowledge("codex", self.sid, job["through"], "no_value", "bounded test round")
        self.assertEqual(it.review_status("codex", self.sid, job["through"])["status"], "complete")
        self.assertFalse(it.status("codex", self.sid)["pending"])
        # The session can append and exit before Stop; fingerprint is a hint to
        # revalidate raw's durable prefix, never proof that new work was reviewed.
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1) + codex_round(2))
        caught = it.catchup(100)
        job2 = next(j for j in caught["jobs"] if j["conversation_id"] == self.sid)
        self.assertEqual(len(job2["pending_refs"]), 1)
        self.assertNotEqual(job["through"], job2["through"])

    def test_review_manifests_are_bounded_and_find_root_specific_states(self):
        self.transcript([r for n in range(1, 19) for r in claude_round(self.sid, n)])
        self.capture()
        job = next(j for j in it.list_pending(100)["jobs"] if j["conversation_id"] == self.sid)
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
            self.assertTrue(any(j["conversation_id"] == git_sid for j in it.list_pending()["jobs"]))
        finally:
            import shutil
            shutil.rmtree(ROOT / ".git")

    def test_source_hash_matches_distillation_and_tampering_rejects_ack(self):
        from osk import distillation, graph
        rows = claude_round(self.sid, 1)
        rows[0]["message"]["content"] = "## 12\nsecret ghp_" + "a" * 36
        self.transcript(rows)
        st = self.capture()
        s = it._load(it.state_path("claude", self.sid), "claude", self.sid)
        source = distillation._source(st["pending_refs"][0], graph.Index())
        self.assertEqual(s["rounds"][0]["hash"], source["hash"])
        path = raw._raw_file(source["path"])
        path.write_bytes(path.read_bytes().replace(b"answer 1", b"tampered"))
        with self.assertRaises(ValueError):
            it.acknowledge("claude", self.sid, st["through"], "no_value", "stale raw is not acknowledged")

    def test_unbound_incomplete_capture_retains_explicit_landing_for_catchup(self):
        self.transcript(claude_round(self.sid, 1, finished=False))
        pending = it.capture("claude", self.sid, str(self.path), self.sid, "= Scope/Capture")
        self.assertTrue(pending["capture_pending"])
        self.assertIsNone(write.resolve_session(self.sid))
        self.transcript(claude_round(self.sid, 1))
        done = it.catchup(100)
        job = next(j for j in done["jobs"] if j["conversation_id"] == self.sid)
        self.assertEqual(len(job["pending_refs"]), 1)
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
        with self.assertRaises(write.WriteError):
            raw.append_rounds("capture-tests", st["record"], [parsed["rounds"][-1]])

    def test_actual_preserved_scope_node_ack_and_later_receipt_validation(self):
        from osk import contract, distillation as D
        self.transcript(claude_round(self.sid, 1))
        st = it.capture("claude", self.sid, str(self.path), self.sid, space="= Scope/W1")
        ref = st["pending_refs"][0]
        spec = {"key": self.sid, "sources": [ref], "hub": "W1"}
        args = {"title": "A retained integration decision", "summary": "bounded retry",
                "body": "A retry is identified by native identity rather than repeated wording.",
                "drafter": "fable-5", "space": "= Scope/W1"}
        created = D.create_node(spec, **args)
        self.assertEqual(created["distillation"]["status"], "complete")
        node = contract.parse(core.ROOT / created["path"])
        hub = contract.parse(core.ROOT / "= Scope/W1/W1.md")
        self.assertIn(ref, write._stored_edges(node.meta["derived-from"]))
        self.assertIn(created["name"], hub.wikilinks())
        ack = it.acknowledge("claude", self.sid, st["through"], "preserved",
                             "The raw observation is retained in the existing project cluster.",
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
        write.update_node(created["id"], body="Changed after review.", expect_hash=created["new_hash"])
        self.assertEqual(it.review_status("claude", self.sid, st["through"])["status"], "pending")

    def test_raw_record_identity_survives_vault_copy_without_duplicate(self):
        import shutil
        self.transcript(claude_round(self.sid, 1))
        code = """import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ['OSK_PROBE_ENGINE'])
from osk import core, integration as it, raw, validate
if not (core.ROOT / '= Scope/W1').is_dir():
    validate.make_mini_vault(core.ROOT)
sid = os.environ['OSK_PROBE_SID']
st = it.capture('claude', sid, os.environ['OSK_PROBE_TRANSCRIPT'], 'copy-session', '= Scope/W1')
assert st['ok'], st
print(json.dumps({'record': st['record'], 'refs': st['pending_refs'], 'appended': st['appended'],
                  'state_path': str(it.state_path('claude', sid)),
                  'raw_rounds': raw.record_state('copy-session', st['record'])['rounds']}))
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
            self.assertEqual([r["appended"] for r in results], [1, 0])
            self.assertEqual([r["raw_rounds"] for r in results], [1, 1])

    def test_existing_saved_record_name_is_preserved(self):
        self.transcript(claude_round(self.sid, 1))
        path = it.state_path("claude", self.sid)
        state = it._load(path, "claude", self.sid)
        state["record"] = "legacy-record-name"
        it._save(path, state)
        st = self.capture()
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["record"], "legacy-record-name")
        self.assertIn("/legacy-record-name.md#1", st["pending_refs"][0])

    def test_review_key_tracks_snapshot_and_recovers_saved_proof(self):
        from osk import distillation as D
        self.transcript(claude_round(self.sid, 1))
        it.capture("claude", self.sid, str(self.path), self.sid, "= Scope/W1")
        first = it.prompt("claude", self.sid)
        self.assertEqual(first["key"], it.prompt("claude", self.sid)["key"])
        spec = {"key": first["key"] + ":stable-target", "sources": first["pending_refs"], "hub": "W1"}
        saved = D.create_node(spec, title="Snapshot-specific retained observation", summary="native observation",
                              body="The observation is preserved before its review acknowledgement arrives.",
                              drafter="fable-5", space="= Scope/W1")
        self.assertEqual(saved["distillation"]["status"], "complete")
        retry = it.prompt("claude", self.sid)
        self.assertEqual(first["key"], retry["key"])
        proof = next(p for p in retry["previous_distillations"] if p["key"] == spec["key"])
        self.assertEqual(proof["status"], "complete")
        self.assertIn(spec["key"], retry["text"])
        self.assertIn("대상별 고정 접미사", retry["text"])
        for listed in (it.list_pending(100), it.catchup(100)):
            job = next(j for j in listed["jobs"] if j["conversation_id"] == self.sid)
            self.assertEqual(job["key"], first["key"])
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        it.capture("claude", self.sid, str(self.path), self.sid)
        later = it.prompt("claude", self.sid)
        self.assertNotEqual(later["through"], first["through"])
        self.assertNotEqual(later["key"], first["key"])
        self.assertEqual(later["key"], it.prompt("claude", self.sid)["key"])
        self.assertTrue(any(p["key"] == spec["key"] for p in later["previous_distillations"]))

    def _discovery_source(self):
        self.transcript(claude_round(self.sid, 1))
        return it.capture("claude", self.sid, str(self.path), self.sid, "= Scope/W1")

    def _discovery_save(self, st, key, title=None):
        from osk import distillation as D
        self.addCleanup(D._job_path(key).unlink, missing_ok=True)
        return D.create_node({"key": key, "sources": st["pending_refs"], "hub": "W1"},
                             title=title or self.sid + "-node", summary="lost ACK recovery",
                             body="Keep the already retained decision and finish its missing acknowledgement.",
                             drafter="fable-5", space="= Scope/W1")

    def test_saved_scope_without_ack_is_discovered_and_reused(self):
        from osk import distillation as D, graph
        st = self._discovery_source()
        key = self.sid + "-opaque-worker-key"
        saved = self._discovery_save(st, key)
        target = core.ROOT / saved["path"]
        before = target.read_bytes()
        next_prompt = it.prompt("claude", self.sid)
        proofs = next_prompt["previous_distillations"]
        self.assertEqual([proof["key"] for proof in proofs], [key])
        self.assertEqual(proofs[0]["status"], "complete")
        catchup = it.catchup(limit=100)
        own = next(job for job in catchup["jobs"] if job["conversation_id"] == self.sid)
        self.assertEqual([proof["key"] for proof in own["previous_distillations"]], [key])
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
        other = it.capture("claude", other_sid, str(other_path), other_sid, "= Scope/W1")
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
        hub = ROOT / "= Scope/W1/W1.md"
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



if __name__ == "__main__":
    unittest.main(verbosity=2)
