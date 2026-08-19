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
                   causal_maxima, resolve_one)

APPROVALS = LEDGER / "approvals.jsonl"
STORE = LEDGER / "approved" / "objects"       # 내용 주소 blob·manifest 보관
# ROOT 기준 저장소의 **정해진** 상대 경로 — 봉쇄의 앵커는 realpath(STORE)가
# 아니라 realpath(ROOT)/이 경로다(STORE 루트 symlink를 trust root로 승격시키지
# 않기 위해; core.resolve_in_root가 realpath(ROOT)를 정본 앵커로 삼는 것과 같다).
_STORE_REL = os.path.relpath(STORE, ROOT)
_STORE_PARTS = tuple(x for x in _STORE_REL.replace("\\", "/").split("/")
                     if x and x != ".")
KINDS = ("protect", "unprotect", "approve", "revert")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")   # 저장소 접근의 유일 형식

# 저장소 I/O를 **검증과 사용이 같은 fd 체인에 결속**되게 열 수 있는가 —
# O_NOFOLLOW로 각 성분을 열면 검사 시점과 I/O 시점 사이 symlink 교체
# (TOCTOU)로 우회할 수 없다. Windows 등 dir_fd 미지원 플랫폼에서는 realpath
# 선검사(_obj_path)로 best-effort 봉쇄한다.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_ODIR = getattr(os, "O_DIRECTORY", 0)
_DIRFD_SAFE = (_NOFOLLOW != 0 and _ODIR != 0
               and {os.open, os.mkdir, os.unlink} <= os.supports_dir_fd)
_CHUNK = 1 << 20

# 영역 tree에서 제외하는 이름 — 저장소 살림살이와 대장 자신(승인본이 대장을
# 담으면 승인이 자기 자신을 포함하는 순환이 된다).
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


