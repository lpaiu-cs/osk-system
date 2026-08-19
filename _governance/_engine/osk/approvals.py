"""osk.approvals — 보호영역 변경관리 (승인 기록부·승인본).

구현 근거: 헌법 10조(보호영역·승인본/작업본·승인/반려, 권한과 효력은
승인본에서만), 시행령 §6(clean/pending/stale·변경집합·초기 승인본·해제 처분·
표면 격리·검증기 적발), Mechanism §3(대장 공통 규율 + 승인 기록부
`approvals.jsonl`·승인본 보관 `approved/`·영역 tree·양측 CAS·순서와 실패).

체제: 서명이 노드 단위 '확인'이었던 것과 달리, 보호는 **구획(영역) 단위**의
'수용'이다. 엔진은 각 보호영역의 **승인본**(사용자가 마지막으로 승인한 영역
전체의 상태)을 내용 주소 저장소에 보존하고, 에이전트는 **작업본**에 평소처럼
쓴다. 지정·해제·승인·반려는 사용자 전속이며 대화형 단말에서만 발의한다
(§6-2 2항 — 이 모듈의 쓰기 함수는 MCP 표면에 노출하지 않는다).

판정은 다른 `_ledger` 대장과 같은 인과 극대다(core). 영역의 인과 극대가
유일하지 않으면 stale — 승인·반려를 보류하고 사용자의 새 기록이 봉합한다.

영역 경로와 대장의 경로는 신뢰 밖 입력이다(다기기 병합) — 읽기·쓰기 전에
core.resolve_in_root로 vault 안에 봉쇄한다. 해석 실패는 언제나 거부 쪽이다.
"""
from __future__ import annotations
import json, os, re, tempfile
from pathlib import Path

from .core import (ROOT, LEDGER, sha256_bytes, sha256_file, posix_rel,
                   resolve_in_root, ledger_append, ledger_read,
                   causal_maxima, mutation_lock, resolve_one, _rid_key)
from . import signatures

APPROVALS = LEDGER / "approvals.jsonl"
MOVES = LEDGER / "moves.jsonl"                # 이동 기록부 — 이동을 이동으로
STORE = LEDGER / "approved" / "objects"       # 내용 주소 blob·manifest 보관
KINDS = ("protect", "unprotect", "approve", "revert")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")   # 저장소 접근의 유일 형식

# 영역 tree에서 제외하는 이름 — 저장소 살림살이와 대장 자신(승인본이 대장을
# 담으면 승인이 자기 자신을 포함하는 순환이 된다). `_raw` 제외는 사용자 판정
# (2026-08-19)이다: raw는 append-only 저장소라 선의의 에이전트가 건드리는 것은
# 사용자 지시가 있을 때뿐이므로, 보호영역의 변경집합 대상이 아니다.
_SKIP_DIRS = {".git", ".venv", "__pycache__", "_ledger", "_raw"}


# ── 영역 tree (내용 주소) ────────────────────────────────────────────────

def _region_files(region_dir: Path) -> list[tuple[str, Path]]:
    """영역 안 정규 파일 전수 — (vault 상대 POSIX 경로, 절대 경로), 경로
    오름차순. 저장소 살림살이·대장·`_raw`는 뺀다(위 _SKIP_DIRS)."""
    out = []
    root_real = Path(os.path.realpath(ROOT))
    for cur, dirs, files in os.walk(region_dir):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS
                         and not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            p = Path(cur) / name
            if not p.is_file() or p.is_symlink():
                continue          # 심볼릭 링크·특수 파일은 내용 계약 밖
            try:
                rel = posix_rel(p, root_real)
            except ValueError:
                continue
            out.append((rel, p))
    return sorted(out, key=lambda x: x[0])


