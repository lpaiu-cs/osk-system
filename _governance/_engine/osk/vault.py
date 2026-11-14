"""osk.vault — 저장소 하나를 가리키는 값.

구현 근거: Mechanism §1(배치). 루트와, 루트에서 정해지는 것(세 공간의 폴더 이름과 대장 자리)을
여는 순간 한 번 정해 담는다. 진입점이 저장소를 열고, 안쪽 함수는 이 값을 첫 인자로 받는다
(docs/CODE-STANDARD.md 4항). 값이므로 한 프로세스에서 저장소 둘을 열 수 있다.

공간의 폴더 이름은 저장소마다 다르다 — 새 저장소는 `00_Scope`, 옛 저장소는 `= Scope`다
(`layout.space_roots`). 그래서 이름도 루트와 함께 담는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .layout import space_roots


def default_root() -> Path:
    """`OSK_VAULT_ROOT`가 있으면 그 경로, 없으면 이 엔진이 놓인 저장소."""
    env = os.environ.get("OSK_VAULT_ROOT")
    if env:
        return Path(env).resolve()
    # <vault>/_governance/_engine/osk/vault.py — 4단계 위가 vault 루트다
    return Path(__file__).resolve().parent.parent.parent.parent


@dataclass(frozen=True)
class Vault:
    root: Path
    scope: str          # 세 공간의 폴더 이름 — `00_Scope` 또는 옛 `= Scope`
    domain: str
    person: str

    @classmethod
    def open(cls, root: Path | str | None = None) -> Vault:
        """저장소를 연다 — 공간 폴더를 한 번 읽어 이름을 정한다. `root`가 없으면 `default_root()`다.
        같은 공간에 자료가 든 폴더가 둘이면 `layout.space_roots`의 RuntimeError가 그대로 나간다."""
        root = default_root() if root is None else Path(root).resolve()
        names = space_roots(root)
        return cls(root, names["Scope"], names["Domain"], names["Person"])

    @property
    def space_roots(self) -> dict[str, str]:
        return {"Scope": self.scope, "Domain": self.domain, "Person": self.person}

    # 대장의 자리 (Mechanism §3)
    @property
    def ledger(self) -> Path:
        return self.root / self.scope / "Workbench/_ledger"

    @property
    def signatures(self) -> Path:
        return self.ledger / "signatures.jsonl"

    @property
    def candidates(self) -> Path:
        return self.ledger / "case" / "candidates.jsonl"

    @property
    def pins(self) -> Path:
        return self.ledger / "pins.jsonl"

    @property
    def validators(self) -> Path:           # 검증기 활성화 기록 (Mechanism §6-1)
        return self.ledger / "validators.jsonl"

    @property
    def routing(self) -> Path:              # 세션→scope 라우팅 (Mechanism §6-2 3항)
        return self.ledger / "routing.jsonl"

    @property
    def evictions(self) -> Path:            # 퇴출 기록부 (Mechanism §9-2 12항)
        return self.ledger / "evictions.jsonl"

    @property
    def roots(self) -> Path:                # 외부 루트 선언 (Mechanism §1 6항)
        return self.ledger / "roots.jsonl"