def _rel_parts(rel: str) -> list[str]:
    r"""정규 vault 상대 POSIX 경로 → 성분 목록. 정본 형상이 아니면 거부한다.

    정본 rel은 `posix_rel()`이 낳는 형상뿐이다 — 빈 문자열·절대경로·백슬래시·
    빈 성분·`.`·`..`이 없다. 이 rel→성분 변환이 manifest 파서와 복원 I/O가
    공유하는 유일한 지점이므로, 형상 강제를 여기 둔다.

    `..`을 남기면 fd 체인이 그 성분에서 **실제 부모로 올라가** 영역 밖에 쓴다
    — `O_NOFOLLOW`는 symlink만 막고 `..` 이동은 막지 못한다. 문자열 단계에서
    거부하고 물리 봉쇄로 이중화하는 규율은 `update._within`과 같다."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        raise ValueError(f"정규 vault 상대 경로가 아니다: {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"정규 vault 상대 경로가 아니다: {rel!r}")
    return parts


def _manifest_blob(entries: list) -> bytes:
    """manifest의 **정본 직렬화** — 공백 없는 UTF-8 JSON. 생성(`_manifest_bytes`·
    `_store_tree`)과 판독 검증(`_tree_table`)이 같은 함수를 쓰므로 정본 형상의
    정의가 한 곳에만 있다."""
    return json.dumps(entries, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _manifest_bytes(region_dir: Path) -> tuple[bytes, dict[str, str]]:
    """영역의 manifest — (rel, sha256) 쌍을 경로 오름차순으로 담은 공백 없는
    UTF-8 JSON. 반환: (manifest 바이트, {rel: sha256})."""
    entries = []
    table: dict[str, str] = {}
    for rel, p in _region_files(region_dir):
        h = sha256_file(p)
        entries.append([rel, h])
        table[rel] = h
    return _manifest_blob(entries), table


def working_tree_hash(region: str) -> str | None:
    """작업본의 영역 tree 해시 — 영역 디렉터리가 없으면 None."""
    d = resolve_in_root(region)
    if d is None or not d.is_dir():
        return None
    return sha256_bytes(_manifest_bytes(d)[0])


# ── 내용 주소 저장소 ─────────────────────────────────────────────────────

def _obj_path(digest: str) -> Path:
    """`sha256:<64hex>` → 저장 경로 `_ledger/approved/objects/<2>/<나머지>`.

    digest는 신뢰 밖 입력일 수 있다(다기기 병합으로 유입된 manifest·대장의
    `accepted`/`base`·manifest 안 blob 해시). 형식을 강제하지 않으면 조작된
    digest의 `../`·절대경로 성분이 STORE를 버리고 vault 밖 파일을 읽게 할 수
    있으므로(그리고 revert가 그 내용을 영역 안으로 복사), 진입점에서 정확히
    `sha256:<64 소문자 hex>`를 강제하고 계산된 경로가 STORE 안에 봉쇄되는지
    재검증한다 — 어긋나면 거부(fail-closed)."""
    if not (isinstance(digest, str) and _DIGEST_RE.match(digest)):
        raise ValueError(f"부적격 digest — 저장소 접근 거부: {digest!r}")
    hexd = digest.split(":", 1)[1]
    p = STORE / hexd[:2] / hexd[2:]
    # 물리 봉쇄 — 경로 **어느 성분의 symlink**(STORE 루트·shard·최종 객체)도
    # I/O를 vault 밖으로 재지정하지 못하게 realpath 정체성으로 확인한다
    # (core.resolve_in_root와 같은 기법 · Mechanism §1-2 5항). 앵커는 정본
    # 신뢰 루트인 realpath(ROOT) 아래의 정해진 canonical 저장소 경로다 —
    # realpath(STORE)를 앵커로 삼으면 STORE 자신이 밖을 가리키는 symlink일 때
    # 외부를 trust root로 승격시킨다. 계산 경로의 realpath가 canonical 기대
    # 위치와 정확히 일치하지 않으면 거부(fail-closed). 양쪽 다 realpath라 정본
    # prefix의 정상 symlink(예: /tmp→/private/tmp)에는 오탐하지 않는다.
    store_canon = os.path.normpath(os.path.join(os.path.realpath(ROOT), _STORE_REL))
    expected = os.path.join(store_canon, hexd[:2], hexd[2:])
    if os.path.realpath(p) != expected:
        raise ValueError(f"저장소 밖·symlink 재지정 경로 — 거부: {digest}")
    return p


def _open_dir_chain(parts, create: bool) -> int | None:
    """realpath(ROOT)에서 `parts`(vault 상대 경로 성분)를 따라 **각 성분을
    O_NOFOLLOW로** 열어 그 디렉터리 fd를 돌려준다 — 검증(비-symlink)과 사용이
    같은 fd 체인에 결속돼 check-then-use TOCTOU가 없다. 어느 성분이든 symlink
    이면(공격) 열기가 ELOOP로 실패하고, 없으면 create=False에서 None,
    create=True에서 O_NOFOLLOW 규율로 만든다. 저장소·영역 복원이 공유한다.
    dir_fd 미지원 플랫폼에서는 부르지 않는다(_DIRFD_SAFE)."""
    try:
        fd = os.open(os.path.realpath(ROOT), _ODIR | _NOFOLLOW)
    except OSError:
        return None
    try:
        for part in parts:
            while True:
                try:
                    nxt = os.open(part, _ODIR | _NOFOLLOW, dir_fd=fd)
                    break
                except FileNotFoundError:
                    if not create:
                        return None
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass              # 경쟁 생성 — 재시도로 연다(symlink면 ELOOP)
            os.close(fd)
            fd = nxt
        return fd
    except OSError:
        os.close(fd)
        return None                       # 성분이 symlink(ELOOP) 등 — fail-closed


def _open_store_dirfd(create: bool) -> int | None:
    """저장소 objects 디렉터리 fd (nofollow 체인)."""
    return _open_dir_chain(_STORE_PARTS, create)


def _readall_fd(fd: int) -> bytes:
    chunks = []
    while True:
        b = os.read(fd, _CHUNK)
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks)


def _read_object_nofollow(hexd: str) -> bytes | None:
    base = _open_store_dirfd(create=False)
    if base is None:
        return None
    try:
        try:
            sfd = os.open(hexd[:2], _ODIR | _NOFOLLOW, dir_fd=base)
        except OSError:
            return None                   # shard 부재·symlink
        try:
            try:
                ofd = os.open(hexd[2:], os.O_RDONLY | _NOFOLLOW, dir_fd=sfd)
            except OSError:
                return None               # 객체 부재·symlink
            try:
                return _readall_fd(ofd)
            finally:
                os.close(ofd)
        finally:
            os.close(sfd)
    finally:
        os.close(base)


def _write_object_nofollow(digest: str, hexd: str, data: bytes) -> None:
    base = _open_store_dirfd(create=True)
    if base is None:
        raise ValueError("승인 저장소 경로 봉쇄 실패 — 쓰지 않았다")
    try:
        while True:                       # shard 열기/만들기 (nofollow)
            try:
                sfd = os.open(hexd[:2], _ODIR | _NOFOLLOW, dir_fd=base)
                break
            except FileNotFoundError:
                try:
                    os.mkdir(hexd[:2], dir_fd=base)
                except FileExistsError:
                    pass              # 경쟁 생성 — 재시도(symlink로 만들어졌으면 ELOOP)
            except OSError as e:
                # shard가 symlink(ELOOP) 등 — 봉쇄 실패를 명시적 거부로 올린다
                raise ValueError(f"저장소 shard 봉쇄 실패(symlink 재지정 등): {e}") from e
        try:
            obj = hexd[2:]
            for _ in range(4):            # 경쟁·손상에 대한 유한 재시도
                try:                      # 기존 객체가 정상이면 멱등
                    efd = os.open(obj, os.O_RDONLY | _NOFOLLOW, dir_fd=sfd)
                    try:
                        if sha256_bytes(_readall_fd(efd)) == digest:
                            return
                    finally:
                        os.close(efd)
                except OSError:
                    pass                  # 부재·symlink(ELOOP)
                try:                      # 손상·symlink·부재 → 제거 후 새로 만든다
                    os.unlink(obj, dir_fd=sfd)
                except OSError:
                    pass
                try:
                    wfd = os.open(obj, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                  | _NOFOLLOW, 0o600, dir_fd=sfd)
                except FileExistsError:
                    continue              # 경쟁 생성 — 위에서 정상 여부 재확인
                try:
                    mv = memoryview(data)
                    while mv:
                        mv = mv[os.write(wfd, mv[:_CHUNK]):]
                    os.fsync(wfd)
                    return
                finally:
                    os.close(wfd)
            raise ValueError("승인 저장소 객체 기록 경쟁 미수렴 — 쓰지 않았다")
        finally:
            os.close(sfd)
    finally:
        os.close(base)


def _store_put(data: bytes) -> str:
    """바이트를 내용 주소로 보관하고 그 digest를 돌려준다. 같은 내용은 한 번만
    저장된다(멱등) — 다기기 병합은 합집합으로 자명하다.

    경로 봉쇄와 실제 I/O를 **같은 O_NOFOLLOW fd 체인**에 결속한다 — 검사 뒤
    symlink 교체(TOCTOU)로 vault 밖에 쓰는 일이 없다. 이미 있는 객체가 digest와
    일치하면 멱등 반환, 손상·symlink면 제거 후 다시 쓴다(내용 주소라 정본 내용이
    유일하게 정해져 치유가 자명하다)."""
    digest = sha256_bytes(data)
    hexd = digest.split(":", 1)[1]
    if _DIRFD_SAFE:
        _write_object_nofollow(digest, hexd, data)
        return digest
    # 폴백(dir_fd 미지원) — realpath 선검사 + 경로 I/O (best-effort)
    dst = _obj_path(digest)
    if dst.exists():
        try:
            if sha256_bytes(dst.read_bytes()) == digest:
                return digest
        except OSError:
            pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return digest


def _store_get(digest: str) -> bytes | None:
    if not (isinstance(digest, str) and _DIGEST_RE.match(digest)):
        return None                      # 신뢰 밖 digest — 부재로 취급(fail-closed)
    hexd = digest.split(":", 1)[1]
    if _DIRFD_SAFE:
        data = _read_object_nofollow(hexd)
    else:
        try:
            p = _obj_path(digest)
            data = p.read_bytes() if p.is_file() else None
        except (ValueError, OSError):
            return None
    if data is None:
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
    manifest에 싣는다 — manifest가 가리키는 해시와 저장된 blob이 언제나 같은
    판독에서 나오므로, `_manifest_bytes`의 해시 판독과 blob 저장 판독이 갈려
    승인본이 복원 불가능(manifest는 hash(A)를 가리키나 저장된 blob은 hash(B))
    해지는 일이 없다. 판독 순서·직렬화는 `_manifest_bytes`와 같아 tree 해시가
    동일하다(state 비교의 정합)."""
    entries = []
    for rel, p in _region_files(region_dir):
        data = p.read_bytes()          # 파일당 유일 판독
        h = _store_put(data)           # 그 bytes를 그대로 박제(반환 = 그 해시)
        entries.append([rel, h])
    return _store_put(_manifest_blob(entries))


