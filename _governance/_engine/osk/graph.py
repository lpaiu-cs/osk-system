"""osk.graph — 공간 배치·참조 위상·중심성.

구현 근거: Mechanism §1(선언표·`_` 규칙), 헌법 8조(참조 위상·conflicts 예외,
Domain의 _raw 직접 참조 금지), 헌법 4조 5항(Workbench 예외),
헌법 11조 2항 + 시행령 §7 1항(중심성 산입: 노드 향 Link·derived-from,
conflicts·비노드 대상·Workbench 비산입).
"""
from __future__ import annotations
import re
from pathlib import Path

from .core import ROOT, LEDGER, ID_RE, resolve_in_root
from . import contract


def _load_cases() -> dict[str, dict]:
    """사건부 헤더 일괄 로드 — conflicts 적격 판정용."""
    from . import signatures as _sig
    out = {}
    cdir = LEDGER / "case"
    if cdir.exists():
        for f in sorted(cdir.glob("CASE-*.md")):
            c = _sig.parse_case(f)
            if c:
                out[f.stem] = c
    return out

NODE_SPACES = ("= Domain", "= Person", "= Scope")
W_LINK, W_DERIVED = 1.0, 3.0  # 계수는 mechanism 재량 — 초기값 (Link·derived-from)


def space_of(path: Path) -> tuple:
    """경로 → 소속. ('domain', d) ('person', f) ('scope', s) ('workbench',)
    ('workbench-transit',) ('ledger',) ('raw', s) ('archive',) ('sources',)
    ('engine',) ('support',). vault 밖 경로는 소속이 없다 — ('support',)로
    보고 죽지 않는다(시행령 §11 — 어떤 입력에도 보류·보고)."""
    try:
        rel = path.resolve().relative_to(ROOT)
    except (ValueError, OSError):
        return ("support",)
    parts = rel.parts
    if not parts:
        return ("support",)
    head = parts[0]
    if head == "= Domain":
        return ("domain", parts[1] if len(parts) > 2 else None)
    if head == "= Person":
        return ("person", parts[1] if len(parts) > 2 else None)
    if head == "_governance":
        # 통치 구획 — Space 밖의 상설 구획. 통치 문서·사료는 특수한 노드다
        # (헌법 3조 6항 — 검색·중심성 불산입, 표면 쓰기 거부, 명시 조회
        # 도달). `_engine/`은 그 하위의 엔진 구획이다.
        if "_engine" in parts:
            return ("engine",)
        return ("governance",)
    if head == "= Scope":
        s = parts[1] if len(parts) > 1 else None
        if s == "Workbench":
            if "_ledger" in parts:
                return ("ledger",)
            if "_raw" in parts:
                return ("raw", "Workbench")
            if "transit" in parts:
                return ("workbench-transit",)
            return ("workbench",)
        if "_raw" in parts:
            return ("raw", s)
        return ("scope", s)
    if head == "_sources":
        return ("sources",)
    if head == "_engine":
        return ("engine",)
    return ("support",)


def is_node_home(kind: tuple) -> bool:
    """노드 군집은 선언표의 `= ` Space 경로·transit과, 유일한 밑줄 예외인
    통치 구획뿐 (Mechanism §1 4항)."""
    return kind[0] in ("domain", "person", "scope", "workbench-transit",
                       "governance")


def iter_nodes():
    # 통치 구획의 통치 문서·사료는 특수한 노드다(시행령 §10 1항) — 색인에
    # 있어야 명시 조회(read_node)가 도달하고 갱신 후 승인(수용 기록)이
    # 성립한다. `_engine`의 .md는 space_of가 ("engine",)으로 걸러낸다.
    for base in NODE_SPACES + ("_governance",):
        root = ROOT / base
        if not root.exists():
            continue
        for p in sorted(root.rglob("*.md")):
            k = space_of(p)
            if is_node_home(k):
                yield p, k


def _vault_md():
    """vault의 md 전수. 점(.)으로 시작하는 도구 구획(.git·.venv·.obsidian 등)은
    저장소의 살림살이지 vault의 배치가 아니다 — 순회에서 뺀다."""
    if not ROOT.exists():
        return
    for top in sorted(ROOT.iterdir()):
        if top.name.startswith(".") or top.name == "__pycache__":
            continue
        if top.is_dir():
            yield from sorted(top.rglob("*.md"))
        elif top.suffix == ".md":
            yield top


def layout_violations() -> list[str]:
    """노드형(frontmatter 보유) 파일은 선언표의 노드 군집에만 둘 수 있다
    (Mechanism §1 4항 — 통치 구획을 제외한 `_` 구획·Workbench 루트·비선언
    루트 디렉토리는 노드를 두지 않는다)."""
    errs = []
    for p in _vault_md():
        k = space_of(p)
        if is_node_home(k):
            continue
        if p.read_text(encoding="utf-8", errors="ignore").startswith("---\n"):
            errs.append(f"비노드 구획에 노드형 파일: {p.relative_to(ROOT)} ({k[0]})")
    return errs


