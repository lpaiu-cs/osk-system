"""osk.write — 노드 쓰기의 **단일 통로**.

구현 근거: Mechanism §6-2 3항(표면은 계약 검증의 강제 지점), 시행령 §1(노드
계약)·§3 4항(pin 대조)·§11(실패는 보류·보고), 헌법 8조(참조 위상)·10조
3항(서명은 사용자 전속)·12조 2항(충돌 후보 기록).

MCP 도구와 CLI가 **같은 이 통로**를 쓴다 — 쓰기 경로가 둘로 갈라지면 한쪽만
계약을 지키게 된다.

이 모듈은 서명 기록부와 pin 기록에 **결코 쓰지 않는다**(Mechanism §6-2 2항).
검증기의 표면 세그먼트가 그 사실을 AST로 강제한다.

동시성 (설계 rev.3 §4):
- v1은 **전역 단일 쓰기 잠금**이다. 노드 단위 잠금을 경로로 키잡으면 move와
  update가 같은 노드에 다른 잠금을 잡아 상호 배제가 깨지고, 이동 뒤 구 경로에
  파일이 부활해 쓰기 통로가 스스로 id 중복을 만든다. 쓰기 1회의 비-토큰 비용이
  40ms 미만이라 경합은 무시 가능하다 — 세분화는 경합이 실측될 때의 최적화다.
- **이름→파일 해석은 잠금 안에서 라이브 파일시스템으로** 한다(캐시 색인으로
  해석하면 낡은 경로에 작용한다).
- 잠금 안 재판독에서 부재·파손이면 거부한다 — 델타가 파손 파일을 "복구"하지
  않는다.

CAS (설계 rev.3 §2): `expect_hash`는 **연산이 아니라 서명에 결속**한다.
본문 전체 치환은 언제나 필수, 무-body 변경은 대상이 **서명 노드일 때만** 필수다.
거부 응답에 현재 해시를 담지 않는다 — 담으면 관측 증명이 연극이 된다.
"""
from __future__ import annotations
import fcntl, json, os, re, tempfile
from pathlib import Path

import yaml

from .core import (ROOT, LEDGER, CANDIDATES, PINS, ROUTING, ID_RE, CASE_RE,
                   ledger_append, ledger_read, new_node_id, now_kst,
                   resolve_in_root, resolve_one, sha256_bytes, sha256_file)
from . import contract, graph, signatures

WRITE_LOCK = LEDGER / ".write.lock"      # 전역 쓰기 잠금 (대장 구획, git 추적 밖)
GOVERNANCE = ("governance",)             # 표면 쓰기 제외 (설계 D8)
CANDIDATE_TYPES = ("contradiction", "duplication", "competition",
                   "lineage-fork", "delegation-overlap")   # Mechanism §4 3항


class WriteError(ValueError):
    """계약 위반·거부. `violations`에 위반 목록을 담는다(부분 성공 없음)."""

    def __init__(self, message: str, violations: list[str] | None = None,
                 **extra):
        super().__init__(message)
        self.violations = violations or [message]
        self.extra = extra


