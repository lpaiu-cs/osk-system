"""osk.approvals — 보호영역 변경관리 (승인 기록부·승인본).

구현 근거: 헌법 10조(보호영역·승인본/작업본·승인/반려, 권한과 효력은
승인본에서만), 시행령 §6(clean/pending/stale·변경집합·초기 승인본·해제 처분·
표면 격리·검증기 적발), Mechanism §3(대장 공통 규율 + 승인 기록부
`approvals.jsonl`·승인본 보관 `approved/`·영역 tree·양측 CAS·순서와 실패).

체제: 서명이 노드 단위 '확인'이었던 것과 달리, 보호는 **구획(영역) 단위**의
'수용'이다. 엔진은 각 보호영역의 **승인본**(사용자가 마지막으로 승인한 영역
전체의 상태)을 내용 주소 저장소에 보존하고, 에이전트는 **작업본**에 평소처럼
쓴다. 지정·해제·승인·반려는 사용자 전속이다. 업데이트의 통치 구획 수용과
(지정 이력이 없을 때의) 최초 지정은 변경집합 재승인 후 비대화형으로 기록하며,
나머지는 대화형 단말에서 발의한다
(§3 7항·§6-2 2항 — 이 모듈의 쓰기 함수는 MCP 표면에 노출하지 않는다).

판정은 다른 `_ledger` 대장과 같은 인과 극대다(core). 영역의 인과 극대가
유일하지 않으면 stale — 승인·반려를 보류하고 사용자의 새 기록이 봉합한다.

영역 경로와 대장의 경로는 신뢰 밖 입력이다(다기기 병합) — 읽기·쓰기 전에
core.resolve_in_root로 vault 안에 봉쇄한다. 해석 실패는 언제나 거부 쪽이다.

이 모듈에는 승인 기록부와 그 판정이 남는다. 영역의 tree와 승인본 보관은 `region_tree`, 지정·승인·반려·해제의
발의는 `approval_acts`가 맡는다.
"""
from __future__ import annotations
import os
from pathlib import Path

from .core import (ledger_damage, sha256_bytes, sha256_file, posix_rel, resolve_in_root, ledger_append,
                   ledger_read, causal_maxima, effective_parents, heads, resolve_one, rid_key)
from . import region_tree, signatures
from .errors import LedgerDamage
from .vault import Vault


def ledger_path(vault: Vault) -> Path:
    """승인 기록부."""
    return vault.ledger / "approvals.jsonl"


def moves_path(vault: Vault) -> Path:
    """이동 기록부 — 이동을 이동으로 적는다."""
    return vault.ledger / "moves.jsonl"


KINDS = ("protect", "unprotect", "approve", "revert")


