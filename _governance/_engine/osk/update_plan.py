"""osk.update_plan — 릴리스가 여기에 무엇을 쓸 수 있고, 지금 상태와 견주면 무엇이 바뀌는가 (Mechanism §1-2 5·6항).

적용 집합은 릴리스 안의 발행 매니페스트가 정하고, 무엇이 와도 인스턴스 소유 바닥에는 쓰지 않는다.
바닥은 매니페스트가 아니라 이 모듈의 상수다. 계획은 직전 적용 상태와 같은 파일만 덮고, 로컬 수정은
덮지 않는다."""
from __future__ import annotations
import os
from pathlib import Path
from .core import ledger_read, sha256_file, resolve_within, canon_rel
from .layout import KINDS, PREFIXES, adapt_path
from . import publish, update_state
from .vault import Vault


ENGINE_PREFIX = "_governance/_engine/"


# 인스턴스 소유 바닥 — 릴리스·매니페스트가 무엇을 말하든 쓰지 않는다.
# (골격 .gitkeep은 디렉터리가 없을 때만 예외 — _skel에서 별도 처리)
SKEL_ROOTS = tuple(prefix + kind for prefix in PREFIXES for kind in KINDS)


FLOOR_HEADS = SKEL_ROOTS + ("_ledger", "_cited", "_raw", "_sources", ".osk", ".git")


def _floor(rel: str) -> bool:
    head = rel.split("/", 1)[0]
    if head in FLOOR_HEADS:
        return True
    return any(f"/{seg}/" in f"/{rel}" for seg in ("_ledger", "_cited", "_raw"))


# 골격을 만들어도 되는 곳은 **최상위 Space 루트 셋**뿐이다. 그 아래는 전부
# 인스턴스 소유 바닥이므로, `SKEL Scope/UserData/newdir` 같은 지시는 사용자
# 영역에 디렉터리를 만들게 된다 — 접두만 보지 않고 정확히 이 셋만 허용한다.
def _allowed_skel(vault: Vault, s: str) -> Path | None:
    """SKEL이 만들어도 되는 절대 경로 — 아니면 None. 골격은 **루트 안으로
    봉쇄된 최상위 Space 루트**만 만든다(Mechanism §1-2 5항의 바닥은 매니페스트가
    무엇을 말하든 지켜진다). `../` 탈출·하위 경로·비Space 경로는 거부."""
    if s not in SKEL_ROOTS:
        return None
    s = adapt_path(s, vault.space_roots)
    p = resolve_within(vault.root, s)                         # `..`·절대경로·심볼릭 탈출은 None
    if p is None:
        return None
    try:
        parts = p.relative_to(Path(os.path.realpath(vault.root))).parts
    except ValueError:
        return None
    if len(parts) != 1 or parts[0] not in vault.space_roots.values():
        return None                              # 최상위 Space 루트만 골격 대상
    return p


# ── 적용 집합 — 릴리스 안의 발행 매니페스트가 정한다 ─────────────────────
def _map_dest(ap: str, man: dict) -> str | None:
    """증빙 경로(정본 source)를 발행 매니페스트 MAP으로 인스턴스 경로에 사상한다
    — publish.collect과 같은 `src/a -> dst/a` 사상이다(dst-prefix 매칭이 아니라).
    비항등 MAP도 올바로 반영하고, 어느 MAP에도 안 걸리면 None(매니페스트 밖)."""
    for s, d in man["map"]:
        if not s.endswith("/"):
            if ap == s:
                return d
        elif ap.startswith(s.rstrip("/") + "/"):
            return d.rstrip("/") + "/" + ap[len(s.rstrip("/")) + 1:]
    return None


