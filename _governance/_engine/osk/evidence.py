"""osk.evidence — 근거 절의 줄과 좌표.

구현 근거: 헌법 제8조 6항(근거 절), 시행령 §1 5항(줄마다 날짜·좌표·지지|조건|반례·보인 것),
Mechanism §8 9항(줄과 좌표의 형식)·§6-1 4항(검증기)·§9 8항(회상은 좌표로).

좌표는 대상에 따라 넷이다 — 인용 기록의 라운드, vault의 비노드 파일, 외부 루트의 파일, URL.
종류는 모양만으로 갈린다. 라운드는 기록 이름 해시의 앞자리로 줄여 쓰므로(`c38d8ef2#46`)
그 앞자리를 기록으로 되돌리는 색인이 여기 있다. 대응표는 두지 않는다 — 기록 이름의 해시가
곧 정체이고, 자리·접두·확장자는 파일 목록에서 다시 계산된다.
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import ROOT
from . import graph, raw, roots, write

# 줄 — `- YYYY-MM-DD · `좌표` · 지지|조건|반례 — 내용`
LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · `([^`]+)` · (지지|조건|반례) — (\S.*)$")
# 라운드 좌표 — `<기록>#N`, 이어진 라운드는 `<기록>#N–M`
ROUND = re.compile(r"^(?P<record>[^#\s][^#]*)#(?P<first>\d+)(?:[–-](?P<last>\d+))?$")
# 해시 이름의 기록 — `<하네스>-<16진 32자>`
HASHED = re.compile(r"^(?P<harness>.+)-(?P<hex>[0-9a-f]{32})$")
PREFIX = re.compile(r"^[0-9a-f]{8,32}$")
URL = re.compile(r"^https?://\S+$")


class Records:
    """인용 기록의 이름 색인. 한 번 훑어 여러 좌표를 해석한다 — 검증기는 근거 줄마다
    파일 목록을 다시 읽지 않는다."""

    def __init__(self):
        self.names: set[str] = set()
        for d in (ROOT / raw.CITED, *raw._legacy_dirs()):
            if d.is_dir():
                for f in (*d.glob("*.md"), *(d / ".records").glob("*.txt"),
                          *(d / ".records").glob("*/record.txt")):
                    self.names.add(f.parent.name if f.name == "record.txt" else f.stem)
        self.hexes: dict[str, list[str]] = {}
        for name in self.names:
            m = HASHED.match(name)
            if m:
                self.hexes.setdefault(m["hex"], []).append(name)
        self._files: dict[str, Path] = {}
        self._rounds: dict[Path, set[int]] = {}

    def find(self, record: str) -> list[str]:
        """좌표의 기록 부분 → 그에 맞는 기록 이름들. 줄인 해시는 앞자리가 같은 해시 이름의
        기록 모두이고, 그 밖은 같은 이름 하나다."""
        if record in self.names:
            return [record]
        if PREFIX.match(record):
            return sorted(n for h, names in self.hexes.items() if h.startswith(record) for n in names)
        return []

    def short(self, record: str) -> str:
        """근거 줄에 쓸 기록 표기 — 해시 이름은 다른 기록과 갈리는 가장 짧은 앞자리(8자
        이상)이고, 해시 이름이 아닌 옛 기록은 이름 그대로다."""
        m = HASHED.match(record)
        if not m:
            return record
        mine = m["hex"]
        others = [h for h in self.hexes if h != mine]
        n = 8
        while n < 32 and any(h.startswith(mine[:n]) for h in others):
            n += 1
        return mine[:n]

    def file(self, record: str) -> Path:
        """좌표의 기록 부분 → 기록 파일. 줄인 해시·옛 이름·전체 경로를 받는다."""
        if record not in self._files:
            if "/" in record:
                self._files[record] = raw._raw_file(record)
            else:
                names = self.find(record)
                if len(names) != 1:
                    raise write.WriteError("없는 기록" if not names else "모호한 좌표",
                                           [f"`{record}`에 맞는 기록 {len(names)}개"
                                            + (f": {names[:3]}" if names else "")])
                found = raw.locate_record(names[0])
                if found is None:
                    raise write.WriteError("없는 기록", [f"`{record}`"])
                self._files[record] = found
        return self._files[record]

    def rounds(self, path: Path) -> set[int]:
        """기록에 실재하는 라운드 — 한 기록을 인용한 근거 줄이 많아도 한 번만 읽는다."""
        if path not in self._rounds:
            self._rounds[path] = set(raw.rounds(raw.read_exact(path)))
        return self._rounds[path]


def record_name(path: Path | str) -> str:
    """기록 파일 → 기록 이름(긴 이름은 디렉터리가 이름이다)."""
    p = Path(path)
    return p.parent.name if p.name == "record.txt" else p.stem


def coordinate(path: Path | str, index: int, records: Records | None = None) -> str:
    """기록 파일과 라운드 → 근거 줄 좌표(Mechanism §8 9항)."""
    return f"{(records or Records()).short(record_name(path))}#{index}"


def kind(coord: str) -> str:
    """좌표의 종류 — `round`·`path`·`external`·`url`, 그 밖은 `name`."""
    if URL.match(coord):
        return "url"
    if roots.split(coord):
        return "external"
    if graph.is_record_ref(coord) or "/" not in coord and ROUND.match(coord):
        return "round"
    return "path" if "/" in coord else "name"


def check(coord: str, idx: "graph.Index | None" = None, records: Records | None = None,
          external: "roots.Roots | None" = None) -> str | None:
    """좌표 하나를 해석한다(Mechanism §6-1 4항 (b)). 문제가 없으면 None, 있으면 보고할 문구.
    이 기기에 없는 외부 루트는 문제가 아니라 보류다 — `external.held`가 루트마다 모은다."""
    from .rechecks import heading_range
    k = kind(coord)
    if k == "url":
        return None
    if k == "external":
        alias, rest = roots.split(coord)
        rel, _, heading = rest.partition("#")
        try:
            p = (external or roots.Roots()).file(alias, rel.strip())
            if p and heading.strip() and heading_range(p.read_bytes(), heading.strip()) is None:
                return f"없는 제목: {coord}"
        except (OSError, ValueError) as e:
            return f"{e}: {coord}"
        return None
    if k == "round":
        m = ROUND.match(coord)
        if m is None:
            return f"라운드가 없는 기록 좌표: {coord}"
        records = records or Records()
        try:
            p = records.file(m["record"])
            have = records.rounds(p)
        except (OSError, ValueError) as e:     # WriteError는 ValueError다
            return (f"모호한 좌표: {coord} — 기록 여럿에 걸린다" if e.args[:1] == ("모호한 좌표",)
                    else f"없는 기록: {coord}")
        first, last = int(m["first"]), int(m["last"] or m["first"])
        if last < first or any(n not in have for n in range(first, last + 1)):
            return f"없는 라운드: {coord}"
        return None
    target = coord.split("#", 1)[0].strip()
    found = (idx or graph.Index()).resolve(target)[0]
    if found == "node":
        return f"노드를 가리키는 근거 줄: {coord} — 노드 근거는 derived-from으로 옮긴다"
    if k == "name":
        return f"알 수 없는 좌표: {coord}"
    from .core import resolve_in_root
    p = resolve_in_root(target)
    if found != "nonnode" or p is None or not p.is_file():     # 색인은 디렉터리도 비노드로 본다
        return f"없는 파일: {coord}"
    heading = coord.partition("#")[2].strip()
    if heading and heading_range(p.read_bytes(), heading) is None:
        return f"없는 제목: {coord}"
    return None


def leftovers(derived, idx: "graph.Index | None" = None) -> list[str]:
    """`derived-from`에 남은 비노드 대상(Mechanism §8 2항) — 인용 기록·URL·외부 루트의 파일,
    그리고 색인이 비노드로 해석하는 경로."""
    refs = derived if isinstance(derived, list) else [] if derived in (None, "") else [derived]
    out = []
    for ref in refs:
        s = str(ref).strip()
        if s.startswith("[[") and s.endswith("]]"):
            s = s[2:-2]
        s = s.split("|", 1)[0].strip()
        name = s.split("#", 1)[0].strip()
        if graph.is_record_ref(s) or kind(s) in ("url", "external"):
            out.append(str(ref))
        # 노드 제목은 열어 보지 않는다 — 경로이거나 비노드 이름일 때만 해석한다
        elif idx is not None and ("/" in name or name in idx.nonnode) and idx.resolve(name)[0] == "nonnode":
            out.append(str(ref))
    return out
