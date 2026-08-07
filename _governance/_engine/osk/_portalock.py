"""파일 잠금 이식 계층 — POSIX는 `fcntl.flock`, Windows는 `msvcrt.locking`.

엔진의 잠금은 셋 다 **프로세스 간 배타 잠금**이다(대장 append·전역 쓰기 잠금·
데몬 싱글턴). `fcntl`은 POSIX 전용이라 Windows에서는 import 단계에서 엔진 전체가
죽는다 — 잠금 의미는 보존한 채 플랫폼만 갈아끼운다.

차이 하나: POSIX `flock`은 파일 전체를 잠그고 Windows `locking`은 **바이트 구간**을
잠근다. 선두 1바이트를 관례 구간으로 삼는다 — 모든 잠금 참여자가 같은 구간을 쓰는 한
배타성은 동일하고, EOF 너머 구간도 잠글 수 있으므로 빈 파일에서도 성립한다.
"""
from __future__ import annotations

try:
    import fcntl
    _WINDOWS = False
except ModuleNotFoundError:      # Windows
    import msvcrt
    _WINDOWS = True

_REGION = 1      # 선두 1바이트 — 잠금 참여자 전원이 공유하는 관례 구간


def lock_exclusive(f, blocking: bool = True) -> None:
    """배타 잠금을 건다. `blocking=False`면 이미 잠겨 있을 때 OSError를 올린다."""
    if not _WINDOWS:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(f, flags)
        return
    pos = f.tell()
    try:
        f.seek(0)
        if not blocking:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, _REGION)
            return
        # LK_LOCK은 1초 간격 10회만 재시도하고 OSError를 올린다 — POSIX flock의
        # 무기한 대기와 맞추려면 성공할 때까지 다시 건다.
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, _REGION)
                return
            except OSError:
                continue
    finally:
        f.seek(pos)


def unlock(f) -> None:
    """`lock_exclusive`로 건 잠금을 푼다(같은 구간이어야 한다)."""
    if not _WINDOWS:
        fcntl.flock(f, fcntl.LOCK_UN)
        return
    pos = f.tell()
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, _REGION)
    finally:
        f.seek(pos)
