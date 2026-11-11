"""osk.node_moves — 노드와 군집의 이동, 경유 노드의 폐기.

이동은 파일의 자리만 바꾼다 — id와 본문은 그대로다. 계획을 세워 한꺼번에 옮기고, 보호영역의 이동
기록과 허브의 Link를 함께 맞춘다. `write`와 같은 전역 변경 잠금 안에서 돈다."""
from __future__ import annotations
import os
from pathlib import Path
from .core import local_lock_path, mutation_lock, posix_rel, resolve_in_root, sha256_file
from . import approvals, cluster_gate, contract, evidence, graph, node_checks, nodefile, rechecks, scope_memory
from .errors import WriteError
from .naming import name_collision, portable_name_key
from .vault import Vault


def _plan_move(vault: Vault, name: str, dest_dir: Path, dest_space: str, idx):
    """이동 하나의 **검사만** 수행하고 (원본, 목표, 노드)를 낸다 — 아무것도
    쓰지 않는다.

    `move_nodes`가 전부-아니면-전무이려면 **검사를 전부 먼저** 돌려야 한다.
    검사와 실행이 한 덩이면 절반 옮긴 상태가 남고, 그 상태는 허브 배선이
    어중간해 사람이 무엇을 되돌릴지 알기 어렵다."""
    path = nodefile.live_locate(vault, name, idx)
    if path is None or not path.is_file():
        # 결과를 함께 말한다 — "전부 아니면 전무"를 문안으로만 약속하면 받는
        # 쪽이 못 믿고 확인 질의를 한 번 더 한다(표면 감사 실측).
        raise WriteError(f"노드 없음: {name} — 아무것도 옮기지 않았다")
    cluster_gate.reject_governance(graph.space_of(vault, path))
    # 허브 노드는 이름이 군집에 결박한다 (시행령 §3 6항) — 밖으로 나가면
    # 군집이 허브를 잃고, 도착지에는 남의 군집 이름을 단 노드가 앉는다.
    # 하위 허브도 같은 식으로 걸린다(`graph.is_hub`) — 하위 군집의 재편은
    # 허브만 옮기는 일이 아니라 폴더째 옮기는 일이므로 `move_cluster`가 맡는다.
    if graph.is_hub(path):
        raise WriteError(
            "허브 노드는 군집 밖으로 이동할 수 없다 — 옮기지 않았다",
            [f"`{name}`은 `= …/{path.parent.name}` 군집의 동명 허브 노드다"
             f"(시행령 §3 6항). 군집째 옮기려면 `move_cluster`를 쓴다"])
    target = dest_dir / path.name
    dst_kind = graph.space_of(vault, target)   # 소속은 노드 파일 경로로 판정한다
    cluster_gate.require_node_cluster(dst_kind, dest_space)
    clash = name_collision(dest_dir, path.stem)
    if clash is not None:
        raise WriteError(
            f"목적지에 이미 있는 이름이다: {clash} — 대소문자나 유니코드 "
            f"정규화만 달라도 NTFS·APFS에서 같은 경로가 된다")
    try:
        n = contract.parse(path)
    except Exception as e:
        raise WriteError(f"파손된 노드다 — 수동 확인이 먼저다: {name} ({e})")
    # pin은 **군집 경로 또는 노드 id**를 대상으로 한다(Mechanism §6 1항 ·
    # 시행령 §3 4항 "필요하면 개별 노드의 배치에도 붙일 수 있다"). 구판은
    # 디렉토리만 대조해 노드 pin이 이동에서 조용히 무시됐다. 판독을 위로
    # 옮겨 id를 손에 쥔 뒤 함께 본다.
    if cluster_gate.pinned(vault, posix_rel(path.parent, vault.root) + "/",
               posix_rel(dest_dir, vault.root) + "/", n.id):
        raise WriteError(
            "pin으로 고정됐거나 pin 판정이 미확정이다 — 자동 재배정에서 제외된다 "
            "(시행령 §3 4항). 사용자 발의로만 옮긴다")
    return path, target, n