def _tree_table(tree_hash: str) -> dict[str, str] | None:
    """tree 해시 → {rel: sha256}. 저장소에 manifest가 없거나 **정본 형상이
    아니면** None — 그러면 integrity·권한 검사·revert가 모두 fail-closed 한다.

    manifest는 신뢰 밖 입력이다(다기기 병합으로 유입된 대장·저장소 객체).
    정본 엔진이 만드는 manifest는 `_store_tree`가 낳는 형상 하나뿐이므로 —
    경로 오름차순, 경로당 정확히 한 항목, 각 항목이 `[rel, sha256:<64hex>]`,
    공백 없는 UTF-8 JSON — 여기서 그 형상을 그대로 강제한다(parse, don't
    validate): 통과한 table은 정상 `protect`/`approve`가 만들 수 있었던
    manifest임이 보장된다. 느슨하게 접으면(예: dict 축약의 last-wins) 중복
    경로를 담은 비정규 manifest가 integrity를 통과하고 위임 권위의 근거가
    될 수 있다."""
    blob = _store_get(tree_hash)
    if blob is None:
        return None
    try:
        entries = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(entries, list):
        return None
    table: dict[str, str] = {}
    for item in entries:
        if not (isinstance(item, list) and len(item) == 2):
            return None
        rel, h = item
        if not (isinstance(h, str) and _DIGEST_RE.match(h)):
            return None
        try:
            _rel_parts(rel)           # 정규 vault 상대 POSIX 경로만(`..` 금지)
        except ValueError:
            return None
        if rel in table:
            return None               # 경로 중복 — 정본 manifest에 없는 형상
        table[rel] = h
    rels = list(table)
    if rels != sorted(rels):
        return None                   # 정렬이 정본과 다름
    if _manifest_blob(entries) != blob:
        return None                   # 직렬화가 정본과 다름
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
    r = region_record(region, recs)
    return bool(r) and r.get("kind") != "unprotect"