def apply_set(vault: Vault, tree: Path, rel: dict):
    """((source, dest) 사상 목록, 골격 절대경로 목록, 건너뜀 보고).
    KEEP은 정본 저장소 전용이라 제외. dest는 floor·루트 봉쇄를 통과한 것만."""
    man_rel = "_governance/_engine/scripts/publish-manifest.txt"
    # 매니페스트는 적용 범위를 정하는 **control plane**이다. 증빙 밖 파일은
    # 적용 대상이 아니지만(허용), 그런 파일이 증빙된 bytes의 목적지·정책을
    # 지배해서는 안 된다 — 반드시 증빙에 있고 이미 해시 검증된 것이어야 한다.
    if man_rel not in rel["files"]:
        raise update_state.UpdateError(
            f"발행 매니페스트가 비준증빙에 없다 — 적용 범위를 신뢰할 수 없다: "
            f"{man_rel}")
    man_path = tree / man_rel
    if not man_path.exists():
        raise update_state.UpdateError("릴리스에 발행 매니페스트가 없다 — 적용 범위 불명")
    man = publish.parse_manifest(man_path)
    keep = set(man["keep"])
    targets, skipped = [], []
    for ap in sorted(rel["files"]):
        if ap in keep or ap == update_state.ATTESTATION:
            skipped.append(f"{ap} (정본 저장소 전용)")
            continue
        if publish.denied_by(ap, man["deny"]):
            skipped.append(f"{ap} (DENY)")
            continue
        dest = _map_dest(ap, man)
        if dest is None:
            skipped.append(f"{ap} (매니페스트 밖)")
            continue
        # dest는 신뢰 밖 입력에서 파생된다 — canonical 경로로 봉쇄한 뒤 그
        # canonical에 floor를 건다(정규화 전 문자열 판정 우회 차단, P1).
        cdest = canon_rel(vault.root, dest)
        if cdest is None or _floor(cdest):
            skipped.append(f"{dest} (바닥·경로 봉쇄 — 쓰지 않는다)")
            continue
        targets.append((ap, cdest))
    # SKEL은 허용 골격(루트 봉쇄된 빈 Space 루트)만 통과시킨다
    skel = []
    for s in man["skel"]:
        d = _allowed_skel(vault, s.rstrip("/"))
        if d is None:
            skipped.append(f"{s} (SKEL 허용 밖 — 쓰지 않는다)")
        else:
            skel.append(d)
    return targets, skel, skipped


def plan(vault: Vault, tree: Path, targets: list, adopt: bool) -> dict:
    """targets는 (source, dest) 사상. add/update/rebaseline/conflict는 파일을
    읽어야 하므로 (source, dest)를, engine_drift/remove/remove_conflict는 보고·
    삭제만 하므로 dest 문자열을 담는다."""
    recs = ledger_read(update_state.journal_path(vault))
    add, same, rebaseline = [], [], []
    update, conflict, engine_drift, adopted = [], [], [], []
    dests = set()
    for src, dest in targets:
        dests.add(dest)
        s, d = tree / src, vault.root / dest
        if not d.exists():
            add.append((src, dest))
            continue
        if d.read_bytes() == s.read_bytes():
            # 내용은 upstream과 같다 — 기준선이 없거나(adopt·수동 동기화) 낡았으면
            # 저널만 갱신한다. 안 하면 다음 릴리스에서 이 파일이 drift로 오판된다.
            if update_state.last_applied_hash(recs, dest) != sha256_file(d):
                rebaseline.append((src, dest))
            else:
                same.append(dest)
            continue
        base = update_state.last_applied_hash(recs, dest)
        drifted = base is None or sha256_file(d) != base
        if drifted and not adopt:
            if dest.startswith(ENGINE_PREFIX):
                engine_drift.append(dest)
            else:
                conflict.append((src, dest))
        else:
            update.append((src, dest))
            if drifted:
                # **adopt가 로컬 수정 위에 덮는 자리다.** 편입의 뜻이 "현재
                # 릴리스를 기준선 삼는다"이므로 덮는 것 자체는 맞다. 맞지 않은
                # 것은 그것이 보고에서 평범한 `update`와 구별되지 않고, 성공하면
                # pre-image까지 `update_txn.txn_clear`가 지워 **되돌릴 길이 없다**는 것이다
                # (실측: 보고 update=[…], conflict=[], 사이드카 없음).
                adopted.append((src, dest))
    # 삭제 전파 — 직전까지 관리하던 파일이 새 릴리스에서 빠졌으면 인스턴스에서도
    # 제거한다(안 하면 하류가 정본과 다른 프레임워크를 실행한다). 로컬 수정이
    # 있는 삭제 대상은 지우지 않고 보존·보고한다. 바닥은 애초에 관리 대상이 아니다.
    remove, remove_conflict = [], []
    for p, h in sorted(update_state.managed_paths(recs).items()):
        cp = canon_rel(vault.root, p)                 # 병합된 저널 path도 봉쇄(P1)
        if cp is None or cp in dests or _floor(cp):
            continue
        rp = vault.root / cp
        if not rp.exists():
            continue
        (remove if sha256_file(rp) == h else remove_conflict).append(cp)
    return {"add": add, "same": len(same), "rebaseline": rebaseline,
            "update": update, "conflict": conflict, "engine_drift": engine_drift,
            "adopted": adopted,
            "remove": remove, "remove_conflict": remove_conflict}


