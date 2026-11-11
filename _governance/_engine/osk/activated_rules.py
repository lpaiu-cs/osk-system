"""osk.activated_rules — 활성화된 뒤에만 판정에 드는 규칙 (시행령 §11 2항·3항).

군집 허브 노드(`cluster_overview_report`)와 노드 끝 근거 절(`evidence_errors`). 검사는 언제나 해서 보고하고,
판정(verdict) 산입은 활성화 뒤에만 한다 — 활성화 순간 무엇이 FAIL이 될지 사용자가 미리 본다. 활성 여부
(`validator_active`)와 위반이 없을 때의 자동 활성화(`auto_activate`)도 여기 있다."""
from __future__ import annotations
import hashlib, json, re
from .core import ledger_read, ledger_append, resolve_one
from . import contract, core, evidence, graph, raw, roots
from .vault import Vault


LEFTOVER = "derived-from에 비노드 대상"


def evidence_errors(vault: Vault, text: str, derived, idx: "graph.Index | None" = None, records=None,
                    external=None) -> list[str]:
    """노드 끝 관측 절의 줄마다 형식과 좌표의 해석, 그리고 `derived-from`에 남은 비노드
    대상(Mechanism §6-1 4항). `text`는 본문, `derived`는 frontmatter의 `derived-from` 값이다."""
    from .rechecks import EVIDENCE, OBSERVATION, evidence_span
    span = evidence_span(text)
    errs = []
    # 관측 절이 아닌 자리의 절 제목 — 노드 끝 절의 앞에 남은 옛 `## 근거`도 그렇다(그 줄들은 관측이
    # 아니게 된다). 본문 맨 앞의 `# 관측`은 노드 제목이다(evidence_span) — 세지 않는다.
    heads = [o for o, line, c, code, _ in contract.md_lines(text)
             if not code and c == line.rstrip("\r").expandtabs(4) and (span is None or o != span[0])
             and (re.match(r"^ {0,3}#[ \t]+" + OBSERVATION + r"[ \t#]*$", c) and text[:o].strip()
                  or re.match(r"^ {0,3}##[ \t]+" + EVIDENCE + r"[ \t#]*$", c))]
    if heads:
        errs.append("관측 절이 노드 끝에 있지 않다")
    if span is not None:
        records = records or raw.Records(vault)
        for line in text[span[0]:].split("\n")[1:]:
            line = line.rstrip("\r")
            if not line.strip():
                continue   # 빈 줄 밖의 모든 줄이 근거 줄이다 — 하위 목록·문단·다른 표지도 형식 위반이다
            m = evidence.LINE.match(line)
            if not m:
                errs.append(f"형식이 다른 관측 줄: {line[:60]}")
                continue
            problem = evidence.check(vault, m.group(2), idx, records, external)
            if problem:
                errs.append(problem)
    left = evidence.leftovers(derived, idx)
    if left:
        errs.append(f"{LEFTOVER} {len(left)}개가 남아 있다 — 관측 절로 옮긴다")
    return errs


def evidence_section_report(vault: Vault, idx: "graph.Index", external=None, records=None) -> dict:
    """노드별 근거 절 위반(Mechanism §6-1 4항). 위반이 없는 노드는 싣지 않는다. 보류된 외부
    루트는 위반이 아니므로 여기 없고 `external.held`에 모인다."""
    records = records or raw.Records(vault)
    out = {}
    for name, (path, kind) in sorted(idx.nodes.items()):
        if kind[0] == "governance":
            continue
        try:
            n = idx.node(path)
        except Exception:
            continue
        errs = evidence_errors(vault, n.body, n.meta.get("derived-from"), idx, records, external)
        if errs:
            out[name] = errs
    return out


def validator_active(vault: Vault, rule: str) -> bool:
    """검증기 규칙의 활성 여부 (Mechanism §6-1). 규칙별 인과 극대가 현재
    상태이고, 대장이 없거나 미확정(분기)이면 비활성이다 — fail-open이 아니라
    보고-전용으로 남는 쪽이 안전하다(시행령 §11 2항: 집행은 활성화 뒤에만)."""
    try:
        recs = ledger_read(vault.validators)
    except Exception:
        return False
    r = resolve_one(recs, rule, "rule")
    return bool(r and r.get("kind") == "activate")


