"""근거 재검토 — `derived-from` 대상의 상태가 바뀐 참조 노드를 후보로 표시하고
점검 완료를 기록한다(시행령 §7 2·3항 · Mechanism §4-1 · §8 4항).

추적하는 대상은 상태가 바뀔 수 있는 것이다 — 노드(id로 식별), 비노드 파일(vault
상대 경로), 그 안의 제목 범위(`#제목`). raw 라운드는 추가만 되는 기록이라 상태가
바뀌지 않고, 외부 URL은 상태를 잴 수 없다. 해석되지 않는 대상은 완료를 만들지
않는다 — dangling으로 따로 보고된다."""
from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path

from .core import (LEDGER, ID_RE, ROOT, causal_maxima, effective_parents, ledger_damage,
                   ledger_extend, ledger_read, posix_rel, sha256_bytes)
from . import contract, graph

RECHECKS = LEDGER / "rechecks.jsonl"
BASELINE = "기준선"
CARRIED = "이어받음"
CLOSE = ("근거와 노드를 read_node로 전문 읽는다. 노드가 맞으면 update_node(name, add_edges="
         "{\"derived-from\": target})로 그 근거를 다시 댄다(unchanged). 고쳐야 하면 그 수정이 next의 "
         "노드들까지 고치게 만들지 않을 때만 같은 호출로 고친다(updated). 그런 수정이거나 cascade가 "
         "참이면 고치지 않고 수정안을 사용자에게 올린다. 읽은 뒤 어느 쪽이 바뀌었으면 완료가 "
         "적히지 않는다(recheck_unread)")
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


def _name(ref: str) -> tuple[str, str] | None:
    """저장 표기 → (대상 이름, 제목). 추적하지 않는 raw·URL은 None."""
    s = str(ref).strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2]
    s = s.split("|", 1)[0].strip()
    if "/_raw/" in s.replace("\\", "/") or re.match(r"^https?://", s):
        return None
    name, _, heading = (x.strip() for x in s.partition("#"))
    return name, heading


def target(ref: str, idx, cache: dict | None = None) -> tuple[str, str | None] | None:
    """근거 하나의 (대상 키, 상태 해시). 추적하지 않거나 대상 파일이 해석되지 않으면
    None. 키는 노드면 id, 비노드면 vault 상대 경로이고, 제목 범위면 `#제목`이 붙는다.
    파일은 있는데 제목이 없거나 둘 이상이면 상태가 None이다 — 추적은 계속된다."""
    parsed = _name(ref)
    if parsed is None:
        return None
    name, heading = parsed
    if cache is not None and (name, heading) in cache:
        return cache[(name, heading)]
    out, hit = None, idx.locate(name) if name else None
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
            else:
                rng = heading_range(data, heading)
                out = (f"{key}#{heading}", None if rng is None else sha256_bytes(rng))
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


def _verdict(maxima: list[dict], node_state: str, target_state: str | None) -> str | None:
    """완료면 None, 후보면 그 까닭. 극대가 여럿이어도 상태가 같으면 하나로 본다
    (두 기기가 같은 점검을 따로 적은 경우)."""
    if target_state is None:
        return "근거를 해석할 수 없다"
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


def _read() -> tuple[list[dict], bool]:
    """재검토 대장과, 그 판독을 믿을 수 있는가. 판독 실패(불완전한 행)나 구조 손상이면
    완료 상태를 믿을 수 없다 — 쌍은 후보로 남고 완료는 적히지 않지만, 노드 쓰기는
    막지 않는다."""
    try:
        recs = ledger_read(RECHECKS)
    except (OSError, ValueError):
        return [], False
    return recs, not ledger_damage(recs, RECHECKS)


def candidates(idx=None) -> tuple[list[dict], bool]:
    """(재검토 후보, 기준선 대기). 읽기만 한다.

    후보마다 `next`(이 노드를 인용한 노드들 — 이 노드를 고치면 다음에 재검토될
    곳)와 `cascade`(대상의 지금 판이 재검토로 고친 판인가)를 싣는다. 재검토로 고친
    노드의 재전파가 또 수정을 부르면 그 수정은 사람이 본다(시행령 §7 2항)."""
    idx = idx or graph.Index()
    recs, ok = _read()
    damaged = not ok
    latest = {} if damaged else _latest(recs)
    revised = {r.get("node_state") for r in recs
               if r.get("kind") == "complete" and r.get("result") == "updated"}
    rows, cited, fh = list(_citing(idx, {})), {}, {}
    for name, _nid, _ns, ps, _kind in rows:
        for key in ps:
            cited.setdefault(key.split("#", 1)[0], []).append(name)

    def cascade(key: str) -> bool:
        tid = key.split("#", 1)[0]
        if tid not in fh:
            hit = idx.by_id.get(tid) if re.match(ID_RE, tid) else None
            try:
                fh[tid] = sha256_bytes(hit[0].read_bytes()) if hit else None
            except OSError:
                fh[tid] = None
        return fh[tid] is not None and fh[tid] in revised

    out = []
    for name, nid, ns, ps, kind in rows:
        for key, (ts, ref) in ps.items():
            why = ("대장을 믿을 수 없다" if damaged and ts is not None
                   else _verdict(latest.get((nid, key), []), ns, ts))
            if why:
                out.append({"node": name, "target": ref, "why": why, "id": nid, "key": key,
                            "scope": kind[1] if kind[0] == "scope" else None,
                            "node_state": ns, "target_state": ts, "cascade": cascade(key),
                            "next": sorted(set(cited.get(nid, [])))})
    _mark_escalated(out)
    return out, ok and not recs


