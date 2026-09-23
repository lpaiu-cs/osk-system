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
import uuid
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="osk-integration-test-")
ROOT = Path(TMP.name) / "vault"
os.environ["OSK_VAULT_ROOT"] = str(ROOT)
sys.path.insert(0, str(ENGINE))
from osk import core, integration as it, raw, scope_memory, transcripts, validate, write

validate.make_mini_vault(ROOT)
(ROOT / "Scope/Capture").mkdir()
scope_memory.replace("capture-tests", "", space="Scope/Capture")


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


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sid = self._testMethodName
        self.path = Path(TMP.name) / f"{self.sid}.jsonl"

    def transcript(self, rows):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def capture(self, harness="claude"):
        return it.capture(harness, self.sid, str(self.path), "capture-tests")

    def test_codex_paginated_history_preserves_raw_receipts_and_byte_boundary(self):
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
            raw_path = raw._raw_file(raw.parse_ref(first['pending_refs'][0])[0])
            raw_before = raw_path.read_bytes()
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
            self.assertTrue(raw_path.read_bytes().startswith(raw_before))
            self.assertNotIn('question 99', raw_path.read_text(encoding='utf-8'))
            self.assertEqual(it.capture('codex', self.sid, str(child), 'capture-tests')['appended'], 0)
            it.state_path('codex', self.sid).unlink()
            self.assertEqual(it.capture('codex', self.sid, str(child), 'capture-tests')['appended'], 0)
            original = raw_path.read_bytes()
            parent.write_bytes(parent.read_bytes().replace(b'answer 1', b'alterd 1'))
            refused = it.capture('codex', self.sid, str(child), 'capture-tests')
            self.assertFalse(refused['ok'])
            self.assertEqual(raw_path.read_bytes(), original)

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

    def test_codex_history_backfill_preserves_unmarked_v1_and_v2_raw(self):
        for codec in ('codex_v1', 'codex_v2'):
            with self.subTest(codec=codec), tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME':home}):
                sid = self.sid + codec
                parent = Path(home) / f'rollout-parent-{sid}.jsonl'
                child = Path(home) / f'rollout-child-{sid}_next.jsonl'
                header = {'type':'session_meta','payload':{'id':sid}}
                def save(path, rows):
                    path.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
                save(parent, [header] + codex_round(1))
                save(child, [header] + codex_round(2))
                legacy = transcripts.read(str(child), 'codex', sid)
                legacy.pop('dialogue_v1')
                legacy['rounds'] = list(legacy[codec].values())
                legacy.pop('codex_v2')
                if codec == 'codex_v1':
                    legacy.pop('codex_v1')
                with mock.patch.object(transcripts, 'read', return_value=legacy):
                    first = it.capture('codex', sid, str(child), 'capture-tests')
                self.assertTrue(first['ok'], first)
                path = raw._raw_file(raw.parse_ref(first['pending_refs'][0])[0])
                before = path.read_bytes()
                self.assertNotIn(b'dialogue-v1', before)
                self.assertNotIn(b'codex-terminal-v3', before)
                it.acknowledge('codex', sid, first['through'], 'no_value', 'synthetic fixture')
                prior = it._load(it.state_path('codex', sid), 'codex', sid)
                resumed = {'type':'session_meta', 'payload':{'id':sid,'history_base':{
                    'thread_id':sid,'end_byte_offset':parent.stat().st_size}}}
                save(child, [resumed] + codex_round(2) + codex_round(3))
                result = it.capture('codex', sid, str(child), 'capture-tests')
                self.assertTrue(result['ok'], result)
                self.assertEqual((result['appended'], result['reviewed_rounds']), (2, 1))
                after = it._load(it.state_path('codex', sid), 'codex', sid)
                self.assertEqual([r['id'] for r in after['rounds']], ['turn-2','turn-1','turn-3'])
                self.assertEqual(after['rounds'][:1], prior['rounds'])
                self.assertEqual(after['reviews'], prior['reviews'])
                self.assertEqual(after['snapshots'][first['through']], prior['snapshots'][first['through']])
                self.assertTrue(path.read_bytes().startswith(before))
                # Losing the local cursor must not remap the still-unmarked raw.
                it.state_path('codex', sid).unlink()
                self.assertEqual(it.capture('codex', sid, str(child), 'capture-tests')['appended'], 0)
                stable = path.read_bytes()
                child.write_bytes(child.read_bytes().replace(b'answer 2', b'alterd 2'))
                self.assertFalse(it.capture('codex', sid, str(child), 'capture-tests')['ok'])
                self.assertEqual(path.read_bytes(), stable)

    def test_codex_missing_ancestor_keeps_existing_raw_in_review_queue(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME':home}):
            parent = Path(home) / f'rollout-parent-{self.sid}.jsonl'
            child = Path(home) / f'rollout-child-{self.sid}_next.jsonl'
            header = {'type':'session_meta','payload':{'id':self.sid}}
            parent.write_text(''.join(json.dumps(r)+'\n' for r in [header] + codex_round(1)), encoding='utf-8')
            header['payload']['history_base'] = {'thread_id':self.sid,'end_byte_offset':parent.stat().st_size}
            child.write_text(''.join(json.dumps(r)+'\n' for r in [header] + codex_round(2)), encoding='utf-8')
            first = it.capture('codex', self.sid, str(child), 'capture-tests')
            self.assertTrue(first['ok'], first)
            path = raw._raw_file(raw.parse_ref(first['pending_refs'][0])[0])
            before = path.read_bytes()
            parent.unlink()
            listed = it.list_pending(100)
            jobs = [j for j in listed['jobs'] if j['conversation_id'] == self.sid]
            self.assertEqual(len(jobs), 1, listed['errors'])
            self.assertEqual(jobs[0]['pending_refs'], first['pending_refs'])
            self.assertFalse(listed['ok'])
            self.assertTrue(any('history_base' in e['error'] for e in listed['errors']))
            caught = it.catchup(100)
            jobs = [j for j in caught['jobs'] if j['conversation_id'] == self.sid]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]['pending_refs'], first['pending_refs'])
            self.assertIn('history_base', jobs[0]['capture_error'])
            self.assertEqual(jobs[0]['reviewed_rounds'], 0)
            self.assertEqual(path.read_bytes(), before)

    def test_codex_legacy_ambiguous_body_needs_matching_saved_identity(self):
        pair = {'user':'same question', 'agent':'same answer'}
        self.path.write_bytes(raw._block(1, pair['user'], pair['agent']).encode('utf-8'))
        codec = {'first':pair, 'second':pair}
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            raw.codex_capture_order(self.path, codec, codec)
        self.assertEqual(raw.codex_capture_order(self.path, codec, codec, ('second',)), ['second'])
        changed = dict(codec, second=dict(pair, agent='changed answer'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            raw.codex_capture_order(self.path, changed, changed, ('second',))

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

    def test_v1_skipped_native_turn_backfills_without_renumbering(self):
        native = codex_round(2)
        native[3] = codex_user_item(self.sid, 2, 'question 2')
        self.transcript([{'type':'session_meta','payload':{'id':self.sid}}]
                        + codex_round(1) + native + codex_round(3))
        legacy = transcripts.read(str(self.path), 'codex', self.sid)
        legacy.pop('codex_v2')
        legacy['rounds'] = list(legacy.pop('codex_v1').values())
        with mock.patch.object(transcripts, 'read', return_value=legacy):
            first = self.capture('codex')
        self.assertEqual(first['captured_rounds'], 2)
        old = it._load(it.state_path('codex', self.sid), 'codex', self.sid)['rounds']
        captured = self.capture('codex')
        self.assertTrue(captured['ok'], captured)
        self.assertEqual(captured['appended'], 1)
        state = it._load(it.state_path('codex', self.sid), 'codex', self.sid)
        self.assertEqual(state['rounds'][:2], old)
        self.assertEqual([r['id'] for r in state['rounds']], ['turn-1','turn-3','turn-2'])
        it.state_path('codex', self.sid).unlink()
        rebuilt = self.capture('codex')
        self.assertEqual((rebuilt['ok'], rebuilt['appended']), (True, 0))

    def test_terminal_backfill_keeps_old_raw_indices_receipts_and_crash_replay(self):
        header = [{'type':'session_meta','payload':{'id':self.sid}}]
        failed = codex_round(2)
        failed[-1]['payload'].update(last_agent_message=None,error={'message':'capacity'})
        retry = codex_round(3)
        del retry[2:4]
        rows = header + codex_round(1) + failed + retry + codex_round(4)
        self.transcript(rows)
        old = transcripts.read(str(self.path),'codex',self.sid)
        prior_codec = old.pop('codex_v2', {r['id']:r for r in old['rounds']})
        old['rounds'] = list(prior_codec.values())
        with mock.patch.object(transcripts,'read',return_value=old):
            first = self.capture('codex')
        self.assertEqual(first['captured_rounds'], 2)
        path = raw._raw_file(raw.parse_ref(first['pending_refs'][0])[0])
        before = path.read_bytes()
        it.acknowledge('codex',self.sid,first['through'],'no_value','old fixture only')
        old_state = it._load(it.state_path('codex',self.sid),'codex',self.sid)
        captured = self.capture('codex')
        self.assertTrue(captured['ok'], captured)
        self.assertEqual((captured['appended'],captured['reviewed_rounds']),(2,2))
        self.assertTrue(path.read_bytes().startswith(before))
        state = it._load(it.state_path('codex',self.sid),'codex',self.sid)
        self.assertEqual(state['rounds'][:2], old_state['rounds'])
        self.assertEqual(state['reviews'], old_state['reviews'])
        self.assertEqual([r['id'] for r in state['rounds']],['turn-1','turn-4','turn-2','turn-3'])
        self.transcript(rows + codex_round(5))
        self.assertEqual(self.capture('codex')['appended'],1)
        stable = path.read_bytes()
        it.state_path('codex',self.sid).unlink()
        rebuilt = self.capture('codex')
        self.assertTrue(rebuilt['ok'], rebuilt)
        self.assertEqual((rebuilt['captured_rounds'],rebuilt['appended']),(5,0))
        self.assertEqual(path.read_bytes(),stable)
        failed[-1]['payload']['error']['message'] = 'changed error'
        self.transcript(rows + codex_round(5))
        self.assertFalse(self.capture('codex')['ok'])
        self.assertEqual(path.read_bytes(),stable)

    def test_claude_completion_and_tool_results(self):
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2, finished=False))
        st = self.capture()
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["captured_rounds"], 1)
        self.assertTrue(st["capture_pending"])
        text = raw.read_round(st["pending_refs"][0])["text"]
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
        text = raw.read_round(st["pending_refs"][0])["text"]
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
        text = raw.read_round(captured["pending_refs"][0])["text"]
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

    def test_codex_raw_index_previews_skip_only_capture_header(self):
        rows = [{"type": "session_meta", "payload": {"id": self.sid}}]
        for n in (1, 2):
            turn = codex_round(n)
            turn[3] = codex_user_item(self.sid, n, f"question {n}")
            rows += turn
        self.transcript(rows)
        captured = self.capture("codex")
        self.assertTrue(captured["ok"], captured)
        ref = raw.parse_ref(captured["pending_refs"][0])[0]
        path = raw._raw_file(ref)
        before = path.read_bytes()
        toc = raw.read_round(ref)["index"]
        self.assertEqual(len(toc), 2)
        for n, item in enumerate(toc, 1):
            self.assertIn(f"question {n}", item["preview"])
            self.assertNotIn("osk-capture", item["preview"])
            recalled = raw.read_round(f"{ref}#{n}")
            self.assertIn(raw._DIALOGUE_V1, recalled["text"])
            self.assertEqual(item["chars"], recalled["chars"])
        self.assertEqual(path.read_bytes(), before)
        # A user can quote that exact comment. Skip only the recorder's header,
        # not matching user content or all HTML comments, in either format.
        for native in (False, True):
            block = raw._block(1, raw._CODEX_V2, "reply", codex_native=native)
            self.assertEqual(raw._preview(block), raw._CODEX_V2)
            crlf = raw._block(1, "CRLF question", "reply", codex_native=native).replace("\n", "\r\n")
            self.assertEqual(raw._preview(crlf), "CRLF question")

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

    def test_codex_upgrade_preserves_v1_rich_prefix_and_resumes_without_cursor(self):
        reader = transcripts.read
        for native_first in (False, True):
            with self.subTest(native_first=native_first):
                sid = self.sid + str(native_first)
                header = [{"type": "session_meta", "payload": {"id": sid}}]
                old_rows = codex_round(1)
                old_rows[3]["payload"]["images"] = ["https://example.invalid/old.png"]
                item = codex_user_item(sid, 1, "question 1")
                item["payload"]["item"]["content"].append({"type": "image", "url": "https://example.invalid/old.png"})
                old_rows.insert(3 if native_first else 4, item)
                if native_first:
                    old_rows.insert(4, {"type": "response_item", "payload": {
                        "type": "message", "role": "assistant", "content": [
                            {"type": "output_text", "text": "before legacy user"}]}})
                self.transcript(header + old_rows)
                # Frozen v3.14 contract: UserMessage items were ignored. The
                # adapter supplies literal historical bytes, without a v2 stamp.
                # Trace before the legacy user was ignored too.
                legacy = reader(str(self.path), "codex", sid)
                legacy.pop("codex_v1", None)
                legacy.pop("codex_v2", None)
                legacy.pop("dialogue_v1", None)
                legacy["rounds"] = [{"id": "turn-1", "end_line": len(header + old_rows), "completion": "completed",
                    "user": '{"images": ["https://example.invalid/old.png"], "message": "question 1"}',
                    "agent": '{"arguments": "{}", "call_id": "tool-1", "name": "probe", "type": "function_call"}\n\n'
                             '{"call_id": "tool-1", "output": "evidence result", "type": "function_call_output"}\n\n'
                             '{"content": [{"text": "answer 1", "type": "output_text"}], "role": "assistant", "type": "message"}'}]
                with mock.patch.object(transcripts, "read", return_value=legacy):
                    first = it.capture("codex", sid, str(self.path), "capture-tests")
                self.assertTrue(first["ok"], first)
                old_ref = first["pending_refs"][0]
                raw_path = raw._raw_file(raw.parse_ref(old_ref)[0])
                before = raw_path.read_bytes()
                self.assertNotIn(b"osk-capture", before)
                self.assertEqual(before.count(b"https://example.invalid/old.png"), 1)
                self.assertNotIn(b"before legacy user", before)
                old_hash = it._load(it.state_path("codex", sid), "codex", sid)["rounds"][0]["hash"]
                # Same snapshot must keep its token and raw source hash after upgrade.
                upgraded = it.capture("codex", sid, str(self.path), "capture-tests")
                self.assertTrue(upgraded["ok"], upgraded)
                self.assertEqual(upgraded["through"], first["through"])
                self.assertEqual(upgraded["coverage"]["codex_v1_rounds"], [1])
                self.assertIn("옛 포착기가 생략한", it.prompt("codex", sid)["text"])
                self.assertEqual(raw_path.read_bytes(), before)
                it.acknowledge("codex", sid, first["through"], "no_value", "historical fixture has no durable knowledge")
                new_rows = codex_round(2)
                new_item = codex_user_item(sid, 2, "distinct native image input")
                new_item["payload"]["item"]["content"].append({"type": "localImage", "path": "C:/images/new.png"})
                new_rows.insert(4, new_item)
                self.transcript(header + old_rows + new_rows)
                resumed = it.capture("codex", sid, str(self.path), "capture-tests")
                self.assertTrue(resumed["ok"], resumed)
                self.assertEqual((resumed["captured_rounds"], resumed["reviewed_rounds"], resumed["appended"]), (2, 1, 1))
                self.assertTrue(raw_path.read_bytes().startswith(before))
                self.assertEqual(it._load(it.state_path("codex", sid), "codex", sid)["rounds"][0]["hash"], old_hash)
                new_text = raw.read_round(resumed["pending_refs"][0])["text"]
                self.assertIn("distinct native image input", new_text)
                self.assertIn("attachment_ref", new_text)
                self.assertIn("osk-capture: dialogue-v1", new_text)
                self.assertEqual(it.capture("codex", sid, str(self.path), "capture-tests")["appended"], 0)
                it.state_path("codex", sid).unlink()
                reconstructed = it.capture("codex", sid, str(self.path), "capture-tests")
                self.assertTrue(reconstructed["ok"], reconstructed)
                self.assertEqual((reconstructed["captured_rounds"], reconstructed["appended"]), (2, 0))
                # Changed native-only content of a v2 round must not fall back
                # to its unchanged legacy envelope, even after cursor loss.
                after = raw_path.read_bytes()
                new_item["payload"]["item"]["content"][-1]["path"] = "C:/images/tampered.png"
                self.transcript(header + old_rows + new_rows)
                self.assertFalse(it.capture("codex", sid, str(self.path), "capture-tests")["ok"])
                self.assertEqual(raw_path.read_bytes(), after)
                new_item["payload"]["item"]["content"][-1]["path"] = "C:/images/new.png"
                next(r for r in old_rows if r["payload"].get("type") == "user_message")["payload"]["images"] = ["https://example.invalid/changed.png"]
                self.transcript(header + old_rows + new_rows)
                self.assertFalse(it.capture("codex", sid, str(self.path), "capture-tests")["ok"])
                self.assertEqual(raw_path.read_bytes(), after)

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
        scope_memory.replace("other-writer", "other session writes shared memory", cur["hash"], "Scope/Capture")
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
        refused = it.capture("claude", self.sid, str(self.path), self.sid + "-elsewhere", "Scope/W1")
        self.assertFalse(refused["ok"])
        self.assertTrue(it.status("claude", self.sid)["capture_pending"])
        self.assertIn("scope changed", it.status("claude", self.sid)["capture_error"])
        self.assertEqual(first["pending_refs"], refused["pending_refs"])
        self.assertTrue(it.capture("claude", self.sid, str(self.path), self.sid + "-renamed")["ok"])

    def test_capture_normalizes_saved_and_requested_scope(self):
        self.transcript(claude_round(self.sid, 1))
        first = it.capture("claude", self.sid, str(self.path), "capture-tests", "Scope/Capture/")
        self.assertTrue(first["ok"], first)
        # Also repair a successful cursor written with the previous spelling.
        path = it.state_path("claude", self.sid)
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["space"], "Scope/Capture")
        state["space"] = "Scope/Capture/"
        path.write_text(json.dumps(state), encoding="utf-8")
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        resumed = self.capture()
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["appended"], 1)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["space"], "Scope/Capture")

    def test_rejected_first_landing_does_not_pin_the_conversation(self):
        for n, space in enumerate(("Scope/W1", "Scope/Absent", "Capture")):
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

    def test_claude_copied_prefix_reuses_raw_without_inheriting_review(self):
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
        source = raw.read_round(original["pending_refs"][0])["text"]
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
            self.assertEqual(raw.record_state("capture-tests", captured["record"])["rounds"], 1)
            self.assertEqual(raw.read_round(original["pending_refs"][0])["text"], source)
            self.assertEqual(it.capture("claude", child, str(self.path), "capture-tests")["appended"], 0)
            it.state_path("claude", child).unlink()
            rebuilt = it.capture("claude", child, str(self.path), "capture-tests")
            self.assertTrue(rebuilt["ok"], rebuilt)
            self.assertEqual((rebuilt["appended"], rebuilt["inherited_rounds"]), (0, 1))
            changed = json.loads(json.dumps(inherited + tail))
            changed[0]["message"]["content"] = "changed copied question"
            self.transcript(changed)
            refused = it.capture("claude", child, str(self.path), "capture-tests")
            self.assertFalse(refused["ok"])
            self.assertIn("inherited native prefix changed", refused["capture_error"])
        self.assertEqual(it.status("claude", parent)["reviewed_rounds"], 0)
        # A copied vault keeps only raw; both local cursors may disappear.
        it.state_path("claude", parent).unlink()
        it.state_path("claude", child).unlink()
        self.transcript(inherited + tail)
        rebuilt = it.capture("claude", child, str(self.path), "capture-tests")
        self.assertEqual((rebuilt["appended"], rebuilt["inherited_rounds"]), (0, 1), rebuilt)
        name, _ = raw.parse_ref(original["pending_refs"][0])
        source_path = raw._raw_file(name)
        source_path.write_bytes(source_path.read_bytes().replace(b"answer 1", b"altered answer 1"))
        refused = it.capture("claude", child, str(self.path), "capture-tests")
        self.assertFalse(refused["ok"])
        self.assertIn("inherited raw changed", refused["capture_error"])

    def test_claude_fork_uses_result_origin_not_raw_owner(self):
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
        texts = [raw.read_round(ref)["text"] for ref in original["pending_refs"]]
        self.assertIn("claude:" + grandparent + ":", texts[0])
        self.assertIn("claude:" + rows[5]["uuid"] + ":", texts[1])
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
        it.state_path("claude", parent).unlink()
        it.state_path("claude", self.sid).unlink()
        self.assertEqual(self.capture()["appended"], 0)
        changed = json.loads(json.dumps(copied + tail))
        changed[4]["message"]["content"] = changed[4]["message"]["content"].replace(grandparent, self.sid)
        self.transcript(changed)
        self.assertFalse(self.capture()["ok"], "dialogue resembling a locator is not metadata")
        changed = json.loads(json.dumps(copied + tail))
        changed[2]["message"]["content"][0]["content"] = "different result"
        self.transcript(changed)
        refused = self.capture()
        self.assertFalse(refused["ok"])
        self.assertIn("inherited native prefix changed", refused["capture_error"])
        self.assertEqual([raw.read_round(ref)["text"] for ref in original["pending_refs"]], texts)

    def test_claude_copied_prefix_is_not_reused_across_scopes(self):
        parent = self.sid + "-parent"
        rows = claude_round(parent, 1)
        rows[0]["uuid"] = str(uuid.uuid4())
        self.transcript(rows)
        original = it.capture("claude", parent, str(self.path), "capture-tests")
        self.assertTrue(original["ok"], original)
        self.transcript([dict(r, sessionId=self.sid) for r in rows])
        copied = it.capture("claude", self.sid, str(self.path), self.sid, "Scope/W1")
        self.assertTrue(copied["ok"], copied)
        self.assertEqual((copied["captured_rounds"], copied["inherited_rounds"]), (1, 0))
        self.assertTrue(copied["pending_refs"][0].startswith("Scope/W1/"))

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

    def test_unstarted_missing_native_does_not_hide_active_missing_source(self):
        absent = self.capture()
        self.assertTrue(absent['pending'])
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
        self.assertFalse(self.capture()['ok'])
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
        self.assertTrue(self.capture()["ok"])
        changed = claude_round(self.sid, 1)
        changed[0]["message"]["content"] = "rewritten old question"
        self.transcript(changed)
        st = self.capture()
        self.assertFalse(st["ok"])
        self.assertEqual(raw.record_state("capture-tests", st["record"])["rounds"], 1)

    def test_unbound_conversation_can_choose_a_stable_session_without_binding_generic_key(self):
        self.transcript(claude_round(self.sid, 1))
        generic = self.sid + '-unbound'
        failed = it.capture('claude', self.sid, str(self.path), generic)
        self.assertFalse(failed['ok'])
        captured = it.capture('claude', self.sid, str(self.path), 'capture-tests', 'Scope/Capture')
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
        self.assertFalse(it.capture('claude', self.sid, str(self.path), generic, 'Scope/W1')['ok'])

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
        text = "\n".join(raw.read_round(ref)["text"] for ref in st["pending_refs"])
        self.assertTrue(st["ok"], st)
        self.assertNotIn("FILE_FULL_CONTENT_MUST_NOT_COPY", text)
        self.assertNotIn("MIXED_OUTPUT_MUST_NOT_COPY", text)
        self.assertNotIn("C:/private/source.txt", text)
        self.assertIn("Read", text)
        self.assertIn("native_result", text)
        self.assertIn("sha256", text)
        self.assertNotIn("evidence result", text)
        self.assertIn("answer 3", text)
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
        self.assertIn("mcp__osk__read_node", text)
        self.assertIn("tool_evidence_ref", text)
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
        pending = it.capture("claude", self.sid, str(self.path), self.sid, "Scope/Capture")
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
                save()
                reader = transcripts.read
                # Historical codec remains byte-exact when the next append upgrades.
                legacy = reader(str(path), harness, sid)
                legacy.pop("dialogue_v1")
                with mock.patch.object(transcripts, "read", return_value=legacy):
                    old = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(old["ok"], old)
                old_path = raw._raw_file(raw.parse_ref(old["pending_refs"][0])[0])
                prefix = old_path.read_bytes()
                tail = claude_round(sid, 2) if harness == "claude" else codex_round(2)
                rows += tail
                save()
                clean = reader(str(path), harness, sid)["dialogue_v1"]
                if harness == "claude":
                    tail[-1]["message"]["content"].insert(0, {"type":"thinking", "thinking":"OPAQUE_PAYLOAD" * 100000})
                    tail[-1]["message"]["transport_metadata"] = "TRANSPORT_BULK" * 100000
                    tail[1]["message"]["content"][0]["input"] = {"code":"TOOL_CODE_BULK" * 100000}
                else:
                    tail[-3]["payload"]["internal_chat_message_metadata_passthrough"] = {"noise":"TRANSPORT_BULK" * 100000}
                    rows.insert(-1, {"type":"response_item", "payload":{"type":"reasoning", "encrypted_content":"OPAQUE_PAYLOAD" * 100000}})
                    tail[4]["payload"]["arguments"] = "TOOL_CODE_BULK" * 100000
                save()
                noisy = reader(str(path), harness, sid)["dialogue_v1"]
                self.assertEqual([r["user"] for r in clean.values()], [r["user"] for r in noisy.values()])
                self.assertEqual(sum(len(r["agent"]) for r in clean.values()), sum(len(r["agent"]) for r in noisy.values()))
                self.assertNotEqual(list(clean.values())[-1]["agent"], list(noisy.values())[-1]["agent"],
                                    "changed tool payload must change its evidence hash")
                upgraded = it.capture(harness, sid, str(path), sid)
                self.assertTrue(upgraded["ok"], upgraded)
                self.assertEqual(upgraded["appended"], 1)
                stored = old_path.read_bytes()
                self.assertTrue(stored.startswith(prefix))
                new_bytes = stored[len(prefix):]
                for noise in (b"OPAQUE_PAYLOAD", b"TRANSPORT_BULK", b"TOOL_CODE_BULK"):
                    self.assertNotIn(noise, new_bytes)
                self.assertIn(b"dialogue-v1", new_bytes)
                self.assertIn(b"answer 2", new_bytes)
                self.assertIn(b"sha256", new_bytes)
                self.assertIn(long_text, noisy[next(iter(noisy))]["user"])
                it.state_path(harness, sid).unlink()
                replay = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(replay["ok"], replay)
                self.assertEqual(replay["appended"], 0)
                self.assertEqual(old_path.read_bytes(), stored)
                # Visible content still participates in prefix verification.
                if harness == "claude":
                    tail[-1]["message"]["content"][-1]["text"] = "tampered visible reply"
                else:
                    tail[-3]["payload"]["content"][0]["text"] = "tampered visible reply"
                save()
                self.assertFalse(it.capture(harness, sid, str(path), sid)["ok"])
                self.assertEqual(old_path.read_bytes(), stored)

    def test_actual_preserved_scope_node_ack_and_later_receipt_validation(self):
        from osk import contract, distillation as D
        self.transcript(claude_round(self.sid, 1))
        st = it.capture("claude", self.sid, str(self.path), self.sid, space="Scope/W1")
        ref = st["pending_refs"][0]
        spec = {"key": self.sid, "sources": [ref], "hub": "W1"}
        args = {"title": "A retained integration decision", "summary": "bounded retry",
                "body": "A retry is identified by native identity rather than repeated wording.",
                "drafter": "fable-5", "space": "Scope/W1"}
        created = D.create_node(spec, **args)
        self.assertEqual(created["distillation"]["status"], "complete")
        node = contract.parse(core.ROOT / created["path"])
        hub = contract.parse(core.ROOT / "Scope/W1/W1.md")
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
if not (core.ROOT / 'Scope/W1').is_dir():
    validate.make_mini_vault(core.ROOT)
