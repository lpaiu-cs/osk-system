"""osk.topology — 그래프 전체의 참조를 읽는다 (헌법 4조 5항·11조 2항, Mechanism §9-4 1항).

참조 위상의 검사(`topology_check`), 노드 하나와 전체의 참조 보고(`reference_review`·`reference_report`),
중심성(`centrality`). 색인(`graph.Index`)으로 참조를 풀고, 근거 줄과 인용 기록과 재검토 기록을 함께 읽는다."""
from __future__ import annotations
import re
from .core import ID_RE
from . import contract, evidence, graph, placement, raw, rechecks
from .errors import WriteError
from .vault import Vault


W_LINK, W_DERIVED = 1.0, 3.0  # 계수는 mechanism 재량 — 초기값 (Link·derived-from)


W_OBSERVATION = 1.0           # 관측 줄 하나의 가중 w_O (Mechanism §8 6항)


def topology_check(vault: Vault, idx: graph.Index) -> list[str]:
    """참조 위상 검증 (헌법 8조 3항 + 4조 5항 + conflicts 예외). 근거 절의 좌표도 참조다
    (8조 2항) — 인용 기록은 모양으로 가리고, vault의 비노드 파일은 Link와 같은 규칙을 지난다."""
    errs = []
    cases = None
    for stem, (p, kind) in idx.nodes.items():
        n = idx.node(p)
        targets = [(t, "pe", pred) for pred in contract.PREDICATES
                   for t in n.edges(pred)]
        targets += [(t, "link", None) for t in n.wikilinks()]
        targets += [(t, "evidence", None) for t in evidence.coordinates(n.body)]
        for name, tclass, pred in targets:
            if tclass == "evidence":
                k = evidence.kind(name)
                if k == "round" and kind[0] == "domain":
                    errs.append(f"Domain의 인용 기록 직접 참조(근거 절): {stem} → {name}")
                if k != "path":
                    continue
                name = name.split("#", 1)[0].strip()
            r = idx.resolve(name)
            if pred == "conflicts":
                # 사건 표지의 예외 — 열린(docketed) 사건 또는 존치 판결 결속 노드뿐
                if cases is None:
                    cases = graph.load_cases(vault)
                if r[0] == "nonnode" and r[1][0] == "ledger":
                    # 헌법 12조 5항 — 사건을 참조하는 것은 **당사자 노드**뿐이다
                    case = cases.get(name) or {}
                    parties = [str(x) for x in (case.get("parties") or [])]
                    if str(case.get("case_no")) != name\
                            or str(case.get("status")) != "docketed":
                        errs.append(
                            f"conflicts 대상 부적격(열린 사건 아님): {stem} → {name}")
                    elif n.id not in parties:
                        errs.append(
                            f"conflicts 대상 부적격(사건의 당사자 아님): {stem} → {name}")
                    continue
                if r[0] == "node":
                    tp = idx.nodes.get(name)
                    back = idx.node(tp[0]).edges("conflicts") if tp else []
                    my_id = n.id
                    other_id = idx.node(tp[0]).id if tp else None
                    bound = any(
                        # 판결은 사용자가 내린다 — 미종결 사건에 verdict를
                        # 선기입해 결속을 얻을 수 없다(restore의 ⓑ와 대칭)
                        str(c.get("status")) == "adjudicated"
                        and str(c.get("verdict")) == "존치"
                        and my_id in [str(x) for x in (c.get("parties") or [])]
                        and other_id in [str(x) for x in (c.get("parties") or [])]
                        for c in cases.values())
                    if stem in back and bound:
                        continue  # 존치 상호 치환 + 실재 존치 사건 결속
                    errs.append(
                        f"conflicts 존치 결속 실패: {stem} → {name} "
                        f"(상호={stem in back}, 존치 사건={bound})")
                    continue
                errs.append(f"conflicts 대상 부적격: {stem} → {name} ({r[0]})")
                continue
            if pred == "derived-from" and "/" in name and r[0] == "node":
                # 노드 근거는 **제목**으로 단다(Mechanism §8 2항). 경로 표기는
                # 이동에 끊어지고(위 `/` 갈래는 그 자리에 파일이 없으면
                # dangling), `contract.target_stem`이 경로형과 이름형을 같은
                # 키로 접어 여기 밖에서는 구별되지도 않는다. 제목은 전역에서
                # 유일하므로 경로가 필요하지 않다.
                errs.append(
                    f"derived-from 노드 대상에 경로 표기는 쓰지 않는다: "
                    f"{stem} → {name} (제목을 쓰라)")
                continue
            if r[0] in ("external", "dangling"):
                continue  # 외부·미해석 — 소속 제한 없음 (dangling은 경고로 별도 보고)
            if r[0] == "ambiguous":
                errs.append(f"모호 참조(동명 노드 중복): {stem} → {name}")
                continue
            tkind = r[1]
            # 작업 상태 참조 금지는 **소속과 무관하다** — Workbench 계약 4.2의
            # 주어에 소속 제한이 없다. 구판은 이 판정이 아래 `scope` 갈래 안에
            # 있어 Domain·Person 노드의 작업 상태 참조를 보고하지 않았다
            # (쓰기 통로 `node_checks.topology_of`도 같은 자리에서 함께 고쳤다).
            if tkind[0] == "workbench":
                errs.append(f"작업 상태는 근거로 쓰지 않는다: {stem} → {name} "
                            f"(Workbench 계약 4.2)")
                continue
            if kind[0] == "domain" and tkind[0] == "raw":
                errs.append(f"Domain의 인용 기록 직접 참조: {stem} → {name}")
                continue
            if kind[0] == "scope":
                # 비노드(인용 기록·원자료·대장)에는 소속 제약이 없다(헌법 8조 3항, 2026-10-13 개정).
                # 대장은 Workbench 아래에 있어도 비노드 제도 기록이다(Workbench 계약 2.3)
                ok = (
                    (tkind[0] == "scope" and tkind[1] == kind[1])
                    or tkind[0] in ("raw", "domain", "person", "workbench-transit",
                                    "sources", "governance", "ledger")
                )
                if not ok:
                    errs.append(f"scope 간 직접 참조: [{kind[1]}] {stem} → "
                                f"{name} {tkind}")
    return errs


