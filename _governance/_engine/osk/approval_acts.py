"""osk.approval_acts — 지정·승인·반려·해제의 발의 (헌법 10조, 시행령 §6, Mechanism §3 7항).

사용자 전속이다. 대화형 단말에서 발의하고, 업데이트의 통치 구획 수용과 최초 지정만 변경집합 재승인 뒤
비대화형으로 기록한다. 이 모듈의 함수는 MCP 표면에 노출하지 않는다(§6-2 2항) — 표면 검사가 네 이름
(`protect`·`approve`·`revert`·`unprotect`)의 호출을 막는다."""
from __future__ import annotations
import contextlib, os
from pathlib import Path
from .core import (posix_rel, atomic_write, fsync_dir, mkdirs_durable, resolve_in_root, ledger_append,
                   causal_maxima, mutation_lock, rid_key, canon_rel)
from . import approvals, region_tree, signatures
from .vault import Vault


def protect(vault: Vault, region: str, reason: str = "", *, expect_work: str | None = None,
            _locked: bool = False) -> dict:
    """보호영역 지정 — 지정 시점 작업본을 **초기 승인본**으로 삼는다
    (시행령 §6 5항). 이미 보호 중이면 거부(이중 지정은 승인·반려로 한다).

    갱신은 트랜잭션 내내 mutation 잠금을 쥐고(`_locked`) 확인받은 통치 구획
    tree를 `expect_work`로 건다 — 박제한 작업본이 그와 다르면 지정하지 않는다
    (작업본 측 CAS, 단일 판독: 검사한 tree가 곧 박제한 tree다)."""
    with (contextlib.nullcontext() if _locked else mutation_lock(vault)):
        d = resolve_in_root(vault, region)
        if d is None or not d.is_dir():
            raise ValueError(f"영역이 vault 안의 디렉터리가 아니다: {region}")
        reg = posix_rel(d, Path(os.path.realpath(vault.root)))
        # **성립할 수 없는 영역을 받지 않는다.** `region_tree.SKIP_DIRS`는 영역 **안**의
        # 하위 디렉터리에만 걸리고 루트 자신에는 걸리지 않아, 대장 구획을 영역으로
        # 지정하면 승인본이 자기 자신을 담는 순환이 된다 — 지정 직후 pending,
        # 승인해도 곧바로 pending, 해제는 pending이라 거부되어 대장을 손으로
        # 고치기 전에는 빠져나올 수 없다(실측). vault 루트(`.`)도 같다: region
        # key가 `"."`이 되어 `containing_regions`의 접두 대조가 어떤 파일도 덮지
        # 못하고 changeset이 해석되지 않는다.
        if reg == "." or set(Path(reg).parts) & region_tree.SKIP_DIRS:
            raise ValueError(
                f"영역이 될 수 없는 구획이다: {reg} — 대장·저장소 살림살이 구획"
                f"({', '.join(sorted(region_tree.SKIP_DIRS))})과 vault 루트는 승인본이 "
                f"자기 자신을 담게 되어 판정이 성립하지 않는다")
        recs = approvals.records(vault)
        if approvals.state(vault, reg, recs) == "stale":
            # is_protected는 stale에서 False라 이중지정 거부를 통과한다 — 분기
            # 영역을 검토 없이 재봉인하지 않도록 stale을 먼저 막는다.
            raise ValueError(f"stale 영역이다 — 인과 분기 해소가 먼저다: {reg}")
        if approvals.is_protected(vault, reg, recs):
            raise ValueError(f"이미 보호 중인 영역이다: {reg}")
        accepted = region_tree.store_tree(vault, d)
        if expect_work is not None and accepted != expect_work:
            raise ValueError(
                "확인한 작업본이 그 사이 바뀌었다 — 지정하지 않았다 (작업본 측 CAS)")
        return ledger_append(approvals.ledger_path(vault), {
            "kind": "protect", "region": reg, "base": None,
            "accepted": accepted, "moves_seen": approvals.moves_boundary(vault),
            "reason": reason},
            # 잠금 안에서 본문과 같은 전제를 다시 본다 — `not is_protected`로는
            # 부족하다(is_protected는 stale에서도 False다). 스냅샷 중 비교 불능
            # 기록이 유입돼 stale이 되면, 그 분기가 표면화되지 않고 새 초기
            # 승인본으로 조용히 봉합된다.
            #
            # 작업본 쪽은 보지 않는다. 스냅샷 직후의 정상 동시 편집은 지정을
            # 무효로 만들 사고가 아니라 **다음 변경집합**이다 — 영역이 곧바로
            # pending으로 드러나고 사용자가 승인하거나 반려하면 된다. 그것을
            # 하드 오류로 바꾸면 자가 치유되는 상태를 재시도로 바꿀 뿐이다
            # (반려처럼 파괴적인 조작에만 작업본 결속을 건다).
            expect=lambda recs2: (
                None if approvals.state(vault, reg, recs2) == "unprotected" else
                f"그 사이 상태가 바뀌었다(다른 기기 기록 유입) — "
                f"{approvals.state(vault, reg, recs2)}: {reg}"))


