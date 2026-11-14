"""대화 포착과 검토의 수트의 한 갈래: 포착 — 원본의 자리, 실패와 복구.

`tests/test_integration.py`의 `IntegrationTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from integration_cases.support import claude_round, codex_round, ENGINE, legacy_cursor, ROOT, TMP, VAULT
from osk import (integration as it, transcripts, write, conversation_capture, conversation_cite,  # noqa: E402
                 conversation_review, conversation_state)  # noqa: E402


class CaptureCases:
    def test_missing_claude_native_file_persists_retry_before_harness_detection(self):
        base = Path(TMP.name) / "claude-home"
        native = base / "projects" / "project" / (self.sid + ".jsonl")
        env = {"session_id": self.sid, "transcript_path": str(native)}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(base)}, clear=False):
            failed = conversation_capture.hook_capture(VAULT, env, "capture-tests")
            self.assertFalse(failed["ok"])
            self.assertIn("FileNotFoundError", failed["capture_error"])
            self.assertTrue(conversation_state.state_path(VAULT, "claude", self.sid).exists())
            native.parent.mkdir(parents=True)
            native.write_text("".join(json.dumps(r) + "\n" for r in claude_round(self.sid, 1)), encoding="utf-8")
            recovered = conversation_capture.hook_capture(VAULT, env, "capture-tests")
            self.assertTrue(recovered["ok"], recovered)
            self.assertEqual(recovered["captured_rounds"], 1)

    def test_a_source_missing_for_a_week_closes_as_lost_and_reopens_when_back(self):
        # 원본이 7일 계속 없으면 커서를 원본 소실로 닫고, 원본이 돌아오면 이어 간다(#244)
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        self.capture()
        unreviewed = conversation_review.acknowledge(VAULT, "claude", self.sid, first["through"], "no_value", "fixture")["pending_refs"]
        away = self.path.with_name(self.path.name + ".away")
        self.path.rename(away)
        mine = lambda: [s for s in it._known_pending(VAULT, None)[0] if s["conversation_id"] == self.sid]
        self.assertTrue(mine(), "the first day without the source still retries it")
        with mock.patch.object(time, "time", return_value=time.time() + conversation_capture.SOURCE_LOST_AFTER + 1):
            self.assertEqual(mine(), [], "a week later the closed cursor is neither captured nor listed")
        st = conversation_capture.status(VAULT, "claude", self.sid)
        self.assertEqual(st["capture_recovery"]["state"], "closed")
        self.assertEqual(st["source_lost"]["refs"], unreviewed)   # 처분에 남고 지워지지 않는다
        self.assertEqual((st["pending_refs"], st["pending"], st["capture_error"]), ([], False, None))
        self.assertEqual(st["captured_rounds"], 2)
        again = conversation_capture.capture(VAULT, "claude", self.sid, None, "capture-tests")
        self.assertEqual((again["capture_recovery"]["state"], again["capture_error"]), ("closed", None))
        away.rename(self.path)
        back = self.capture()
        self.assertIsNone(back["source_lost"])
        self.assertEqual(back["pending_refs"], unreviewed)

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
        job = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertEqual(job["raw_review"]["state"], "native")
        self.assertEqual(job["raw_review"]["rounds"], 1)


    def test_unstarted_missing_native_does_not_hide_active_missing_source(self):
        absent = self.capture()
        self.assertTrue(absent['pending'])
        self.assertEqual(absent["capture_recovery"]["state"], "awaiting_input")
        state_path = conversation_state.state_path(VAULT, 'claude', self.sid)
        before = state_path.read_bytes()
        states, _, _, waiting = it._known_pending(VAULT, 100)
        self.assertNotIn(self.sid, [s['conversation_id'] for s in states])
        self.assertIn(self.sid, [s['conversation_id'] for s in waiting])
        after = json.loads(state_path.read_bytes())
        self.assertIsInstance(after.pop('source_missing_since'), float)   # 원본 시계만 더한다(#244)
        self.assertEqual(after, json.loads(before))
        conversation_capture.tick(VAULT, 'claude', self.sid)
        states, _, _, waiting = it._known_pending(VAULT, 100)
        self.assertIn(self.sid, [s['conversation_id'] for s in states])
        self.assertNotIn(self.sid, [s['conversation_id'] for s in waiting])
        missing = self.capture()
        self.assertFalse(missing['ok'])
        self.assertEqual(missing["capture_recovery"]["state"], "action_required")
        self.transcript(claude_round(self.sid,1))
        self.assertEqual(self.capture()['appended'], 1)
        self.path.unlink()
        states, _, _, waiting = it._known_pending(VAULT, 100)
        self.assertIn(self.sid, [s['conversation_id'] for s in states])
        self.assertNotIn(self.sid, [s['conversation_id'] for s in waiting])

    def test_unstarted_native_arrival_is_discovered_without_prompt_tick(self):
        self.capture()
        self.transcript(claude_round(self.sid,1))
        states, _, _, waiting = it._known_pending(VAULT, 100)
        self.assertIn(self.sid, [s['conversation_id'] for s in states])
        self.assertNotIn(self.sid, [s['conversation_id'] for s in waiting])
        self.assertEqual(self.capture()['appended'], 1)

    def test_archived_codex_recovery_keeps_prefix_and_review_receipts(self):
        self.transcript([{'type': 'session_meta', 'payload': {'id': self.sid}}] + codex_round(1))
        first = self.capture('codex')
        conversation_review.acknowledge(VAULT, 'codex', self.sid, first['through'], 'no_value', 'synthetic fixture')
        old = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'codex', self.sid), 'codex', self.sid)
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {'CODEX_HOME': home}):
            archived = Path(home) / 'archived_sessions' / f'rollout-date-{self.sid}.jsonl'
            archived.parent.mkdir()
            self.path.replace(archived)
            with archived.open('a', encoding='utf-8') as f:
                f.write(''.join(json.dumps(r) + '\n' for r in codex_round(2)))
            recovered = conversation_capture.hook_capture(VAULT, {'harness': 'codex', 'session_id': self.sid}, 'capture-tests')
            self.assertTrue(recovered['ok'], recovered)
            self.assertEqual(recovered['appended'], 1)
            self.assertEqual(recovered['transcript_path'], str(archived.resolve()))
            new = conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'codex', self.sid), 'codex', self.sid)
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
            states, _, _, waiting = it._known_pending(VAULT, 100)
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
            self.assertTrue(conversation_capture.capture(VAULT, 'codex', self.sid, str(resumed), 'capture-tests')['ok'])

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
                first = conversation_capture.capture(VAULT, harness, sid, str(self.path), "capture-tests")
                conversation_review.acknowledge(VAULT, harness, sid, first["through"], "no_value", "Completed fixture only.")
                state_path = conversation_state.state_path(VAULT, harness, sid)
                before = conversation_state.load_state(VAULT, state_path, harness, sid)
                other = self.path.with_suffix(".foreign.jsonl")
                other.write_text("".join(json.dumps(r) + "\n" for r in rows("foreign", 1)), encoding="utf-8")
                refused = conversation_capture.capture(VAULT, harness, sid, str(other), "capture-tests")
                self.assertFalse(refused["ok"], refused)
                self.assertEqual(refused["capture_recovery"]["phase"], "read")
                self.assertEqual(conversation_state.load_state(VAULT, state_path, harness, sid)["transcript_path"], before["transcript_path"])
                self.transcript(rows(sid, 2))
                recovered = conversation_capture.capture(VAULT, harness, sid, None, "capture-tests")
                self.assertTrue(recovered["ok"], recovered)
                self.assertEqual(recovered["appended"], 1)
                after = conversation_state.load_state(VAULT, state_path, harness, sid)
                self.assertEqual(after["rounds"][:1], before["rounds"])
                self.assertEqual(after["reviews"], before["reviews"])
                self.assertEqual(after["reviewed_count"], 1)
                self.assertEqual(conversation_review.review_status(VAULT, harness, sid, first["through"])["status"], "complete")
                self.assertEqual(conversation_capture.capture(VAULT, harness, sid, None, "capture-tests")["appended"], 0)


    def test_changed_prefix_at_another_path_does_not_replace_the_verified_source(self):
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        conversation_review.acknowledge(VAULT, "claude", self.sid, first["through"], "no_value", "Completed fixture only.")
        state_path = conversation_state.state_path(VAULT, "claude", self.sid)
        before = conversation_state.load_state(VAULT, state_path, "claude", self.sid)
        changed = claude_round(self.sid, 1) + claude_round(self.sid, 2)
        changed[0]["uuid"] = "user-1-rewritten"
        candidate = self.path.with_suffix(".changed.jsonl")
        candidate.write_text("".join(json.dumps(r) + "\n" for r in changed), encoding="utf-8")
        refused = conversation_capture.capture(VAULT, "claude", self.sid, str(candidate), "capture-tests")
        self.assertFalse(refused["ok"], refused)
        self.assertEqual(refused["capture_recovery"]["phase"], "replay")
        after = conversation_state.load_state(VAULT, state_path, "claude", self.sid)
        self.assertEqual(after["transcript_path"], before["transcript_path"])
        for key in ("rounds", "reviews", "reviewed_count", "snapshots", "native_fingerprint", "coverage"):
            self.assertEqual(after[key], before[key], key)
        recovered = conversation_capture.capture(VAULT, "claude", self.sid, None, "capture-tests")
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["appended"], 0)


    def test_missing_native_and_unavailable_raw_have_separate_recovery_states(self):
        self.transcript(claude_round(self.sid, 1))
        self.capture()
        state, record = legacy_cursor("claude", self.sid, self.path)
        through = conversation_state.snapshot(state)
        raw_before = record.read_bytes()
        self.path.unlink()
        missing = conversation_capture.capture(VAULT, "claude", self.sid, None, "capture-tests")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["capture_recovery"]["state"], "action_required")
        self.assertEqual(missing["capture_recovery"]["phase"], "source")
        job = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertEqual(job["raw_review"], {"state": "verified", "rounds": 1, "error": None})
        self.assertEqual(job["through"], through)
        self.assertEqual(record.read_bytes(), raw_before)
        record.write_bytes(raw_before.replace(b"question 1", b"altered question"))
        blocked = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertEqual(blocked["raw_review"]["state"], "unavailable")
        self.assertTrue(blocked["raw_review"]["error"])
        with self.assertRaises((ValueError, write.WriteError)):
            conversation_review.acknowledge(VAULT, "claude", self.sid, through, "no_value", "Fixture only.")
        self.assertEqual(conversation_capture.status(VAULT, "claude", self.sid)["reviewed_rounds"], 0)
        record.write_bytes(raw_before)
        # This ACK judges only the verified stored round; it cannot close capture.
        reviewed = conversation_review.acknowledge(VAULT, "claude", self.sid, through, "no_value", "Stored fixture has no reusable claim.")
        self.assertTrue(reviewed["capture_pending"])
        self.assertTrue(reviewed["capture_error"])
        self.assertTrue(reviewed["pending"])
        self.assertEqual(it.prompt(VAULT, "claude", self.sid, include_organization=False)["raw_review"]["state"], "none")


    def test_relocated_capture_io_failure_keeps_candidate_until_cursor_recovery(self):
        # Catch up only this fault matrix, not every unrelated suite fixture.
        state_path_for = conversation_state.state_path
        state_dir = Path(TMP.name) / self.sid
        patcher = mock.patch.object(conversation_state, "state_path", side_effect=lambda vault, harness, sid:
                                    state_dir / state_path_for(vault, harness, sid).name)
        patcher.start()
        self.addCleanup(patcher.stop)
        for retry in ("pathless", "catchup"):
            with self.subTest(retry=retry):
                sid = f"{self.sid}-{retry}"
                self.transcript(claude_round(sid, 1))
                first = conversation_capture.capture(VAULT, "claude", sid, str(self.path), "capture-tests")
                conversation_review.acknowledge(VAULT, "claude", sid, first["through"], "no_value", "Completed fixture only.")
                state_path = conversation_state.state_path(VAULT, "claude", sid)
                before = conversation_state.load_state(VAULT, state_path, "claude", sid)
                candidate = self.path.with_name(sid + ".relocated.jsonl")
                candidate.write_text("".join(json.dumps(r) + "\n" for r in
                                            claude_round(sid, 1) + claude_round(sid, 2)), encoding="utf-8")
                # A transient read failure, explicit or pathless, keeps the candidate source.
                for attempt in (str(candidate), None):
                    with mock.patch.object(transcripts, "read", side_effect=PermissionError("Native temporarily locked.")):
                        failed = conversation_capture.capture(VAULT, "claude", sid, attempt, "capture-tests")
                    self.assertFalse(failed["ok"], failed)
                    self.assertTrue(failed["capture_pending"])
                    self.assertEqual((failed["captured_rounds"], failed["reviewed_rounds"]), (1, 1))
                    failed_state = conversation_state.load_state(VAULT, state_path, "claude", sid)
                    self.assertEqual(failed_state["transcript_path"], before["transcript_path"])
                    self.assertEqual(failed_state.get("capture_path"), str(candidate.resolve()))
                if retry == "pathless":
                    recovered = conversation_capture.capture(VAULT, "claude", sid, None, "capture-tests")
                else:
                    caught = it.catchup(VAULT, 100)
                    recovered = next(c for c in caught["captures"] if c["conversation_id"] == sid)
                self.assertTrue(recovered["ok"], recovered)
                self.assertEqual(recovered["appended"], 1)
                after = conversation_state.load_state(VAULT, state_path, "claude", sid)
                self.assertEqual(after["transcript_path"], str(candidate.resolve()))
                self.assertNotIn("capture_path", after)
                self.assertEqual(after["rounds"][:1], before["rounds"])
                self.assertEqual(after["reviews"], before["reviews"])
                self.assertEqual(after["snapshots"][first["through"]], before["snapshots"][first["through"]])
                self.assertEqual((len(after["rounds"]), after["reviewed_count"]), (2, 1))
                job = it.prompt(VAULT, "claude", sid, include_organization=False)
                self.assertEqual(job["pending_refs"], [after["rounds"][1]["ref"]])
                self.assertEqual(job["raw_review"], {"state": "native", "rounds": 1, "error": None})

    def test_hook_process_resume_keeps_pending_and_diagnostics_are_visible(self):
        self.transcript(claude_round(self.sid, 1))
        self.capture()
        for _ in range(9):
            conversation_capture.tick(VAULT, "claude", self.sid)
        cwd = Path(TMP.name) / "capture-tests"
        cwd.mkdir(exist_ok=True)
        payload = {"session_id": self.sid, "harness": "claude", "transcript_path": str(self.path), "source": "resume", "cwd": str(cwd)}
        result = subprocess.run([sys.executable, str(ENGINE / "scripts/hooks/claude_session_start.py")],
                                input=json.dumps(payload).encode(), capture_output=True, env=os.environ, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.sid, out)
        self.assertEqual(conversation_capture.status(VAULT, "claude", self.sid)["prompt_count"], 9)
        broken = subprocess.run([sys.executable, str(ENGINE / "scripts/hooks/claude_prompt_submit.py")],
                                input=b"{broken", capture_output=True, env=os.environ, timeout=30)
        self.assertEqual(broken.returncode, 0)
        self.assertIn("diagnostic", json.loads(broken.stdout)["hookSpecificOutput"]["additionalContext"])


    def test_stop_before_flush_catchup_without_resume_and_changed_after_ack(self):
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1, finished=False))
        pending = self.capture("codex")
        self.assertTrue(pending["capture_pending"])
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1))
        caught = it.catchup(VAULT, 100)
        capture = next(c for c in caught["captures"] if c["conversation_id"] == self.sid)
        self.assertEqual((capture["ok"], capture["appended"]), (True, 1))
        self.assertNotIn(self.sid, [j["conversation_id"] for j in caught["jobs"]])
        # The conversation itself reviews its turns at its next prompt.
        job = it.prompt(VAULT, "codex", self.sid, include_organization=False)
        self.assertEqual(len(job["pending_refs"]), 1)
        self.assertIn(sys.executable, job["text"])
        conversation_review.acknowledge(VAULT, "codex", self.sid, job["through"], "no_value", "bounded test round")
        self.assertEqual(conversation_review.review_status(VAULT, "codex", self.sid, job["through"])["status"], "complete")
        self.assertFalse(conversation_capture.status(VAULT, "codex", self.sid)["pending"])
        # The session can append and exit before Stop; the fingerprint only says to look again.
        self.transcript([{"type": "session_meta", "payload": {"id": self.sid}}] + codex_round(1) + codex_round(2))
        it.catchup(VAULT, 100)
        job2 = it.prompt(VAULT, "codex", self.sid, include_organization=False)
        self.assertEqual(len(job2["pending_refs"]), 1)
        self.assertNotEqual(job["through"], job2["through"])

    def test_scripted_codex_exec_run_is_not_tracked_or_reviewed(self):
        path = conversation_state.state_path(VAULT, "codex", self.sid)
        old = conversation_state.load_state(VAULT, path, "codex", self.sid)
        # An earlier engine left an open-tail error on this run.
        old.update(session="capture-tests", capture_pending=True, capture_error="ValueError: old open tail")
        conversation_state.save_state(path, old)
        meta = {"type": "session_meta", "payload": {"id": self.sid, "originator": "codex_exec", "source": "exec"}}
        self.transcript([meta] + codex_round(1) + codex_round(2))
        found = self.capture("codex")
        self.assertEqual((found["ok"], found["captured_rounds"], found["pending"], found["excluded"]),
                         (True, 0, False, "codex_exec"))
        self.assertIsNone(found["capture_error"])
        ended = time.time() - conversation_capture.ENDED_AFTER - 60
        os.utime(self.path, (ended, ended))
        self.assertNotIn(self.sid, [j["conversation_id"] for j in it.list_pending(VAULT, 100)["jobs"]])
        # An interactive Codex conversation is still tracked.
        meta["payload"].update(originator="codex_cli_rs", source="cli")
        other = f"{self.sid}-interactive"
        meta["payload"]["id"] = other
        talk = Path(TMP.name) / f"{other}.jsonl"
        talk.write_text("".join(json.dumps(r) + "\n" for r in [meta] + codex_round(1)), encoding="utf-8")
        tracked = conversation_capture.capture(VAULT, "codex", other, str(talk), "capture-tests")
        self.assertEqual((tracked["ok"], tracked["captured_rounds"], tracked["excluded"]), (True, 1, None))


    def test_claude_hook_detects_identity_after_leading_metadata(self):
        self.transcript([{"type": "file-history-snapshot", "snapshot": {}},
                         {"type": "queue-operation", "operation": "enqueue"}]
                        + claude_round(self.sid, 1))
        st = conversation_capture.hook_capture(VAULT, {"session_id": self.sid, "transcript_path": str(self.path)}, "capture-tests")
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["harness"], "claude")
        self.assertEqual(st["captured_rounds"], 1)

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
                st = conversation_capture.capture(VAULT, harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertEqual(st["captured_rounds"], 2)
                record = ROOT / conversation_cite.cite(VAULT, f"{harness}/{sid}", quote="MIDPOINT CORRECTION")["path"]
                conversation_cite.cite(VAULT, f"{harness}/{sid}", turn="-1")
                stored = record.read_bytes()
                for kept in (b"USER START", b"MIDPOINT CORRECTION", b"USER END", b"question 2"):
                    self.assertIn(kept, stored)
                for noise in (b"OPAQUE_PAYLOAD", b"TRANSPORT_BULK", b"TOOL_CODE_BULK", b"answer 2"):
                    self.assertNotIn(noise, stored)

    def test_crash_during_capture_retries_without_duplicate(self):
        self.transcript(claude_round(self.sid, 1))
        with mock.patch.object(transcripts, "read", side_effect=SystemExit("simulated power loss mid-capture")):
            with self.assertRaises(SystemExit):
                self.capture()
        self.assertTrue(conversation_capture.status(VAULT, "claude", self.sid)["pending"])
        st = self.capture()
        self.assertEqual((st["appended"], st["captured_rounds"]), (1, 1))
        self.assertEqual(self.capture()["appended"], 0)
        conversation_state.state_path(VAULT, "claude", self.sid).unlink()
        again = self.capture()
        self.assertEqual((again["appended"], again["pending_refs"]), (1, st["pending_refs"]))

    def test_relocated_capture_crash_keeps_candidate_for_catchup(self):
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        conversation_review.acknowledge(VAULT, "claude", self.sid, first["through"], "no_value", "Completed fixture only.")
        state_path = conversation_state.state_path(VAULT, "claude", self.sid)
        before = conversation_state.load_state(VAULT, state_path, "claude", self.sid)
        candidate = self.path.with_suffix(".relocated.jsonl")
        candidate.write_text("".join(json.dumps(r) + "\n" for r in
                                    claude_round(self.sid, 1) + claude_round(self.sid, 2)), encoding="utf-8")
        with mock.patch.object(transcripts, "read", side_effect=SystemExit("Power loss while reading the relocated source.")):
            with self.assertRaises(SystemExit):
                conversation_capture.capture(VAULT, "claude", self.sid, str(candidate), "capture-tests")
        crashed = conversation_state.load_state(VAULT, state_path, "claude", self.sid)
        self.assertEqual(crashed["transcript_path"], before["transcript_path"])
        self.assertEqual(crashed.get("capture_path"), str(candidate.resolve()))
        caught = it.catchup(VAULT, 100)
        capture = next(c for c in caught["captures"] if c["conversation_id"] == self.sid)
        self.assertEqual((capture["ok"], capture["appended"]), (True, 1))
        # The new turn waits for its own conversation; no offline worker reviews it.
        self.assertNotIn(self.sid, [j["conversation_id"] for j in caught["jobs"]])
        recovered = conversation_state.load_state(VAULT, state_path, "claude", self.sid)
        self.assertEqual(recovered["transcript_path"], str(candidate.resolve()))
        self.assertNotIn("capture_path", recovered)
        self.assertEqual(recovered["rounds"][:1], before["rounds"])
        self.assertEqual(recovered["reviews"], before["reviews"])
        self.assertEqual((len(recovered["rounds"]), recovered["reviewed_count"]), (2, 1))
        self.assertEqual(conversation_capture.capture(VAULT, "claude", self.sid, None, "capture-tests")["appended"], 0)