RULES = ("cluster-overview", "evidence-section")


AUTO_REASON = "위반 0 — 자동 활성화"


AUTO_EVERY = 24 * 3600      # 근거 절 검사는 노드 2천여 개에 8초 — 하루 한 번만 잰다


def auto_activate(vault: Vault, idx=None, *, now: float | None = None) -> list[str]:
    """기록이 없는 규칙의 위반이 0이면 활성화를 적는다(시행령 §11 3항, Mechanism §6-1 2항).
    기록이 있는 규칙 — 사용자가 해제한 것도 — 은 건드리지 않는다. 잰 시각은 기기 로컬에 두고
    하루 한 번만 잰다. 켠 규칙을 낸다."""
    import time
    todo = [r for r in RULES if not any(x.get("rule") == r for x in ledger_read(vault.validators))]
    # worktree들은 git 디렉터리를 나눠 쓰지만 노드와 대장은 저마다다 — 잰 시각도 작업 트리마다 둔다
    key = hashlib.sha256(str(vault.root).encode("utf-8")).hexdigest()[:16]
    mark = core.local_lock_path(f"osk-validators-auto-{key}.json", vault.root)
    now = time.time() if now is None else now
    try:
        if not todo or now - json.loads(mark.read_text(encoding="utf-8"))["at"] < AUTO_EVERY:
            return []
    except (OSError, ValueError, KeyError, TypeError):
        pass
    mark.write_text(json.dumps({"at": now}), encoding="utf-8")   # 먼저 적어 겹친 작업이 다시 재지 않게 한다
    idx = idx or graph.Index(vault)
    clean = {"cluster-overview": lambda: not cluster_overview_errors(cluster_overview_report(vault, idx)),
             "evidence-section": lambda: not evidence_section_report(vault, idx, roots.Roots(vault), raw.Records(vault))}
    on = []
    for r in todo:
        if not clean[r]():
            continue
        try:
            # 재는 사이 생긴 기록 — 사용자의 해제도 — 은 대장 잠금 안에서 보고 물러선다
            ledger_append(vault.validators, {"kind": "activate", "rule": r, "reason": AUTO_REASON},
                          expect=lambda recs, r=r: any(x.get("rule") == r for x in recs) and "기록이 생겼다")
            on.append(r)
        except ValueError:
            pass
    return on


def cluster_overview_errors(co: dict) -> list[str]:
    """`cluster_overview_report`의 군집별 상태를 위반 문구로 — verdict와 자동 활성화가 같이 쓴다."""
    errs = []
    for c, st in sorted(co.items()):
        if not st["overview"]:
            errs.append(f"{c}: 동명 허브 노드 없음")
            continue
        if st.get("hubless"):
            errs.append(f"{c}: 허브 없는 하위 군집 {len(st['hubless'])}개 — {', '.join(st['hubless'][:3])}")
        if st["unreachable"]:
            errs.append(f"{c}: 허브 미도달 {st['unreachable']}개")
    return errs