def protected_regions() -> list[str]:
    """현재 보호 중인 영역 — 각 영역의 인과 극대가 unprotect가 아닌 것."""
    recs = records()
    out = []
    for region in {r.get("region") for r in recs if r.get("region")}:
        if is_protected(region, recs):
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


def region_of(path: Path | str) -> str | None:
    """경로를 포함하는 **가장 안쪽** 보호영역 — 없으면 None. 권한 소비자가
    노드 하나의 보호 여부를 묻는 입구다."""
    p = resolve_in_root(path)
    if p is None:
        return None
    rel = posix_rel(p, Path(os.path.realpath(ROOT)))
    best = None
    for region in protected_regions():
        r = region.rstrip("/")
        if rel == r or rel.startswith(r + "/"):
            if best is None or len(r) > len(best):
                best = r
    return best


def file_matches_baseline(path: Path | str) -> bool:
    """이 파일의 현재 내용이 그 파일이 속한 보호영역의 **승인본**과 일치하는가.
    보호영역 밖이면 True(제약 없음). 보호영역 안인데 승인본에 없거나 해시가
    다르면 False — 권한·효력의 근거는 승인본뿐이다(헌법 10조 4항)."""
    region = region_of(path)
    if region is None:
        return True
    return file_in_region_baseline(region, path)