def _fold_eol(data: bytes) -> bytes:
    """줄바꿈만 접는다 — 두 바이트열이 **줄바꿈 말고는 같은가**를 묻기 위한 것.

    판정에는 쓰지 않는다. 승인본 tree는 raw 바이트 해시이고(Mechanism §3 4항)
    그것이 반려가 원본을 되살릴 수 있는 근거다 — 여기서 접는 것은 사용자에게
    **차이의 성질**을 알려 주기 위해서뿐이다."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


# ── 판정 (인과 극대) ─────────────────────────────────────────────────────

def records(vault: Vault) -> list[dict]:
    return ledger_read(ledger_path(vault))


def ledger_is_damaged(vault: Vault, recs: list[dict]) -> bool:
    """대장에 **구조 손상**(rid 부재·형식 위반·중복)이 있는가.

    `core.unresolved_nodes`의 손상 접기는 `field == "node"`일 때만 돌아서
    `region` 키에는 적용되지 않았다. 그 결과가 fail-**open**이었다: 손상된 행은
    `effective_parents`가 DAG에서 아예 빼므로 극대가 0이 되고, `state()`는 그것을
    `unprotected`로 읽었다 — 보호영역이 조용히 사라진 것처럼 보이고, 남은 행만
    극대가 되면 사용자가 승인한 내용이 "에이전트 변경"으로 보여 반려가 그것을
    지웠다(실측 재현).

    Mechanism §3 2항은 반대를 명한다 — "구조 손상은 해당 대상을 **미확정으로
    두고** … 그 상태에서는 새 기록의 append도 거부한다." 손상은 영역을 가리지
    않으므로(어느 행이 어느 갈래에 속했는지가 깨진 것이다) 전 영역을 미확정으로
    본다. 해소는 §3 8항의 수동 복구다."""
    return bool(ledger_damage(recs, ledger_path(vault)))


def region_record(vault: Vault, region: str, recs: list[dict] | None = None) -> dict | None:
    """영역의 현행 기록 — 그 `region`의 **유일한 인과 극대**. 극대가 여럿
    (다기기 비교 불능 분기)이거나 대장이 구조 손상이면 None(stale)."""
    recs = records(vault) if recs is None else recs
    if ledger_is_damaged(vault, recs):
        return None
    return resolve_one(recs, region, "region")


def approved_hash(vault: Vault, region: str, recs: list[dict] | None = None) -> str | None:
    """영역의 현행 승인본 tree 해시. protect·approve는 `accepted`,
    revert는 `base`, unprotect·미보호·stale는 None."""
    r = region_record(vault, region, recs)
    if not r:
        return None
    k = r.get("kind")
    if k in ("protect", "approve"):
        return r.get("accepted")
    if k == "revert":
        return r.get("base")
    return None                       # unprotect


def is_protected(vault: Vault, region: str, recs: list[dict] | None = None) -> bool:
    """승인 판정이 **가능한** 보호 상태인가 — 유일 극대가 있고 해제가 아닐 때.

    주의: **stale에서도 False다**(극대가 여럿이면 현행 기록을 못 고른다). 승인
    여부를 묻는 소비자에게는 그것이 옳은 fail-closed지만, "보호되지 않았다"는
    뜻으로 읽으면 stale을 미보호로 오인한다. 상태를 구분해야 하는 자리에서는
    `state()`를 쓴다."""
    r = region_record(vault, region, recs)
    return bool(r) and r.get("kind") != "unprotect"


def protected_regions(vault: Vault) -> list[str]:
    """해제되지 않은 영역 전부 — 상태가 `unprotected`가 아닌 것.

    **stale도 포함한다.** stale은 판정 불능이지 해제가 아니다 — 빼면 그 영역이
    현황에서 사라지고(사용자가 봉합할 대상을 못 본다), `region_of`가 None을
    돌려 파일 판정이 '보호영역 밖 = 제약 없음'으로 새어 나간다(fail-open).
    `is_protected`는 stale에서 False이므로 승인 여부 판정은 그대로 fail-closed다.

    판정은 대장만 본다 — 작업본 tree를 읽지 않는다(clean/pending 구분은 여기서
    필요 없고, 그것 때문에 영역 전수 판독을 하면 이 함수를 쓰는 모든 쓰기가
    비싸진다)."""
    recs = records(vault)
    named = {r.get("region") for r in recs if r.get("region")}
    if ledger_is_damaged(vault, recs):
        # 손상 위에서는 어느 영역이 해제됐는지도 말할 수 없다 — 이름이 오른 것을
        # 전부 남긴다(fail-closed). 빼면 `region_of`가 None을 돌려 그 아래 파일이
        # '보호영역 밖 = 제약 없음'으로 새어 나간다.
        return sorted(named)
    out = []
    for region in named:
        maxima = causal_maxima(recs, region, None, "region")
        if maxima and not (len(maxima) == 1
                           and maxima[0].get("kind") == "unprotect"):
            out.append(region)
    return sorted(out)


def state(vault: Vault, region: str, recs: list[dict] | None = None) -> str:
    """'unprotected' | 'clean' | 'pending' | 'stale'.
    stale = 인과 극대가 유일하지 않음(승인·반려 보류). clean = 작업본 tree가
    승인본 tree와 같음. pending = 다름."""
    recs = records(vault) if recs is None else recs
    if ledger_is_damaged(vault, recs):
        # 손상은 판정 불능이지 해제가 아니다. 이름이 대장에 오른 적 없는 영역만
        # `unprotected`이고, 나머지는 stale로 두어 승인·반려가 함께 막힌다.
        return ("stale" if any(r.get("region") == region for r in recs)
                else "unprotected")
    maxima = causal_maxima(recs, region, None, "region")
    if len(maxima) != 1:
        return "stale" if maxima else "unprotected"
    if maxima[0].get("kind") == "unprotect":
        return "unprotected"
    appr = approved_hash(vault, region, recs)
    work = region_tree.working_tree_hash(vault, region)
    return "clean" if appr is not None and appr == work else "pending"


def governance_warning(vault: Vault, recs: list[dict] | None = None) -> str | None:
    """통치 구획이 보호영역이 아니면 그 안내 — 실패가 아니라 경고다.

    통치 구획은 상설 보호영역인데(헌법 10조 1항) 갱신이 지정하기 전의 설치는
    지정 없이 남았다 — 통치 문서를 직접 고쳐도 검증은 PASS이고 status는
    아무것도 보이지 않았다(실측). 알리는 것까지가 엔진의 몫이고 지정은 사용자의
    확인 행위다."""
    recs = records(vault) if recs is None else recs
    # is_protected가 먼저다 — 보호 중이면 state()의 영역 전수 해시를 하지 않는다.
    if (not (vault.root / "_governance").is_dir() or is_protected(vault, "_governance", recs)
            or state(vault, "_governance", recs) != "unprotected"):
        return None
    return ("통치 구획(_governance)이 보호영역이 아니다 — 통치 문서를 고쳐도 "
            "변경집합으로 드러나지 않는다. 지정 이력이 없으면 갱신의 확인 적용"
            "(지금 판과 같은 태그의 재적용 포함)이 비준증빙과 같은 내용으로 "
            "지정하고, 그 밖에는 사용자가 대화형 단말에서 "
            "`osk protect _governance`로 지정한다")


def containing_regions(vault: Vault, path: Path | str) -> list[str]:
    """경로를 포함하는 보호영역 **전부** — 없으면 빈 목록. 영역은 중첩될 수
    있으므로(사용자가 하위 구획을 따로 지정) 포함 관계는 여럿일 수 있다."""
    p = resolve_in_root(vault, path)
    if p is None:
        return []
    rel = posix_rel(p, Path(os.path.realpath(vault.root)))
    out = []
    for region in protected_regions(vault):
        r = region.rstrip("/")
        if rel == r or rel.startswith(r + "/"):
            out.append(r)
    return out


def record_move(vault: Vault, node_id: str, src: Path, dst: Path) -> None:
    """이동을 **이동으로** 기록한다 — 시행령 §6 4항: "이동은 출발지와 도착지 중
    하나라도 보호영역이면 (변경집합에) 포함한다."

    기록이 없으면 반려가 이동을 '도착 영역의 추가 + 출발 영역의 삭제'로만 보아,
    되돌릴 때 노드를 지우거나(도착 쪽 반려 — 출발지에 복원할 정보가 없다)
    복제한다(출발 쪽 반려 — 도착 사본이 남는다). 기록은 물리 사건의 일지다 —
    권위 판정이 아니므로 인과 해소를 쓰지 않고, 소비자는 시각(rid)이 가장
    늦은 행을 본다. 이동이 실패해 남는 행은 무해하다 — 해석이 도착지의
    **실물**(그 id의 파일)로 위치를 확인하므로(`chain_position`), rename이
    실패한 행은 어떤 판정에도 쓰이지 않는다. 그래서 이동 **전에** 기록해,
    기록 없는 이동이 생기지 않게 한다."""
    if not node_id:
        return                            # 동일성 없는 파일은 추적 대상이 아니다
    if not (containing_regions(vault, src) or containing_regions(vault, dst)):
        # 양끝 다 보호 밖이라도, 이 노드가 이미 기록부에 있으면 **사슬을 계속
        # 적는다** — 보호영역을 빠져나온 노드가 미처리 변경집합인 채로 한 번 더
        # 이동하면, 그 hop을 놓친 반려가 옛 기록의 도착지에서 노드를 못 찾아
        # 승인본을 재생성하고 같은 id가 둘 남는다(복제). 사슬의 완결이 반려의
        # 전제다; 무관해진 잔행은 어차피 어떤 판정에도 쓰이지 않는다.
        if not any(r.get("node") == node_id for r in ledger_read(moves_path(vault))):
            return                        # 변경집합 무관 — 기록부를 소음으로 안 채운다
    root_real = Path(os.path.realpath(vault.root))
    ledger_append(moves_path(vault), {
        "kind": "move", "node": node_id,
        "from": posix_rel(Path(os.path.realpath(src)), root_real),
        "to": posix_rel(Path(os.path.realpath(dst)), root_real)})


def moves_boundary(vault: Vault) -> list[str]:
    """지금 이 순간 이동 기록부의 head rid들 — 승인 생애 기록(protect·approve·
    revert)에 `moves_seen`으로 박제한다. 생애 경계는 rid 크기 비교(전역 시계
    가정)가 아니라 **그 기록이 실제로 본 이동 기록부 상태**여야 한다: 두 대장의
    rid는 각자 독립 단조라 기기 간 시계 편차에서 순서가 어긋난다."""
    return heads(ledger_read(moves_path(vault)))


def moves_since(vault: Vault, region: str, recs: list[dict] | None = None) -> list[dict]:
    """이 영역의 반려·표시가 해석할 이동 행 — 현재 승인본이 성립한 기록이
    **보지 못한** 행만. 기록부는 물리 사건 전체를 보존하지만, 승인·반려로 이미
    처분된 이동은 그 시점의 승인본에 반영이 끝났으므로 다시 해석하면 수용된
    과거를 새 반려가 되밟는다(생애 경계).

    경계는 인과다 — 생애 기록의 `moves_seen`(그때의 이동 기록부 head들)의
    조상 폐포가 "이미 본 것"이고, 그 밖(후손이든 비교 불능 병행이든)이 해석
    대상이다. rid 크기 비교는 쓰지 않는다: 두 대장의 rid는 각자 독립 단조라
    기기 시계가 어긋나면 승인 이후의 실제 이동이 승인 rid보다 작아져 잘리고,
    반려가 밖의 실물을 못 본 채 승인본을 재생성해 같은 id가 둘 남는다.
    병행(비교 불능) 이동을 해석에 넣는 것도 옳다 — 승인이 못 본 이동이면
    작업본에는 반영돼 있으므로 되돌릴 변경집합의 일부다."""
    rec = region_record(vault, region, recs)
    if rec is None or not rec.get("rid"):
        return []
    rows = ledger_read(moves_path(vault))
    boundary = rec.get("moves_seen")
    if boundary is None:
        # 경계 미기록(이 필드 도입 전의 기록) — rid 시각 비교로 최선 노력
        cut = rid_key(rec["rid"])
        return [r for r in rows if r.get("rid") and rid_key(r["rid"]) > cut]
    par = effective_parents(rows)
    seen: set = set()
    stack = [r for r in boundary if isinstance(r, str)]
    while stack:
        rid = stack.pop()
        if rid in seen:
            continue
        seen.add(rid)
        stack.extend(par.get(rid, []))
    return [r for r in rows if r.get("rid") and r["rid"] not in seen]


def latest_move(rows: list[dict], key: str, rel: str, node: str | None = None,
                 before: str | None = None, after: str | None = None) -> dict | None:
    """rel을 `key`(from|to)로 갖는 가장 최근 이동 행 — 없으면 None.
    `node`로 id를, `before`/`after`로 rid 시각 경계를 좁힌다(사슬 추적용)."""
    hits = [r for r in rows if r.get(key) == rel and r.get("rid")
            and (node is None or r.get("node") == node)
            and (before is None or rid_key(r["rid"]) < rid_key(before))
            and (after is None or rid_key(r["rid"]) > rid_key(after))]
    return max(hits, key=lambda r: rid_key(r["rid"])) if hits else None


def region_of(vault: Vault, path: Path | str) -> str | None:
    """경로를 포함하는 **가장 안쪽** 보호영역 — 없으면 None."""
    regions = containing_regions(vault, path)
    return max(regions, key=len) if regions else None


def changeset(vault: Vault, region: str, *, record: dict | None = None,
              expect_work: str | None = None) -> dict | None:
    """승인본과 작업본의 **차이** — 헌법 10조 2항이 사용자에게 검토를 요구하는
    그 차이다. `record`를 주면 그 승인 갈래를 기준으로 비교한다. 판정할 수
    없으면(미보호·갈래 미지정 stale·승인본 미해석) None. `expect_work`가 있으면
    비교한 파일 목록·해시가 그 작업본과 일치해야 한다.

    반환: {"added": [rel…], "removed": [rel…], "modified": [rel…]}. 해시 비교라
    내용 diff는 아니지만, 사용자가 무엇이 생기고 사라지고 바뀌는지를 파일 단위로
    보고 판단할 수 있어야 승인이 확인 절차가 된다 — 해시 두 개만 보여주는 것은
    검토가 아니다."""
    recs = records(vault)
    if ledger_is_damaged(vault, recs):
        return None
    if record is not None:
        if record.get("region") != region or record not in recs:
            return None
        recs = [record]  # 이동의 생애 경계도 비교하는 그 갈래를 따른다.
    tree = approved_hash(vault, region, recs)
    if record is not None and record.get("kind") == "unprotect":
        tree = record.get("base")  # 해제 갈래는 해제 직전 승인본과 비교한다.
    table = region_tree.tree_table_for_region(vault, region, tree) if tree else None
    if table is None:
        return None
    d = resolve_in_root(vault, region)
    files = ({rel: p for rel, p in region_tree.region_files(vault, d)}
             if d is not None and d.is_dir() else {})
    cur = {rel: sha256_file(p) for rel, p in files.items()}
    if expect_work is not None and sha256_bytes(region_tree.manifest_blob(
            [[rel, cur[rel]] for rel in sorted(cur)])) != expect_work:
        raise ValueError("변경집합을 읽는 사이 작업본이 바뀌었다 — 다시 검토하라")
    cs = {
        "added": sorted(set(cur) - set(table)),
        "removed": sorted(set(table) - set(cur)),
        "modified": sorted(r for r in set(cur) & set(table) if cur[r] != table[r]),
    }
    # 개정 전 형상의 승인본은 **파일 단위 차이가 없어도 pending**이다(제외 구획
    # 항목이 승인본에만 남아 tree 해시가 어긋난다). 그 사실을 말해 주지 않으면
    # 사용자는 "차이가 없는데 왜 pending인가"에서 멈춘다 — 한 번 승인하면
    # 해소된다는 것까지 함께 낸다.
    legacy = region_tree.legacy_excluded(vault, tree, region)
    if legacy:
        cs["legacy_excluded"] = legacy
    # **줄바꿈만 다른 것**은 그렇게 말한다. EOL 고정 이행에서는 영역 전체가
    # 수정으로 보이는데 `git diff`는 빈 출력이라(정규화가 blob을 같게 만든다)
    # 사용자가 원인을 짚을 자리가 없다 — 내용이 그대로임을 여기서 알린다.
    eol_only = []
    for rel in cs["modified"]:
        blob = region_tree.store_get(vault, table[rel])
        if blob is None:
            continue
        try:
            now = (files[rel]).read_bytes()
        except OSError:
            continue
        if expect_work is not None and sha256_bytes(now) != cur[rel]:
            raise ValueError("변경집합을 읽는 사이 파일이 바뀌었다 — 다시 검토하라")
        if _fold_eol(blob) == _fold_eol(now):
            eol_only.append(rel)
    if eol_only:
        cs["eol_only"] = eol_only
    # 이동은 이동으로 보인다(시행령 §6 4항) — 반려와 **같은 해석**(노드별
    # 사슬, 생애 경계)으로 낸다. 표시와 복원이 갈리면 사용자가 검토한 것과
    # 반려가 하는 일이 달라진다(added/removed는 tree 차이 그대로 둔다).
    rows = moves_since(vault, region, recs)
    seen, moves = set(), []
    for rel in cs["added"] + cs["removed"]:
        row = (latest_move(rows, "to", rel) if rel in cur and rel not in table
               else latest_move(rows, "from", rel))
        node = row.get("node") if row else None
        if not node or node in seen:
            continue
        seen.add(node)
        chain = sorted((r for r in rows if r.get("node") == node),
                       key=lambda r: rid_key(r["rid"]))
        region_real = Path(os.path.realpath(d)) if d is not None else None

        def _ins(rel):
            q = resolve_in_root(vault, rel or "")
            return (region_real is not None and q is not None
                    and (q == region_real
                         or str(q).startswith(str(region_real) + os.sep)))
        touching = [r for r in chain if _ins(r.get("from")) or _ins(r.get("to"))]
        if not touching:
            continue
        origin = touching[0].get("from")
        at = chain_position(vault, node, chain)  # 복원과 같은 해석 — 실물이 사실이다
        if at is not None and origin != at:
            moves.append({"node": node, "from": origin, "to": at})
    cs["moves"] = moves
    return cs


def divergence(vault: Vault, region: str, recs: list[dict] | None = None) -> list[dict]:
    """stale 영역의 **갈래** — 인과 극대 기록 전부(비-stale이면 0~1개).
    사용자가 무엇이 갈렸는지 보고 봉합을 판단할 근거다(Mechanism §3 5항)."""
    recs = records(vault) if recs is None else recs
    return causal_maxima(recs, region, None, "region")


def file_matches_baseline(vault: Vault, path: Path | str) -> bool:
    """이 파일의 현재 내용이 자신을 포함하는 **모든** 보호영역의 승인본과
    일치하는가. 보호영역 밖이면 True(제약 없음).

    가장 안쪽 영역만 보면 안 된다 — 하위 구획만 승인된 경우 바깥 영역이 그
    변경을 승인한 적 없는데도 일치로 새어 나간다. 판정 불능(stale) 영역도
    불일치로 본다(`file_in_region_baseline`의 fail-closed). 권한·효력의 근거는
    승인본뿐이다(헌법 10조 4항)."""
    return all(file_in_region_baseline(vault, r, path)
               for r in containing_regions(vault, path))


def file_in_region_baseline(vault: Vault, region: str, path: Path | str) -> bool:
    """지정한 **정확한** region의 승인본 안에 이 파일이 그 해시로 들어 있는가.
    `region_of`(가장 안쪽 보호영역)가 아니라 호출자가 지정한 region으로 판정한다
    — 권한 검사는 위임 Facet **자체**의 승인본 반영이어야 하며, Facet은 미보호인데
    그 하위 디렉터리만 보호된 경우로 우회되지 않는다(헌법 7조 3항). region이
    보호 중이 아니거나 파일이 그 승인본에 없거나 해시가 다르면 False(fail-closed)
    — 미보호·stale은 `approved_hash`가 None이라 같은 지점에서 걸린다."""
    p = resolve_in_root(vault, path)
    if p is None or not p.is_file():
        return False
    tree = approved_hash(vault, region)
    table = region_tree.tree_table_for_region(vault, region, tree) if tree else None
    if not table:
        return False                  # 승인본 미해석 — fail-closed
    rel = posix_rel(p, Path(os.path.realpath(vault.root)))
    try:
        return table.get(rel) == sha256_file(p)
    except OSError:
        return False


def chain_position(vault: Vault, node: str, chain: list[dict]) -> str | None:
    """사슬에서 노드의 **실제** 현재 위치 — 도착지에 그 id의 실물이 있는 가장
    최근 hop의 to. 기록은 이동 **전에** 남으므로(실패 규율) 마지막 행의 to는
    의도이지 사실이 아니다 — rename이 실패한 잔행이 사슬 끝에 남으면 그 거짓
    to가 앞선 성공 이동의 실물을 가린다. 사실은 파일시스템이 정본이므로
    실물로 확인한다(실패 잔행이 다시 무해해진다). 없으면 None."""
    for r in reversed(chain):
        rel = r.get("to")
        q = resolve_in_root(vault, rel or "")
        if q is not None and q.is_file() and signatures.id_of(q) == node:
            return rel
    return None


# ── 검증기 지원 ──────────────────────────────────────────────────────────

def integrity(vault: Vault) -> list[str]:
    """승인 기록부의 정합성 — 형식 위반, 저장소에서 해석되지 않는 승인본,
    stale 영역(사용자 봉합 필요)을 보고한다(시행령 §6 7항 — 검증기가
    불일치를 적발한다). 대장 구조 손상(rid)은 core의 공통 검사가 맡는다."""
    errs = []
    try:
        recs = records(vault)
    except (OSError, LedgerDamage) as e:
        return [f"승인 기록부 판독 실패: {e}"]
    for i, r in enumerate(recs):
        if r.get("kind") not in KINDS:
            errs.append(f"행{i+1}: 미정의 kind {r.get('kind')}")
        if not r.get("region"):
            errs.append(f"행{i+1}: region 부재")
    for region in {r.get("region") for r in recs if r.get("region")}:
        st = state(vault, region, recs)
        if st == "stale":
            errs.append(f"영역 stale(인과 극대 비유일) — 사용자 봉합 필요: {region}")
            continue
        if is_protected(vault, region, recs):
            tree = approved_hash(vault, region, recs)
            table = region_tree.tree_table_for_region(vault, region, tree) if tree else None
            if table is None:
                errs.append(
                    f"승인본 manifest 해석 불가(부재·영역 불일치): "
                    f"{region} ({tree})")
                continue
            # manifest가 가리키는 **모든 blob의 실재**를 확인한다 — manifest만
            # 해석되고 그것이 가리키는 blob이 없으면 승인본이 clean으로 보여도
            # revert가 나중에 실패한다(복원 불가능한 승인본 적발).
            missing = [rel for rel, h in table.items() if region_tree.store_get(vault, h) is None]
            if missing:
                errs.append(
                    f"승인본 blob 부재(복원 불가): {region} — {sorted(missing)[:5]}")
    return errs