def _apply_move(vault: Vault, path: Path, target: Path, n, idx) -> None:
    """검사를 통과한 이동 하나를 수행한다. 바이트 불변 — `updated` 갱신 없음."""
    # 마지막 방어선 — 목적지가 이미 차 있으면 덮지 않는다. 계획 단계의 검사는
    # 계획을 세울 때의 파일시스템을 보므로, **이 묶음의 앞선 이동이 방금 만든**
    # 파일은 보지 못한다. 대소문자를 접는 파일시스템에서 `Foo.md`가 놓인 자리에
    # `foo.md`를 rename하면 `os.replace`가 조용히 덮는다.
    if target.exists():
        raise WriteError(
            f"목적지가 이미 차 있다: {posix_rel(target, vault.root)} — 덮지 않았다 "
            f"(대소문자·정규화만 다른 이름이 같은 경로가 되는 파일시스템이다)")
    # 이동을 이동으로 기록한다(시행령 §6 4항) — 기록이 없으면 반려가 이동을
    # 추가+삭제로 보아 노드를 지우거나 복제한다. 실패한 이동의 잔행은
    # 무해하므로(그 자리에 그 id가 없으면 안 쓰인다) 이동 **전에** 적는다.
    approvals.record_move(vault, n.id, path, target)
    os.replace(path, target)
    # 손에 든 색인은 이동 **전** 경로를 쥐고 있다. 갱신하지 않으면 아래
    # `node_checks.dangling_of`가 옛 경로를 열려다 실패해 이 노드의 자기링크를
    # dangling으로 오보한다 — `create_node`가 `register_new`로 막는 것과
    # 같은 실패이고, 오타가 아닌 것을 오타라고 알리게 된다.
    idx.retarget(path, target, graph.space_of(vault, target))


def _hub_links(srcs: set, dest: Path, moved: set, idx) -> list:
    """이동으로 손봐야 할 허브를 **양쪽 다** 보고한다 — 뺄 것(`remove`)과
    더할 것(`add`).

    도달은 본문 Link로만 성립하므로(v3.6.0), 노드가 다른 군집으로 가면 출발지
    허브의 Link는 죽은 줄이 되고 **도착지 허브에는 그 노드로 가는 줄이 없다.**
    기계가 대신 고칠 수 없다 — 항해는 큐레이션이라 어느 갈래에 걸지가
    판단이다(시행령 §3 7항). 그래서 고치지 않고 알린다.

    구판은 **출발지만** 알렸다. 표면 감사에서 감사자가 그 안내대로 출발지
    링크를 지웠더니 그 노드가 도착지에서 고아가 됐고, 허브 검사가 비활성이라
    `verdict`는 PASS였다 — **안내를 따르면 vault가 나빠지는데 아무도 말해
    주지 않았다.** 절반만 아는 안내는 안 하느니만 못하다.

    허브의 손잡이는 **제목**으로 낸다 — 이 표면의 다른 모든 손잡이가 제목인데
    여기만 경로를 내면 호출자가 변환을 스스로 해야 한다(감사 지적)."""
    out = []
    for h in sorted(srcs):
        hub = graph.hub_file(h)
        if hub is None:
            continue
        try:
            n = idx.node(hub)
        except Exception:
            continue
        gone = sorted({contract.target_stem(t) for t in n.wikilinks()} & moved)
        if gone:
            out.append({"hub": hub.stem, "remove": gone})
    dhub = graph.hub_file(dest)
    if dhub:
        try:
            have = {contract.target_stem(t) for t in idx.node(dhub).wikilinks()}
            need = sorted(moved - have)
        except Exception:
            need = sorted(moved)
        if need:
            out.append({"hub": dhub.stem, "add": need})
    return out


