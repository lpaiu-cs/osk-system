"""osk.update_txn — 크래시-안전 트랜잭션 (Mechanism §1-2 7항).

적용은 원상을 먼저 남기고(`txn_begin`), 파일을 원자적으로 쓰고(`write_atomic`), 끝나면 치운다
(`txn_clear`). 도중에 죽은 적용은 다음 실행이 되돌리거나 마친다(`txn_recover`). 무엇을 쓸지는 모른다."""
from __future__ import annotations
import errno, json, os, shutil, stat, tempfile
from pathlib import Path
from .core import sha256_file, canon_rel
from .durable import fsync_dir as _fsync_dir, mkdirs_durable as _mkdirs_durable
from . import update_state
from .vault import Vault


# ── 크래시-안전 트랜잭션 (Mechanism §1-2 7항) ────────────────────────────
#
# 프로토콜 — 파일과 저널을 **하나의 txn id**로 묶는다:
#   ①pre-image 백업 fsync → ②manifest(txn) 원자 기록·fsync  ← 트랜잭션 시작
#   ③저널 begin(txn) → ④파일 조작 → ⑤저널 apply/remove(txn) → ⑥저널 done(txn)
#   ⑦manifest 제거                                            ← 트랜잭션 종료
# 복구 판정은 결정적이다: manifest의 txn이 저널에 `done`이면 **roll-forward**
# (파일은 이미 새 판이므로 manifest만 정리), 없으면 **rollback**(pre-image 복구).
# 커밋 전 apply/remove는 `update_state.committed()`가 판정에서 가린다 — 파일과 저널이 어긋나지
# 않는 이유다. 복구 실패는 fail-closed로 백업을 보존한 채 중단한다.


def txn_dir(vault: Vault) -> Path:
    """크래시-안전 트랜잭션 영역(비동기화)"""
    return vault.root / ".osk" / "txn"


def txn_manifest(vault: Vault) -> Path:
    """존재 = 미완료 트랜잭션(복구 대상)"""
    return txn_dir(vault) / "manifest.json"


def txn_backup(vault: Vault) -> Path:
    return txn_dir(vault) / "backup"


def _fsync_file(p: Path) -> None:
    """파일의 메타데이터까지 내구화 — `chmod` 뒤 fsync하지 않으면 내용은
    복구됐는데 권한 변경만 유실될 수 있다.

    핸들은 **쓰기 가능**으로 연다. Windows CRT는 읽기 전용 핸들에 `_commit`을
    허용하지 않아 `O_RDONLY`로 열면 `os.fsync`가 `EBADF`를 낸다(실측: O_RDONLY
    → errno 9, O_RDWR → OK). 이 함수를 부르는 유일한 자리가 롤백 루프이므로,
    그 EBADF는 **모든 롤백을 첫 파일에서 중단**시켜 half-applied를 영구
    고착시켰다 — 정상 경로는 여기 오지 않아 드러나지 않았다.

    핸들을 못 얻으면 조용히 넘긴다. 내구화는 최선 노력이고, 그 실패로 이미
    복구된 내용을 되돌리는 것이 더 나쁘다 — `_fsync_dir`이 EACCES·EPERM에
    쓰는 규율과 같다."""
    try:
        fd = os.open(str(p), os.O_RDWR)
    except OSError:
        return          # 읽기 전용·부재 — 내구화할 핸들이 없다
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF,
                           errno.EACCES):
            raise
    finally:
        os.close(fd)


def fsync_journal_home(vault: Vault) -> None:
    """저널 파일의 **디렉터리 엔트리**를 내구화한다. `ledger_append`가 처음
    만든 대장의 엔트리를 내구화하지만, 직전 실행이 행 fsync 직후·엔트리 내구화
    전에 죽었으면 이름 자체가 전원 차단에 유실될 수 있다 — 그 상태에서 트랜잭션
    표식까지 지우면 파일은 새 판인데 baseline·관리 이력이 통째로 사라진다. ROOT
    까지 조상을 함께 내구화한다(대장 구획도 이번에 생겼을 수 있다)."""
    d = update_state.journal_path(vault).parent
    root_real = Path(os.path.realpath(vault.root))
    while True:
        _fsync_dir(d)
        if Path(os.path.realpath(d)) == root_real or d.parent == d:
            break
        d = d.parent


def _planned_dirs(vault: Vault, rels: list[str]) -> list[str]:
    """이 트랜잭션이 새로 만들 수 있는 디렉터리(canonical 상대, 얕은 순).
    manifest에 미리 담아 두면 rollback이 **비어 있는 것만** 되돌릴 수 있다 —
    SKEL은 디렉터리 존재 자체가 적용 결과이므로 잔재로 남겨서는 안 된다."""
    out: set[str] = set()
    root_real = Path(os.path.realpath(vault.root))
    for rel in rels:
        p = (vault.root / rel).parent
        while True:
            try:
                cp = p.relative_to(vault.root)
            except ValueError:
                break
            if not cp.parts or p.exists() or Path(os.path.realpath(p)) == root_real:
                break
            out.add(cp.as_posix())
            p = p.parent
    return sorted(out, key=lambda s: (s.count("/"), s))


