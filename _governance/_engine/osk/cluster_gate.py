"""osk.cluster_gate — 군집의 자리 규칙과 신설 관문 (Mechanism §6-2 3항).

노드가 놓일 수 있는 군집인지, 통치 구획인지, pin된 자리인지를 가리고, 새 군집은 확인을 거쳐서만
서게 한다."""
from __future__ import annotations
import json, os, time
from pathlib import Path
from .core import ledger_damage, ledger_read, posix_rel, resolve_one
from . import placement
from .errors import WriteError
from .naming import title_errors
from .vault import Vault


GOVERNANCE = ("governance",)             # 표면 쓰기 제외 (설계 D8)


def _is_cluster(kind: tuple) -> bool:
    """소속의 둘째 원소가 실제 군집 이름인가. Space 루트 직속 파일은
    `space_of`가 파일명을 군집 이름 자리에 넣으므로(`('scope','x.md')`)
    그것을 군집으로 오인하지 않는다 — 노드는 군집 안에만 둔다."""
    if kind[0] == "workbench-transit":
        return True
    return len(kind) > 1 and bool(kind[1]) and not str(kind[1]).endswith(".md")


def require_node_cluster(kind: tuple, dest: str) -> None:
    """노드를 둘 자리인가 — 생성·이동·군집 이동이 같은 판정을 쓴다."""
    reject_governance(kind)
    if not placement.is_node_home(kind) or not _is_cluster(kind):
        raise WriteError(
            f"노드를 둘 수 없는 구획이다: {dest} {kind} — 노드는 군집 안에 둔다. "
            f"Space 루트 직속과 `_`·`.` 접두 구획은 노드 자리가 아니다 "
            f"(Mechanism §1 4항)")


def reject_governance(kind: tuple) -> None:
    if kind[:1] == GOVERNANCE:
        raise WriteError(
            "통치 구획은 표면 쓰기 대상이 아니다 — 통치 문서의 개정은 정본 "
            "저장소에서 하고 갱신으로 도달한다 (헌법 3조 6항·시행령 §10 1항)")


def pinned(vault: Vault, *targets: str, subtree: str | None = None) -> bool:
    """대상 또는 이동할 하위 군집의 pin·미확정 여부. 대장은 한 번만 읽는다."""
    try:
        recs = ledger_read(vault.pins)
    except Exception:
        return True             # 판독 실패는 보수적으로 '고정됨' — fail-closed
    if ledger_damage(recs, vault.pins):
        return True
    wanted = {t.rstrip("/") for t in targets}
    prefix = subtree.rstrip("/") + "/" if subtree else None
    keys = {r.get("target") for r in recs if r.get("target")}
    for k in keys:
        if str(k).rstrip("/") not in wanted and not (prefix and str(k).startswith(prefix)):
            continue
        r = resolve_one(recs, k, "target")
        # 분기·손상은 unpin이 아니다(Mechanism §3 1·2항).
        if r is None or r.get("kind") != "unpin":
            return True
    return False


def cluster_names(vault: Vault) -> list[str]:
    """노드를 둘 수 있는 군집 경로 — **거부 응답에만** 실어 보낸다(상주 비용 0).
    표면에 열거 도구가 없으므로 실패하는 순간에 주소를 가르치는 것이 이 목록의
    일이다(10차 ②의 완화분 — 해결은 `overview`).

    하위 군집도 **함께 낸다** — 분화(시행령 §3 7항)로 생긴 자리를 못 보면
    에이전트가 거기에 노드를 둘 방법이 없다. 하위인지는 **허브의 존재**로
    가른다: 폴더와 동명인 노드가 있어야 군집이며, 없으면 그냥 폴더다.
    """
    out = set()

    def walk(d: Path, prefix: str) -> None:
        # DirEntry already knows the type on Windows. Do not stat every node
        # just to find the few directories.
        with os.scandir(d) as entries:
            dirs = [Path(e.path) for e in entries if e.is_dir()]
        for sub in sorted(dirs):
            k = placement.space_of(vault, sub / "x.md")
            if not (placement.is_node_home(k) and _is_cluster(k)
                    and k[:1] != GOVERNANCE):
                continue
            # 허브가 있어야 군집이다 — 허브 없는 자리는 **싣지 않는다.**
            # 구판은 실었고, 그래서 이 목록이 "노드를 둘 수 있는 군집"이라는
            # 자기 설명과 어긋났다: 신설 관문을 지났으나 뒤이은 검사에 거부돼
            # 남은 빈 디렉토리가 목록에 오르고, 거기 쓰려 하면 "첫 노드는
            # 허브"로 다시 거부된다(표면 감사 실측 — 처음 오는 자를 두 번
            # 헛걸음시킨다). 허브를 세우는 첫 쓰기는 이 목록을 거치지 않고
            # 관문이 안내하므로 잃는 것이 없다.
            path = f"{prefix}/{sub.name}"
            if placement.hub_file(sub):
                out.add(path)
                walk(sub, path)

    for space in (vault.scope, vault.domain, vault.person):
        d = vault.root / space
        if d.is_dir():
            walk(d, space)
    if (vault.root / vault.scope / "Workbench/transit").is_dir():
        out.add(f"{vault.scope}/Workbench/transit")
    return sorted(out)


# 확인 표식의 유효 시간. 관문의 목적은 차단이 아니라 "오류 → 사용자 확인 →
# 재시도"의 한 왕복이며, 그 왕복은 분 단위다. 너무 길면 잊힌 표식이 뒷날의
# 다른 요청을 무확인 통과시키고, 너무 짧으면 확인을 받아 오는 사이에 만료된다.
_ACK_TTL = 3600.0