def _move_topology(vault: Vault, plans, stale, idx) -> list[str]:
    """이동 **후**의 참조 위상 중 이 이동이 **새로 만드는** 위반.

    소속은 노드 파일의 경로로 정해지므로(`space_of`) 옮긴 노드에서 나가는
    참조와 그 노드로 들어오는 참조의 판정이 함께 바뀐다. 구판은 이것을 보지
    않고 `ok`를 냈다 — 검증기는 FAIL(scope 간 Link·derived-from, Domain의
    `_raw` 근거)이었고 그 노드들의 무관한 다음 쓰기가 거부됐다. 같은 간선을
    `create_node`·`update_node`는 처음부터 거부한다(2026-09-24 재현).

    이미 있던 위반은 이 이동의 몫이 아니므로 세지 않는다(설계 D10 — 남이 만든
    위반 때문에 내 쓰기가 막히면 안 된다). 출발지 허브의 Link는 `hub_links`의
    `remove`가 닫으므로 뺀다. 들어오는 참조의 후보는 바이트에서 고르고
    (`mentioning`), 판정은 판독한 노드로 한다 — 전수 판독을 부르지 않는다."""
    after = {src: graph.space_of(vault, dst) for src, dst, _n in plans}
    moved = {src: n for src, _dst, n in plans if after[src] != graph.space_of(vault, src)}
    if not moved:
        return []                     # 소속이 그대로면 판정도 그대로다
    new_kind = {}
    for src, n in moved.items():
        new_kind[src.stem] = after[src]
        if n.id:
            new_kind[n.id] = after[src]

    gone = {Path(os.path.realpath(src)) for src in after}

    class _After:                     # `node_checks.topology_of`가 색인에 묻는 것은 해석뿐이다
        @staticmethod
        def resolve(name):
            r = idx.resolve(name)
            if r[0] != "node":
                return r
            if "/" in name:
                # 경로형은 옛 자리를 가리킨다 — 이동 뒤에는 끊긴 참조(경고)다.
                # 옛 소속으로 판정하면 함께 옮기는 두 노드 사이의 경로형 Link가
                # scope 간 위반으로 오판돼 이동이 거부됐다.
                p = resolve_in_root(vault, name)
                return ("dangling",) if p is not None and {p, p.with_suffix(".md")} & gone else r
            return ("node", new_kind[name]) if name in new_kind else r

    hub_keep = {h["hub"]: set(h["remove"]) for h in stale if "remove" in h}
    out = []
    for p in dict.fromkeys([*moved, *idx.mentioning(new_kind)]):
        if not idx._readable(p):
            continue                  # 판독 불가 파일은 참조를 말하지 않는다
        n = idx.parsed[p]
        k0 = graph.space_of(vault, p)
        k1 = after.get(p, k0)
        refs = [(t, pred) for pred in contract.PREDICATES for t in n.edges(pred)]
        refs += [(t, None) for t in n.wikilinks()]
        refs += [(t, rechecks.EVIDENCE) for t in evidence.coordinates(n.body)]
        for t, pred in refs:
            if pred is None and contract.target_stem(t) in hub_keep.get(p.stem, ()):
                continue
            errs = node_checks.topology_of(vault, _After, k1, p.stem, t, pred, n.id)
            if errs and not node_checks.topology_of(vault, idx, k0, p.stem, t, pred, n.id):
                # 쓰기용 꼬리("옮기는 중이면 먼저 옮기고…")는 여기서 거꾸로 읽힌다
                out += [e.split(" — ", 1)[0] for e in errs]
    return list(dict.fromkeys(out))   # Link와 derived-from이 같은 줄을 낸다


def move_node(vault: Vault, name: str, dest_space: str) -> dict:
    """군집 재배정. 이동은 바이트 불변이라 CAS가 없다(경로는 상태, 동일성은
    id). pin된 군집은 출발·도착 어느 쪽이든 거부한다(시행령 §3 4항)."""
    r = move_nodes(vault, [name], dest_space)
    if not r["ok"]:
        return r
    one = r["moved"][0]
    return {"ok": True, "name": one["name"], "id": one["id"],
            "path": one["path"], "new_hash": one["new_hash"],
            "moved_from": one["moved_from"], "dangling": one["dangling"],
            "hub_links": r["hub_links"]}


