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

from .core import posix_rel
from . import graph, raw, roots
from .errors import WriteError
from .vault import Vault

# 줄 — `- YYYY-MM-DD · `좌표` · 지지|조건|반례 — 내용`
LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · `([^`]+)` · (지지|조건|반례) — (\S.*)$")
URL = re.compile(r"^https?://\S+$")
# 해석은 되지만 자리가 틀린 줄 — 미해석 참조가 아니라 검증기만 보고한다
NODE_LINE = "노드를 가리키는 근거 줄"


def lines(text: str) -> list[re.Match]:
    """노드 끝 관측 절에서 형식이 맞는 줄(시행령 §1 5항). 형식이 다른 줄은 검증기가 보고한다."""
    from .rechecks import evidence_span
    span = evidence_span(text)
    if span is None:
        return []
    return [m for line in text[span[0]:].split("\n")[1:] if (m := LINE.match(line.rstrip("\r")))]


def coordinates(text: str) -> list[str]:
    """관측 절 줄의 좌표."""
    return [m[2] for m in lines(text)]


def cited_rounds(vault: Vault, text: str, records: raw.Records | None = None) -> set[str]:
    """근거 절의 줄이 가리키는 인용 라운드 — `raw.canonical_ref` 꼴. 해석되는 좌표만 담는다 —
    범위는 기록에 다 있을 때만 펴므로 펴는 수가 기록의 라운드 수를 넘지 않는다."""
    records = records or raw.Records(vault)
    out = set()
    for coord in coordinates(text):
        m = raw.ROUND.match(coord) if kind(coord) == "round" else None
        if m is None or check(vault, coord, records=records) is not None:
            continue
        rel = posix_rel(records.file(m["record"]), vault.root)
        out |= {raw.canonical_ref(vault, f"{rel}#{n}")
                for n in range(int(m["first"]), int(m["last"] or m["first"]) + 1)}
    return out


def kind(coord: str) -> str:
    """좌표의 종류 — `round`·`path`·`external`·`url`, 그 밖은 `name`."""
    if URL.match(coord):
        return "url"
    if roots.split(coord):
        return "external"
    if graph.is_record_ref(coord) or "/" not in coord and raw.ROUND.match(coord):
        return "round"
    return "path" if "/" in coord else "name"


def check(vault: Vault, coord: str, idx: "graph.Index | None" = None, records: raw.Records | None = None,
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
            p = (external or roots.Roots(vault)).file(alias, rel.strip())
            if p and heading.strip() and heading_range(p.read_bytes(), heading.strip()) is None:
                return f"없는 제목: {coord}"
        except (OSError, ValueError) as e:
            return f"{e}: {coord}"
        return None
    if k == "round":
        m = raw.ROUND.match(coord)
        if m is None:
            return f"라운드가 없는 기록 좌표: {coord}"
        records = records or raw.Records(vault)
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
    found = (idx or graph.Index(vault)).resolve(target)[0]
    if found == "node":
        return f"{NODE_LINE}: {coord} — 노드 근거는 derived-from으로 옮긴다"
    if k == "name":
        return f"알 수 없는 좌표: {coord}"
    from .core import resolve_in_root
    p = resolve_in_root(vault, target)
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