def approve(vault: Vault, region: str, base: str, expect_work: str,
            reason: str = "", seal_heads: list[str] | None = None) -> dict:
    """승인 — **양측 CAS**(시행령 §6 3항): 검토가 전제한 승인본(`base`)이
    현행이고(승인본 측), 검토한 작업본(`expect_work`)이 승인 시점에도 그대로일
    때만(작업본 측) 성립한다. 어느 한쪽이 그 사이 달라지면 승인하지 않고 사용자의
    새 검토로 넘긴다 — 검토 뒤 에이전트가 더 쓴 변경까지 승인되는 일이 없게 한다.
    승인본을 검토한 작업본으로 갱신한다.

    `base`·`expect_work`는 **필수**다. 둘 중 하나가 None이면 CAS가 빈 비교가
    되므로 승인이 성립하지 않는다 — 영역을 판정할 수 없을 때(`approved_hash`·
    `working_tree_hash`가 None) CLI가 그 None을 그대로 넘기는 경로가 실제로
    있으므로, 그 자리에서 거부한다. 다만 **경로 진단이 먼저다** — 영역 자체가
    성립하지 않는 것이 원인이면 인자 탓으로 보고하지 않는다.

    예외는 **봉합 승인**뿐이다. stale(인과 극대 비유일)에서는 현행 승인본이
    하나로 정해지지 않아 걸 `base`가 없으므로 `base=None`으로 부르되, 대신
    사용자가 검토한 **갈래 집합**(`seal_heads` — 갈래 기록들의 rid)을 건다 —
    일반 승인의 `base`가 하던 "검토한 승인 상태에만 성립"을 봉합에서는 이
    집합이 맡는다. 프롬프트 사이 동기화로 새 갈래가 유입되면 집합이 어긋나
    거부된다 — 사용자가 본 적 없는 갈래를 함께 봉합하지 않는다. 성립한 기록이
    그 모든 head를 이어 분기를 봉합한다(Mechanism §3 5항 · 시행령 §6 3항의
    "해소는 사용자의 새 검토")."""
    with mutation_lock(vault):
        return approve_locked(vault, region, base, expect_work, reason, seal_heads)


