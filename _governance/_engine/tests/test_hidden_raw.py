"""Storage migration checks in an isolated vault; no live vault or GUI claims."""
import copy
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock


def main():
    with tempfile.TemporaryDirectory(prefix="osk-hidden-raw-") as folder:
        os.environ["OSK_VAULT_ROOT"] = folder
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from osk import core, contract, distillation as D, graph, raw, secrets, validate, write
        validate.make_mini_vault(folder)
        root = core.ROOT
        assert root == Path(folder).resolve()
        base = root / "= Scope/W1/_raw"
        base.mkdir(exist_ok=True)

        def reject(fn, *args, **kwargs):
            try:
                fn(*args, **kwargs)
            except (ValueError, OSError):
                return
            raise AssertionError("unsafe operation accepted")

        # New writes are neither Markdown files nor Obsidian wiki links.
        first = raw.append_round("hidden-test", "new", "observation", "answer", "= Scope/W1")
        new = root / first["path"]
        assert new.suffix == ".txt" and new.parent.name == ".records"
        assert "[[" not in first["round_ref"]
        assert not (base / "new.md").exists()
        assert raw.read_round("[[= Scope/W1/_raw/new.md#1]]")["text"] == raw.read_round(first["round_ref"])["text"]
        assert graph.Index().resolve("= Scope/W1/_raw/new.md") == ("nonnode", ("raw", "W1"))
        reject(secrets.write_raw, base / "bypass.md", "not allowed")
        assert not (base / "bypass.md").exists()
        url = "https://example.invalid/_raw/evidence.md#1"
        assert raw.canonical_ref(url) == url
        assert write._as_links("derived-from", url) == f"[[{url}]]"
        assert write._as_links("derived-from", f"[[{url}]]") == f"[[{url}]]"
        assert write._as_links("derived-from", f"[[ {url} ]]") == f"[[ {url} ]]"
        assert raw.canonical_ref("[[ = Scope/W1/_raw/new.md#1 ]]") == first["round_ref"]
        assert raw.canonical_ref("= Scope/W1/_raw/new.md #1") == first["round_ref"]
        assert graph.Index().resolve(url) == ("external",)
        reject(raw.append_round, "hidden-test", "x" * 252, "q", "a")
        # A contained symlink must not move another scope's canonical record.
        with mock.patch.object(raw, "resolve_in_root", return_value=new):
            reject(raw._record_pair, base / "redirect.md")

        # CRLF, Unicode, escaped headings and markers survive a byte-exact rename.
        old = base / "Legacy.MD"
        original = (raw._block(1, "질문\r\n\\## 2", "답변", codex_native=True)
                    .replace("\n", "\r\n").encode())
        old.write_bytes(original)
        legacy = "[[= Scope/W1/_raw/Legacy.MD#1]]"
        before = raw.read_round(legacy)["text"]
        plan = raw.migrate()
        assert plan["count"] == 1 and old.read_bytes() == original
        moved = raw.migrate(apply=True)
        dest = root / moved["files"][0]["to"]
        assert not old.exists() and dest.read_bytes() == original
        assert raw.read_round(legacy)["text"] == before
        assert raw.migrate(apply=True)["count"] == 0
        assert {r["record"] for r in raw.list_records("= Scope/W1")["records"]} == {"new", "Legacy"}
        assert raw.record_path("W1", "LEGACY") == dest

        # Duplicate destinations fail closed, including a same-byte duplicate.
        old.write_bytes(original)
        reject(raw.migrate, apply=True)
        reject(raw.append_round, "hidden-test", "Legacy", "new question", "new answer")
        assert old.read_bytes() == dest.read_bytes() == original
        old.unlink()  # Test fixture cleanup only.

        # NFC/NFD duplicates can coexist even on Windows. Never choose one.
        for directory, suffix in ((base, ".md"), (base / ".records", ".txt")):
            duplicates = [directory / (stem + suffix) for stem in ("caf\u00e9", "cafe\u0301")]
            for i, path in enumerate(duplicates):
                path.write_bytes(raw._block(1, f"different source {i}", "a").encode())
            frozen = {path: path.read_bytes() for path in duplicates}
            reject(raw.record_path, "W1", "caf\u00e9")
            reject(raw.append_round, "hidden-test", "caf\u00e9", "q2", "a2")
            if suffix == ".md":
                reject(raw.migrate, apply=True)
            assert all(path.read_bytes() == data for path, data in frozen.items())
            for path in duplicates:
                path.unlink()  # Isolated conflicting fixtures only.

        # A failed rename keeps old bytes; retry converges without changing rounds.
        crash = base / "crash.md"
        crash.write_bytes(raw._block(1, "q", "a").encode())
        with mock.patch.object(Path, "rename", side_effect=OSError("injected rename failure")):
            reject(raw.migrate, apply=True)
        assert crash.is_file()
        raw.migrate(apply=True)
        assert raw.read_round("= Scope/W1/_raw/crash.md#1")["index"] == 1

        # Existing capture retries migrate even when there is nothing to append.
        retry = base / "retry.md"
        retry.write_bytes(raw._block(1, "q", "a").encode())
        replay = raw.append_rounds("hidden-test", "retry", [{"user": "q", "agent": "a"}], replay_prefix=True)
        assert replay["appended"] == 0 and not retry.exists()
        appended = raw.append_round("hidden-test", "retry", "q2", "a2")
        assert appended["index"] == 2
        assert raw.read_round("= Scope/W1/_raw/retry.md")["rounds"] == 2

        # Old receipts still verify after migration; new PE values are plain.
        record = base / "proof.md"
        record.write_bytes(raw._block(1, "durable observation", "verified result").encode())
        ref = "[[= Scope/W1/_raw/proof.md#1]]"
        out = D.create_node({"key": "legacy-proof", "sources": [ref], "hub": "W1"},
                            title="hidden-raw-proof", summary="preserved observation",
                            body="The observed result is retained.", drafter="test-model", space="= Scope/W1")
        assert out["distillation"]["status"] == "complete", out
        target = root / out["path"]
        saved = target.read_bytes()
        assert "[[" not in str(contract.parse(target).meta["derived-from"])
        historic = copy.deepcopy(out["distillation"])
        historic["sources"][0].update(ref=ref, path="= Scope/W1/_raw/proof.md")
        raw.migrate(apply=True)
        assert target.read_bytes() == saved
        assert D.verify_receipt(historic)["status"] == "complete"
        assert D.status("legacy-proof")["status"] == "complete"
        raw.append_round("hidden-test", "proof", "later", "reply")
        assert D.verify_receipt(historic)["status"] == "complete"
        proof_path = raw._raw_file("= Scope/W1/_raw/proof.md")
        proof_path.write_bytes(proof_path.read_bytes().replace(b"verified result", b"tampered result"))
        assert D.verify_receipt(historic)["status"] == "pending"
        reject(raw.read_round, "../../outside.md#1")
        assert not list(base.glob("*.md")) and not list(base.glob("*.MD"))
        print("PASS hidden storage, aliases, byte identity, conflict/retry, replay, provenance")


if __name__ == "__main__":
    main()
