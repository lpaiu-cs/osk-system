"""osk.durable — 꺼져도 남는 쓰기 (Mechanism §1-2 7항 · §3 1항).

디렉터리 엔트리의 내구화(`fsync_dir`), 새로 만든 디렉터리의 내구화(`mkdirs_durable`), 임시 파일에 쓰고 바꿔
끼우는 원자 쓰기(`atomic_write`)."""
from __future__ import annotations
import errno, os, tempfile, time
from pathlib import Path


def fsync_dir(d: Path) -> None:
    """디렉터리 엔트리를 내구화 — 파일만 fsync하면 rename·create·unlink가 전원
    차단에 유실될 수 있다(POSIX). 디렉터리 fsync 개념이 없는 파일시스템(EINVAL·
    ENOTSUP)과 열 권한이 없는 디렉터리(EACCES·EPERM)만 넘기고, 그 밖의 오류는
    올려 쓰기가 내구화 없이 성공한 척하지 못하게 한다.

    **Windows에서는 의도된 no-op이다.** 디렉터리를 `os.open`으로 열 수 없고
    (EACCES — 예전에는 그 예외를 삼켜 조용히 no-op이었다), NTFS는 메타데이터를
    저널링하므로 rename·create는 전원 차단 뒤에도 옛 엔트리나 새 엔트리 중
    하나로 남는다(찢긴 엔트리는 없다). 다만 **마지막 변경의 내구성은 보장하지
    않는다** — 저널이 디스크에 닿기 전에 전원이 나가면 직전 상태로 돌아갈 수
    있다. 프로세스 사망에는 두 OS 모두 안전하다(커널 캐시가 살아 있다)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except FileNotFoundError:
        return                      # 이미 사라진 디렉터리 — 내구화할 대상이 없다
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


def mkdirs_durable(d: Path) -> list[Path]:
    """`d`까지의 없는 조상을 만들고, **만든 각 디렉터리의 부모를 fsync**한다.
    `mkdir(parents=True)` 뒤 자신만 fsync하면 그 엔트리를 소유한 부모가 내구화되지
    않아 전원 차단 시 디렉터리째 유실된다(그 안의 파일은 기록 뒤에도 사라진다).
    반환: 새로 만든 디렉터리(깊은 순) — 갱신 트랜잭션 rollback이 되돌릴 대상이다."""
    missing = []
    p = d
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    created = []
    for q in reversed(missing):     # 얕은 곳부터 만든다
        q.mkdir(exist_ok=True)
        created.append(q)
        fsync_dir(q.parent)         # 그 엔트리를 소유한 부모를 내구화
    created.reverse()
    return created


def atomic_write(path: Path, data: bytes) -> None:
    """원자 교체 + 내구화: 임시 파일 fsync → `os.replace` → **부모 디렉터리
    fsync**. 호출부가 이 뒤에 그 파일을 가리키는 대장 기록을 남기므로, 엔트리가
    기록보다 먼저 내구화돼야 전원 차단 뒤 '기록은 있는데 파일은 없는' 상태가
    생기지 않는다. 권한은 mkstemp 기본이다 — 기존 권한을 보존해야 하는 갱신은
    `update_txn.write_atomic`을 쓴다.

    Windows에서는 대상이나 임시 파일을 FILE_SHARE_DELETE 없이 연 읽기(Python의
    `open()`이 그렇다)가 있으면 교체가 WinError 5·32로 실패한다. 그런 읽기는 곧
    닫히므로 잠깐씩 다시 건다 — 대화 커서를 잠금 없이 읽는 엔진 안팎의 읽기가
    분리 작업자의 저장과 겹친다(#192 실측)."""
    mkdirs_durable(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # ponytail: 약 1초의 고정 재시도 — 그보다 오래 쥔 읽기에는 전처럼 실패한다. 실측되면
        # 엔진의 잠금 없는 커서 읽기(integration의 넷)를 상태 잠금 아래로 옮긴다.
        for wait in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, None):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if os.name != "nt" or wait is None:
                    raise
                time.sleep(wait)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    fsync_dir(path.parent)
