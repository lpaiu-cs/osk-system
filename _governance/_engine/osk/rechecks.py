"""근거 재검토 — `derived-from` 대상의 상태가 바뀐 참조 노드를 후보로 표시하고
점검 완료를 기록한다(시행령 §7 2·3항 · Mechanism §4-1 · §8 4항).

추적하는 대상은 상태가 바뀔 수 있는 것이다 — 노드(id로 식별), 비노드 파일(vault
상대 경로), 그 안의 제목 범위(`#제목`). raw 라운드는 추가만 되는 기록이라 상태가
바뀌지 않고, 외부 URL은 상태를 잴 수 없다. 해석되지 않는 대상은 완료를 만들지
않는다 — dangling으로 따로 보고된다."""
from __future__ import annotations

import re
from pathlib import Path

from .core import (LEDGER, ID_RE, ROOT, causal_maxima, effective_parents, ledger_damage,
                   ledger_extend, ledger_read, posix_rel, resolve_in_root, sha256_bytes)
from . import contract, graph

RECHECKS = LEDGER / "rechecks.jsonl"
BASELINE = "기준선"
CARRIED = "이어받음"
CLOSE = ("근거를 읽고 노드를 확인한 뒤 update_node(name, add_edges={\"derived-from\": target})로 "
         "그 근거를 다시 댄다 — 본문을 함께 고치면 updated, 그대로면 unchanged로 닫힌다")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_FRONT = re.compile(r"---\r?\n.*?\n---\r?\n", re.S)
_ALREADY = "재검토 대장에 이미 기록이 있다"


def heading_range(data: bytes, heading: str) -> bytes | None:
    """제목 범위(§8 4항) — 제목 행의 첫 바이트부터 다음 동급 이상 제목의 직전
    바이트까지. 제목이 없거나 둘 이상이면 None(해석 불능). 코드 구획의 `#` 행은
    제목이 아니다."""
    # ponytail: ATX 제목만 본다. setext(밑줄) 제목이 근거가 되면 여기에 더한다.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    m = _FRONT.match(text)
    start = m.end() if m else 0
    found = []
    for off, _line, content, code, _cont in contract.md_lines(text[start:]):
        h = None if code else _ATX.match(content)
        if h:
            title = re.sub(r"(?:^|[ \t]+)#+$", "", h.group(2) or "").strip()
            found.append((start + off, len(h.group(1)), title))
    hits = [f for f in found if f[2] == heading]
    if len(hits) != 1:
        return None
    off, level, _ = hits[0]
    end = next((o for o, lv, _ in found if o > off and lv <= level), len(text))
    return text[off:end].encode("utf-8")


def _locate(name: str, idx) -> tuple[Path, tuple] | None:
    """`graph.Index.resolve`와 같은 순서로 대상 파일 하나를 찾는다."""
    if re.match(ID_RE, name):
        return None if name in idx.dup_ids else idx.by_id.get(name)
    if "/" in name:
        p = resolve_in_root(name)
        if p is None:
            return None
        for c in (p, p.with_suffix(".md")):
            if c.is_file():
                return c, graph.space_of(c)
        return None
    live, _errors = idx.lookup_name(name)
    if len(live) == 1:
        return live[0]
    return None if live else idx.nonnode.get(name)


def target(ref: str, idx, cache: dict | None = None) -> tuple[str, str] | None:
    """근거 하나의 (대상 키, 상태 해시). 추적하지 않거나 해석되지 않으면 None.
    키는 노드면 id, 비노드면 vault 상대 경로이고, 제목 범위면 `#제목`이 붙는다."""
    s = str(ref).strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2]
    s = s.split("|", 1)[0].strip()
    if "/_raw/" in s.replace("\\", "/") or re.match(r"^https?://", s):
        return None
    name, _, heading = (x.strip() for x in s.partition("#"))
    if cache is not None and (name, heading) in cache:
        return cache[(name, heading)]
    out, hit = None, _locate(name, idx) if name else None
    if hit:
        path, kind = hit
        try:
            data = path.read_bytes()
            key = idx.node(path).id if graph.is_node_home(kind) else posix_rel(path, ROOT)
        except Exception:
            data = key = None
        if key:
            if not heading:
                out = (key, sha256_bytes(data))
            elif (rng := heading_range(data, heading)) is not None:
                out = (f"{key}#{heading}", sha256_bytes(rng))
    if cache is not None:
        cache[(name, heading)] = out
    return out


