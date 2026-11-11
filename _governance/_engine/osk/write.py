"""osk.write — 노드 쓰기의 **단일 통로**.

구현 근거: Mechanism §6-2 3항(표면은 계약 검증의 강제 지점), 시행령 §1(노드
계약)·§3 4항(pin 대조)·§11(실패는 보류·보고), 헌법 8조(참조 위상)·12조
2항(충돌 후보 기록).

MCP 도구와 CLI가 **같은 이 통로**를 쓴다 — 쓰기 경로가 둘로 갈라지면 한쪽만
계약을 지키게 된다.

통로의 부분은 제 모듈에 산다: 파일 해석과 직렬화는 `nodefile`, 군집의 자리 규칙과
신설 관문은 `cluster_gate`, 쓰기 전 검사는 `node_checks`, 이동은 `node_moves`다. 여기에는
생성·갱신·충돌 후보 기록이 남는다. 표면 검사(`validate.SURFACE_MODULES`)는 이 모두를 본다.

이 모듈은 승인 기록부와 pin 기록에 **결코 쓰지 않는다**(Mechanism §6-2 2항).
보호영역의 승인·반려는 사용자 전속(대화형 단말)이며, 표면 쓰기는 언제나
작업본에 반영될 뿐이다(§6-2 8항) — 검증기의 표면 세그먼트가 이를 AST로
강제한다.

동시성 (설계 rev.3 §4):
- v1은 **전역 단일 쓰기 잠금**이다. 노드 단위 잠금을 경로로 키잡으면 move와
  update가 같은 노드에 다른 잠금을 잡아 상호 배제가 깨지고, 이동 뒤 구 경로에
  파일이 부활해 쓰기 통로가 스스로 id 중복을 만든다. 쓰기 1회의 비-토큰 비용이
  40ms 미만이라 경합은 무시 가능하다 — 세분화는 경합이 실측될 때의 최적화다.
- **이름→파일 해석은 잠금 안에서 라이브 파일시스템으로** 한다(캐시 색인으로
  해석하면 낡은 경로에 작용한다).
- 잠금 안 재판독에서 부재·파손이면 거부한다 — 델타가 파손 파일을 "복구"하지
  않는다.

CAS (Mechanism §6-2 4항): `expect_hash`는 **본문 전체 치환**에 결속한다 —
보지 않은 상태를 덮지 않게 한다. 부분 변경(엣지 델타·summary)에는 요구하지
않는다. 거부 응답에 현재 해시를 담지 않는다 — 담으면 관측 증명이 연극이 된다.
"""
from __future__ import annotations
import re, unicodedata
from pathlib import Path

from .core import (ID_RE, ledger_append, ledger_read, mutation_lock, new_node_id, now_kst, posix_rel,
                   resolve_in_root, sha256_bytes, sha256_file)
from . import (approvals, cluster_gate, contract, evictions, graph, node_checks, nodefile, prose, raw, rechecks,
               workspaces, placement)
from .contract import as_list, stored_edges
from .errors import WriteError
from .naming import name_collision, portable_name_key, title_errors
from .prose import norm_body, norm_newlines, space_numeric_tags
# 결속·라우팅은 `routing`에 산다. 위층과 시험이 `write.<이름>`으로 부르는 동안 이 이름들을 남긴다.
from .routing import (ALIAS_DEPTH, EPHEMERAL_SESSION_RE, alias_session, bind_after_write, bind_session,
                      bindings, bound_scopes, canonical_session, ephemeral_session_errors, repo_session,
                      resolve_landing, resolve_session, routing_errors, set_primary, unbind_session)
from .vault import Vault

CANDIDATE_TYPES = ("contradiction", "duplication", "competition",
                   "delegation-overlap")   # Mechanism §4 3항 (lineage-fork 폐지)


# ── 도구 ─────────────────────────────────────────────────────────────────

def create_node(vault: Vault, title: str, summary: str, body: str, drafter: str,
                session: str | None = None, space: str | None = None,
                edges: dict | None = None, settle: str | None = None) -> dict:
    """Create an ordinary node under the shared mutation lock, then hang it in its place."""
    with mutation_lock(vault):
        idx = graph.Index(vault)             # 생성과 자리 걸기가 이 한 벌을 쓴다
        return _place(vault, create_node_locked(vault, title, summary, body, drafter, session,
                                          space, edges, settle, _idx=idx), idx)


def node_placement(vault: Vault, p: Path, idx, *, repair: bool = False, selected: dict | None = None) -> dict:
    """Current navigation is separate from the immutable selected source/target."""
    top = vault.root.joinpath(*p.relative_to(vault.root).parts[:2])
    child = p
    directory = p.parent.parent if placement.is_hub(p) else p.parent
    current_hubs = []
    while len(directory.relative_to(vault.root).parts) >= 2:
        hp = placement.hub_file(directory)
        if hp is None:
            raise WriteError("current placement hub missing; pending")
        hn = contract.parse(hp)
        # 선택한 허브는 그 군집(폴더)으로 가린다 — 확장자 표기가 바뀐 허브도 저장한 id와 대조한다.
        if (selected and posix_rel(p, vault.root) == selected["target"]["path"]
                and directory == vault.root / Path(selected["hub"]["path"]).parent
                and hn.id != selected["hub"]["id"]):
            raise WriteError("hub identity changed; pending")
        if child not in {nodefile.live_locate(vault, ref, idx) for ref in hn.wikilinks()}:
            if not repair:
                raise WriteError("selected hub does not link to target; pending")
            # 이름으로 부른다 — id 핸들은 색인 전수 판독을 부른다(`live_locate`). 같은 잠금 안의 색인을 잇는다.
            update_node_locked(vault, hp.stem,
                body=hn.body.rstrip() + "\n\n- [[" + child.stem + "]]\n",
                expect_hash=sha256_file(hp), _idx=idx)
        current_hubs.append({"id": hn.id, "path": posix_rel(hp, vault.root), "hash": sha256_file(hp)})
        if directory == top:
            break
        child, directory = hp, directory.parent
    if not current_hubs:
        raise WriteError("current placement has no parent hub; pending")
    return {"status": "complete", "target_path": posix_rel(p, vault.root), "hubs": current_hubs}