class _Lock:
    """전역 쓰기 잠금 — 잠금 파일은 `_ledger/`에 두고 git에서 무시한다."""

    def __enter__(self):
        WRITE_LOCK.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(WRITE_LOCK, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()
        return False


# ── 파일 해석·직렬화 ─────────────────────────────────────────────────────

def _live_locate(name: str) -> Path | None:
    """이름 → 파일. **라이브 파일시스템**을 훑는다(잠금 안에서만 부른다).

    같은 이름이 둘 이상이면 **거부한다** — 읽기(색인)와 쓰기가 서로 다른 쪽을
    고르면 에이전트가 본 파일과 고쳐지는 파일이 달라진다. 표면 자신은 중복을
    만들 수 없으므로(create_node가 동명을 거부한다) 중복은 언제나 외부 기원이며,
    그렇기에 표면이 임의로 한쪽을 택하는 것이 더 나쁘다."""
    hits = [p for p, _k in graph.iter_nodes() if p.stem == name]
    if not hits and re.match(ID_RE, str(name).strip()):
        # id 형태면 id로도 찾는다 — 쓰기 응답이 id를 돌려주므로 그것을 핸들로
        # 잡은 호출자에게 "노드 없음"은 틀린 진단이다(10차 ②)
        hits = [p for p, _k in graph.iter_nodes()
                if signatures._id_of(p) == str(name).strip()]
    if len(hits) > 1:
        raise WriteError(
            f"같은 이름의 노드가 {len(hits)}개다 — 어느 것인지 정해지지 않아 "
            f"고치지 않았다: {[str(h.relative_to(ROOT)) for h in hits]}")
    return hits[0] if hits else None


def _norm_body(body: str) -> str:
    """`_render`가 쓸 형태로 접는다 — 변경 여부는 **쓰일 형태**로 판정해야
    앞뒤 공백 차이가 헛 변경으로 잡히지 않는다."""
    return body.lstrip("\n").rstrip()


def _render(meta: dict, body: str) -> bytes:
    """계약 순서대로 frontmatter를 직렬화한다 (Mechanism §2 5항)."""
    lines = ["---"]
    for k in contract.ORDER:
        lines.append(f"{k}: {_scalar(meta[k])}")
    for k in contract.PREDICATES:
        if k in meta and meta[k] not in (None, [], ""):
            lines.append(f"{k}: {_scalar(meta[k])}")
    lines.append("---")
    return ("\n".join(lines) + "\n\n" + _norm_body(body) + "\n").encode()


def _scalar(v):
    """JSON 문자열은 유효한 YAML 스칼라다 — 따옴표·백슬래시를 손으로 감싸면
    표면이 스스로 파싱 불가 노드를 만든다(7차 중대 A)."""
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(str(x), ensure_ascii=False)
                               for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── 결속 ─────────────────────────────────────────────────────────────────

def _is_cluster(kind: tuple) -> bool:
    """소속의 둘째 원소가 실제 군집 이름인가. Space 루트 직속 파일은
    `space_of`가 파일명을 군집 이름 자리에 넣으므로(`('scope','x.md')`)
    그것을 군집으로 오인하지 않는다 — 노드는 군집 안에만 둔다."""
    if kind[0] == "workbench-transit":
        return True
    return len(kind) > 1 and bool(kind[1]) and not str(kind[1]).endswith(".md")


def _reject_governance(kind: tuple) -> None:
    if kind[:1] == GOVERNANCE:
        raise WriteError(
            "통치 Facet은 표면 쓰기 대상이 아니다 — 통치 문서의 개정은 "
            "사용자 발의와 대화형 확인의 절차다 (설계 D8)")


def _pinned(target: str) -> bool:
    """군집이 pin으로 고정돼 있는가 (시행령 §3 4·5항 · Mechanism §6 2항)."""
    try:
        recs = ledger_read(PINS)
    except Exception:
        return True             # 판독 실패는 보수적으로 '고정됨' — fail-closed
    keys = {r.get("target") for r in recs if r.get("target")}
    for k in keys:
        r = resolve_one(recs, k, "target")
        if r and r.get("kind") == "pin" and str(k).rstrip("/") == target.rstrip("/"):
            return True
    return False


def _cluster_names() -> list[str]:
    """노드를 둘 수 있는 군집 경로 — **거부 응답에만** 실어 보낸다(상주 비용 0).
    표면에 열거 도구가 없으므로 실패하는 순간에 주소를 가르치는 것이 이 목록의
    일이다(10차 ②의 완화분 — 해결은 `overview`)."""
    out = set()
    for space in ("= Scope", "= Domain", "= Person"):
        d = ROOT / space
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            if not sub.is_dir() or sub.name.startswith((".", "_")):
                continue
            k = graph.space_of(sub / "x.md")
            if graph.is_node_home(k) and _is_cluster(k) and k[:1] != GOVERNANCE:
                out.add(f"{space}/{sub.name}")
    if (ROOT / "= Scope/Workbench/transit").is_dir():
        out.add("= Scope/Workbench/transit")
    return sorted(out)


def _open_cases() -> list[str]:
    """열린(docketed) 사건 번호 — conflicts 거부에 실어 보낸다."""
    return sorted(no for no, c in graph._load_cases().items()
                  if str(c.get("status")) == "docketed")


def _check_edges(edges: dict | None) -> list[str]:
    """술어와 **대상 값의 형**을 함께 본다. 스키마(표면)와 통로(여기) 이중으로
    거는 이유는 CLI·Bash 경유가 스키마를 통과하지 않기 때문이다 — 검증은
    통로에, 교육은 스키마에(10차 정정 ①)."""
    errs = []
    for pred, targets in (edges or {}).items():
        if pred not in contract.PREDICATES:
            errs.append(f"계약 외 술어: {pred} "
                        f"(쓸 수 있는 것: {', '.join(contract.PREDICATES)})")
            continue
        for t in (targets if isinstance(targets, list) else [targets]):
            if not isinstance(t, str) or not t.strip():
                errs.append(f"엣지 대상은 비어 있지 않은 문자열이어야 한다: "
                            f"{pred} → {t!r}")
                continue
            if pred == "conflicts" and not re.match(CASE_RE, t.strip()):
                # 존치 상호 치환은 표면으로 만들 수 없다(닭-달걀) — 에이전트가 달 수
                # 있는 conflicts는 열린 사건 참조뿐이다 (설계 rev.3 §5)
                opened = _open_cases()
                errs.append(
                    f"conflicts는 열린 사건 번호(CASE-<연도>-<일련>)만 표면에서 달 수 "
                    f"있다: {t} — 지금 열린 사건: "
                    f"{', '.join(opened) if opened else '없음'} "
                    f"(노드 간 존치 상호 치환은 판결 적용 절차의 일이다)")
    return errs


def _validate_render(path: Path, meta: dict, body: str) -> tuple[bytes, list[str]]:
    """**렌더된 바이트를 되읽어** 검증한다 — 쓰려던 것이 아니라 쓸 것을
    검증해야 한다(7차 중대 A). 직렬화가 깨뜨린 것은 dict 검사로는 안 보인다."""
    data = _render(meta, body)
    fd, tmp = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            back = contract.parse(Path(tmp))
        except Exception as e:
            return data, [f"직렬화 왕복 실패 — 쓰지 않았다: {e}"]
        # 되읽은 노드에 **목적지 경로**를 씌운다 — contract.validate가 stem을 보므로
        # 임시 파일명을 그대로 두면 stem에 걸린 규칙이 통째로 무력화된다(7차 계열)
        back = contract.Node(path=path, meta=back.meta, body=back.body,
                             fm_keys=back.fm_keys)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if {k: str(v) for k, v in back.meta.items()} != {k: str(v) for k, v in meta.items()}:
        return data, [f"직렬화 왕복 불일치 — 쓰지 않았다: {sorted(back.meta)} vs {sorted(meta)}"]
    return data, _validate_node(path, back, body)


def _validate_node(path: Path, node, body: str) -> list[str]:
    """계약 + 그 노드에서 **나가는 참조**의 위상. 전역 검사는 하지 않는다 —
    남이 만든 위반 때문에 내 쓰기가 막히면 안 된다 (설계 D10)."""
    errs = list(contract.validate(node))
    idx = graph.Index()
    kind = graph.space_of(path)
    for pred in contract.PREDICATES:
        for t in node.edges(pred):
            errs += _topology_of(idx, kind, path.stem, t, pred, node.id)
    for t in node.wikilinks():
        errs += _topology_of(idx, kind, path.stem, t, None, node.id)
    return errs


def _topology_of(idx, kind, stem, name, pred, node_id=None) -> list[str]:
    if pred == "conflicts":
        # 사건 표지는 참조 위상의 예외다(헌법 8조). 표면에서는 열린 사건에
        # 당사자로서 다는 것만 허용한다 — 전역 topology_check와 같은 의미론.
        case = graph._load_cases().get(name)
        if not case:
            return [f"실재하지 않는 사건 참조: {stem} → {name}"]
        if str(case.get("status")) != "docketed":
            return [f"열린 사건이 아니다: {stem} → {name} ({case.get('status')})"]
        if node_id not in [str(x) for x in (case.get("parties") or [])]:
            return [f"그 사건의 당사자가 아니다: {stem} → {name} (헌법 12조 5항)"]
        return []
    r = idx.resolve(name)
    if r[0] in ("external", "dangling"):
        return []                       # dangling은 경고이지 위반이 아니다
    if r[0] == "ambiguous":
        return [f"모호 참조(동명 노드 중복): {stem} → {name}"]
    tkind = r[1]
    if kind[0] == "domain" and tkind[0] == "raw":
        return [f"Domain의 _raw 직접 참조: {stem} → {name}"]
    if kind[0] == "scope":
        ok = ((tkind[0] == "scope" and tkind[1] == kind[1])
              or (tkind[0] == "raw" and tkind[1] == kind[1])
              or tkind[0] in ("domain", "person", "workbench-transit",
                              "sources", "governance"))
        if not ok:
            return [f"scope 간 직접 참조: [{kind[1]}] {stem} → {name} {tkind}"]
    return []


def _dangling_of(path: Path, meta: dict, body: str) -> list[str]:
    """그 노드의 미해석 참조 — 위반이 아니라 경고. 응답에 실어 에이전트가
    조용히 dangling을 쌓지 않게 한다(`list_nodes` 제거의 부작용 차단)."""
    node = meta if isinstance(meta, contract.Node) else \
        contract.Node(path=path, meta=meta, body=body)
    idx = graph.Index()
    out = []
    for t in set(node.wikilinks()) | {t for p in contract.PREDICATES
                                      for t in node.edges(p)}:
        if idx.resolve(t)[0] == "dangling":
            out.append(t)
    return sorted(out)


def _cas(path: Path, node_id: str, expect_hash: str | None,
         body_given: bool) -> dict:
    """CAS는 **서명에 결속**한다 (설계 rev.3 §2). 반환: 서명 표면화 정보.
    거부 응답에 현재 해시를 담지 않는다 — 관측 증명이 연극이 되지 않게."""
    signed = signatures.status(node_id, path) == "signed"
    need = body_given or signed
    if need and not expect_hash:
        rec = signatures.causal_maxima(signatures.records(), node_id) \
            if signed else []
        raise WriteError(
            "서명된 노드다 — 읽은 상태의 해시(expect_hash)를 함께 보내야 한다"
            if signed else
            "본문 전체 치환에는 읽은 상태의 해시(expect_hash)가 필요하다",
            signed=signed,
            signature_rid=(rec[0]["rid"] if rec else None))
    if expect_hash and sha256_file(path) != expect_hash:
        raise WriteError(
            "그 사이 노드가 변경됐다 — 다시 읽고 재시도하라 (CAS 불일치)")
    return {"was_signed": signed}


# ── 세션 라우팅 (Mechanism §6-2 3항) ─────────────────────────────────────

ALIAS_DEPTH = 8          # 별칭 사슬 추적 한계 — 순환·장난에 대한 보수적 상한


def canonical_session(session: str | None,
                      recs: list[dict] | None = None) -> str | None:
    """세션 키를 **정본 키**로 접는다 (Mechanism §6-2 6항).

    저장소·서버의 개명은 흔한 일이고(이 체계도 llm-vault → ltm-vault →
    osk-system을 거쳤다), 그때마다 세션이 다른 scope로 흩어지면 라우팅이
    이름의 역사에 인질이 된다. 별칭 기록(`kind: "alias"`)이 구 이름을 정본
    이름으로 접어 **어느 이름으로 들어오든 한 scope로 모이게** 한다.

    별칭도 인과 극대가 정본이다 — 분기·순환이면 접지 않고 원래 키를 쓴다."""
    if not session:
        return None
    if recs is None:
        try:
            recs = ledger_read(ROUTING)
        except Exception:
            return session
    seen = {session}
    cur = session
    for _ in range(ALIAS_DEPTH):
        r = resolve_one(recs, cur, "session")
        if not r or r.get("kind") != "alias":
            return cur
        nxt = r.get("canonical")
        if not nxt or nxt in seen:
            return session      # 순환은 접지 않는다 — 원래 입력 키로 남는다
        seen.add(nxt)
        cur = nxt
    return cur


def resolve_session(session: str | None) -> str | None:
    """세션 키 → scope 이름. 별칭을 접은 뒤 판정하며, 인과 극대가 유일할 때만
    확정이고 분기(다기기 동시 최초-확정)면 미확정으로 두어 `space`를 요구한다
    — fail-closed."""
    if not session:
        return None
    try:
        recs = ledger_read(ROUTING)
    except Exception:
        return None
    key = canonical_session(session, recs)
    r = resolve_one(recs, key, "session")
    return r.get("scope") if r and r.get("kind") == "bind" else None


def bind_session(session: str, scope: str, reason: str = "") -> dict:
    """세션→scope 확정. 별칭은 정본 키로 접어 기록하므로 구 이름으로 들어와도
    한 자리에 모인다. 재바인딩도 **새 행 append**다(append-only 유지 — 새
    인과 극대가 새 바인딩)."""
    return ledger_append(ROUTING, {
        "kind": "bind", "session": canonical_session(session) or session,
        "scope": scope, "reason": reason or "최초 작업에서 확정"})


def alias_session(alt: str, canonical: str, reason: str = "") -> dict:
    """구 이름 → 정본 이름 별칭. 개명 이력을 대장에 남기는 일이며 MCP 표면에
    노출하지 않는다 — 이름의 정본을 정하는 것은 사용자의 일이다."""
    return ledger_append(ROUTING, {
        "kind": "alias", "session": alt, "canonical": canonical,
        "reason": reason or "개명 이력"})


# ── 도구 ─────────────────────────────────────────────────────────────────

def create_node(title: str, summary: str, body: str, drafter: str,
                session: str | None = None, space: str | None = None,
                edges: dict | None = None) -> dict:
    """노드 생성. id·시각은 **서버 전속**이고 author는 `agent` 고정이다(D5).
    space가 없으면 세션 라우팅으로 착지를 정하고, 라우팅이 없으면 space를
    요구한 뒤 성공 시 그 scope로 세션을 확정한다."""
    with _Lock():
        errs = _check_edges(edges)
        if "/" in title or title.startswith(".") or not title.strip():
            errs.append(f"부적격 제목: {title!r}")
        if errs:
            raise WriteError("계약 위반 — 쓰지 않았다", errs)

        bound = resolve_session(session)
        dest = space or (f"= Scope/{bound}" if bound else None)
        if not dest:
            raise WriteError(
                "착지가 정해지지 않았다 — space를 지정하라. "
                f"지금 쓸 수 있는 군집: {', '.join(_cluster_names()) or '없음'}. "
                "session도 함께 주면(저장소 이름처럼 세션이 바뀌어도 같은 값) "
                "그 scope로 결속되어 다음부터 space 없이 착지한다")
        dest_dir = resolve_in_root(dest)
        if dest_dir is None or not dest_dir.is_dir():
            raise WriteError(
                f"선언되지 않은 군집이다 — 군집 신설은 사용자 발의다: {dest}. "
                f"지금 쓸 수 있는 군집: {', '.join(_cluster_names()) or '없음'}")
        path = dest_dir / f"{title}.md"
        kind = graph.space_of(path)      # 소속은 노드 파일 경로로 판정한다
        _reject_governance(kind)
        if not graph.is_node_home(kind) or not _is_cluster(kind):
            raise WriteError(
                f"노드를 둘 수 없는 구획이다: {dest} {kind} — 노드는 군집 안에 둔다"
                f" (Space 루트 직속 불가, Mechanism §1 4항)")

        idx = graph.Index()
        if title in idx.nodes or title in getattr(idx, "broken", {}):
            raise WriteError(
                f"같은 이름의 노드가 이미 있다: {title} — 생성하면 중복 후보가 된다")
        if path.exists():
            raise WriteError(f"이미 있는 파일이다: {title}")

        now = now_kst()
        meta = {"id": new_node_id(_existing_ids(idx)),
                "created": now, "updated": now,
                "author": "agent", "drafter": drafter, "summary": summary}
        for pred, tg in (edges or {}).items():
            meta[pred] = _as_links(tg)
        data, errs = _validate_render(path, meta, body)
        if errs:
            raise WriteError("계약·위상 위반 — 쓰지 않았다", errs)

        _atomic_write(path, data)
        # 결속은 **scope일 때만** — Domain/Person에 결속하면 자동 라우팅이
        # 존재하지 않는 `= Scope/<이름>`을 가리켜 그 키가 벽돌이 된다(7차 중대 C)
        bound_now = None
        if session and not bound and kind[0] == "scope":
            bind_session(session, dest_dir.name)
            bound_now = dest_dir.name       # 실제로 결속했을 때만 보고한다
        return {"ok": True, "name": title,
                "path": str(path.relative_to(ROOT)), "id": meta["id"],
                "new_hash": sha256_bytes(data), "signed": False,
                "bound_scope": bound_now,
                "dangling": _dangling_of(path, meta, body)}


def _existing_ids(idx) -> set[str]:
    """전수 중복 대조용 id 집합 (Mechanism §2 1항). 파싱 실패 노드는 id를 알 수
    없으므로 건너뛴다 — 그 파일은 색인이 broken으로 이미 보고한다."""
    out = set()
    for p, _k in idx.nodes.values():
        try:
            out.add(idx.node(p).id)
        except Exception:
            continue
    return out


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def _as_links(targets) -> str | list:
    vals = _as_list(targets)
    out = [t if str(t).startswith("[[") else f"[[{t}]]" for t in vals]
    return out[0] if len(out) == 1 else out


def update_node(name: str, body: str | None = None,
                expect_hash: str | None = None, summary: str | None = None,
                add_edges: dict | None = None,
                remove_edges: dict | None = None) -> dict:
    """본문·summary·엣지 수정. 엣지는 **델타**이므로 서버가 잠금 안에서 현재
    상태에 적용한다 — 낡은 읽기가 앞선 갱신을 덮는 일이 구조적으로 없다."""
    with _Lock():
        errs = _check_edges(add_edges) + _check_edges(remove_edges)
        if errs:
            raise WriteError("계약 위반 — 쓰지 않았다", errs)
        path = _live_locate(name)
        if path is None or not path.is_file():
            raise WriteError(f"노드 없음: {name}")
        kind = graph.space_of(path)
        _reject_governance(kind)
        try:
            n = contract.parse(path)
        except Exception as e:
            raise WriteError(f"파손된 노드다 — 수동 확인이 먼저다: {name} ({e})")

        sig = _cas(path, n.id, expect_hash, body is not None)
        meta = dict(n.meta)
        replaced_summary = None
        changed = False
        if summary is not None and str(meta.get("summary")) != summary:
            replaced_summary = str(meta.get("summary"))
            meta["summary"] = summary
            changed = True
        for pred, tg in (add_edges or {}).items():
            cur = n.edges(pred)
            have = {contract.target_stem(x) for x in cur}   # 표기 차이는 같은 대상
            new = [t for t in _as_list(tg)
                   if contract.target_stem(t) not in have]
            if new:
                meta[pred] = _as_links(cur + new)
                changed = True
        for pred, tg in (remove_edges or {}).items():
            drop = {contract.target_stem(t) for t in _as_list(tg)}
            cur = n.edges(pred)
            keep = [t for t in cur if contract.target_stem(t) not in drop]
            if len(keep) != len(cur):
                changed = True
                if keep:
                    meta[pred] = _as_links(keep)
                else:
                    meta.pop(pred, None)
        new_body = n.body if body is None else body
        if body is not None and _norm_body(body) != _norm_body(n.body):
            changed = True
        extra_keys = [k for k in meta
                      if k not in contract.ORDER and k not in contract.PREDICATES]
        if extra_keys:
            # 손으로 넣은 필드를 조용히 지우지 않는다 — 직렬화가 계약 필드만
            # 쓰므로 통과시키면 무경고 소실이 된다(7차 경미 F)
            raise WriteError(
                f"계약 밖 필드가 있는 노드다 — 표면으로 고칠 수 없다: {extra_keys}",
                [f"계약 외 필드: {k}" for k in extra_keys])
        # conflicts 표지의 부착·원상 제거는 updated을 갱신하지 않는다
        # (시행령 §1 4항 — 헌법 3조 4항의 예외)
        only_conflicts = (body is None and summary is None
                          and not (add_edges or {}).keys() - {"conflicts"}
                          and not (remove_edges or {}).keys() - {"conflicts"}
                          and bool(add_edges or remove_edges))
        if not changed:
            # 변경이 없으면 쓰지 않는다 — 내용이 그대로인데 updated만
            # 갱신하면 "상태가 변경될 때 갱신한다"(시행령 §1 4항)에 어긋난다
            return {"ok": True, "no_change": True, "name": name,
                    "path": str(path.relative_to(ROOT)), "id": n.id,
                    "new_hash": sha256_file(path),
                    "signed": signatures.status(n.id, path) == "signed",
                    "edges": {p: n.edges(p) for p in contract.PREDICATES},
                    "dangling": _dangling_of(path, n.meta, n.body)}
        if not only_conflicts:
            meta["updated"] = now_kst()

        data, errs = _validate_render(path, meta, new_body)
        if errs:
            raise WriteError("계약·위상 위반 — 쓰지 않았다", errs)
        _atomic_write(path, data)
        out = {"ok": True, "name": name, "path": str(path.relative_to(ROOT)),
               "id": n.id, "new_hash": sha256_bytes(data),
               "signed": False, "updated_kept": only_conflicts,
               "edges": {p: contract.Node(path=path, meta=meta,
                                          body=new_body).edges(p)
                         for p in contract.PREDICATES},
               "dangling": _dangling_of(path, meta, new_body)}
        out.update(sig)
        if sig.get("was_signed"):
            out["now_unsigned"] = True
        if replaced_summary is not None:
            out["replaced_summary"] = replaced_summary
        return out


def move_node(name: str, dest_space: str) -> dict:
    """군집 재배정. 이동은 바이트 불변이라 서명이 존속한다 — CAS가 없다.
    pin된 군집은 출발·도착 어느 쪽이든 거부한다(시행령 §3 4항)."""
    with _Lock():
        path = _live_locate(name)
        if path is None or not path.is_file():
            raise WriteError(f"노드 없음: {name}")
        src_kind = graph.space_of(path)
        _reject_governance(src_kind)
        dest_dir = resolve_in_root(dest_space)
        if dest_dir is None or not dest_dir.is_dir():
            raise WriteError(
                f"선언되지 않은 군집이다: {dest_space}. "
                f"지금 쓸 수 있는 군집: {', '.join(_cluster_names()) or '없음'}")
        target = dest_dir / path.name
        dst_kind = graph.space_of(target)   # 소속은 노드 파일 경로로 판정한다
        _reject_governance(dst_kind)
        if not graph.is_node_home(dst_kind) or not _is_cluster(dst_kind):
            raise WriteError(
                f"노드를 둘 수 없는 구획이다: {dest_space} {dst_kind} —"
                f" 노드는 군집 안에 둔다 (Space 루트 직속 불가)")
        src_rel = str(path.parent.relative_to(ROOT)) + "/"
        if _pinned(src_rel) or _pinned(str(dest_dir.relative_to(ROOT)) + "/"):
            raise WriteError(
                "pin으로 고정된 군집이다 — 자동 재배정에서 제외된다 "
                "(시행령 §3 4항). 사용자 발의로만 옮긴다")
        if target.exists():
            raise WriteError(f"목적지에 같은 이름이 있다: {target.name}")
        try:
            n = contract.parse(path)
        except Exception as e:
            raise WriteError(f"파손된 노드다 — 수동 확인이 먼저다: {name} ({e})")
        before = sha256_file(path)
        os.replace(path, target)          # 바이트 불변 — updated 갱신 없음
        return {"ok": True, "name": name, "id": n.id,
                "path": str(target.relative_to(ROOT)),
                "new_hash": before, "moved_from": str(path.relative_to(ROOT)),
                "signed": signatures.status(n.id, target) == "signed",
                "dangling": _dangling_of(target, n.meta, n.body)}


def record_candidate(type: str, nodes: list[str], reason: str = "") -> dict:
    """충돌 후보 상정 (헌법 12조 2항). 같은 근거(basis)의 후보·각하가 이미
    있으면 append하지 않고 기존 기록을 돌려준다 — 12조 2항 후단의 중복 금지와
    12조 3항의 각하 억제가 여기서 함께 닫힌다. 각하는 사용자 전속이다."""
    with _Lock():
        if type not in CANDIDATE_TYPES:
            raise WriteError(f"미정의 충돌 유형: {type} (Mechanism §4 3항)")
        idx = graph.Index()
        parties = []
        for nm in nodes:
            hit = idx.nodes.get(nm)
            if hit is None:
                raise WriteError(f"당사자 노드 없음: {nm} — 근거가 성립하지 않는다")
            try:
                parties.append((idx.node(hit[0]).id, sha256_file(hit[0])))
            except Exception as e:
                raise WriteError(f"당사자 판독 실패: {nm} ({e})")
        if len({i for i, _ in parties}) < 2:
            raise WriteError(
                "충돌은 **서로 다른** 둘 이상의 당사자를 요한다 — 자기 자신과의"
                " 충돌은 성립하지 않는다")
        basis = _basis(type, parties)
        recs = ledger_read(CANDIDATES)
        for r in recs:
            if r.get("basis") == basis and r.get("basis_version") == 1:
                return {"ok": True, "deduped": True, "rid": r.get("rid"),
                        "kind": r.get("kind"), "basis": basis,
                        "note": "같은 근거의 기록이 이미 있다 (헌법 12조 2·3항)"}
        rec = ledger_append(CANDIDATES, {
            "kind": "candidate", "basis": basis, "basis_version": 1,
            "type": type, "nodes": [i for i, _ in parties], "reason": reason})
        return {"ok": True, "deduped": False, "rid": rec["rid"],
                "basis": basis, "note": "사용자 심의 대상으로 상정됐다"}


def _basis(type: str, parties: list[tuple[str, str]]) -> str:
    """정렬된 당사자 id + 유형 + 검사 당시 상태 해시 (Mechanism §4 2항).
    상태 해시가 '무엇이 달라지면 다시 물어도 되는가'의 답이다 — 없으면 각하가
    영구 봉인이 된다."""
    key = "v1|" + type + "|" + ",".join(sorted(f"{i}@{h}" for i, h in parties))
    return sha256_bytes(key.encode())