def _manifest_blob(entries: list) -> bytes:
    """manifest의 **정본 직렬화** — 공백 없는 UTF-8 JSON. 생성(작업본 해시·
    `_store_tree`)과 판독이 같은 함수를 쓰므로 정본 형상의 정의가 한 곳이다."""
    return json.dumps(entries, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def working_tree_hash(region: str) -> str | None:
    """작업본의 영역 tree 해시 — 판정할 수 없으면 None.

    세 경우를 구분한다. ①디렉터리면 그 tree의 해시. ②**부재**면 빈 tree의
    해시 — 영역째 지워진 것은 판정 불능이 아니라 "파일이 하나도 없는 상태"이며,
    그렇게 봐야 반려가 그 사고를 되돌릴 수 있다. ③그 경로에 디렉터리가 아닌
    무언가가 있으면 None — 부재와 접으면 빈 승인본을 가진 영역이 구조가 깨진
    채로 clean으로 오판되고 해제까지 허용된다(fail-closed)."""
    d = resolve_in_root(region)
    if d is None:
        return None
    if d.is_dir():
        return sha256_bytes(_manifest_blob(
            [[rel, sha256_file(p)] for rel, p in _region_files(d)]))
    if d.exists():
        return None                       # 디렉터리가 아닌 객체 — 판정 불능
    return sha256_bytes(_manifest_blob([]))          # 부재 = 빈 작업본


# ── 내용 주소 저장소 ─────────────────────────────────────────────────────

def _obj_path(digest: str) -> Path:
    """`sha256:<64hex>` → 저장 경로 `_ledger/approved/objects/<2>/<나머지>`.

    형식을 진입점에서 강제한다 — 손상된 대장에서 온 쓰레기 값이 경로 계산에
    섞이지 않고 그 자리에서 거부된다(fail-closed)."""
    if not (isinstance(digest, str) and _DIGEST_RE.match(digest)):
        raise ValueError(f"부적격 digest — 저장소 접근 거부: {digest!r}")
    hexd = digest.split(":", 1)[1]
    return STORE / hexd[:2] / hexd[2:]


def _store_put(data: bytes) -> str:
    """바이트를 내용 주소로 보관하고 그 digest를 돌려준다. 같은 내용은 한 번만
    저장된다(멱등) — 다기기 병합은 합집합으로 자명하다.

    이미 그 경로에 객체가 있으면 **그 내용이 digest와 일치하는지 확인**하고,
    동기화 충돌·디스크 손상·변조로 어긋나 있으면 올바른 bytes로 다시 쓴다 —
    내용 주소라 정본 내용이 유일하게 정해지므로 치유가 자명하다."""
    digest = sha256_bytes(data)
    dst = _obj_path(digest)
    if dst.exists():
        try:
            if sha256_bytes(dst.read_bytes()) == digest:
                return digest             # 정상 객체 — 멱등 반환
        except OSError:
            pass                          # 판독 불가 → 아래 원자 재기록으로 치유
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)              # 원자 교체 — 손상 객체를 정상으로 치유
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return digest


def _store_get(digest: str) -> bytes | None:
    try:
        p = _obj_path(digest)            # 부적격 digest는 ValueError
    except ValueError:
        return None                      # 신뢰 밖 digest — 부재로 취급(fail-closed)
    try:
        if not p.is_file():
            return None
        data = p.read_bytes()
    except OSError:
        return None
    # 내용 주소 계약: 읽은 bytes가 실제로 digest로 해시되어야 한다. 동기화
    # 충돌·디스크 손상·변조로 어긋난 객체는 부재/손상으로 취급한다(fail-closed)
    # — 손상 blob이 승인본으로 신뢰되어 integrity가 PASS하거나 revert가 잘못된
    # bytes를 복원하는 일이 없다. 이 한 지점의 검증이 두 소비자를 함께 막는다.
    if sha256_bytes(data) != digest:
        return None
    return data


def _store_tree(region_dir: Path) -> str:
    """영역의 작업본 전체(manifest + 각 파일 내용)를 저장소에 넣고 tree 해시를
    돌려준다.

    각 파일을 **한 번만** 읽어, 그 **같은 bytes**를 blob으로 저장하고 그 해시를
    manifest에 싣는다 — 해시 판독과 blob 저장 판독이 갈려 승인본이 복원 불가능
    (manifest는 hash(A)를 가리키나 저장된 blob은 hash(B))해지는 일이 없다.
    판독 순서·직렬화는 `working_tree_hash`와 같아 tree 해시가 동일하다."""
    entries = []
    for rel, p in _region_files(region_dir):
        data = p.read_bytes()          # 파일당 유일 판독
        h = _store_put(data)           # 그 bytes를 그대로 박제(반환 = 그 해시)
        entries.append([rel, h])
    return _store_put(_manifest_blob(entries))


def _tree_table(tree_hash: str) -> dict[str, str] | None:
    """tree 해시 → {rel: sha256}. 저장소에 manifest가 없으면 None."""
    blob = _store_get(tree_hash)
    if blob is None:
        return None
    try:
        entries = json.loads(blob.decode("utf-8"))
        return {str(rel): str(h) for rel, h in entries}
    except (ValueError, TypeError):
        return None


def _tree_table_for_region(region: str, tree_hash: str) -> dict[str, str] | None:
    """그 **영역의** 승인본 table — 형상이 정본이고 모든 rel이 `region/` 아래일
    때만 반환한다(아니면 None).

    `_store_tree(region_dir)`는 영역 디렉터리를 걸어 만들므로 정본 승인본의 rel은
    전부 그 영역 안이다. 이 결속이 없으면 영역과 승인본이 어긋난 조합에서
    **권한 판정은 성립인데 복원은 거부되는** 상태가 생긴다
    (`file_in_region_baseline`은 자기 파일 항목만 보고, 복원은 영역 밖 rel을 가진
    승인본을 근거로 삼을 수 없다). 세 소비자(integrity·권한 검사·반려)가 같은
    해석기를 쓰도록 결속을 이 한 곳에 둔다."""
    table = _tree_table(tree_hash)
    if table is None:
        return None
    r = str(region).rstrip("/")
    if not r or any(not rel.startswith(r + "/") for rel in table):
        return None                   # 영역 밖 항목 — 그 영역의 tree가 아니다
    return table