def move_nodes(vault: Vault, names: list[str], dest_space: str) -> dict:
    """노드 여럿을 한 군집으로 옮긴다 — 검사 실패는 전부 거부, I/O 중단은 이어서 복구.

    분화(시행령 §3 7항)의 실제 모양이 이것이다: 한 갈래를 통째로 하위 군집에
    내린다. 하나씩 부르면 잠금과 색인을 N번 짓고(v3.7.0이 세운 "쓰기 1회 =
    색인 1회"의 반대), 중간에 하나가 거부되면 절반만 내려간 상태가 남는다.

    검사를 **전부 먼저** 돌리고 하나라도 걸리면 아무것도 쓰지 않는다. 그 뒤의
    실행은 개별 `os.replace`라 원자적이지만 묶음은 아니다 — 실행 중 OSError는
    이미 옮긴 것을 되돌리지 않고 그대로 보고한다(자료는 어느 쪽에도 남아 있고,
    이동은 바이트 불변이라 되돌리기가 다시 옮기는 일이다)."""
    if not names:
        raise WriteError("옮길 노드가 없다")
    with mutation_lock(vault):
        idx = graph.Index(vault)                     # 이 이동이 쓰는 색인은 하나다
        nodefile.require_complete(idx)
        dest_dir = resolve_in_root(vault, dest_space)
        if dest_dir is None or not dest_dir.is_dir():
            dest_dir = cluster_gate.new_cluster_gate(vault, dest_space, dest_dir, "이 이동이")
        # 중복 판정은 **해석된 경로**로 한다. 이름 문자열로만 보면 같은 노드를
        # 제목과 id로 두 번 줬을 때 통과해, 첫 rename 뒤 둘째가 부재로 죽는다 —
        # 이미 옮긴 것은 남으므로 "전부 아니면 전무"가 깨진다.
        #
        # 그리고 **이식성 키**로도 본다. 대소문자·정규화만 다른 두 노드를 한
        # 군집으로 옮기면 목적지 검사(`_plan_move`의 `name_collision`)는 아직
        # 그 파일이 없으니 통과시키고, `os.replace`가 둘째로 첫째를 덮는다 —
        # 노드 하나가 소실되는데 응답은 `ok:true`였다(실측 재현).
        seen, seen_keys, plans = set(), {}, []
        for name in names:
            plan = _plan_move(vault, name, dest_dir, dest_space, idx)
            src = plan[0]
            if src in seen:
                raise WriteError(f"같은 노드를 두 번 옮길 수 없다: {name}")
            seen.add(src)
            key = portable_name_key(src.stem)
            if key in seen_keys:
                raise WriteError(
                    f"한 번에 옮길 수 없는 이름 충돌이다: {seen_keys[key]} / "
                    f"{src.stem} — 대소문자나 유니코드 정규화만 다른 이름은 "
                    f"NTFS·APFS에서 같은 경로가 되어 한쪽이 덮인다. 아무것도 "
                    f"옮기지 않았다")
            seen_keys[key] = src.stem
            plans.append(plan)
        srcs = {p.parent for p, _t, _n in plans}
        moved_stems = {p.stem for p, _t, _n in plans}
        stale = _hub_links(srcs - {dest_dir}, dest_dir, moved_stems, idx)
        # 최상위 군집을 건너는 것 자체는 막지 않는다 — 노드 하나를 다른 scope로
        # 재배정하는 것은 정당한 행위다. 막는 것은 이동이 **새로 만드는** 참조
        # 위반이다(`_move_topology`). 출발지 허브의 Link만은 예외로 두고
        # `hub_links`의 `remove`로 닫는다 — 이동 전에 지우면 허브 검사가 고아를
        # 부르고, 이동 뒤 한 번의 편집으로 닫히는 것이 알려진 순서다.
        #
        # 건넜다는 사실은 침묵하지 않는다. 표면 감사에서 감사자가 이 이동을 하고
        # `ok:true`를 받은 뒤 검증기 FAIL로 알게 됐다.
        bad = _move_topology(vault, plans, stale, idx)
        if bad:
            raise WriteError(
                "옮기면 참조 위상이 깨진다 — 아무것도 옮기지 않았다", bad + [
                    "소속은 경로가 정하므로(헌법 8조 3항) 이 참조들은 이동 뒤 "
                    "위반이 되고, 그 노드들의 무관한 다음 쓰기까지 거부된다. "
                    "옮기기 전에 걷어내라(`update_node`의 `remove_edges`·"
                    "`old_text`). 함께 가야 할 노드면 같은 `names`에 넣는다. "
                    "scope를 넘어 공유할 지식이면 domain 노드로 증류해 경유한다"])
        dtop = dest_dir.relative_to(vault.root).parts[:2]
        crossed = sorted(p.stem for p, _t, _n in plans
                         if p.relative_to(vault.root).parts[:2] != dtop)
        out = []
        from . import organization
        move_key = organization.record_move(vault, plans, stale, idx)
        for path, target, n in plans:
            before = sha256_file(path)
            try:
                _apply_move(vault, path, target, n, idx)
            except (OSError, WriteError) as exc:
                return {"ok": False, "moved": out, "count": len(out),
                        "dest": posix_rel(dest_dir, vault.root), "hub_links": stale,
                        "move_key": move_key, "reason": str(exc),
                        "remaining": [q.stem for q, t, _n in plans if q.is_file()],
                        "instruction": "일부 이동 뒤 중단됐다. ID·현재 위치·hash를 읽고 remaining만 재개하라. "
                                       "허브 배선까지 organization 검토의 미결로 유지한다."}
            out.append({"name": path.stem, "id": n.id,
                        "path": posix_rel(target, vault.root), "new_hash": before,
                        "moved_from": posix_rel(path, vault.root),
                        "dangling": node_checks.dangling_of(vault, target, n.meta, n.body, idx)})
            organization.checkpoint_moves(vault, idx)
        r = {"ok": True, "moved": out, "count": len(out),
             "dest": posix_rel(dest_dir, vault.root), "hub_links": stale, "move_key": move_key}
        if crossed:
            r["crossed_scope"] = {
                "nodes": crossed,
                "why": "최상위 군집을 건넜다 — 출발지 허브가 이 노드들을 계속 "
                       "가리키면 scope 간 직접 참조가 되어 검증기가 막는다"
                       "(헌법 8조 3항). `hub_links`의 `remove`를 먼저 하라"}
        return r