def file_in_region_baseline(region: str, path: Path | str) -> bool:
    """지정한 **정확한** region의 승인본 안에 이 파일이 그 해시로 들어 있는가.
    `region_of`(가장 안쪽 보호영역)가 아니라 호출자가 지정한 region으로 판정한다
    — 권한 검사는 위임 Facet **자체**의 승인본 반영이어야 하며, Facet은 미보호인데
    그 하위 디렉터리만 보호된 경우로 우회되지 않는다(헌법 7조 3항). region이
    보호 중이 아니거나 파일이 그 승인본에 없거나 해시가 다르면 False(fail-closed)."""
    if not is_protected(region):
        return False
    p = resolve_in_root(path)
    if p is None or not p.is_file():
        return False
    tree = approved_hash(region)
    table = _tree_table(tree) if tree else None
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
        "base": None, "accepted": accepted, "reason": reason})


def approve(region: str, base: str, expect_work: str,
            reason: str = "") -> dict:
    """승인 — **양측 CAS**(시행령 §6 3항): 검토가 전제한 승인본(`base`)이
    현행이고(승인본 측), 검토한 작업본(`expect_work`)이 승인 시점에도 그대로일
    때만(작업본 측) 성립한다. 어느 한쪽이 그 사이 달라지면 승인하지 않고 사용자의
    새 검토로 넘긴다 — 검토 뒤 에이전트가 더 쓴 변경까지 승인되는 일이 없게 한다.
    승인본을 검토한 작업본으로 갱신한다.

    `expect_work`는 **필수**다 — 기본값을 두면 내부 호출이 작업본 측 CAS를 건너뛰어
    권위의 핵심 불변식이 호출 관례에만 의존하게 된다. None으로 부르면 즉시 거부한다."""
    if expect_work is None:
        raise ValueError(
            "expect_work(검토한 작업본 tree 해시)는 필수다 — 양측 CAS를 건너뛸 수 없다")
    d = resolve_in_root(region)
    if d is None or not d.is_dir():
        raise ValueError(f"영역이 vault 안의 디렉터리가 아니다: {region}")
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
            f"검토가 전제한 승인본이 현행이 아니다 (승인본 측 CAS): 전제={base} 현행={cur}")
    # 작업본을 **한 번** 읽어 박제하고, 그 **같은** tree를 작업본 측 CAS에
    # 쓴다 — 검사한 상태와 박제한 상태가 언제나 동일해야 검토하지 않은 변경이
    # 끼어들 창(TOCTOU)이 없다. 별도 판독으로 검사하고 또 다른 판독을 박제하면
    # 그 사이 에이전트가 쓴 상태가 승인본이 된다.
    accepted = _store_tree(d)         # 승인 시점 작업본을 박제·해시(단일 판독)
    if accepted != expect_work:
        raise ValueError(
            "검토한 작업본이 그 사이 바뀌었다 — 다시 검토하라 (작업본 측 CAS)")
    if accepted == cur:
        raise ValueError("변경집합이 없다 — 승인할 pending 차이가 없다")
    return ledger_append(APPROVALS, {
        "kind": "approve", "region": reg,
        "base": base, "accepted": accepted, "reason": reason})


def revert(region: str, reason: str = "") -> dict:
    """반려 — 작업본을 승인본으로 **원상 복원**한다(시행령 §6 6항 · Mechanism
    §3 6항: 파일 복원을 마친 뒤에만 기록한다 — 기록에 실패해도 작업본이
    승인본과 같아졌으므로 clean으로 수렴할 뿐이다)."""
    d = resolve_in_root(region)
    if d is None or not d.is_dir():
        raise ValueError(f"영역이 vault 안의 디렉터리가 아니다: {region}")
    reg = posix_rel(d, Path(os.path.realpath(ROOT)))
    recs = records()
    st = state(reg, recs)
    if st == "stale":
        raise ValueError(f"stale 영역이다 — 인과 분기 해소가 먼저다: {reg}")
    if st == "unprotected":
        raise ValueError(f"보호 중이 아니다: {reg}")
    base = approved_hash(reg, recs)
    table = _tree_table(base) if base else None
    if table is None:
        raise ValueError(
            f"승인본 manifest를 해석하지 못했다(부재 또는 비정규) — 복원 불가: {base}")
    discarded = working_tree_hash(reg)    # 복원 **전** — 실제로 버려지는 상태(감사)
    _restore_tree(d, table)
    # 복원 완료 최종 확인 — 작업본 tree가 실제로 승인본과 일치할 때만 기록한다.
    # 삭제·쓰기가 부분 실패해 작업본이 여전히 pending인데도 '복원을 마친 뒤에만
    # 기록한다'(Mechanism §3 6항)는 계약이 지켜진 것처럼 감사 대장에 남지 않게
    # 한다(fail-closed) — 실패는 미기록으로 남아 다음 revert가 다시 시도한다.
    if working_tree_hash(reg) != base:
        raise ValueError(
            "복원이 승인본과 일치하지 않는다 — revert를 기록하지 않았다(fail-closed)")
    return ledger_append(APPROVALS, {
        "kind": "revert", "region": reg,
        "base": base, "discarded": discarded, "reason": reason})