# ── 판정 (인과 극대) ─────────────────────────────────────────────────────

def records() -> list[dict]:
    return ledger_read(APPROVALS)


def region_record(region: str, recs: list[dict] | None = None) -> dict | None:
    """영역의 현행 기록 — 그 `region`의 **유일한 인과 극대**. 극대가 여럿
    (다기기 비교 불능 분기)이면 None(stale)."""
    recs = records() if recs is None else recs
    return resolve_one(recs, region, "region")


def approved_hash(region: str, recs: list[dict] | None = None) -> str | None:
    """영역의 현행 승인본 tree 해시. protect·approve는 `accepted`,
    revert는 `base`, unprotect·미보호·stale는 None."""
    r = region_record(region, recs)
    if not r:
        return None
    k = r.get("kind")
    if k in ("protect", "approve"):
        return r.get("accepted")
    if k == "revert":
        return r.get("base")
    return None                       # unprotect


def is_protected(region: str, recs: list[dict] | None = None) -> bool:
    """승인 판정이 **가능한** 보호 상태인가 — 유일 극대가 있고 해제가 아닐 때.

    주의: **stale에서도 False다**(극대가 여럿이면 현행 기록을 못 고른다). 승인
    여부를 묻는 소비자에게는 그것이 옳은 fail-closed지만, "보호되지 않았다"는
    뜻으로 읽으면 stale을 미보호로 오인한다. 상태를 구분해야 하는 자리에서는
    `state()`를 쓴다."""
    r = region_record(region, recs)
    return bool(r) and r.get("kind") != "unprotect"


def protected_regions() -> list[str]:
    """해제되지 않은 영역 전부 — 상태가 `unprotected`가 아닌 것.

    **stale도 포함한다.** stale은 판정 불능이지 해제가 아니다 — 빼면 그 영역이
    현황에서 사라지고(사용자가 봉합할 대상을 못 본다), `region_of`가 None을
    돌려 파일 판정이 '보호영역 밖 = 제약 없음'으로 새어 나간다(fail-open).
    `is_protected`는 stale에서 False이므로 승인 여부 판정은 그대로 fail-closed다.

    판정은 대장만 본다 — 작업본 tree를 읽지 않는다(clean/pending 구분은 여기서
    필요 없고, 그것 때문에 영역 전수 판독을 하면 이 함수를 쓰는 모든 쓰기가
    비싸진다)."""
    recs = records()
    out = []
    for region in {r.get("region") for r in recs if r.get("region")}:
        maxima = causal_maxima(recs, region, None, "region")
        if maxima and not (len(maxima) == 1
                           and maxima[0].get("kind") == "unprotect"):
            out.append(region)
    return sorted(out)


def state(region: str, recs: list[dict] | None = None) -> str:
    """'unprotected' | 'clean' | 'pending' | 'stale'.
    stale = 인과 극대가 유일하지 않음(승인·반려 보류). clean = 작업본 tree가
    승인본 tree와 같음. pending = 다름."""
    recs = records() if recs is None else recs
    maxima = causal_maxima(recs, region, None, "region")
    if len(maxima) != 1:
        return "stale" if maxima else "unprotected"
    if maxima[0].get("kind") == "unprotect":
        return "unprotected"
    appr = approved_hash(region, recs)
    work = working_tree_hash(region)
    return "clean" if appr is not None and appr == work else "pending"


def containing_regions(path: Path | str) -> list[str]:
    """경로를 포함하는 보호영역 **전부** — 없으면 빈 목록. 영역은 중첩될 수
    있으므로(사용자가 하위 구획을 따로 지정) 포함 관계는 여럿일 수 있다."""
    p = resolve_in_root(path)
    if p is None:
        return []
    rel = posix_rel(p, Path(os.path.realpath(ROOT)))
    out = []
    for region in protected_regions():
        r = region.rstrip("/")
        if rel == r or rel.startswith(r + "/"):
            out.append(r)
    return out


