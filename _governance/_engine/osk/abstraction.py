"""Opt-in v9 review inputs and persisted receipts; meaning remains the reviewer's judgment."""
from __future__ import annotations

from difflib import SequenceMatcher
from datetime import datetime
import json
from pathlib import Path
import re

from . import contract, core, evidence, graph, raw, rechecks

PROMPTS = Path(__file__).with_name("prompts")
VERSIONS = {"v9", "v10", "v11"}
DATED = {"v10", "v11"}  # v11 = v10 with judgments per sentence instead of per commit


def clean_ref(ref):
    return str(ref).removeprefix("[[").removesuffix("]]").split("|", 1)[0].strip()


def excerpt(text, diff, budget=6000, hard_limit=8000):
    """Literal hits + neighboring lines, never semantic claims about unseen context."""
    lines = text.splitlines(keepends=True)
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    noise = [m.span() for pattern in (
        r"<!--\s*osk-cited:.*?-->",
        r"<system-reminder>\s*Linked sessions now.*?</system-reminder>",
        r'<ci-monitor-event>\s*"Auto-fix pull requests" was just enabled.*?</ci-monitor-event>',
        r"<task-id>.*?</task-id>", r"<tool-use-id>.*?</tool-use-id>")
        for m in re.finditer(pattern, text, re.S)]
    # Only whole notification/metadata lines are excluded; keep user claims and command results.
    ignored = {i for i, start in enumerate(starts)
               if any(a <= start + len(lines[i]) - len(lines[i].lstrip())
                      and start + len(lines[i].rstrip()) <= b for a, b in noise)}
    eligible = set(range(len(lines))) - ignored
    changed = "\n".join(l[1:] for l in diff.splitlines()
                        if l[:1] in {"+", "-"} and not l.startswith(("+++", "---")))
    terms = set(re.findall(r"[\w./%−-]{3,}", changed.casefold()))
    scores = [sum(min(len(t), 20) for t in terms if t in l.casefold()) if i in eligible else 0
              for i, l in enumerate(lines)]
    selected = set()
    if sum(len(lines[i]) for i in eligible) <= budget:
        selected.update(eligible)
    else:
        for i in sorted(range(len(lines)), key=lambda n: (-scores[n], n)):
            if not scores[i]:
                break
            window = set(range(max(0, i - 2), min(len(lines), i + 3)))
            if lines[i].lstrip().startswith("|"):
                j = i
                while j > 0 and lines[j - 1].lstrip().startswith("|"):
                    j -= 1
                window.update(range(j, min(j + 2, len(lines))))
            candidate = selected | (window & eligible)
            size = sum(len(lines[n]) for n in candidate)
            if size <= budget or (not selected and size <= hard_limit):
                selected = candidate
    spans = []
    for i in sorted(selected):
        start, end = starts[i], starts[i] + len(lines[i])
        if spans and spans[-1][1] == start:
            spans[-1][1] = end
        else:
            spans.append([start, end])
    return {"hash": core.sha256_bytes(text.encode()), "length": len(text),
            "truncated": selected != eligible,
            "omitted_noise_chars": sum(len(lines[i]) for i in ignored),
            "selection": "matched_context" if any(scores) else "full" if selected else "no_match",
            "spans": [{"start": a, "end": b, "text": text[a:b]} for a, b in spans]}