def _ack_file(vault: Vault) -> Path:
    """표식은 **기기 로컬**이다(추적 트리 밖 — `git add -A`에 딸려 가지 않고
    다른 기기와 공유되지 않는다). 확인은 지금 이 대화의 일이지 저장소의
    상태가 아니다."""
    from .core import local_lock_path
    return local_lock_path("osk-cluster-ack.json", vault.root)


def _read_ack(vault: Vault) -> dict:
    try:
        d = json.loads(_ack_file(vault).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}   # 없음·손상 = 표식 없음 — 관문이 한 번 더 물을 뿐이다


def _write_ack(vault: Vault, d: dict) -> None:
    try:
        _ack_file(vault).write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass        # 표식을 못 남겨도 쓰기를 막지 않는다 — 1차 거부가 반복될 뿐


def new_cluster_gate(vault: Vault, dest: str, dest_dir: Path | None, doing: str) -> Path:
    """선언되지 않은 군집 — **2단계 확인 관문** (Mechanism §6-2 3항).

    신설을 막지 않는다 — 규범은 군집 형성의 자동화를 기본으로 두므로(헌법
    5조 4항·6조 9항) 여기의 일은 **한 번 묻는 것**이다. 1차 요청은 신설임을
    알리며 거부하고, 같은 군집에 대한 재시도는 통과시킨다. 선의의 에이전트는
    오류 원인이 돌아오면 사용자 허락을 확인한 뒤 같은 요청을 다시 보낸다 —
    그 한 왕복이 관문의 전부이며, 보안 경계가 아니라 확인 지점이다.

    통과하면 디렉토리를 만들어 돌려준다. 이후 검사(동명 등)가 거부해 빈
    디렉토리가 남을 수 있는데, git은 빈 디렉토리를 추적하지 않으므로
    무해하고 다음 시도에서는 선언된 군집으로 보인다."""
    if dest_dir is None:
        raise WriteError(f"군집 경로가 vault를 벗어난다: {dest}")
    # 신설이 설 수 있는 자리는 둘이다 — Space 루트 바로 아래(최상위 군집),
    # 그리고 **이미 선언된 군집 안**(하위 군집). 뒤엣것이 시행령 §3 7항의
    # 분화다: 한 허브가 지는 주제가 갈리면 하위 허브로 분화하고 상위 허브는
    # 하위 허브를 참조한다. 구판은 하위를 막았고, 그래서 규범에 있는 분화를
    # 표면으로 수행할 길이 없었다.
    parent = dest_dir.parent.resolve()
    roots = {(vault.root / vault.scope).resolve(), (vault.root / vault.domain).resolve(),
             (vault.root / vault.person).resolve()}
    top_level = parent in roots
    if not top_level and not placement.hub_file(parent):
        raise WriteError(
            f"선언되지 않은 군집이다: {dest}. 신설은 Space 루트 바로 아래이거나 "
            f"**허브가 있는 군집 안**이어야 한다(Mechanism §1 2항 · 시행령 §3 7항). "
            f"`{parent.name}`에 먼저 동명 허브 노드를 만들면 그 안에 분화할 수 "
            f"있다. 지금 쓸 수 있는 군집: {', '.join(cluster_names(vault)) or '없음'}")
    # 확인 표식·디렉토리를 남기기 **전에** 자리를 본다 — 노드 자리가 아닌 곳
    # (`_misc`·`.notes`)은 물어볼 것도 없이 거부다.
    require_node_cluster(placement.space_of(vault, dest_dir / "x.md"), dest)
    name_errs = title_errors(dest_dir.name)
    if name_errs:
        raise WriteError("군집 이름 부적격 — 이름이 곧 디렉토리명이다", name_errs)
    # 하위 군집에는 **2단계 확인을 붙이지 않는다.** 관문의 일은 "이거 신설인데
    # 맞습니까"를 한 번 묻는 것인데, 하위 군집은 그 물음의 답이 이미 나와 있다 —
    # 부모 허브가 존재한다는 것이 그 갈래를 사용자가 안다는 증거이고, 분화는
    # 시행령 §3 7항이 권장하는 정상 행위다. 게다가 "첫 노드는 허브"가 어차피
    # 한 왕복을 만들므로, 관문까지 붙이면 왕복이 둘이 된다.
    if not top_level:
        dest_dir.mkdir(parents=True, exist_ok=True)
        return dest_dir
    key = posix_rel(dest_dir, vault.root)
    now = time.time()
    pending = {k: v for k, v in _read_ack(vault).items()
               if isinstance(v, (int, float)) and now - v < _ACK_TTL}
    if key in pending:
        del pending[key]
        _write_ack(vault, pending)
        dest_dir.mkdir(parents=True, exist_ok=True)
        return dest_dir
    pending[key] = now
    _write_ack(vault, pending)
    raise WriteError(
        "새 군집 신설 — 1차 확인 거부 (쓰지 않았다)",
        [f"`{dest}`는 아직 없는 군집이라 {doing} **새 군집을 만든다**. "
         f"그 신설이 시행령이 맡긴 것인지 확인한 뒤 **같은 요청을 그대로 다시** "
         f"보내라(1시간 안의 재시도는 통과한다) — 노드를 써서 세션의 소속 scope를 "
         f"여는 것은 시행령 §3 9항, 주기 처리가 domain을 여는 것은 §3 1항을 따른다. "
         f"노드를 옮겨 scope를 여는 것과 그 밖의 최상위 군집 신설은 사용자의 허락을 "
         f"확인한 뒤 다시 보낸다(Mechanism §6-2 3항). 기존 군집을 쓰려던 것이면 "
         f"여기서 골라라: {', '.join(cluster_names(vault)) or '없음'}"])