def pairs(idx, meta: dict, cache: dict | None = None) -> dict[str, tuple[str, str]]:
    """노드가 추적하는 근거 — {대상 키: (상태 해시, 저장 표기)}."""
    v = meta.get("derived-from")
    out: dict[str, tuple[str, str]] = {}
    for ref in ([] if v in (None, "", []) else v if isinstance(v, list) else [v]):
        t = target(str(ref), idx, cache)
        if t:
            out.setdefault(t[0], (t[1], str(ref)))
    return out


def _citing(idx, cache: dict):
    """근거를 추적하는 노드마다 (제목, id, 노드 상태, 쌍)."""
    for name, (path, kind) in sorted(idx.nodes.items()):
        if kind[0] == "governance":
            continue
        try:
            n = idx.node(path)
            data = path.read_bytes()
        except Exception:
            continue
        if n.id in idx.dup_ids:
            continue
        ps = pairs(idx, n.meta, cache)
        if ps:
            yield name, n.id, sha256_bytes(data), ps, kind


def _latest(records: list[dict], node: str | None = None) -> dict[tuple, list[dict]]:
    """(node, target) → 인과 극대 완료 기록들(§3 1항).

    한 기기의 append는 그때의 head 전부를 잇는다 — 그래서 대부분의 기록은 앞선
    기록 전부를 조상으로 갖고, 그런 기록이 무리의 마지막이면 그것이 유일 극대다.
    병합 직후처럼 그렇지 않은 무리만 DAG를 따라간다."""
    par = effective_parents(records)
    closed, hs = set(), set()
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        rid = r.get("rid")
        if rid not in par:
            continue
        ps = set(par[rid])
        if hs <= ps:
            closed.add(rid)
        hs = (hs - ps) | {rid}
        if r.get("kind") == "complete" and (node is None or r.get("node") == node):
            groups.setdefault((r.get("node"), r.get("target")), []).append(r)
    out = {}
    for (n, t), rs in groups.items():
        if len(rs) == 1 or rs[-1]["rid"] in closed:
            out[(n, t)] = [rs[-1]]
        else:
            out[(n, t)] = causal_maxima(
                records, n, par, "node",
                candidate=lambda r, t=t: r.get("kind") == "complete" and r.get("target") == t)
    return out


def _verdict(maxima: list[dict], node_state: str, target_state: str) -> str | None:
    """완료면 None, 후보면 그 까닭. 극대가 여럿이어도 상태가 같으면 하나로 본다
    (두 기기가 같은 점검을 따로 적은 경우)."""
    if not maxima:
        return "기록 없음"
    states = {(m.get("node_state"), m.get("target_state")) for m in maxima}
    if len(states) > 1:
        return "기록이 갈라졌다"
    ns, ts = next(iter(states))
    if ts != target_state:
        return "근거가 바뀌었다"
    if ns != node_state:
        return "노드가 바뀌었다"
    return None


def candidates(idx=None) -> tuple[list[dict], bool]:
    """(재검토 후보, 기준선 대기). 읽기만 한다."""
    idx = idx or graph.Index()
    recs = ledger_read(RECHECKS)
    damaged = bool(ledger_damage(recs, RECHECKS))
    latest = {} if damaged else _latest(recs)
    out = []
    for name, nid, ns, ps, kind in _citing(idx, {}):
        for key, (ts, ref) in ps.items():
            why = "대장 손상" if damaged else _verdict(latest.get((nid, key), []), ns, ts)
            if why:
                out.append({"node": name, "target": ref, "why": why, "id": nid,
                            "key": key, "scope": kind[1] if kind[0] == "scope" else None})
    return out, not recs


