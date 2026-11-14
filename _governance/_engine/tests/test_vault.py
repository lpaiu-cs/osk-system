"""저장소 값(`osk.vault.Vault`) — 한 프로세스에서 임시 저장소 둘을 열어 서로 섞이지 않음을 보고,
그 둘에 노드를 쓴다.

엔진의 어떤 모듈도 import할 때 저장소를 정하지 않는다. 이 시험이 여는 저장소는 이 시험이 고른 둘뿐이고,
환경 변수는 셋째 자리를 가리키게 둔다 — 거기에 무엇이든 생기면 전역이 남은 것이다.

    python tests/test_vault.py
"""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from osk.vault import Vault, default_root  # noqa: E402

PATHS = ("ledger", "signatures", "candidates", "pins", "validators", "routing", "evictions", "roots")


def _node(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "n.md").write_text("x", encoding="utf-8")


def writes() -> None:
    """한 프로세스에서 임시 저장소 둘에 노드와 인용 기록을 쓴다 — 노드·허브의 Link·색인·검색·기록 판독이 제
    저장소에만 닿고, 저장소마다 접어 두는 값이 섞이지 않는다."""
    from osk import evidence, graph, raw, search, validate, workspaces, write
    saved = os.environ.get("OSK_VAULT_ROOT")
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, tempfile.TemporaryDirectory() as c, \
            tempfile.TemporaryDirectory() as d:
        os.environ["OSK_VAULT_ROOT"] = c                      # 어느 쪽도 아닌 자리
        try:
            va, vb = Vault.open(a), Vault.open(b)
            for vault in (va, vb):
                validate.make_mini_vault(vault, vault.root)
            made = {va: write.create_node(va, "only in a", "첫 저장소의 노드", "alpha 본문", "fable-5", space=f"{va.scope}/W1"),
                    vb: write.create_node(vb, "only in b", "둘째 저장소의 노드", "beta 본문", "fable-5", space=f"{vb.scope}/W1")}
            for vault, other in ((va, vb), (vb, va)):
                out, theirs = made[vault], made[other]["name"]
                assert out["ok"] and out.get("placed") == ["W1"], out
                files = {p.stem for p in vault.root.rglob("*.md")}
                assert out["name"] in files and theirs not in files, (vault.root, sorted(files))
                hub = (vault.root / vault.scope / "W1" / "W1.md").read_text(encoding="utf-8")
                assert f"[[{out['name']}]]" in hub and f"[[{theirs}]]" not in hub, hub
                idx = graph.Index(vault)
                assert out["name"] in idx.names and theirs not in idx.names, sorted(idx.names)
                hits = {h["title"] for h in search.Searcher(vault, idx).view_search("본문", 5)}
                assert out["name"] in hits and theirs not in hits, hits
            # 같은 이름의 인용 기록이 두 저장소에 있어도 제 저장소의 것만 읽는다 — 라운드 수가 다르다.
            for vault, rounds in ((va, 2), (vb, 1)):
                pairs = [{"user": f"질문 {n}", "agent": f"답 {n}"} for n in range(1, rounds + 1)]
                assert raw.append_rounds(vault, "two-vaults", "same-record", pairs)["ok"]
            for vault, have in ((va, {1, 2}), (vb, {1})):
                records = raw.Records(vault)
                file = records.file("same-record")
                assert vault.root in file.parents and records.rounds(file) == have, (file, records.rounds(file))
            assert evidence.check(va, "same-record#2") is None
            assert evidence.check(vb, "same-record#2") == "없는 라운드: same-record#2"   # 첫 저장소에만 있다
            # 저장소마다 접어 두는 값 둘 — 한 저장소에서 정한 것이 다른 저장소에 닿지 않는다.
            # 시각의 눈금은 그 저장소가 놓인 볼륨의 것이다.
            graph._racy_margin_cache[va.root] = 123_456_789
            assert graph.racy_margin_ns(va) == 123_456_789 != graph.racy_margin_ns(vb), graph._racy_margin_cache
            # 일회성 작업 폴더의 판별은 그 저장소의 규칙으로 한다: 규칙은 첫 저장소에만 있다.
            spot = Path(d).resolve() / "run-1"
            spot.mkdir()
            workspaces.add(va, "claude", str(spot.parent), "run-[0-9]+", "1.0")
            assert workspaces.enter(va, str(spot))["one_off"] and workspaces.enter(vb, str(spot)) is None
            assert workspaces.is_one_off(va, "run-1") and not workspaces.is_one_off(vb, "run-1")
            assert not any(Path(c).iterdir()), sorted(Path(c).iterdir())
        finally:
            os.environ.pop("OSK_VAULT_ROOT", None)
            if saved is not None:
                os.environ["OSK_VAULT_ROOT"] = saved


def main() -> int:
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        new, old = Path(a).resolve(), Path(b).resolve()
        _node(old / "= Scope" / "Workbench")                  # 옛 배치의 저장소
        va, vb = Vault.open(new), Vault.open(old)

        # 폴더 이름은 저장소마다 정한다 — 빈 저장소는 새 이름, 자료가 있으면 있는 이름
        assert (va.root, va.scope, va.domain, va.person) == (new, "00_Scope", "00_Domain", "00_Person"), va
        assert (vb.root, vb.scope, vb.domain) == (old, "= Scope", "00_Domain"), vb
        assert va.space_roots == {"Scope": "00_Scope", "Domain": "00_Domain", "Person": "00_Person"}

        # 대장의 자리는 제 루트 아래에 있고, 두 저장소가 겹치지 않는다
        assert va.ledger == new / "00_Scope" / "Workbench/_ledger" and vb.ledger == old / "= Scope" / "Workbench/_ledger"
        for name in PATHS:
            pa, pb = getattr(va, name), getattr(vb, name)
            assert new in pa.parents and old in pb.parents and pa != pb, (name, pa, pb)
        assert va.candidates == va.ledger / "case" / "candidates.jsonl"

        # 값이다 — 같은 루트는 같은 값이고, 바꿀 수 없고, 키로 쓸 수 있다
        assert Vault.open(new) == va and len({va, vb, Vault.open(old)}) == 2
        try:
            va.root = old
        except dataclasses.FrozenInstanceError:
            pass
        else:
            raise AssertionError("Vault를 바꿀 수 있다")

        # 여는 순간의 폴더로 정한다 — 연 뒤에 생긴 폴더는 이미 연 값에 닿지 않는다
        _node(new / "= Scope" / "Later")
        assert va.scope == "00_Scope" and Vault.open(new).scope == "= Scope"

        # 루트를 주지 않으면 환경 변수, 그것도 없으면 이 엔진이 놓인 저장소
        saved = os.environ.pop("OSK_VAULT_ROOT", None)
        try:
            assert default_root() == Path(__file__).resolve().parents[3], default_root()
            os.environ["OSK_VAULT_ROOT"] = str(old)
            assert default_root() == old and Vault.open() == vb
        finally:
            os.environ.pop("OSK_VAULT_ROOT", None)
            if saved is not None:
                os.environ["OSK_VAULT_ROOT"] = saved

        # 같은 공간에 자료가 든 폴더가 둘이면 열지 않는다
        _node(old / "00_Scope" / "Twin")
        try:
            Vault.open(old)
        except RuntimeError:
            pass
        else:
            raise AssertionError("공간 폴더가 둘인데 열렸다")
    writes()
    print("저장소 값: 임시 저장소 둘이 섞이지 않고, 둘에 쓴 노드가 제 저장소에만 있다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