def _place(vault: Vault, out: dict, idx) -> dict:
    """새 노드를 같은 작업 안에서 자리에 건다(시행령 §3 8항) — 착지 군집의 허브부터 최상위
    허브까지 각 층이 아래 고리를 Link로 가리키게 한다. 증류와 같은 배선이며, 허브 본문 끝에
    `- [[제목]]` 줄을 더한다. 갈래 안의 정리는 조직 검토가 맡는다. 노드를 먼저 세웠으므로 허브
    쓰기가 실패하면 노드는 그대로 두고, 아직 아래 고리를 가리키지 않는 허브를 `hub_links`로
    알린다. 최상위 허브는 이을 위가 없고, Workbench는 자체 계약이라 걸지 않는다. 증류 쓰기는
    `create_node_locked`를 직접 부르고 자기 배선을 한다."""
    if not out.get("ok"):
        return out
    path = vault.root / out["path"]
    if (placement.space_of(vault, path)[0] not in ("scope", "domain", "person")
            or placement.is_hub(path) and len(path.parent.relative_to(vault.root).parts) <= 2):
        return out
    try:
        placed = node_placement(vault, path, idx, repair=True)
        out["placed"] = [Path(h["path"]).stem for h in placed["hubs"]]
    except (WriteError, OSError, ValueError) as e:
        out["hub_links"] = _unwired(vault, path, idx)
        out["placement_error"] = list(dict.fromkeys([str(e), *getattr(e, "violations", [])]))
    return out


def _unwired(vault: Vault, path: Path, idx) -> list[dict]:
    """아래 고리를 Link로 가리키지 않는 허브 — `move_nodes`의 `hub_links`와 같은 꼴이다. 허브
    없는 층에서 멈춘다. 그 자리는 검증기가 hubless로 알린다."""
    out, child = [], path
    d = path.parent.parent if placement.is_hub(path) else path.parent
    while len(d.relative_to(vault.root).parts) >= 2 and (hub := placement.hub_file(d)) is not None:
        try:
            linked = child in {nodefile.live_locate(vault, r, idx) for r in contract.parse(hub).wikilinks()}
        except (WriteError, OSError, ValueError):
            linked = False
        if not linked:
            out.append({"hub": hub.stem, "add": [child.stem]})
        child, d = hub, d.parent
    return out