def dangling_refs(vault: Vault, idx: graph.Index) -> list[str]:
    """미해석 참조 목록 — 위반이 아니라 경고(탐색 링크는 자유)."""
    return reference_report(vault, idx)["dangling_refs"]


def reference_report(vault: Vault, idx: graph.Index, records=None) -> dict:
    records = records or raw.Records(vault)     # 노드마다 기록 색인을 다시 짓지 않는다
    out, items, dups = [], [], []
    for stem, (p, kind) in idx.nodes.items():
        n = idx.node(p)
        for item in reference_review(vault, n, idx, records=records):
            items.append(dict(item, name=stem))
            if item["resolution"] == "dangling":
                relation, ref = item["relation"], item["ref"]
                out.append(f"{stem} → {ref}" if relation == "Link" else
                           f"{stem} [{relation}] → {ref}")
        # 한 노드 안에서 바이트까지 같은 근거가 되풀린 엣지. 위반이 아니라 경고다 —
        # 엣지 집합은 같고, 그 노드의 다음 쓰기가 접는다(`write.update_node_locked`).
        for relation in contract.PREDICATES:
            seen = set()
            for ref in contract.stored_edges(n.meta.get(relation)):
                if ref in seen:
                    dups.append(f"{stem} [{relation}] → {ref}")
                seen.add(ref)
    return {"dangling_refs": sorted(out), "duplicate_edges": sorted(dups),
            "reference_review": items}


