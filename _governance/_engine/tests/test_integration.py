"""Isolated native transcript, crash retry, and durable conversation review checks.

Run: python _governance/_engine/tests/test_integration.py
Native fixture envelopes mirror inspected Claude 2.x JSONL and Codex task_complete
rows. These checks prove parser/mechanical state boundaries, not semantic quality.
"""
import json
import os
import time
import unittest
import uuid
from pathlib import Path

from integration_cases.support import claude_round, claude_row, evidenced, stamped, TMP, VAULT
from integration_cases.copies import CopyCases
from integration_cases.turns import TurnCases
from integration_cases.citations import CitationCases
from integration_cases.landing import LandingCases
from integration_cases.reviews import ReviewCases
from integration_cases.capture import CaptureCases
from osk import transcripts, conversation_capture, conversation_cite, conversation_state  # noqa: E402


class IntegrationTests(CopyCases, TurnCases, CitationCases, LandingCases, ReviewCases, CaptureCases,
                       unittest.TestCase):
    def setUp(self):
        self.sid = self._testMethodName
        self.path = Path(TMP.name) / f"{self.sid}.jsonl"

    def transcript(self, rows):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def capture(self, harness="claude"):
        return conversation_capture.capture(VAULT, harness, self.sid, str(self.path), "capture-tests")

    def notice(self, uid, pid):
        """A background task's completion notice, which Claude Code sends as a prompt."""
        return {**claude_row(self.sid, "user", "<task-notification>\n<task-id>b1</task-id>\n"
                             "<status>completed</status>\n</task-notification>", uid), "promptId": pid}

    def rewind(self, leaf):
        return {"type": "last-prompt", "leafUuid": leaf, "explicit": True, "rewound": True, "sessionId": self.sid}

    def fork(self, at, own_at, went_on=False, cut=True):
        """The app's rewind: a copy of the original up to round 1 that goes on at `own_at`. It carries
        the file-history snapshot of round 2's prompt, the message it was cut before; `cut=False` is
        a copy split at the turn boundary instead. The original holds rounds 1–3 from `at`;
        `went_on` adds round 5 to it afterwards."""
        parent, child = self.sid + "-o", self.sid + "-r"
        ours = lambda owner, rows: [dict(r, uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, owner + r["uuid"]))) for r in rows]
        original = ours(parent, stamped(claude_round(parent, 1) + claude_round(parent, 2) + claude_round(parent, 3), at))
        opath, cpath = Path(TMP.name) / f"{parent}.jsonl", Path(TMP.name) / f"{child}.jsonl"
        write = lambda path, rows: path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        write(opath, original)
        self.assertTrue(conversation_capture.capture(VAULT, "claude", parent, str(opath), "capture-tests")["ok"])
        prompt = original[5]["uuid"]
        trace = [{"type": "file-history-snapshot", "messageId": prompt, "isSnapshotUpdate": False,
                  "snapshot": {"messageId": prompt, "trackedFileBackups": {}}}] if cut else []
        write(cpath, [dict(r, sessionId=child) for r in original[:5]] + trace
              + ours(child, stamped(claude_round(child, 4), own_at)))
        st = conversation_capture.capture(VAULT, "claude", child, str(cpath), "capture-tests")
        self.assertEqual((st["inherited_rounds"], st["captured_rounds"]), (1, 1), st)
        if went_on:
            write(opath, original + ours(parent, stamped(claude_round(parent, 5), own_at + 60)))
            self.assertTrue(conversation_capture.capture(VAULT, "claude", parent, str(opath), "capture-tests")["ok"])
        quiet = time.time() - 13 * 3600   # the original has gone quiet
        os.utime(opath, (quiet, quiet))
        return parent, opath, original

    def _discovery_source(self):
        self.transcript(claude_round(self.sid, 1))
        st = conversation_capture.capture(VAULT, "claude", self.sid, str(self.path), self.sid, "00_Scope/W1")
        return {**st, "cited": conversation_cite.cite(VAULT, f"claude/{self.sid}", turn="-1")["round_ref"]}


    def _discovery_save(self, st, key, title=None):
        from osk import distillation as D
        self.addCleanup(D._job_path(VAULT, key).unlink, missing_ok=True)
        return D.create_node(VAULT, {"key": key, "sources": [st["cited"]], "hub": "W1"},
                             title=title or self.sid + "-node", summary="lost ACK recovery",
                             body=evidenced("Keep the already retained decision and finish its missing "
                                            "acknowledgement.", st["cited"]),
                             drafter="fable-5", space="00_Scope/W1")

    def turns(self, harness="claude", rules=None):
        """The parsed dialogue — tracked by hash, copied into the vault only by `cite`."""
        return list(transcripts.read(str(self.path), harness, self.sid, rules=rules)["dialogue_v1"].values())

    def antigravity(self, cid, steps):
        path = Path(TMP.name) / cid / "transcript_full.jsonl"   # the file sits under its conversation
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(s) + "\n" for s in steps), encoding="utf-8")
        return path

    # ── 소속 scope 없는 대화 (#179 · 시행령 §3 9항 · Mechanism §6-2 6항·§9-4 3항) ──

    def forget(self, *sids):
        for sid in sids:
            self.addCleanup(conversation_state.state_path(VAULT, "claude", sid).unlink, missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
