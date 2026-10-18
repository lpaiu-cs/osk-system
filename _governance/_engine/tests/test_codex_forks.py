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
        self.assertTrue(cited["path"].startswith("_cited/.records/"), cited)     # the one root record

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
        s["capture_failure_phase"] = "read"
        with mock.patch.object(it, "_locate_transcript", side_effect=AssertionError("recorded phase is sufficient")):
            self.assertEqual(it._capture_recovery(s)["basis"], "recorded_phase")
        # A recorded miss is checked against the disk: the source may be there now (#203).
        s["capture_failure_phase"] = "source"
        s["capture_error"] = "FileNotFoundError: fixture"
        existing = self.page(self.child, self.child, turn(self.child, "c1"))
        with mock.patch.object(it, "_locate_transcript", return_value=str(existing)):
            self.assertEqual(it._capture_recovery(s)["state"], "retryable")


def compress(path, *, keep=False):
    """Codex's cold-rollout compression: `<name>.zst` with the plain size and mtime, then the
    plain file removed (`keep` is the moment between the two)."""
    import zstandard
    zst = path.with_name(path.name + ".zst")
    zst.write_bytes(zstandard.ZstdCompressor(level=3).compress(path.read_bytes()))
    st = path.stat()
    os.utime(zst, ns=(st.st_atime_ns, st.st_mtime_ns))
    if not keep:
        path.unlink()
    return zst


def restore(zst):
    """Codex's resume: the plain file back with the mtime, the `.zst` removed."""
    import zstandard
    plain = zst.with_suffix("")
    plain.write_bytes(zstandard.ZstdDecompressor().decompress(zst.read_bytes()))
    st = zst.stat()
    os.utime(plain, ns=(st.st_atime_ns, st.st_mtime_ns))
    zst.unlink()
    return plain


