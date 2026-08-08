#!/usr/bin/env python3
"""갱신 트랜잭션 복구 부트스트랩 — 엔진과 **독립**이다.

근거: Mechanism §1-2 7항(미완료 트랜잭션은 pre-image로 복구한다).

왜 별도 스크립트인가: 갱신은 엔진 자신(`osk/*.py`)을 교체한다. 그 도중에
프로세스가 죽으면 엔진이 반쯤 교체된 상태가 되어 `osk.update`의 import 자체가
깨질 수 있고, 그러면 정작 복구 코드에 도달할 수 없다. 이 파일은 표준 라이브러리만
쓰고 osk 패키지를 import하지 않으므로 그 상황에서도 돈다.

사용:
    python3 _governance/_engine/scripts/recover.py [--root <vault>] [--apply]

기본은 **보고**다. `--apply`가 있어야 되돌린다. 트랜잭션이 커밋된 뒤(저널에
`done(txn)`) 남은 표식은 파일을 건드리지 않고 표식만 정리한다(roll-forward).
복구 자료(백업)가 없거나 손상됐으면 아무것도 지우지 않고 중단한다(fail-closed).
"""
from __future__ import annotations
import argparse, errno, hashlib, json, os, shutil, stat, subprocess, sys, tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
    _WINDOWS = False
except ModuleNotFoundError:          # Windows
    import msvcrt
    _WINDOWS = True

# 아래 잠금·fsync·경로 규율은 엔진(osk/_portalock.py·osk/update.py)과 **의도적으로
# 중복**된다. 이 스크립트의 존재 이유가 "엔진이 반쯤 교체돼 import가 깨져도 복구가
# 성립한다"이므로, 엔진을 import해 규율을 공유할 수 없다.


def _lock(f) -> None:
    if not _WINDOWS:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock(f) -> None:
    if not _WINDOWS:
        fcntl.flock(f, fcntl.LOCK_UN)
    else:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def _lock_path(root: Path, name: str) -> Path:
    """sync_daemon._lock_path와 같은 자리 — 추적 트리 밖."""
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse",
                            "--git-common-dir"], capture_output=True,
                           text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            d = Path(r.stdout.strip())
            if not d.is_absolute():
                d = root / d
            if d.is_dir():
                return d / name
    except Exception:                # noqa: BLE001
        pass
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"{name.rsplit('.', 1)[0]}-{key}.lock"


@contextmanager
def _exclusive(path: Path, busy: str):
    f = open(path, "w")
    ok = False
    try:
        try:
            _lock(f)
            ok = True
        except OSError:
            raise RuntimeError(busy)
        yield
    finally:
        if ok:
            _unlock(f)
        f.close()


def _fsync_dir(d: Path) -> None:
    """엔진의 osk.update._fsync_dir와 같은 정책 — 디렉터리 fsync 개념이 없는
    파일시스템만 예외로 넘기고 그 밖의 오류는 올린다."""
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except FileNotFoundError:
        return
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.ENOTSUP, errno.EACCES, errno.EPERM):
            return
        raise
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(fd)


def _mkdirs_durable(d: Path) -> None:
    """없는 조상을 만들고 **만든 각 디렉터리의 부모**를 fsync한다."""
    missing = []
    p = d
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for q in reversed(missing):
        q.mkdir(exist_ok=True)
        _fsync_dir(q.parent)


def _rmtree_checked(d: Path) -> None:
    """정리도 상태 전이의 일부다 — 성공을 확인하고, 남으면 fail-closed."""
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    if d.exists():
        raise RuntimeError(f"트랜잭션 영역 정리 실패 — 수동 개입 필요: {d}")
    _fsync_dir(d.parent)