class Index:
    """파일명(stem) → (경로, 소속). 참조 해석과 중심성의 기반."""

    def __init__(self):
        self.nodes: dict[str, tuple[Path, tuple]] = {}
        self.by_id: dict[str, tuple[Path, tuple]] = {}   # id → 노드 (derived-from 해석)
        self.parsed: dict[Path, contract.Node] = {}
        self.dup_stems: dict[str, list[str]] = {}
        self.broken: dict[str, str] = {}
        for p, k in iter_nodes():
            # 파싱 불가 파일(임시 메모 등)은 노드에서 분리해 소비자가 건너뛰게
            # 하고 별도로 보고한다 — 하나가 검증기·검색 전체를 죽이지 않는다
            # (시행령 §11 — 실패는 보류·보고).
            try:
                self.parsed[p] = contract.parse(p)
            except Exception as e:
                msg = f"{p.relative_to(ROOT)}: {e}"
                self.broken[p.stem] = (f"{self.broken[p.stem]}; {msg}"
                                       if p.stem in self.broken else msg)
                continue
            if p.stem in self.nodes:
                self.dup_stems.setdefault(p.stem, [
                    str(self.nodes[p.stem][0].relative_to(ROOT))]).append(
                    str(p.relative_to(ROOT)))
            self.nodes[p.stem] = (p, k)
            nid = self.parsed[p].id
            if nid:
                self.by_id[nid] = (p, k)
        # 비노드 파일(원자료·대장·raw)도 대상 해석용으로 등재
        self.nonnode: dict[str, tuple] = {}
        for base in ("_sources", "= Scope", "= Person"):
            root = ROOT / base
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.stem not in self.nodes:
                    k = space_of(p)
                    if k[0] in ("raw", "sources", "ledger"):
                        self.nonnode[p.stem] = (p, k)

    def node(self, path: Path) -> contract.Node:
        if path not in self.parsed:
            self.parsed[path] = contract.parse(path)
        return self.parsed[path]

    def resolve(self, name: str):
        """대상명 → ('node',소속) | ('nonnode',소속) | ('ambiguous',) |
        ('dangling',) | ('external',). 경로형([[= Scope/B/b]])은 경로로 우선
        해석한다 — 파일명 우회를 막는다. 다만 URL은 `/`가 있어도 경로가
        아니며, 경로 해석은 vault 안으로 봉쇄한다([[/etc/passwd]])."""
        if re.match(r"^https?://", name):
            return ("external",)
        if re.match(ID_RE, name):
            # derived-from 노드 대상 — id가 정본 동일성(경로·이름은 상태)
            hit = self.by_id.get(name)
            return ("node", hit[1]) if hit else ("dangling",)
        if "/" in name:
            p = resolve_in_root(name)
            if p is None:
                return ("dangling",)      # vault 밖 — 참조 대상이 아니다
            for cand in (p, p.with_suffix(".md")):
                if cand.exists():
                    k = space_of(cand)
                    return (("node", k) if is_node_home(k) else ("nonnode", k))
            return ("dangling",)
        if name in self.dup_stems:
            return ("ambiguous",)
        if name in self.nodes:
            return ("node", self.nodes[name][1])
        if name in self.nonnode:
            return ("nonnode", self.nonnode[name][1])
        return ("dangling",)