def record_move(node_id: str, src: Path, dst: Path) -> None:
    """이동을 **이동으로** 기록한다 — 시행령 §6 4항: "이동은 출발지와 도착지 중
    하나라도 보호영역이면 (변경집합에) 포함한다."

    기록이 없으면 반려가 이동을 '도착 영역의 추가 + 출발 영역의 삭제'로만 보아,
    되돌릴 때 노드를 지우거나(도착 쪽 반려 — 출발지에 복원할 정보가 없다)
    복제한다(출발 쪽 반려 — 도착 사본이 남는다). 기록은 물리 사건의 일지다 —
    권위 판정이 아니므로 인과 해소를 쓰지 않고, 소비자는 시각(rid)이 가장
    늦은 행을 본다. 이동이 실패해 남는 행은 무해하다(그 자리에 그 id의 파일이
    없으면 어떤 판정에도 쓰이지 않는다) — 그래서 이동 **전에** 기록해, 기록
    없는 이동이 생기지 않게 한다."""
    if not node_id:
        return                            # 동일성 없는 파일은 추적 대상이 아니다
    if not (containing_regions(src) or containing_regions(dst)):
        # 양끝 다 보호 밖이라도, 이 노드가 이미 기록부에 있으면 **사슬을 계속
        # 적는다** — 보호영역을 빠져나온 노드가 미처리 변경집합인 채로 한 번 더
        # 이동하면, 그 hop을 놓친 반려가 옛 기록의 도착지에서 노드를 못 찾아
        # 승인본을 재생성하고 같은 id가 둘 남는다(복제). 사슬의 완결이 반려의
        # 전제다; 무관해진 잔행은 어차피 어떤 판정에도 쓰이지 않는다.
        if not any(r.get("node") == node_id for r in ledger_read(MOVES)):
            return                        # 변경집합 무관 — 기록부를 소음으로 안 채운다
    root_real = Path(os.path.realpath(ROOT))
    ledger_append(MOVES, {
        "kind": "move", "node": node_id,
        "from": posix_rel(Path(os.path.realpath(src)), root_real),
        "to": posix_rel(Path(os.path.realpath(dst)), root_real)})


def _latest_move(rows: list[dict], key: str, rel: str, node: str | None = None,
                 before: str | None = None, after: str | None = None) -> dict | None:
    """rel을 `key`(from|to)로 갖는 가장 최근 이동 행 — 없으면 None.
    `node`로 id를, `before`/`after`로 rid 시각 경계를 좁힌다(사슬 추적용)."""
    hits = [r for r in rows if r.get(key) == rel and r.get("rid")
            and (node is None or r.get("node") == node)
            and (before is None or _rid_key(r["rid"]) < _rid_key(before))
            and (after is None or _rid_key(r["rid"]) > _rid_key(after))]
    return max(hits, key=lambda r: _rid_key(r["rid"])) if hits else None


def region_of(path: Path | str) -> str | None:
    """경로를 포함하는 **가장 안쪽** 보호영역 — 없으면 None."""
    regions = containing_regions(path)
    return max(regions, key=len) if regions else None


def changeset(region: str) -> dict | None:
    """승인본과 작업본의 **차이** — 헌법 10조 2항이 사용자에게 검토를 요구하는
    그 차이다. 판정할 수 없으면(미보호·stale·승인본 미해석) None.

    반환: {"added": [rel…], "removed": [rel…], "modified": [rel…]}. 해시 비교라
    내용 diff는 아니지만, 사용자가 무엇이 생기고 사라지고 바뀌는지를 파일 단위로
    보고 판단할 수 있어야 승인이 확인 절차가 된다 — 해시 두 개만 보여주는 것은
    검토가 아니다."""
    tree = approved_hash(region)
    table = _tree_table_for_region(region, tree) if tree else None
    if table is None:
        return None
    d = resolve_in_root(region)
    files = ({rel: p for rel, p in _region_files(d)}
             if d is not None and d.is_dir() else {})
    cur = {rel: sha256_file(p) for rel, p in files.items()}
    cs = {
        "added": sorted(set(cur) - set(table)),
        "removed": sorted(set(table) - set(cur)),
        "modified": sorted(r for r in set(cur) & set(table) if cur[r] != table[r]),
    }
    # 이동은 이동으로 보인다(시행령 §6 4항) — 추가·삭제 중 이동 기록과 id가
    # 맞는 것을 해석해 함께 낸다(added/removed는 tree 차이 그대로 둔다).
    rows = ledger_read(MOVES)
    moves = []
    for rel in cs["added"]:
        row = _latest_move(rows, "to", rel)
        if row and signatures._id_of(files[rel]) == row.get("node"):
            moves.append({"node": row["node"], "from": row["from"], "to": rel})
    for rel in cs["removed"]:
        row = _latest_move(rows, "from", rel)
        out = resolve_in_root(row.get("to") or "") if row else None
        if row and out is not None and out.is_file() \
                and signatures._id_of(out) == row.get("node"):
            moves.append({"node": row["node"], "from": rel, "to": row["to"]})
    cs["moves"] = moves
    return cs


def divergence(region: str, recs: list[dict] | None = None) -> list[dict]:
    """stale 영역의 **갈래** — 인과 극대 기록 전부(비-stale이면 0~1개).
    사용자가 무엇이 갈렸는지 보고 봉합을 판단할 근거다(Mechanism §3 5항)."""
    recs = records() if recs is None else recs
    return causal_maxima(recs, region, None, "region")


def file_matches_baseline(path: Path | str) -> bool:
    """이 파일의 현재 내용이 자신을 포함하는 **모든** 보호영역의 승인본과
    일치하는가. 보호영역 밖이면 True(제약 없음).

    가장 안쪽 영역만 보면 안 된다 — 하위 구획만 승인된 경우 바깥 영역이 그
    변경을 승인한 적 없는데도 일치로 새어 나간다. 판정 불능(stale) 영역도
    불일치로 본다(`file_in_region_baseline`의 fail-closed). 권한·효력의 근거는
    승인본뿐이다(헌법 10조 4항)."""
    return all(file_in_region_baseline(r, path)
               for r in containing_regions(path))