def source(ref, idx, records, diff, *, dated=False):
    ref = clean_ref(ref)
    kind = evidence.kind(ref)
    result = {"coordinate": ref, "kind": kind}
    try:
        if kind == "round":
            m = evidence.ROUND.fullmatch(ref)
            if not m:
                raise ValueError("round coordinate required")
            path = records.file(m["record"])
            text = raw.read_exact(path)
            matches = list(raw._ROUND.finditer(text))
            blocks = {int(m[1]): text[m.start():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
                      for i, m in enumerate(matches)}
            ns = range(int(m["first"]), int(m["last"] or m["first"]) + 1)
            if dated:
                from . import transcripts
                stamps = [transcripts.normalized_timestamp((raw.cited_header(blocks[n]) or {}).get("source_timestamp")) for n in ns]
                dates = {datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(core.KST).date().isoformat()
                         for s in stamps if s}
                # A multi-day or partly undated coordinate has no single source date.
                result["source_date"] = next(iter(dates)) if all(stamps) and len(dates) == 1 else None
                result["source_timestamps"] = stamps
            text = "\n".join(blocks[n] for n in ns)
            result["coordinate"] = (records.short(evidence.record_name(path)) + "#" + ref.split("#", 1)[1])
            result["read_ref"] = core.posix_rel(path, core.ROOT) + "#" + ref.split("#", 1)[1]
        else:
            name, _, heading = ref.partition("#")
            hit = idx.locate(name)
            if not hit:
                raise ValueError("source unavailable locally; use its coordinate")
            path = hit[0]
            result["kind"] = "node" if idx.resolve(name)[0] == "node" else "path"
            data = path.read_bytes()
            if heading:
                data = rechecks.heading_range(data, heading)
                if data is None:
                    raise ValueError("heading missing or ambiguous")
            text = data.decode("utf-8")
        result.update(excerpt(text, diff), available=True)
    except (ValueError, OSError, KeyError) as exc:
        result.update(available=False, error=str(exc))
    return result


def match_holds(held, commits, current):
    """Only exact, unique anchors with a known review version establish recurrence."""
    out = []
    for h in held:
        if not isinstance(h, dict):
            continue
        part = h.get("part", "")
        for c in commits:
            before, after = c.get("before_body", ""), c.get("after_body", "")
            confirmed = bool(part and before.count(part) == 1 and h.get("reviewed_state")
                             == core.sha256_bytes(before.encode()) and h.get("reviewed_at", "") < c.get("at", ""))
            matched = None
            if confirmed:
                start, end = before.index(part), before.index(part) + len(part)
                for tag, a, b, x, y in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                    if tag != "equal" and a <= start and b >= end:
                        matched = after[x:y]
                        break
            valid = matched is not None and bool(matched) and current.count(matched) == 1
            out.append({"held_id": h.get("id"), "commit": c["commit"],
                        "current_part": matched if valid else "",
                        "match": "confirmed" if valid else "uncertain",
                        "changed_again": valid})
    return out


def prepare(job, commits, held=(), idx=None, *, version="v9"):
    if version not in VERSIONS:
        raise ValueError("unsupported abstraction version")
    if len(commits) != 1:
        raise ValueError("abstraction requires one commit per job; keep propagation diffs separate")
    idx = idx or graph.Index()
    hit = idx.locate(clean_ref(job["target"]).split("#", 1)[0])
    if not hit:
        raise ValueError("abstraction target missing")
    path = hit[0]
    node = contract.parse(path)
    text = path.read_text(encoding="utf-8")
    refs = [ref for relation, ref in node.references() if relation == "derived-from"]
    refs += evidence.coordinates(text)
    for c in commits:
        refs += c.get("rounds", [])
        refs += [m[2] for line in c.get("evidence_added", []) if (m := evidence.LINE.match(line))]
    records = evidence.Records()
    diff = "\n".join(c["diff"] for c in commits)
    line_dates = {m[2]: m[1] for line in text.splitlines() if (m := evidence.LINE.match(line))}
    cited = {}
    for ref in dict.fromkeys(refs):
        item = source(ref, idx, records, diff, dated=version in DATED)
        if item.get("available"):
            item["observed_at"] = core.now_kst()[:10]
            if version in DATED:
                # An existing line keeps its date; old records carry no source timestamp.
                old = line_dates.get(ref) or line_dates.get(item["coordinate"]) or ""
                item["date_label"] = ((old if old[:1].isdigit() else None) or item.get("source_date") or old
                                      or f"날짜 미상 (관측: {item['observed_at']})")
        cited.setdefault(item["coordinate"], item)
    job["_abstraction_snapshot"] = {"path": core.posix_rel(path, core.ROOT), "text": text,
                                    "hash": core.sha256_bytes(text.encode())}
    job["abstraction"] = {"commits": commits, "held_parts": list(held),
                          "held_matches": match_holds(held, commits, node.body), "cited": cited}
    if version in DATED:
        job["abstraction"]["version"] = version
    job["_abstraction_snapshot"]["selection_hash"] = core.sha256_bytes(
        json.dumps(job["abstraction"], ensure_ascii=False, sort_keys=True).encode())
    job["change"] = {"side": "target", "commits": [c["commit"] for c in commits]}
    return job


def prompt(planned):
    if any(planned.get(q) for q in ("candidates", "scope_jobs", "organization_jobs", "eviction_jobs")):
        raise ValueError("v9/v10 currently accept recheck-only batches")
    has = any(j.get("abstraction") for j in planned.get("recheck_jobs", []))
    version = planned.get("prompt_version", "v9")
    if version not in VERSIONS:
        raise ValueError("unsupported abstraction version")
    if has and any(j.get("abstraction", {}).get("version", "v9") != version for j in planned.get("recheck_jobs", []) if j.get("abstraction")):
        raise ValueError("abstraction input version mismatch; prepare inputs again")
    name = "issue-273-" + version + "-" + ("abstraction-recheck" if has else "recheck") + ".ko.txt"
    data = {k: v for k, v in planned.items() if not k.startswith("_") and k not in {"daily", "kind", "rid", "parents"}}
    data["recheck_jobs"] = []
    for job in planned.get("recheck_jobs", []):
        item = {k: v for k, v in job.items() if not k.startswith("_")}
        # Evidence references may carry wikilink markup or a heading; read_node takes the document name.
        item["target_name"] = clean_ref(job["target"]).split("#", 1)[0]
        if "abstraction" in item:
            item["abstraction"] = {**item["abstraction"], "commits": [
                {k: v for k, v in c.items() if k not in {"before_body", "after_body"}}
                for c in item["abstraction"]["commits"]]}
            if version in DATED:
                item["abstraction"]["cited"] = {
                    coord: {k: v for k, v in s.items() if k not in {"source_timestamps", "source_date", "observed_at"}}
                    for coord, s in item["abstraction"]["cited"].items()}
        data["recheck_jobs"].append(item)
    return (PROMPTS / name).read_text(encoding="utf-8") + "\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def validate_entries(entries, planned):
    if not isinstance(entries, list):
        raise ValueError("abstraction must be an array")
    jobs = {j["key"]: j for j in planned.get("recheck_jobs", []) if j.get("abstraction")}
    seen, judgments = {}, {}
    for e in entries:
        if not isinstance(e, dict) or not {"key", "commit", "outcome", "reason"} <= e.keys() <= {"key", "commit", "outcome", "part", "reason", "preserved_at", "judgment"}:
            raise ValueError("invalid abstraction fields")
        if any(not isinstance(v, str) or not v.strip() for k, v in e.items() if k != "preserved_at"):
            raise ValueError("abstraction fields must be nonempty strings")
        if "preserved_at" in e and (not isinstance(e["preserved_at"], list) or not e["preserved_at"]
                or any(not isinstance(v, str) or not v.strip() for v in e["preserved_at"])):
            raise ValueError("preserved_at must be a nonempty array of locations")
        if e["outcome"] == "deleted" and not e.get("preserved_at"):
            raise ValueError("삭제 보고에는 preserved_at이 필요합니다. 다른 근거 노드 이름 또는 evidence:<기존 근거 좌표>를 지목하세요. 문구 일치는 검사하지 않습니다.")
        job = jobs.get(e["key"])
        if not job or e["commit"] not in {c["commit"] for c in job["abstraction"]["commits"]}:
            raise ValueError("unselected abstraction commit")
        version = job["abstraction"].get("version")
        if version in DATED:
            judgment = e.get("judgment")
            if judgment not in {"결론·범위 변경", "신뢰도 변경", "논지 불변", "미확정"}:
                raise ValueError("judgment는 결론·범위 변경 / 신뢰도 변경 / 논지 불변 / 미확정 중 하나입니다.")
            thesis = judgment in {"결론·범위 변경", "신뢰도 변경"}
            if ((thesis and (e["outcome"] != "kept" or (version == "v10" and e.get("part"))))
                    or (judgment == "미확정" and e["outcome"] != "held")):
                raise ValueError("논지 변경은 kept, 미확정은 held로 보고하세요. 편집했다면 본문을 복원하세요.")
            if version == "v10" and judgments.setdefault((e["key"], e["commit"]), judgment) != judgment:
                raise ValueError("한 커밋의 judgment는 같아야 합니다.")
        if e["outcome"] not in {"deleted", "moved", "kept", "held"} or (e["outcome"] != "kept" and not e.get("part")):
            raise ValueError("invalid abstraction outcome or missing quoted part")
        parts = seen.setdefault((e["key"], e["commit"]), [])
        p = e.get("part", "")
        if any(not p or not old or p in old or old in p for old in parts):
            raise ValueError("overlapping abstraction reports")
        parts.append(p)
    for key, job in jobs.items():
        if any((key, c["commit"]) not in seen for c in job["abstraction"]["commits"]):
            raise ValueError("missing abstraction commit report")
    return entries


def check_preserved(job, entries, idx):
    """Check location identity and type, never paraphrase equivalence."""
    snap = job["_abstraction_snapshot"]
    cited = job["abstraction"]["cited"]
    aliases = {s["read_ref"]: c for c, s in cited.items() if s.get("read_ref")}
    existing = {aliases.get(c, c) for c in evidence.coordinates(snap["text"])}
    target = core.resolve_in_root(snap["path"])
    allowed_nodes = set()
    for ref, s in cited.items():
        if s.get("kind") == "node" and s.get("available"):
            hit = idx.locate(clean_ref(ref).split("#", 1)[0])
            if hit:
                allowed_nodes.add(hit[0])
    for e in entries:
        if e["outcome"] != "deleted":
            continue
        for ref in e["preserved_at"]:
            if ref.startswith("evidence:"):
                coord = clean_ref(ref[len("evidence:"):])
                if aliases.get(coord, coord) in existing:
                    continue
            else:
                name = clean_ref(ref).split("#", 1)[0]
                hit = idx.locate(name)
                if idx.resolve(name)[0] == "node" and hit and hit[0] != target and hit[0] in allowed_nodes:
                    continue
            raise ValueError("삭제 보존 위치가 허용된 다른 기억 노드나 기존 근거 줄이 아닙니다: " + ref
                + ". 원본 대화에만 있다면 남은 주장과의 관계를 확인해 이동하거나 본문을 복원해 보존·보류하세요. 의미상 동일 여부는 직접 판단하세요.")


def verify(manifest, entries):
    """Read-only feedback during the worker turn; final application writes receipts."""
    from . import growth
    planned = next((r for r in growth._records() if r.get("kind") == "plan" and r.get("rid") == manifest), None)
    if not planned or planned.get("prompt_version") not in VERSIONS:
        return {"ok": False, "error": "선택된 v9/v10 manifest가 없습니다."}
    try:
        validate_entries(entries, planned)
        with core.mutation_lock():
            idx = graph.Index()
            checked = {j["key"]: check_saved(j, [e for e in entries if e["key"] == j["key"]], idx)
                       for j in planned["recheck_jobs"] if j.get("abstraction")}
        return {"ok": True, "jobs": checked, "semantic_validation": "not_performed"}
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc),
                "recovery": "현재 문서와 판단을 다시 확인하고 필요한 저장·보고를 수정한 뒤 verify_abstraction을 다시 호출하세요. 이 응답은 변경을 되돌리지 않습니다."}


