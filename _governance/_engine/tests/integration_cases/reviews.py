"""대화 포착과 검토의 수트의 한 갈래: 검토와 영수증 — 확인, 기존 증거의 발견, 묶음.

`tests/test_integration.py`의 `IntegrationTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import json
import os
import time
from pathlib import Path
from unittest import mock

from integration_cases.support import claude_round, evidenced, legacy_cursor, ROOT, stamped, TMP, VAULT
from osk import (integration as it, raw, scope_memory, write, nodefile, conversation_capture,  # noqa: E402
                 conversation_cite, conversation_review, conversation_state)  # noqa: E402


class ReviewCases:
    def test_resume_empty_memory_and_other_writer_never_ack(self):
        self.transcript(claude_round(self.sid, 1))
        st = self.capture()
        for _ in range(8):
            self.assertFalse(conversation_capture.tick(VAULT, "claude", self.sid)["due"])
        self.assertTrue(conversation_capture.tick(VAULT, "claude", self.sid)["due"])
        self.assertIn("공유 기억이 비어 있어도", it.prompt(VAULT, "claude", self.sid)["text"])
        cur = scope_memory.read(VAULT, "capture-tests")
        scope_memory.replace(VAULT, "other-writer", "other session writes shared memory", cur["hash"], "00_Scope/Capture")
        resumed = conversation_capture.hook_capture(VAULT, {"harness": "claude", "session_id": self.sid, "transcript_path": str(self.path)}, "capture-tests")
        self.assertEqual(resumed["prompt_count"], 9)
        self.assertIsNone(resumed["reviewed_through"])
        self.assertTrue(resumed["pending"])
        other = conversation_capture.status(VAULT, "claude", self.sid + "-other")
        self.assertFalse(other["pending"])
        self.assertNotIn(st["pending_refs"][0], it.prompt(VAULT, "claude", self.sid + "-other")["text"])

    def test_exact_snapshot_ack_leaves_newer_round_pending(self):
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        conversation_capture.tick(VAULT, "claude", self.sid)
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        latest = self.capture()
        deferred = conversation_review.acknowledge(VAULT, "claude", self.sid, first["through"], "deferred", "need a source check")
        self.assertEqual(deferred["reviewed_rounds"], 0)
        ack = conversation_review.acknowledge(VAULT, "claude", self.sid, first["through"], "no_value", "first round is transient")
        self.assertEqual(ack["reviewed_rounds"], 1)
        self.assertEqual(ack["pending_refs"], latest["pending_refs"][1:])
        self.assertFalse(ack["last_review"]["mechanical_preservation"])
        with self.assertRaises(ValueError):
            conversation_review.acknowledge(VAULT, "claude", self.sid + "-other", latest["through"], "no_value", "cannot acknowledge another conversation")

    def test_summary_is_verified_and_not_node_success(self):
        self.transcript(claude_round(self.sid, 1))
        st = self.capture()
        with self.assertRaises(ValueError):
            conversation_review.acknowledge(VAULT, "claude", self.sid, st["through"], "summary", "summary only", [{"text": "missing excerpt"}])
        cur = scope_memory.read(VAULT, "capture-tests")
        scope_memory.replace(VAULT, "capture-tests", "an exact saved summary", cur["hash"])
        done = conversation_review.acknowledge(VAULT, "claude", self.sid, st["through"], "summary", "temporary summary is sufficient", [{"text": "exact saved summary"}])
        self.assertFalse(done["pending"])
        self.assertFalse(done["last_review"]["mechanical_preservation"])
        self.assertFalse(done["last_review"]["semantic_verified"])

    def test_changed_original_turn_never_closes_its_old_snapshot(self):
        rows = claude_round(self.sid, 1) + claude_round(self.sid, 2)
        second = len(claude_round(self.sid, 1))
        self.transcript(rows)
        first = self.capture()
        ref = raw.native_ref("claude", self.sid, "user-2")
        # The same turn ID now carries other words.
        rows[second]["message"]["content"] = "question 2, for every project"
        self.transcript(rows)
        [seen] = conversation_cite.read_turns(VAULT, [ref])["turns"]
        self.assertTrue(seen["changed"], seen)
        with self.assertRaisesRegex(ValueError, "changed after this snapshot"):
            conversation_review.acknowledge(VAULT, "claude", self.sid, first["through"], "no_value", "judged the old words")
        again = self.capture()
        self.assertEqual((again["ok"], again["appended"], again["changed"]), (True, 0, 1))
        self.assertNotEqual(again["through"], first["through"])
        [seen] = conversation_cite.read_turns(VAULT, [ref])["turns"]
        self.assertNotIn("changed", seen)
        self.assertTrue(seen["hash"])
        with self.assertRaisesRegex(ValueError, "not an unreviewed snapshot"):
            conversation_review.acknowledge(VAULT, "claude", self.sid, first["through"], "no_value", "judged the old words")
        done = conversation_review.acknowledge(VAULT, "claude", self.sid, again["through"], "no_value", "judged the current words")
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
            conversation_review.acknowledge(VAULT, "claude", self.sid, third["through"], "no_value", "unread")
        conversation_review.acknowledge(VAULT, "claude", self.sid, third["through"], "deferred", "the transcript is away")


    def test_review_manifests_are_bounded_and_find_root_specific_states(self):
        self.transcript([r for n in range(1, 19) for r in claude_round(self.sid, n)])
        self.capture()
        job = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertEqual(len(job["pending_refs"]), 15)
        self.assertEqual(job["remaining_rounds"], 3)
        ack = conversation_review.acknowledge(VAULT, "claude", self.sid, job["through"], "no_value", "first fifteen reviewed")
        self.assertEqual(ack["reviewed_rounds"], 15)
        self.assertEqual(len(ack["pending_refs"]), 3)
        # .git directories and nongit fallback retain the identical root prefix.
        expected = conversation_state.state_path(VAULT, "claude", "inventory").name.split("-")[2]
        self.assertEqual(conversation_state.state_path(VAULT, "claude", self.sid).name.split("-")[2], expected)
        (ROOT / ".git").mkdir()
        try:
            git_sid = self.sid + "-git"
            git_path = Path(TMP.name) / (git_sid + ".jsonl")
            git_path.write_text("".join(json.dumps(r) + "\n" for r in claude_round(git_sid, 1)), encoding="utf-8")
            conversation_capture.capture(VAULT, "claude", git_sid, str(git_path), "capture-tests")
            git_path.write_text("".join(json.dumps(r) + "\n" for r in
                                        claude_round(git_sid, 1) + claude_round(git_sid, 2)), encoding="utf-8")
            states, _, _, _ = it._known_pending(VAULT, 100)
            self.assertIn(git_sid, [s["conversation_id"] for s in states])
        finally:
            import shutil
            shutil.rmtree(ROOT / ".git")


    def test_actual_preserved_scope_node_ack_and_later_receipt_validation(self):
        from osk import contract, distillation as D, evidence
        self.transcript(claude_round(self.sid, 1))
        st = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, space="00_Scope/W1")
        ref = conversation_cite.cite(VAULT, f"claude/{self.sid}", quote="question 1")["round_ref"]
        spec = {"key": self.sid, "sources": [ref], "hub": "W1"}
        args = {"title": "A retained integration decision", "summary": "bounded retry",
                "body": evidenced("A retry is identified by native identity rather than repeated wording.", ref),
                "drafter": "fable-5", "space": "00_Scope/W1"}
        created = D.create_node(VAULT, spec, **args)
        self.assertEqual(created["distillation"]["status"], "complete")
        node = contract.parse(VAULT.root / created["path"])
        hub = contract.parse(VAULT.root / "00_Scope/W1/W1.md")
        self.assertEqual(evidence.cited_rounds(VAULT, node.body), {raw.canonical_ref(VAULT, ref)})
        self.assertNotIn("derived-from", node.meta)   # a round is a 근거 line, not an edge
        self.assertIn(created["name"], hub.wikilinks())
        # The cited round binds the review of the original turn it cites.
        ack = conversation_review.acknowledge(VAULT, "claude", self.sid, st["through"], "preserved",
                             "The cited observation is retained in the existing project cluster.",
                             [{"key": spec["key"]}])
        self.assertFalse(ack["pending"])
        self.assertEqual(ack["reviewed_rounds"], 1)
        self.assertTrue(ack["last_review"]["mechanical_preservation"])
        self.assertFalse(ack["last_review"]["semantic_verified"])
        self.assertEqual(conversation_review.review_status(VAULT, "claude", self.sid, st["through"])["status"], "complete")
        sibling = D.create_node(VAULT, dict(spec, key=self.sid + "-sibling"),
                                **dict(args, title="A sibling retained decision",
                                       body=evidenced("Another reusable observation.", ref)))
        self.assertEqual(sibling["distillation"]["status"], "complete")
        self.assertEqual(conversation_review.review_status(VAULT, "claude", self.sid, st["through"])["status"], "complete")
        # After ACK only structure is observed: a body edit keeps the review closed.
        changed = write.update_node(VAULT, created["id"], body=evidenced("Changed after review.", ref),
                                    expect_hash=created["new_hash"])
        after = conversation_review.review_status(VAULT, "claude", self.sid, st["through"])
        self.assertEqual(after["status"], "complete")
        self.assertNotIn("structure", after)
        # Dropping the source is reported, still without reopening the conversation's review.
        write.update_node(VAULT, created["id"], body="Changed after review.", expect_hash=changed["new_hash"])
        broken = conversation_review.review_status(VAULT, "claude", self.sid, st["through"])
        self.assertEqual(broken["status"], "complete")
        self.assertEqual(broken["structure"], [{"key": spec["key"], "reason": "target no longer cites its source"}])
        self.assertFalse(conversation_capture.status(VAULT, "claude", self.sid)["pending"])


    def test_existing_saved_record_name_is_preserved(self):
        self.transcript(claude_round(self.sid, 1))
        path = conversation_state.state_path(VAULT, "claude", self.sid)
        state = conversation_state.load_state(VAULT, path, "claude", self.sid)
        state["record"] = "legacy-record-name"
        conversation_state.save_state(path, state)
        st = self.capture()
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["record"], "legacy-record-name")
        cited = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1")
        self.assertIn("/.records/legacy-record-name.txt#1", cited["round_ref"])


    def test_raw_migration_preserves_pending_snapshot_and_legacy_receipt_match(self):
        from osk import distillation as D
        self.transcript(claude_round(self.sid, 1))
        conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        state, physical = legacy_cursor("claude", self.sid, self.path, "W1")
        legacy = physical.parent.parent / (physical.stem + ".md")
        physical.rename(legacy)  # Simulate the pre-upgrade visible record and cursor.
        saved = legacy.read_bytes()
        old_ref = f"[[{legacy.relative_to(ROOT).as_posix()}#1]]"
        state_path = conversation_state.state_path(VAULT, "claude", self.sid)
        state["rounds"][0]["ref"] = old_ref
        token = conversation_state.snapshot(state)
        state["snapshots"] = {token: {"count": 1, "prompt_count": state["prompt_count"]}}
        conversation_state.save_state(state_path, state)
        replayed = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid)
        self.assertTrue(replayed["ok"], replayed)
        self.assertEqual(replayed["through"], token)
        self.assertEqual(replayed["pending_refs"], [old_ref])
        self.assertEqual(replayed["appended"], 0)
        self.assertEqual(legacy.read_bytes(), saved)  # tracking never rewrites a record
        moved = raw.migrate(VAULT, apply=True)
        self.assertIn(legacy.relative_to(ROOT).as_posix(), [f["from"] for f in moved["files"]])
        self.assertFalse(legacy.exists())
        self.assertEqual(physical.read_bytes(), saved)
        out = D.create_node(VAULT, {"key": self.sid, "sources": [old_ref], "hub": "W1"},
                            title=self.sid, summary="retained observation",
                            body=evidenced("Observation retained with its original source.", old_ref),
                            drafter="test-model", space="00_Scope/W1")
        self.assertEqual(out["distillation"]["status"], "complete")
        ack = conversation_review.acknowledge(VAULT, "claude", self.sid, token, "preserved", "fixture retained",
                             [{"key": self.sid}])
        self.assertTrue(ack["ok"], ack)


    def test_review_key_tracks_snapshot_and_recovers_saved_proof(self):
        from osk import distillation as D
        self.transcript(claude_round(self.sid, 1))
        conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        first = it.prompt(VAULT, "claude", self.sid)
        self.assertEqual(first["key"], it.prompt(VAULT, "claude", self.sid)["key"])
        cited = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1")["round_ref"]
        spec = {"key": first["key"] + ":stable-target", "sources": [cited], "hub": "W1"}
        saved = D.create_node(VAULT, spec, title="Snapshot-specific retained observation", summary="native observation",
                              body=evidenced("The observation is preserved before its review acknowledgement arrives.",
                                             cited),
                              drafter="fable-5", space="00_Scope/W1")
        self.assertEqual(saved["distillation"]["status"], "complete")
        retry = it.prompt(VAULT, "claude", self.sid)
        self.assertEqual(first["key"], retry["key"])
        proof = next(p for p in retry["previous_distillations"] if p["key"] == spec["key"])
        self.assertEqual(proof["status"], "complete")
        self.assertIn(spec["key"], retry["text"])
        self.assertIn("대상별 고정 접미사", retry["text"])
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid)
        later = it.prompt(VAULT, "claude", self.sid)
        self.assertNotEqual(later["through"], first["through"])
        self.assertNotEqual(later["key"], first["key"])
        self.assertEqual(later["key"], it.prompt(VAULT, "claude", self.sid)["key"])
        self.assertTrue(any(p["key"] == spec["key"] for p in later["previous_distillations"]))


    def test_worker_batch_does_not_reduce_ordinary_cadence_or_ack_the_tail(self):
        self.transcript([row for n in range(20) for row in claude_round(self.sid, n)])
        original = self.capture()
        worker = it.prompt(VAULT, "claude", self.sid, include_organization=False, max_rounds=3)
        ordinary = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertEqual(len(worker["pending_refs"]), 3)
        self.assertEqual(len(ordinary["pending_refs"]), 15)
        self.assertNotEqual(worker["through"], ordinary["through"])
        self.assertEqual(conversation_capture.status(VAULT, "claude", self.sid)["pending_refs"], original["pending_refs"])
        conversation_review.acknowledge(VAULT, "claude", self.sid, worker["through"], "no_value", "Only fixture questions in selected first three rounds.")
        pending = conversation_capture.status(VAULT, "claude", self.sid)["pending_refs"]
        self.assertEqual(pending, original["pending_refs"][3:])
        self.assertEqual(it.prompt(VAULT, "claude", self.sid, max_rounds=3)["pending_refs"], pending[:3])


    def test_saved_scope_without_ack_is_discovered_and_reused(self):
        from osk import graph
        st = self._discovery_source()
        key = self.sid + "-opaque-worker-key"
        saved = self._discovery_save(st, key)
        target = VAULT.root / saved["path"]
        before = target.read_bytes()
        own = it.prompt(VAULT, "claude", self.sid)
        proofs = own["previous_distillations"]
        self.assertEqual([proof["key"] for proof in proofs], [key])
        self.assertEqual(proofs[0]["status"], "complete")
        nodes_before = set(graph.Index(VAULT).nodes)
        ack = conversation_review.acknowledge(VAULT, "claude", self.sid, own["through"], "preserved",
                             "Reuse the verified saved decision; the previous ACK was lost.",
                             [{"key": proofs[0]["key"]}])
        self.assertFalse(ack["pending"])
        self.assertEqual(conversation_review.review_status(VAULT, "claude", self.sid, own["through"])["status"], "complete")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(set(graph.Index(VAULT).nodes), nodes_before)


    def test_discovery_excludes_other_conversation_and_root(self):
        import hashlib
        from osk import distillation as D
        own = self._discovery_source()
        key = self.sid + "-own"
        self._discovery_save(own, key)
        other_sid = self.sid + "-other"
        other_path = Path(TMP.name) / (other_sid + ".jsonl")
        other_path.write_text("".join(json.dumps(row) + "\n" for row in claude_round(other_sid, 1)), encoding="utf-8")
        other = conversation_capture.capture(VAULT, "claude", other_sid, str(other_path), other_sid, "00_Scope/W1")
        other["cited"] = conversation_cite.cite(VAULT, f"claude/{other_sid}", turn="-1")["round_ref"]
        other_key = self.sid + "-unrelated"
        self._discovery_save(other, other_key, self.sid + "-other-node")
        # Model a journal in the same common git directory with a different ROOT.
        # Its valid body/source bytes would pass if canonical filename binding
        # were omitted. The key is never exposed to this conversation.
        foreign_key = self.sid + "-foreign-root"
        foreign = dict(D._load(VAULT, key), key=foreign_key)
        digest = hashlib.sha256((str(ROOT / "other-root") + "\0" + foreign_key).encode()).hexdigest()
        own_path = D._job_path(VAULT, foreign_key)
        own_digest = hashlib.sha256((str(VAULT.root) + "\0" + foreign_key).encode()).hexdigest()
        foreign_path = own_path.with_name(own_path.name.replace(own_digest, digest))
        self.assertNotEqual(foreign_path, D._job_path(VAULT, foreign_key))
        self.addCleanup(foreign_path.unlink, missing_ok=True)
        nodefile.write_file(foreign_path, json.dumps(foreign).encode())
        found = D.discover(VAULT, own["pending_refs"])
        self.assertEqual([proof["key"] for proof in found["proofs"]], [key])
        self.assertFalse(found["errors"], found)
        self.assertNotIn(other_key, json.dumps(it.prompt(VAULT, "claude", self.sid)))
        self.assertNotIn(foreign_key, json.dumps(it.prompt(VAULT, "claude", self.sid)))


    def test_uncited_pending_turns_need_no_receipt_scan(self):
        # A proof binds an original turn only through its cited round. With 891 receipts the
        # scan of the newest 256 made every review prompt say the lookup was incomplete (#227).
        from osk import distillation as D
        other_sid = self.sid + "-other"
        other_path = Path(TMP.name) / (other_sid + ".jsonl")
        other_path.write_text("".join(json.dumps(row) + "\n" for row in claude_round(other_sid, 1)), encoding="utf-8")
        other = conversation_capture.capture(VAULT, "claude", other_sid, str(other_path), other_sid, "00_Scope/W1")
        other["cited"] = conversation_cite.cite(VAULT, f"claude/{other_sid}", turn="-1")["round_ref"]
        for n in range(2):
            self._discovery_save(other, f"{self.sid}-other-{n}", f"{self.sid}-other-node-{n}")
        self.transcript(claude_round(self.sid, 1))
        own = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        self.assertEqual(D.discover(VAULT, own["pending_refs"], scan_limit=1),
                         {"proofs": [], "errors": [], "scanned": 0, "truncated": False})
        self.assertNotIn("조회가 불완전", it.prompt(VAULT, "claude", self.sid)["text"])
        # A cited turn of unknown time (these rows carry no timestamp) may have a proof among
        # the older receipts: the count-bounded scan says so.
        conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1")
        found = D.discover(VAULT, own["pending_refs"], scan_limit=1)
        self.assertEqual((found["scanned"], found["truncated"]), (1, True), found)


    def test_receipts_older_than_the_pending_turns_are_not_scanned(self):
        # A receipt cannot predate the turn it cites: the scan stops a day before the earliest
        # pending turn, so a long receipt history alone no longer reads incomplete (#227).
        from osk import distillation as D
        other_sid = self.sid + "-older"
        other_path = Path(TMP.name) / (other_sid + ".jsonl")
        other_path.write_text("".join(json.dumps(row) + "\n" for row in claude_round(other_sid, 1)), encoding="utf-8")
        other = conversation_capture.capture(VAULT, "claude", other_sid, str(other_path), other_sid, "00_Scope/W1")
        other["cited"] = conversation_cite.cite(VAULT, f"claude/{other_sid}", turn="-1")["round_ref"]
        self._discovery_save(other, f"{self.sid}-older", f"{self.sid}-older-node")
        receipt = D._job_path(VAULT, f"{self.sid}-older")
        # The turn is stamped two days ahead, so every receipt on disk (this suite's too) is older.
        at = time.time() + 2 * 86400
        self.transcript(stamped(claude_round(self.sid, 1), at))
        conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1")
        st = it.prompt(VAULT, "claude", self.sid)
        self.assertEqual(st["proof_discovery"], {"errors": [], "truncated": False, "scanned": 0})
        self.assertNotIn("조회가 불완전", st["text"])
        # A receipt written after the turn is still read.
        os.utime(receipt, (at + 60, at + 60))
        self.assertEqual(it.prompt(VAULT, "claude", self.sid)["proof_discovery"]["scanned"], 1)
        # A pending turn of unknown time leaves no bound: the older receipts are read again.
        os.utime(receipt)
        self.transcript(stamped(claude_round(self.sid, 1), at) + claude_round(self.sid, 2))
        conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        self.assertGreaterEqual(it.prompt(VAULT, "claude", self.sid)["proof_discovery"]["scanned"], 1)


    def test_discovered_pending_hub_resumes_without_body_rewrite(self):
        from osk import distillation as D
        st = self._discovery_source()
        key = self.sid + "-interrupted"
        atomic = nodefile.write_file
        hub = ROOT / "00_Scope/W1/W1.md"
        def fail_hub(path, data):
            if path == hub:
                raise OSError("injected lost hub write")
            return atomic(path, data)
        with mock.patch.object(nodefile, "write_file", side_effect=fail_hub):
            saved = self._discovery_save(st, key)
        self.assertTrue(saved["ok"])
        before = (ROOT / saved["path"]).read_bytes()
        prompt = it.prompt(VAULT, "claude", self.sid)
        proof = next(proof for proof in prompt["previous_distillations"] if proof["key"] == key)
        self.assertEqual(proof["status"], "pending")
        self.assertIn("distill={resume:proof.key}", prompt["text"])
        repaired = D.update_node(VAULT, {"resume": proof["key"]}, name=proof["target"]["id"])
        self.assertEqual(repaired["distillation"]["status"], "complete")
        ack = conversation_review.acknowledge(VAULT, "claude", self.sid, prompt["through"], "preserved",
                             "Recovered the saved target and its hub link.", [{"key": proof["key"]}])
        self.assertFalse(ack["pending"])
        self.assertEqual((ROOT / saved["path"]).read_bytes(), before)

    def test_discovery_errors_are_visible_and_stale_proof_is_not_reused(self):
        from osk import distillation as D
        st = self._discovery_source()
        key = self.sid + "-saved"
        saved = self._discovery_save(st, key)
        write.update_node(VAULT, saved["id"], body="A later writer changed this decision.",
                          expect_hash=saved["new_hash"])
        damaged = D._job_path(VAULT, self.sid + "-damaged")
        self.addCleanup(damaged.unlink, missing_ok=True)
        nodefile.write_file(damaged, b"{bad-json")
        prompt = it.prompt(VAULT, "claude", self.sid)
        self.assertEqual(prompt["previous_distillations"], [])
        self.assertTrue(prompt["proof_discovery"]["errors"])
        self.assertIn("조회가 불완전", prompt["text"])
        self.assertTrue(prompt["pending"])

    def test_a_review_behind_names_its_range_reads_it_once_and_refuses_a_later_turn_by_name(self):
        # #219: the reviewer's latest turn is past a review that fell behind. The prompt names the
        # range, one call reads it, a short ID cites, and the refusal says the cited turn is later.
        from osk import distillation as D
        import mcp_server
        rows = []
        for n in range(1, 5):
            r = claude_round(self.sid, n)
            r[0]["uuid"] = f"aaaaaaaa-{n}-turn"
            r[0]["message"]["content"] = f"question {n} " + "w" * 2000
            rows += r
        self.transcript(rows)
        conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        conversation = f"claude/{self.sid}"
        st = it.prompt(VAULT, "claude", self.sid, include_organization=False, max_rounds=2)
        self.assertIn("1–2번째 턴 2개(`aaaaaaaa-1` … `aaaaaaaa-2`, 전체 4턴)", st["text"])
        self.assertIn(f'read_cited(conversation="{conversation}", through=', st["text"])
        read = mcp_server.read_cited(conversation=conversation, through=st["through"])
        self.assertTrue(read["ok"], read)
        self.assertEqual([(t["short"], t["position"]) for t in read["turns"]], [("aaaaaaaa-1", 1), ("aaaaaaaa-2", 2)])
        self.assertTrue(all(t["budget_sides"] == {"user": 1500, "agent": 1500} and len(t["text"]) <= 3000
                            and "question" in t["text"] for t in read["turns"]), read)
        self.assertFalse(mcp_server.read_cited(ref="x", conversation=conversation, through=st["through"])["ok"])
        with self.assertRaisesRegex(ValueError, "4 turns match"):
            conversation_cite.cite(VAULT, conversation, turn="aaaaaaaa")
        late = conversation_cite.cite(VAULT, conversation, turn="aaaaaaaa-4")
        self.assertEqual(late["turn"], "aaaaaaaa-4-turn")
        key = self.sid + "-late"
        self.addCleanup(D._job_path(VAULT, key).unlink, missing_ok=True)
        D.create_node(VAULT, {"key": key, "sources": [late["round_ref"]], "hub": "W1"}, title=self.sid,
                      summary="a later turn", body=evidenced("Knowledge from the latest turn.", late["round_ref"]),
                      drafter="test-model",
                      space="00_Scope/W1")
        with self.assertRaisesRegex(ValueError, r"cites no turn of this review \(1–2번째 턴 2개.*it cites aaaaaaaa-4"):
            conversation_review.acknowledge(VAULT, "claude", self.sid, st["through"], "preserved", "Late.", [{"key": key}])
        with self.assertRaisesRegex(ValueError, "no complete body/source/hub receipt: unknown job"):
            conversation_review.acknowledge(VAULT, "claude", self.sid, st["through"], "preserved", "Lost.", [{"key": self.sid + "-missing"}])
        # The range closes on its own; the later distillation waits for its own turn's review.
        conversation_review.acknowledge(VAULT, "claude", self.sid, st["through"], "no_value", "fixture questions only")
        rest = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertIn("3–4번째 턴 2개", rest["text"])
        ack = conversation_review.acknowledge(VAULT, "claude", self.sid, rest["through"], "preserved", "Later turn kept.", [{"key": key}])
        self.assertFalse(ack["pending"])

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
        online = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertEqual(online["pending_refs"], st["pending_refs"])
        self.assertIn("read_cited", online["text"])
        self.assertIn(f'cite_round(conversation="claude/{self.sid}"', online["text"])
        # An offline worker has only the stored rounds.
        job = next(j for j in it.list_pending(VAULT, 100)["jobs"] if j["conversation_id"] == self.sid)
        self.assertEqual(job["pending_refs"], stored)
        self.assertEqual(job["raw_review"]["state"], "verified")
        self.assertNotIn("cite_round", job["prompt"])
        conversation_review.acknowledge(VAULT, "claude", self.sid, job["through"], "no_value", "stored fixture rounds only")
        self.assertNotIn(self.sid, [j["conversation_id"] for j in it.list_pending(VAULT, 100)["jobs"]])
        rest = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertEqual((rest["pending_refs"], rest["raw_review"]["state"]), ([turn], "native"))
        # A same-name record in `_raw/` stays the record and takes the new citation.
        cited = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1")
        self.assertEqual(cited["path"], record.relative_to(ROOT).as_posix())
        self.assertEqual(cited["index"], 3)

    def test_batch_reads_only_the_unreviewed_turns_of_an_ended_conversation(self):
        import mcp_server
        # Inventory only this test's conversations.
        state_path_for = conversation_state.state_path
        state_dir = Path(TMP.name) / self.sid
        patcher = mock.patch.object(conversation_state, "state_path", side_effect=lambda vault, harness, sid:
                                    state_dir / state_path_for(vault, harness, sid).name)
        patcher.start()
        self.addCleanup(patcher.stop)
        talks = {}
        for name, count in (("done", 2), ("short", 5), ("running", 3)):
            sid = f"{self.sid}-{name}"
            path = Path(TMP.name) / f"{sid}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for n in range(1, count + 1)
                                    for r in claude_round(sid, n)), encoding="utf-8")
            st = conversation_capture.capture(VAULT, "claude", sid, str(path), "capture-tests")
            self.assertTrue(st["ok"], st)
            talks[name] = sid, path, st
        sid, path, st = talks["done"]
        conversation_review.acknowledge(VAULT, "claude", sid, st["through"], "no_value", "fixture turns only")
        sid, path, st = talks["short"]
        first = it.prompt(VAULT, "claude", sid, include_organization=False, max_rounds=2)
        conversation_review.acknowledge(VAULT, "claude", sid, first["through"], "no_value", "first two fixture turns")
        ended = time.time() - conversation_capture.ENDED_AFTER - 60
        for name in ("done", "short"):
            os.utime(talks[name][1], (ended, ended))
        jobs = {j["conversation_id"]: j for j in it.list_pending(VAULT, 100)["jobs"]}
        # A fully reviewed conversation is not read; a running one keeps its own cadence.
        self.assertEqual(set(jobs), {sid})
        job = jobs[sid]
        self.assertEqual(job["pending_refs"], [raw.native_ref("claude", sid, f"user-{n}") for n in (3, 4, 5)])
        # A sandboxed worker reads them through MCP, not the CLI.
        self.assertIn("read_cited(ref=", job["prompt"])
        self.assertNotIn("read_command", job)
        read = conversation_cite.read_turns(VAULT, job["pending_refs"])["turns"]
        self.assertEqual([t["position"] for t in read], [3, 4, 5])
        self.assertTrue(all(f"question {t['position']}" in t["text"] for t in read), read)
        self.assertNotIn("question 2", json.dumps(read))
        self.assertEqual(read[0]["previous"], raw.native_ref("claude", sid, "user-2"))
        # Earlier context only on demand, one turn back at a time.
        [back] = conversation_cite.read_turns(VAULT, [read[0]["previous"]])["turns"]
        self.assertEqual((back["position"], back["previous"]), (2, raw.native_ref("claude", sid, "user-1")))
        out = mcp_server.read_cited(job["pending_refs"][0], view="full")
        self.assertEqual((out["ok"], out["position"]), (True, 3), out)
        self.assertIn("answer 3", out["text"])
        with self.assertRaisesRegex(ValueError, "not a completed turn"):
            conversation_cite.read_turns(VAULT, [raw.native_ref("claude", sid, "user-9")])
        done = conversation_review.acknowledge(VAULT, "claude", sid, job["through"], "no_value", "ended fixture turns")
        self.assertEqual((done["reviewed_rounds"], done["pending"]), (5, False))
        self.assertNotIn(sid, [j["conversation_id"] for j in it.list_pending(VAULT, 100)["jobs"]])

    def test_complete_review_restarts_the_conversation_cadence(self):
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2) + claude_round(self.sid, 3))
        self.capture()
        for _ in range(6):
            conversation_capture.tick(VAULT, "claude", self.sid)
        path = conversation_state.state_path(VAULT, "claude", self.sid)
        state = conversation_state.load_state(VAULT, path, "claude", self.sid)
        state["response_growth"] = {"counter": "finals", "seen": ["final"], "count": 6,
                                    "attempted_count": 0, "history_baselined": True}
        conversation_state.save_state(path, state)
        part = it.prompt(VAULT, "claude", self.sid, include_organization=False, max_rounds=2)
        conversation_review.acknowledge(VAULT, "claude", self.sid, part["through"], "no_value", "first two fixture turns")
        state = conversation_state.load_state(VAULT, path, "claude", self.sid)
        # A partial review leaves both counts running.
        self.assertEqual((state["reviewed_prompt_count"], state["response_growth"]["attempted_count"]), (2, 0))
        rest = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        conversation_review.acknowledge(VAULT, "claude", self.sid, rest["through"], "no_value", "last fixture turn")
        state = conversation_state.load_state(VAULT, path, "claude", self.sid)
        self.assertEqual((state["reviewed_prompt_count"], state["response_growth"]["attempted_count"]), (6, 6))
        self.assertEqual(conversation_capture.tick(VAULT, "claude", self.sid)["unreviewed_prompts"], 1)

    def test_no_value_closes_only_the_judged_turns_and_a_new_turn_reopens(self):
        key = self.sid + "-key"
        self.forget(self.sid)
        self.transcript(claude_round(self.sid, 1))
        st = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), key)
        done = conversation_review.acknowledge(VAULT, "claude", self.sid, st["through"], "no_value", "a one-off question")
        self.assertEqual((done["reviewed_rounds"], done["pending"], done["space"]), (1, False, None))
        self.assertIsNone(write.resolve_session(VAULT, key))
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        again = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), key)
        self.assertTrue(again["pending"], again)
        self.assertEqual(again["pending_refs"], [raw.native_ref("claude", self.sid, "user-2")])
        job = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertNotEqual(job["through"], st["through"])
        self.assertIn("시행령 §3 9항", job["text"])

    def test_catchup_tracks_every_waiting_conversation_before_it_selects(self):
        # Capturing costs no model. It used to stop once the job budget filled, so a conversation
        # reopened by a new turn behind it was never tracked and never selected.
        ended = time.time() - conversation_capture.ENDED_AFTER - 60
        talks = {}
        for name, at in (("oldest", 1_000_000_000), ("reopened", 1_000_100_000)):
            sid = f"{self.sid}-{name}"
            path = Path(TMP.name) / f"{sid}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in stamped(claude_round(sid, 1), at)),
                            encoding="utf-8")
            talks[name] = sid, path, conversation_capture.capture(VAULT, "claude", sid, str(path), "capture-tests")
            self.forget(sid)
        sid, path, st = talks["reopened"]
        conversation_review.acknowledge(VAULT, "claude", sid, st["through"], "no_value", "fixture turn")
        path.write_text("".join(json.dumps(r) + "\n" for r in stamped(
            claude_round(sid, 1) + claude_round(sid, 2), 1_000_100_000)), encoding="utf-8")
        for _, p, _ in talks.values():
            os.utime(p, (ended, ended))
        first = it.catchup(VAULT, 1)
        captured = {c["conversation_id"]: c for c in first["captures"]}
        self.assertEqual(captured[sid]["appended"], 1, first)
        self.assertEqual([j["conversation_id"] for j in first["jobs"]], [talks["oldest"][0]])
        job = first["jobs"][0]
        conversation_review.acknowledge(VAULT, "claude", job["conversation_id"], job["through"], "no_value", "fixture turn")
        self.assertEqual([j["conversation_id"] for j in it.catchup(VAULT, 1)["jobs"]], [sid])