def approve_locked(vault: Vault, region: str, base: str, expect_work: str,
                    reason: str = "", seal_heads: list[str] | None = None) -> dict:
    """Shared CAS; updater already owns the mutation lock across its transaction."""
    d = resolve_in_root(vault, region)
    if d is None or not d.is_dir():
        raise ValueError(f"영역이 vault 안의 디렉터리가 아니다: {region}")
    reg = posix_rel(d, Path(os.path.realpath(vault.root)))
    recs = approvals.records(vault)
    st = approvals.state(vault, reg, recs)
    if st == "unprotected":
        raise ValueError(f"보호 중이 아니다: {reg}")
    sealing = (st == "stale")
    if expect_work is None or (base is None) != sealing\
            or (seal_heads is not None) != sealing or (sealing and not seal_heads):
        raise ValueError(
            "stale 영역의 봉합 승인은 base 없이(None), 검토한 갈래 집합"
            f"(seal_heads)과 함께 한다 — 현행 승인본이 유일하지 않다: {reg}"
            if sealing else
            "base·expect_work(검토한 승인본·작업본)는 필수다 — 양측 CAS를 "
            "건너뛸 수 없다 (base=None·seal_heads는 stale 봉합에서만)")
    if sealing:
        now_heads = {r["rid"] for r in causal_maxima(recs, reg, None, "region")}
        if now_heads != set(seal_heads):
            raise ValueError(
                "갈래가 그 사이 바뀌었다(다른 기기 기록 유입) — 갈래를 다시 "
                f"보고 봉합하라: 검토={sorted(seal_heads)} 현행={sorted(now_heads)}")
    cur = approvals.approved_hash(vault, reg, recs)
    if not sealing and cur != base:
        raise ValueError(
            f"검토가 전제한 승인본이 현행이 아니다 (승인본 측 CAS): 전제={base} 현행={cur}")
    # 작업본을 **한 번** 읽어 박제하고, 그 **같은** tree를 작업본 측 CAS에
    # 쓴다 — 검사한 상태와 박제한 상태가 언제나 동일해야 검토하지 않은 변경이
    # 끼어들 창(TOCTOU)이 없다. 별도 판독으로 검사하고 또 다른 판독을 박제하면
    # 그 사이 에이전트가 쓴 상태가 승인본이 된다.
    accepted = region_tree.store_tree(vault, d)         # 승인 시점 작업본을 박제·해시(단일 판독)
    if accepted != expect_work:
        raise ValueError(
            "검토한 작업본이 그 사이 바뀌었다 — 다시 검토하라 (작업본 측 CAS)")
    if not sealing and accepted == cur:
        raise ValueError("변경집합이 없다 — 승인할 pending 차이가 없다")
    # 승인본 측 CAS를 **대장 잠금 안에서 다시** 본다 — 위 검사와 append 사이에
    # 영역 전수 판독(`region_tree.store_tree`)이 있어 창이 길다. 그 사이 다른 기기의 승인이
    # 동기화로 들어오면, 못 본 채 붙은 이 행이 그 기록의 인과 자식이 되어
    # 사용자가 검토한 적 없는 승인본을 조용히 대체하고 행의 base도 거짓이 된다.
    return ledger_append(approvals.ledger_path(vault), {
        "kind": "approve", "region": reg, "base": base,
        "accepted": accepted, "moves_seen": approvals.moves_boundary(vault),
        "reason": reason},
        expect=lambda recs2: (
            None if ({r["rid"] for r in causal_maxima(recs2, reg, None,
                                                      "region")}
                     == set(seal_heads) if sealing
                     else approvals.approved_hash(vault, reg, recs2) == base) else
            "승인본이 그 사이 바뀌었다(다른 기기 기록 유입) — 다시 검토하라"
            f" (승인본 측 CAS): 전제={base} 현행={approvals.approved_hash(vault, reg, recs2)}"))