def _parts(text):
    body = text.encode()[contract.body_offset(text.encode()):].decode()
    span = rechecks.evidence_span(body)
    return (body[:span[0]], body[span[0]:]) if span else (body, "")


def _flat(s):
    return re.sub(r"\s+", " ", s).strip()


def check_saved(job, entries, idx):
    """Mechanical bounds/receipts, not a replacement for semantic grading."""
    snap = job["_abstraction_snapshot"]
    path = core.resolve_in_root(snap["path"])
    if path is None or not path.is_file():
        raise ValueError("target disappeared")
    before, after = snap["text"], path.read_text(encoding="utf-8")
    bb, be = _parts(before)
    ab, ae = _parts(after)
    old_meta = contract.parse_bytes(path, before.encode()).meta
    new_meta = contract.parse(path).meta
    for k in set(old_meta) | set(new_meta):
        if k not in {"updated", "summary", "hash"} and old_meta.get(k) != new_meta.get(k):
            raise ValueError("abstraction changed frontmatter")
    edits = [e for e in entries if e["outcome"] in {"deleted", "moved"}]
    if not edits and (bb.strip() != ab.strip() or be.strip() != ae.strip()
                      or old_meta.get("summary") != new_meta.get("summary")):
        raise ValueError("kept/held target was edited")
    for e in entries:
        part = e.get("part")
        if not part:
            continue
        if _flat(part) not in _flat(bb):
            raise ValueError("reported part is not a snapshot body quote")
        if e["outcome"] in {"kept", "held"} and _flat(part) not in _flat(ab):
            raise ValueError("kept/held part is missing")
        if e["outcome"] in {"deleted", "moved"} and _flat(part) in _flat(ab):
            raise ValueError("reported edit was not saved")
    allowed = {l[1:].strip() for c in job["abstraction"]["commits"] for l in c["diff"].splitlines()
               if l.startswith("+") and not l.startswith("+++")}
    old, new = bb.splitlines(), ab.splitlines()
    warnings = []
    for tag, a, b, x, y in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if [_flat(l) for l in old[a:b] if l.strip()] == [_flat(l) for l in new[x:y] if l.strip()]:
            warnings.append("formatting_only; list/code meaning requires semantic review")
            continue
        if any(l.strip() and l.strip() not in allowed for l in old[a:b]):
            raise ValueError("body edit outside selected commit")
        if tag == "insert" and any(l.strip() for l in new[x:y]) and not any(
                l.strip() in allowed for l in old[max(0, a - 1):a + 1]):
            raise ValueError("body insertion outside selected commit")
        if new[x:y]:
            warnings.append("replacement wording requires semantic review")
    aliases = {s["read_ref"]: coord for coord, s in job["abstraction"]["cited"].items() if s.get("read_ref")}
    old_ev = {aliases.get(m[2], m[2]): m for l in be.splitlines() if (m := evidence.LINE.match(l))}
    new_ev = {aliases.get(m[2], m[2]): m for l in ae.splitlines() if (m := evidence.LINE.match(l))}
    for line in ae.splitlines():
        if line.strip() and line != "## 근거" and line not in be.splitlines() and not evidence.LINE.match(line):
            raise ValueError("new evidence content is not a source line")
    if set(old_ev) - set(new_ev):
        raise ValueError("existing evidence coordinate removed")
    changed = {c: m for c, m in new_ev.items() if c not in old_ev or m[0] != old_ev[c][0]}
    allowed_coords = set(job["abstraction"]["cited"])
    records = evidence.Records()
    for coord, m in changed.items():
        if coord not in allowed_coords or evidence.check(coord, idx, records):
            raise ValueError("invalid or unselected evidence coordinate")
        if coord in old_ev and old_ev[coord][4] not in m[4]:
            warnings.append("existing evidence reworded; meaning requires review")
    if changed and not any(e["outcome"] == "moved" for e in edits):
        raise ValueError("evidence edit without a moved report")
    if any(e["outcome"] == "moved" for e in edits) and not changed:
        raise ValueError("moved report has no saved evidence change")
    check_preserved(job, entries, idx)
    for coord, m in changed.items():
        if job["abstraction"].get("version") in DATED:
            expected = job["abstraction"]["cited"][coord].get("date_label")
            if not expected or m[1] != expected:
                raise ValueError(f"출처 {coord}의 날짜는 엔진의 date_label을 사용하세요: {expected}")
        elif m[1].startswith("날짜 미상"):
            observed = job["abstraction"]["cited"][coord].get("observed_at")
            if m[1] != f"날짜 미상 (관측: {observed})":
                raise ValueError("미상 날짜의 관측일은 입력 출처의 observed_at을 사용하세요.")
    return {"after_hash": core.sha256_bytes(after.encode()), "warnings": sorted(set(warnings)),
            "semantic_validation": "not_performed"}