def txn_clear(vault: Vault) -> None:
    """트랜잭션 영역을 지운다. 정리도 상태 전이의 일부이므로 **성공을 확인**한다 —
    표식이 남으면 데몬이 모든 tick을 거부하고 다음 갱신이 남은 백업과 충돌한다."""
    if txn_dir(vault).exists():
        shutil.rmtree(txn_dir(vault), ignore_errors=True)
    if txn_dir(vault).exists() or txn_manifest(vault).exists():
        raise update_state.UpdateError(
            f"트랜잭션 영역 정리 실패 — 수동 개입 필요(권한·파일시스템 확인): "
            f"{txn_dir(vault)}")
    _fsync_dir(txn_dir(vault).parent if txn_dir(vault).parent.exists() else vault.root)


def txn_begin(vault: Vault, txn: str, version: str, touch: list[str]) -> None:
    """건드릴 파일(canonical ROOT-상대 경로)의 pre-image를 영속 백업·fsync한 뒤
    manifest를 원자 기록한다. manifest 기록이 트랜잭션 시작의 커밋 지점이므로,
    그 전에 죽으면 target은 아직 손대지 않은 상태다. 경로는 절대경로로 굳히지
    않는다 — 복구 시 다시 봉쇄·정체성 검증하기 위해 canonical 상대로 남긴다."""
    txn_clear(vault)
    # `.osk/`까지 처음 만들 수 있다 — 만든 각 디렉터리의 부모(최상단은 ROOT)를
    # 내구화해야 target은 남고 복구 표식만 유실되는 창이 닫힌다.
    _mkdirs_durable(txn_backup(vault))
    entries = []
    for i, rel in enumerate(touch):
        key = f"{i:06d}"
        p = vault.root / rel
        existed = p.is_file()
        h = mode = None
        if existed:
            mode = stat.S_IMODE(p.stat().st_mode)   # 권한도 pre-image의 일부다
            data = p.read_bytes()
            bp = txn_backup(vault) / key
            write_atomic(bp, data)              # 백업 자체도 fsync된다
            h = sha256_file(bp)
        entries.append({"rel": rel, "backup": key, "existed": existed,
                        "hash": h, "mode": mode})
    _fsync_dir(txn_backup(vault))
    write_atomic(txn_manifest(vault), json.dumps(
        {"txn": txn, "version": version, "entries": entries,
         "dirs": _planned_dirs(vault, touch)},           # rollback이 되돌릴 새 디렉터리
        ensure_ascii=False).encode())
    _fsync_dir(txn_dir(vault))


def journal_lenient(vault: Vault) -> list[dict]:
    """**복구 판정 전용** 관대한 판독 — 판독 가능한 행만 모은다.

    크래시가 저널 append 도중이었으면 부분 JSON 행이 남는데, 엄격 판독
    (`ledger_read`)은 거기서 즉시 실패한다. 그러면 트랜잭션 표식이 남은 바로
    그 상황에서 복구에 도달하지 못한다 — 복구 판정에 필요한 것은 `done(txn)`의
    존재 여부뿐이므로, 그 판정만 관대하게 하고 저널의 엄격 검증은 복구 뒤
    정상 경로에 맡긴다(독립 `recover.py`와 같은 규율)."""
    if not update_state.journal_path(vault).exists():
        return []
    out = []
    for line in update_state.journal_path(vault).read_text(encoding="utf-8",
                                         errors="ignore").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue                    # 부분 행 — 복구 판정에서 건너뛴다
        if isinstance(r, dict):
            out.append(r)
    return out