def file_in_region_baseline(region: str, path: Path | str) -> bool:
    """지정한 **정확한** region의 승인본 안에 이 파일이 그 해시로 들어 있는가.
    `region_of`(가장 안쪽 보호영역)가 아니라 호출자가 지정한 region으로 판정한다
    — 권한 검사는 위임 Facet **자체**의 승인본 반영이어야 하며, Facet은 미보호인데
    그 하위 디렉터리만 보호된 경우로 우회되지 않는다(헌법 7조 3항). region이
    보호 중이 아니거나 파일이 그 승인본에 없거나 해시가 다르면 False(fail-closed)
    — 미보호·stale은 `approved_hash`가 None이라 같은 지점에서 걸린다."""
    p = resolve_in_root(path)
    if p is None or not p.is_file():
        return False
    tree = approved_hash(region)
    table = _tree_table_for_region(region, tree) if tree else None
    if not table:
        return False                  # 승인본 미해석 — fail-closed
    rel = posix_rel(p, Path(os.path.realpath(ROOT)))
    try:
        return table.get(rel) == sha256_file(p)
    except OSError:
        return False


# ── 발의 (사용자 전속 — 대화형 단말) ─────────────────────────────────────

def protect(region: str, reason: str = "") -> dict:
    """보호영역 지정 — 지정 시점 작업본을 **초기 승인본**으로 삼는다
    (시행령 §6 5항). 이미 보호 중이면 거부(이중 지정은 승인·반려로 한다)."""
    with mutation_lock():   # 엔진이 내는 변경을 직렬화
        d = resolve_in_root(region)
        if d is None or not d.is_dir():
            raise ValueError(f"영역이 vault 안의 디렉터리가 아니다: {region}")
        reg = posix_rel(d, Path(os.path.realpath(ROOT)))
        recs = records()
        if state(reg, recs) == "stale":
            # is_protected는 stale에서 False라 이중지정 거부를 통과한다 — 분기
            # 영역을 검토 없이 재봉인하지 않도록 stale을 먼저 막는다.
            raise ValueError(f"stale 영역이다 — 인과 분기 해소가 먼저다: {reg}")
        if is_protected(reg, recs):
            raise ValueError(f"이미 보호 중인 영역이다: {reg}")
        accepted = _store_tree(d)
        return ledger_append(APPROVALS, {
            "kind": "protect", "region": reg,
            "base": None, "accepted": accepted, "reason": reason},
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
                None if state(reg, recs2) == "unprotected" else
                f"그 사이 상태가 바뀌었다(다른 기기 기록 유입) — "
                f"{state(reg, recs2)}: {reg}"))


