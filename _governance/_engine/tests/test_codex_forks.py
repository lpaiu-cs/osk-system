"""Codex fork ancestry, parent-owned turns and isolated recovery checks (from PR #130)."""
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

    def guidance(self):
        return it.prompt("codex", self.child, include_organization=False)["text"]

    def test_parent_turns_and_ack_remain_owned_by_parent(self):
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
        found = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(found["ok"], found)
        self.assertEqual((found["captured_rounds"], found["inherited_rounds"], found["reviewed_rounds"]), (1, 2, 0))
        self.assertEqual(found["pending_refs"], [raw.native_ref("codex", self.child, "c1")])
        self.assertEqual(parent_state.read_bytes(), before_state)
        self.assertIn("fork 부모 대화의 과거 2턴은 부모의 몫이다", self.guidance())
        # A lost child cursor tracks the same turns again, still without the parent's.
        it.state_path("codex", self.child).unlink()
        restored = it.capture("codex", self.child, str(child), "forks")
        self.assertEqual((restored["ok"], restored["captured_rounds"], restored["inherited_rounds"]), (True, 1, 2))
        # The parent's unreviewed turn stays the parent's.
        self.assertEqual(it.status("codex", self.parent)["pending_refs"], [raw.native_ref("codex", self.parent, "p2")])

    def test_untracked_parent_turns_stay_out_of_the_child(self):
        _, child = self.fork()
        parsed = transcripts.read(str(child), "codex", self.child)
        self.assertEqual([(r["id"], r["origin_conversation_id"]) for r in parsed["rounds"]],
                         [("p1", self.parent), ("c1", self.child)])
        self.assertIn("codex:" + self.parent + ":p1", parsed["dialogue_v1"]["p1"]["agent"])
        found = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(found["ok"], found)
        # The parent is not tracked here; its turn is still the parent's, not the child's.
        self.assertEqual((found["captured_rounds"], found["inherited_rounds"]), (1, 1))
        self.assertEqual(it.capture("codex", self.child, None, "forks")["appended"], 0)
        self.assertIn("부모 대화를 capture해 그쪽에서 검토한다", self.guidance())
        # Cited or read through the child, the turn still names its parent.
        cited = it.cite("codex/" + self.child, quote="question p1")
        _, header = raw.find_cited(core.ROOT / cited["path"], raw.native_ref("codex", self.parent, "p1"))
        self.assertEqual(header["conversation"], self.parent)
        [own] = it.read_turns([raw.native_ref("codex", self.child, "c1")])["turns"]
        [back] = it.read_turns([own["previous"]])["turns"]  # the parent's own transcript, untracked here
        self.assertEqual((back["ref"], back["turn"]), (raw.native_ref("codex", self.parent, "p1"), "p1"))

    def test_parent_turns_keep_their_owner_when_cited_or_read_from_the_child(self):
        parent, child = self.fork()
        it.capture("codex", self.parent, str(parent), "forks")
        it.capture("codex", self.child, str(child), "forks")
        cited = it.cite("codex/" + self.child, quote="question p1")
        _, header = raw.find_cited(core.ROOT / cited["path"], raw.native_ref("codex", self.parent, "p1"))
        self.assertEqual((cited["turn"], header["conversation"]), ("p1", self.parent))
        # The parent's own citation of that turn is the same round of the same record.
        again = it.cite("codex/" + self.parent, quote="question p1")
        self.assertEqual((again["reused"], again["round_ref"]), (True, cited["round_ref"]))
        [own] = it.read_turns([raw.native_ref("codex", self.child, "c1")])["turns"]
        self.assertEqual(own["previous"], raw.native_ref("codex", self.parent, "p1"))
        # A child coordinate cannot pass for the parent's turn.
        with self.assertRaisesRegex(ValueError, "belongs to the fork parent; read native:codex:"):
            it.read_turns([raw.native_ref("codex", self.child, "p1")])
        [back] = it.read_turns([own["previous"]])["turns"]
        self.assertEqual((back["ref"], back["position"], back["previous"]), (own["previous"], 1, None))

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
        found = it.capture("codex", self.child, str(child), "forks")
        self.assertEqual((found["captured_rounds"], found["inherited_rounds"]), (2, 2))

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
        found = it.capture("codex", self.child, str(child), "forks")
        self.assertEqual((found["ok"], found["captured_rounds"], found["inherited_rounds"]), (True, 1, 0))
        self.assertIn("fork 부모의 선언된 history 범위에 미완료 꼬리가 있다", self.guidance())

    def test_parent_tracked_in_another_scope_keeps_its_turns(self):
        parent, child = self.fork()
        first = it.capture("codex", self.parent, str(parent), "other")
        self.assertTrue(first["ok"], first)
        captured = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(captured["ok"], captured)
        # The parent reviews its turn in its own scope; the child does not take it over.
        self.assertEqual((captured["captured_rounds"], captured["inherited_rounds"]), (1, 1))
        self.assertEqual(it.status("codex", self.parent)["pending_refs"], [raw.native_ref("codex", self.parent, "p1")])
        # Evidence the child needs from that turn still names the parent.
        cited = it.cite("codex/" + self.child, quote="question p1")
        _, header = raw.find_cited(core.ROOT / cited["path"], raw.native_ref("codex", self.parent, "p1"))
        self.assertEqual(header["conversation"], self.parent)
        self.assertTrue(cited["path"].startswith("00_Scope/Forks/"), cited)

    def test_parent_cursor_with_stored_and_native_turns_is_left_alone(self):
        parent = self.page(self.parent, self.parent, turn(self.parent, "p1") + turn(self.parent, "p2"))
        it.capture("codex", self.parent, str(parent), "forks")
        # An upgraded parent keeps its first turn as a stored `_raw/` round.
        path = it.state_path("codex", self.parent)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["rounds"][0]["ref"] = "00_Scope/Forks/_raw/.records/codex-legacy.txt#1"
        path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        before = path.read_bytes()
        child = self.page(self.child, self.child, turn(self.child, "c1"),
                          base=(self.parent, parent.stat().st_size), fork=self.parent)
        found = it.capture("codex", self.child, str(child), "forks")
        self.assertEqual((found["ok"], found["captured_rounds"], found["inherited_rounds"]), (True, 1, 2))
        self.assertEqual(path.read_bytes(), before)

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
        self.assertIn("다른 대화에서 전달된 메시지", self.guidance())
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

    def test_delivered_message_keeps_existing_input_and_resumable_turns_unchanged(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                sid = self.child + ("-resume" if resume else "-input")
                delivered = event("response_item", type="function_call_output", namespace="codex_app",
                                  name="ordinary_fixture", output="delivered task",
                                  internal_chat_message_metadata_passthrough={"turn_id": "c1"})
                rows = turn(sid, "c1")
                if resume:
                    prior = turn(sid, "prior")
                    prior[-1] = event("event_msg", type="turn_aborted", turn_id="prior")
                    rows[1] = delivered
                    rows = prior + rows
                else:
                    rows.insert(2, delivered)
                path = self.page(sid, sid, rows)
                first = it.capture("codex", sid, str(path), "forks")
                self.assertTrue(first["ok"], first)
                # The tracked hash holds: a turn with input or resumable context reads as before.
                delivered["payload"]["name"] = "send_message_to_thread"
                self.page(sid, sid, rows)
                repeated = it.capture("codex", sid, None, "forks")
                self.assertTrue(repeated["ok"], repeated)
                self.assertEqual((repeated["appended"], repeated["changed"]), (0, 0))
                self.assertEqual(repeated["through"], first["through"])

    def test_changed_parent_prefix_is_refused_without_tracking(self):
        parent, child = self.fork()
        found = it.capture("codex", self.child, str(child), "forks")
        self.assertTrue(found["ok"], found)
        # A same-length edit inside the declared parent range keeps the byte bound.
        parent.write_bytes(parent.read_bytes().replace(b"answer p1", b"ANSWER p1"))
        refused = it.capture("codex", self.child, None, "forks")
        self.assertFalse(refused["ok"], refused)
        self.assertIn("inherited native prefix changed", refused["capture_error"])
        self.assertEqual((refused["captured_rounds"], refused["inherited_rounds"]), (1, 1))

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