def topology_check(idx: Index) -> list[str]:
    """참조 위상 검증 (헌법 8조 3항 + 4조 5항 + conflicts 예외)."""
    errs = []
    cases = None
    for stem, (p, kind) in idx.nodes.items():
        n = idx.node(p)
        targets = [(t, "pe", pred) for pred in contract.PREDICATES
                   for t in n.edges(pred)]
        targets += [(t, "link", None) for t in n.wikilinks()]
        for name, tclass, pred in targets:
            r = idx.resolve(name)
            if pred == "conflicts":
                # 사건 표지의 예외 — 열린(docketed) 사건 또는 존치 판결 결속 노드뿐
                if cases is None:
                    cases = _load_cases()
                if r[0] == "nonnode" and r[1][0] == "ledger":
                    # 헌법 12조 5항 — 사건을 참조하는 것은 **당사자 노드**뿐이다
                    case = cases.get(name) or {}
                    parties = [str(x) for x in (case.get("parties") or [])]
                    if str(case.get("case_no")) != name \
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
            if pred == "derived-from" and not re.match(ID_RE, name) \
                    and r[0] in ("node", "ambiguous"):
                # 노드 근거의 동일성은 id다 — 경로·이름은 상태이므로 이동·개명에
                # 끊어지고, 같은 이름이 재사용되면 다른 노드를 가리킨다
                # (Mechanism §8 2항 · 헌법 8조 5항).
                errs.append(f"derived-from 노드 대상은 id로 단다: {stem} → {name}")
                continue
            if r[0] in ("external", "dangling"):
                continue  # 외부·미해석 — 소속 제한 없음 (dangling은 경고로 별도 보고)
            if r[0] == "ambiguous":
                errs.append(f"모호 참조(동명 노드 중복): {stem} → {name}")
                continue
            tkind = r[1]
            if kind[0] == "domain" and tkind[0] == "raw":
                errs.append(f"Domain의 _raw 직접 참조: {stem} → {name}")
                continue
            if kind[0] == "scope":
                ok = (
                    (tkind[0] == "scope" and tkind[1] == kind[1])
                    or (tkind[0] == "raw" and tkind[1] == kind[1])
                    or tkind[0] in ("domain", "person", "workbench-transit",
                                    "sources", "governance")
                )
                if not ok:
                    errs.append(
                        f"작업 상태는 근거로 쓰지 않는다: {stem} → {name} "
                        f"(Workbench 계약 4.2)" if tkind[0] == "workbench"
                        else f"scope 간 직접 참조: [{kind[1]}] {stem} → {name} {tkind}")
    return errs


def dangling_refs(idx: Index) -> list[str]:
    """미해석 참조 목록 — 위반이 아니라 경고(탐색 링크는 자유)."""
    out = []
    for stem, (p, kind) in idx.nodes.items():
        n = idx.node(p)
        for t in set(n.wikilinks()):
            if idx.resolve(t)[0] == "dangling":
                out.append(f"{stem} → {t}")
    return sorted(out)


def centrality(idx: Index) -> dict[str, float]:
    """중심성 = 개정 비용의 근사(들어오는 의존 가중합). 노드 간 참조만 산입.
    Workbench(헌법 4조 5항)와 통치 구획(시행령 §10 1항)은 출발·도착 모두
    불산입이다."""
    EXCL = ("workbench-transit", "governance")
    score: dict[str, float] = {s: 0.0 for s in idx.nodes}
    for stem, (p, kind) in idx.nodes.items():
        if kind[0] in EXCL:
            continue
        n = idx.node(p)
        for t in n.edges("derived-from"):
            r = idx.resolve(t)
            if r[0] == "node" and r[1][0] not in EXCL:
                score[_score_key(idx, t)] = score.get(_score_key(idx, t), 0.0) + W_DERIVED
        for t in n.wikilinks():
            r = idx.resolve(t)
            if r[0] == "node" and r[1][0] not in EXCL:
                score[_score_key(idx, t)] = score.get(_score_key(idx, t), 0.0) + W_LINK
    return score


def _score_key(idx: "Index", t: str) -> str:
    """중심성 점수는 노드 **stem**으로 키잡는다(score 초기화가 stem이므로).
    대상 표기를 그 노드의 stem으로 접는다 — id형은 by_id로, 경로형·stem형은
    마지막 요소 stem으로. 경로형(`[[= Scope/X/design]]`)을 raw 문자열로 키잡으면
    참조된 노드가 유입 중심성을 통째로 놓친다."""
    if re.match(ID_RE, t) and t in idx.by_id:
        return idx.by_id[t][0].stem
    return contract.target_stem(t)


# ── scope 이름과 space 표기 ──────────────────────────────────────────────
# 배치가 정본이므로(시행령 §3 3항) scope 목록은 디렉토리에서 읽는다. `raw`와
# `wm`이 같은 판정을 쓰므로 여기 한 벌만 둔다 — 두 벌이면 조용히 갈라진다.

def scope_names() -> list[str]:
    """`= Scope/` 아래의 scope 이름. Workbench도 하나의 scope다(헌법 4조 5항)."""
    d = ROOT / "= Scope"
    return sorted(x.name for x in d.iterdir()
                  if x.is_dir() and not x.name.startswith(".")) if d.is_dir() else []


def space_list() -> str:
    """거부 메시지에 싣는 유효 space 목록 — 틀린 값을 준 호출자가 다음에 무엇을
    써야 하는지 그 자리에서 알 수 있어야 한다."""
    return ", ".join(f"= Scope/{s}" for s in scope_names())


def scope_of_space(space: str) -> str | None:
    """`"= Scope/<이름>"` → `<이름>`. 맨 이름은 접지 않는다 — `create_node`가
    맨 이름을 거부하는 것과 같은 규율이며, 같은 표면에서 같은 인자가 다른
    관대함을 가지면 호출자가 규칙을 하나로 배우지 못한다."""
    parts = [x for x in (space or "").strip().strip("/").split("/") if x]
    return parts[1] if len(parts) == 2 and parts[0] == "= Scope" else None