def _exact(vault: Vault, rel: str, names: dict | None = None) -> Path:
    """반려가 쓰거나 지울 rel의 실경로 — rel이 **바로 그 자리**일 때만.

    realpath가 다른 이름을 내면(대소문자만 바뀐 이름, 정션·링크 너머, vault 밖)
    그 경로로 쓰고 지우는 일은 사용자가 검토한 적 없는 다른 파일을 바꾼다 —
    방금 복원한 파일을 삭제 순회가 지우거나 남의 구획을 덮는다(실측). 판정은
    갱신과 같은 계약(core.canon_rel)이고, 어긋나면 쓰기 전에 반려 전체를 멈춘다.

    POSIX realpath는 대소문자·정규화를 접지 않는다(macOS 등 대소문자 무시 FS) —
    그래서 이미 있는 구성요소는 디렉터리 목록에 **그 이름 그대로** 있는지도 본다.
    `names`는 한 반려 안의 목록 캐시다."""
    c = canon_rel(vault.root, rel)
    names = {} if names is None else names
    q = Path(os.path.realpath(vault.root))
    for part in Path(c).parts if c else ():
        if not os.path.lexists(q / part):
            break                         # 여기부터는 새로 만든다 — 그 이름 그대로 생긴다
        if q not in names:
            names[q] = set(os.listdir(q))
        if part not in names[q]:
            c = None
            break
        q = q / part
    if c is None:
        raise ValueError(
            f"반려할 경로가 그 이름 그대로의 자리가 아니다(대소문자·정규화만 바뀐 이름·"
            f"정션/링크 너머·vault 밖) — 아무것도 건드리지 않았다. 승인된 이름으로 "
            f"되돌리거나 링크를 치우거나, 지금 상태를 승인하라: {rel}")
    return Path(os.path.realpath(vault.root)) / c


def _plan_unmoves(vault: Vault, region_dir: Path, table: dict[str, str],
                  rows: list[dict]) -> list[tuple[Path, Path]]:
    """반려가 되돌릴 **이동**의 목록 — (지금 자리, 원위치) 쌍. 계획만 하고
    아무것도 건드리지 않는다. 되돌릴 수 없는 이동이 있으면 거부한다.

    해석 단위는 rel이 아니라 **노드**다 — 한 노드의 이동 사슬(rows는 이미 생애
    경계로 잘려 있다)을 통째로 읽어 계획을 노드당 하나만 세운다. rel별로 두
    방향을 따로 해석하면 나갔다가 다른 자리로 재진입한 노드에 계획이 둘 잡혀
    복원이 중도에 깨진다.

    - **원위치** = 사슬에서 영역에 닿는(어느 끝이든 영역 안) **최초 hop의
      출발지** — 이 영역의 변경집합(시행령 §6 4항)이 노드를 처음 옮긴 자리다.
      영역에 닿지 않는 hop(밖↔밖 방랑)은 이 영역이 되돌릴 일이 아니다.
    - **현재 위치** = 사슬 마지막 hop의 도착지(실물·id 확인). 실물이 없으면
      승인본 재생성으로 족하다.
    - 원위치가 영역 안인데 승인본에 없으면 영역 안 생성분이다 — 이동이 아니라
      생성의 반려이므로 평소대로 지운다.
    - 원위치가 차 있으면 반려를 거부한다(사용자 파일을 덮지 않는다 — 치우면
      진행된다)."""
    if not rows:
        return []
    region_real = Path(os.path.realpath(region_dir))

    def _inside_rel(rel: str | None) -> bool:
        p = resolve_in_root(vault, rel or "")
        return p is not None and (p == region_real
                                  or str(p).startswith(str(region_real) + os.sep))

    cur = {rel: p for rel, p in region_tree.region_files(vault, region_dir)}
    cand = set()                          # 이 영역의 변경집합에 연루된 노드
    for rel in set(cur) - set(table):     # 추가분 — 이동으로 들어온 노드
        row = approvals.latest_move(rows, "to", rel)
        if row and signatures.id_of(cur[rel]) == row.get("node"):
            cand.add(row["node"])
    for rel in set(table) - set(cur):     # 소실분 — 이동으로 나간 노드
        row = approvals.latest_move(rows, "from", rel)
        if row and row.get("node"):
            cand.add(row["node"])
    plan, targets = [], set()
    for node in sorted(cand):
        chain = sorted((r for r in rows if r.get("node") == node),
                       key=lambda r: rid_key(r["rid"]))
        touching = [r for r in chain
                    if _inside_rel(r.get("from")) or _inside_rel(r.get("to"))]
        if not touching:
            continue
        origin = touching[0].get("from")  # 변경집합이 처음 옮긴 자리
        at = approvals.chain_position(vault, node, chain) # 실물로 확인한 현재 위치
        if at is None:
            continue                      # 실물 없음 — 승인본 재생성으로 족하다
        if origin == at:
            continue                      # 제자리 순환 — 되돌릴 위치 변화 없음
        if _inside_rel(origin) and origin not in table:
            continue                      # 영역 안 생성분 — 생성의 반려(삭제)
        src, dst = _exact(vault, at), _exact(vault, origin or "")
        if dst.exists() or str(dst) in targets:
            raise ValueError(
                f"이동 원위치가 이미 차 있다 — 그 자리를 치우면 반려가 이동을 "
                f"되돌린다: {origin}")
        plan.append((src, dst))
        targets.add(str(dst))
    return plan