def unprotect(region: str, reason: str = "") -> dict:
    """보호영역 해제 — 미처리 변경집합이 있으면 승인 또는 반려로 먼저 처분한다
    (시행령 §6 5항). 상설 보호영역(위임 Facet·통치 구획·모듈 Facet)의 해제는
    제도 개정과 함께 한다 — 그 정책 판정은 사용자·CLI가 지고, 이 함수는 물리만
    본다."""
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
        "base": base, "accepted": None, "reason": reason})


def _write_file_nofollow(rel: str, data: bytes) -> None:
    """vault 상대 `rel` 파일에 `data`를 쓴다 — realpath(ROOT)에서 부모까지 각
    성분을 O_NOFOLLOW로 열어(없으면 만들어) 그 dir_fd 상대로 O_NOFOLLOW 생성한다.
    부모 성분이 symlink면 ELOOP로 거부되므로, 검증 뒤 부모를 symlink로 교체해도
    (TOCTOU) I/O가 vault 밖으로 새지 않는다. 저장소 접근자와 같은 규율."""
    parts = _rel_parts(rel)           # 정규형 위반은 여기서 거부(성분 `..` 없음)
    parent = _open_dir_chain(parts[:-1], create=True)
    if parent is None:
        raise ValueError(f"복원 부모 경로 봉쇄 실패(symlink 재지정 등): {rel}")
    name = parts[-1]
    try:
        try:
            os.unlink(name, dir_fd=parent)   # 파일·symlink 제거(따라가지 않음)
        except OSError:
            pass                             # 부재·디렉터리·권한 — 아래 O_EXCL가 판정
        try:
            wfd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                          0o600, dir_fd=parent)
        except FileExistsError as e:
            raise ValueError(
                f"복원 대상을 비울 수 없다(디렉터리 등) — 수동 확인: {rel}") from e
        try:
            mv = memoryview(data)
            while mv:
                mv = mv[os.write(wfd, mv[:_CHUNK]):]
            os.fsync(wfd)
        finally:
            os.close(wfd)
    finally:
        os.close(parent)


def _unlink_nofollow(rel: str) -> None:
    """vault 상대 `rel`을 nofollow 부모 fd 체인 안에서 지운다.

    '지울 것이 없음'(부모 부재·symlink, 대상 이미 없음)과 '대상이 실재하지만
    삭제 실패'(권한 등)를 **구분**한다 — 후자는 삼키지 않고 올린다. 삭제 실패를
    조용히 넘기면 작업본이 승인본과 달리 남는데도 revert가 완료된 것처럼
    기록될 수 있다(복원 완료 계약 위반)."""
    parts = _rel_parts(rel)           # 정규형 위반은 여기서 거부(성분 `..` 없음)
    parent = _open_dir_chain(parts[:-1], create=False)
    if parent is None:
        return                           # 부모 부재·symlink — 지울 것이 없다
    try:
        try:
            os.unlink(parts[-1], dir_fd=parent)
        except FileNotFoundError:
            pass                         # 이미 없음 — 정상
        except OSError as e:
            # 대상이 실재하나 삭제 실패(권한·디렉터리 등) — 삼키지 않고 명시적
            # 거부로 올린다. 삭제 실패를 복원 완료로 위장하지 않는다.
            raise ValueError(
                f"승인본 밖 파일 삭제 실패 — 복원 미완료: {rel} ({e})") from e
    finally:
        os.close(parent)