class CompressedRolloutTests(unittest.TestCase):
    """Codex compresses a rollout left 7 days to `.jsonl.zst` in place (#203)."""
    setUp, page, fork = CodexForkTests.setUp, CodexForkTests.page, CodexForkTests.fork

    def stored(self, sid):
        return json.loads(it.state_path("codex", sid).read_text(encoding="utf-8"))

    def test_compressed_rollout_is_the_same_conversation_not_a_new_one(self):
        from osk import response_growth
        usage = {"input_tokens": 7, "cached_input_tokens": 5}
        rows = (turn(self.parent, "p1") + [event("event_msg", type="token_count", info={"total_token_usage": usage})]
                + turn(self.parent, "p2"))
        plain = self.page(self.parent, self.parent, rows)
        first = it.capture("codex", self.parent, str(plain), "forks")
        it.acknowledge("codex", self.parent, first["through"], "no_value", "synthetic fixture")
        fingerprint = self.stored(self.parent)["native_fingerprint"]
        zst = compress(plain)
        self.assertEqual(it._locate_transcript("codex", self.parent, str(plain)), str(zst.resolve()))
        self.assertEqual(it.adapters.get("codex").transcripts(self.parent), [zst])
        # Plain bytes and the kept mtime: the stored fingerprint holds, so nothing is due.
        self.assertEqual(transcripts.native_fingerprint(str(zst), "codex", self.parent), fingerprint)
        self.assertEqual(it._known_pending(None)[0], [])
        again = it.capture("codex", self.parent, None, "forks")
        self.assertTrue(again["ok"], again)
        self.assertEqual((again["appended"], again["changed"], again["through"]), (0, 0, first["through"]))
        self.assertEqual(again["transcript_path"], str(zst.resolve()))
        # The other native readers see the plain bytes too.
        self.assertEqual(response_growth.profile(str(zst), "codex", self.parent)["usage"], usage)
        self.assertIsNone(it.adapters.get("codex").subagent(str(zst), self.parent))
        # Resumed, Codex restores the plain file first; only a new turn is new.
        plain = restore(zst)
        with plain.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in turn(self.parent, "p3")))
        resumed = it.capture("codex", self.parent, None, "forks")
        self.assertEqual((resumed["ok"], resumed["appended"], resumed["transcript_path"]),
                         (True, 1, str(plain.resolve())))

    def test_both_forms_during_the_switch_read_as_one_rollout(self):
        parent, child = self.fork()
        before = it.capture("codex", self.child, str(child), "forks")
        fingerprint = self.stored(self.child)["native_fingerprint"]
        for path in (parent, child):
            compress(path, keep=True)
        self.assertEqual(it.adapters.get("codex").transcripts(self.child), [child])
        self.assertEqual(transcripts.native_fingerprint(str(child), "codex", self.child), fingerprint)
        during = it.capture("codex", self.child, None, "forks")
        self.assertEqual((during["ok"], during["appended"]), (True, 0))
        # The plain file goes between the lookup and the read: the read takes the `.zst`.
        for path in (parent, child):
            path.unlink()
        self.assertEqual([r["id"] for r in transcripts.read(str(child), "codex", self.child)["rounds"]], ["p1", "c1"])
        after = it.capture("codex", self.child, str(child), "forks")
        self.assertEqual((after["ok"], after["appended"], after["through"]), (True, 0, before["through"]))
        self.assertEqual(self.stored(self.child)["native_fingerprint"], fingerprint)

    def test_compressed_history_base_keeps_its_plain_byte_bound(self):
        first = self.page(self.parent, self.parent + "-old", turn(self.parent, "p1"))
        second = self.page(self.parent, self.parent, turn(self.parent, "p2"),
                           base=(self.parent + "-old", first.stat().st_size))
        bound = second.stat().st_size
        child = self.page(self.child, self.child, turn(self.child, "c1"), base=(self.parent, bound), fork=self.parent)
        with second.open("a", encoding="utf-8") as f:   # past the declared bound
            f.write("".join(json.dumps(r) + "\n" for r in turn(self.parent, "excluded")))
        before = it.capture("codex", self.child, str(child), "forks")
        self.assertEqual((before["captured_rounds"], before["inherited_rounds"]), (1, 2))
        fingerprint = self.stored(self.child)["native_fingerprint"]
        compress(first), compress(second)
        self.assertEqual(transcripts.native_fingerprint(str(child), "codex", self.child), fingerprint)
        self.assertTrue(all(page["path"].endswith(".jsonl") for page in fingerprint["history"]), fingerprint)
        parsed = transcripts.read(str(child), "codex", self.child)
        self.assertEqual([r["id"] for r in parsed["rounds"]], ["p1", "p2", "c1"])
        # The conversation's own page compressed as well.
        compress(child)
        again = it.capture("codex", self.child, None, "forks")
        self.assertEqual((again["ok"], again["appended"], again["through"]), (True, 0, before["through"]))
        # A bound past the plain bytes is refused as before, not read short.
        sid = self.child + "-far"
        far = self.page(sid, sid, turn(sid, "f1"), base=(self.parent, bound + 10 ** 6), fork=self.parent)
        with self.assertRaisesRegex(ValueError, "exceeds source"):
            transcripts.read(str(far), "codex", sid)

    def test_missing_source_error_with_a_compressed_rollout_is_retried(self):
        plain = self.page(self.parent, self.parent, turn(self.parent, "p1"))
        first = it.capture("codex", self.parent, str(plain), "forks")
        it.acknowledge("codex", self.parent, first["through"], "no_value", "synthetic fixture")
        compress(plain)
        # What an engine without `.zst` support left: a miss of the source that is there.
        path = it.state_path("codex", self.parent)
        s = self.stored(self.parent)
        s.update(capture_pending=True, capture_failure_phase="source", capture_path=str(plain),
                 capture_error=f"FileNotFoundError: [Errno 2] No such file or directory: '{plain}'")
        path.write_text(json.dumps(s, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        advice = it.status("codex", self.parent)["capture_recovery"]
        self.assertEqual((advice["state"], advice["phase"], advice["basis"]), ("retryable", "source", "source_lookup"))
        self.assertIn("Codex 압축본 포함", advice["next_action"])
        # The fingerprint did not change, yet the error is retried and cleared.
        [due] = it._known_pending(None)[0]
        self.assertEqual(due["conversation_id"], self.parent)
        self.assertEqual(it.catchup(1)["jobs"], [])
        cleared = it.status("codex", self.parent)
        self.assertEqual((cleared["ok"], cleared["capture_error"], cleared["capture_recovery"]["state"]),
                         (True, None, "ready"))
        # With neither form on this device the advice says so.
        transcripts.on_disk(plain).unlink()
        s = self.stored(self.parent)
        s.update(capture_pending=True, capture_failure_phase="source", capture_error="FileNotFoundError: gone")
        path.write_text(json.dumps(s, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        advice = it.status("codex", self.parent)["capture_recovery"]
        self.assertEqual(advice["state"], "action_required")
        self.assertIn("Codex 압축본(`.jsonl.zst`)으로도 없다", advice["next_action"])

    def test_an_unchanged_source_with_another_error_is_not_read_again(self):
        plain = self.page(self.parent, self.parent, turn(self.parent, "p1"))
        with plain.open("a", encoding="utf-8") as f:
            f.write('{"type": "event_msg", "payl')      # a tail the writer never finished
        first = it.capture("codex", self.parent, str(plain), "forks")
        self.assertIn("incomplete JSONL tail", first["capture_error"])
        it.acknowledge("codex", self.parent, first["through"], "no_value", "synthetic fixture")
        compress(plain)
        # The error stands, but nothing changed: the next catch-up does not decompress it again.
        self.assertEqual(it.status("codex", self.parent)["capture_recovery"]["state"], "action_required")
        self.assertEqual(it._known_pending(None)[0], [])

    def test_a_missing_decompressor_is_named_not_taken_for_a_lost_source(self):
        plain = self.page(self.parent, self.parent, turn(self.parent, "p1"))
        it.capture("codex", self.parent, str(plain), "forks")
        zst = compress(plain)
        with mock.patch.object(transcripts, "_zstd", side_effect=transcripts.ZstdMissing("no zstandard")):
            failed = it.capture("codex", self.parent, None, "forks")
            self.assertEqual(it._known_pending(None)[2][0]["phase"], "native_lookup")
        self.assertFalse(failed["ok"])
        self.assertEqual((failed["capture_recovery"]["state"], failed["capture_recovery"]["phase"]),
                         ("action_required", "dependency"))
        self.assertIn("zstandard", failed["capture_recovery"]["next_action"])
        self.assertEqual(self.stored(self.parent)["capture_path"], str(zst.resolve()))   # kept for the retry
        self.assertTrue(it.capture("codex", self.parent, None, "forks")["ok"])


class RevertedTurnTests(unittest.TestCase):
    """Codex `thread/revert` continues on a new page that keeps the old one only up to a turn (#208)."""
    setUp, page = CodexForkTests.setUp, CodexForkTests.page

    def revert(self, sid, *, reviewed=False):
        """Page A holds t1·t2 and the cursor tracks both; page B keeps A up to t2 and adds t2b."""
        old = self.page(sid, sid + "-a", turn(sid, "t1"))
        bound = old.stat().st_size
        first = it.capture("codex", sid, str(old), "forks")
        with old.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in turn(sid, "t2")))
        second = it.capture("codex", sid, str(old), "forks")
        if reviewed:
            it.acknowledge("codex", sid, second["through"], "no_value", "synthetic fixture")
        new = self.page(sid, sid + "-b", turn(sid, "t2b"), base=(sid + "-a", bound))
        return first, second, old, new

    def test_an_unreviewed_reverted_turn_leaves_the_cursor(self):
        first, second, _, new = self.revert(self.parent)
        found = it.capture("codex", self.parent, str(new), "forks")
        self.assertTrue(found["ok"], found)
        self.assertEqual(transcripts.read(str(new), "codex", self.parent)["reverted"], ["t2"])
        self.assertEqual((found["captured_rounds"], found["appended"], found["reverted"]), (2, 1, 1))
        self.assertEqual(found["pending_refs"], [raw.native_ref("codex", self.parent, t) for t in ("t1", "t2b")])
        # The snapshot that held t2 is gone; the one before it still closes t1.
        snapshots = json.loads(it.state_path("codex", self.parent).read_text(encoding="utf-8"))["snapshots"]
        self.assertEqual(set(snapshots), {first["through"], found["through"]})
        with self.assertRaisesRegex(ValueError, "not an unreviewed snapshot"):
            it.acknowledge("codex", self.parent, second["through"], "no_value", "synthetic fixture")
        self.assertEqual(it.capture("codex", self.parent, None, "forks")["appended"], 0)

    def test_a_reviewed_reverted_turn_is_refused_by_name(self):
        _, second, _, new = self.revert(self.parent, reviewed=True)
        before = json.loads(it.state_path("codex", self.parent).read_text(encoding="utf-8"))["rounds"]
        found = it.capture("codex", self.parent, str(new), "forks")
        self.assertFalse(found["ok"])
        self.assertIn("Codex reverted a reviewed turn", found["capture_error"])
        after = json.loads(it.state_path("codex", self.parent).read_text(encoding="utf-8"))["rounds"]
        self.assertEqual(after, before)

    def test_a_missing_turn_that_was_not_reverted_is_still_refused(self):
        _, _, old, _ = self.revert(self.parent)
        # The old page itself loses t2: no later page declares a bound, so nothing reverted it.
        old.write_text("".join(json.dumps(r) + "\n" for r in [event("session_meta", id=self.parent)]
                               + turn(self.parent, "t1")), encoding="utf-8")
        found = it.capture("codex", self.parent, str(old), "forks")
        self.assertIn("identity prefix changed", found["capture_error"])

    def test_response_growth_drops_the_reverted_turn_and_keeps_its_stop(self):
        from osk import response_growth
        _, _, old, new = self.revert(self.parent)
        self.assertEqual(response_growth.observe(response_growth.profile(str(old), "codex", self.parent))["count"], 0)
        source = response_growth.profile(str(new), "codex", self.parent)
        self.assertEqual(response_growth.observe(source)["count"], 1)
        self.assertEqual((source["finals"], source["reverted"]), (["t1", "t2b"], ["t2"]))
        saved = json.loads(it.state_path("codex", self.parent).read_text(encoding="utf-8"))["response_growth"]
        self.assertEqual(saved["seen"], ["t1", "t2b"])

    def test_a_legacy_cursor_whose_every_seen_turn_was_reverted_keeps_its_baseline(self):
        from osk import response_growth
        _, _, _, new = self.revert(self.parent)
        # An older engine saw only its current page: its cursor starts at t2, after t1.
        path = it.state_path("codex", self.parent)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["response_growth"] = {"counter": "finals", "seen": ["t2"], "count": 3, "attempted_count": 0}
        path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        # Only t2b is new; t1 came before the cursor and is no new Stop.
        self.assertEqual(response_growth.observe(response_growth.profile(str(new), "codex", self.parent))["count"], 4)

    def test_a_fork_parents_later_turns_are_not_reverted(self):
        parent = self.page(self.parent, self.parent, turn(self.parent, "p1"))
        bound = parent.stat().st_size
        with parent.open("a", encoding="utf-8") as f:   # the parent goes on past the fork
            f.write("".join(json.dumps(r) + "\n" for r in turn(self.parent, "p2")))
        child = self.page(self.child, self.child + "-a", turn(self.child, "c1"),
                          base=(self.parent, bound), fork=self.parent)
        own = child.stat().st_size
        with child.open("a", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in turn(self.child, "c2")))
        self.assertEqual(transcripts.read(str(child), "codex", self.child)["reverted"], [])
        # The child's own revert counts; the parent's later turn still does not.
        new = self.page(self.child, self.child + "-b", turn(self.child, "c2b"),
                        base=(self.child + "-a", own), fork=self.parent)
        self.assertEqual(transcripts.read(str(new), "codex", self.child)["reverted"], ["c2"])


if __name__ == "__main__":
    unittest.main()