sid = os.environ['OSK_PROBE_SID']
st = it.capture('claude', sid, os.environ['OSK_PROBE_TRANSCRIPT'], 'copy-session', 'Scope/W1')
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
        self.assertIn("/.records/legacy-record-name.txt#1", st["pending_refs"][0])

    def test_raw_migration_preserves_pending_snapshot_and_legacy_receipt_match(self):
        from osk import distillation as D
        self.transcript(claude_round(self.sid, 1))
        first = it.capture("claude", self.sid, str(self.path), self.sid, "Scope/W1")
        state_path = it.state_path("claude", self.sid)
        state = it._load(state_path, "claude", self.sid)
        physical = raw._raw_file(raw.parse_ref(first["pending_refs"][0])[0])
        legacy = physical.parent.parent / (physical.stem + ".md")
        physical.rename(legacy)  # Simulate the pre-upgrade physical record and cursor.
        saved = legacy.read_bytes()
        old_ref = f"[[{legacy.relative_to(ROOT).as_posix()}#1]]"
        state["rounds"][0]["ref"] = old_ref
        token = it._snapshot(state)
        state["snapshots"] = {token: {"count": 1, "prompt_count": state["prompt_count"]}}
        it._save(state_path, state)
        replayed = it.capture("claude", self.sid, str(self.path), self.sid)
        self.assertTrue(replayed["ok"], replayed)
        self.assertEqual(replayed["through"], token)
        self.assertEqual(replayed["pending_refs"], [old_ref])
        self.assertEqual(replayed["appended"], 0)
        self.assertFalse(legacy.exists())
        self.assertEqual(physical.read_bytes(), saved)
        out = D.create_node({"key": self.sid, "sources": [old_ref], "hub": "W1"},
                            title=self.sid, summary="retained observation",
                            body="Observation retained with its original source.",
                            drafter="test-model", space="Scope/W1")
        self.assertEqual(out["distillation"]["status"], "complete")
        ack = it.acknowledge("claude", self.sid, token, "preserved", "fixture retained",
                             [{"key": self.sid}])
        self.assertTrue(ack["ok"], ack)

    def test_review_key_tracks_snapshot_and_recovers_saved_proof(self):
        from osk import distillation as D
        self.transcript(claude_round(self.sid, 1))
        it.capture("claude", self.sid, str(self.path), self.sid, "Scope/W1")
        first = it.prompt("claude", self.sid)
        self.assertEqual(first["key"], it.prompt("claude", self.sid)["key"])
        spec = {"key": first["key"] + ":stable-target", "sources": first["pending_refs"], "hub": "W1"}
        saved = D.create_node(spec, title="Snapshot-specific retained observation", summary="native observation",
                              body="The observation is preserved before its review acknowledgement arrives.",
                              drafter="fable-5", space="Scope/W1")
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
        return it.capture("claude", self.sid, str(self.path), self.sid, "Scope/W1")

    def _discovery_save(self, st, key, title=None):
        from osk import distillation as D
        self.addCleanup(D._job_path(key).unlink, missing_ok=True)
        return D.create_node({"key": key, "sources": st["pending_refs"], "hub": "W1"},
                             title=title or self.sid + "-node", summary="lost ACK recovery",
                             body="Keep the already retained decision and finish its missing acknowledgement.",
                             drafter="fable-5", space="Scope/W1")

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
        other = it.capture("claude", other_sid, str(other_path), other_sid, "Scope/W1")
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
        hub = ROOT / "Scope/W1/W1.md"
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



    def test_native_secret_strings_are_filtered_before_json_capture(self):
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
                with mock.patch.object(raw.secrets, "write_raw", wraps=raw.secrets.write_raw) as sink:
                    st = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertEqual(st["captured_rounds"], 1)
                self.assertEqual(sink.call_count, 1)  # mandatory final filter still runs
                raw_path, _ = raw.parse_ref(st["pending_refs"][0])
                saved = (ROOT / raw_path).read_bytes()
                for token in tokens:
                    self.assertNotIn(token.encode(), saved)
                self.assertEqual(saved.count(b"[FILTERED:github-token]"), 2)
                self.assertNotIn(b"ghp_short", saved)  # tool payload is a reference
                self.assertIn(b"tool_evidence_ref", saved)
                self.assertEqual(path.read_bytes(), native)  # native evidence is not edited
                again = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(again["ok"], again)
                self.assertEqual(again["appended"], 0)
                self.assertEqual((ROOT / raw_path).read_bytes(), saved)

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
                with mock.patch.object(raw.secrets, "write_raw", wraps=raw.secrets.write_raw) as sink:
                    st = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertFalse(st["capture_pending"])
                self.assertEqual(st["captured_rounds"], 1)
                self.assertEqual(sink.call_count, 1)
                raw_path, _ = raw.parse_ref(st["pending_refs"][0])
                saved = (ROOT / raw_path).read_bytes()
                self.assertEqual(saved.count(b"[FILTERED:github-token]"), 2)
                self.assertIn(b"first", saved)
                self.assertIn(b"second", saved)
                self.assertLess(saved.index(b"first"), saved.index(b"second"))
                for token in collision:
                    self.assertNotIn(token.encode(), saved)
                self.assertEqual(path.read_bytes(), native)
                again = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(again["ok"], again)
                self.assertEqual(again["appended"], 0)
                self.assertEqual((ROOT / raw_path).read_bytes(), saved)

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
                with mock.patch.object(raw.secrets, "write_raw", wraps=raw.secrets.write_raw) as sink:
                    st = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertFalse(st["capture_pending"])
                self.assertEqual(st["captured_rounds"], 1)
                self.assertEqual(sink.call_count, 1)
                raw_path, _ = raw.parse_ref(st["pending_refs"][0])
                saved = (ROOT / raw_path).read_bytes()
                # Exact encoded content retains all duplicate pairs and whitespace.
                self.assertIn(json.dumps(expected).encode(), saved)
                self.assertEqual(saved.count(b"[FILTERED:github-token]"), 2)
                for token in tokens:
                    self.assertNotIn(token.encode(), saved)
                self.assertEqual(path.read_bytes(), native)
                again = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(again["ok"], again)
                self.assertEqual(again["appended"], 0)
                self.assertEqual((ROOT / raw_path).read_bytes(), saved)

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
                first = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(first["ok"], first)
                self.assertFalse(first["capture_pending"])
                self.assertEqual(first["appended"], 1)
                raw_path, _ = raw.parse_ref(first["pending_refs"][0])
                saved_first = (ROOT / raw_path).read_bytes()
                self.assertEqual(transcripts._dump(dialogue), dialogue)
                # raw._block already strips trailing whitespace from whole sections.
                expected = dialogue.rstrip() if harness == "claude" else json.dumps(dialogue)
                self.assertIn(expected.encode(), saved_first)
                self.assertEqual(path.read_bytes(), native)
                native += "".join(json.dumps(row) + "\n" for row in later).encode("utf-8")
                path.write_bytes(native)
                second = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(second["ok"], second)
                self.assertFalse(second["capture_pending"])
                self.assertEqual(second["captured_rounds"], 2)
                self.assertEqual(second["appended"], 1)
                saved = (ROOT / raw_path).read_bytes()
                self.assertTrue(saved.startswith(saved_first))
                self.assertIn(b"question 2", saved)
                self.assertIn(b"answer 2", saved)
                again = it.capture(harness, sid, str(path), sid, "Scope/W1")
                self.assertTrue(again["ok"], again)
                self.assertEqual(again["appended"], 0)
                self.assertEqual((ROOT / raw_path).read_bytes(), saved)
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



if __name__ == "__main__":
    unittest.main(verbosity=2)
