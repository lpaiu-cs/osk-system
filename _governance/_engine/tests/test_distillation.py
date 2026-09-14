"""Real write-path recovery tests in a subprocess-owned temporary vault.

Faults are injected at disk-write boundaries; these are implementation tests,
not model-behavior or semantic-quality evaluations.
"""
from __future__ import annotations
import os
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
                                                "observed evidence", "fable-5", space="= Scope/W1")
                self.spec = {"key": self.key,
                             "sources": [{"ref": self.source["id"], "hash": self.source["new_hash"]}],
                             "hub": "W1"}
                self.args = {"title": self.name + "-target", "summary": "retained decision",
                             "body": "Use a bounded operation because retries can lose their response.",
                             "drafter": "fable-5", "space": "= Scope/W1"}
                self.hub = core.ROOT / "= Scope/W1/W1.md"

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
                target = core.ROOT / "= Scope/W1" / (self.args["title"] + ".md")
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
                target = core.ROOT / "= Scope/W1" / (self.args["title"] + ".md")
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
                    dict(self.spec, sources=["[[= Scope/W1/_raw/missing.md#1]]"]),
                    dict(self.spec, sources=[dict(self.spec["sources"][0], hash="stale")]),
                ):
                    with self.assertRaises(write.WriteError):
                        D.create_node(spec, **self.args)
                with self.assertRaises(write.WriteError):
                    D.create_node(self.spec, **dict(self.args, body=" \n"))
                self.assertIsNone(D._load(self.key))
                self.assertFalse((core.ROOT / "= Scope/W1" / (self.args["title"] + ".md")).exists())

            def test_key_cannot_bind_different_request(self):
                out = self.create()
                with self.assertRaises(write.WriteError):
                    D.create_node(self.spec, **dict(self.args, body="different conclusion"))
                self.assertEqual(D.status(self.key)["target"]["hash"], out["new_hash"])

            def test_update_records_before_hash(self):
                existing = write.create_node(self.args["title"], "old", "old knowledge",
                                             "fable-5", space="= Scope/W1")
                out = D.update_node(self.spec, name=existing["id"],
                                    body=self.args["body"], expect_hash=existing["new_hash"])
                self.assertEqual(out["distillation"]["status"], "complete")
                self.assertEqual(out["distillation"]["target"]["before_hash"], existing["new_hash"])
                again = D.update_node(self.spec, name=existing["id"],
                                      body=self.args["body"], expect_hash=existing["new_hash"])
                self.assertEqual(again["distillation"]["status"], "complete")
                self.assertEqual(again["id"], existing["id"])

            def test_domain_distillation_uses_scope_provenance(self):
                domain = core.ROOT / "= Domain" / self.name
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
                     {"user": "second observation", "agent": "second result"}], space="= Scope/W1")
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
                target = core.ROOT / "= Scope/W1" / (self.args["title"] + ".md")
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
                record = core.ROOT / "= Scope/W1/_raw/legacy-reference.md"
                record.parent.mkdir(exist_ok=True)
                record.write_text("legacy section content", encoding="utf-8")
                self.args["edges"] = {"derived-from": "[[= Scope/W1/_raw/legacy-reference.md#section]]"}
                out = self.create()
                self.assertEqual(out["distillation"]["status"], "complete")

            def test_anchor_normalization_noop_is_rejected(self):
                existing = write.create_node(self.args["title"], "old", "retained knowledge",
                                             "fable-5", space="= Scope/W1")
                with self.assertRaises(write.WriteError):
                    D.update_node(self.spec, name=existing["id"],
                                  old_text="retained knowledge", new_text="retained knowledge  ")
                self.assertIsNone(D._load(self.key))
                self.assertEqual(core.sha256_file(core.ROOT / existing["path"]), existing["new_hash"])

            def test_required_provenance_cannot_be_removed(self):
                existing = write.create_node(self.args["title"], "old", "old retained knowledge",
                                             "fable-5", space="= Scope/W1")
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