def create_node_locked(vault: Vault, title: str, summary: str, body: str, drafter: str,
                session: str | None = None, space: str | None = None,
                edges: dict | None = None, settle: str | None = None, *, _before_write=None,
                _identity=None, _legacy_raw=False, _idx=None) -> dict:
    """노드 생성. id·시각은 **서버 전속**이고 author는 `agent` 고정이다(D5).
    space가 없으면 세션 라우팅으로 착지를 정하고, 라우팅이 없으면 space를
    요구한 뒤 성공 시 그 scope로 세션을 확정한다."""
    # 이 쓰기가 쓰는 색인은 **하나**다 (v3.7.0). 구판은 한 번의 생성에서
    # 세 벌을 지었다 — 계약 검사·이름 유일성·검증·dangling이 각자 지었고,
    # 그래서 체감 비용이 단가의 3배였다. 잠금 안이라 그 사이에 파일이
    # 바뀌지 않으므로 한 벌이면 족하다. 자리 걸기(`_place`)도 같은 벌을 받는다.
    idx = graph.Index(vault) if _idx is None else _idx
    nodefile.require_complete(idx)
    if settle is not None:
        evictions.require_evict(vault, settle)
    errs = (node_checks.check_edges(vault, edges, idx) + title_errors(title)
            + ephemeral_session_errors(session) + routing_errors(vault, session))
    if errs:
        raise WriteError("계약 위반 — 쓰지 않았다", errs)

    # 기본 착지는 프로젝트의 주된 scope다. 결속에 없는 scope를 명시해도 거부하지 않는다 —
    # 한 프로젝트가 여러 scope의 활동을 품고, 성공한 쓰기가 그 scope를 결속에 더한다
    # (Mechanism §6-2 6항 · 시행령 §3 10항). Domain·Person 착지는 결속과 무관하다.
    bound = resolve_session(vault, session)
    dest = space or (f"{vault.scope}/{bound}" if bound else None)
    if not dest:
        bound_set = bindings(vault, session)["scopes"]
        raise WriteError(
            "착지가 정해지지 않았다 — space를 지정하라. "
            f"지금 쓸 수 있는 군집: {', '.join(cluster_gate.cluster_names(vault)) or '없음'}. "
            + ("이 세션은 일회성 작업 폴더라 결속하지 않는다 — 쓸 때마다 space를 준다"
               if workspaces.is_one_off(vault, session) else
               (f"이 프로젝트의 결속은 {', '.join(bound_set)}이지만 주된 scope가 없다 — "
                f"쓸 때마다 space를 준다. 주된 scope의 지정은 대화 검토나 사용자가 한다"
                if bound_set else
                "session(훅이 준 프로젝트 키)도 함께 주면 그 scope가 결속에 더해진다. "
                "주된 scope는 대화 검토의 소속 판정이나 사용자가 지정한다")))
    dest_dir = resolve_in_root(vault, dest)
    if dest_dir is None or not dest_dir.is_dir():
        # 신설 후보 — 막는 대신 한 번 묻는다. 통과하면 디렉토리가 생긴다.
        # (구판은 "신설은 사용자 발의다"라며 전면 거부했으나 그 문구는
        # 규범 무근거였다 — 헌법은 형성의 자동화를 기본으로 둔다.)
        dest_dir = cluster_gate.new_cluster_gate(vault, dest, dest_dir, "이 쓰기가")
    path = dest_dir / f"{title}.md"
    kind = placement.space_of(vault, path)      # 소속은 노드 파일 경로로 판정한다
    cluster_gate.require_node_cluster(kind, dest)
    # 새 군집의 첫 노드는 **동명 허브 노드**다 (시행령 §3 6항). 신설
    # 관문을 지나 방금 생겼든 이미 비어 있든, 허브 없이 출발한 군집은
    # 이름뿐인 통이 된다 — 무엇인지 서술하는 노드가 먼저다. 이동·재배정
    # 형성은 이 검사를 받지 않고(§3 1항의 주기 처리 보호) 검증기 보고가
    # 채움을 독촉한다. Workbench 구획은 자체 계약이라 제외.
    if (title != dest_dir.name and "Workbench" not in dest_dir.parts
            and not any(dest_dir.glob("*.md"))):
        raise WriteError(
            "새 군집의 첫 노드는 허브 노드다 — 쓰지 않았다",
            [f"`{dest_dir.name}` 군집이 비어 있다. 먼저 군집과 동명의 "
             f"허브 노드 `{dest_dir.name}`을(를) 만들어 이 군집이 무엇인지 "
             f"서술하고, 그 다음 이 노드를 만들어 허브에서 닿게 하라 "
             f"(헌법 3조 8항 · 시행령 §3 6항)"])

    # 이름 색인 하나로 끝난다 — 구판의 `nodes ∪ broken`과 **같은 집합**임이
    # 1,809·10,000 노드 양쪽에서 차집합 공집합으로 검증됐다(심의 실측).
    # 이 검사 때문에 전 노드를 열 이유가 없다.
    if title in idx.names:
        raise WriteError(
            f"같은 이름의 노드가 이미 있다: {title} — 생성하면 중복 후보가 된다")
    # 전역 유일성도 **이식성 키**로 본다. 정확 일치만 보면 `Foo`와 `foo`가
    # 다른 군집에 함께 서고, 그 둘을 한 군집으로 옮기는 순간 대소문자를
    # 접는 파일시스템에서 한쪽이 조용히 덮인다(`move_nodes`). 제목이
    # 전역에서 유일하다는 계약(Mechanism §8 2항)은 "모든 기기에서"여야 한다.
    pkey = portable_name_key(title)
    gclash = next((s for s in idx.names if portable_name_key(s) == pkey),
                  None)
    if gclash is not None:
        raise WriteError(
            f"이식성 기준으로 같은 이름의 노드가 이미 있다: {gclash} — "
            f"대소문자나 유니코드 정규화만 다른 이름은 NTFS·APFS에서 같은 "
            f"경로가 된다. 한 군집으로 모이면 한쪽이 덮인다")
    clash = name_collision(dest_dir, title)
    if clash is not None:
        raise WriteError(
            f"같은 군집에 이미 있는 이름이다: {clash} — 대소문자나 유니코드 "
            f"정규화만 다른 이름은 NTFS·APFS에서 **같은 경로**가 되어 그 기기의 "
            f"체크아웃에서 충돌한다(한쪽만 남는다)")

    now = _identity["created"] if _identity else now_kst()
    meta = {"id": _identity["id"] if _identity else new_node_id(),
            "created": now, "updated": now,
            "author": "agent", "drafter": drafter, "summary": summary}
    for pred, tg in (edges or {}).items():
        meta[pred] = _as_links(vault, pred, _merge_edges(vault, [], tg, idx), legacy_raw=_legacy_raw)
    data, errs = node_checks.validate_render(vault, path, meta, body, idx)
    if errs:
        raise WriteError("계약·위상 위반 — 쓰지 않았다", errs)
    if settle is not None and not norm_body(contract.parse_bytes(path, data).body):
        raise WriteError("보존할 본문이 없다 — 쓰지도 처분하지도 않았다")

    _baseline(vault, idx)
    if _before_write is not None:
        _before_write(path, data)
    nodefile.write_file(path, data)
    # 방금 쓴 노드를 손에 든 색인의 **이름 색인**에 등재한다 —
    # `node_checks.dangling_of`가 보는 것은 쓰기 **후**의 상태여야 한다. 구판은 여기서
    # 색인을 새로 지어 그 상태를 얻었다. 등재하지 않으면 자기 자신을
    # 가리키는 Link가 dangling으로 잘못 실려, 오타가 아닌 것을 오타라고
    # 알리게 된다. 판독은 `resolve`가 그때 한 번 한다(파일은 이미 있다).
    idx.register_new(path, kind)
    # 결속은 **scope일 때만** — Domain/Person에 결속하면 자동 라우팅이
    # 존재하지 않는 `Scope/<이름>`을 가리켜 그 키가 벽돌이 된다(7차 중대 C).
    # 주된 scope가 있어도 다른 scope의 쓰기는 집합에 더한다(시행령 §3 10항) —
    # 이미 결속에 있는 scope는 `bind_after_write`가 거른다.
    bound_now, receipt = None, {}
    if session and kind[0] == "scope":
        # 결속 값은 **scope 이름**이지 말단 디렉토리명이 아니다. 구판은
        # `dest_dir.name`을 썼고, 그래서 하위 군집(`Scope/W1/Sub`)에서
        # 처음 쓴 세션이 존재하지 않는 `Sub`에 묶였다 — 그 뒤 `append_raw`는
        # `Scope/Sub/_raw/`라는 유령 scope를 만들고, 노드 생성은 최상위
        # 신설 관문으로 갔다. 깊이는 갈래이지 소속이 아니며(Mechanism §1
        # 2항), 소속은 `space_of`가 이미 정확히 말해 준다.
        if bind_after_write(vault, receipt, session, kind[1]):
            bound_now = kind[1]         # 실제로 결속했을 때만 보고한다
    result = {"ok": True, "name": title,
            "path": posix_rel(path, vault.root), "id": meta["id"],
            "new_hash": sha256_bytes(data),
            "bound_scope": bound_now, **receipt,
            **node_checks.reference_feedback(vault, path, meta, body, idx)}
    result.update(rechecks.after_write(vault, idx, path, meta))
    return evictions.after_node_write(vault, result, settle, "node", title)