def cluster_overview_report(vault: Vault, idx: "graph.Index") -> dict:
    """군집 허브 노드 검사 (헌법 3조 8항 · 시행령 §3 6항) — 군집별 (a) 동명
    허브 노드 존재, (b) 허브에서 **Link의 방향**(헌법 8조 4항)을 따라
    출발했을 때 전 구성 노드의 도달 가능성. 도달은 허브가 가리키는 것으로
    성립하고, 노드가 허브를 가리키는 것으로는 성립하지 않는다.

    간선은 **본문 Link의 노드 대상만**이며 **군집 안으로 한정**한다 —
    Predicate Edge는 근거·사건의 관계라 세지 않고(`_outgoing_refs` 참조),
    군집 밖 대상은 위상 규칙의 몫이며, 비노드 대상(원자료·대장)은 노드
    그래프가 아니다. Workbench 구획은 자체 계약을 따르므로 제외한다(§6-1 3항).

    묶음은 **최상위 군집**이며 하위 군집의 노드도 그 안에 든다. 구판은
    폴더별로 따로 판정해서 **층 사이의 도달을 전혀 검사하지 못했다** — 부모
    허브가 하위 허브를 안 가리켜도, 떠난 노드를 계속 가리켜도 미직결이 0으로
    나왔다(실측 재현). 헌법 3조 8항이 요구하는 것은 *"최상위 허브에서 하위
    허브를 거쳐"* 닿는 것이므로, 출발점은 하나이고 구성원은 하위를 포함한다.

    하위 허브의 식별은 **이름이 한다** — 군집 폴더와 이름이 같은 노드가 그
    군집의 허브다(`placement.is_hub`). 별도의 종이 아니라 자리이므로 노드에 표식이
    붙지 않는다(시행령 §3 7항). 허브가 없는 하위 폴더는 `hubless`로 보고한다 —
    그 안의 노드는 부모가 직접 가리키지 않는 한 미직결이 된다.

    시행령 §3 7항의 "항해의 내부 노드는 허브뿐"과 "각 노드는 하나의 갈래에
    속한다"는 여전히 검사하지 않는다(A안).
    순회는 방문 집합 위에서만 전진하므로 상호·자기 참조의 순환에서도 종료가
    보장된다."""
    clusters: dict = {}
    subdirs: dict = {}
    for stem, (p, _k) in idx.nodes.items():
        rel = p.relative_to(vault.root)
        if "Workbench" in rel.parts or rel.parts[0] == "_governance":
            continue
        if len(rel.parts) < 3:
            continue                      # Space 루트 직속은 군집이 아니다
        top = vault.root / rel.parts[0] / rel.parts[1]
        clusters.setdefault(top, set()).add(stem)
        subdirs.setdefault(top, set()).add(p.parent)
    out = {}
    for cdir, members in clusters.items():
        key = str(cdir.relative_to(vault.root)).replace("\\", "/")
        ov = cdir.name
        if ov not in members:
            out[key] = {"overview": False, "nodes": len(members),
                        "unreachable": len(members)}
            continue
        seen, queue = {ov}, [ov]
        while queue:
            for t in _outgoing_refs(idx, queue.pop(), members) - seen:
                seen.add(t)
                queue.append(t)
        unreach = sorted(members - seen)
        # 허브 없는 하위 폴더 — 그 안의 노드는 부모가 직접 가리키지 않는 한
        # 미직결이 된다. 원인을 미직결 목록이 아니라 **자리**로 보고해야
        # 고치는 쪽이 무엇을 만들지 안다.
        hubless = sorted(
            str(d.relative_to(vault.root)).replace("\\", "/")
            for d in subdirs.get(cdir, ()) if d.name not in members)
        st = {"overview": True, "nodes": len(members),
              "unreachable": len(unreach)}
        if unreach:
            st["orphans"] = unreach[:20]
        if hubless:
            st["hubless"] = hubless
        out[key] = st
    return out


def _outgoing_refs(idx: "graph.Index", stem: str, members: set) -> set:
    """`stem` 노드가 **본문 Link로** 가리키는 군집 구성원 (헌법 8조 4항).

    Predicate Edge는 세지 않는다(Mechanism §6-1 3항). 도달이 `derived-from`을
    세면 고아를 지우는 가장 싼 길이 근거를 하나 더 다는 것이 되어, 검증기의
    압력이 증거 계층으로 샌다 — 근거는 조건부인데(헌법 9조 1항) 도달은
    필수이므로, 필수를 조건부 위에 얹지 않는다.

    이름형·경로형 Link 모두 마지막 조각으로 접는다. 구성원이 아닌 대상 —
    군집 밖 노드·비노드 — 은 마지막 교집합에서 떨어진다."""
    n = idx.node(idx.nodes[stem][0])
    near = set()
    for t in set(n.wikilinks()):
        base = t.rsplit("/", 1)[-1].strip()
        near.add(base[:-3] if base.endswith(".md") else base)
    near.discard(stem)
    return near & members