def reference_review(vault: Vault, node: contract.Node, idx: graph.Index,
                     previous: contract.Node | None = None, records=None) -> list[dict]:
    """One readout for writes, global checks and durable organization work.

    Resolution proves a destination category, not truth or an external URL's
    availability. Raw remains evidence in hidden non-Markdown storage. Evidence-section
    coordinates are references too (Mechanism §9-4 1): an unresolved one is reported here;
    a held external root and a line pointing at a node are the validator's to report.
    """
    import difflib
    old = set(previous.references()) if previous else set()
    out = []
    for relation, ref in node.references():
        target, sep, anchor = ref.partition("#")
        resolution = idx.resolve(target)
        kind = resolution[0]
        issue = None
        if kind in {"dangling", "ambiguous"}:
            issue = "unresolved"
        elif relation == "Link" and kind == "nonnode":
            issue = "source_navigation"
        elif relation == "derived-from" and kind == "nonnode" and resolution[1][0] == "raw":
            if not anchor:
                issue = "raw_round_required"
            elif raw.parse_ref(ref)[1] is None:
                issue = "raw_round_unresolved"
            else:
                try:
                    raw.read_round(vault, ref, max_chars=0)
                except (OSError, UnicodeError, ValueError, WriteError):
                    issue = "raw_round_unresolved"
        if issue:
            item = {"id": node.id, "relation": relation, "ref": ref,
                    "resolution": kind, "issue": issue,
                    "new": (relation, ref) not in old}
            if kind == "dangling":
                item["candidates"] = difflib.get_close_matches(target, idx.names, n=3, cutoff=0.65)
            out.append(item)
    coords = evidence.coordinates(node.body)
    if coords:
        before = set(evidence.coordinates(previous.body)) if previous else set()
        records = records or raw.Records(vault)
        for coord in coords:
            problem = evidence.check(vault, coord, idx, records)
            if problem and not problem.startswith(evidence.NODE_LINE):
                out.append({"id": node.id, "relation": "근거", "ref": coord,
                            "resolution": "unresolved", "issue": "evidence_unresolved",
                            "problem": problem, "new": coord not in before})
    return out


OBSERVATION_SIGN = {"지지": 1, "반례": -1, "조건": 0}   # 관측 줄의 설명적 중심성 산입(시행령 §7 1항)


REFERENCE_WEIGHT = {"derived-from": W_DERIVED, "Link": W_LINK}


def centrality(vault: Vault, idx: graph.Index) -> tuple[dict[str, float], dict[str, float]]:
    """중심성 C(v) = (E(v), D(v)) — (설명적, 의존적)을 stem으로 키잡은 두 사전으로 낸다(헌법 제11조
    2항, Mechanism §8 6항). R의 참조 하나가 참조하는 쪽의 E와 참조받는 쪽의 D에 같은 가중을
    더하고, 관측 절의 줄은 E에만 지지 +1·반례 −1·조건 0으로 더한다(w_O = 1). R은 참조의 유무를
    센다 — 같은 쌍의 같은 참조는 하나이고, 자기 참조·허브가 거는 참조·관측 절 안의 Link는 들지
    않는다. Workbench(헌법 4조 5항)와 통치 구획(시행령 §10 1항)은 출발·도착 모두 불산입이다."""
    EXCL = ("workbench-transit", "governance")
    score: dict[str, float] = {s: 0.0 for s in idx.nodes}
    explain: dict[str, float] = {s: 0.0 for s in idx.nodes}
    for stem, (p, kind) in idx.nodes.items():
        if kind[0] in EXCL:
            continue
        n = idx.node(p)
        explain[stem] = sum(OBSERVATION_SIGN[m[3]] for m in evidence.lines(n.body)) * W_OBSERVATION
        if placement.is_hub(p):
            continue
        span = rechecks.evidence_span(n.body)
        # 인용 기록 좌표는 비노드다(Mechanism §8 6항) — 기록 파일을 찾아 해석하지 않고
        # 건너뛴다. 그 해석이 중심성 계산 시간의 90%였다(#170, 노드 2,334개에서 6.4초).
        refs = {("derived-from", t) for t in n.edges("derived-from") if not placement.is_record_ref(t)}
        refs |= {("Link", t) for t in n.wikilinks(span[0] if span else None)}
        pairs = {(rel, _score_key(idx, t)) for rel, t in refs
                 if (r := idx.resolve(t))[0] == "node" and r[1][0] not in EXCL}
        for rel, key in pairs:
            if key != stem:
                score[key] = score.get(key, 0.0) + REFERENCE_WEIGHT[rel]
                explain[stem] += REFERENCE_WEIGHT[rel]
    return explain, score


def _score_key(idx: "graph.Index", t: str) -> str:
    """중심성 점수는 노드 **stem**으로 키잡는다(score 초기화가 stem이므로).
    대상 표기를 그 노드의 stem으로 접는다 — id형은 by_id로, 경로형·stem형은
    마지막 요소 stem으로. 경로형(`[[00_Scope/X/design]]`)을 raw 문자열로 키잡으면
    참조된 노드가 유입 중심성을 통째로 놓친다."""
    if re.match(ID_RE, t) and t in idx.by_id:
        return idx.by_id[t][0].stem
    return contract.target_stem(t)
