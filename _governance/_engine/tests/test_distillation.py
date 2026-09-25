"""Real write-path recovery tests in a subprocess-owned temporary vault.

Faults are injected at disk-write boundaries; these are implementation tests,
not model-behavior or semantic-quality evaluations.
"""
from __future__ import annotations
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent


def test_distillation():
    result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--child"],
                            capture_output=True, text=True, timeout=120,
                            stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stdout + result.stderr


def _child():
    with tempfile.TemporaryDirectory(prefix="osk-distillation-test-") as folder:
        os.environ["OSK_VAULT_ROOT"] = folder
        sys.path.insert(0, str(ENGINE))
        from osk import core, contract, distillation as D, raw, validate, write
        validate.make_mini_vault(folder)
        assert core.ROOT == Path(folder).resolve()

        class Crash(BaseException):
            pass

        class Recovery(unittest.TestCase):
            def setUp(self):
                self.name = self._testMethodName
                self.key = "test-" + self.name
                self.source = write.create_node(self.name + "-source", "source",
                                                "observed evidence", "fable-5", space="00_Scope/W1")
                self.spec = {"key": self.key,
                             "sources": [{"ref": self.source["id"], "hash": self.source["new_hash"]}],
                             "hub": "W1"}
                self.args = {"title": self.name + "-target", "summary": "retained decision",
                             "body": "Use a bounded operation because retries can lose their response.",
                             "drafter": "fable-5", "space": "00_Scope/W1"}
                self.hub = core.ROOT / "00_Scope/W1/W1.md"

            def tearDown(self):
                D._job_path(self.key).unlink(missing_ok=True)

            def create(self):
                return D.create_node(self.spec, **self.args)

            def fail_hub(self):
                atomic = write._atomic_write
                def fail(path, data):
                    if path == self.hub:
                        raise OSError("injected hub failure")
                    return atomic(path, data)
                return mock.patch.object(write, "_atomic_write", side_effect=fail)

            def test_create_and_response_retry(self):
                out = self.create()
                self.assertTrue(out["ok"])
                self.assertEqual(out["distillation"]["status"], "complete")
                path = core.ROOT / out["path"]
                saved = path.read_bytes()
                again = self.create()
                self.assertTrue(again["resumed"])
                self.assertEqual(again["id"], out["id"])
                self.assertEqual(path.read_bytes(), saved)
                self.assertEqual(D.status(self.key)["status"], "complete")

            def test_partial_hub_preserves_and_resumes(self):
                with self.fail_hub():
                    out = self.create()
                self.assertTrue(out["ok"])
                self.assertTrue(out["node_preserved"])
                self.assertEqual(out["distillation"]["status"], "pending")
                before = (core.ROOT / out["path"]).read_bytes()
                again = self.create()
                self.assertEqual(again["distillation"]["status"], "complete")
                self.assertEqual((core.ROOT / out["path"]).read_bytes(), before)

            def test_crash_before_node_uses_reserved_identity(self):
                atomic = write._atomic_write
                target = core.ROOT / "00_Scope/W1" / (self.args["title"] + ".md")
                def crash(path, data):
                    if path == target:
                        raise Crash()
                    return atomic(path, data)
                with mock.patch.object(write, "_atomic_write", side_effect=crash):
                    with self.assertRaises(Crash):
                        self.create()
                job = D._load(self.key)
                self.assertFalse(target.exists())
                self.assertEqual(job["target"]["path"], target.relative_to(core.ROOT).as_posix())
                again = self.create()
                self.assertEqual(again["id"], job["target"]["id"])
                self.assertEqual(again["distillation"]["status"], "complete")

            def test_crash_after_node_before_hub(self):
                atomic = write._atomic_write
                target = core.ROOT / "00_Scope/W1" / (self.args["title"] + ".md")
                def crash(path, data):
                    atomic(path, data)
                    if path == target:
                        raise Crash()
                with mock.patch.object(write, "_atomic_write", side_effect=crash):
                    with self.assertRaises(Crash):
                        self.create()
                saved = target.read_bytes()
                self.assertEqual(D.status(self.key)["status"], "pending")
                out = self.create()
                self.assertEqual(out["distillation"]["status"], "complete")
                self.assertEqual(target.read_bytes(), saved)

            def test_legacy_unwritten_journal_replays_reserved_bytes(self):
                # Frozen v1 serialization; the fixture never calls the new
                # legacy flag to construct its expected bytes.
                def v1_links(pred, targets, **_):
                    values = [str(t).strip() for t in write._as_list(targets)]
                    values = [s if s.startswith("[[") or (pred == "derived-from" and re.match(core.ID_RE, s))
                              else f"[[{s}]]" for s in values]
                    return values[0] if len(values) == 1 else values

                for operation in ("create", "update"):
                    with self.subTest(operation=operation):
                        name = self.name + "-" + operation
                        ref = f"[[00_Scope/W1/_raw/{name}-raw.md#1]]"
                        raw_path = core.ROOT / "00_Scope/W1/_raw" / (name + "-raw.md")
                        raw_path.parent.mkdir(exist_ok=True)
                        raw_path.write_bytes(raw._block(1, "original source", "measured answer").encode())
                        spec = dict(self.spec, sources=[ref])
                        request = dict(self.args, title=name)
                        target = core.ROOT / "00_Scope/W1" / (name + ".md")
                        if operation == "update":
                            old = write.create_node(name, "prior", "prior body", "test-model", space="00_Scope/W1")
                            request = dict(name=old["id"], body=self.args["body"], expect_hash=old["new_hash"])
                        source = D._source(ref, None)
                        source.update(ref=ref, path=raw_path.relative_to(core.ROOT).as_posix())
                        atomic, save = write._atomic_write, D._save
                        reserved = []

                        def v1_save(job):
                            job["version"] = 1
                            save(job)

                        def crash(path, data):
                            if path == target:
                                reserved.append(data)
                                raise Crash()
                            return atomic(path, data)

                        with mock.patch.object(write, "_as_links", side_effect=v1_links),\
                                mock.patch.object(D, "_sources", return_value=[source]),\
                                mock.patch.object(D, "_save", side_effect=v1_save),\
                                mock.patch.object(write, "_atomic_write", side_effect=crash):
                            with self.assertRaises(Crash):
                                D._execute(operation, spec, request)
                        journal = D._job_path(self.key).read_bytes()
                        job = D._load(self.key)
                        self.assertEqual(job["target"]["hash"], core.sha256_bytes(reserved[0]))
                        raw.migrate(apply=True)
                        migrated = raw._raw_file(source["path"])
                        evidence = migrated.read_bytes()
                        migrated.write_bytes(evidence.replace(b"original source", b"changed source"))
                        failed = D._execute(operation, spec, request)
                        self.assertEqual(failed["distillation"]["status"], "pending")
                        self.assertIn("source", failed["distillation"]["reason"])
                        migrated.write_bytes(evidence)
                        with self.assertRaises(write.WriteError):
                            D._execute(operation, spec, dict(request, body="different request"))
                        # A forged expected hash cannot be replaced by retry.
                        job["target"]["hash"] = "sha256:" + "0" * 64
                        D._save(job)
                        failed = D._execute(operation, spec, request)
                        self.assertEqual(failed["distillation"]["status"], "pending")
                        self.assertIn("prepared target changed", failed["distillation"]["reason"])
                        D._job_path(self.key).write_bytes(journal)
                        result = D._execute(operation, spec, request)
                        self.assertEqual(result["distillation"]["status"], "complete")
                        self.assertEqual(target.read_bytes(), reserved[0])
                        self.assertEqual(D._job_path(self.key).read_bytes(), journal)
                        self.assertEqual(D.discover([ref])["proofs"][0]["key"], self.key)
                        D._job_path(self.key).unlink()

            def test_new_raw_journal_retries_in_plain_format(self):
                record = raw.append_round(self.name, self.name + "-raw", "q", "a", "00_Scope/W1")
                self.spec["sources"] = [record["round_ref"]]
                atomic = write._atomic_write
                target = core.ROOT / "00_Scope/W1" / (self.args["title"] + ".md")
                reserved = []

                def crash(path, data):
                    if path == target:
                        reserved.append(data)
                        raise Crash()
                    return atomic(path, data)

                with mock.patch.object(write, "_atomic_write", side_effect=crash):
                    with self.assertRaises(Crash):
                        self.create()
                self.assertEqual(D._load(self.key)["version"], 2)
                out = self.create()
                self.assertEqual(out["distillation"]["status"], "complete")
                self.assertEqual(target.read_bytes(), reserved[0])
                self.assertNotIn("[[", contract.parse(target).meta["derived-from"])
                self.assertEqual(D.discover([record["round_ref"]])["proofs"][0]["key"], self.key)

            def test_source_mutation_blocks_resume(self):
                with self.fail_hub():
                    out = self.create()
                before = (core.ROOT / out["path"]).read_bytes()
                write.update_node(self.source["id"], body="evidence was corrected",
                                  expect_hash=self.source["new_hash"])
                again = self.create()
                self.assertEqual(again["distillation"]["status"], "pending")
                self.assertIn("source", again["distillation"]["reason"])
                self.assertEqual((core.ROOT / out["path"]).read_bytes(), before)
                self.assertNotIn(self.args["title"], contract.parse(self.hub).wikilinks())

            def test_concurrent_target_update_is_not_overwritten(self):
                with self.fail_hub():
                    out = self.create()
                target = core.ROOT / out["path"]
                write.update_node(out["id"], body="another conversation revised this",
                                  expect_hash=core.sha256_file(target))
                concurrent = target.read_bytes()
                again = self.create()
                self.assertFalse(again["ok"])
                self.assertEqual(again["distillation"]["status"], "pending")
                self.assertEqual(target.read_bytes(), concurrent)

            def test_invalid_hub_ref_and_empty_body(self):
                for spec in (
                    dict(self.spec, hub="missing-hub"),
                    dict(self.spec, hub=self.source["name"]),
                    dict(self.spec, sources=["missing-source"]),
                    dict(self.spec, sources=["[[00_Scope/W1/_raw/missing.md#1]]"]),
                    dict(self.spec, sources=[dict(self.spec["sources"][0], hash="stale")]),
                ):
                    with self.assertRaises(write.WriteError):
                        D.create_node(spec, **self.args)
                with self.assertRaises(write.WriteError):
                    D.create_node(self.spec, **dict(self.args, body=" \n"))
                self.assertIsNone(D._load(self.key))
                self.assertFalse((core.ROOT / "00_Scope/W1" / (self.args["title"] + ".md")).exists())

            def test_key_cannot_bind_different_request(self):
                out = self.create()
                with self.assertRaises(write.WriteError):
                    D.create_node(self.spec, **dict(self.args, body="different conclusion"))
                self.assertEqual(D.status(self.key)["target"]["hash"], out["new_hash"])

            def test_update_records_before_hash(self):
                existing = write.create_node(self.args["title"], "old", "old knowledge",
                                             "fable-5", space="00_Scope/W1")
                out = D.update_node(self.spec, name=existing["id"],
                                    body=self.args["body"], expect_hash=existing["new_hash"])
                self.assertEqual(out["distillation"]["status"], "complete")
                self.assertEqual(out["distillation"]["target"]["before_hash"], existing["new_hash"])
                again = D.update_node(self.spec, name=existing["id"],
                                      body=self.args["body"], expect_hash=existing["new_hash"])
                self.assertEqual(again["distillation"]["status"], "complete")
                self.assertEqual(again["id"], existing["id"])

            def test_mcp_update_rechecks_use_observed_states(self):
                import mcp_server as M
                from osk import rechecks

                for case in ("fresh", "source_changed", "target_changed", "unread"):
                    with self.subTest(case=case):
                        M._SEEN.clear()
                        existing = write.create_node(self.args["title"] + "-" + case,
                            "old", "old knowledge", "fable-5", space="00_Scope/W1",
                            edges={"derived-from": self.source["id"]})
                        if case != "unread":
                            M.read_node(self.source["id"])
                            M.read_node(existing["id"])
                        if case == "source_changed":
                            write.update_node(self.source["id"], old_text="observed evidence",
                                              new_text="corrected evidence")
                        if case == "target_changed":
                            write.update_node(existing["id"], old_text="old knowledge",
                                              new_text="old knowledge\nConcurrent addition.")
                        spec = dict(self.spec, sources=[self.source["id"]])
                        request = dict(name=existing["id"], old_text="old knowledge",
                                       new_text="revised knowledge", distill=spec)

                        def pending():
                            return [r for r in rechecks.candidates()[0] if r["id"] == existing["id"]]

                        try:
                            out = M.update_node(**request)
                            self.assertTrue(out["ok"], out)
                            self.assertEqual(out["distillation"]["status"], "complete")
                            self.assertEqual(bool(out.get("recheck_unread")), case != "fresh", out)
                            self.assertEqual(bool(pending()), case != "fresh")
                            # Process-local reads must not change the persisted request binding.
                            M._SEEN.clear()
                            again = M.update_node(**request)
                            self.assertTrue(again["resumed"], again)
                            self.assertEqual(again["new_hash"], out["new_hash"])
                            resumed = M.update_node(existing["id"], distill={"resume": self.key})
                            self.assertTrue(resumed["resumed"], resumed)
                            self.assertEqual(bool(pending()), case != "fresh")
                            M.read_node(self.source["id"])
                            M.read_node(existing["id"])
                            closed = M.update_node(existing["id"],
                                                   add_edges={"derived-from": self.source["id"]})
                            self.assertTrue(closed.get("rechecked"), closed)
                            self.assertFalse(pending())
                        finally:
                            D._job_path(self.key).unlink(missing_ok=True)
                            M._SEEN.clear()

            def test_domain_distillation_uses_scope_provenance(self):
                domain = core.ROOT / "00_Domain" / self.name
                domain.mkdir()
                write.create_node(self.name, "domain hub", "general reusable knowledge",
                                  "fable-5", space=domain.relative_to(core.ROOT).as_posix())
                self.spec["hub"] = self.name
                self.args["space"] = domain.relative_to(core.ROOT).as_posix()
                out = self.create()
                self.assertEqual(out["distillation"]["status"], "complete")
                self.assertEqual(out["distillation"]["sources"][0]["ref"], self.source["id"])

            def test_exact_raw_anchor_and_append_stability(self):
                record = raw.append_rounds(self.name, self.name,
                    [{"user": "first observation", "agent": "first result"},
                     {"user": "second observation", "agent": "second result"}], space="00_Scope/W1")
                self.spec["sources"] = [record["round_refs"][0]]
                out = self.create()
                self.assertEqual(out["distillation"]["status"], "complete")
                node = contract.parse(core.ROOT / out["path"])
                self.assertIn(record["round_refs"][0],
                              write._stored_edges(node.meta["derived-from"]))
                raw.append_rounds(self.name, self.name,
                    [{"user": "third observation", "agent": "third result"}])
                self.assertEqual(D.status(self.key)["status"], "complete")
                # Isolate exact-anchor verification: even a matching target byte
                # receipt cannot turn round #2 into evidence from round #1.
                write.update_node(out["id"], remove_edges={"derived-from": record["round_refs"][0]},
                                  add_edges={"derived-from": record["round_refs"][1]})
                job = D._load(self.key)
                job["target"]["hash"] = core.sha256_file(core.ROOT / out["path"])
                D._save(job)
                checked = D.status(self.key)
                self.assertEqual(checked["status"], "pending")
                self.assertIn("provenance", checked["reason"])


            def test_later_worker_resumes_without_original_request(self):
                with self.fail_hub():
                    out = self.create()
                target = core.ROOT / out["path"]
                before = target.read_bytes()
                resumed = D.update_node({"resume": self.key}, name=out["id"],
                                        body=None, expect_hash=None, summary=None,
                                        add_edges=None, remove_edges=None, old_text=None,
                                        new_text=None, settle=None)
                self.assertEqual(resumed["distillation"]["status"], "complete")
                self.assertEqual(target.read_bytes(), before)

            def test_resume_rejects_wrong_name_and_mutations(self):
                with self.fail_hub():
                    out = self.create()
                with self.assertRaises(write.WriteError):
                    D.update_node({"resume": self.key}, name=self.source["id"])
                for extra in ({"body": "mutation"}, {"summary": "mutation"},
                              {"add_edges": {}}, {"old_text": "old", "new_text": "new"},
                              {"settle": "anything"}, {"expect_hash": out["new_hash"]}):
                    with self.assertRaises(write.WriteError):
                        D.update_node({"resume": self.key}, name=out["id"], **extra)

            def test_later_worker_cannot_create_missing_reserved_target(self):
                atomic = write._atomic_write
                target = core.ROOT / "00_Scope/W1" / (self.args["title"] + ".md")
                def crash(path, data):
                    if path == target:
                        raise Crash()
                    return atomic(path, data)
                with mock.patch.object(write, "_atomic_write", side_effect=crash):
                    with self.assertRaises(Crash):
                        self.create()
                job = D._load(self.key)
                out = D.update_node({"resume": self.key}, name=job["target"]["id"])
                self.assertFalse(out["ok"])
                self.assertEqual(out["distillation"]["status"], "pending")
                self.assertIn("original request", out["distillation"]["reason"])
                self.assertFalse(target.exists())

            def test_shared_receipt_survives_local_journal_loss(self):
                out = self.create()
                receipt = out["distillation"]
                D._job_path(self.key).unlink()
                self.assertEqual(D.status(self.key)["status"], "pending")
                self.assertEqual(D.verify_receipt(receipt)["status"], "complete")
                target = core.ROOT / out["path"]
                write.update_node(out["id"], body="changed after synchronization",
                                  expect_hash=core.sha256_file(target))
                self.assertEqual(D.verify_receipt(receipt)["status"], "pending")

            def test_two_jobs_same_hub_keep_receipts_complete(self):
                self.create()
                second_key = self.key + "-second"
                try:
                    second = D.create_node(dict(self.spec, key=second_key),
                                           **dict(self.args, title=self.args["title"] + "-second"))
                    self.assertEqual(second["distillation"]["status"], "complete")
                    self.assertEqual(D.status(self.key)["status"], "complete")
                finally:
                    D._job_path(second_key).unlink(missing_ok=True)

            def test_resume_preserves_concurrent_hub_addition(self):
                with self.fail_hub():
                    self.create()
                prior = contract.parse(self.hub).body
                write.update_node("W1", body=prior + "\\n- [[" + self.source["name"] + "]]",
                                  expect_hash=core.sha256_file(self.hub))
                out = self.create()
                self.assertEqual(out["distillation"]["status"], "complete")
                links = contract.parse(self.hub).wikilinks()
                self.assertIn(self.source["name"], links)
                self.assertIn(self.args["title"], links)

            def test_hub_link_removal_invalidates_receipt(self):
                self.create()
                write.update_node("W1", old_text="- [[" + self.args["title"] + "]]",
                                  new_text="")
                self.assertEqual(D.status(self.key)["status"], "pending")

            def test_unrelated_existing_citation_survives(self):
                record = core.ROOT / "00_Scope/W1/_raw/legacy-reference.md"
                record.parent.mkdir(exist_ok=True)
                record.write_text("legacy section content", encoding="utf-8")
                self.args["edges"] = {"derived-from": "[[00_Scope/W1/_raw/legacy-reference.md#section]]"}
                out = self.create()
                self.assertEqual(out["distillation"]["status"], "complete")

            def test_anchor_normalization_noop_is_rejected(self):
                existing = write.create_node(self.args["title"], "old", "retained knowledge",
                                             "fable-5", space="00_Scope/W1")
                with self.assertRaises(write.WriteError):
                    D.update_node(self.spec, name=existing["id"],
                                  old_text="retained knowledge", new_text="retained knowledge  ")
                self.assertIsNone(D._load(self.key))
                self.assertEqual(core.sha256_file(core.ROOT / existing["path"]), existing["new_hash"])

            def test_required_provenance_alias_cannot_be_removed(self):
                existing = write.create_node(self.args["title"], "old", "old retained knowledge",
                                             "fable-5", space="00_Scope/W1")
                alias = "[[" + self.name + "-source|근거]]"
                with self.assertRaises(write.WriteError):
                    D.update_node(self.spec, name=existing["id"], body="new retained knowledge",
                                  expect_hash=existing["new_hash"],
                                  remove_edges={"derived-from": alias})
                self.assertIsNone(D._load(self.key))
                self.assertEqual(core.sha256_file(core.ROOT / existing["path"]), existing["new_hash"])

            def test_required_provenance_cannot_be_removed(self):
                existing = write.create_node(self.args["title"], "old", "old retained knowledge",
                                             "fable-5", space="00_Scope/W1")
                with self.assertRaises(write.WriteError):
                    D.update_node(self.spec, name=existing["id"], body="new retained knowledge",
                                  expect_hash=existing["new_hash"],
                                  remove_edges={"derived-from": self.source["id"]})
                self.assertIsNone(D._load(self.key))
                self.assertEqual(core.sha256_file(core.ROOT / existing["path"]), existing["new_hash"])


        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Recovery)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return result.wasSuccessful()


if __name__ == "__main__":
    if "--child" in sys.argv:
        raise SystemExit(0 if _child() else 1)
    test_distillation()