def _baseline(vault: Vault, idx) -> None:
    """재검토 기준선(`rechecks.ensure_baseline`). 적지 못해도 쓰기를 막지 않는다 —
    그 근거들이 후보로 남을 뿐이다."""
    try:
        rechecks.ensure_baseline(vault, idx)
    except Exception:      # 격리: 기준선을 못 적어도 쓰기는 간다 — 그 근거들이 후보로 남을 뿐이다
        pass


# 파손 파일에 대한 생성 관문은 **두지 않는다.**
#
# v3.7.2까지는 `_existing_ids` 안에 그런 관문이 있었고 근거는 id였다 — "그
# 파일의 id를 모르면 새 id가 겹치지 않는다고 증명할 수 없다". 근거는 §4-1이
# 지웠지만 관문 자체는 다른 일을 하고 있었다: 파손 파일이 오래 남지 못하게
# 하는 **강제 함수**. 그것을 제자리에 다시 세우려 했으나 값이 맞지 않는다 —
# "파손이 있는가"를 물으려면 `idx.broken`을 봐야 하고, 그것은 전수 판독을
# 강제한다. §4-1이 없앤 6.4초를 관문 하나에 도로 내는 셈이다.
#
# 대신 알림에 기댄다. `overview`가 이미 `broken`을 싣고, 그 자리에서는 색인이
# 이미 파싱돼 있어 **무료**다. 노드를 쓰기 전에 불리므로 쓰는 세션은 본다. 검색만 하는 세션은 모른다(#200).
#
# 관문을 되살리고 싶어지면 조건은 하나다: **전수 판독이 이미 일어난 자리**에
# 두어야 한다. 쓰기 통로는 그런 자리가 아니다.


def edge_key(vault: Vault, target: str, idx) -> tuple[str, str]:
    """노드는 해석된 제목과 절 앵커, 비노드 근거는 전체 경로와 앵커로 구별한다.
    id·제목·경로 어느 표기든 같은 노드의 같은 범위면 같은 키다. 해석되지 않는 이름만 표기
    규칙(`contract.target_stem`)으로 접는다 — 제목에 든 `.md`를 떼지 않기 위해서다."""
    s = raw.canonical_ref(vault, target).strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2]
    path, sep, anchor = s.split("|", 1)[0].strip().partition("#")
    path = path.strip().replace("\\", "/")
    if "/" in path or sep:
        kind = idx.resolve(path)[0]
        if kind == "external":
            # URL은 확장자까지 주소다 — `…/Makefile`과 `…/Makefile.md`는 다른 근거다.
            # `.md` 생략은 vault 안 경로의 표기 규칙이다(`resolve`가 `p`와 `p.md`를 본다).
            return path, anchor
        if kind != "node":
            return path.removesuffix(".md"), anchor
    hit = idx.locate(path) if path else None
    if hit and placement.is_node_home(hit[1]):
        return hit[0].stem, anchor.strip()
    return contract.target_stem(path), anchor.strip()


def _id_title(s: str, idx) -> str | None:
    """id 맨값이 가리키는 노드의 제목. 해석되지 않거나 모호하면 None."""
    if re.match(ID_RE, s) and s not in idx.dup_ids and s in idx.by_id:
        return idx.by_id[s][0].stem
    return None