def approve(region: str, base: str, expect_work: str,
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
    with mutation_lock():   # 엔진이 내는 변경을 직렬화
        d = resolve_in_root(region)
        if d is None or not d.is_dir():
            raise ValueError(f"영역이 vault 안의 디렉터리가 아니다: {region}")
        reg = posix_rel(d, Path(os.path.realpath(ROOT)))
        recs = records()
        st = state(reg, recs)
        if st == "unprotected":
            raise ValueError(f"보호 중이 아니다: {reg}")
        sealing = (st == "stale")
        if expect_work is None or (base is None) != sealing \
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
        cur = approved_hash(reg, recs)
        if not sealing and cur != base:
            raise ValueError(
                f"검토가 전제한 승인본이 현행이 아니다 (승인본 측 CAS): 전제={base} 현행={cur}")
        # 작업본을 **한 번** 읽어 박제하고, 그 **같은** tree를 작업본 측 CAS에
        # 쓴다 — 검사한 상태와 박제한 상태가 언제나 동일해야 검토하지 않은 변경이
        # 끼어들 창(TOCTOU)이 없다. 별도 판독으로 검사하고 또 다른 판독을 박제하면
        # 그 사이 에이전트가 쓴 상태가 승인본이 된다.
        accepted = _store_tree(d)         # 승인 시점 작업본을 박제·해시(단일 판독)
        if accepted != expect_work:
            raise ValueError(
                "검토한 작업본이 그 사이 바뀌었다 — 다시 검토하라 (작업본 측 CAS)")
        if not sealing and accepted == cur:
            raise ValueError("변경집합이 없다 — 승인할 pending 차이가 없다")
        # 승인본 측 CAS를 **대장 잠금 안에서 다시** 본다 — 위 검사와 append 사이에
        # 영역 전수 판독(_store_tree)이 있어 창이 길다. 그 사이 다른 기기의 승인이
        # 동기화로 들어오면, 못 본 채 붙은 이 행이 그 기록의 인과 자식이 되어
        # 사용자가 검토한 적 없는 승인본을 조용히 대체하고 행의 base도 거짓이 된다.
        return ledger_append(APPROVALS, {
            "kind": "approve", "region": reg,
            "base": base, "accepted": accepted, "reason": reason},
            expect=lambda recs2: (
                None if ({r["rid"] for r in causal_maxima(recs2, reg, None,
                                                          "region")}
                         == set(seal_heads) if sealing
                         else approved_hash(reg, recs2) == base) else
                "승인본이 그 사이 바뀌었다(다른 기기 기록 유입) — 다시 검토하라"
                f" (승인본 측 CAS): 전제={base} 현행={approved_hash(reg, recs2)}"))


def _plan_unmoves(region_dir: Path, table: dict[str, str]) -> list[tuple[Path, Path]]:
    """반려가 되돌릴 **이동**의 목록 — (지금 자리, 원위치) 쌍. 계획만 하고
    아무것도 건드리지 않는다. 되돌릴 수 없는 이동이 있으면 거부한다.

    두 방향이 있다(시행령 §6 4항 — 이동은 한쪽 끝만 보호여도 변경집합이다).
    이동은 여러 hop의 **사슬**일 수 있으므로(기록부가 사슬을 완결해 적는다)
    한 행이 아니라 사슬 전체를 해석한다:
    ①안으로 온 이동 — 승인본에 없는 파일이 이동 기록의 도착지이고 그 id가
      기록과 일치하면, 지우는 대신 사슬을 **거슬러** 영역 밖 원위치(또는 영역
      안 승인본 원적)로 돌려보낸다. 이동으로 온 노드를 지우는 것은 원상 복원이
      아니라 소실이다. 원위치가 차 있으면 반려를 거부한다(치우면 진행된다).
      영역 안에서 생성된 뒤 이동만 한 노드는 사슬의 끝이 영역 안 비원적이라
      평소대로 지운다(생성의 반려).
    ②밖으로 간 이동 — 승인본의 rel이 비어 있으면 사슬을 **따라가** 최종
      위치를 찾고, 거기에 그 id의 파일이 실재하면 되가져온다(내용은 이어지는
      승인본 복원이 정본으로 덮는다). 재생성만 하면 밖의 사본이 남아 같은 id가
      둘이 된다. 최종 위치에도 없으면 재생성으로 족하다.

    판정의 열쇠는 경로가 아니라 **id**다(이 체계의 동일성) — 같은 자리에 다른
    파일이 새로 생긴 경우는 이동과 무관하므로 평소대로 다룬다."""
    rows = ledger_read(MOVES)
    if not rows:
        return []
    region_real = Path(os.path.realpath(region_dir))

    def _inside(p: Path) -> bool:
        return p == region_real or str(p).startswith(str(region_real) + os.sep)

    cur = {rel: p for rel, p in _region_files(region_dir)}
    plan: list[tuple[Path, Path]] = []
    used, targets = set(), set()
    for rel in sorted(set(cur) - set(table)):          # ① 안으로 온 이동
        row = _latest_move(rows, "to", rel)
        if row is None or signatures._id_of(cur[rel]) != row.get("node"):
            continue                                   # 이동한 그 노드가 아니다
        # 사슬을 거슬러 원위치를 찾는다 — 영역 밖이거나 승인본 원적이면 정지
        target, guard, chain = row.get("from"), row["rid"], [row["rid"]]
        while True:
            t = resolve_in_root(target or "")
            if t is None:
                raise ValueError(
                    f"이동 원위치를 해석할 수 없다 — 반려 보류: {rel} ← {target!r}")
            if not _inside(t) or target in table:
                break                                  # 영역 밖 or 영역 안 원적
            prev = _latest_move(rows, "to", target, node=row["node"], before=guard)
            if prev is None:
                t = None                               # 영역 안 생성분 — 평소대로 삭제
                break
            target, guard = prev.get("from"), prev["rid"]
            chain.append(prev["rid"])
        if t is None:
            continue
        if t.exists() or str(t) in targets:
            raise ValueError(
                f"이동 원위치가 이미 차 있다 — 그 자리를 치우면 반려가 이동을 "
                f"되돌린다: {target}")
        plan.append((cur[rel], t))
        used.update(chain); targets.add(str(t))
    for rel in sorted(set(table) - set(cur)):          # ② 밖으로 간 이동
        row = _latest_move(rows, "from", rel)
        if row is None or row["rid"] in used:
            continue
        # 사슬을 따라가 최종 위치를 찾는다
        at, guard = row.get("to"), row["rid"]
        while True:
            nxt = _latest_move(rows, "from", at or "", node=row.get("node"),
                               after=guard)
            if nxt is None:
                break
            at, guard = nxt.get("to"), nxt["rid"]
        out = resolve_in_root(at or "")
        if out is None or not out.is_file() \
                or signatures._id_of(out) != row.get("node"):
            continue                                   # 밖 사본 없음 — 재생성으로 족하다
        home = resolve_in_root(rel)
        if home is None or str(home) in targets:
            continue
        plan.append((out, home))
        targets.add(str(home))
    return plan


def revert(region: str, base: str, expect_work: str, reason: str = "") -> dict:
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
    with mutation_lock():   # 엔진이 내는 변경을 직렬화
        d = resolve_in_root(region)
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
        reg = posix_rel(d, Path(os.path.realpath(ROOT)))
        recs = records()
        st = state(reg, recs)
        if st == "stale":
            raise ValueError(f"stale 영역이다 — 인과 분기 해소가 먼저다: {reg}")
        if st == "unprotected":
            raise ValueError(f"보호 중이 아니다: {reg}")
        cur = approved_hash(reg, recs)
        if cur != base:
            raise ValueError(
                f"검토가 전제한 승인본이 현행이 아니다 (승인본 측): 전제={base} 현행={cur}")
        table = _tree_table_for_region(reg, base)
        if table is None:
            raise ValueError(
                f"승인본 manifest를 해석하지 못했다(부재·영역 불일치) — 복원 불가: {base}")
        discarded = working_tree_hash(reg)    # 복원 **전** — 실제로 버려지는 상태(감사)
        if discarded != expect_work:
            raise ValueError(
                "버릴 변경집합이 검토한 것과 다르다 — 다시 검토하라 (작업본 측): "
                f"검토={expect_work} 현재={discarded}")

        staged = _stage_tree(d, table)     # 준비 — 아직 아무것도 건드리지 않았다
        unmoves = _plan_unmoves(d, table)  # 이동의 원상 복원 계획(무변)
        # 준비를 마치고 **첫 쓰기 직전에** 두 전제를 다시 본다. 복원은 파괴적이므로
        # 전제는 기록의 정직성만이 아니라 파일을 건드리기 전에 유효해야 한다 —
        # 사후 검사는 기록만 막을 뿐 파괴는 이미 끝난 뒤다.
        if approved_hash(reg) != base:
            raise ValueError(f"승인본이 그 사이 바뀌었다(다른 기기 기록 유입) — "
                             f"작업본을 건드리지 않았다: {reg}")
        work = working_tree_hash(reg)
        if work != expect_work:
            raise ValueError("버릴 변경집합이 그 사이 바뀌었다 — 다시 검토하라 "
                             f"(작업본 측): 검토={expect_work} 현재={work}")
        # 여기부터 파괴적이다. 이동의 원상 복원이 먼저다 — 안으로 온 노드는
        # 삭제 순회가 보기 전에 원위치로 나가고, 밖으로 간 노드는 승인본 내용
        # 쓰기가 덮기 전에 제자리로 돌아온다(순서가 뒤면 밖의 내용이 승인본을
        # 덮거나 이동 노드가 삭제된다).
        for now_at, home in unmoves:
            home.parent.mkdir(parents=True, exist_ok=True)
            os.replace(now_at, home)
        _apply_tree(d, table, staged)
        # 복원 완료 최종 확인 — 작업본 tree가 실제로 승인본과 일치할 때만 기록한다.
        # 삭제·쓰기가 부분 실패해 작업본이 여전히 pending인데도 '복원을 마친 뒤에만
        # 기록한다'(Mechanism §3 6항)는 계약이 지켜진 것처럼 감사 대장에 남지 않게
        # 한다(fail-closed) — 실패는 미기록으로 남아 다음 revert가 다시 시도한다.
        if working_tree_hash(reg) != base:
            raise ValueError(
                "복원이 승인본과 일치하지 않는다 — revert를 기록하지 않았다(fail-closed)")
        return ledger_append(APPROVALS, {
            "kind": "revert", "region": reg,
            "base": base, "discarded": discarded, "reason": reason},
            expect=lambda recs2: (            # 복원 중 유입된 승인을 덮지 않는다
                None if approved_hash(reg, recs2) == base else
                f"승인본이 그 사이 바뀌었다(다른 기기 기록 유입) — 다시 보라: {reg}"))


def unprotect(region: str, reason: str = "") -> dict:
    """보호영역 해제 — 미처리 변경집합이 있으면 승인 또는 반려로 먼저 처분한다
    (시행령 §6 5항). 상설 보호영역(위임 Facet·통치 구획·모듈 Facet)의 해제는
    제도 개정과 함께 한다 — 그 정책 판정은 사용자·CLI가 지고, 이 함수는 물리만
    본다."""
    with mutation_lock():   # 엔진이 내는 변경을 직렬화
        d = resolve_in_root(region)
        reg = posix_rel(d, Path(os.path.realpath(ROOT))) if d else str(region).rstrip("/")
        recs = records()
        st = state(reg, recs)
        if st == "stale":
            raise ValueError(f"stale 영역이다 — 인과 분기 해소가 먼저다: {reg}")
        if st == "unprotected":
            raise ValueError(f"보호 중이 아니다: {reg}")
        if st == "pending":
            raise ValueError(
                "미처리 변경집합이 있다 — 해제 전에 승인 또는 반려로 처분하라 (시행령 §6 5항)")
        base = approved_hash(reg, recs)
        return ledger_append(APPROVALS, {
            "kind": "unprotect", "region": reg,
            "base": base, "accepted": None, "reason": reason},
            expect=lambda recs2: (            # 유입된 승인을 해제로 덮지 않는다
                None if (state(reg, recs2) == "clean"
                         and approved_hash(reg, recs2) == base) else
                "그 사이 승인 기록이 바뀌었다(다른 기기 기록 유입) — 해제 전에 "
                f"다시 보라: {reg}"))


def _stage_tree(region_dir: Path, table: dict[str, str]) -> list[tuple[Path, bytes]]:
    """복원할 내용을 **전부 메모리에 올린다** — 아직 아무것도 건드리지 않는다.

    blob이 없거나 **작업본의 경로 구조가 승인본과 충돌하면**(파일↔디렉터리가
    뒤바뀐 자리) 여기서 거부한다. **부분 복원**을 만들지 않는 것이 요점이다
    (반쯤 되돌아간 영역은 사용자가 검토할 수 없다). 준비와 반영을
    가르는 이유는 그 사이가 호출부가 전제를 마지막으로 확인할 자리이기
    때문이다 — 파괴가 시작된 뒤의 확인은 기록만 막을 뿐이다.

    table의 rel이 영역 안이라는 것은 `_tree_table_for_region`이 이미 보장한다
    (그 함수를 거치지 않은 table은 복원의 근거가 아니다)."""
    region_real = Path(os.path.realpath(region_dir))
    staged: list[tuple[Path, bytes]] = []
    for rel, h in sorted(table.items()):
        p = resolve_in_root(rel)
        if p is None:
            raise ValueError(f"승인본 경로가 vault 밖이다 — 복원 거부: {rel}")
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
                    f"바로잡으라: {rel} ({posix_rel(anc, Path(os.path.realpath(ROOT)))})")
        data = _store_get(h)
        if data is None:
            raise ValueError(f"승인본 blob 부재 — 복원 불가: {rel} {h}")
        staged.append((p, data))
    return staged


