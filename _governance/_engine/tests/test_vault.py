"""저장소 값(`osk.vault.Vault`) — 한 프로세스에서 임시 저장소 둘을 열어 서로 섞이지 않음을 본다.

`osk.vault`만 import한다. 저장소를 import 시각에 고정하는 모듈(`osk.core`)을 거치지 않으므로,
이 시험이 여는 저장소는 이 시험이 고른 둘뿐이다.

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
    print("저장소 값: 임시 저장소 둘이 섞이지 않는다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
