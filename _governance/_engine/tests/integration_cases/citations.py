"""대화 포착과 검토의 수트의 한 갈래: 인용과 비밀값 — 턴을 근거로 남기는 일.

`tests/test_integration.py`의 `IntegrationTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from integration_cases.support import claude_round, codex_round, codex_user_item, ENGINE, evidenced, ROOT, TMP, VAULT
from osk import (core, raw, transcripts, write, conversation_capture, conversation_cite,  # noqa: E402
                 conversation_review, conversation_state)  # noqa: E402


class CitationCases:
    def test_recited_turn_reports_the_stored_record_and_what_it_sees_now(self):
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2, finished=False))
        self.capture()
        first = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="user-2")
        self.assertIsNone(first["agent_sha256"])  # the open turn has no reply yet
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        again = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="user-2", note="a later gist")
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
        kept = conversation_cite.cite(VAULT, f"claude/{self.sid}", user="question 1", turn="user-1")
        self.assertEqual(kept["user_by"], "caller")
        away.rename(self.path)
        back = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="user-1")
        self.assertEqual((back["reused"], back["user_by"]), (True, "caller"))
        self.assertEqual(back["observed"]["user_by"], "engine")
        self.assertNotIn("note_saved", back)


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
                st = conversation_capture.capture(VAULT, harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertFalse(st["capture_pending"])
                self.assertEqual(st["captured_rounds"], 1)
                cited = conversation_cite.cite(VAULT, f"{harness}/{sid}", turn="-1")
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
                st = conversation_capture.capture(VAULT, harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertFalse(st["capture_pending"])
                self.assertEqual(st["captured_rounds"], 1)
                cited = conversation_cite.cite(VAULT, f"{harness}/{sid}", turn="-1")
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
                first = conversation_capture.capture(VAULT, harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(first["ok"], first)
                self.assertFalse(first["capture_pending"])
                self.assertEqual(first["appended"], 1)
                cited = conversation_cite.cite(VAULT, f"{harness}/{sid}", turn="-1")
                saved_first = (ROOT / cited["path"]).read_bytes()
                self.assertEqual(transcripts._dump(dialogue), dialogue)
                # raw.round_block already strips trailing whitespace from whole sections.
                expected = dialogue.rstrip() if harness == "claude" else json.dumps(dialogue)
                self.assertIn(expected.encode(), saved_first)
                self.assertEqual(path.read_bytes(), native)
                native += "".join(json.dumps(row) + "\n" for row in later).encode("utf-8")
                path.write_bytes(native)
                second = conversation_capture.capture(VAULT, harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(second["ok"], second)
                self.assertFalse(second["capture_pending"])
                self.assertEqual((second["captured_rounds"], second["appended"]), (2, 1))
                self.assertEqual(conversation_cite.cite(VAULT, f"{harness}/{sid}", turn="-1")["index"], 2)
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

    def test_cited_record_index_previews_skip_only_recorder_headers(self):
        rows = [{"type": "session_meta", "payload": {"id": self.sid}}]
        for n in (1, 2):
            turn = codex_round(n)
            turn[3] = codex_user_item(self.sid, n, f"question {n}")
            rows += turn
        self.transcript(rows)
        self.assertTrue(self.capture("codex")["ok"])
        refs = [conversation_cite.cite(VAULT, f"codex/{self.sid}", quote=f"question {n}")["round_ref"] for n in (1, 2)]
        name = raw.parse_ref(refs[0])[0]
        path = raw.raw_file(VAULT, name)
        before = path.read_bytes()
        toc = raw.read_round(VAULT, name)["index"]
        self.assertEqual(len(toc), 2)
        for n, item in enumerate(toc, 1):
            self.assertIn(f"question {n}", item["preview"])
            self.assertNotIn("osk-cited", item["preview"])
            recalled = raw.read_round(VAULT, f"{name}#{n}")
            self.assertEqual(raw.cited_header(recalled["text"])["turn"], f"turn-{n}")
            self.assertEqual(item["chars"], recalled["chars"])
        self.assertEqual(path.read_bytes(), before)
        # A user can quote a recorder header. Skip only the header, not matching user
        # content or all HTML comments, in the cited and the older stored formats.
        for stamp in ("", raw._CODEX_V2 + "\n\n", raw.DIALOGUE_V1 + '"turn-1" -->\n\n',
                      raw._CITED + '{"conversation": "c", "harness": "codex", "turn": "t"} -->\n\n'):
            block = f"## 1\n\n{stamp}### user\n\n{raw._CODEX_V2}\n\n### agent\n\nreply\n"
            self.assertEqual(raw._preview(block), raw._CODEX_V2)
            crlf = f"## 1\n\n{stamp}### user\n\nCRLF question\n\n### agent\n\nreply\n".replace("\n", "\r\n")
            self.assertEqual(raw._preview(crlf), "CRLF question")

    def test_cited_round_filters_secrets_and_a_later_hand_edit_does_not_reopen_review(self):
        from osk import distillation as D, graph
        token = "ghp_" + "a" * 36
        rows = claude_round(self.sid, 1)
        rows[0]["message"]["content"] = "## 12\nsecret " + token
        self.transcript(rows)
        st = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        cited = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1", note="the question that carried a secret")
        stored = (ROOT / cited["path"]).read_text(encoding="utf-8")
        self.assertNotIn(token, stored)
        self.assertIn("[FILTERED:github-token]", stored)
        self.assertIn("\\## 12", stored)  # a numeric H2 in the words is escaped, not a round
        self.assertNotIn(token, conversation_state.state_path(VAULT, "claude", self.sid).read_text(encoding="utf-8"))
        source = D._source(VAULT, cited["round_ref"], graph.Index(VAULT))
        self.assertEqual(source["native"], st["pending_refs"][0])
        saved = D.create_node(VAULT, {"key": self.sid, "sources": [cited["round_ref"]], "hub": "W1"},
                              title=self.sid, summary="cited question",
                              body=evidenced("The question is kept with its cited turn.", cited["round_ref"]),
                              drafter="test-model",
                              space="00_Scope/W1")
        self.assertEqual(saved["distillation"]["status"], "complete")
        conversation_review.acknowledge(VAULT, "claude", self.sid, st["through"], "preserved", "Cited and kept.", [{"key": self.sid}])
        self.assertEqual(conversation_review.review_status(VAULT, "claude", self.sid, st["through"])["status"], "complete")
        # Records are append-only; ACK checked the round, and nothing rechecks it later. A real
        # correction is registered by its corrector (reopen), which a hand edit skips.
        path = ROOT / cited["path"]
        path.write_bytes(path.read_bytes().replace(b"secret", b"tampered"))
        self.assertEqual(conversation_review.review_status(VAULT, "claude", self.sid, st["through"])["status"], "complete")
        self.assertFalse(conversation_capture.status(VAULT, "claude", self.sid)["pending"])

    def test_original_turn_read_filters_secrets_and_hashes_the_original(self):
        # Antigravity keeps the user's words and its replies as written; only a citation filtered them.
        token = "ghp_" + "c" * 36
        step = lambda i, typ, text: {"step_index": i, "source": "x", "type": typ, "status": "DONE", "content": text}
        path = self.antigravity(self.sid, [step(0, "USER_INPUT", f"<USER_REQUEST>\nuse {token}\n</USER_REQUEST>"),
                                           step(1, "PLANNER_RESPONSE", f"stored {token}")])
        st = conversation_capture.capture(VAULT, "antigravity", self.sid, str(path), self.sid, "00_Scope/W1")
        tracked = json.loads(conversation_state.state_path(VAULT, "antigravity", self.sid).read_text(encoding="utf-8"))["rounds"][0]["hash"]
        for view in ("full", "review"):
            got = conversation_cite.read_turns(VAULT, st["pending_refs"], view=view)["turns"][0]
            self.assertNotIn(token, json.dumps(got, ensure_ascii=False))
            self.assertEqual(got["filtered"], ["github-token"])
            self.assertEqual(got["hash"], tracked)  # the original is what was tracked

    def test_turn_hash_keeps_the_locators_a_user_writes(self):
        said = lambda target: {"id": "u1:m1", "user": f"retarget native:claude:{target}:turn-1", "agent": "done"}
        self.assertNotEqual(conversation_capture.turn_hashes(said("session-A")), conversation_capture.turn_hashes(said("session-B")))
        # A resumed copy rewrites only the result locators the parser wrote.
        copy = lambda sid: {"id": "u1:m1", "user": "q", "agent": json.dumps(
            {"type": "tool_evidence_ref", "native_result": f"claude:{sid}:m1"})}
        self.assertEqual(conversation_capture.turn_hashes(copy("parent")), conversation_capture.turn_hashes(copy("child")))

    def test_changed_words_under_one_turn_id_are_cited_anew_and_unbind_the_old_receipt(self):
        from osk import distillation as D
        rows = claude_round(self.sid, 1)
        rows[0]["message"]["content"] = "apply the rule to project A only"
        self.transcript(rows)
        conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        first = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1")
        again = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1")
        self.assertTrue(again["reused"])
        self.assertEqual(again["round_ref"], first["round_ref"])
        saved = D.create_node(VAULT, {"key": self.sid, "sources": [first["round_ref"]], "hub": "W1"},
                              title=self.sid, summary="scope of a rule",
                              body=evidenced("The rule applies to project A only.", first["round_ref"]),
                              drafter="test-model", space="00_Scope/W1")
        self.assertEqual(saved["distillation"]["status"], "complete")
        rows[0]["message"]["content"] = "apply the rule to every project"
        self.transcript(rows)
        changed = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        with self.assertRaisesRegex(ValueError, "words changed after it was cited"):  # the receipt cited the earlier words
            conversation_review.acknowledge(VAULT, "claude", self.sid, changed["through"], "preserved", "Kept.", [{"key": self.sid}])
        second = conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1")
        self.assertFalse(second["reused"])
        self.assertEqual(second["supersedes"], first["round_ref"])
        self.assertNotEqual(second["round_ref"], first["round_ref"])
        self.assertIn("every project", (ROOT / second["path"]).read_text(encoding="utf-8"))

    def test_numeric_turn_ids_name_turns_and_only_minus_counts_back(self):
        # Antigravity turn IDs are step numbers. The ID read_cited returns must cite that turn,
        # even where the same words repeat and a quote cannot tell the turns apart.
        step = lambda i, typ, text: {"step_index": i, "source": "x", "type": typ, "status": "DONE", "content": text}
        path = self.antigravity(self.sid, [step(0, "USER_INPUT", "<USER_REQUEST>\nretry\n</USER_REQUEST>"),
                                           step(1, "PLANNER_RESPONSE", "first"),
                                           step(2, "USER_INPUT", "<USER_REQUEST>\nretry\n</USER_REQUEST>"),
                                           step(3, "PLANNER_RESPONSE", "second")])
        st = conversation_capture.capture(VAULT, "antigravity", self.sid, str(path), self.sid, "00_Scope/W1")
        read = conversation_cite.read_turns(VAULT, st["pending_refs"], view="full")["turns"]
        self.assertEqual([t["turn"] for t in read], ["0", "2"])
        for t in read:
            self.assertEqual(conversation_cite.cite(VAULT, f"antigravity/{self.sid}", turn=t["turn"])["turn"], t["turn"])
        self.assertEqual(conversation_cite.cite(VAULT, f"antigravity/{self.sid}", turn="-1")["turn"], "2")
        with self.assertRaisesRegex(ValueError, "counts back"):
            conversation_cite.cite(VAULT, f"antigravity/{self.sid}", turn=1)  # a position counts back; 1 is none

    def test_record_identity_survives_vault_copy(self):
        import shutil
        self.transcript(claude_round(self.sid, 1))
        code = """import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ['OSK_PROBE_ENGINE'])