def sidecar_plan(vault: Vault, p: dict, tree: Path, version: str, dests: set) -> tuple:
    """conflict 사이드카를 (쓸 것, 이미 같아 인정할 것, 손대지 않을 것)으로 가른다.

    사이드카는 사용자가 **수동 병합에 쓰는 작업 파일**이다. 같은 버전을 다시
    적용할 때 무조건 덮으면 그 작업이 영구히 사라진다(성공 후 pre-image도
    지워지므로 복구 경로가 없다). 그래서 내용이 incoming과 같을 때만 그대로
    인정하고, 다르면 **덮지 않고 보고**한다.

    `dests`에는 이번에 **적용할 경로와 삭제할 경로를 모두** 넘긴다 — 삭제 예정
    경로와 사이드카 이름이 겹치면 '보존·인정'으로 보고한 파일이 뒤의 삭제 단계에서
    사라져 보고가 거짓이 된다. 겹치면 갱신을 중단한다."""
    write, kept, held, collide = [], [], [], []
    for src, dest in p["conflict"]:
        side = dest + f".upstream-{version}"
        if side in dests:                        # 관리·삭제 예정 경로와 충돌
            collide.append(side)
            continue
        sp = vault.root / side
        if not sp.exists():
            write.append((src, side))
        elif sp.read_bytes() == (tree / src).read_bytes():
            kept.append(side)                    # 이미 같다 — 그대로 인정
        else:
            held.append(side)                    # 사용자 작업 — 손대지 않는다
    return write, kept, held, collide


def adopt_sidecar_plan(vault: Vault, p: dict, version: str, dests: set) -> tuple:
    """adopt가 덮을 로컬 내용을 옆에 남길 계획 — (쓸 것, 인정할 것, 보존할 것,
    겹친 것). 반환의 첫 항은 `(로컬 바이트, 사이드카 경로)`다.

    `--adopt`는 "현재 릴리스를 기준선 삼는다"는 뜻이므로 덮는 것 자체는 맞다.
    문제는 **되돌릴 길이 없다**는 것이었다: 성공하면 `update_txn.txn_clear`가 pre-image를
    지우고, 보고에서도 평범한 `update`와 구별되지 않아 사용자는 자기 수정이
    사라진 사실조차 모른다(실측). 그래서 덮기 **전에** 로컬 내용을
    `<경로>.local-<판본>`으로 남긴다.

    `conflict`의 사이드카(`.upstream-…`)와 방향이 반대다 — 저기서는 로컬이
    제자리에 남고 incoming을 옆에 두지만, 여기서는 incoming이 제자리를 갖고
    로컬을 옆에 둔다. 이름이 그 방향을 말한다.

    이미 있는 사이드카를 무조건 덮지 않는 것과 관리·삭제 예정 경로와 겹치면
    중단하는 것은 `sidecar_plan`과 같다. **다른 것이 하나 있다:** 내용이 다른
    사이드카가 이미 있으면 여기서는 `held`로 넘기고 진행할 수 없다.

    conflict의 `held`가 안전한 것은 **원본이 제자리에 남기** 때문이다 — 갱신하지
    못한 것은 옆에 둘 사본뿐이다. adopt는 반대로 원본을 덮으므로, 사이드카를
    쓰지 못한 채 진행하면 로컬 내용이 어디에도 남지 않는다(pre-image는 성공 후
    `update_txn.txn_clear`가 지운다). 사용자가 같은 이름의 `.local-<판본>`을 이미 가지고
    있는 것만으로 그 자리의 수정이 영구히 사라지는 셈이다(리뷰 지적). 그래서
    **덮기를 시작하지 않는다** — 무엇을 잃을지 모르는 채 파괴하지 않는다."""
    write, kept, blocked, collide = [], [], [], []
    for _src, dest in p["adopted"]:
        side = dest + f".local-{version}"
        if side in dests:
            collide.append(side)
            continue
        local = (vault.root / dest).read_bytes()      # **덮기 전** 바이트
        sp = vault.root / side
        if not sp.exists():
            write.append((local, side))
        elif sp.read_bytes() == local:
            kept.append(side)                   # 이미 같다 — 그대로 인정
        else:
            blocked.append(side)                # 다른 내용 — 진행하면 원본을 잃는다
    return write, kept, blocked, collide