def _sha256(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _write_atomic(dst: Path, data: bytes) -> None:
    """원자 교체. `mkstemp`는 0600으로 만들므로 **기존 권한을 보존**한다 —
    보존하지 않으면 갱신이 0644 프레임워크 파일을 0600으로 바꿔 다른 계정의
    서비스가 읽지 못하고, rollback도 원래 권한을 되돌리지 못한다."""
    _mkdirs_durable(dst.parent)
    try:
        mode = stat.S_IMODE(dst.stat().st_mode)
    except OSError:
        mode = 0o644                # 새 파일 — 프레임워크의 기본 권한
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
        _fsync_dir(dst.parent)          # rename 자체의 내구성 (전원 차단 대비)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _canon_rel(root: Path, rel: str) -> str | None:
    """osk.update._canon_rel과 같은 규율 — 탈출·`..`/`.`·symlink 재지정 거부."""
    p = Path(rel)
    if not p.parts or p.is_absolute() or any(s in ("..", ".") for s in p.parts):
        return None
    try:
        broot = Path(os.path.realpath(root))
        real = Path(os.path.realpath(root / p))
        if real == broot:
            return None
        canon = real.relative_to(broot).as_posix()
    except (ValueError, OSError):
        return None
    return canon if canon == p.as_posix() else None


def _journal_done(root: Path, txn: str) -> bool:
    j = root / "= Scope/Workbench/_ledger/update.jsonl"
    if not j.is_file():
        return False
    for line in j.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue          # 손상 행은 이 판정에서 건너뛴다(보수적으로 미커밋)
        if isinstance(r, dict) and r.get("kind") == "done" and r.get("txn") == txn:
            return True
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="osk-recover", description=__doc__)
    ap.add_argument("--root", help="vault 루트 (기본: 이 스크립트에서 3단계 위)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else \
        Path(__file__).resolve().parent.parent.parent.parent
    if not a.apply:
        return _report(root)
    # 복구도 working tree를 바꾼다 — update·데몬과 같은 잠금 순서
    # (싱글턴 → mutation)로 상호배제한다. 크래시 뒤 service manager가 구 데몬을
    # 되살린 환경에서도 데몬이 half-applied를 커밋하지 못하게 한다.
    try:
        with _exclusive(_lock_path(root, "osk-sync.lock"),
                        "동기화 데몬이 실행 중이다 — 복구 전에 데몬을 멈춘다"), \
                _exclusive(_lock_path(root, "osk-mutation.lock"),
                           "다른 갱신·복구가 진행 중이다 — 잠시 후 다시 실행한다"):
            return _recover(root)
    except RuntimeError as e:
        print(f"[중단] {e}", file=sys.stderr)
        return 2


def _report(root: Path) -> int:
    """보고 전용 — 쓰지 않으므로 잠금 없이 현재 상태만 낸다."""
    return _recover(root, report_only=True)


def _recover(root: Path, report_only: bool = False) -> int:
    txn_dir = root / ".osk" / "txn"
    manifest = txn_dir / "manifest.json"
    if not manifest.is_file():
        print(json.dumps({"pending": False, "root": str(root)},
                         ensure_ascii=False))
        return 0
    try:
        man = json.loads(manifest.read_text(encoding="utf-8"))
        txn, entries = man["txn"], man["entries"]
    except (OSError, ValueError, KeyError) as e:
        print(f"[중단] manifest 손상 — 수동 개입 필요(보존: {txn_dir}): {e}",
              file=sys.stderr)
        return 2

    committed = _journal_done(root, txn)
    plan = []
    for e in entries:
        cp = _canon_rel(root, str(e.get("rel", "")))
        if cp is None:
            print(f"[중단] 복구 경로가 봉쇄·정체성 검증 실패 — 수동 개입 "
                  f"필요(보존: {txn_dir}): {e.get('rel')}", file=sys.stderr)
            return 2
        plan.append((cp, bool(e.get("existed")), str(e.get("backup")),
                     e.get("hash"), e.get("mode")))
    rep = {"pending": True, "txn": txn, "version": man.get("version"),
           "committed": committed,
           "action": "roll-forward" if committed else "rollback",
           "files": [c for c, *_ in plan], "applied": False}
    if report_only:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    if committed:                     # 파일은 새 판이 정답 — 표식만 정리
        _rmtree_checked(txn_dir)
        rep["applied"] = True
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    for cp, existed, key, h, _m in plan:  # 사전 검증 후 되돌린다(fail-closed)
        bp = txn_dir / "backup" / key
        if existed and (not bp.is_file() or (h and _sha256(bp) != h)):
            print(f"[중단] 백업 부재·손상 — 복구 불가, 수동 개입 필요"
                  f"(보존: {txn_dir}): {cp}", file=sys.stderr)
            return 2
    for cp, existed, key, _h, mode in plan:
        p = root / cp
        try:
            if existed:
                _write_atomic(p, (txn_dir / "backup" / key).read_bytes())
                if mode is not None:      # pre-image의 권한까지 복원한다
                    os.chmod(p, int(mode))
            else:
                p.unlink(missing_ok=True)
                _fsync_dir(p.parent)  # 삭제 엔트리 내구화
        except OSError as e:
            print(f"[중단] 복구 실패 — 백업 보존({txn_dir}): {cp} {e}",
                  file=sys.stderr)
            return 2
    # 이 트랜잭션이 만든 디렉터리 중 **비어 있는 것만** 깊은 순서로 되돌린다
    for rel in sorted(man.get("dirs") or [], key=lambda s: -str(s).count("/")):
        cp = _canon_rel(root, str(rel))
        if cp is None:
            print(f"[중단] 복구 대상 디렉터리가 봉쇄·정체성 검증 실패 — 수동 "
                  f"개입 필요(보존: {txn_dir}): {rel}", file=sys.stderr)
            return 2
        d = root / cp
        try:
            d.rmdir()
        except OSError as ex:
            # 허용: 이미 없음(ENOENT) · 사용자 파일로 비어 있지 않음(ENOTEMPTY)
            if ex.errno in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                continue
            print(f"[중단] 디렉터리 복구 실패 — 백업 보존({txn_dir}): {cp} {ex}",
                  file=sys.stderr)
            return 2
        _fsync_dir(d.parent)
    _rmtree_checked(txn_dir)
    rep["applied"] = True
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
