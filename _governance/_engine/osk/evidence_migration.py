"""osk.evidence_migration — 비노드 `derived-from`을 근거 절의 `지지` 줄로 옮긴다(v5.4 이행).

구현 근거: 헌법 제8조 5·6항·제14조 8~10항, 시행령 §1 4·5항, Mechanism §5(이행 기록)·§8 9항(줄).

엣지마다 판정하지 않는다. 입장은 모두 `지지`이고, 줄의 날짜와 보인 것은 이미 있는 것을 기계로
옮긴다 — 인용 라운드는 그 라운드가 기록에 들어온 커밋의 날과 인용 때 적은 요지(없으면 시스템 알림을
걷은 사용자 발화의 앞부분, 사용자 차례가 알림뿐이면 응답의 앞부분), 파일·URL은 그 항목을 처음 단
커밋의 날과 파일 이름·주소의 끝부분이다.
근거 줄이 이미 그 좌표를 덮으면 frontmatter 항목만 지운다. 줄을 만들 수 없는 항목은 보류로
frontmatter에 남는다.

본문은 바이트 그대로 두고 그 끝에 줄을 잇는다. 근거 절이 없던 노드도 제목 앞에 빈 줄을 넣지
않는다 — 관련 상태(Mechanism §4-1 1항, 근거 절 앞까지의 바이트)가 그대로여야 이 노드를
`derived-from`으로 드는 노드에 재검토 후보가 생기지 않는다.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import approvals, contract, core, evidence, graph, prose, raw, raw_view, rechecks, write, validate
from .core import LEDGER, ROOT, posix_rel, sha256_bytes

EVENTS = LEDGER / "migration" / "events.jsonl"
RULE = "비노드 derived-from을 근거 절의 지지 줄로 (v5.4 — 헌법 제8조 5·6항)"
PREVIEW = ROOT / ".osk" / "evidence-section-plan.md"
WIDTH = 50                                  # 보인 것 — 50자 안팎
NOT_AT_END = "관측 절이 노드 끝에 있지 않다"
from .harness.base import GOAL as _GOAL, INJECTED as _SYSTEM   # 하네스가 끼워 넣은 알림
_OBJECTIVE = re.compile(r"<objective>(.*?)</objective>", re.S)
_TAG = re.compile(r"</?[A-Za-z][\w-]*(?:\s[^<>]*)?>")


def _bare(ref) -> str:
    s = str(ref).strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2]
    return s.split("|", 1)[0].strip()


def _clip(text: str) -> str:
    text = " ".join(text.split()).replace("[[", "[").replace("]]", "]")   # 줄은 Link를 만들지 않는다
    return text if len(text) <= WIDTH else text[:WIDTH].rstrip() + "…"


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _words(text: str) -> str:
    text = _GOAL.sub(lambda m: " ".join(_OBJECTIVE.findall(m[0])), text)
    return _clip(_TAG.sub("", _SYSTEM.sub(" ", text)))


def _seen(chunk: str) -> str:
    """라운드가 보인 것 — 인용 때 적은 요지, 없으면 시스템 알림을 걷은 사용자 발화. 사용자 차례가 알림뿐인
    포착 라운드(작업 완료 알림 등)는 응답의 앞부분이다. 새로 쓰거나 요약하지 않는다."""
    try:
        cited = raw.cited_header(chunk) is not None
    except ValueError:
        cited = False
    events = list(raw_view.parse_events(chunk.partition("\n")[2]))
    agent = " ".join(raw_view.visible_text(v) for r, v in events if r in ("agent", "assistant"))
    user = " ".join(raw_view.visible_text(v) for r, v in events if r in ("user", "plain"))
    for text in ((agent, user) if cited else (user, agent)):    # 인용 라운드의 응답 자리는 요지다
        if text := _words(text):
            return text
    return ""


def _rounds_seen(path: Path, indices: set[int]) -> dict[int, tuple[str | None, str]]:
    """기록의 라운드 → (그 제목 행이 기록에 들어온 커밋의 날, 보인 것). 기록마다 한 번 읽고 git blame도
    한 번 부른다. 날짜는 커밋한 기기의 시간대로 읽는다."""
    text = raw.read_exact(path)
    spans = raw.round_spans(text)
    rows = {text.count("\n", 0, spans[i][0]) + 1: i for i in indices if i in spans}
    days, row, when = {}, None, None
    out = _git("blame", "--line-porcelain", *(f"-L{n},{n}" for n in sorted(rows)), "--",
               posix_rel(path, ROOT)) if rows else None
    for line in (out or "").splitlines():
        if m := re.match(r"([0-9a-f]{40}) \d+ (\d+)", line):
            row = int(m[2]) if m[1].strip("0") else None    # 커밋 전의 줄(영 해시)에는 날짜가 없다
        elif line.startswith("author-time "):
            when = int(line.split()[1])
        elif line.startswith("author-tz ") and row in rows and when is not None:
            tz = line.split()[1]
            shift = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * (-1 if tz[0] == "-" else 1)
            days[rows[row]] = datetime.fromtimestamp(when, timezone(shift)).strftime("%Y-%m-%d")
    return {i: (days.get(i), _seen(raw.recalled_text(text[s:e]))) for i, (s, e) in spans.items() if i in indices}


def _entry_day(node: Path, entry: str) -> str | None:
    """그 항목을 frontmatter에 처음 단 커밋의 날 — 노드가 옮겨지고 이름이 바뀌어도 따라간다. `-S`는 본문의
    같은 글자도 잡으므로, 이른 후보부터 그 판의 frontmatter에 항목이 있는지 본다."""
    out = _git("-c", "core.quotepath=off", "log", "--follow", "--name-only",
               "--format=%x1e%H %ad", "--date=short", "-S" + entry, "--", posix_rel(node, ROOT)) or ""
    for block in reversed(out.split("\x1e")[1:]):        # `--follow`는 `--reverse`와 함께면 아무것도 내지 않는다
        head, _, names = block.partition("\n")
        sha, day = head.split()
        old = (_git("show", f"{sha}:{names.strip().split(chr(10))[0]}") or "").replace("\r\n", "\n")
        if old.startswith("---\n") and entry in old.split("\n---\n", 1)[0]:
            return day
    return None


def _name(coord: str, kind: str) -> str:
    """파일은 이름(과 제목), URL은 주소의 끝부분."""
    if kind == "url":
        u = urlsplit(coord)
        return u.path.rstrip("/").rsplit("/", 1)[-1] or u.netloc
    target, _, heading = coord.partition("#")
    name = target.rstrip("/").rsplit("/", 1)[-1].split(":")[-1]
    return f"{name} — {heading.strip()}" if heading.strip() else name


def _refs(derived) -> list:
    return derived if isinstance(derived, list) else [] if derived in (None, "") else [derived]


def _items(n, idx, records) -> list[dict]:
    """노드의 비노드 `derived-from` 항목(evidence.leftovers와 같은 대상)과 그 처리."""
    coords, cited = set(evidence.coordinates(n.body)), evidence.cited_rounds(n.body, records)
    out = []
    for ref in _refs(n.meta.get("derived-from")):
        s, it = _bare(ref), {"entry": str(ref)}
        if graph.is_record_ref(s):
            rec, index = raw.parse_ref(s)
            try:
                p = raw.raw_file(rec)
            except (OSError, ValueError):
                out.append({**it, "kind": "round", "action": "hold", "reason": f"없는 기록: {rec}"})
                continue
            if index is None:
                out.append({**it, "kind": "round", "action": "hold", "reason": "라운드 번호가 없는 기록 참조"})
                continue
            it.update(kind="round", coord=evidence.coordinate(p, index, records), record=p, index=index)
            covered = raw.canonical_ref(s) in cited
        elif evidence.kind(s) in ("url", "external"):
            it.update(kind=evidence.kind(s), coord=s)
            covered = s in coords
        elif idx.resolve(s.split("#", 1)[0].strip())[0] == "nonnode":
            it.update(kind="path", coord=s)
            covered = s in coords
        else:
            continue                        # 노드와 끊긴 대상은 이 이행의 것이 아니다
        if covered:
            it["action"] = "covered"
        elif problem := evidence.check(it["coord"], idx, records):
            it.update(action="hold", reason=problem)
        out.append(it)
    return out


def _change(path: Path, n, items: list[dict]) -> dict:
    """한 노드의 새 바이트 — frontmatter만 다시 쓰고, 본문은 그대로 둔 채 끝에 줄을 잇는다."""
    data = path.read_bytes()
    out = {"path": posix_rel(path, ROOT), "items": items, "before": sha256_bytes(data), "new": None}
    extra = set(n.meta) - set(contract.ORDER) - set(contract.PREDICATES)
    if extra:
        for it in items:
            if it["action"] in ("line", "covered"):
                it.update(action="hold", reason=f"계약 밖 frontmatter 키 {sorted(extra)} — 다시 쓰면 잃는다")
    body = data[contract.body_offset(data):].decode("utf-8")
    lines = [it["line"] for it in items if it["action"] == "line"]
    if lines:
        span = rechecks.evidence_span(body)
        add = prose.space_numeric_tags("\n".join(lines)) + "\n"
        new_body = body.rstrip("\n") + "\n" + add if span else body + "# 관측\n\n" + add
        start = span[0] if span else len(body)
        if (span is None and NOT_AT_END in validate.evidence_errors(body, [])
                or not body.endswith("\n") or (rechecks.evidence_span(new_body) or (None,))[0] != start):
            for it in items:
                if it["action"] == "line":
                    it.update(action="hold", reason="근거 절을 본문을 바꾸지 않고 노드 끝에 달 수 없다")
            new_body = body
        body = new_body
    gone = {it["entry"] for it in items if it["action"] in ("line", "covered")}
    if gone:
        meta = dict(n.meta)
        keep = [r for r in _refs(meta.get("derived-from")) if str(r) not in gone]
        if keep:
            meta["derived-from"] = keep
        else:
            meta.pop("derived-from", None)
        meta["updated"] = core.now_kst()
        out["new"] = (write.frontmatter(meta) + body).encode("utf-8")
    return out


def plan() -> dict:
    """노드마다 옮길 항목과 새 바이트 — 쓰지 않는다. 통치 문서는 근거 절 검증기의 대상이 아니므로
    빠진다(validate.evidence_section_report와 같다)."""
    idx, records = graph.Index(), evidence.Records()
    found = []
    for _name_, (path, kind) in sorted(idx.nodes.items()):
        if kind[0] != "governance":
            n = idx.node(path)
            if items := _items(n, idx, records):
                found.append((path, n, items))
    want = defaultdict(set)
    for _p, _n, items in found:
        for it in items:
            if it["kind"] == "round" and "action" not in it:
                want[it["record"]].add(it["index"])
    seen = {p: _rounds_seen(p, ix) for p, ix in want.items()}
    nodes = []
    for path, n, items in found:
        for it in items:
            if "action" in it:
                continue
            if it["kind"] == "round":
                day, text = seen[it["record"]].get(it["index"], (None, ""))
            else:
                day, text = _entry_day(path, _bare(it["entry"])), _clip(_name(it["coord"], it["kind"]))
            line = f"- {day} · `{it['coord']}` · 지지 — {text}"
            if not day:
                it.update(action="hold", reason="날짜를 정할 수 없다 — git 기록에 없다")
            elif not text:
                it.update(action="hold", reason="보인 것이 없다 — 요지도 사용자 발화도 없다")
            elif not evidence.LINE.match(line):
                it.update(action="hold", reason="근거 줄의 형식을 이루지 못한다")
            else:
                it.update(action="line", line=line)
        nodes.append(_change(path, n, items))
    counts = Counter(it["action"] for node in nodes for it in node["items"])
    return {"nodes": nodes, "counts": {k: counts[k] for k in ("line", "covered", "hold")},
            "regions": sorted({r for node in nodes if node["new"] for r in approvals.containing_regions(node["path"])})}


def _manifest(p: dict, day: str) -> str:
    """착수 때의 전수 목록(Mechanism §5 3항) — 노드마다 원래 항목과 그 처리."""
    c = p["counts"]
    out = [f"# 근거 절 이행 — {day}", "",
           "비노드 `derived-from`을 노드 끝 근거 절의 `지지` 줄로 옮긴 기록이다(헌법 제8조 5·6항·제14조 8·9항,",
           f"Mechanism §5). 노드마다의 변경은 `events.jsonl`에 규칙 `{RULE}`로 남는다.", "",
           "## 규칙", "",
           "- 근거 줄이 이미 그 좌표를 덮으면 frontmatter 항목만 지운다(덮임).",
           "- 아니면 `- 날짜 · `좌표` · 지지 — 보인 것` 줄을 노드 끝 근거 절에 잇는다. 입장은 모두 `지지`이며 "
           "엣지마다 판정하지 않는다.",
           "- 날짜: 인용 라운드는 그 라운드가 기록에 들어온 커밋의 날, 파일·URL은 그 항목을 처음 단 커밋의 날.",
           "- 보인 것: 인용 때 적은 요지, 없으면 시스템 알림을 걷은 사용자 발화의 앞부분(50자), 사용자 차례가 "
           "알림뿐이면 응답의 앞부분. 파일은 이름(과 제목), URL은 주소의 끝부분. 새로 쓰거나 요약하지 않는다.",
           "- 본문은 바이트 그대로 두고 끝에 잇는다 — 근거 절이 없던 노드는 제목 앞에 빈 줄이 없다(관련 상태 보존).",
           "- 줄을 만들 수 없는 항목은 보류로 frontmatter에 남는다.", "",
           f"## 대상 — 노드 {len(p['nodes'])}개 · 줄 {c['line']} · 덮임 {c['covered']} · 보류 {c['hold']}", ""]
    for node in p["nodes"]:
        out += [f"### {node['path']}", ""]
        for it in node["items"]:
            how = (it.get("line") if it["action"] == "line" else f"덮임 `{it['coord']}`"
                   if it["action"] == "covered" else f"보류 — {it['reason']}")
            out.append(f"- `{it['entry']}` → {how}")
        out.append("")
    return "\n".join(out)


def preview() -> dict:
    """계획만 낸다 — 전수 목록은 이 기기의 `.osk/`에 쓰고, 대장과 노드는 건드리지 않는다."""
    p = plan()
    PREVIEW.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW.write_text(_manifest(p, core.now_kst()[:10]), encoding="utf-8")
    holds = [{"path": n["path"], "entry": it["entry"], "reason": it["reason"]}
             for n in p["nodes"] for it in n["items"] if it["action"] == "hold"]
    return {"ok": True, "nodes": sum(1 for n in p["nodes"] if n["new"]), **p["counts"],
            "protected_regions": p["regions"], "holds": holds, "plan": posix_rel(PREVIEW, ROOT)}


def _confirmed(planned: list[tuple[str, str]], expect) -> None:
    """잠금 안에서 다시 세운 계획의 (경로, 원본 해시)가 모두 확인받은 대상(`osk doctor --migrate`의
    `[[경로, 해시], …]`)에 있어야 쓴다 — 확인받지 않은 노드도, 확인 뒤 바뀐 노드도 아무것도 쓰기 전에 새
    확인을 받는다. 같은 실행의 앞선 이행이 쓴 노드는 doctor가 그 결과 해시를 예상값으로 넘긴다. 대상이
    줄어든 것(이미 옮겨졌거나 보류로 바뀐 것)은 받는다."""
    ok = {tuple(e) for e in expect} if expect is not None else None
    extra = sorted(p for p, h in planned if (p, h) not in ok) if ok is not None else []
    if extra:
        raise RuntimeError("확인받지 않았거나 확인 뒤 바뀐 노드가 이행 계획에 들었다 — 쓰지 않았다. "
                           "`osk doctor --migrate`로 새 확인을 받는다: " + ", ".join(extra))


def apply(expect=None) -> dict:
    """계획대로 옮긴다. 전수 목록을 먼저 적고, 노드마다 이행 기록을 남긴 뒤 쓴다(Mechanism §5 1·3항).
    끝나면 전수 목록을 결과와 보류로 닫는다. 동기화와 쓰기는 같은 잠금을 기다린다. `expect`는 확인받은
    대상이다(`_confirmed`)."""
    with core.mutation_lock(core.VAULT):
        p = plan()
        _confirmed([(n["path"], n["before"]) for n in p["nodes"] if n["new"] is not None], expect)
        day = core.now_kst()[:10]
        manifest = EVENTS.parent / f"evidence-section-{day}.md"
        k = 1
        while manifest.exists():
            k += 1
            manifest = EVENTS.parent / f"evidence-section-{day}-{k}.md"
        core.atomic_write(manifest, _manifest(p, day).encode("utf-8"))
        written = 0
        for node in p["nodes"]:
            if node["new"] is None:
                continue
            path = ROOT / node["path"]
            if sha256_bytes(path.read_bytes()) != node["before"]:
                raise RuntimeError(f"계획 뒤에 바뀐 노드 — 멈췄다: {node['path']}")
            done = Counter(it["action"] for it in node["items"])
            core.ledger_append(EVENTS, {"kind": "transform", "source": node["path"], "dest": node["path"],
                                        "before": node["before"], "after": sha256_bytes(node["new"]),
                                        "rule": RULE, "note": f"줄 {done['line']} · 덮임 {done['covered']}"})
            core.atomic_write(path, node["new"])
            written += 1
        holds = [f"- `{n['path']}` `{it['entry']}` — {it['reason']}"
                 for n in p["nodes"] for it in n["items"] if it["action"] == "hold"]
        c = p["counts"]
        close = ["## 결과", "", f"- 쓴 노드 {written}개 · 줄 {c['line']} · 덮임 {c['covered']} · 보류 {c['hold']}",
                 "- 보호영역: " + (", ".join(p["regions"]) + " — 사용자 승인(`osk approve`)이 필요하다"
                                  if p["regions"] else "없음"), "", "### 보류", "", *(holds or ["없음"]), ""]
        core.atomic_write(manifest, (_manifest(p, day) + "\n" + "\n".join(close)).encode("utf-8"))
    return {"ok": True, "nodes": written, **c, "protected_regions": p["regions"],
            "holds": len(holds), "manifest": posix_rel(manifest, ROOT),
            "after": {n["path"]: sha256_bytes(n["new"]) for n in p["nodes"] if n["new"] is not None}}


def pending() -> int:
    """옮길 노드 수 — `osk doctor`의 점검(읽기만). 보류만 남은 노드는 세지 않는다 — 세면 doctor가
    영구히 경고하고 `--migrate`마다 빈 전수 목록이 생긴다. 계획은 남은 항목에만 비싸다."""
    return sum(1 for n in plan()["nodes"] if n["new"])


# ── v5.6: 관측 절 제목 `## 근거` → `# 관측` (헌법 제8조 6항, Mechanism §4-1 1항) ──
HEADING_RULE = "관측 절 제목 `## 근거` → `# 관측` (v5.6 — 헌법 제8조 6항)"
_OLD_HEAD = re.compile(r"^ {0,3}##[ \t]+" + rechecks.EVIDENCE + r"[ \t#]*(\r?)$")


def heading_plan() -> list[dict]:
    """관측 절이 이행 전 제목 `## 근거`로 열리는 노드와 바꾼 바이트 — 쓰지 않는다. 제목 행만 바꾸고
    frontmatter와 수정시간은 그대로 둔다. 주장과 관측 줄은 한 글자도 바뀌지 않는 표기 이행이고, 관련
    상태(제목 앞까지)도 그대로라 재검토 후보가 생기지 않는다. 바꾼 뒤 같은 자리에서 관측 절로 읽히지
    않거나(본문 맨 앞의 `# 관측`은 노드 제목이다) 상태가 달라지거나, 다른 노드가 `derived-from`이나
    본문 링크로 가리킨 제목 범위가 달라지면(`#근거`가 사라지고, H1 `# 관측`이 앞 H1의 범위를 닫는다)
    보류로 둔다 — `## 근거`도 읽힌다."""
    idx = graph.Index()
    cited: dict[str, set[str]] = defaultdict(set)   # 노드 id → 제목으로 참조된 제목
    for _n, (path, kind) in idx.nodes.items():
        n = idx.node(path)
        for ref in [*_refs(n.meta.get("derived-from")), *n.wikirefs()]:
            t = rechecks.target(str(ref), idx) if "#" in str(ref) else None
            if t and "#" in t[0]:
                key, heading = t[0].split("#", 1)
                cited[key].add(heading)
    out = []
    for _n, (path, kind) in sorted(idx.nodes.items()):
        if kind[0] == "governance":
            continue
        data = path.read_bytes()
        off = contract.body_offset(data)
        try:
            body = data[off:].decode("utf-8")
        except UnicodeDecodeError:
            continue
        span = rechecks.evidence_span(body)
        end = body.find("\n", span[0]) if span else -1
        m = _OLD_HEAD.match(body[span[0]:end if end >= 0 else len(body)]) if span else None
        if m is None:
            continue
        line_end = span[0] + m.end()
        new_body = body[:span[0]] + "# " + rechecks.OBSERVATION + m.group(1) + body[line_end:]
        new = data[:off] + new_body.encode("utf-8")
        item = {"path": posix_rel(path, ROOT), "before": sha256_bytes(data), "new": new}
        moved = sorted(h for h in cited.get(idx.node(path).id, ())
                       if rechecks.heading_range(data, h) != rechecks.heading_range(new, h))
        if ((rechecks.evidence_span(new_body) or (None,))[0] != span[0]
                or rechecks.state(new) != rechecks.state(data)):
            item.update(new=None, reason="바꾸면 관측 절로 읽히지 않는다 — 본문 맨 앞의 `# 관측`은 노드 제목이다")
        elif moved:
            item.update(new=None, reason="제목으로 참조된다(" + ", ".join(f"`#{h}`" for h in moved)
                        + ") — 바꾸면 그 범위가 달라진다(인용은 재검토 후보가 되고, 링크는 가리키던 곳을 잃는다)")
        out.append(item)
    return out


def heading_apply(expect=None) -> dict:
    """제목을 바꾼다. 전수 목록을 먼저 적고, 노드마다 이행 기록을 남긴 뒤 쓴다(Mechanism §5 1·3항).
    `expect`는 확인받은 대상이다(`_confirmed`)."""
    with core.mutation_lock(core.VAULT):
        items = heading_plan()
        _confirmed([(it["path"], it["before"]) for it in items if it["new"] is not None], expect)
        day = core.now_kst()[:10]
        manifest = EVENTS.parent / f"observation-heading-{day}.md"
        k = 1
        while manifest.exists():
            k += 1
            manifest = EVENTS.parent / f"observation-heading-{day}-{k}.md"
        holds = [f"- `{it['path']}` — {it['reason']}" for it in items if it["new"] is None]
        todo = [it for it in items if it["new"] is not None]
        head = [f"# 관측 절 제목 이행 — {day}", "",
                f"노드 끝 관측 절의 제목 `## 근거`를 `# 관측`으로 바꾼다. 규칙 `{HEADING_RULE}`. 제목 행만 바꾸며",
                "frontmatter·수정시간·관련 상태는 그대로다.", "",
                f"## 대상 — 노드 {len(todo)}개 · 보류 {len(holds)}", "", *(f"- `{it['path']}` → 바꾼다" for it in todo),
                *(h.replace("` — ", "` → 보류 — ", 1) for h in holds), ""]
        core.atomic_write(manifest, "\n".join(head).encode("utf-8"))
        regions = sorted({r for it in todo for r in approvals.containing_regions(it["path"])})
        for it in todo:
            path = ROOT / it["path"]
            if sha256_bytes(path.read_bytes()) != it["before"]:
                raise RuntimeError(f"계획 뒤에 바뀐 노드 — 멈췄다: {it['path']}")
            core.ledger_append(EVENTS, {"kind": "transform", "source": it["path"], "dest": it["path"],
                                        "before": it["before"], "after": sha256_bytes(it["new"]),
                                        "rule": HEADING_RULE})
            core.atomic_write(path, it["new"])
        close = ["## 결과", "", f"- 쓴 노드 {len(todo)}개 · 보류 {len(holds)}",
                 "- 보호영역: " + (", ".join(regions) + " — 사용자 승인(`osk approve`)이 필요하다"
                                  if regions else "없음"), "", "### 보류", "", *(holds or ["없음"]), ""]
        core.atomic_write(manifest, ("\n".join(head) + "\n" + "\n".join(close)).encode("utf-8"))
    return {"ok": True, "nodes": len(todo), "holds": len(holds), "protected_regions": regions,
            "manifest": posix_rel(manifest, ROOT), "after": {it["path"]: sha256_bytes(it["new"]) for it in todo}}