def revert(vault: Vault, region: str, base: str, expect_work: str, reason: str = "") -> dict:
    """반려 — 작업본을 승인본으로 **원상 복원**한다(시행령 §6 6항 · Mechanism
    §3 6항: 파일 복원을 마친 뒤에만 기록한다 — 기록에 실패해도 작업본이
    승인본과 같아졌으므로 clean으로 수렴할 뿐이다).

    `base`·`expect_work`는 **필수**다 — 반려는 파괴적이므로 사용자가 확인한
    그 변경집합(승인본 base → 작업본 expect_work)에만 성립해야 한다. 확인
    프롬프트 사이에 에이전트가 더 쓴 변경까지 '사용자가 승인한 반려'로 묶여
    사라지면 안 된다(approve의 양측 CAS와 같은 이유·같은 결속).

    영역 디렉터리가 통째로 사라진 경우도 복원 대상이다 — 승인본이 유효하면
    디렉터리를 다시 만들어 되돌린다. 보호가 되돌려야 할 가장 기본적인 사고가
    영역 삭제인데 그것만 수동 복구를 요구하면 장치의 뜻이 무너진다.

    반면 그 경로에 **디렉터리가 아닌 무언가가 있으면** 거부한다(pending으로
    남는다). 삭제와 달리 이쪽은 사용자가 치울 것이 눈앞에 있고(`rm` 한 번),
    엔진이 승인본에 없던 객체를 말없이 지우는 것은 반려의 범위가 아니다 —
    승인본 밖 물건을 지우는 것은 영역 **안**에서만 하는 일이다."""
    with mutation_lock(vault):   # 엔진이 내는 변경을 직렬화
        d = resolve_in_root(vault, region)
        if d is None:
            raise ValueError(f"영역이 vault 안의 경로가 아니다: {region}")
        if d.exists() and not d.is_dir():
            # 경로 진단이 인자 검사보다 **먼저**다 — 이 상태에서는 작업본을 판정할
            # 수 없어 호출부가 expect_work=None을 넘기게 되는데, 그때 인자 탓으로
            # 보고하면 사용자가 실제 원인(치울 객체)을 못 본다.
            raise ValueError(
                f"영역 경로에 디렉터리가 아닌 것이 있다 — 그것을 치우면(rm) 반려가 "
                f"승인본을 복원한다. 엔진은 승인본 밖 객체를 대신 지우지 않는다: {region}")
        if base is None or expect_work is None:
            raise ValueError(
                "base·expect_work(검토한 승인본·작업본)는 필수다 — 반려는 파괴적이다")
        reg = posix_rel(d, Path(os.path.realpath(vault.root)))
        recs = approvals.records(vault)
        st = approvals.state(vault, reg, recs)
        if st == "stale":
            raise ValueError(f"stale 영역이다 — 인과 분기 해소가 먼저다: {reg}")
        if st == "unprotected":
            raise ValueError(f"보호 중이 아니다: {reg}")
        cur = approvals.approved_hash(vault, reg, recs)
        if cur != base:
            raise ValueError(
                f"검토가 전제한 승인본이 현행이 아니다 (승인본 측): 전제={base} 현행={cur}")
        table = region_tree.tree_table_for_region(vault, reg, base)
        if table is None:
            raise ValueError(
                f"승인본 manifest를 해석하지 못했다(부재·영역 불일치) — 복원 불가: {base}")
        # 개정 전 형상의 승인본이면 **파괴 전에** 멈춘다(§3 4항). 제외 구획
        # 항목은 위에서 걸러졌으므로 복원이 그 자리를 덮지는 않지만, 그 승인본은
        # 새 규칙의 작업본 tree와 결코 같아질 수 없어 마지막 확인에서 어차피
        # 실패한다 — 그때는 다른 파일이 이미 되돌려진 뒤이고 기록은 남지 않는다.
        # 사용자가 할 일은 한 번의 승인이므로 그것을 말해 준다.
        # 개정 전 형상의 승인본이면 **반려하면서 이행한다**(§3 4항 마지막 문단).
        # 막지 않는다 — 막으면 그 영역의 일반 파일 변경을 버릴 길이 사라져,
        # 버리려던 것을 승인해야만 벗어나는 정반대 결과가 된다(리뷰 지적).
        # 제외 구획은 복원이 건드리지 않고(위 `table`에서 이미 걸러졌다),
        # 그 복원을 기록하는 `revert`가 다시 해석한 tree를 `base`로 적는다.
        stale_rels = region_tree.legacy_excluded(vault, base, reg)
        # **걸러내기가 작업본 스캔보다 많이 지우려 하면 멈춘다.** `table`은 승인본을
        # 지금 규칙으로 해석한 것이고 삭제 순회는 `region_files`가 연다 — 같은
        # 규칙을 두 곳에서 읽으므로 갈릴 수 있고, 갈리면 복원이 **승인본에 든 살아
        # 있는 파일을** 지운다. 그 뒤의 최종 확인은 잡지 못한다: 지운 결과로 다시
        # 계산한 해시가 같은 table에서 다시 유도한 해시와 맞아떨어져 삭제를
        # 확정해 버린다(4차 리뷰의 실측 경로). 그래서 파괴 전에, 두 해석을 서로
        # 대조해 한 번 확인한다 — 승인본에 있고 지금도 영역 파일로 열거되는
        # 자리는 복원 계획에 반드시 남아 있어야 한다.
        stored = region_tree.tree_table(vault, base) or {}
        live = {rel for rel, _ in region_tree.region_files(vault, d)}
        lost = sorted((live & set(stored)) - set(table))
        if lost:
            raise ValueError(
                "복원 계획이 승인본에 든 현재 파일을 지운다 — 제외 판정과 작업본 "
                "스캔이 어긋났다(반려하지 않았다): "
                + ", ".join(lost[:5]) + (" 외" if len(lost) > 5 else ""))
        discarded = region_tree.working_tree_hash(vault, reg)    # 복원 **전** — 실제로 버려지는 상태(감사)
        if discarded != expect_work:
            raise ValueError(
                "버릴 변경집합이 검토한 것과 다르다 — 다시 검토하라 (작업본 측): "
                f"검토={expect_work} 현재={discarded}")

        staged = _stage_tree(vault, d, table)     # 준비 — 아직 아무것도 건드리지 않았다
        unmoves = _plan_unmoves(vault, d, table, approvals.moves_since(vault, reg, recs))  # 이동 복원 계획(무변)
        # 준비를 마치고 **첫 쓰기 직전에** 두 전제를 다시 본다. 복원은 파괴적이므로
        # 전제는 기록의 정직성만이 아니라 파일을 건드리기 전에 유효해야 한다 —
        # 사후 검사는 기록만 막을 뿐 파괴는 이미 끝난 뒤다.
        if approvals.approved_hash(vault, reg) != base:
            raise ValueError(f"승인본이 그 사이 바뀌었다(다른 기기 기록 유입) — "
                             f"작업본을 건드리지 않았다: {reg}")
        work = region_tree.working_tree_hash(vault, reg)
        if work != expect_work:
            raise ValueError("버릴 변경집합이 그 사이 바뀌었다 — 다시 검토하라 "
                             f"(작업본 측): 검토={expect_work} 현재={work}")
        # 여기부터 파괴적이다. 이동의 원상 복원이 먼저다 — 안으로 온 노드는
        # 삭제 순회가 보기 전에 원위치로 나가고, 밖으로 간 노드는 승인본 내용
        # 쓰기가 덮기 전에 제자리로 돌아온다(순서가 뒤면 밖의 내용이 승인본을
        # 덮거나 이동 노드가 삭제된다).
        for now_at, home in unmoves:
            mkdirs_durable(home.parent)
            os.replace(now_at, home)
            fsync_dir(home.parent)        # revert 기록보다 엔트리가 먼저 내구화
            fsync_dir(now_at.parent)
        _apply_tree(vault, d, table, staged)
        # 복원 완료 최종 확인 — 작업본 tree가 실제로 승인본과 일치할 때만 기록한다.
        # 삭제·쓰기가 부분 실패해 작업본이 여전히 pending인데도 '복원을 마친 뒤에만
        # 기록한다'(Mechanism §3 6항)는 계약이 지켜진 것처럼 감사 대장에 남지 않게
        # 한다(fail-closed) — 실패는 미기록으로 남아 다음 revert가 다시 시도한다.
        now = region_tree.working_tree_hash(vault, reg)
        record_base = base
        if now != base and stale_rels:
            # **이행.** 개정 전 승인본을 지금 규칙으로 다시 해석한 tree가 곧
            # 복원된 작업본이면, 그것이 이 영역의 승인본이다 — 내용은 하나도
            # 바뀌지 않고 manifest에서 제외 구획 항목만 빠진다. 저장소에 넣어야
            # 다음 판독이 해석할 수 있다(blob은 이미 전부 들어 있다).
            rederived = region_tree.store_put(vault, region_tree.manifest_blob(
                [[rel, table[rel]] for rel in sorted(table)]))
            if rederived == now:
                record_base = rederived
        if now != record_base:
            raise ValueError(
                "복원이 승인본과 일치하지 않는다 — revert를 기록하지 않았다(fail-closed)")
        return ledger_append(approvals.ledger_path(vault), {
            "kind": "revert", "region": reg, "base": record_base,
            "discarded": discarded, "moves_seen": approvals.moves_boundary(vault),
            "reason": reason},
            expect=lambda recs2: (            # 복원 중 유입된 승인을 덮지 않는다
                None if approvals.approved_hash(vault, reg, recs2) == base else
                f"승인본이 그 사이 바뀌었다(다른 기기 기록 유입) — 다시 보라: {reg}"))


