"""대화 포착과 검토의 수트의 한 갈래: 복사본과 되감기 — 갈라진 대화가 부모의 턴을 다시 세지 않는다.

`tests/test_integration.py`의 `IntegrationTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import json
import os
import time
import uuid
from pathlib import Path
from unittest import mock

from integration_cases.support import claude_round, claude_row, queued, stamped, TMP, VAULT
from osk import (integration as it, raw, transcripts, conversation_capture, conversation_review,  # noqa: E402
                 conversation_state)  # noqa: E402


class CopyCases:
    def test_claude_copy_that_wrote_a_line_separator_as_a_line_end_matches_its_parent(self):
        # #252: copying the parent's history, Claude Code wrote a U+2028 in a tool result as a
        # raw line end. The copied round reads as the parent tracked it, so the copy goes on.
        parent, child = self.sid + "-p", self.sid + "-c"
        rows = claude_round(parent, 1) + claude_round(parent, 2)
        rows[2]["message"]["content"][0]["content"] = "evidence result"
        own = claude_round(child, 3)
        for owner, part in ((parent, rows), (child, own)):
            for row in part:
                if "uuid" in row:   # native rows carry UUIDs; a copy keeps them
                    row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, owner + row["uuid"]))
        self.transcript(rows)
        self.assertTrue(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")["ok"])
        self.transcript([dict(r, sessionId=child) for r in rows] + own)
        copied = self.path.read_bytes().replace(b"evidence\\u2028result", b"evidence\nresult")
        self.assertNotEqual(copied, self.path.read_bytes())
        self.path.write_bytes(copied)
        st = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
        self.assertTrue(st["ok"], st)
        self.assertEqual((st["inherited_rounds"], st["captured_rounds"]), (2, 1), st)

    def test_claude_rewind_drops_an_unreviewed_turn_from_tracking(self):
        # #235: a rewind appends `last-prompt` with `rewound` and the kept leaf; the next message
        # hangs on that leaf. The dropped turn leaves the cursor as if it had never been tracked.
        one, two = claude_round(self.sid, 1), claude_round(self.sid, 2)
        self.transcript(one + two)
        first = self.capture()
        self.assertEqual(first["captured_rounds"], 2, first)
        self.transcript(one + two + [self.rewind("answer-1")] + claude_round(self.sid, 3))
        found = self.capture()
        self.assertTrue(found["ok"], found)
        self.assertEqual((found["captured_rounds"], found["appended"], found["reverted"]), (2, 1, 1), found)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual([r["id"] for r in s["rounds"]], ["user-1:final-1", "user-3:final-3"])
        parsed = transcripts.read(str(self.path), "claude", self.sid, rules=conversation_capture.turn_rules(s))
        self.assertEqual(parsed["reverted"], ["user-2:final-2"])
        self.assertNotIn("question 2", " ".join(t["user"] for t in self.turns(rules=conversation_capture.turn_rules(s))))
        self.assertEqual(self.capture()["appended"], 0)

    def test_claude_rewind_past_a_reviewed_turn_is_refused_by_name(self):
        one, two = claude_round(self.sid, 1), claude_round(self.sid, 2)
        self.transcript(one + two)
        first = self.capture()
        conversation_review.acknowledge(VAULT, "claude", self.sid, first["through"], "no_value", "synthetic fixture")
        before = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)["rounds"]
        self.transcript(one + two + [self.rewind("answer-1")] + claude_round(self.sid, 3))
        found = self.capture()
        self.assertFalse(found["ok"])
        self.assertIn("Claude rewound past a reviewed turn", found["capture_error"])
        self.assertEqual(conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)["rounds"], before)

    def test_claude_rewind_into_a_copy_drops_the_turns_it_left_behind(self):
        # #235: the app's rewind cuts a copy right before the message it rewinds to and goes on
        # there. Once the original is quiet, its turns no copy took and that came before the
        # copy's first turn leave tracking unreviewed — as Codex's reverted turns do (#208).
        at = time.time() - 2 * 86400
        parent, opath, original = self.fork(at, at + 100)
        copies = it._copies_by_parent(VAULT)[("claude", parent)]
        self.assertEqual(it._drop_fork_rewound(VAULT, "claude", parent, copies), 2)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", parent), "claude", parent)
        self.assertEqual(len(s["rounds"]), 1)
        self.assertEqual(len(s["rewound"]["rounds"]), 2)
        self.assertEqual(conversation_capture.state_view(VAULT, s)["pending_refs"], [s["rounds"][0]["ref"]])
        self.assertEqual(it._drop_fork_rewound(VAULT, "claude", parent, copies), 0)
        # A capture of the unchanged original keeps them out ...
        self.assertEqual(conversation_capture.capture(VAULT, "claude", parent, str(opath), "capture-tests")["captured_rounds"], 1)
        # ... until the original goes on: then they come back to review with the new turn.
        opath.write_text("".join(json.dumps(r) + "\n" for r in original + [
            dict(r, uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, parent + r["uuid"])))
            for r in stamped(claude_round(parent, 5), time.time())]), encoding="utf-8")
        st = conversation_capture.capture(VAULT, "claude", parent, str(opath), "capture-tests")
        self.assertEqual(st["captured_rounds"], 4, st)
        self.assertNotIn("rewound", conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", parent), "claude", parent))

    def test_claude_copy_the_original_went_on_from_is_not_a_rewind(self):
        at = time.time() - 2 * 86400
        # the copy's first turn came before the original's last turn: a parallel fork
        parent, _, _ = self.fork(at, at + 6)
        self.assertEqual(it._drop_fork_rewound(VAULT, "claude", parent, it._copies_by_parent(VAULT)[("claude", parent)]), 0)
        # the original went on after the copy's first turn
        parent, _, _ = self.fork(at - 86400, at - 86400 + 100, went_on=True)
        self.assertEqual(it._drop_fork_rewound(VAULT, "claude", parent, it._copies_by_parent(VAULT)[("claude", parent)]), 0)

    def test_claude_copy_split_at_a_turn_boundary_is_not_a_rewind(self):
        # The copy's first turn came after the original's last and the original went quiet, but
        # the copy holds no trace of a message it was cut before: a plain fork from an earlier
        # turn. Time alone cannot tell it from a rewind, so the original keeps its turns.
        at = time.time() - 2 * 86400
        parent, _, _ = self.fork(at, at + 100, cut=False)
        self.assertEqual(it._drop_fork_rewound(VAULT, "claude", parent, it._copies_by_parent(VAULT)[("claude", parent)]), 0)
        self.assertEqual(len(conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", parent), "claude", parent)["rounds"]), 3)

    def test_codex_fork_is_not_a_rewind(self):
        # Codex leaves a rewind in the same thread (`thread/revert`, #208). A fork carries no trace
        # of one, so even a fork whose first turn came after the original's last keeps them.
        quiet = Path(TMP.name) / f"{self.sid}.jsonl"
        quiet.write_text("{}\n", encoding="utf-8")
        old = time.time() - 13 * 3600
        os.utime(quiet, (old, old))
        tid = lambda ms: f"{ms:012x}"[:8] + "-" + f"{ms:012x}"[8:] + "-7000-8000-000000000000"
        base = int((time.time() - 86400) * 1000)
        turns = [tid(base + i * 1000) for i in range(3)]
        parent = {"harness": "codex", "conversation_id": "p", "reviewed_count": 0, "transcript_path": str(quiet),
                  "rounds": [{"id": t, "ref": raw.native_ref("codex", "p", t)} for t in turns]}
        copy = lambda first: ({"harness": "codex", "conversation_id": "c", "transcript_path": None,
                               "rounds": [{"id": first, "ref": raw.native_ref("codex", "c", first)}]}, turns[0])
        self.assertIsNone(it._fork_rewound(VAULT, parent, [copy(tid(base + 5000))]))

    def test_claude_rewind_rule_starts_after_the_turns_already_tracked(self):
        # A turn an engine without the rule tracked stays tracked though a later rewind dropped it.
        one, two = claude_round(self.sid, 1), claude_round(self.sid, 2)
        self.transcript(one + two)
        legacy = {"since": None, "prompts_since": None}
        with mock.patch.object(conversation_capture, "turn_rules", return_value={**legacy, "cuts": [], "now": time.time()}), \
                mock.patch.object(conversation_capture, "_copied_since", return_value=legacy):
            self.assertTrue(self.capture()["ok"])
        self.transcript(one + two + [self.rewind("answer-1")] + claude_round(self.sid, 3))
        found = self.capture()
        self.assertTrue(found["ok"], found)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual(s["harness_since"], "user-2:final-2")
        self.assertEqual([r["id"] for r in s["rounds"]], ["user-1:final-1", "user-2:final-2", "user-3:final-3"])

    def test_claude_copy_reads_its_copied_prefix_with_its_parents_rules(self):
        # #164 review: a resume or fork copy must not re-cut the turns its parent tracked
        # before this engine; the copy's own new turns follow the new rules.
        parent, child = self.sid + "-parent", self.sid + "-child"
        old = time.time() - 60 * 3600
        rows = (stamped(claude_round(parent, 1, finished=False) + queued(parent, "q-p", "noted while it ran"), old)
                + stamped(claude_round(parent, 2), old + 13 * 3600))
        own = stamped(claude_round(child, 3, finished=False), old + 30 * 3600) + stamped(claude_round(child, 4), old + 44 * 3600)
        for owner, part in ((parent, rows), (child, own)):
            for row in part:
                if "uuid" in row:   # native rows carry UUIDs; a copy keeps them
                    row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, owner + row["uuid"]))
        self.transcript(rows)
        with mock.patch.object(conversation_capture, "turn_rules", return_value=None):
            self.assertTrue(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")["ok"])
        tracked = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", parent), "claude", parent)["rounds"]
        self.assertEqual(len(tracked), 1, "the parent's legacy round joins the turn and the next prompt")
        self.transcript([dict(r, sessionId=child) for r in rows] + own)
        st = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["inherited_rounds"], 1, st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", child), "claude", child)
        self.assertEqual(s["turns_since"], tracked[0]["id"])
        self.assertEqual(s["prompts_since"], tracked[0]["id"])
        self.assertEqual([r["completion"] for r in s["rounds"]], ["interrupted", "completed"])

    def test_claude_copy_finds_its_parents_prompt_boundary_past_an_idle_close(self):
        # #233 review: the parent tracked its turns with the turn rules but before the prompt
        # rule, and its last round follows an idle close. A read without rules joins that round
        # to the idle turn: the copy looks for the boundary with the turn rules.
        prompted = lambda rows, pid: [{**r, "promptId": pid} for r in rows]
        parent, child = self.sid + "-pp", self.sid + "-pc"
        old = time.time() - 60 * 3600
        rows = (stamped(prompted(claude_round(parent, 1, finished=False), "p1")
                        + prompted(claude_round(parent, 2), "p2")
                        + prompted(claude_round(parent, 3, finished=False), "p3"), old)
                + stamped(prompted(claude_round(parent, 4), "p4"), old + 13 * 3600))
        own = stamped(prompted(claude_round(child, 5), "p5"), old + 30 * 3600)
        for owner, part in ((parent, rows), (child, own)):
            for row in part:
                if "uuid" in row:   # native rows carry UUIDs; a copy keeps them
                    row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, owner + row["uuid"]))
        self.transcript(rows)
        legacy = {"since": None, "prompts_since": "never"}   # turn rules, no prompt rule
        with mock.patch.object(conversation_capture, "turn_rules", return_value={**legacy, "cuts": [], "now": time.time()}), \
                mock.patch.object(conversation_capture, "_copied_since", return_value=legacy):
            self.assertTrue(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")["ok"])
        with conversation_state.state_lock(VAULT, "claude", parent) as p:     # that engine wrote no prompts_since
            s = conversation_state.load_state(VAULT, p, "claude", parent)
            del s["prompts_since"]
            conversation_state.save_state(p, s)
        tracked = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", parent), "claude", parent)["rounds"]
        self.assertEqual([r["completion"] for r in tracked], ["completed", "interrupted", "completed"])
        self.transcript([dict(r, sessionId=child) for r in rows] + own)
        st = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["inherited_rounds"], 3, st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", child), "claude", child)
        self.assertEqual(s["prompts_since"], tracked[-1]["id"])

    def test_claude_copy_keeps_its_parents_prompt_boundary_when_the_parent_went_on(self):
        # The parent tracked before the prompt rule and went on after the copy forked: its
        # boundary, its last round, is not in the copy. The copied rounds all come before it, so
        # the copy keeps the tool-ended turn joined to the next prompt — and, as the parent used
        # the turn rules from the start, the queued words in it.
        prompted = lambda rows, pid: [{**r, "promptId": pid} for r in rows]
        parent, child = self.sid + "-pp", self.sid + "-pc"
        rows = (prompted(claude_round(parent, 1, finished=False), "p1") + queued(parent, "q-1", "noted while it ran")
                + prompted(claude_round(parent, 2), "p2"))
        went_on, own = prompted(claude_round(parent, 3), "p3"), prompted(claude_round(child, 5), "p5")
        for owner, part in ((parent, rows + went_on), (child, own)):
            for row in part:
                if "uuid" in row:   # native rows carry UUIDs; a copy keeps them
                    row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, owner + row["uuid"]))
        self.transcript(rows + went_on)
        legacy = {"since": None, "prompts_since": "never"}   # turn rules, no prompt rule
        with mock.patch.object(conversation_capture, "turn_rules", return_value={**legacy, "cuts": [], "now": time.time()}), \
                mock.patch.object(conversation_capture, "_copied_since", return_value=legacy):
            self.assertTrue(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")["ok"])
        with conversation_state.state_lock(VAULT, "claude", parent) as p:     # that engine wrote no prompts_since
            s = conversation_state.load_state(VAULT, p, "claude", parent)
            del s["prompts_since"]
            conversation_state.save_state(p, s)
        tracked = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", parent), "claude", parent)["rounds"]
        self.assertEqual(len(tracked), 2, "the parent's tool-ended turn joins the next prompt")
        self.transcript([dict(r, sessionId=child) for r in rows] + own)
        st = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
        self.assertTrue(st["ok"], st)
        self.assertEqual((st["inherited_rounds"], st["captured_rounds"]), (1, 1), st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", child), "claude", child)
        self.assertEqual((s["turns_since"], s["prompts_since"]), (None, tracked[0]["id"]))

    def test_claude_copy_keeps_its_parents_turn_boundary_when_the_parent_went_on(self):
        # The parent tracked before the turn rules and went on after the copy forked. Read with
        # the turn rules, the copied turn would take the queued words under the same ID, and the
        # copy's capture would stop on the changed content.
        parent, child = self.sid + "-tp", self.sid + "-tc"
        turn = claude_round(parent, 1)
        rows = turn[:3] + queued(parent, "q-1", "noted while it ran") + turn[3:]
        went_on, own = claude_round(parent, 2), claude_round(child, 3)
        for owner, part in ((parent, rows + went_on), (child, own)):
            for row in part:
                if "uuid" in row:
                    row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, owner + row["uuid"]))
        self.transcript(rows + went_on)
        with mock.patch.object(conversation_capture, "turn_rules", return_value=None):
            self.assertTrue(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")["ok"])
        tracked = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", parent), "claude", parent)["rounds"]
        self.transcript([dict(r, sessionId=child) for r in rows] + own)
        st = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
        self.assertTrue(st["ok"], st)
        self.assertEqual((st["inherited_rounds"], st["captured_rounds"]), (1, 1), st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", child), "claude", child)
        self.assertEqual((s["turns_since"], s["prompts_since"]), (tracked[0]["id"], tracked[0]["id"]))

    def test_claude_copy_ignores_a_cut_its_sibling_stored(self):
        # Copies share their ancestor's rows (#164 review). A sibling that stopped at a shared
        # row and closed there does not cut the branch that went on from it, nor its copy.
        shared_owner, sibling, parent, child = (self.sid + s for s in ("-g", "-b", "-a", "-c"))
        base = time.time() - 13 * 3600
        shared = stamped(claude_round(shared_owner, 1, finished=False), base)
        went_on = stamped([claude_row(shared_owner, "assistant", [{"type": "text", "text": "answer 1"}],
                                      "late-1", "end_turn", "final-late-1")], base + 3600)
        own = stamped(claude_round(child, 2), time.time())
        for owner, part in ((shared_owner, shared), (parent, went_on), (child, own)):
            for row in part:
                row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, owner + row["uuid"]))
        self.transcript([dict(r, sessionId=sibling) for r in shared])
        self.assertTrue(conversation_capture.capture(VAULT, "claude", sibling, str(self.path), "capture-tests")["ok"])
        stopped = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", sibling), "claude", sibling)["rounds"]
        self.assertEqual([r["completion"] for r in stopped], ["interrupted"])
        self.transcript([dict(r, sessionId=parent) for r in shared + went_on])
        self.assertTrue(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")["ok"])
        tracked = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", parent), "claude", parent)["rounds"]
        self.assertEqual([r["completion"] for r in tracked], ["completed"], "the sibling's cut split the parent")
        self.transcript([dict(r, sessionId=child) for r in shared + went_on] + own)
        st = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
        self.assertTrue(st["ok"], st)
        self.assertEqual((st["inherited_rounds"], st["captured_rounds"]), (1, 1), st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", child), "claude", child)
        self.assertEqual(s["inherited"]["rounds"][0]["id"], tracked[0]["id"])


    def test_claude_copied_prefix_is_not_reused_across_scopes(self):
        parent = self.sid + "-parent"
        rows = claude_round(parent, 1)
        rows[0]["uuid"] = str(uuid.uuid4())
        self.transcript(rows)
        original = conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")
        self.assertTrue(original["ok"], original)
        self.transcript([dict(r, sessionId=self.sid) for r in rows])
        copied = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
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
        self.assertEqual(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")["captured_rounds"], 1)
        for child, changed in ((self.sid + "-copied", False), (self.sid + "-altered", True)):
            tail = claude_round(child, 2)
            for n, r in enumerate(tail):
                r["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, child + str(n)))
                r["timestamp"] = f"2026-09-27T09:14:{n:02d}.000Z"
            copied = json.loads(json.dumps([dict(r, sessionId=child) for r in (q, c1, c2, r1, r2, end)]))
            if changed:  # Order is forgiven, content is not.
                copied[3]["message"]["content"][0]["content"] = "other evidence"
            self.transcript(copied + tail)
            captured = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
            if changed:
                self.assertFalse(captured["ok"])
                self.assertIn("copied native round ID has different content", captured["capture_error"])
            else:
                self.assertTrue(captured["ok"], captured)
                self.assertEqual((captured["captured_rounds"], captured["inherited_rounds"]), (1, 1))
                self.assertEqual(conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")["appended"], 0)

    def test_claude_copied_prefix_matches_results_written_out_of_time_order(self):
        # Parallel results can reach the original out of their own time order — the one
        # stamped a millisecond earlier written second — and the copy keeps that order
        # among the results while it moves the calls ahead. Sorted by time, the copy
        # matched neither order and its capture stopped for good (a resume after /compact).
        parent = self.sid + "-raced"
        ids = lambda name: str(uuid.uuid5(uuid.NAMESPACE_URL, parent + name))

        def row(role, content, name, ms, stop=None, message_id=None):
            return {"type": role, "sessionId": parent, "uuid": ids(name), "isSidechain": False,
                    "timestamp": f"2026-10-12T18:03:{ms / 1000:06.3f}Z",
                    "message": {"role": role, "content": content, "id": message_id, "stop_reason": stop}}
        call = lambda n: [{"type": "tool_use", "id": f"call-{n}", "name": "probe", "input": {"n": n}}]
        result = lambda n: [{"type": "tool_result", "tool_use_id": f"call-{n}", "content": f"evidence {n}"}]
        q, end = row("user", "question", "q", 1000), row("assistant", [{"type": "text", "text": "answer"}],
                                                          "end", 7000, "end_turn", "m-2")
        c1, c2, c3 = (row("assistant", call(n), f"c{n}", ms, "tool_use", "m-1") for n, ms in ((1, 2000), (2, 4000), (3, 5000)))
        r1, r2, r3 = (row("user", result(n), f"r{n}", ms) for n, ms in ((1, 3000), (2, 6001), (3, 6002)))
        self.transcript([q, c1, r1, c2, c3, r3, r2, end])
        self.assertEqual(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")["captured_rounds"], 1)
        for child, changed in ((self.sid + "-copied", False), (self.sid + "-altered", True)):
            tail = claude_round(child, 2)
            for n, r in enumerate(tail):
                r["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, child + str(n)))
                r["timestamp"] = f"2026-10-12T18:04:{n:02d}.000Z"
            copied = json.loads(json.dumps([dict(r, sessionId=child) for r in (q, c1, c2, c3, r1, r3, r2, end)]))
            if changed:  # Order is forgiven, content is not.
                copied[6]["message"]["content"][0]["content"] = "other evidence"
            self.transcript(copied + tail)
            captured = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
            if changed:
                self.assertFalse(captured["ok"])
                self.assertIn("copied native round ID has different content", captured["capture_error"])
            else:
                self.assertTrue(captured["ok"], captured)
                self.assertEqual((captured["captured_rounds"], captured["inherited_rounds"]), (1, 1))
                self.assertEqual(conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")["appended"], 0)

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

    def test_claude_copied_prefix_is_not_reviewed_again_by_the_child(self):
        parent = self.sid + "-parent"
        rows = claude_round(parent, 1)
        rows[1]["message"]["content"][0]["name"] = "Bash"
        previous = None
        for row in rows:
            row["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, parent + row["uuid"]))
            row["parentUuid"], previous = previous, row["uuid"]
        self.transcript(rows)
        original = conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")
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
            captured = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
            self.assertTrue(captured["ok"], captured)
            self.assertEqual((captured["captured_rounds"], captured["inherited_rounds"]), (1, 1))
            self.assertEqual(captured["reviewed_rounds"], 0)
            self.assertNotIn(original["pending_refs"][0], captured["pending_refs"])
            self.assertIn("과거 1라운드", it.prompt(VAULT, "claude", child)["text"])
            self.assertEqual(conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")["appended"], 0)
            conversation_state.state_path(VAULT, "claude", child).unlink()
            rebuilt = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
            self.assertTrue(rebuilt["ok"], rebuilt)
            self.assertEqual((rebuilt["appended"], rebuilt["inherited_rounds"]), (1, 1))
            changed = json.loads(json.dumps(inherited + tail))
            changed[0]["message"]["content"] = "changed copied question"
            self.transcript(changed)
            refused = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
            self.assertFalse(refused["ok"])
            self.assertIn("inherited native prefix changed", refused["capture_error"])
        self.assertEqual(conversation_capture.status(VAULT, "claude", parent)["reviewed_rounds"], 0)
        # Without both local cursors the copy is the child's own history again.
        conversation_state.state_path(VAULT, "claude", parent).unlink()
        conversation_state.state_path(VAULT, "claude", child).unlink()
        self.transcript(inherited + tail)
        rebuilt = conversation_capture.capture(VAULT, "claude", child, str(self.path), "capture-tests")
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
        self.assertFalse(conversation_state.state_path(VAULT, "claude", grandparent).exists())
        original = conversation_capture.capture(VAULT, "claude", parent, str(self.path), "capture-tests")
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