def _titled(t, idx):
    """새로 받은 노드 대상의 id 맨값을 제목 위키링크로 적는다(Mechanism §8 2항).
    해석되지 않거나 모호한 id는 받은 그대로 둔다 — dangling·모호로 보고된다."""
    title = _id_title(str(t).strip(), idx)
    return f"[[{title}]]" if title else t


def _merge_edges(vault: Vault, cur: list[str], add, idx) -> list[str]:
    """저장 목록 `cur` 뒤에 `add` 가운데 아직 없는 근거(`edge_key`)만 잇는다.

    엣지를 더하는 자리는 모두 이 한 벌을 지난다 — 생성, 갱신의 추가, 그리고 그 둘을
    부르는 증류. 구판은 갱신의 추가만 새 대상을 걸렀다. 그래서 생성은 받은 만큼
    적었다(distill의 `sources`와 `edges`가 같은 라운드를 줄 때). 같은 기록의
    **다른 라운드**는 앵커가 달라 다른 근거로 남는다.

    `cur`는 키로 줄이지 않는다. 키는 "이미 있는가"를 묻는 데만 쓴다. 저장 목록을
    키로 줄이면 해석 규칙이 달라진 자리마다 저장된 근거를 조용히
    지우게 된다. 저장 목록의 되풀이는 바이트가 같을 때만 접는다(`update_node_locked`)."""
    seen = {edge_key(vault, t, idx) for t in cur}
    out = list(cur)
    for t in as_list(add):
        t = _titled(t, idx)
        k = edge_key(vault, t, idx)
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _as_links(vault: Vault, pred: str, targets, *, legacy_raw: bool = False) -> str | list:
    """입력 대상을 저장 표기로 접는다 — 맨값은 위키링크로 감싸고, 이미
    위키링크면 그대로 둔다. raw 좌표와 `derived-from`의 id는 감싸지 않는다.

    v3.7.3부터 노드 근거의 정본 표기는 **제목 위키링크**이므로, 제목을 맨값으로
    받으면 여기서 `[[제목]]`이 된다. id 맨값을 그대로 두는 것은 **구형 표기의
    호환**이다(§8 2항 — 계속 해석한다).

    이 함수는 id를 제목으로 바꾸지 않는다. `add_edges`·`remove_edges` 경로에서
    **이미 저장된 간선까지** 다시 접는 자리라, 바꾸면 호출자가 요청하지 않은
    간선이 바뀐다. 새로 받은 id는 `_merge_edges`가 `_titled`로 먼저 제목으로
    적는다."""
    out = []
    for t in as_list(targets):
        s = str(t).strip()
        if (not legacy_raw and pred == "derived-from" and placement.is_record_ref(s)
                and not re.match(r"^(?:\[\[\s*)?https?://", s)):
            out.append(raw.canonical_ref(vault, s))
        elif s.startswith("[[") or (pred == "derived-from" and re.match(ID_RE, s)):
            out.append(s)
        else:
            out.append(f"[[{s}]]")
    return out[0] if len(out) == 1 else out


def update_node(vault: Vault, name: str, body: str | None = None,
                expect_hash: str | None = None, summary: str | None = None,
                add_edges: dict | None = None, remove_edges: dict | None = None,
                old_text: str | None = None, new_text: str | None = None,
                settle: str | None = None, rechecked: list | None = None, *,
                _seen: dict | None = None) -> dict:
    """Apply an ordinary node update under the shared mutation lock.

    `rechecked` names the `derived-from` targets whose recheck this call completes.
    `_seen` is the surface's record of the bodies its caller read (node id →
    `rechecks.state`); a recorded read must still match for a recheck to close
    (Mechanism §4-1)."""
    with mutation_lock(vault):
        return update_node_locked(vault, name, body, expect_hash, summary, add_edges,
                                   remove_edges, old_text, new_text, settle, rechecked,
                                   _seen=_seen)