def _mark_escalated(items: list[dict]) -> None:
    """사람에게 올린 후보에 `escalated`를 붙인다 — 그때의 두 상태가 지금과 같은 동안만."""
    try:
        from . import growth
        rows = growth._records()
    except Exception:
        return
    if not items or not any(r.get("kind") == "recheck_review" for r in rows):
        return
    par = effective_parents(rows)
    for i in items:
        top = causal_maxima(rows, f"recheck:{i['id']}:{i['key']}", par, "key")
        r = top[0] if len(top) == 1 else {}
        if (r.get("kind") == "recheck_review" and r.get("outcome") == "escalated"
                and r.get("node_state") == i["node_state"]
                and r.get("target_state") == i["target_state"]):
            i["escalated"] = {"reason": r.get("reason"), "proposal": r.get("proposal")}


def report(idx=None, limit: int = 5) -> dict:
    """표면·검증기가 싣는 요약. 후보가 없으면 빈 dict."""
    items, pending = candidates(idx)
    if not items:
        return {}
    if pending:
        return {"baseline_pending": len(items),
                "note": "재검토 기록이 없다 — 다음 쓰기나 세션 시작이 지금 근거를 기준선으로 적는다"}
    mine = [i for i in items if "escalated" not in i]
    out = {"count": len(mine), "close": CLOSE,
           "items": [{k: i[k] for k in ("node", "target", "why", "cascade", "next")} for i in mine[:limit]]}
    held = [{"node": i["node"], "target": i["target"], **i["escalated"]}
            for i in items if "escalated" in i]
    if held:
        out["escalated"] = held      # 사람 검토 대기 — 에이전트의 큐에서 빠진다
    return out


def complete_keys(idx, path: Path, meta: dict) -> set[str]:
    """이 노드의 쌍 가운데 지금 점검 완료인 대상 키."""
    ps = pairs(idx, meta)
    recs, ok = _read() if ps else ([], False)
    if not recs or not ok:
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