def apply(entries, planned):
    from . import growth
    result = {"errors": [], "jobs": {}}
    for job in planned.get("recheck_jobs", []):
        if not job.get("abstraction"):
            continue
        selected = [e for e in entries if e["key"] == job["key"]]
        try:
            with core.mutation_lock():
                validate_entries(selected, {"recheck_jobs": [job]})
                checked = check_saved(job, selected, graph.Index())
                same = [r for r in growth._records() if r.get("kind") == "abstraction_review"
                        and r.get("manifest") == planned["manifest"] and r.get("key") == job["key"]]
                record = {"kind": "abstraction_review", "manifest": planned["manifest"], "key": job["key"],
                          "before_hash": job["_abstraction_snapshot"]["hash"],
                          "selection_hash": job["_abstraction_snapshot"]["selection_hash"],
                          "entries": selected, **checked}
                if same and any(same[-1].get(k) != v for k, v in record.items()):
                    raise ValueError("abstraction receipt changed after completion")
                if not same:
                    core.ledger_append(growth.LEDGER, record)
            result["jobs"][job["key"]] = checked
        except (ValueError, OSError) as exc:
            result["errors"].append(f"Abstraction {job['key']}: {exc}")
    return result


def complete(job):
    if not job.get("abstraction"):
        return True
    from . import growth
    snap = job.get("_abstraction_snapshot", {})
    path = core.resolve_in_root(snap.get("path", ""))
    if not path or not path.is_file():
        return False
    now = core.sha256_bytes(path.read_text(encoding="utf-8").encode())
    return any(r.get("kind") == "abstraction_review" and r.get("key") == job["key"]
               and r.get("selection_hash") == snap.get("selection_hash")
               and r.get("before_hash") == snap.get("hash") and r.get("after_hash") == now
               for r in growth._records())