def _write_file_path(p: Path, data: bytes) -> None:
    """폴백(dir_fd 미지원) — 경로 기반 원자 교체(best-effort)."""
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


def _restore_tree(region_dir: Path, table: dict[str, str]) -> None:
    """영역을 manifest 상태로 되돌린다 — manifest의 각 파일을 저장소 내용으로
    복원하고, manifest에 없는 현재 파일은 지운다.

    manifest는 신뢰 밖 입력이다(다기기 병합으로 유입). ①모든 rel이 그 영역 안에
    봉쇄되는가 ②모든 blob이 저장소에 실재하는가를 **전수 사전 검증**으로 먼저
    확인해(부분 복원·영역 밖 쓰기 동시 차단), 그다음 실제 쓰기·삭제는 저장소와
    같은 **O_NOFOLLOW fd 체인**에 결속한다 — 사전 검증 뒤 영역 하위 부모를
    symlink로 교체해도(TOCTOU) I/O가 vault 밖으로 새지 않는다(dir_fd 지원
    플랫폼; 그 밖에는 경로 기반 best-effort 폴백).

    영역 봉쇄는 두 겹이다(`update._within`과 같은 규율): 선언된 rel이 영역
    접두인지(문자열 — tree 해시·상태 비교가 쓰는 그 표기)와, 그 rel이 실제로
    가리키는 경로가 영역 realpath 아래인지(물리 — symlink 재배치를 흡수)."""
    root_real = Path(os.path.realpath(ROOT))
    region_rel = posix_rel(region_dir, root_real).rstrip("/")
    region_real = os.path.realpath(region_dir)
    # 0) 전수 사전 검증 — 봉쇄(영역 접두 + 물리 경로)·blob 실재를 쓰기 전에 확인
    validated: list[tuple[str, bytes]] = []
    for rel, h in sorted(table.items()):
        if not (rel == region_rel or rel.startswith(region_rel + "/")):
            raise ValueError(
                f"승인본 경로가 영역 밖이다 — 복원 거부: {rel} ⊄ {region_rel}")
        p = resolve_in_root(rel)
        if p is None:
            raise ValueError(f"승인본 경로가 vault 밖이다 — 복원 거부: {rel}")
        real = os.path.realpath(p)
        if real != region_real and not real.startswith(region_real + os.sep):
            raise ValueError(
                f"승인본 경로의 실제 위치가 영역 밖이다 — 복원 거부: {rel} → {real}")
        data = _store_get(h)
        if data is None:
            raise ValueError(f"승인본 blob 부재 — 복원 불가: {rel} {h}")
        validated.append((rel, data))
    # 1) 승인본 내용으로 복원 (쓰기를 fd 체인에 결속)
    for rel, data in validated:
        if _DIRFD_SAFE:
            _write_file_nofollow(rel, data)
        else:
            p = resolve_in_root(rel)
            if p is None:
                raise ValueError(f"승인본 경로가 vault 밖이다 — 복원 거부: {rel}")
            _write_file_path(p, data)
    # 2) manifest에 없는 현재 파일 제거 (에이전트가 추가한 것)
    for rel, p in _region_files(region_dir):
        if rel not in table:
            if _DIRFD_SAFE:
                _unlink_nofollow(rel)
            else:
                try:
                    p.unlink()
                except OSError:
                    pass


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
            table = _tree_table(tree) if tree else None
            if table is None:
                errs.append(
                    f"승인본 manifest 해석 불가(부재 또는 비정규): {region} ({tree})")
                continue
            # manifest가 가리키는 **모든 blob의 실재**를 확인한다 — manifest만
            # 해석되고 그것이 가리키는 blob이 없으면 승인본이 clean으로 보여도
            # revert가 나중에 실패한다(복원 불가능한 승인본 적발).
            missing = [rel for rel, h in table.items() if _store_get(h) is None]
            if missing:
                errs.append(
                    f"승인본 blob 부재(복원 불가): {region} — {sorted(missing)[:5]}")
    return errs
