"""osk.roots — 외부 루트(시행령 §2 8항 · Mechanism §1 6항).

vault 밖의 파일은 사용자가 등록한 외부 루트 안의 것만 근거로 참조한다. 선언(별칭·공유 원본의
주소·설명)은 대장 `roots.jsonl`에 적혀 모든 기기로 가고, 기기마다의 위치는 그 기기의
`.osk/roots.json`에만 있다. 등록과 해제는 사용자 CLI(`osk roots`)만 기록한다 — 외부 표면에는
이것을 쓰는 도구가 없다.

별칭 좌표(`<별칭>:<루트 기준 경로>`, Mechanism §8 9항)의 끊김은 셋으로 갈린다. 선언되지 않은
별칭과 루트 안에 없는 파일은 끊김이고, 선언된 루트가 이 기기에 없으면 끊김이 아니라 보류다.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import core
from .vault import Vault


def config_path(vault: Vault) -> Path:
    """이 기기의 위치를 적는 파일 — `.osk/roots.json`."""
    return vault.root / ".osk" / "roots.json"


NAME = re.compile(r"[a-z][a-z0-9-]+")
# URL과 원본 턴 좌표(`native:…`)의 머리 — 별칭으로 쓰면 좌표의 종류가 갈리지 않는다
RESERVED = frozenset({"http", "https", "native"})
COORD = re.compile(rf"^({NAME.pattern}):([^/].*)$")


def split(coord: str) -> tuple[str, str] | None:
    """별칭 좌표면 (별칭, 루트 기준 경로) — 아니면 None. 드라이브 문자(`C:`)와는 글자 수로,
    URL·원본 턴 좌표와는 쓰지 않는 별칭으로 갈린다."""
    m = COORD.match(coord)
    return (m[1], m[2]) if m and m[1] not in RESERVED else None


def declarations(vault: Vault) -> dict[str, dict | None]:
    """별칭 → 현재 기록, 곧 그 별칭의 인과 극대(Mechanism §3). 극대가 갈리면 None이다 — 다시
    선언하거나 해제하면 새 기록이 모든 머리를 부모로 받아 닫는다."""
    records = core.ledger_read(vault.roots)
    return {a: core.resolve_one(records, a, "alias")
            for a in {r.get("alias") for r in records} if isinstance(a, str)}


def alias_problem(alias: str, decl: dict[str, dict | None]) -> str | None:
    """선언된 별칭이면 None — 아니면 보고할 문구."""
    if alias not in decl:
        return "선언되지 않은 별칭"
    if decl[alias] is None:
        return "선언이 갈린 별칭"
    return None if decl[alias].get("kind") == "declare" else "해제된 별칭"


def locations(vault: Vault) -> dict[str, str]:
    """이 기기의 위치 — `.osk/roots.json`의 `{별칭: 절대 경로}`."""
    if not config_path(vault).is_file():
        return {}
    data = json.loads(config_path(vault).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ValueError(f"{config_path(vault)}: {{별칭: 절대 경로}} 객체가 아니다")
    return data


def location_problem(vault: Vault, where: str) -> str | None:
    """위치로 쓸 수 있으면 None — 아니면 그 사유. vault 안에 있거나 vault를 품는 위치는 받지 않는다."""
    p = Path(where)
    if not p.is_absolute():
        return f"절대 경로가 아니다: {where}"
    if not p.is_dir():
        return f"이 기기에 없는 위치: {where}"
    real, root = Path(os.path.realpath(p)), Path(os.path.realpath(vault.root))
    if real.is_relative_to(root) or root.is_relative_to(real):
        return f"vault 안에 있거나 vault를 품는 위치: {where}"
    return None


class Roots:
    """선언과 이 기기의 위치를 한 번 읽어 별칭 좌표를 해석한다(검증기 — Mechanism §6-1 4항). 이
    기기에 없는 루트는 위반이 아니라 보류이므로, `held`에 사유와 함께 모아 루트마다 한 번 알린다."""

    def __init__(self, vault: Vault):
        self.vault = vault
        self.decl, self.places = declarations(vault), locations(vault)
        self.held: dict[str, str] = {}

    def file(self, alias: str, rel: str) -> Path | None:
        """별칭과 루트 기준 경로 → 루트 안의 파일. 보류면 None이고, 끊김이면 ValueError(보고할
        문구)다. 경로는 위치 안으로 봉쇄하고, 위치 아래 경로 구성요소의 symlink는 받지 않는다
        (§1-2 5항의 경로 정체성 — `core.canon_rel`)."""
        problem = alias_problem(alias, self.decl)
        if problem:
            raise ValueError(problem)
        where = self.places.get(alias)
        why = "이 기기에 위치가 없다" if where is None else location_problem(self.vault, where)
        if why:
            self.held.setdefault(alias, why)
            return None
        canon = core.canon_rel(Path(where), rel)
        if canon is None:
            raise ValueError("루트 안으로 해석되지 않는 경로")
        p = Path(os.path.realpath(where)) / canon
        if not p.is_file():
            raise ValueError("없는 파일")
        return p


# ── 등록과 해제 — 사용자 CLI(`osk roots`)만 부른다 ─────────────────────────

def declare(vault: Vault, alias: str, note: str, origin: str | None = None) -> dict:
    """별칭 선언 — 다시 선언하면 설명·원본을 고친다. 로컬에만 있는 루트도 선언한다(시행령 §2 8항)."""
    if not NAME.fullmatch(alias) or alias in RESERVED:
        raise ValueError(f"쓸 수 없는 별칭: {alias!r} — 두 글자 이상의 [a-z][a-z0-9-]*이며 "
                         "http·https·native는 쓰지 않는다")
    with core.mutation_lock(vault):       # 대장도 작업 트리다 — 데몬의 rebase와 겹치지 않게 적는다
        return core.ledger_append(vault.roots, {"kind": "declare", "alias": alias, "note": note,
                                          **({"origin": origin} if origin else {})})


def retire(vault: Vault, alias: str, note: str = "") -> dict:
    """별칭 해제 — 그 루트를 향한 근거 줄은 끊김이 된다. 선언이 갈린 별칭도 해제로 닫는다."""
    with core.mutation_lock(vault):       # 확인과 기록 사이에 동기화가 끼어들지 않게 한 잠금 안에서
        if alias_problem(alias, declarations(vault)) in ("선언되지 않은 별칭", "해제된 별칭"):
            raise ValueError(f"선언된 별칭이 아니다: {alias}")
        return core.ledger_append(vault.roots, {"kind": "retire", "alias": alias, "note": note})


def mount(vault: Vault, alias: str, where: str) -> str:
    """이 기기에서 그 별칭의 위치를 둔다 — 선언된 별칭만 받는다."""
    where = os.path.abspath(where)
    problem = alias_problem(alias, declarations(vault)) or location_problem(vault, where)
    if problem:
        raise ValueError(f"{problem} — `{alias}`의 위치를 두지 않았다")
    with core.device_lock(vault):         # 데몬의 rebase·`.osk/` 되돌리기와 겹치지 않게 읽고 쓴다
        _save(vault, {**locations(vault), alias: where})
    return where


def unmount(vault: Vault, alias: str) -> None:
    """이 기기에서 그 별칭의 위치를 지운다."""
    with core.device_lock(vault):
        places = locations(vault)
        if places.pop(alias, None) is None:
            raise ValueError(f"이 기기에 위치가 없는 별칭: {alias}")
        _save(vault, places)


def _save(vault: Vault, places: dict[str, str]) -> None:
    core.atomic_write(config_path(vault), (json.dumps(places, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def show(vault: Vault) -> dict:
    """별칭마다 선언 상태와 설명, 그리고 이 기기에 위치가 있으면 그것을 지금 쓸 수 있는지."""
    decl, places = declarations(vault), locations(vault)
    rows = []
    for alias in sorted({*decl, *places}):
        row = {"alias": alias, "declaration": alias_problem(alias, decl) or "선언됨"}
        row.update((k, v) for k, v in (decl.get(alias) or {}).items() if k in ("origin", "note", "at"))
        if alias in places:
            row.update(location=places[alias], here=location_problem(vault, places[alias]) or "있음")
        rows.append(row)
    return {"roots": rows}