def move_cluster(vault: Vault, name: str, dest_parent: str) -> dict:
    """하위 군집을 **폴더째** 옮긴다 — 재편(승격·강등·자리 바꾸기).

    `name`은 하위 군집의 이름(= 그 폴더명 = 그 허브의 이름)이고 `dest_parent`는
    새 부모 군집의 경로다. 폴더가 통째로 가므로 **그 아래의 하위 군집도 함께
    간다** — 재귀 처리가 따로 필요 없다.

    **같은 최상위 군집 안에서만** 옮긴다. `_space_of_parts`가 소속을 `parts[1]`
    로 정하므로 최상위를 건너면 그 안 노드 **전부의 scope가 한꺼번에 바뀌고**,
    그러면 기존 참조가 무더기로 헌법 8조 3항 위반이 된다. scope를 건너는 것은
    재편이 아니라 **증류**이며(헌법 8조 3항 — "scope 사이에 공유되는 지식은
    domain 노드로 증류하여 경유한다") 다른 문으로 간다.

    노드는 바이트가 안 바뀌고 소속도 안 바뀐다(최상위가 같으므로). 그래서
    위상 검사를 다시 돌릴 필요가 없다 — 기존 참조가 전부 그대로 유효하다.
    이동 대장은 노드마다 남긴다(시행령 §6 4항 — 반려가 이동을 추가+삭제로
    오독하지 않게)."""
    with mutation_lock(vault):
        idx = graph.Index(vault)
        nodefile.require_complete(idx)
        src = nodefile.live_locate(vault, name, idx)
        if src is None or not src.is_file():
            raise WriteError(f"노드 없음: {name}")
        if not graph.is_hub(src):
            raise WriteError(
                f"군집이 아니다: {name} — 군집의 허브(폴더와 동명인 노드)만 "
                f"군집째 옮길 수 있다. 노드 하나를 옮기려면 `move_nodes`를 쓴다")
        sdir = src.parent
        cluster_gate.reject_governance(graph.space_of(vault, src))
        ddir = resolve_in_root(vault, dest_parent)
        if ddir is None or not ddir.is_dir():
            raise WriteError(
                f"목적지 군집이 없다: {dest_parent} — 재편은 이미 있는 군집 "
                f"안으로만 한다(먼저 그 군집을 만든다)")
        cluster_gate.reject_governance(graph.space_of(vault, ddir / "x.md"))
        cluster_gate.require_node_cluster(graph.space_of(vault, ddir / sdir.name / "x.md"),
                              dest_parent)
        srel, drel = sdir.relative_to(vault.root).parts, ddir.relative_to(vault.root).parts
        if srel[:2] != drel[:2]:
            raise WriteError(
                f"최상위 군집을 건널 수 없다: {posix_rel(sdir, vault.root)} → "
                f"{dest_parent}",
                ["그 안 노드 전부의 소속이 한꺼번에 바뀌어 기존 참조가 무더기로 "
                 "위반이 된다(헌법 8조 3항). scope 사이의 지식 이동은 재편이 "
                 "아니라 **증류**다 — domain 노드로 증류하여 경유한다"])
        if ddir == sdir.parent:
            raise WriteError(f"이미 그 자리다: {posix_rel(sdir, vault.root)}")
        if sdir in ddir.parents or sdir == ddir:
            raise WriteError(
                f"자기 안으로 옮길 수 없다: {posix_rel(sdir, vault.root)} → {dest_parent}")
        if not graph.hub_file(ddir)\
                and ddir.parent.resolve() not in {
                    (vault.root / s).resolve() for s in graph.node_spaces(vault)}:
            raise WriteError(
                f"허브 없는 군집 안으로는 옮길 수 없다: {dest_parent}")
        target = ddir / sdir.name
        if target.exists():
            raise WriteError(
                f"목적지에 같은 이름의 군집이 이미 있다: {posix_rel(target, vault.root)} "
                f"— 하위 군집 이름은 전역에서 유일하다")
        inside = [(p, k) for p, k in idx.nodes.values() if sdir in p.parents]
        if cluster_gate.pinned(vault, posix_rel(sdir, vault.root), posix_rel(ddir, vault.root),
                   *(idx.node(p).id for p, _k in inside),
                   subtree=posix_rel(sdir, vault.root)):
            raise WriteError(
                "pin으로 고정됐거나 pin 판정이 미확정인 군집·노드가 있다 "
                "— 사용자 발의로만 옮긴다 (시행령 §3 4항)")
        for p, _k in inside:
            approvals.record_move(vault, idx.node(p).id, p,
                                  target / p.relative_to(sdir))
        os.replace(sdir, target)          # 같은 볼륨 — 원자적 rename
        for p, k in inside:
            idx.retarget(p, target / p.relative_to(sdir), k)
        return {"ok": True, "cluster": name,
                "path": posix_rel(target, vault.root),
                "moved_from": posix_rel(sdir, vault.root),
                "nodes": len(inside),
                "hub_links": _hub_links(
                    {sdir.parent}, ddir, {sdir.name}, idx)}


