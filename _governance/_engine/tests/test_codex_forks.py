"""Codex fork ancestry, immutable raw reuse and isolated recovery checks."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

TMP = tempfile.TemporaryDirectory(prefix="osk-codex-fork-test-")
os.environ["OSK_VAULT_ROOT"] = str(Path(TMP.name) / "vault")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from osk import core, integration as it, raw, scope_memory, transcripts, validate

if core.ROOT != Path(os.environ["OSK_VAULT_ROOT"]).resolve():
    raise RuntimeError("Codex fork tests require their own process")
validate.make_mini_vault(core.ROOT)
for scope in ("Forks", "Other"):
    (core.ROOT / "00_Scope" / scope).mkdir()
    scope_memory.replace(scope.lower(), "", space="00_Scope/" + scope)


def event(kind, **payload):
    return {"type": kind, "payload": payload}


def turn(owner, rid, *, finished=True):
    rows = [event("event_msg", type="task_started", turn_id=rid),
            event("event_msg", type="item_completed", thread_id=owner, turn_id=rid,
                  item={"type": "UserMessage", "content": [{"type": "text", "text": "question " + rid}]}),
            event("response_item", type="function_call", name="probe", arguments="{}", call_id=rid),
            event("response_item", type="function_call_output", call_id=rid, output="evidence"),
            event("response_item", type="message", role="assistant", phase="final_answer",
                  content=[{"type": "output_text", "text": "answer " + rid}])]
    if finished:
        rows.append(event("event_msg", type="task_complete", turn_id=rid, last_agent_message="answer " + rid))
    return rows


class CodexForkTests(unittest.TestCase):
    def setUp(self):
        self.parent = self._testMethodName + "-parent"
        self.child = self._testMethodName + "-child"
        self.home = Path(TMP.name) / self._testMethodName
        self.folder = self.home / "sessions/2026/10/03"
        self.folder.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        state_path = it.state_path
        states = mock.patch.object(it, "state_path", side_effect=lambda harness, sid:
                                   self.home / "states" / state_path(harness, sid).name)
        states.start()
        self.addCleanup(states.stop)

    def page(self, owner, key, rows, *, base=None, fork=None):
        meta = {"id": owner}
        if base:
            meta["history_base"] = {"thread_id": base[0], "end_byte_offset": base[1]}
        if fork:
            meta["forked_from_id"] = fork
        path = self.folder / ("rollout-" + key + ".jsonl")
        path.write_text("".join(json.dumps(r) + "\n" for r in [event("session_meta", **meta)] + rows), encoding="utf-8")
        return path

    def fork(self, *, parent_rows=None, child_rows=None, fork=None):
        parent = self.page(self.parent, self.parent, parent_rows if parent_rows is not None else turn(self.parent, "p1"))
        child = self.page(self.child, self.child, child_rows if child_rows is not None else turn(self.child, "c1"),
                          base=(self.parent, parent.stat().st_size), fork=fork or self.parent)
        return parent, child

    def test_shared_parent_raw_and_ack_remain_owned_by_parent(self):
        parent = self.page(self.parent, self.parent, turn(self.parent, "p1"))
        first = it.capture("codex", self.parent, str(parent), "forks")
        it.acknowledge("codex", self.parent, first["through"], "no_value", "synthetic fixture")
        with parent.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in turn(self.parent, "p2")))
        pending = it.capture("codex", self.parent, str(parent), "forks")
        self.assertEqual((pending["captured_rounds"], pending["reviewed_rounds"]), (2, 1))
        child = self.page(self.child, self.child, turn(self.child, "c1"),
                          base=(self.parent, parent.stat().st_size), fork=self.parent)
        parent_state = it.state_path("codex", self.parent)
        before_state = parent_state.read_bytes()
        parent_raw = raw._raw_file(raw.parse_ref(first["pending_refs"][0])[0])
        before_raw = parent_raw.read_bytes()
        found = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(found["ok"], found)
        self.assertEqual((found["captured_rounds"], found["inherited_rounds"], found["reviewed_rounds"]), (1, 2, 0))
        self.assertEqual(parent_state.read_bytes(), before_state)
        self.assertEqual(parent_raw.read_bytes(), before_raw)
        child_raw = raw._raw_file(raw.parse_ref(found["pending_refs"][0])[0])
        before_child = child_raw.read_bytes()
        it.state_path("codex", self.child).unlink()
        restored = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(restored["ok"], restored)
        self.assertEqual((restored["appended"], restored["inherited_rounds"]), (0, 2))
        self.assertEqual(child_raw.read_bytes(), before_child)
        self.assertEqual(parent_raw.read_bytes(), before_raw)

    def test_uncaptured_parent_keeps_native_ownership_and_all_completed_rounds(self):
        _, child = self.fork()
        parsed = transcripts.read(str(child), "codex", self.child)
        self.assertEqual([(r["id"], r["origin_conversation_id"]) for r in parsed["rounds"]],
                         [("p1", self.parent), ("c1", self.child)])
        self.assertIn("codex:" + self.parent + ":p1", parsed["dialogue_v1"]["p1"]["agent"])
        found = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(found["ok"], found)
        self.assertEqual((found["captured_rounds"], found["inherited_rounds"]), (2, 0))
        self.assertEqual(it.capture("codex", self.child, None, "forks")["appended"], 0)

    def test_paginated_parent_and_child_follow_declared_byte_bounds(self):
        first = self.page(self.parent, self.parent + "-old", turn(self.parent, "p1"))
        second = self.page(self.parent, self.parent, turn(self.parent, "p2"),
                           base=(self.parent + "-old", first.stat().st_size))
        child_first = self.page(self.child, self.child + "-old", turn(self.child, "c1"),
                                base=(self.parent, second.stat().st_size), fork=self.parent)
        child = self.page(self.child, self.child, turn(self.child, "c2"),
                          base=(self.child + "-old", child_first.stat().st_size), fork=self.parent)
        with second.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in turn(self.parent, "excluded")))
        parsed = transcripts.read(str(child), "codex", self.child)
        self.assertEqual([r["id"] for r in parsed["rounds"]], ["p1", "p2", "c1", "c2"])
        self.assertFalse(parsed["pending_tail"])

    def test_unlinked_parent_and_foreign_user_items_are_rejected(self):
        for bad in ("fork", "user", "turn", "duplicate"):
            with self.subTest(bad=bad):
                rows = turn(self.child, "c1" if bad != "duplicate" else "p1")
                if bad == "user":
                    rows[1]["payload"]["thread_id"] = self.parent
                if bad == "turn":
                    rows[1]["payload"]["turn_id"] = "wrong-turn"
                _, child = self.fork(child_rows=rows, fork="unrelated" if bad == "fork" else self.parent)
                with self.assertRaises(ValueError):
                    transcripts.read(str(child), "codex", self.child)

    def test_open_parent_tail_is_not_completed_by_the_child(self):
        _, child = self.fork(parent_rows=turn(self.parent, "p1", finished=False))
        parsed = transcripts.read(str(child), "codex", self.child)
        self.assertEqual([r["id"] for r in parsed["rounds"]], ["c1"])
        self.assertFalse(parsed["pending_tail"])
        self.assertEqual(parsed["coverage"]["ancestor_pending_tails"], 1)

    def test_parent_raw_from_other_scope_is_not_reused(self):
        parent, child = self.fork()
        first = it.capture("codex", self.parent, str(parent), "other")
        self.assertTrue(first["ok"], first)
        captured = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(captured["ok"], captured)
        self.assertEqual((captured["captured_rounds"], captured["inherited_rounds"]), (2, 0))

    def test_delivered_thread_message_is_a_native_trigger_not_a_human_prompt(self):
        rows = turn(self.child, "c1")
        rows[1] = event("response_item", type="function_call_output", namespace="codex_app",
                        name="send_message_to_thread", output="synthetic delivered task",
                        internal_chat_message_metadata_passthrough={"turn_id": "c1"})
        parent, child = self.fork(child_rows=rows)
        it.capture("codex", self.parent, str(parent), "forks")
        parsed = transcripts.read(str(child), "codex", self.child)
        self.assertFalse(parsed["pending_tail"])
        self.assertIn('"native_trigger": "thread_message"', parsed["dialogue_v1"]["c1"]["user"])
        captured = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(captured["ok"], captured)
        self.assertEqual((captured["captured_rounds"], captured["inherited_rounds"]), (1, 1))
        # A regular tool result or a result for another turn cannot become input.
        for change in ({"call_id": "ordinary-call"},
                       {"internal_chat_message_metadata_passthrough": {"turn_id": "foreign"}}):
            changed = json.loads(json.dumps(rows))
            changed[1]["payload"].update(change)
            _, child = self.fork(child_rows=changed)
            try:
                refused = transcripts.read(str(child), "codex", self.child)
            except ValueError:
                continue
            self.assertEqual([r["id"] for r in refused["rounds"]], ["p1"])
            self.assertTrue(refused["pending_tail"])

    def test_inherited_raw_change_is_refused_without_appending(self):
        parent, child = self.fork()
        first = it.capture("codex", self.parent, str(parent), "forks")
        found = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(found["ok"], found)
        child_raw = raw._raw_file(raw.parse_ref(found["pending_refs"][0])[0])
        before = child_raw.read_bytes()
        parent_raw = raw._raw_file(raw.parse_ref(first["pending_refs"][0])[0])
        parent_raw.write_bytes(parent_raw.read_bytes().replace(b"answer p1", b"changed p1"))
        refused = it.capture("codex", self.child, None, "forks")
        self.assertFalse(refused["ok"], refused)
        self.assertIn("inherited raw changed", refused["capture_error"])
        self.assertEqual(child_raw.read_bytes(), before)

    def test_legacy_empty_fields_require_failed_source_discovery(self):
        s = it._load(it.state_path("codex", self.child), "codex", self.child)
        s.update(capture_pending=True, capture_error="ValueError: native transcript path unavailable")
        before = json.dumps(s, sort_keys=True)
        existing = self.page(self.child, self.child, turn(self.child, "c1"))
        for found, expected in ((None, "awaiting_input"), (str(self.home / "missing.jsonl"), "awaiting_input"),
                                (str(existing), "retryable"), (str(self.home), "action_required")):
            with self.subTest(found=found), mock.patch.object(it, "_locate_transcript", return_value=found):
                self.assertEqual(it._capture_recovery(s)["state"], expected)
                self.assertEqual(json.dumps(s, sort_keys=True), before)
        with mock.patch.object(it, "_locate_transcript", side_effect=PermissionError("fixture")):
            advice = it._capture_recovery(s)
            self.assertEqual((advice["state"], advice["basis"]), ("action_required", "source_lookup_failed"))
        s["prompt_count"] = 1
        with mock.patch.object(it, "_locate_transcript", return_value=None):
            self.assertEqual(it._capture_recovery(s)["state"], "action_required")

    def test_legacy_failure_phase_does_not_infer_no_input_from_empty_fields(self):
        s = it._load(it.state_path("codex", self.child), "codex", self.child)
        for error, phase in (("WriteError: 착지 미정", "landing"),
                             ("ValueError: conversation scope changed", "landing"),
                             ("ValueError: native round identity prefix changed", "replay"),
                             ("ValueError: Codex history identity mismatch", "read"),
                             ("unrecognized old failure", "unknown")):
            with self.subTest(error=error):
                s["capture_error"] = error
                advice = it._capture_recovery(s)
                self.assertEqual((advice["state"], advice["phase"]), ("action_required", phase))
        s["capture_failure_phase"] = "source"
        s["capture_error"] = "FileNotFoundError: fixture"
        with mock.patch.object(it, "_locate_transcript", side_effect=AssertionError("recorded phase is sufficient")):
            self.assertEqual(it._capture_recovery(s)["basis"], "recorded_phase")


if __name__ == "__main__":
    unittest.main()