def update_node_locked(vault: Vault, name: str, body: str | None = None,
                expect_hash: str | None = None, summary: str | None = None,
                add_edges: dict | None = None,
                remove_edges: dict | None = None,
                old_text: str | None = None,
                new_text: str | None = None, settle: str | None = None,
                rechecked: list | None = None, *, _before_write=None,
                _stamp=None, _legacy_raw=False, _seen=None, _idx=None) -> dict:
    """본문·summary·엣지 수정. 엣지는 **델타**이므로 서버가 잠금 안에서 현재
    상태에 적용한다 — 낡은 읽기가 앞선 갱신을 덮는 일이 구조적으로 없다.

    `old_text`/`new_text`는 본문의 **앵커 편집**이며 같은 규율을 본문으로
    넓힌 것이다. 앵커는 "고칠 자리를 봤다"는 증거이므로 "전체를 봤다"는
    증거(`expect_hash`)를 요구하지 않는다 — §6-2 4항이 부분 변경을 그렇게
    갈라 둔다. 전문 치환(`body`)은 그대로 해시를 요구한다.

    실측이 이 경로를 부른 이유: 본문을 실은 `update_node` 78건이 그 도구
    발화 비용의 85%를 썼는데 연속 재작성 간 유사도 중앙값이 0.84였다 —
    국소 편집에 전문을 다시 뱉고 있었다. 하네스 `Edit`의 실측 대조에서
    부분 편집은 전문 치환의 0.119배이고 실패율은 오히려 낮다(2.2%)."""
    # 인자 검사는 잠금 **밖**에서 — 형태가 틀린 요청이 잠금을 잡을 이유가 없다.
    if (old_text is None) != (new_text is None):
        raise WriteError(
            "앵커 편집은 `old_text`와 `new_text`를 함께 받는다 — 쓰지 않았다",
            ["지울 때는 `new_text`에 빈 문자열을 준다. 한쪽만으로는 무엇을 "
             "무엇으로 바꾸는지 정해지지 않는다."])
    if old_text is not None:
        if body is not None:
            raise WriteError(
                "`body`와 앵커 편집은 함께 쓸 수 없다 — 쓰지 않았다",
                ["`body`는 전문 치환이고 앵커는 부분 편집이다. 둘을 함께 주면 "
                 "어느 쪽이 이겼는지 응답으로 구분되지 않는다. 하나만 보내라."])
        # 앵커도 **파서가 읽을 형태**로 접는다 — 본문은 CRLF 없이 저장되므로
        # (`norm_body`), CRLF 앵커를 그대로 대조하면 호출자가 방금 넣은 문장도
        # 못 찾는다. 접는 자리를 한 벌로 두어 대조와 치환이 갈리지 않게 한다.
        old_text = norm_newlines(old_text)
        new_text = norm_newlines(new_text or "")
        if not old_text:
            raise WriteError(
                "`old_text`가 비어 있다 — 쓰지 않았다",
                ["빈 앵커는 본문의 모든 자리에 맞으므로 고칠 자리를 가리키지 "
                 "못한다. 바꿀 대목을 그대로 넣어라."])
        if old_text == new_text:
            raise WriteError(
                "`old_text`와 `new_text`가 같다 — 쓰지 않았다",
                ["바뀌는 것이 없다. 고칠 내용을 `new_text`에 담아라."])
    # 이 쓰기가 쓰는 색인은 하나다. 같은 잠금 안의 허브 배선은 앞 쓰기의 색인을 넘긴다.
    idx = graph.Index(vault) if _idx is None else _idx
    nodefile.require_complete(idx)
    if settle is not None:
        evictions.require_evict(vault, settle)
    errs = node_checks.check_edges(vault, add_edges, idx) + node_checks.check_edges(vault, remove_edges, idx, adding=False)
    if errs:
        raise WriteError("계약 위반 — 쓰지 않았다", errs)
    path = nodefile.live_locate(vault, name, idx)
    if path is None or not path.is_file():
        raise WriteError(f"노드 없음: {name}")
    kind = placement.space_of(vault, path)
    cluster_gate.reject_governance(kind)
    if settle is not None:
        evictions.require_target(path.stem, idx)
    try:
        n = contract.parse(path)
    except (OSError, ValueError) as e:
        raise WriteError(f"파손된 노드다 — 수동 확인이 먼저다: {name} ({e})")

    node_checks.cas(path, expect_hash, body is not None)   # 위반 시 raise
    # 근거 재검토(Mechanism §4-1) — 쓰기 전의 쌍과 완료 상태, 이 호출이 `rechecked`에 적은 근거.
    # 잴 수 있는 쌍이 아닌 항목은 쓰기를 막지 않고 응답에 비춘다(헌법 1조 3항).
    _baseline(vault, idx)
    rc_before = rechecks.pairs(idx, n.meta)
    rc_pre = rechecks.state(path.read_bytes())  # 검토자가 읽었어야 할 노드의 본문
    rc_prior = rechecks.complete_keys(vault, idx, path, n.meta) if rc_before else set()
    rc_again, rc_stray = set(), []
    for ref in as_list(rechecked):
        t = rechecks.target(str(ref), idx)
        if t and t[0] in rc_before and t[1] is not None:
            rc_again.add(t[0])
        else:
            rc_stray.append(str(ref))
    rc_note = {"recheck_unmatched": {"targets": rc_stray, "why": (
        "이 노드의 derived-from에 상태를 잴 수 있는 근거로 없다 — 닫을 쌍이 없다")}} if rc_stray else {}
    if old_text is not None:
        # **유일성이 안전 계약의 전부다.** 여러 곳에 맞으면 어디를 고칠지
        # 호출자가 정한 바가 없고, 아무 곳이나 고르는 것은 조용히 틀린
        # 자리를 고치는 길이다. 하네스 `Edit` 실측에서 유일성 위반은
        # 5,454건 중 5건(0.09%)이라 이 계약은 실무에서 거의 공짜다.
        hits, windows = nodefile.anchor_windows(n.body, old_text)
        if hits == 0 and (spaced := space_numeric_tags(old_text)) != old_text:
            # 저장본은 태그 방어로 `#숫자` 뒤를 띄웠다(`norm_body`). 호출자가 방금 쓴 글 그대로의
            # 앵커도 같은 변환으로 맞춘다(#292). 원문부터 보는 것은 조각이 코드 구획 한가운데서
            # 시작하면 변환의 구획 판정이 틀릴 수 있어서다.
            old_text = spaced
            hits, windows = nodefile.anchor_windows(n.body, spaced)
        if hits != 1:
            raise WriteError(
                ("앵커가 본문에 없다 — 쓰지 않았다" if hits == 0 else
                 f"앵커가 본문에 {hits}곳 맞는다 — 쓰지 않았다"),
                ([f"`old_text`가 `{name}`의 본문에 나오지 않는다. 노드가 "
                  f"그 사이 바뀌었을 수 있다 — `read_node`로 지금 본문을 "
                  f"보고 그대로 복사해 넣어라. 공백·줄바꿈까지 일치해야 한다. "
                  f"큰 노드는 `read_node(view=고칠 문장)`이 그 자리를 앞뒤 문단째 준다."]
                 if hits == 0 else
                 [f"어느 자리를 고칠지 정해지지 않는다. 앞뒤 줄을 함께 "
                  f"넣어 앵커를 **유일하게** 만들어라. 맞는 자리의 앞뒤 문단은 "
                  f"`views`를 `read_node(view=…)`로 읽는다."]),
                **({"views": [f"{a}:{b}" for a, b in windows]} if hits else {}))
        body = n.body.replace(old_text, new_text, 1)
    meta = dict(n.meta)
    replaced_summary = None
    changed = False
    if summary is not None and str(meta.get("summary")) != summary:
        replaced_summary = str(meta.get("summary"))
        meta["summary"] = summary
        changed = True
    # 두 루프 모두 **누적된 `meta`**에서 현재값을 읽는다. 구판은 각자
    # `n.edges(pred)`로 **원본**을 다시 읽었고, 그래서 같은 술어에 add와
    # remove를 함께 주면 remove가 "add가 없었던 것처럼" 계산한 값으로
    # meta를 덮었다 — 원본에 지울 것 하나뿐이었으면 술어를 통째로 `pop`해
    # **방금 추가한 근거까지 사라졌다.** 그러면서 `ok: true`를 냈다.
    #
    # "근거를 A에서 B로 바꾼다"는 드문 호출이 아니라 이관·오타 수정·근거
    # 갱신의 자연스러운 표현이다. 실제로 v3.7.3 이관에서 두 번 걸렸고,
    # 두 번째는 이 결함을 재현해 기록한 직후였다 — 알고도 피해지지 않았다.
    #
    # 제거가 먼저다. id와 제목은 같은 근거이므로(`edge_key`), 저장된 id 표기를
    # 빼고 제목으로 다시 대는 호출이 뒤의 제거에 지워지지 않는다. 같은 근거를
    # 함께 빼고 대면 남는다 — 근거를 조용히 잃지 않는다.
    for pred, tg in (remove_edges or {}).items():
        drop = {edge_key(vault, t, idx) for t in as_list(tg)}
        cur = stored_edges(meta.get(pred))
        keep = [t for t in cur if edge_key(vault, t, idx) not in drop]
        if len(keep) != len(cur):
            changed = True
            if keep:
                meta[pred] = _as_links(vault, pred, keep, legacy_raw=_legacy_raw)
            else:
                meta.pop(pred, None)
    for pred, tg in (add_edges or {}).items():
        cur = stored_edges(meta.get(pred))            # 저장 표기 그대로
        merged = _merge_edges(vault, cur, tg, idx)  # 한 호출 안의 중복도 한 번만 앉는다
        if len(merged) != len(cur):
            meta[pred] = _as_links(vault, pred, merged, legacy_raw=_legacy_raw)
            changed = True
    # 저장 목록에서 바이트가 같은 되풀이는 이 쓰기에서 접는다 — 위의 추가·제거가 옛
    # 표기를 정규 표기로 다시 적어 겹친 것까지. 접기만으로는 쓰지 않는다(`changed` 불변).
    # ponytail: 바이트 비교만 한다(해소가 없어 공짜이고 실패하지 않는다). 키로 접으면
    # raw 근거마다 해소 비용(Windows 실측 ~7 ms)을 내고, 키가 거친 자리에서 서로 다른
    # 근거를 지운다.
    for pred in contract.PREDICATES:
        cur = stored_edges(meta.get(pred))
        if len(set(cur)) != len(cur):
            meta[pred] = list(dict.fromkeys(cur))
    new_body = n.body if body is None else body
    if body is not None and norm_body(body) != norm_body(n.body):
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
    if not changed and settle is None:
        # 변경이 없으면 쓰지 않는다 — 내용이 그대로인데 updated만
        # 갱신하면 "상태가 변경될 때 갱신한다"(시행령 §1 4항)에 어긋난다
        res = {"ok": True, "no_change": True, "name": name,
               "path": posix_rel(path, vault.root), "id": n.id,
               "new_hash": sha256_file(path),
               "edges": node_checks.edge_report(n),
               **node_checks.reference_feedback(vault, path, n.meta, n.body, idx, n), **rc_note}
        if rc_again:     # 바꿀 것 없이 점검을 적었다 — 점검 완료(unchanged)
            res.update(rechecks.after_write(vault, idx, path, n.meta, before=rc_before.keys(),
                                            prior=rc_prior, rechecked=rc_again,
                                            changed=False, pre=rc_pre, seen=_seen))
        return res
    if not only_conflicts:
        meta["updated"] = _stamp or now_kst()

    data, errs = node_checks.validate_render(vault, path, meta, new_body, idx)
    if errs:
        raise WriteError("계약·위상 위반 — 쓰지 않았다", errs)
    if settle is not None:
        saved_body = norm_body(contract.parse_bytes(path, data).body)
        if not saved_body or saved_body == norm_body(n.body):
            raise WriteError("보존할 본문이 없거나 변경되지 않았다 — 쓰지도 처분하지도 않았다", [
                "settle은 이 호출이 보존한 evict의 rid다. 이미 노드에 보존된 내용이면 "
                "본문을 확인한 뒤 CLI tidy settle로 기록하라."])
    if _before_write is not None:
        _before_write(path, data)
    nodefile.write_file(path, data)
    out = {"ok": True, "name": name, "path": posix_rel(path, vault.root),
           "id": n.id, "new_hash": sha256_bytes(data),
           "updated_kept": only_conflicts,
           "edges": node_checks.edge_report(contract.Node(path=path, meta=meta, body=new_body)),
           **node_checks.reference_feedback(vault, path, meta, new_body, idx, n), **rc_note}
    out.update(rechecks.after_write(vault, idx, path, meta, before=rc_before.keys(),
                                    prior=rc_prior, rechecked=rc_again,
                                    pre=rc_pre, seen=_seen))
    # 쓰기 직전 본문을 읽었던 호출자는 방금 쓴 본문도 안다 — 쓰기 응답의 해시를 다음
    # `expect_hash`로 잇는 것과 같은 규율이다. 읽지 않았으면 잇지 않는다.
    if _seen is not None and _seen.get(n.id) == rc_pre:
        _seen[n.id] = rechecks.state(data)
    if old_text is None and body is not None:
        # 전문 치환은 긴 글을 다시 쓰다 흘릴 수 있다. 무엇이 빠졌는지 비추면 선의의 에이전트는
        # 스스로 되살린다(헌법 1조 3항). 앵커 편집은 지울 글을 호출자가 `old_text`에 직접
        # 적었으므로 비출 것이 없다.
        dropped = _dropped(n.body, norm_body(new_body))
        if dropped:
            out["dropped"] = dropped
    if replaced_summary is not None:
        out["replaced_summary"] = replaced_summary
    return evictions.after_node_write(vault, out, settle, "merged", path.stem)