def _apply_tree(region_dir: Path, table: dict[str, str],
                staged: list[tuple[Path, bytes]]) -> None:
    """준비된 내용을 실제로 반영한다 — **여기부터가 파괴적이다.**

    영역째 사라진 경우의 디렉터리 재생성이 첫 mutation이다. 쓰기 도중 외부에서
    들어오는 변경까지 막지는 못한다 — 그 잔여 창은 이 프로세스 밖(git pull 등)
    이라 닫을 수 없고, 그때는 영역이 pending으로 남아 다음 반려가 새 승인본으로
    복원한다."""
    region_dir.mkdir(parents=True, exist_ok=True)
    # 2) 승인본 내용으로 원자 교체
    for p, data in staged:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    # 3) manifest에 없는 현재 파일 제거 (에이전트가 추가한 것)
    #    삭제 실패(권한 등)는 삼키지 않는다 — 조용히 넘기면 작업본이 승인본과
    #    다른 채로 남는데도 revert가 완료된 것처럼 기록될 수 있다.
    for rel, p in _region_files(region_dir):
        if rel not in table:
            try:
                p.unlink()
            except FileNotFoundError:
                pass                      # 이미 없음 — 정상
            except OSError as e:
                raise ValueError(
                    f"승인본 밖 파일 삭제 실패 — 복원 미완료: {rel} ({e})") from e