from osk import core, integration as it, raw, validate, conversation_capture, conversation_cite, conversation_review, conversation_state
from osk.vault import Vault
VAULT = Vault.open()
if not (VAULT.root / '00_Scope/W1').is_dir():
    validate.make_mini_vault(VAULT, VAULT.root)
sid = os.environ['OSK_PROBE_SID']
st = conversation_capture.capture(VAULT, 'claude', sid, os.environ['OSK_PROBE_TRANSCRIPT'], 'copy-session', '00_Scope/W1')
assert st['ok'], st
cited = conversation_cite.cite(VAULT, 'claude/' + sid, turn='-1')
print(json.dumps({'record': st['record'], 'refs': st['pending_refs'], 'appended': st['appended'],
                  'state_path': str(conversation_state.state_path(VAULT, 'claude', sid)), 'cited': cited['round_ref'],
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
        self.assertFalse(raw.record_path(VAULT, "Capture", st["record"]).exists())
        conversation = f"claude/{self.sid}"
        cited = conversation_cite.cite(VAULT, conversation, quote="  question\n1 ")
        self.assertTrue(cited["round_ref"].startswith("_cited/.records/"), cited)
        # The 근거 line's coordinate: the record hash shortened, and the round (Mechanism §8 9항).
        self.assertRegex(cited["coordinate"], r"^[0-9a-f]{8}#1$")
        self.assertEqual(raw.read_round(VAULT, cited["coordinate"])["round_ref"], cited["round_ref"])
        self.assertEqual((cited["turn"], cited["user_by"], cited["reused"]), ("user-1", "engine", False))
        text = raw.read_round(VAULT, cited["round_ref"])["text"]
        self.assertIn("question 1", text)
        self.assertNotIn("answer 1", text)  # the reply stays in the transcript, kept as its hash
        header = raw.cited_header(text)
        self.assertEqual((header["harness"], header["conversation"], header["turn"]), ("claude", self.sid, "user-1"))
        self.assertEqual(header["agent_sha256"], cited["agent_sha256"])
        self.assertEqual(raw.cited_native(text), st["pending_refs"][0])
        # The same turn, named again either way, keeps its one coordinate.
        again = conversation_cite.cite(VAULT, conversation, turn="user-1")
        self.assertEqual((again["round_ref"], again["reused"]), (cited["round_ref"], True))
        # The open turn is citable before it finishes and agrees with its finished round.
        open_turn = conversation_cite.cite(VAULT, conversation, turn="-1", note="decision under discussion")
        self.assertEqual((open_turn["turn"], open_turn["agent_sha256"]), ("user-3", None))
        self.assertIn("decision under discussion", raw.read_round(VAULT, open_turn["round_ref"])["text"])
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2) + claude_round(self.sid, 3))
        self.capture()
        self.assertEqual(conversation_cite.cite(VAULT, conversation, quote="question 3")["round_ref"], open_turn["round_ref"])
        with self.assertRaisesRegex(ValueError, "3 turns match"):
            conversation_cite.cite(VAULT, conversation, quote="question")
        with self.assertRaisesRegex(ValueError, "relative turn"):
            conversation_cite.cite(VAULT, conversation, turn=2)
        with self.assertRaisesRegex(ValueError, "0 turns match"):
            conversation_cite.cite(VAULT, conversation, turn="2")  # a string other than "-N" is a turn ID
        with self.assertRaisesRegex(ValueError, "transcript is readable"):
            conversation_cite.cite(VAULT, conversation, user="words supplied by the caller")
        with self.assertRaisesRegex(ValueError, "harness"):
            conversation_cite.cite(VAULT, "claude:" + self.sid, quote="question 1")

    def test_cite_without_transcript_marks_caller_supplied_words(self):
        self.transcript(claude_round(self.sid, 1))
        self.capture()
        self.path.unlink()
        conversation = f"claude/{self.sid}"
        with self.assertRaisesRegex(ValueError, "pass user="):
            conversation_cite.cite(VAULT, conversation, quote="question 1")
        cited = conversation_cite.cite(VAULT, conversation, user="question 1, as the agent recalls it", turn="user-1:final-1")
        self.assertEqual((cited["user_by"], cited["turn"], cited["agent_sha256"]), ("caller", "user-1", None))
        self.assertEqual(raw.cited_header(raw.read_round(VAULT, cited["round_ref"])["text"])["user_by"], "caller")
        self.assertTrue(conversation_cite.cite(VAULT, conversation, user="anything", turn="user-1")["reused"])

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
                st = conversation_capture.capture(VAULT, harness, sid, str(path), sid, "00_Scope/W1")
                self.assertTrue(st["ok"], st)
                self.assertEqual(st["captured_rounds"], 1)
                with mock.patch.object(raw, "write_raw", wraps=raw.write_raw) as sink:
                    cited = conversation_cite.cite(VAULT, f"{harness}/{sid}", turn="-1", note="the reply is kept as its hash")
                self.assertEqual(sink.call_count, 1)  # mandatory final filter still runs
                saved = (ROOT / cited["path"]).read_bytes()
                cursor = conversation_state.state_path(VAULT, harness, sid).read_bytes()
                for token in tokens:
                    self.assertNotIn(token.encode(), saved)
                    self.assertNotIn(token.encode(), cursor)
                self.assertEqual(saved.count(b"[FILTERED:github-token]"), 1)  # the user's words only
                self.assertTrue(cited["agent_sha256"])
                self.assertEqual(path.read_bytes(), native)  # native evidence is not edited
                again = conversation_cite.cite(VAULT, f"{harness}/{sid}", turn="-1")
                self.assertTrue(again["reused"])
                self.assertEqual((ROOT / cited["path"]).read_bytes(), saved)

    def test_a_citation_appends_to_the_one_root_record_whatever_space_is_named(self):
        # 기록은 루트 `_cited/` 한 자리다(헌법 4조 3항) — 인용은 착지를 고정하지도, 다른 space를
        # 거부하지도 않는다. `space`는 호환 인자다.
        key = self.sid + "-key"
        self.forget(self.sid)
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        st = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), key)
        raw.append_rounds(VAULT, key, st["record"], [{"user": "question 1", "agent": ""}], "00_Scope/Capture",
                          cited={"harness": "claude", "conversation": self.sid, "turn": "user-1"}, bind=False)
        second = conversation_cite.cite(VAULT, f"claude/{self.sid}", quote="question 2", space="00_Scope/W1")
        self.assertTrue(second["round_ref"].startswith("_cited/.records/"), second)
        self.assertEqual(second["index"], 2)
        self.assertIsNone(conversation_capture.status(VAULT, "claude", self.sid)["space"])
        self.assertIsNone(write.resolve_session(VAULT, key))                 # citing binds nothing

    def test_concurrent_first_citations_land_the_conversation_once(self):
        # The landing check, the write and the pin are one step under the conversation's lock:
        # two first citations into different scopes cannot both land. A one-off workspace has no
        # binding to stop the second one, so only that step does.
        import threading
        from osk import workspaces
        root = Path(TMP.name) / f"{self.sid}-root"
        core.ledger_append(workspaces.ledger_path(VAULT), {"kind": "rule", "harness": "claude", "version": "2.1.281",
                                               "root": str(root), "shape": r"[a-z]+-[a-z]+"})
        self.addCleanup(workspaces.ledger_path(VAULT).unlink, missing_ok=True)
        (root / "fusion-orion").mkdir(parents=True)
        key = "fusion-orion"
        self.forget(self.sid)
        self.transcript([{**r, "cwd": str(root / "fusion-orion")}
                         for r in claude_round(self.sid, 1) + claude_round(self.sid, 2)])
        self.assertTrue(conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), key)["workspace"]["one_off"])
        original, inside, outcomes = raw.append_rounds, threading.Event(), {}

        def slow(*args, **kwargs):
            inside.set()
            time.sleep(0.5)             # the other citation tries to land meanwhile
            return original(*args, **kwargs)

        def cite(name, space, quote):
            try:
                outcomes[name] = conversation_cite.cite(VAULT, f"claude/{self.sid}", quote=quote, space=space)
            except ValueError as exc:
                outcomes[name] = exc

        with mock.patch.object(raw, "append_rounds", slow):
            first = threading.Thread(target=cite, args=("first", "00_Scope/Capture", "question 1"))
            first.start()
            self.assertTrue(inside.wait(10))
            second = threading.Thread(target=cite, args=("second", "00_Scope/W1", "question 2"))
            second.start()
            first.join(30)
            second.join(30)
        # Both citations append to the one root record under the conversation's lock: no second
        # record anywhere, and the named spaces decide nothing (Constitution art. 4 §3).
        self.assertTrue(outcomes["first"]["round_ref"].startswith("_cited/.records/"), outcomes)
        self.assertEqual(outcomes["second"]["path"], outcomes["first"]["path"], outcomes)
        self.assertEqual({outcomes["first"]["index"], outcomes["second"]["index"]}, {1, 2}, outcomes)
        self.assertFalse(any((ROOT / "00_Scope/W1/_cited").glob(f".records/{conversation_capture.status(VAULT, 'claude', self.sid)['record']}*")))
        self.assertIsNone(conversation_capture.status(VAULT, "claude", self.sid)["space"])