def _dropped(old_body: str, new_body: str) -> list[str]:
    """옛 본문의 단위(문단·목록 항목·제목) 가운데 새 본문에 글자 그대로 없는 것.
    고쳐 쓴 단위도 옛 판이 나온다 — 고친 것인지 흘린 것인지는 읽는 쪽이 가른다.
    많으면 앞의 10개를 300자까지만 싣고 나머지는 개수로 알린다."""
    gone = [t for t in prose.segment_texts(old_body) if t not in new_body]
    shown = [t if len(t) <= 300 else t[:299] + "…" for t in gone[:10]]
    return shown + ([f"… 외 {len(gone) - 10}개"] if len(gone) > 10 else [])


def record_candidate(vault: Vault, type: str, nodes: list[str], reason: str = "") -> dict:
    """충돌 후보 상정 (헌법 12조 2항). 같은 근거(basis)의 후보·각하가 이미
    있으면 append하지 않고 기존 기록을 돌려준다 — 12조 2항 후단의 중복 금지와
    12조 3항의 각하 억제가 여기서 함께 닫힌다. 각하는 사용자 전속이다."""
    with mutation_lock(vault):
        if type not in CANDIDATE_TYPES:
            raise WriteError(f"미정의 충돌 유형: {type} (Mechanism §4 3항)")
        idx = graph.Index(vault)
        nodefile.require_complete(idx)
        parties = []
        for nm in nodes:
            # `live_locate`를 쓴다 — 동명·동 id면 **고르지 않고 거부**한다
            # (Mechanism §2 1항). 구판은 `idx.nodes.get`으로 뒤에 오는 쪽을
            # 조용히 골라, 쓰기 통로가 거부하는 모호성을 이 통로가 사건부에
            # 박제했다. id 핸들도 여기서 함께 해석된다.
            p = nodefile.live_locate(vault, nm, idx)
            if p is None or not p.is_file():
                raise WriteError(f"당사자 노드 없음: {nm} — 근거가 성립하지 않는다")
            try:
                parties.append((idx.node(p).id, sha256_file(p)))
            except (OSError, ValueError) as e:
                raise WriteError(f"당사자 판독 실패: {nm} ({e})")
        # 같은 노드를 제목과 id로 두 번 줘도 근거 키는 하나여야 한다 — 안 그러면
        # 같은 쌍의 후보가 다른 `basis`로 두 번 앉아 중복 억제가 빗나간다.
        parties = sorted(set(parties))
        if len({i for i, _ in parties}) < 2:
            raise WriteError(
                "충돌은 **서로 다른** 둘 이상의 당사자를 요한다 — 자기 자신과의"
                " 충돌은 성립하지 않는다")
        basis = _basis(type, parties)
        recs = ledger_read(vault.candidates)
        for r in recs:
            if r.get("basis") == basis and r.get("basis_version") == 1:
                return {"ok": True, "deduped": True, "rid": r.get("rid"),
                        "kind": r.get("kind"), "basis": basis,
                        "note": "같은 근거의 기록이 이미 있다 (헌법 12조 2·3항)"}
        rec = ledger_append(vault.candidates, {
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