def unprotect(vault: Vault, region: str, reason: str = "") -> dict:
    """보호영역 해제 — 미처리 변경집합이 있으면 승인 또는 반려로 먼저 처분한다
    (시행령 §6 5항). 상설 보호영역(위임 Facet·통치 구획·모듈 Facet)의 해제는
    제도 개정과 함께 한다 — 그 정책 판정은 사용자·CLI가 지고, 이 함수는 물리만
    본다."""
    with mutation_lock(vault):   # 엔진이 내는 변경을 직렬화
        d = resolve_in_root(vault, region)
        reg = posix_rel(d, Path(os.path.realpath(vault.root))) if d else str(region).rstrip("/")
        recs = approvals.records(vault)
        st = approvals.state(vault, reg, recs)
        if st == "stale":
            raise ValueError(f"stale 영역이다 — 인과 분기 해소가 먼저다: {reg}")
        if st == "unprotected":
            raise ValueError(f"보호 중이 아니다: {reg}")
        if st == "pending":
            raise ValueError(
                "미처리 변경집합이 있다 — 해제 전에 승인 또는 반려로 처분하라 (시행령 §6 5항)")
        base = approvals.approved_hash(vault, reg, recs)
        return ledger_append(approvals.ledger_path(vault), {
            "kind": "unprotect", "region": reg,
            "base": base, "accepted": None, "reason": reason},
            expect=lambda recs2: (            # 유입된 승인을 해제로 덮지 않는다
                None if (approvals.state(vault, reg, recs2) == "clean"
                         and approvals.approved_hash(vault, reg, recs2) == base) else
                "그 사이 승인 기록이 바뀌었다(다른 기기 기록 유입) — 해제 전에 "
                f"다시 보라: {reg}"))


