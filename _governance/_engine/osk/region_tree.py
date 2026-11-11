"""osk.region_tree — 영역의 tree와 승인본 보관 (Mechanism §3).

보호영역의 상태를 내용 주소로 본다: 영역의 파일 목록과 그 해시의 표가 tree이고, tree의 해시가
영역의 상태다. 승인된 tree와 그 파일은 내용 주소 저장소(`approved/`)에 보관한다. 무엇이 승인됐는지는
모른다 — 그것은 승인 기록부(`approvals`)가 정한다."""
from __future__ import annotations
import json, os, re
from pathlib import Path
from .core import sha256_bytes, sha256_file, posix_rel, atomic_write, resolve_in_root
from .vault import Vault


def store_dir(vault: Vault) -> Path:
    """승인본 보관소 — 내용 주소 blob과 manifest."""
    return vault.ledger / "approved" / "objects"


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")   # 저장소 접근의 유일 형식


# 영역 tree에서 제외하는 이름 — **Mechanism §3 4항이 열거한다.** 저장소
# 살림살이, 대장 자신(승인본이 대장을 담으면 승인이 자기 자신을 포함하는
# 순환이 된다), 그리고 append-only 기록과 기기 공유 기억이다.
#
# `_raw` 제외는 사용자 판정(2026-08-19)이었고 `_scope_memory`는 같은 근거가 더
# 강하게 적용되는 자리인데 빠져 있었다: 케이던스가 15턴마다 쓰므로 그 구획을
# 담은 영역은 계속 pending이 되고, **반려가 다른 기기의 공유 기억까지 지운다**.
# 되돌릴 것이 없는 자리를 되돌림의 대상으로 두지 않는다.
SKIP_DIRS = {".git", ".venv", "__pycache__", "_ledger", "_cited", "_raw",
              "_scope_memory"}


# ── 영역 tree (내용 주소) ────────────────────────────────────────────────

def region_files(vault: Vault, region_dir: Path) -> list[tuple[str, Path]]:
    """영역 안 정규 파일 전수 — (vault 상대 POSIX 경로, 절대 경로), 경로
    오름차순. 저장소 살림살이·대장·`_raw`는 뺀다(위 `SKIP_DIRS`).

    realpath가 제 이름과 다른 디렉터리(정션·심볼릭 링크)로는 내려가지 않는다 —
    그 너머는 다른 구획이나 vault 밖이지 이 영역이 아니다. 따라가면 승인·상태가
    남의 파일을 영역 것으로 삼고, 반려가 그 파일을 지운다(실측)."""
    out = []
    root_real = Path(os.path.realpath(vault.root))
    def scan_error(error):
        if isinstance(error, FileNotFoundError) and Path(error.filename) == region_dir:
            return  # 영역 자체의 삭제는 빈 작업본이다. 반려가 되살릴 수 있다.
        raise error  # 읽지 못한 구획을 빈 승인본으로 기록하지 않는다.

    for cur, dirs, files in os.walk(region_dir, onerror=scan_error):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                         and not d.startswith(".")
                         and os.path.realpath(os.path.join(cur, d))
                         == os.path.join(cur, d))
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