def _txn_pending(vault: Vault) -> dict | None:
    """미완료(또는 미정리) 트랜잭션 manifest — 없으면 None. 판독 실패는 손상이며
    **지우지 않는다**(유일한 복구 자료다) — 호출부가 fail-closed로 중단한다."""
    if not txn_manifest(vault).is_file():
        return None
    try:
        man = json.loads(txn_manifest(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise update_state.UpdateError(
            f"트랜잭션 manifest 손상 — 자동 복구 불가, 수동 개입 필요: {e} "
            f"(백업은 {txn_dir(vault)} 에 보존했다)")
    if not isinstance(man.get("entries"), list) or not man.get("txn"):
        raise update_state.UpdateError(
            f"트랜잭션 manifest 형식 위반 — 수동 개입 필요 (보존: {txn_dir(vault)})")
    return man


def txn_recover(vault: Vault, recs: list[dict]) -> str | None:
    """미완료 트랜잭션을 결정적으로 처리한다 — 'rollback'|'roll-forward'|None.

    저널에 그 txn의 `done`이 있으면 커밋된 것이므로 파일은 새 판이 정답이고
    manifest만 정리한다(roll-forward). 없으면 pre-image로 되돌린다(rollback).
    복구 대상 경로는 다시 봉쇄·정체성 검증하며, 하나라도 되돌리지 못하면
    백업을 보존한 채 중단한다(fail-closed)."""
    man = _txn_pending(vault)
    if man is None:
        return None
    txn = man["txn"]
    if any(r.get("kind") == "done" and r.get("txn") == txn for r in recs):
        # 커밋됨 — 파일은 그대로 둔다. 다만 직전 실행이 `done` 기록 직후·저널
        # 홈 내구화 **전에** 죽었을 수 있으므로, 그 내구화를 여기서 재시도한
        # 뒤에만 표식을 지운다(안 그러면 누락된 fsync가 영영 재시도되지 않는다).
        fsync_journal_home(vault)
        txn_clear(vault)
        return "roll-forward"
    for e in man["entries"]:
        rel = e.get("rel")
        cp = canon_rel(vault.root, str(rel)) if rel else None
        if cp is None:
            raise update_state.UpdateError(
                f"복구 경로가 봉쇄·정체성 검증에 실패했다 — 중단(보존: {txn_dir(vault)}): {rel}")
        p = vault.root / cp
        try:
            if e.get("existed"):
                bp = txn_backup(vault) / str(e.get("backup"))
                if not bp.is_file():
                    raise update_state.UpdateError(
                        f"백업이 없다 — 복구 불가, 수동 개입 필요: {rel}")
                if e.get("hash") and sha256_file(bp) != e["hash"]:
                    raise update_state.UpdateError(
                        f"백업이 손상됐다 — 복구 불가, 수동 개입 필요: {rel}")
                write_atomic(p, bp.read_bytes())
                # 삭제됐던 파일은 대상이 없어 write_atomic이 기본 권한을 쓴다 —
                # pre-image의 권한을 명시 복원해야 복구가 완전해진다.
                if e.get("mode") is not None:
                    os.chmod(p, int(e["mode"]))
                    _fsync_file(p)       # 권한 변경도 내구화한 뒤 표식을 지운다
            else:
                p.unlink(missing_ok=True)
                _fsync_dir(p.parent)
        except OSError as ex:
            raise update_state.UpdateError(
                f"복구 실패 — 중단(백업 보존: {txn_dir(vault)}): {rel} {ex}")
    # 파일을 되돌린 뒤, 이 트랜잭션이 만든 디렉터리 중 **비어 있는 것만** 깊은
    # 순서로 제거한다 — SKEL은 디렉터리 존재 자체가 적용 결과라 잔재로 남으면
    # 'pre-image로 복구'가 성립하지 않는다. 비어 있지 않으면(사용자 파일 등) 둔다.
    for rel in sorted(man.get("dirs") or [], key=lambda s: -s.count("/")):
        cp = canon_rel(vault.root, str(rel))
        if cp is None:
            raise update_state.UpdateError(                   # 경로 정체성이 바뀌었다 — 중단
                f"복구 대상 디렉터리가 봉쇄·정체성 검증에 실패했다 "
                f"— 중단(보존: {txn_dir(vault)}): {rel}")
        d = vault.root / cp
        try:
            d.rmdir()
        except OSError as ex:
            # 허용하는 것은 둘뿐이다: 이미 없다(ENOENT), 사용자 파일이 생겨
            # 비어 있지 않다(ENOTEMPTY/EEXIST). 권한·I/O 오류는 복구 실패이므로
            # 파일 복구와 같은 규율로 fail-closed한다.
            if ex.errno in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                continue
            raise update_state.UpdateError(
                f"디렉터리 복구 실패 — 중단(백업 보존: {txn_dir(vault)}): {cp} {ex}")
        _fsync_dir(d.parent)
    txn_clear(vault)
    return "rollback"


def write_atomic(dst: Path, data: bytes) -> None:
    """원자 교체. `mkstemp`는 0600으로 만들므로 **기존 권한을 보존**한다 —
    보존하지 않으면 갱신이 0644 프레임워크 파일을 0600으로 바꿔 다른 계정의
    서비스가 읽지 못하고, rollback도 원래 권한을 되돌리지 못한다."""
    _mkdirs_durable(dst.parent)     # 새 조상 각각의 부모까지 내구화
    try:
        mode = stat.S_IMODE(dst.stat().st_mode)
    except OSError:
        mode = 0o644                # 새 파일 — 프레임워크의 기본 권한
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            # mode를 **fsync 앞에서** 확정한다 — 뒤에 chmod하면 그 메타데이터가
            # 내구화되지 않아, 내용은 신판인데 권한만 mkstemp의 0600으로 남는
            # 창이 생긴다(부모 디렉터리 fsync는 rename 엔트리의 내구성이지
            # 파일 mode의 내구성이 아니다). 한 번의 fsync가 둘 다 내구화한다.
            try:
                os.fchmod(f.fileno(), mode)
            except (AttributeError, OSError):    # Windows 등 — 경로로 대체
                os.chmod(tmp, mode)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
        _fsync_dir(dst.parent)          # rename 자체의 내구성 (전원 차단 대비)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