def _stage_tree(vault: Vault, region_dir: Path, table: dict[str, str]) -> list[tuple[Path, bytes]]:
    """복원할 내용을 **전부 메모리에 올린다** — 아직 아무것도 건드리지 않는다.

    blob이 없거나 **작업본의 경로 구조가 승인본과 충돌하면**(파일↔디렉터리가
    뒤바뀐 자리) 여기서 거부한다. **부분 복원**을 만들지 않는 것이 요점이다
    (반쯤 되돌아간 영역은 사용자가 검토할 수 없다). 준비와 반영을
    가르는 이유는 그 사이가 호출부가 전제를 마지막으로 확인할 자리이기
    때문이다 — 파괴가 시작된 뒤의 확인은 기록만 막을 뿐이다.

    table의 rel이 영역 안이라는 것은 `tree_table_for_region`이 이미 보장한다
    (그 함수를 거치지 않은 table은 복원의 근거가 아니다)."""
    region_real = Path(os.path.realpath(region_dir))
    staged: list[tuple[Path, bytes]] = []
    names: dict = {}
    for rel, h in sorted(table.items()):
        p = _exact(vault, rel, names)            # 정체성 훼손·vault 밖은 여기서 거부
        # 구조 충돌은 **쓰기 전에** 잡는다. 작업본에서 파일↔디렉터리가 뒤바뀐
        # 평범한 재구성(예: `sub/` 디렉터리를 지우고 파일 `sub`를 만듦)이면,
        # 반영 도중 mkdir·replace가 실패해 앞선 파일만 덮인 **부분 복원**이 된다.
        if p.is_dir():
            raise ValueError(
                f"복원 대상 자리에 디렉터리가 있다 — 구조를 먼저 바로잡으라: {rel}")
        for anc in p.parents:
            if anc == region_real:
                break
            if anc.exists() and not anc.is_dir():
                raise ValueError(
                    f"복원 경로의 부모가 디렉터리가 아니다 — 구조를 먼저 "
                    f"바로잡으라: {rel} ({posix_rel(anc, Path(os.path.realpath(vault.root)))})")
        data = region_tree.store_get(vault, h)
        if data is None:
            raise ValueError(f"승인본 blob 부재 — 복원 불가: {rel} {h}")
        staged.append((p, data))
    return staged