# ── 검증기 지원 ──────────────────────────────────────────────────────────

def integrity() -> list[str]:
    """승인 기록부의 정합성 — 형식 위반, 저장소에서 해석되지 않는 승인본,
    stale 영역(사용자 봉합 필요)을 보고한다(시행령 §6 7항 — 검증기가
    불일치를 적발한다). 대장 구조 손상(rid)은 core의 공통 검사가 맡는다."""
    errs = []
    try:
        recs = records()
    except Exception as e:
        return [f"승인 기록부 판독 실패: {e}"]
    for i, r in enumerate(recs):
        if r.get("kind") not in KINDS:
            errs.append(f"행{i+1}: 미정의 kind {r.get('kind')}")
        if not r.get("region"):
            errs.append(f"행{i+1}: region 부재")
    for region in {r.get("region") for r in recs if r.get("region")}:
        st = state(region, recs)
        if st == "stale":
            errs.append(f"영역 stale(인과 극대 비유일) — 사용자 봉합 필요: {region}")
            continue
        if is_protected(region, recs):
            tree = approved_hash(region, recs)
            table = _tree_table_for_region(region, tree) if tree else None
            if table is None:
                errs.append(
                    f"승인본 manifest 해석 불가(부재·영역 불일치): "
                    f"{region} ({tree})")
                continue
            # manifest가 가리키는 **모든 blob의 실재**를 확인한다 — manifest만
            # 해석되고 그것이 가리키는 blob이 없으면 승인본이 clean으로 보여도
            # revert가 나중에 실패한다(복원 불가능한 승인본 적발).
            missing = [rel for rel, h in table.items() if _store_get(h) is None]
            if missing:
                errs.append(
                    f"승인본 blob 부재(복원 불가): {region} — {sorted(missing)[:5]}")
    return errs