def _versions(rel: str, at: str) -> list[bytes]:
    """`at` 바로 앞과 바로 뒤에 그 경로를 바꾼 커밋의 판. 이력이 없으면 빈 목록."""
    out = []
    for args in (("-1", f"--before={at}"), ("--reverse", f"--after={at}")):
        try:
            r = subprocess.run(["git", "-C", str(ROOT), "log", *args, "--format=%H", "--", rel],
                               capture_output=True, timeout=20)
            sha = r.stdout.decode("ascii", "replace").split()[:1] if r.returncode == 0 else []
            if sha:
                b = subprocess.run(["git", "-C", str(ROOT), "show", f"{sha[0]}:./{rel}"],
                                   capture_output=True, timeout=20)
                if b.returncode == 0:
                    out.append(b.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return out


def _diff(old: bytes, new: bytes, limit: int = 3000) -> str | None:
    try:
        a, b = old.decode("utf-8").splitlines(), new.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    text = "\n".join(difflib.unified_diff(a, b, "checked", "now", n=2, lineterm=""))
    return text if len(text) <= limit else text[:limit] + "\n… (잘림)"


def change(nid: str, key: str, ref: str, idx) -> dict:
    """마지막 점검 뒤 무엇이 바뀌었는가 — 검토자에게 싣는 변경분. 바뀐 쪽(`side`)의
    diff를 볼트의 git 이력에서 그때의 판(상태 해시가 같은 판)을 찾아 만든다.
    찾지 못하면 전문을 읽으라는 메모만 낸다."""
    recs, ok = _read()
    maxima = _latest(recs, nid).get((nid, key), []) if ok else []
    node = idx.by_id.get(nid)
    parsed = _name(ref)
    hit = idx.locate(parsed[0]) if parsed and parsed[0] else None
    now = target(ref, idx)
    if now and now[1] is None:
        return {"note": "근거의 제목을 해석할 수 없다 — 없거나 둘 이상이다"}
    if not maxima or node is None or hit is None:
        return {"note": "점검 기록이 없다 — 근거와 노드의 전문을 읽는다"}
    m, heading = maxima[-1], parsed[1]
    if now and now[1] != m.get("target_state"):
        side, path, want = "target", hit[0], m.get("target_state")
    else:
        side, path, want, heading = "node", node[0], m.get("node_state"), ""
    cut = (lambda b: heading_range(b, heading)) if heading else (lambda b: b)
    for old in _versions(posix_rel(path, ROOT), str(m.get("at", ""))):
        old = cut(old)
        if old is not None and sha256_bytes(old) == want:
            new = cut(path.read_bytes())
            diff = None if new is None else _diff(old, new)
            if diff is not None:
                return {"side": side, "diff": diff}
    return {"side": side, "note": "점검 때의 판을 이력에서 찾지 못했다 — 전문을 읽는다"}


def ensure_baseline(idx=None) -> int:
    """기록이 하나도 없는 대장에 지금 추적되는 쌍을 `bound`로 한 번 적는다 —
    재검토 기록이 없던 vault의 근거가 한꺼번에 후보가 되지 않게 한다. 적은 수를
    돌려준다. 이미 기록이 있으면 아무것도 하지 않는다."""
    if RECHECKS.exists() and RECHECKS.stat().st_size:
        return 0
    rows = [_row(nid, ns, key, ts, "bound", BASELINE)
            for _name, nid, ns, ps, _kind in _citing(idx or graph.Index(), {})
            for key, (ts, _ref) in ps.items() if ts is not None]
    try:
        ledger_extend(RECHECKS, rows, expect=lambda recs: _ALREADY if recs else None)
    except ValueError as e:
        if str(e) != _ALREADY:
            raise
        return 0
    return len(rows)


def _presented(nid: str, key: str) -> str | None:
    """정기 실행 작업이 그 쌍을 마지막으로 보여 줄 때의 근거 상태. 없으면 None."""
    try:
        from . import growth
        rows = growth._records()
    except Exception:
        return None
    want = f"recheck:{nid}:{key}"
    for r in reversed(rows):
        for job in r.get("recheck_jobs", []) if r.get("kind") == "plan" else []:
            if job.get("key") == want:
                return job.get("target_state")
    return None


def _reviewed(idx, meta: dict, rel: str, pre: str, key: str, ts: str, ref: str,
              seen: dict | None) -> bool:
    """다시 댄 근거가 검토자가 읽은 두 상태 그대로인가. `seen`은 표면이 `read_node`로
    전문을 읽은 판(경로 → 해시)이다. 엔진 안의 호출(`seen`이 None)은 지금 상태로 본다.
    비노드 근거는 표면으로 읽지 못하므로 정기 실행 작업이 보여 준 상태와 대조한다."""
    if seen is None:
        return True
    if seen.get(rel) != pre:
        return False
    parsed = _name(ref)
    hit = idx.locate(parsed[0]) if parsed and parsed[0] else None
    if hit is None:
        return False
    if graph.is_node_home(hit[1]):
        try:
            return seen.get(posix_rel(hit[0], ROOT)) == sha256_bytes(hit[0].read_bytes())
        except OSError:
            return False
    shown = _presented(meta["id"], key)
    return shown is None or shown == ts


def after_write(idx, path: Path, meta: dict, *, before=frozenset(), prior=frozenset(),
                reasserted=frozenset(), changed: bool = True, pre: str | None = None,
                seen: dict | None = None) -> dict:
    """노드 쓰기가 성공한 뒤의 완료 기록(§4-1).

    새 배선은 `bound`, 다시 댄 근거는 본문을 함께 고쳤으면 `updated`·아니면
    `unchanged`, 쓰기 직전 완료였던 그 밖의 근거는 새 노드 상태로 이어 적는다
    (`unchanged`, 사유 `이어받음`) — 이어 적지 않으면 노드를 고칠 때마다 근거가
    전부 후보가 된다. 반려로 옛 바이트가 돌아오면 노드 상태가 어긋나 다시 후보가
    된다. 기록이 실패해도 쓰기는 성공이다 — 완료를 주장하지 않고 후보로 남는다."""
    ps = pairs(idx, meta)
    if not ps:
        return {}
    if RECHECKS.exists() and not _read()[1]:
        return {"recheck_error": "재검토 대장을 읽을 수 없거나 손상됐다 — 완료를 적지 않았다"
                                 "(쌍은 후보로 남고 검증기가 대장을 보고한다)"}
    ns = sha256_bytes(path.read_bytes())
    rel = posix_rel(path, ROOT)
    rows, closed, unread = [], [], []
    for key, (ts, ref) in ps.items():
        if ts is None:
            continue                  # 해석되지 않는 근거에는 완료가 없다
        if key not in before:
            rows.append(_row(meta["id"], ns, key, ts, "bound"))
        elif key in reasserted:
            if not _reviewed(idx, meta, rel, pre or ns, key, ts, ref, seen):
                unread.append(ref)
                continue
            closed.append(ref)
            if changed or key not in prior:
                rows.append(_row(meta["id"], ns, key, ts, "updated" if changed else "unchanged"))
        elif changed and key in prior:
            rows.append(_row(meta["id"], ns, key, ts, "unchanged", CARRIED))
    out = {"recheck_unread": {"targets": unread, "why": (
        "근거나 노드가 read_node로 읽은 판과 달라 완료를 적지 않았다 — 다시 읽고 대라")}} if unread else {}
    try:
        ledger_extend(RECHECKS, rows)
    except Exception as e:
        return {**out, "recheck_error": f"재검토 완료를 기록하지 못했다 — 후보로 남는다: {e}"}
    return {**out, "rechecked": closed} if closed else out