def manifest_blob(entries: list) -> bytes:
    """manifest의 **정본 직렬화** — 공백 없는 UTF-8 JSON. 생성(작업본 해시·
    `store_tree`)과 판독이 같은 함수를 쓰므로 정본 형상의 정의가 한 곳이다."""
    return json.dumps(entries, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def working_tree_hash(vault: Vault, region: str) -> str | None:
    """작업본의 영역 tree 해시 — 판정할 수 없으면 None.

    세 경우를 구분한다. ①디렉터리면 그 tree의 해시. ②**부재**면 빈 tree의
    해시 — 영역째 지워진 것은 판정 불능이 아니라 "파일이 하나도 없는 상태"이며,
    그렇게 봐야 반려가 그 사고를 되돌릴 수 있다. ③그 경로에 디렉터리가 아닌
    무언가가 있으면 None — 부재와 접으면 빈 승인본을 가진 영역이 구조가 깨진
    채로 clean으로 오판되고 해제까지 허용된다(fail-closed)."""
    d = resolve_in_root(vault, region)
    if d is None:
        return None
    if d.is_dir():
        try:
            return sha256_bytes(manifest_blob(
                [[rel, sha256_file(p)] for rel, p in region_files(vault, d)]))
        except OSError:
            return None  # 불완전 관측은 판정 불능이며 clean이 아니다.
    if d.exists():
        return None                       # 디렉터리가 아닌 객체 — 판정 불능
    return sha256_bytes(manifest_blob([]))          # 부재 = 빈 작업본


# ── 내용 주소 저장소 ─────────────────────────────────────────────────────

def _obj_path(vault: Vault, digest: str) -> Path:
    """`sha256:<64hex>` → 저장 경로 `_ledger/approved/objects/<2>/<나머지>`.

    형식을 진입점에서 강제한다 — 손상된 대장에서 온 쓰레기 값이 경로 계산에
    섞이지 않고 그 자리에서 거부된다(fail-closed)."""
    if not (isinstance(digest, str) and _DIGEST_RE.match(digest)):
        raise ValueError(f"부적격 digest — 저장소 접근 거부: {digest!r}")
    hexd = digest.split(":", 1)[1]
    return store_dir(vault) / hexd[:2] / hexd[2:]


def store_put(vault: Vault, data: bytes) -> str:
    """바이트를 내용 주소로 보관하고 그 digest를 돌려준다. 같은 내용은 한 번만
    저장된다(멱등) — 다기기 병합은 합집합으로 자명하다.

    이미 그 경로에 객체가 있으면 **그 내용이 digest와 일치하는지 확인**하고,
    동기화 충돌·디스크 손상·변조로 어긋나 있으면 올바른 bytes로 다시 쓴다 —
    내용 주소라 정본 내용이 유일하게 정해지므로 치유가 자명하다."""
    digest = sha256_bytes(data)
    dst = _obj_path(vault, digest)
    if dst.exists():
        try:
            if sha256_bytes(dst.read_bytes()) == digest:
                return digest             # 정상 객체 — 멱등 반환
        except OSError:
            pass                          # 판독 불가 → 아래 원자 재기록으로 치유
    # 원자 교체 — 손상 객체를 정상으로 치유. 새 객체 디렉터리와 엔트리를
    # 내구화한 뒤에야 돌아간다: 호출부가 이 digest를 대장에 적으므로, 순서가
    # 뒤집히면 전원 차단 뒤 대장이 사라진 객체를 가리킨다.
    atomic_write(dst, data)
    return digest


def _obj_digest_of_path(rel: str) -> str | None:
    """저장 경로에서 그 객체가 **가져야 할** digest를 되읽는다.

    `_obj_path`의 역이다 — `…/objects/<2>/<62>` → `sha256:<64hex>`. 이름이
    형식에 맞지 않으면 None(저장소 소관이 아닌 파일)."""
    parts = str(rel).replace(chr(92), "/").split("/")
    if len(parts) < 2:
        return None
    a, b = parts[-2], parts[-1]
    d = "sha256:" + a + b
    return d if _DIGEST_RE.match(d) else None


def reconcile_store(vault: Vault, index_bytes, apply_fn=None) -> dict:
    """내용 주소 저장소를 **파일 이름을 심판으로** 판독·이행한다.

    EOL 이행에서 `-text`를 얻은 뒤의 문제다. 그 구획은 clean 필터가 사라지므로
    작업 트리를 다시 stage하면 그 바이트가 그대로 blob이 되고, 반대로 통째로
    빼면 blob이 그대로 작업 트리로 펼쳐진다. **어느 쪽도 무조건은 틀렸다**:

      · 구판 checkout이 정상 LF 객체의 작업 트리만 CRLF로 펼쳐 놓았으면,
        stage하는 순간 옳던 blob이 CRLF로 바뀌어 이름과 내용이 갈린다 —
        `store_get`이 실패하고 그 객체를 참조하는 승인본이 해석 불능이 된다.
      · 반대로 원본이 CRLF였던 객체는 구판 정규화로 blob 쪽이 이미 깨져 있고
        작업 트리(왕복 대칭으로 살아남은 쪽)가 옳다 — 통째로 빼면 재전개가
        그 깨진 blob을 작업 트리에 덮어써 양쪽 다 잃는다.

    내용 주소에는 심판이 있다: **파일 이름이 곧 내용의 sha256이다.** 양쪽을
    digest에 대조해 맞는 쪽만 남기고, 둘 다 아니면 그 객체는 손댈 근거가 없으니
    **아무것도 하지 않고 멈춘다**(fail-closed) — 어느 쪽을 골라도 추측이다.

    `index_bytes(rel)`은 그 경로의 git 색인 내용을 돌려주거나(bytes) 추적되지
    않으면 None. `apply_fn(kind, rel)`이 주어지면 판정을 이행한다
    (`"stage"`=작업 트리를 색인에, `"restore"`=색인을 작업 트리에). 손상이
    하나라도 있으면 **어떤 이행도 하지 않는다** — 절반만 고친 저장소는
    고치기 전보다 판독하기 어렵다.

    반환: {"ok": bool, "stage": [rel…], "restore": [rel…], "broken": [rel…],
           "checked": int}
    """
    root = Path(os.path.realpath(vault.root))
    out = {"stage": [], "restore": [], "broken": [], "checked": 0}
    if not store_dir(vault).is_dir():
        return {"ok": True, **out}
    for f in sorted(store_dir(vault).rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        rel = posix_rel(f, root)
        want = _obj_digest_of_path(rel)
        if want is None:
            out["broken"].append(rel)      # 저장소 안의 이름 없는 파일 — 판정 불능
            continue
        out["checked"] += 1
        work = f.read_bytes()
        idx = index_bytes(rel)
        w_ok = sha256_bytes(work) == want
        i_ok = idx is not None and sha256_bytes(idx) == want
        if i_ok and w_ok:
            continue                       # 둘 다 옳다 — 건드릴 것이 없다
        if w_ok:
            out["stage"].append(rel)       # 작업 트리가 정본 — 색인을 고친다
        elif i_ok:
            out["restore"].append(rel)     # 색인이 정본 — 작업 트리를 고친다
        else:
            out["broken"].append(rel)
    ok = not out["broken"]
    if ok and apply_fn is not None:
        for rel in out["stage"]:
            apply_fn("stage", rel)
        for rel in out["restore"]:
            apply_fn("restore", rel)
    return {"ok": ok, **out}


def store_get(vault: Vault, digest: str) -> bytes | None:
    try:
        p = _obj_path(vault, digest)            # 부적격 digest는 ValueError
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


def store_tree(vault: Vault, region_dir: Path) -> str:
    """영역의 작업본 전체(manifest + 각 파일 내용)를 저장소에 넣고 tree 해시를
    돌려준다.

    각 파일을 **한 번만** 읽어, 그 **같은 bytes**를 blob으로 저장하고 그 해시를
    manifest에 싣는다 — 해시 판독과 blob 저장 판독이 갈려 승인본이 복원 불가능
    (manifest는 hash(A)를 가리키나 저장된 blob은 hash(B))해지는 일이 없다.
    판독 순서·직렬화는 `working_tree_hash`와 같아 tree 해시가 동일하다."""
    entries = []
    for rel, p in region_files(vault, region_dir):
        data = p.read_bytes()          # 파일당 유일 판독
        h = store_put(vault, data)           # 그 bytes를 그대로 박제(반환 = 그 해시)
        entries.append([rel, h])
    return store_put(vault, manifest_blob(entries))


def tree_table(vault: Vault, tree_hash: str) -> dict[str, str] | None:
    """tree 해시 → {rel: sha256}. 저장소에 manifest가 없으면 None."""
    blob = store_get(vault, tree_hash)
    if blob is None:
        return None
    try:
        entries = json.loads(blob.decode("utf-8"))
        return {str(rel): str(h) for rel, h in entries}
    except (ValueError, TypeError):
        return None


def excluded_rel(rel: str, region: str) -> bool:
    """그 경로가 `region` **안에서** 지금 규칙(§3 4항)의 제외 구획에 드는가.

    `region_files`의 걸러내기와 같은 판정을 경로 문자열에 대해 한다 — 작업본
    스캔과 승인본 판독이 같은 규칙을 써야 둘이 비교 가능하다. 그래서 판정은
    **영역 루트 아래**의 구성요소만 본다: `region_files(region_dir)`는 루트
    자신에는 걸러내기를 걸지 않고 그 아래 자식 디렉터리와 파일 이름만 거른다
    (§3 4항도 "영역 **안에** 있어도"라고 적는다 — 루트는 판정 대상이 아니다).

    루트 경로까지 판정에 넣으면 두 해석이 갈린다. 루트에 제외 이름이나 점
    접두가 든 영역 — 개정 전에 합법적으로 지정된 `= …/_scope_memory`, 그리고
    `protect`가 **지금도 허용하는** `.foo` — 에서 승인본 table의 모든 행이
    빠져 `table={}`가 되는데, 삭제 순회는 `region_files`로 그 파일들을 정상
    열거하므로 **반려가 영역을 통째로 지운다.** 그 뒤 이행 논리가 빈 manifest를
    새 `base`로 적어 삭제를 clean한 반려로 확정한다(4차 리뷰, 실측 재현)."""
    r = str(region).rstrip("/")
    s = str(rel)
    if not r or not s.startswith(r + "/"):
        return False              # 영역 밖 — 여기서 판정할 자리가 아니다
    parts = s[len(r) + 1:].split("/")
    return any(p in SKIP_DIRS or p.startswith(".") for p in parts)


def legacy_excluded(vault: Vault, tree_hash: str, region: str) -> list[str]:
    """저장된 승인본 manifest에 든 **지금은 제외되는** 항목들.

    §3 4항 개정 전에 지정된 영역의 승인본은 `_scope_memory/*`를 담고 있을 수
    있다. 그 항목은 이제 영역 tree에 들지 않으므로, 그대로 두면 복원 계획이
    **현재의 공유 기억을 과거 승인본으로 덮는다.** 어느 자리가 그런지 여기서
    한 번 답하고, 판독(`tree_table_for_region`)과 반려가 같은 답을 쓴다."""
    table = tree_table(vault, tree_hash)
    return sorted(r for r in (table or {}) if excluded_rel(r, region))


def tree_table_for_region(vault: Vault, region: str, tree_hash: str) -> dict[str, str] | None:
    """그 **영역의** 승인본 table — 형상이 정본이고 모든 rel이 `region/` 아래일
    때만 반환한다(아니면 None).

    `store_tree(region_dir)`는 영역 디렉터리를 걸어 만들므로 정본 승인본의 rel은
    전부 그 영역 안이다. 이 결속이 없으면 영역과 승인본이 어긋난 조합에서
    **권한 판정은 성립인데 복원은 거부되는** 상태가 생긴다
    (`file_in_region_baseline`은 자기 파일 항목만 보고, 복원은 영역 밖 rel을 가진
    승인본을 근거로 삼을 수 없다). 세 소비자(integrity·권한 검사·반려)가 같은
    해석기를 쓰도록 결속을 이 한 곳에 둔다."""
    table = tree_table(vault, tree_hash)
    if table is None:
        return None
    r = str(region).rstrip("/")
    if not r or any(not rel.startswith(r + "/") for rel in table):
        return None                   # 영역 밖 항목 — 그 영역의 tree가 아니다
    # **지금 규칙으로 해석한다.** 개정 전 승인본이 담고 있을 수 있는 제외 구획
    # 항목(`_scope_memory/*`)을 걸러 낸다 — 걸러 내지 않으면 복원 계획이 현재의
    # 공유 기억 위에 과거 승인본을 쓰고, 그 뒤 최종 확인이 새 규칙으로 계산되어
    # 어차피 실패한다(기록은 안 남는데 자료만 덮인다). 삭제 쪽은 위험이 없다 —
    # `_apply_tree`의 삭제 순회가 `region_files`를 쓰므로 제외 구획을 애초에
    # 열거하지 않는다.
    return {rel: h for rel, h in table.items()
            if not excluded_rel(rel, r)}