def discard_transit(vault: Vault, title: str, confirm: str | None = None) -> dict:
    """경유 노드의 폐기 — Workbench 계약 §3의 통합·폐기 출구를 실행한다.

    들어오는 참조(Link·Predicate Edge)가 남았거나 scope 기억이 그 노드를 가리키면
    거부한다 — 재배선·정정이 먼저다. `confirm` 없이 부르면 지울 판(해시)만 알린다.
    사용자에게 확인받은 그 해시로 다시 부를 때만 지운다 — 확인한 뒤 바뀐 판은 지우지
    않는다. 새 대장은 두지 않는다. 지우기 전 판은 이 기기에 사본으로 남긴다 — 동기화
    전의 판은 vault git 이력에 없을 수 있다."""
    with mutation_lock(vault):
        idx = graph.Index(vault)
        if not idx.complete:
            raise ValueError("vault를 전부 관측하지 못했다 — 폐기하지 않았다: "
                             + "; ".join(idx.scan_errors[:3]))
        hit = idx.locate(title)
        if not hit or hit[1][0] != "workbench-transit":
            raise ValueError(f"`{title}`는 Workbench 경유 노드가 아니다 — 폐기는 경유 노드의 "
                             f"출구다(Workbench 계약 §3)")
        path = hit[0]
        # 입력이 id·경로여도 실제 제목과 id로 찾는다 — 참조와 기억은 그 둘로 적힌다
        name, nid = path.stem, idx.node(path).id
        inbound = []
        for p in idx.mentioning([name, nid]):
            if p == path:
                continue
            try:
                n = idx.node(p)
            except Exception:
                continue          # 판독되지 않는 파일은 참조를 말하지 않는다
            # 그래프와 같은 해석(`Index.locate` — 제목·id·경로, 제목 속 `.md` 포함)으로 대조한다
            for _relation, text in n.references():
                found = idx.locate(text.split("#", 1)[0].strip())
                if found and graph.same_file(found[0], path):
                    inbound.append(p.stem)
                    break
        memory = sorted(f.stem for f in scope_memory.sm_dir(vault).glob("*.md")
                        if any(k in f.read_text(encoding="utf-8", errors="replace")
                               for k in (name, nid)))
        where = []
        if inbound:
            where.append("노드 " + ", ".join(sorted(inbound)))
        if memory:
            where.append("scope 기억 " + ", ".join(memory))
        if where:
            raise ValueError("폐기하지 않았다 — 이 노드를 가리키는 곳을 먼저 재배선·정정하라: "
                             + "; ".join(where))
        plan = {"title": name, "id": nid, "path": posix_rel(path, vault.root), "hash": sha256_file(path)}
        if confirm is None:
            return {"ok": False, "plan": plan, "next": (
                "사용자에게 확인받은 뒤 같은 명령에 --confirm <hash>를 붙여 다시 실행한다. "
                "지운 판은 이 기기의 사본(응답의 copy)이나, 동기화된 판이면 vault git 이력에서 되살린다")}
        if confirm != plan["hash"]:
            raise ValueError("확인한 판과 지금 판이 다르다 — 폐기하지 않았다. 다시 확인받아라")
        # 사본 이름은 확인받은 판의 해시다 — frontmatter의 id는 검증 전 값이라 경로 문자가
        # 들어 있으면 vault 밖을 가리키고, 같은 id의 다른 판(다른 worktree, 다시 만든 노드)은
        # 앞 사본을 덮는다(#138 리뷰 P1).
        copy = local_lock_path(f"osk-discarded-{plan['hash'].removeprefix('sha256:')}.md", vault.root)
        nodefile.write_file(copy, path.read_bytes())
        path.unlink()
        return {"ok": True, "discarded": plan, "copy": str(copy)}
