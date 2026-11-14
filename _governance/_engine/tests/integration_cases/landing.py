"""대화 포착과 검토의 수트의 한 갈래: 착지와 프로젝트 키 — 대화가 어느 scope에 놓이는가.

`tests/test_integration.py`의 `IntegrationTests`가 이 클래스를 이어받아 한 클래스로 돈다. 따로 돌리는 파일이 아니다."""
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from integration_cases.support import claude_round, ENGINE, evidenced, ROOT, TMP, VAULT
from osk import (core, integration as it, raw, scope_memory, write, conversation_capture,  # noqa: E402
                 conversation_cite, conversation_review, conversation_state)  # noqa: E402


class LandingCases:
    def test_scope_routing_changes_are_recorded_and_same_scope_keys_resume(self):
        self.transcript(claude_round(self.sid, 1))
        first = self.capture()
        write.bind_session(VAULT, self.sid + "-renamed", "Capture")
        resumed = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid + "-renamed")
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(first["pending_refs"], resumed["pending_refs"])
        write.alias_session(VAULT, self.sid + "-alias", self.sid + "-renamed")
        self.assertTrue(conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid + "-alias")["ok"])
        refused = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid + "-elsewhere", "00_Scope/W1")
        self.assertFalse(refused["ok"])
        self.assertTrue(conversation_capture.status(VAULT, "claude", self.sid)["capture_pending"])
        self.assertIn("scope changed", conversation_capture.status(VAULT, "claude", self.sid)["capture_error"])
        self.assertEqual(first["pending_refs"], refused["pending_refs"])
        self.assertTrue(conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid + "-renamed")["ok"])

    def test_capture_normalizes_saved_and_requested_scope(self):
        self.transcript(claude_round(self.sid, 1))
        first = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), "capture-tests", "00_Scope/Capture/")
        self.assertTrue(first["ok"], first)
        # Also repair a successful cursor written with the previous spelling.
        path = conversation_state.state_path(VAULT, "claude", self.sid)
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["space"], "00_Scope/Capture")
        state["space"] = "00_Scope/Capture/"
        path.write_text(json.dumps(state), encoding="utf-8")
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        resumed = self.capture()
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["appended"], 1)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["space"], "00_Scope/Capture")

    def test_rejected_first_landing_does_not_pin_the_conversation(self):
        # A scope other than the key's primary is a legitimate landing now (Bylaws §3 10): it pins
        # the conversation's review context without touching the binding.
        self.transcript(claude_round(self.sid + "w", 1))
        other = conversation_capture.capture(VAULT, "claude", self.sid + "w", str(self.path), "capture-tests", "00_Scope/W1")
        self.assertTrue(other["ok"], other)
        self.assertEqual(other["space"], "00_Scope/W1")
        self.assertEqual(write.resolve_session(VAULT, "capture-tests"), "Capture")
        # 고정한 착지가 검토 문맥이다 — 허브 트리와 summary 확인은 W1이지 주된 scope Capture가 아니다(#191 리뷰 2차)
        review = it.prompt(VAULT, "claude", self.sid + "w", include_organization=False)
        self.assertTrue(review["hub_tree"].startswith("- 00_Scope/W1 "), review["hub_tree"])
        pinned = scope_memory.replace(VAULT, "capture-tests", "- pinned summary", space="00_Scope/W1")
        conversation_review.acknowledge(VAULT, "claude", self.sid + "w", review["through"], "summary", "fixture",
                       targets=[{"text": "pinned summary"}])
        self.assertEqual(conversation_capture.status(VAULT, "claude", self.sid + "w")["reviewed_rounds"], 1)
        scope_memory.replace(VAULT, "capture-tests", "", pinned["hash"], "00_Scope/W1")      # 다른 시험에 남기지 않는다
        write.unbind_session(VAULT, "capture-tests", "W1", "fixture")
        for n, space in enumerate(("00_Scope/Absent", "Capture")):
            sid = self.sid + str(n)
            self.transcript(claude_round(sid, 1))
            rejected = conversation_capture.capture(VAULT, "claude", sid, str(self.path), "capture-tests", space)
            self.assertFalse(rejected["ok"], rejected)
            self.assertTrue(rejected["capture_pending"])
            self.assertEqual(rejected["captured_rounds"], 0)
            state = json.loads(conversation_state.state_path(VAULT, "claude", sid).read_text(encoding="utf-8"))
            self.assertIsNone(state["space"])
            self.assertFalse(raw.record_path(VAULT, "Capture", rejected["record"]).exists())
            resumed = conversation_capture.capture(VAULT, "claude", sid, str(self.path), "capture-tests")
            self.assertTrue(resumed["ok"], resumed)
            self.assertEqual(resumed["appended"], 1)


    def test_unbound_conversation_can_choose_a_stable_session_without_binding_generic_key(self):
        self.transcript(claude_round(self.sid, 1))
        generic = self.sid + '-unbound'
        # Tracking does not wait for a landing (#179): the turns are tracked and nothing is bound.
        tracked = conversation_capture.capture(VAULT, 'claude', self.sid, str(self.path), generic)
        self.assertTrue(tracked['ok'], tracked)
        self.assertEqual((tracked['captured_rounds'], tracked['space']), (1, None))
        captured = conversation_capture.capture(VAULT, 'claude', self.sid, str(self.path), 'capture-tests', '00_Scope/Capture')
        self.assertTrue(captured['ok'], captured)
        self.assertEqual(captured['captured_rounds'], 1)
        self.assertIsNone(write.resolve_session(VAULT, generic))
        resumed = conversation_capture.hook_capture(VAULT, {'harness': 'claude', 'session_id': self.sid}, generic)
        self.assertTrue(resumed['ok'], resumed)
        self.assertEqual(resumed['session'], 'capture-tests')
        self.assertIsNone(write.resolve_session(VAULT, generic))
        other = conversation_capture.capture(VAULT, 'claude', self.sid + '-other', str(self.path), generic)
        self.assertFalse(other['ok'])
        self.assertIsNone(conversation_state.load_state(VAULT, conversation_state.state_path(VAULT, 'claude', self.sid + '-other'),
                                  'claude', self.sid + '-other')['space'])
        self.assertFalse(conversation_capture.capture(VAULT, 'claude', self.sid, str(self.path), generic, '00_Scope/W1')['ok'])


    def test_unbound_incomplete_capture_retains_explicit_landing_for_catchup(self):
        self.transcript(claude_round(self.sid, 1, finished=False))
        pending = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/Capture")
        self.assertTrue(pending["capture_pending"])
        self.assertEqual(pending["capture_recovery"]["state"], "pending")
        self.assertEqual(it.prompt(VAULT, "claude", self.sid, include_organization=False)["raw_review"]["state"], "none")
        self.assertIsNone(write.resolve_session(VAULT, self.sid))
        self.transcript(claude_round(self.sid, 1))
        done = it.catchup(VAULT, 100)
        capture = next(c for c in done["captures"] if c["conversation_id"] == self.sid)
        self.assertEqual((capture["ok"], capture["appended"]), (True, 1))
        # Tracking writes nothing, so it binds nothing; citing writes the one root record and binds nothing
        # either — only a node written with a space adds that scope to the project's binding.
        self.assertIsNone(write.resolve_session(VAULT, self.sid))
        cited = conversation_cite.cite(VAULT, f"claude/{self.sid}", quote="question 1")
        self.assertTrue(cited["round_ref"].startswith("_cited/.records/"), cited)
        self.assertIsNone(write.resolve_session(VAULT, self.sid))
        self.assertEqual(conversation_capture.status(VAULT, "claude", self.sid)["space"], "00_Scope/Capture")   # the explicit landing stays

    def test_an_unlanded_conversation_is_tracked_and_its_first_citation_lands_it(self):
        key = self.sid + "-key"
        self.forget(self.sid)
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        st = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), key)
        self.assertTrue(st["ok"], st)
        self.assertEqual((st["captured_rounds"], st["space"]), (2, None))
        self.assertIsNone(write.resolve_session(VAULT, key))       # tracking writes nothing and binds nothing
        self.assertIn("시행령 §3 9항", it.prompt(VAULT, "claude", self.sid, include_organization=False)["text"])
        # Citing needs no landing: the record is the one root `_cited/` (Constitution art. 4 §3).
        cited = conversation_cite.cite(VAULT, f"claude/{self.sid}", quote="question 1")
        self.assertTrue(cited["round_ref"].startswith("_cited/.records/"), cited)
        self.assertIsNone(conversation_capture.status(VAULT, "claude", self.sid)["space"])
        self.assertIsNone(write.resolve_session(VAULT, key))
        self.assertEqual(conversation_cite.cite(VAULT, f"claude/{self.sid}", quote="question 2", space="00_Scope/W1")["path"],
                         cited["path"])                   # any named space: the same record
        # A node written with a space adds that scope to the project's binding without making it
        # primary (Mechanism §6-2 6); the primary scope is designated.
        if not (ROOT / "00_Scope/Capture/Capture.md").exists():        # a cluster's first node is its hub
            write.create_node(VAULT, "Capture", "Capture 허브", "시험 scope", "gpt-6-astra", space="00_Scope/Capture")
        made = write.create_node(VAULT, self.sid + " node", "summary", "body", "gpt-6-astra",
                                 session=key, space="00_Scope/Capture")
        self.assertTrue(made["ok"], made)
        self.assertEqual((made["bound_scope"], write.bindings(VAULT, key)["scopes"], write.resolve_session(VAULT, key)),
                         ("Capture", ["Capture"], None))
        with self.assertRaisesRegex(write.WriteError, "주된 scope가 없다"):
            write.create_node(VAULT, self.sid + " node 2", "summary", "body", "gpt-6-astra", session=key)
        review = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        self.assertIn("주된 scope 없음", review["text"])
        # 결속 지정이 실패하면 검토도 남지 않는다 — 같은 through로 재시도한다(#191 리뷰 P2)
        from unittest import mock as _mock
        with _mock.patch.object(write, "set_primary", side_effect=OSError("ledger refused")):
            with self.assertRaises(OSError):
                conversation_review.acknowledge(VAULT, "claude", self.sid, review["through"], "no_value", "fixture", primary_scope="00_Scope/Capture")
        self.assertEqual(conversation_capture.status(VAULT, "claude", self.sid)["reviewed_rounds"], 0)
        self.assertIsNone(write.resolve_session(VAULT, key))
        conversation_review.acknowledge(VAULT, "claude", self.sid, review["through"], "no_value", "fixture", primary_scope="00_Scope/Capture")
        self.assertEqual(write.resolve_session(VAULT, key), "Capture")
        self.assertTrue(write.create_node(VAULT, self.sid + " node 2", "summary", "body", "gpt-6-astra", session=key)["ok"])

    def test_a_one_off_workspace_key_is_neither_bound_nor_followed(self):
        from osk import workspaces
        root = Path(TMP.name) / f"{self.sid}-root"
        core.ledger_append(workspaces.ledger_path(VAULT), {"kind": "rule", "harness": "claude", "version": "2.1.281",
                                               "root": str(root), "shape": r"\d{4}-\d{2}-\d{2}/[^/]+"})
        self.addCleanup(workspaces.ledger_path(VAULT).unlink, missing_ok=True)
        key = self.sid + "-chat"

        def talk(sid, folder, version="2.1.281"):
            cwd = root / folder
            cwd.mkdir(parents=True, exist_ok=True)
            path = Path(TMP.name) / f"{sid}.jsonl"
            path.write_text("".join(json.dumps({**r, "cwd": str(cwd), "version": version}) + "\n"
                                    for r in claude_round(sid, 1)), encoding="utf-8")
            self.forget(sid)
            return conversation_capture.capture(VAULT, "claude", sid, str(path), key)

        # The key also names an ordinary scope elsewhere: a binding made before the rule, or a
        # repository folder of the same name. The one-off conversation does not follow it.
        write.bind_session(VAULT, key, "Capture", "a binding of the same name")
        st = talk(self.sid, f"2026-10-11/{key}")
        self.assertTrue(st["ok"], st)
        self.assertEqual((st["workspace"]["one_off"], st["space"]), (True, None))
        self.assertNotIn("warning", st["workspace"])
        rows = len(core.ledger_read(VAULT.routing))
        cited = conversation_cite.cite(VAULT, f"claude/{self.sid}", quote="question 1", space="00_Scope/W1")
        self.assertTrue(cited["round_ref"].startswith("_cited/.records/"), cited)   # the one root record
        self.assertNotIn("binding", cited)
        self.assertEqual(len(core.ledger_read(VAULT.routing)), rows)          # nothing bound
        self.assertIsNone(conversation_capture.status(VAULT, "claude", self.sid)["space"])            # citing pins no landing
        # An explicit landing at capture is this conversation's review context only.
        self.assertEqual(conversation_capture.capture(VAULT, "claude", self.sid, None, key, "00_Scope/W1")["space"], "00_Scope/W1")
        self.assertEqual(len(core.ledger_read(VAULT.routing)), rows)          # still nothing bound
        # The judgment belongs to the conversation, not to the key: elsewhere the key keeps its scope.
        self.assertEqual(write.resolve_session(VAULT, key), "Capture")
        self.assertIsNone(talk(self.sid + "-other", f"2026-10-12/{key}")["space"])
        # A write tool takes only a key; a process standing in the one-off folder does not bind it.
        self.addCleanup(workspaces._here.clear)
        workspaces.enter(VAULT, str(root / f"2026-10-11/{key}"))
        self.assertIsNone(write.resolve_session(VAULT, key))
        with self.assertRaisesRegex(write.WriteError, "일회성 작업 폴더"):
            write.create_node(VAULT, self.sid + " node", "summary", "body", "gpt-6-astra", session=key)
        workspaces.enter(VAULT, str(TMP.name))
        self.assertEqual(write.resolve_session(VAULT, key), "Capture")
        odd = talk(self.sid + "-odd", "artifacts/x")
        self.assertTrue(odd["workspace"]["one_off"])
        self.assertIn("규칙에 없는 모양", odd["workspace"]["warning"])
        self.assertIn("판본", talk(self.sid + "-newer", f"2026-10-13/{key}", "9.9.9")["workspace"]["warning"])
        self.assertNotIn("warning", talk(self.sid + "-patch", f"2026-10-13/{key}-p", "2.1.295")["workspace"])
        workspaces.add(VAULT, "claude", str(root), r"\d{4}-\d{2}-\d{2}/[^/]+", "9.9.0", "re-checked")
        self.assertNotIn("warning", talk(self.sid + "-rechecked", f"2026-10-13/{key}-r", "9.9.9")["workspace"])
        (root / "2026-10-14" / "repo" / ".git").mkdir(parents=True)
        self.assertFalse(talk(self.sid + "-repo", "2026-10-14/repo")["workspace"]["one_off"])

    def test_the_engine_rules_know_both_claude_scratch_roots(self):
        # Claude desktop 3.16 (CLI 2.1.308) moved the Store install's scratch workspaces from
        # %APPDATA%/Claude into %LOCALAPPDATA%/Claude-Data/claude_<package>.
        if os.name != "nt":
            self.skipTest("the engine's Claude rules are Windows paths")
        from osk import workspaces
        tail = "/" + "/".join([str(uuid.uuid4()), str(uuid.uuid4()), "scratch-2026-10-27-269921"])
        with tempfile.TemporaryDirectory() as roaming, tempfile.TemporaryDirectory() as local, \
                mock.patch.dict(os.environ, {"APPDATA": roaming, "LOCALAPPDATA": local}):
            new = Path(local + "/Claude-Data/claude_pzs8sxrjxfjjc/scratch-workspaces" + tail)
            for cwd, version in ((roaming + "/Claude/scratch-workspaces" + tail, "2.1.281"),
                                 (str(new), "2.1.308"), (str(new / "chiikawa-addon"), "2.1.309")):
                got = workspaces.judge(VAULT, "claude", cwd, version) or {}
                self.assertEqual((got.get("one_off"), got.get("warning")), (True, None), cwd)
            self.assertIsNone(workspaces.judge(VAULT, "claude", local + "/Claude-Data/claude_pzs8sxrjxfjjc/claude-code"))
            # A new scratch conversation starts unlanded even if its key was bound before the rule.
            new.mkdir(parents=True)
            write.bind_session(VAULT, new.name, "Capture", "bound before the rule")
            self.transcript([{**r, "cwd": str(new), "version": "2.1.308"} for r in claude_round(self.sid, 1)])
            st = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), new.name)
            self.assertEqual((st["workspace"]["one_off"], st["space"]), (True, None), st)

    def test_a_subfolder_of_the_starting_folder_keeps_the_conversation_key(self):
        # #271: a conversation started outside Git moves into a subfolder; the hooks' key stays
        # the starting folder's, so the next capture is not refused as a changed scope.
        import importlib
        from osk import workspaces
        sys.path.insert(0, str(ENGINE / "scripts/hooks"))
        hook = importlib.import_module("claude_session_start")
        self.addCleanup(workspaces._here.clear)
        start = Path(TMP.name) / f"{self.sid}-brain"
        sub, elsewhere = start / "wiki/courses/em2", Path(TMP.name) / f"{self.sid}-other"
        sub.mkdir(parents=True)
        elsewhere.mkdir()
        self.transcript([{**r, "cwd": str(start)} for r in claude_round(self.sid, 1)]
                        + [{**r, "cwd": str(sub)} for r in claude_round(self.sid, 2)])
        key = lambda cwd: hook.session_key(VAULT, hook.project_cwd(
            {"harness": "claude", "session_id": self.sid, "transcript_path": str(self.path), "cwd": str(cwd)}))
        self.assertEqual((key(start), key(sub), key(elsewhere)), (start.name, start.name, elsewhere.name))
        self.assertEqual(hook.project_cwd({"cwd": str(sub)}), str(sub))       # no transcript: the hook's folder
        self.assertTrue(conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), key(start))["ok"])
        st = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), key(sub))
        self.assertTrue(st["ok"], st)
        self.assertIsNone(st["capture_error"])
        # Another repository below the starting folder is its own project: a repository under the
        # Git-less start, and a submodule of the repository a conversation started in.
        g = lambda *a, cwd: subprocess.run(
            ["git", "-c", "user.name=x", "-c", "user.email=x@x.invalid", "-c", "protocol.file.allow=always", *a],
            cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)
        inner, parent, child = start / "tools/inner", Path(TMP.name) / f"{self.sid}-up", Path(TMP.name) / f"{self.sid}-lib"
        for p in (inner, parent, child):
            p.mkdir(parents=True)
            g("init", "-q", cwd=p)
            g("commit", "-q", "--allow-empty", "-m", p.name, cwd=p)
        g("submodule", "add", "-q", str(child), "vendor/lib", cwd=parent)
        self.assertEqual(key(inner), hook.session_key(VAULT, str(inner)))
        self.assertNotEqual(key(inner), start.name)
        self.transcript([{**r, "cwd": str(parent)} for r in claude_round(self.sid, 1)])
        lib = parent / "vendor/lib"
        self.assertEqual(key(parent / "vendor"), key(parent))                 # a plain folder of the repository
        self.assertEqual(key(lib), hook.session_key(VAULT, str(lib)))
        self.assertNotEqual(key(lib), key(parent))

    def test_a_resumed_copy_of_a_landed_one_off_conversation_keeps_its_landing(self):
        # A copy of a conversation landed only by its first citation starts unlanded. The parent's
        # turns stay the parent's, and the copy continues in the parent's landing (#184 review).
        from osk import workspaces
        root = Path(TMP.name) / f"{self.sid}-root"
        core.ledger_append(workspaces.ledger_path(VAULT), {"kind": "rule", "harness": "claude", "version": "2.1.281",
                                               "root": str(root), "shape": r"[a-z]+-[a-z]+"})
        self.addCleanup(workspaces.ledger_path(VAULT).unlink, missing_ok=True)
        cwd = root / "silver-gravity"
        cwd.mkdir(parents=True)
        parent = self.sid + "-parent"
        self.forget(parent, self.sid)
        rows = [{**r, "cwd": str(cwd)} for r in claude_round(parent, 1)]
        rows[0]["uuid"] = str(uuid.uuid4())
        self.transcript(rows)
        self.assertTrue(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "silver-gravity")["workspace"]["one_off"])
        # the parent lands by an explicit capture landing; its citation goes to the one root record
        self.assertEqual(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "silver-gravity", "00_Scope/W1")["space"],
                         "00_Scope/W1")
        conversation_cite.cite(VAULT, f"claude/{parent}", quote="question 1")
        self.transcript([dict(r, sessionId=self.sid) for r in rows]
                        + [{**r, "cwd": str(cwd)} for r in claude_round(self.sid, 2)])
        copied = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), "silver-gravity")
        self.assertTrue(copied["ok"], copied)
        self.assertEqual((copied["inherited_rounds"], copied["captured_rounds"], copied["space"]),
                         (1, 1, "00_Scope/W1"), copied)

    def test_a_resumed_copy_of_an_unlanded_one_off_conversation_takes_no_binding_of_its_key(self):
        # The parent's landing is the parent's, judged by its own workspace: a process outside
        # that folder (a catch-up) must not read a same-named repository key's binding as it.
        from osk import workspaces
        root = Path(TMP.name) / f"{self.sid}-root"
        core.ledger_append(workspaces.ledger_path(VAULT), {"kind": "rule", "harness": "claude", "version": "2.1.281",
                                               "root": str(root), "shape": r"[a-z]+-[a-z]+"})
        self.addCleanup(workspaces.ledger_path(VAULT).unlink, missing_ok=True)
        cwd = root / "amber-quartz"
        cwd.mkdir(parents=True)
        write.bind_session(VAULT, "amber-quartz", "W1", "a repository key of the same name")
        parent = self.sid + "-parent"
        self.forget(parent, self.sid)
        rows = [{**r, "cwd": str(cwd)} for r in claude_round(parent, 1)]
        rows[0]["uuid"] = str(uuid.uuid4())
        self.transcript(rows)
        self.assertIsNone(conversation_capture.capture(VAULT, "claude", parent, str(self.path), "amber-quartz")["space"])
        self.transcript([dict(r, sessionId=self.sid) for r in rows]
                        + [{**r, "cwd": str(cwd)} for r in claude_round(self.sid, 2)])
        copied = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), "amber-quartz")
        self.assertTrue(copied["ok"], copied)
        self.assertEqual((copied["inherited_rounds"], copied["captured_rounds"], copied["space"]),
                         (1, 1, None), copied)

    def test_a_person_only_conversation_cites_and_distills_into_person_space(self):
        from osk import distillation
        key = self.sid + "-key"
        self.forget(self.sid)
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2))
        conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), key)
        cited = conversation_cite.cite(VAULT, f"claude/{self.sid}", quote="question 1")
        self.assertTrue(cited["round_ref"].startswith("_cited/.records/"), cited)   # the one root record
        self.assertIsNone(conversation_capture.status(VAULT, "claude", self.sid)["space"])
        self.assertIsNone(write.resolve_session(VAULT, key))
        self.assertIn("question 1", raw.read_round(VAULT, cited["round_ref"])["text"])
        hub = ROOT / "00_Person" / "Habits" / "Habits.md"
        hub.parent.mkdir(parents=True, exist_ok=True)
        hub.write_text('---\nid: "260801-zzzz-hbit"\ncreated: "2026-08-01 00:00 (KST)"\n'
                       'updated: "2026-08-01 00:00 (KST)"\nauthor: "agent"\ndrafter: "sonnet-5"\n'
                       'summary: "Person 시험 facet 허브"\n---\n\n# Habits\n\n시험 facet.\n', encoding="utf-8")
        job = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        made = distillation.create_node(VAULT, {"key": job["key"] + ":habit", "sources": [cited["round_ref"]],
                                         "hub": "Habits"},
                                        title=self.sid + " habit", summary="a habit the user stated",
                                        body=evidenced("The user keeps a habit they stated.", cited["round_ref"]),
                                        drafter="gpt-6-astra",
                                        space="00_Person/Habits")
        self.assertEqual(made["distillation"]["status"], "complete", made)
        self.assertFalse(made.get("dangling"), made)
        done = conversation_review.acknowledge(VAULT, "claude", self.sid, job["through"], "preserved", "a Person fact",
                              [{"key": job["key"] + ":habit"}])
        self.assertEqual((done["reviewed_rounds"], done["space"]), (2, None))
        # The same conversation later lands in a scope: its new rounds join the same root record.
        self.transcript(claude_round(self.sid, 1) + claude_round(self.sid, 2) + claude_round(self.sid, 3))
        conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), key, "00_Scope/Capture")
        later = conversation_cite.cite(VAULT, f"claude/{self.sid}", quote="question 3")
        self.assertEqual(later["path"], cited["path"], later)
        self.assertTrue(raw.read_round(VAULT, cited["round_ref"])["ok"])
        # A record belongs to no scope: a Person node may be distilled from any cited round
        # (Constitution art. 8 §3).
        job2 = it.prompt(VAULT, "claude", self.sid, include_organization=False)
        made2 = distillation.create_node(VAULT, {"key": job2["key"] + ":scope-habit", "sources": [later["round_ref"]],
                                          "hub": "Habits"},
                                         title=self.sid + " scope habit", summary="from a later round",
                                         body=evidenced("A habit the user stated later.", later["round_ref"]),
                                         drafter="gpt-6-astra",
                                         space="00_Person/Habits")
        self.assertEqual(made2["distillation"]["status"], "complete", made2)
        self.assertTrue(raw.is_person(VAULT, "00_Person") and raw.is_person(VAULT, "Person") and raw.is_person(VAULT, "= Person"))
        self.assertFalse(raw.is_person(VAULT, "00_Person/Habits"))
        listed = [r["path"] for r in raw.list_records(VAULT)["records"]]        # the root `_cited/` listing
        self.assertIn(cited["path"], listed)