def _apply_tree(vault: Vault, region_dir: Path, table: dict[str, str],
                staged: list[tuple[Path, bytes]]) -> None:
    """준비된 내용을 실제로 반영한다 — **여기부터가 파괴적이다.**

    영역째 사라진 경우의 디렉터리 재생성이 첫 mutation이다. 쓰기 도중 외부에서
    들어오는 변경까지 막지는 못한다 — 그 잔여 창은 이 프로세스 밖(git pull 등)
    이라 닫을 수 없고, 그때는 영역이 pending으로 남아 다음 반려가 새 승인본으로
    복원한다."""
    mkdirs_durable(region_dir)
    # 2) 승인본 내용으로 원자 교체 — 엔트리까지 내구화한다(revert 기록이 뒤따른다)
    for p, data in staged:
        atomic_write(p, data)
    # 3) manifest에 없는 현재 파일 제거 (에이전트가 추가한 것)
    #    삭제 실패(권한 등)는 삼키지 않는다 — 조용히 넘기면 작업본이 승인본과
    #    다른 채로 남는데도 revert가 완료된 것처럼 기록될 수 있다.
    for rel, p in region_tree.region_files(vault, region_dir):
        if rel not in table:
            try:
                p.unlink()
            except FileNotFoundError:
                pass                      # 이미 없음 — 정상
            except OSError as e:
                raise ValueError(
                    f"승인본 밖 파일 삭제 실패 — 복원 미완료: {rel} ({e})") from e
            fsync_dir(p.parent)           # 삭제 엔트리 내구화