def report(idx=None, limit: int = 5) -> dict:
    """표면·검증기가 싣는 요약. 후보가 없으면 빈 dict."""
    items, pending = candidates(idx)
    if not items:
        return {}
    if pending:
        return {"baseline_pending": len(items),
                "note": "재검토 기록이 없다 — 다음 쓰기나 세션 시작이 지금 근거를 기준선으로 적는다"}
    return {"count": len(items), "close": CLOSE,
            "items": [{k: i[k] for k in ("node", "target", "why")} for i in items[:limit]]}


def complete_keys(idx, path: Path, meta: dict) -> set[str]:
    """이 노드의 쌍 가운데 지금 점검 완료인 대상 키."""
    ps = pairs(idx, meta)
    recs = ledger_read(RECHECKS) if ps else []
    if not recs or ledger_damage(recs, RECHECKS):
        return set()
    latest = _latest(recs, meta["id"])
    ns = sha256_bytes(path.read_bytes())
    return {k for k, (ts, _ref) in ps.items()
            if _verdict(latest.get((meta["id"], k), []), ns, ts) is None}


def _row(node: str, node_state: str, key: str, target_state: str, result: str,
         reason: str | None = None) -> dict:
    row = {"kind": "complete", "node": node, "node_state": node_state, "target": key,
           "target_state": target_state, "result": result}
    if reason:
        row["reason"] = reason
    return row


def ensure_baseline(idx=None) -> int:
    """기록이 하나도 없는 대장에 지금 추적되는 쌍을 `bound`로 한 번 적는다 —
    재검토 기록이 없던 vault의 근거가 한꺼번에 후보가 되지 않게 한다. 적은 수를
    돌려준다. 이미 기록이 있으면 아무것도 하지 않는다."""
    if RECHECKS.exists() and RECHECKS.stat().st_size:
        return 0
    rows = [_row(nid, ns, key, ts, "bound", BASELINE)
            for _name, nid, ns, ps, _kind in _citing(idx or graph.Index(), {})
            for key, (ts, _ref) in ps.items()]
    try:
        ledger_extend(RECHECKS, rows, expect=lambda recs: _ALREADY if recs else None)
    except ValueError as e:
        if str(e) != _ALREADY:
            raise
        return 0
    return len(rows)


def after_write(idx, path: Path, meta: dict, *, before=frozenset(), prior=frozenset(),
                reasserted=frozenset(), changed: bool = True) -> dict:
    """노드 쓰기가 성공한 뒤의 완료 기록(§4-1).

    새 배선은 `bound`, 다시 댄 근거는 본문을 함께 고쳤으면 `updated`·아니면
    `unchanged`, 쓰기 직전 완료였던 그 밖의 근거는 새 노드 상태로 이어 적는다
    (`unchanged`, 사유 `이어받음`) — 이어 적지 않으면 노드를 고칠 때마다 근거가
    전부 후보가 된다. 반려로 옛 바이트가 돌아오면 노드 상태가 어긋나 다시 후보가
    된다. 기록이 실패해도 쓰기는 성공이다 — 완료를 주장하지 않고 후보로 남는다."""
    ps = pairs(idx, meta)
    if not ps:
        return {}
    ns = sha256_bytes(path.read_bytes())
    rows, closed = [], []
    for key, (ts, ref) in ps.items():
        if key not in before:
            rows.append(_row(meta["id"], ns, key, ts, "bound"))
        elif key in reasserted:
            closed.append(ref)
            if changed or key not in prior:
                rows.append(_row(meta["id"], ns, key, ts, "updated" if changed else "unchanged"))
        elif changed and key in prior:
            rows.append(_row(meta["id"], ns, key, ts, "unchanged", CARRIED))
    try:
        ledger_extend(RECHECKS, rows)
    except Exception as e:
        return {"recheck_error": f"재검토 완료를 기록하지 못했다 — 후보로 남는다: {e}"}
    return {"rechecked": closed} if closed else {}
