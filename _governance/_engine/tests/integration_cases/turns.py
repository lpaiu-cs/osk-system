"""대화 포착과 검토의 수트의 한 갈래: 전사를 턴으로 읽기 — 완료·실패·끊김의 경계.

`tests/test_integration.py`의 `IntegrationTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from integration_cases.support import (
    claude_round, claude_row, codex_round, codex_user_item, queued, stamped, TMP, VAULT
)
from osk import (integration as it, raw, transcripts, write, conversation_capture, conversation_cite,  # noqa: E402
                 conversation_review, conversation_state)  # noqa: E402


class TurnCases:
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
        self.assertFalse(raw.record_path(VAULT, "Capture", st["record"]).exists())  # tracking copies no dialogue
        [turn] = self.turns()
        text = turn["user"] + "\n" + turn["agent"]
        self.assertNotIn("evidence result", text)
        self.assertIn("tool_evidence_ref", text)
        self.assertIn('"calls": 1', text)
        self.assertIn("answer 1", text)
        self.assertNotIn("question 2", text)


    def test_a_record_split_by_raw_newlines_is_rejoined(self):
        # Copying a parent's history, Claude Code wrote each U+2028 in a string as a raw line
        # end: one record over three lines (#243). The breaks read back as U+2028 (#252).
        rows = claude_round(self.sid, 1) + claude_round(self.sid, 2)
        rows[4]["message"]["content"][0]["text"] = "answer  one"
        self.transcript(rows)
        intact = transcripts.read(str(self.path), "claude", self.sid)
        split = self.path.read_bytes().replace(b"answer\\u2028\\u2028one", b"answer\n\none")
        self.assertNotEqual(split, self.path.read_bytes())
        self.path.write_bytes(split)
        parsed = transcripts.read(str(self.path), "claude", self.sid)
        self.assertEqual([(r["id"], r["user"], r["agent"]) for r in parsed["rounds"]],
                         [(r["id"], r["user"], r["agent"]) for r in intact["rounds"]])
        self.assertEqual(parsed["diagnostics"], [])
        self.assertFalse(parsed["pending_tail"])
        self.assertEqual(parsed["coverage"]["rejoined_lines"], [[5, 7]])
        # A break between tokens was never a U+2028: it reads as the raw newline it is.
        lines = split.splitlines(keepends=True)
        self.path.write_bytes(lines[0].replace(b'", ', b'",\n ', 1) + b"".join(lines[1:]))
        parsed = transcripts.read(str(self.path), "claude", self.sid)
        self.assertEqual([(r["id"], r["user"], r["agent"]) for r in parsed["rounds"]],
                         [(r["id"], r["user"], r["agent"]) for r in intact["rounds"]])
        self.assertEqual(parsed["coverage"]["rejoined_lines"], [[1, 2], [6, 8]])
        # A complete line no following lines complete still stops the read.
        self.path.write_bytes(b"".join(lines[:7] + [b'{"type": "user", "sess\n'] + lines[7:]))
        with self.assertRaisesRegex(ValueError, "unreadable transcript record at line 8"):
            transcripts.read(str(self.path), "claude", self.sid)

    def test_end_turn_thinking_is_not_a_final_response(self):
        self.transcript(claude_round(self.sid, 1)[:-1])
        parsed = transcripts.read(str(self.path), "claude", self.sid)
        self.assertEqual(parsed["rounds"], [])
        self.assertTrue(parsed["pending_tail"])

    def test_idle_claude_turn_closes_as_interrupted_and_keeps_its_boundary(self):
        # Claude records nothing that ends an abandoned turn (2026-10-07 decision B): after
        # 12 hours without a row it closes as interrupted at its last row, and the stored cut
        # keeps that boundary when the conversation goes on, whichever way it goes on.
        base = time.time() - 13 * 3600
        first = stamped(claude_round(self.sid, 1) + claude_round(self.sid, 2, finished=False), base)
        self.transcript(first)
        st = self.capture()
        self.assertTrue(st["ok"], st)
        self.assertFalse(st["capture_pending"], st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual([r["completion"] for r in s["rounds"]], ["completed", "interrupted"])
        self.assertEqual(s["rounds"][1]["cut"], "result-2")
        closed = s["rounds"][1]
        turn = self.turns(rules=conversation_capture.turn_rules(s))[1]
        self.assertIn("question 2", turn["user"])
        # the same turn goes on (its row stamped before the close: only the cut keeps the
        # boundary), then a new prompt follows
        more = (stamped([claude_row(self.sid, "assistant", [{"type": "text", "text": "late answer 2"}],
                                    "late-2", "end_turn", "final-late-2")], base + 60)
                + stamped(claude_round(self.sid, 3), time.time()))
        self.transcript(first + more)
        st = self.capture()
        self.assertTrue(st["ok"], st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual(s["rounds"][1], closed, "a later row moved the closed boundary")
        self.assertEqual([r["completion"] for r in s["rounds"]],
                         ["completed", "interrupted", "completed", "completed"])
        parsed = transcripts.read(str(self.path), "claude", self.sid, rules=conversation_capture.turn_rules(s))
        self.assertEqual(parsed["rounds"][2]["continued_from"], closed["id"])
        self.assertIn("late answer 2", parsed["rounds"][2]["agent"])
        self.assertIn("question 3", parsed["rounds"][3]["user"])
        # the closed turn still acknowledges against the transcript
        self.assertEqual(len(conversation_review._verify_native(VAULT, s, s["rounds"])), 4)

    def test_claude_turn_rules_start_after_the_turns_already_tracked(self):
        # A conversation tracked before this engine keeps its boundaries: there an unfinished
        # turn and the prompt that followed it a day later are one round.
        old = time.time() - 40 * 3600
        merged = (stamped(claude_round(self.sid, 1, finished=False)
                          + queued(self.sid, "q-1", "while you work, note this"), old)
                  + stamped(claude_round(self.sid, 2), old + 13 * 3600))
        self.transcript(merged)
        with mock.patch.object(conversation_capture, "turn_rules", return_value=None):
            self.assertTrue(self.capture()["ok"])
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        before = list(s["rounds"])
        self.assertEqual([r["id"] for r in before], ["user-1:final-2"])
        self.assertNotIn("turns_since", s)
        # after the update: the stored round reads as it was; new turns follow the new rules
        later = stamped(claude_round(self.sid, 3, finished=False), old + 20 * 3600) + stamped(
            claude_round(self.sid, 4)[:1] + queued(self.sid, "q-4", "and also this")
            + queued(self.sid, "n-4", "a background task ended", kind="task-notification")
            + queued(self.sid, "d-4", "sent as its own prompt", reason="dequeued")
            + claude_round(self.sid, 4)[1:], old + 34 * 3600)
        self.transcript(merged + later)
        st = self.capture()
        self.assertTrue(st["ok"], st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual(s["turns_since"], "user-1:final-2")
        self.assertEqual(s["rounds"][0], before[0], "the tracked round changed under the new rules")
        self.assertEqual([r["completion"] for r in s["rounds"]], ["completed", "interrupted", "completed"])
        turns = self.turns(rules=conversation_capture.turn_rules(s))
        self.assertNotIn("while you work", turns[0]["user"])
        self.assertIn("and also this", turns[2]["user"])
        self.assertNotIn("background task", turns[2]["user"])
        self.assertNotIn("its own prompt", turns[2]["user"])

    def test_claude_prompt_after_a_tool_ended_turn_opens_its_own_round(self):
        # #218: a turn that ends in a tool call (the app's next-step box, ScheduleWakeup) has
        # no end_turn. The next prompt (another promptId) closes it at its last row, so the
        # words seen while it was open stay its words; an interruption keeps its promptId.
        prompted = lambda rows, pid: [{**r, "promptId": pid} for r in rows]
        one = prompted(claude_round(self.sid, 1, finished=False), "p1")
        self.transcript(one)
        self.assertTrue(self.capture()["ok"])
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual(s["rounds"], [])
        tail = transcripts.read(str(self.path), "claude", self.sid, rules=conversation_capture.turn_rules(s))["tail"]
        stop = claude_row(self.sid, "user", [{"type": "text", "text": "[Request interrupted by user for tool use]"}], "stop-2")
        later = (prompted(claude_round(self.sid, 2, finished=False) + [stop], "p2")
                 + prompted(claude_round(self.sid, 3), "p3"))
        self.transcript(one + later)
        st = self.capture()
        self.assertTrue(st["ok"], st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual([(r["id"], r["completion"]) for r in s["rounds"]],
                         [("user-1:result-1", "completed"), ("user-2:stop-2", "interrupted"),
                          ("user-3:final-3", "completed")])
        rounds = transcripts.read(str(self.path), "claude", self.sid, rules=conversation_capture.turn_rules(s))["rounds"]
        self.assertEqual(rounds[0]["user"], tail["user"], "the next prompt joined the closed turn")
        self.assertIn("[Request interrupted", rounds[1]["user"])
        self.assertIn("question 3", rounds[2]["user"])

    def test_claude_prompt_rule_starts_after_the_turns_already_tracked(self):
        # #218: a conversation tracked before the rule keeps its boundaries — there a turn
        # that ended in a tool call and the prompt after it are one round.
        prompted = lambda rows, pid: [{**r, "promptId": pid} for r in rows]
        merged = prompted(claude_round(self.sid, 1, finished=False), "p1") + prompted(claude_round(self.sid, 2), "p2")
        self.transcript(merged)
        with mock.patch.object(conversation_capture, "turn_rules", return_value=None):
            self.assertTrue(self.capture()["ok"])
        before = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)["rounds"]
        self.assertEqual([r["id"] for r in before], ["user-1:final-2"])
        self.transcript(merged + prompted(claude_round(self.sid, 3, finished=False), "p3")
                        + prompted(claude_round(self.sid, 4), "p4"))
        st = self.capture()
        self.assertTrue(st["ok"], st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual(s["prompts_since"], "user-1:final-2")
        self.assertEqual(s["rounds"][0], before[0], "the tracked round changed under the new rule")
        self.assertEqual([r["id"] for r in s["rounds"]], ["user-1:final-2", "user-3:result-3", "user-4:final-4"])

    def test_claude_notice_sent_as_a_prompt_is_not_the_users_turn(self):
        # #275: the notice neither closes the open turn nor joins its words. Work it starts with
        # no turn open is a round without the user's words, and cite's count skips that round.
        prompted = lambda rows, pid: [{**r, "promptId": pid} for r in rows]
        rows = (prompted(claude_round(self.sid, 1, finished=False), "p1") + [self.notice("n-1", "pn1")]
                + prompted(claude_round(self.sid, 1)[3:], "p1") + [self.notice("n-2", "pn2")]
                + [claude_row(self.sid, "assistant", [{"type": "text", "text": "the task ended"}],
                              "after-2", "end_turn", "final-n2")]
                + prompted(claude_round(self.sid, 3), "p3"))
        self.transcript(rows)
        st = self.capture()
        self.assertTrue(st["ok"], st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertIsNone(s["harness_since"])
        self.assertEqual([r["id"] for r in s["rounds"]], ["user-1:final-1", "n-2:final-n2", "user-3:final-3"])
        turns = self.turns(rules=conversation_capture.turn_rules(s))
        self.assertNotIn("task-notification", turns[0]["user"])
        self.assertIn("question 1", turns[0]["user"])
        self.assertEqual(turns[1]["user"], "")
        self.assertIn("the task ended", turns[1]["agent"])
        self.assertEqual(conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-2")["turn"], "user-1")
        self.assertEqual(conversation_cite.cite(VAULT, f"claude/{self.sid}")["turn"], "user-3")

    def test_claude_notice_rule_starts_after_the_turns_already_tracked(self):
        # #275: rounds an engine without the rule tracked keep their boundaries and words.
        prompted = lambda rows, pid: [{**r, "promptId": pid} for r in rows]
        old = (prompted(claude_round(self.sid, 1, finished=False), "p1") + [self.notice("n-1", "pn1")]
               + prompted(claude_round(self.sid, 1)[3:], "p1"))
        self.transcript(old)
        legacy = {"since": None, "prompts_since": None}   # turn and prompt rules, no notice rule
        with mock.patch.object(conversation_capture, "turn_rules", return_value={**legacy, "cuts": [], "now": time.time()}), \
                mock.patch.object(conversation_capture, "_copied_since", return_value=legacy):
            self.assertTrue(self.capture()["ok"])
        before = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)["rounds"]
        self.assertEqual([r["id"] for r in before], ["user-1:result-1", "n-1:final-1"])
        self.transcript(old + prompted(claude_round(self.sid, 2, finished=False), "p2")
                        + [self.notice("n-2", "pn2")] + prompted(claude_round(self.sid, 2)[3:], "p2"))
        st = self.capture()
        self.assertTrue(st["ok"], st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual(s["harness_since"], "n-1:final-1")
        self.assertEqual(s["rounds"][:2], before, "the tracked rounds changed under the new rule")
        self.assertEqual([r["id"] for r in s["rounds"][2:]], ["user-2:final-2"])

    def test_recent_claude_tail_stays_open_until_the_conversation_ends(self):
        self.transcript(stamped(claude_round(self.sid, 1, finished=False), time.time() - 3600))
        ago = time.time() - 13 * 3600
        os.utime(self.path, (ago, ago))   # the file is not touched again: its fingerprint holds
        st = self.capture()
        self.assertTrue(st["capture_pending"])
        self.assertEqual(st["captured_rounds"], 0)
        # twelve hours on, the scheduled catch-up reads the unchanged file once more and closes it
        with mock.patch.object(time, "time", return_value=time.time() + 12 * 3600):
            out = it.catchup(VAULT, limit=100)
        self.assertIn(self.sid, [j["conversation_id"] for j in out["jobs"]], out)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual([r["completion"] for r in s["rounds"]], ["interrupted"])
        self.assertFalse(s["capture_pending"])

    def test_absorbed_message_moves_the_idle_clock_and_its_cut_keeps_the_words(self):
        # #164 review: the message is the turn's latest activity, and a cut stored at it
        # closes the turn after its words on every later read.
        base = time.time() - 23 * 3600 - 120
        self.transcript(stamped(claude_round(self.sid, 1, finished=False), base)
                        + stamped(queued(self.sid, "q-1", "one more thing"), base + 11 * 3600))
        with mock.patch.object(time, "time", return_value=base + 12 * 3600 + 60):
            st = self.capture()
        self.assertTrue(st["capture_pending"], "the idle clock ignored the message")
        st = self.capture()
        self.assertFalse(st["capture_pending"], st)
        s = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, "claude", self.sid), "claude", self.sid)
        self.assertEqual(s["rounds"][0]["cut"], "q-1")
        self.assertIn("one more thing", self.turns(rules=conversation_capture.turn_rules(s))[0]["user"])
        self.assertEqual(len(conversation_review._verify_native(VAULT, s, s["rounds"])), 1)

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
        raw.append_rounds(VAULT, "capture-tests", self.sid + "-manual", [parsed["rounds"][-1]])
        with self.assertRaises(write.WriteError):
            raw.append_rounds(VAULT, "capture-tests", self.sid + "-manual", [parsed["rounds"][-1]])

    def test_codex_paginated_history_keeps_receipts_and_byte_boundary(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME': home}):
            folder = Path(home) / 'sessions/2026/09/21'
            folder.mkdir(parents=True)
            parent = folder / f'rollout-first-{self.sid}.jsonl'
            child = folder / f'rollout-second-{self.sid}_continuation.jsonl'
            header = {'type': 'session_meta', 'payload': {'id': self.sid}}
            parent.write_text(''.join(json.dumps(r) + '\n' for r in [header] + codex_round(1)), encoding='utf-8')
            first = conversation_capture.capture(VAULT, 'codex', self.sid, str(parent), 'capture-tests')
            conversation_review.acknowledge(VAULT, 'codex', self.sid, first['through'], 'no_value', 'synthetic fixture')
            before = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'codex', self.sid), 'codex', self.sid)
            offset = parent.stat().st_size
            # The declared history excludes later events in the ancestor file.
            with parent.open('a', encoding='utf-8') as f:
                f.write(''.join(json.dumps(r) + '\n' for r in codex_round(99)))
            child_header = {'type': 'session_meta', 'payload': {'id': self.sid,
                'history_base': {'thread_id': self.sid, 'end_byte_offset': offset}}}
            child.write_text(''.join(json.dumps(r) + '\n' for r in [child_header] + codex_round(2)), encoding='utf-8')
            child_name = '\\\\?\\' + str(child.resolve()) if os.name == 'nt' else str(child)
            captured = conversation_capture.capture(VAULT, 'codex', self.sid, child_name, 'capture-tests')
            self.assertTrue(captured['ok'], captured)
            self.assertEqual((captured['appended'], captured['captured_rounds'], captured['reviewed_rounds']), (1, 2, 1))
            after = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'codex', self.sid), 'codex', self.sid)
            self.assertEqual(after['rounds'][:1], before['rounds'])
            self.assertEqual(after['reviews'], before['reviews'])
            self.assertEqual(after['snapshots'][first['through']], before['snapshots'][first['through']])
            self.assertEqual([r['id'] for r in after['rounds']], ['turn-1', 'turn-2'])
            self.assertNotIn('question 99', json.dumps(transcripts.read(str(child), 'codex', self.sid)['dialogue_v1']))
            self.assertFalse(raw.record_path(VAULT, 'Capture', captured['record']).exists())
            self.assertEqual(conversation_capture.capture(VAULT, 'codex', self.sid, str(child), 'capture-tests')['appended'], 0)
            # A lost local cursor tracks the same original turns again, under the same refs.
            refs = [r['ref'] for r in after['rounds']]
            conversation_state.state_path(VAULT, 'codex', self.sid).unlink()
            rebuilt = conversation_capture.capture(VAULT, 'codex', self.sid, str(child), 'capture-tests')
            self.assertEqual((rebuilt['appended'], rebuilt['pending_refs']), (2, refs))

    def test_codex_missing_ancestor_keeps_tracked_turns_pending(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME':home}):
            parent = Path(home) / f'rollout-parent-{self.sid}.jsonl'
            child = Path(home) / f'rollout-child-{self.sid}_next.jsonl'
            header = {'type':'session_meta','payload':{'id':self.sid}}
            parent.write_text(''.join(json.dumps(r)+'\n' for r in [header] + codex_round(1)), encoding='utf-8')
            header['payload']['history_base'] = {'thread_id':self.sid,'end_byte_offset':parent.stat().st_size}
            child.write_text(''.join(json.dumps(r)+'\n' for r in [header] + codex_round(2)), encoding='utf-8')
            first = conversation_capture.capture(VAULT, 'codex', self.sid, str(child), 'capture-tests')
            self.assertTrue(first['ok'], first)
            parent.unlink()
            listed = it.list_pending(VAULT, 100)
            self.assertFalse(listed['ok'])
            self.assertTrue(any('history_base' in e['error'] for e in listed['errors']))
            # An offline worker has no copy of these turns; they wait for their own conversation.
            self.assertNotIn(self.sid, [j['conversation_id'] for j in listed['jobs']])
            caught = it.catchup(VAULT, 100)
            capture = next(c for c in caught['captures'] if c['conversation_id'] == self.sid)
            self.assertFalse(capture['ok'])
            self.assertIn('history_base', capture['capture_error'])
            self.assertNotIn(self.sid, [j['conversation_id'] for j in caught['jobs']])
            st = conversation_capture.status(VAULT, 'codex', self.sid)
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
        conversation_review.acknowledge(VAULT, 'codex', self.sid, first['through'], 'no_value', 'fixture turns only')
        old = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'codex', self.sid), 'codex', self.sid)
        captured = self.capture('codex')
        self.assertTrue(captured['ok'], captured)
        self.assertEqual((captured['appended'], captured['reviewed_rounds']), (1, 2))
        state = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'codex', self.sid), 'codex', self.sid)
        self.assertEqual(state['rounds'][:2], old['rounds'])
        self.assertEqual(state['reviews'], old['reviews'])
        self.assertEqual([r['id'] for r in state['rounds']], ['turn-1', 'turn-3', 'turn-2'])
        self.assertEqual(captured['pending_refs'], [raw.native_ref('codex', self.sid, 'turn-2')])
        # Without the local cursor, the turns are tracked again in transcript order.
        conversation_state.state_path(VAULT, 'codex', self.sid).unlink()
        rebuilt = self.capture('codex')
        self.assertEqual((rebuilt['ok'], rebuilt['appended']), (True, 3))

    def test_open_kiro_and_antigravity_turns_are_cited_under_their_final_ids(self):
        step = lambda i, typ, text: {"step_index": i, "source": "x", "type": typ, "status": "DONE", "content": text}
        steps = [step(0, "USER_INPUT", "<USER_REQUEST>\nfirst\n</USER_REQUEST>"), step(1, "PLANNER_RESPONSE", "answered"),
                 step(2, "USER_INPUT", "<USER_REQUEST>\nthe current request\n</USER_REQUEST>")]
        ag = self.antigravity(self.sid, steps)
        conversation_capture.capture(VAULT, "antigravity", self.sid, str(ag), self.sid, "00_Scope/W1")
        self.assertEqual(conversation_cite.cite(VAULT, f"antigravity/{self.sid}", turn="-1")["turn"], "2")
        self.assertEqual(conversation_cite.cite(VAULT, f"antigravity/{self.sid}", quote="current request")["turn"], "2")
        self.antigravity(self.sid, steps + [step(3, "PLANNER_RESPONSE", "answered now")])
        done = conversation_capture.capture(VAULT, "antigravity", self.sid, str(ag), self.sid, "00_Scope/W1")
        self.assertIn(f"native:antigravity:{self.sid}:2", done["pending_refs"])
        cid = self.sid + "-kiro"
        kiro = Path(TMP.name) / cid / "messages.jsonl"  # Kiro's folder is the conversation's
        kiro.parent.mkdir(parents=True, exist_ok=True)
        row = lambda n, payload: json.dumps({"id": f"r{n}", "timestamp": n, "payload": payload}) + "\n"
        open_turn = [row(1, {"type": "user", "content": "the current request"}),
                     row(2, {"type": "turn_start", "executionId": "exec-1"})]
        kiro.write_text("".join(open_turn), encoding="utf-8")
        conversation_capture.capture(VAULT, "kiro", cid, str(kiro), self.sid, "00_Scope/W1")
        self.assertEqual(conversation_cite.cite(VAULT, f"kiro/{cid}", turn="-1")["turn"], "exec-1")
        kiro.write_text("".join(open_turn + [
            row(3, {"type": "assistant", "operationType": "Say", "content": "done"}),
            row(4, {"type": "turn_end", "executionId": "exec-1", "stopReason": "end_turn"})]), encoding="utf-8")
        done = conversation_capture.capture(VAULT, "kiro", cid, str(kiro), self.sid, "00_Scope/W1")
        self.assertIn(f"native:kiro:{cid}:exec-1", done["pending_refs"])
