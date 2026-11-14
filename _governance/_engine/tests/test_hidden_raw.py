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
        from osk.vault import Vault
        VAULT = Vault.open()
        validate.make_mini_vault(VAULT, folder)
        root = VAULT.root
        assert root == Path(folder).resolve()
        base = root / "00_Scope/W1/_raw"
        base.mkdir(exist_ok=True)

        def append_round(session, record, user, agent, space=None):
            r = raw.append_rounds(VAULT, session, record, [{"user": user, "agent": agent}], space)
            return {"path": r["path"], "index": r["indices"][0], "round_ref": r["round_refs"][0]}

        def reject(fn, *args, **kwargs):
            try:
                fn(*args, **kwargs)
            except (ValueError, OSError):
                return
            raise AssertionError("unsafe operation accepted")

        # New writes are neither Markdown files nor Obsidian wiki links, and live in _cited.
        first = append_round("hidden-test", "new", "observation", "answer", "00_Scope/W1")
        new = root / first["path"]
        assert new.suffix == ".txt" and new.parent.name == ".records"
        assert new.parent.parent == root / "_cited"          # 루트 `_cited/` (헌법 4조 3항)
        assert "[[" not in first["round_ref"]
        assert not (base / "new.md").exists() and not (base / ".records/new.txt").exists()
        # 옛 scope 자리의 좌표는 같은 이름의 루트 기록으로 접힌다(Mechanism §8 2항 · §9 5항)
        assert raw.read_round(VAULT, "[[00_Scope/W1/_cited/new.md#1]]")["text"] == raw.read_round(VAULT, first["round_ref"])["text"]
        assert graph.Index(VAULT).resolve("00_Scope/W1/_cited/new.md") == ("nonnode", ("raw", None))
        assert graph.Index(VAULT).resolve(first["path"]) == ("nonnode", ("raw", None))
        reject(raw.write_raw, VAULT, base / "bypass.md", "not allowed")
        assert not (base / "bypass.md").exists()
        url = "https://example.invalid/_raw/evidence.md#1"
        assert raw.canonical_ref(VAULT, url) == url
        assert write._as_links(VAULT, "derived-from", url) == f"[[{url}]]"
        assert write._as_links(VAULT, "derived-from", f"[[{url}]]") == f"[[{url}]]"
        assert write._as_links(VAULT, "derived-from", f"[[ {url} ]]") == f"[[ {url} ]]"
        assert raw.canonical_ref(VAULT, "[[ 00_Scope/W1/_cited/new.md#1 ]]") == first["round_ref"]
        assert raw.canonical_ref(VAULT, "00_Scope/W1/_cited/new.md #1") == first["round_ref"]
        assert graph.Index(VAULT).resolve(url) == ("external",)
        reject(append_round, "hidden-test", "x" * 253, "q", "a")
        # A contained symlink must not move another scope's canonical record.
        with mock.patch.object(raw, "resolve_in_root", return_value=new):
            reject(raw._record_pair, VAULT, base / "redirect.md")

        # CRLF, Unicode, escaped headings and markers survive a byte-exact rename.
        old = base / "Legacy.MD"
        original = (f"## 1\n\n{raw._CODEX_V2}\n\n### user\n\n질문\r\n\\## 2\n\n### agent\n\n답변\n"
                    .replace("\n", "\r\n").encode())
        old.write_bytes(original)
        legacy = "[[00_Scope/W1/_raw/Legacy.MD#1]]"
        before = raw.read_round(VAULT, legacy)["text"]
        plan = raw.migrate(VAULT)
        assert plan["count"] == 1 and old.read_bytes() == original
        moved = raw.migrate(VAULT, apply=True)
        dest = root / moved["files"][0]["to"]
        assert not old.exists() and dest.read_bytes() == original
        assert raw.read_round(VAULT, legacy)["text"] == before
        assert raw.migrate(VAULT, apply=True)["count"] == 0
        assert {r["record"] for r in raw.list_records(VAULT, "00_Scope/W1")["records"]} == {"Legacy"}   # 옛 자리
        assert {r["record"] for r in raw.list_records(VAULT)["records"]} == {"new"}                 # 루트
        assert raw.record_path(VAULT, "W1", "LEGACY") == dest

        # Duplicate destinations fail closed, including a same-byte duplicate.
        old.write_bytes(original)
        reject(raw.migrate, VAULT, apply=True)
        reject(append_round, "hidden-test", "Legacy", "new question", "new answer")
        assert old.read_bytes() == dest.read_bytes() == original
        old.unlink()  # Test fixture cleanup only.

        # NFC/NFD duplicates can coexist even on Windows. Never choose one.
        for directory, suffix in ((base, ".md"), (base / ".records", ".txt")):
            duplicates = [directory / (stem + suffix) for stem in ("caf\u00e9", "cafe\u0301")]
            for i, path in enumerate(duplicates):
                path.write_bytes(raw.round_block(1, f"different source {i}", "a").encode())
            frozen = {path: path.read_bytes() for path in duplicates}
            if os.path.samefile(*duplicates):
                # APFS/HFS+ fold normalization: the second write reopened the first
                # file, so one physical record exists and there is nothing to choose.
                assert os.path.samefile(raw.record_path(VAULT, "W1", "caf\u00e9"), duplicates[0])
            else:
                reject(raw.record_path, VAULT, "W1", "caf\u00e9")
                reject(append_round, "hidden-test", "caf\u00e9", "q2", "a2")
                if suffix == ".md":
                    reject(raw.migrate, VAULT, apply=True)
            assert all(path.read_bytes() == data for path, data in frozen.items())
            for path in duplicates:
                path.unlink(missing_ok=True)  # Isolated conflicting fixtures only.

        # Old 255-byte filenames remain readable and migrate without shortening.
        # This also runs on ext4: .txt on the old stem would raise ENAMETOOLONG.
        for name in ("x" * 252, "가" * 84, "e\u0301" * 84):
            old = base / (name + ".md")
            original = raw.round_block(1, "q", "a").encode()
            old.write_bytes(original)
            ref = f"00_Scope/W1/_raw/{name}.md#1"
            assert raw.read_round(VAULT, ref)["index"] == 1
            assert name in {r["record"] for r in raw.list_records(VAULT, "00_Scope/W1")["records"]}
            raw.migrate(VAULT, apply=True)
            dest = raw.record_path(VAULT, "W1", name)
            assert dest.name == "record.txt" and dest.parent.name == name
            assert all(len(p.encode("utf-8")) <= 255 for p in dest.relative_to(base).parts)
            assert dest.read_bytes() == original and not old.exists()
            assert raw.read_round(VAULT, ref)["text"] == raw.read_round(VAULT, str(dest) + "#1")["text"]
            assert raw.canonical_ref(VAULT, ref) == raw.canonical_ref(VAULT, str(dest) + "#1")
            alternate = write.unicodedata.normalize("NFC", name.upper())
            assert raw.record_path(VAULT, "W1", alternate) == dest
            assert append_round("hidden-test", alternate, "q2", "a2")["index"] == 2
            assert dest.read_bytes().startswith(original)
            assert name in {r["record"] for r in raw.list_records(VAULT, "00_Scope/W1")["records"]}
            # No old/new ambiguity or normalization-equivalent directory twin.
            old.write_bytes(original)
            reject(raw.read_round, VAULT, ref)
            reject(raw.migrate, VAULT, apply=True)
            old.unlink()
            normalized = write.unicodedata.normalize("NFC", name)
            if normalized != name:
                twin = base / ".records" / normalized
                if twin.exists():  # Normalization-insensitive FS: the twin is dest.parent.
                    assert os.path.samefile(twin, dest.parent)
                else:
                    twin.mkdir()
                    reject(raw.record_path, VAULT, "W1", name)
                    reject(append_round, "hidden-test", name, "q3", "a3")
                    twin.rmdir()
                flat = base / ".records" / (normalized + ".txt")
                flat.write_bytes(original)
                reject(raw.record_path, VAULT, "W1", name)
                flat.unlink()
        assert raw.migrate(VAULT, apply=True)["count"] == 0

        # New maximum-length names use the same lossless layout.
        fresh_long = append_round("hidden-test", "z" * 252, "q", "a")
        assert (root / fresh_long["path"]).name == "record.txt"
        assert "_cited" in Path(fresh_long["path"]).parts

        # A failed rename keeps old bytes; retry converges without changing rounds.
        crash = base / "crash.md"
        crash.write_bytes(raw.round_block(1, "q", "a").encode())
        with mock.patch.object(Path, "rename", side_effect=OSError("injected rename failure")):
            reject(raw.migrate, VAULT, apply=True)
        assert crash.is_file()
        raw.migrate(VAULT, apply=True)
        assert raw.read_round(VAULT, "00_Scope/W1/_raw/crash.md#1")["index"] == 1

        # The next append to a visible legacy record migrates it and keeps its rounds.
        retry = base / "retry.md"
        retry.write_bytes(raw.round_block(1, "q", "a").encode())
        appended = append_round("hidden-test", "retry", "q2", "a2")
        assert appended["index"] == 2 and not retry.exists()
        assert raw.read_round(VAULT, "00_Scope/W1/_raw/retry.md")["rounds"] == 2

        # Old receipts still verify after migration; the round is bound by its evidence line.
        record = base / "proof.md"
        record.write_bytes(raw.round_block(1, "durable observation", "verified result").encode())
        ref = "[[00_Scope/W1/_raw/proof.md#1]]"
        out = D.create_node(VAULT, {"key": "legacy-proof", "sources": [ref], "hub": "W1"},
                            title="hidden-raw-proof", summary="preserved observation",
                            body="The observed result is retained.\n\n# 관측\n\n"
                                 "- 2026-10-31 · `proof#1` · 지지 — durable observation",
                            drafter="test-model", space="00_Scope/W1")
        assert out["distillation"]["status"] == "complete", out
        target = root / out["path"]
        saved = target.read_bytes()
        assert "derived-from" not in contract.parse(target).meta
        historic = copy.deepcopy(out["distillation"])
        historic["sources"][0].update(ref=ref, path="00_Scope/W1/_raw/proof.md")
        raw.migrate(VAULT, apply=True)
        assert target.read_bytes() == saved
        assert D.verify_receipt(VAULT, historic)["status"] == "complete"
        assert D.status(VAULT, "legacy-proof")["status"] == "complete"
        append_round("hidden-test", "proof", "later", "reply")
        assert D.verify_receipt(VAULT, historic)["status"] == "complete"
        proof_path = raw.raw_file(VAULT, "00_Scope/W1/_raw/proof.md")
        proof_path.write_bytes(proof_path.read_bytes().replace(b"verified result", b"tampered result"))
        assert D.verify_receipt(VAULT, historic)["status"] == "pending"
        reject(raw.read_round, VAULT, "../../outside.md#1")
        assert not list(base.glob("*.md")) and not list(base.glob("*.MD"))

        # `doctor --migrate` gathers old-place records into the root `_cited/` (Mechanism §9 5항):
        # it plans first, moves only on the confirmed second call, and keeps bytes, old
        # coordinates and receipts. A copy shadowed by a root record is reported, never moved.
        from osk import cli, doctor
        proof_path.write_bytes(proof_path.read_bytes().replace(b"tampered result", b"verified result"))
        assert D.verify_receipt(VAULT, historic)["status"] == "complete"
        before = {r["record"]: raw.read_round(VAULT, r["path"] + "#1")["text"]
                  for r in raw.list_records(VAULT, "00_Scope/W1")["records"]}
        shadow = base / ".records" / "new.txt"
        shadow.write_bytes(raw.round_block(1, "older copy", "a").encode())
        frozen = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
        plan = raw.consolidate(VAULT)
        assert plan["count"] == len(before) and not plan["applied"], plan
        assert [c["path"] for c in plan["conflicts"]] == ["00_Scope/W1/_raw/.records/new.txt"], plan
        records = "옛 자리의 인용 기록 → 루트 `_cited/` (v5.2)"
        rep, code = doctor.vault(VAULT, cli.published_tools)
        checks = {i["check"]: i for i in rep["items"]}
        assert checks[records]["level"] == "warn" and "기록 충돌" in checks, rep["items"]
        rep, code = doctor.migrate(VAULT)
        assert code == 2 and rep["approval_required"], rep
        assert dict(rep["plan"]["migrations"]) == {records: [[m["from"], m["hash"]] for m in plan["files"]]}, rep
        assert rep["plan"]["root"] == str(root)            # worktree끼리 확인표 자리를 함께 써도 계획이 갈린다
        assert {p: p.read_bytes() for p in base.rglob("*") if p.is_file()} == frozen   # 계획은 옮기지 않는다
        changed = base / ".records" / "retry.txt"
        changed.write_bytes(frozen[changed] + b"\n")       # 보인 뒤에 바뀐 기록은 다시 확인받는다
        assert doctor.migrate(VAULT)[1] == 2 and changed.exists()
        changed.write_bytes(frozen[changed])
        assert doctor.migrate(VAULT)[1] == 2
        rep, code = doctor.migrate(VAULT)
        assert code == 0 and [(a["migration"], a["count"]) for a in rep["applied"]] == [(records, len(before))], rep
        # Mechanism §5: each move is in the migration ledger before it runs, and the manifest opens with every
        # record and closes with the result and the conflicts held (#317 review).
        from osk import evidence_migration as em
        moved = [e for e in core.ledger_read(em.events_path(VAULT)) if e.get("rule") == raw.CONSOLIDATE_RULE]
        assert [(e["kind"], e["source"], e["before"]) for e in moved] == [
            ("move", m["from"], m["hash"]) for m in plan["files"]], moved
        listing = (root / rep["applied"][0]["manifest"]).read_text(encoding="utf-8")
        assert all(f"`{m['from']}` → `{m['to']}`" in listing for m in plan["files"]), listing
        assert "## 결과" in listing and "→ 보류 — " in listing, listing
        assert {i["check"]: i["level"] for i in doctor.vault(VAULT, cli.published_tools)[0]["items"]}[records] == "ok"
        assert [p for p in base.rglob("*") if p.is_file()] == [shadow]
        for m in plan["files"]:
            dest = root / m["to"]
            assert dest.is_relative_to(root / "_cited/.records") and core.sha256_file(dest) == m["hash"]
        for name, text in before.items():
            assert raw.read_round(VAULT, f"00_Scope/W1/_raw/{name}.md#1")["text"] == text   # 옛 좌표
        assert target.read_bytes() == saved and D.verify_receipt(VAULT, historic)["status"] == "complete"
        assert raw.consolidate(VAULT)["count"] == 0
        shadow.unlink()
        print("PASS hidden storage, aliases, byte identity, conflict/retry, provenance, consolidation")


if __name__ == "__main__":
    main()
