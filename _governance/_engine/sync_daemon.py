"""osk-system 동기화 데몬 — 얇은 재작성.

구 vault_daemon(동기화+구 검색 서빙 혼성)을 대체한다. 동기화 기능만 남기며
검색·색인은 서빙하지 않는다 — 그것은 osk 엔진과 MCP 서버의 일이다.
vault_sync(순수 git 헬퍼)는 「동기화 데몬 예외」로 재사용한다.

실행:  SYNC_ENABLED=1 .venv/bin/python _engine/sync_daemon.py [--interval 900]
       (SYNC_ENABLED가 명시돼 있지 않으면 즉시 종료 — 템플릿 계약과 동일)
대상:  vault_sync.SYNC_BRANCH(=main) **고정**. HEAD를 따라가지 않는다 — 다른
       브랜치가 checkout돼 있으면 깨끗할 때만 전환하고, 더러우면 동기화를
       거부한다(남의 작업을 옮기거나 감추지 않는다).
중지:  SIGTERM/SIGINT (진행 중 sync는 완료 후 종료)
잠금:  실제 git 디렉터리(rev-parse --git-common-dir) 안의 osk-sync.lock.
       구하지 못하면 임시 디렉터리에 루트 경로 해시를 키로 둔다 — 추적 트리로는
       절대 폴백하지 않는다(데몬 자신의 `git add -A`가 잠금 파일을 커밋한다).
"""
from __future__ import annotations
import argparse, hashlib, os, signal, sys, tempfile, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_sync  # noqa: E402
from osk._portalock import lock_exclusive, unlock  # noqa: E402

ROOT = (Path(os.environ["OSK_VAULT_ROOT"]).resolve()
        if os.environ.get("OSK_VAULT_ROOT")
        else Path(__file__).resolve().parent.parent.parent)
_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True


def _lock_path(root: Path = ROOT, name: str = "osk-sync.lock") -> Path:
    """잠금 파일의 경로 — 추적 트리 밖으로만 고른다. `name`으로 잠금을 구분한다:
    `osk-sync.lock`은 데몬 싱글턴(한 기기 한 데몬), `osk-mutation.lock`은 update와
    공유하는 **working-tree 변경 상호배제** 잠금이다.

    `<vault>/.git`은 worktree에서 디렉터리가 아니라 파일이므로 존재 여부로
    판단하면 안 된다. 실제 git 디렉터리를 git에게 묻고, 그것도 실패하면
    임시 디렉터리에 루트 경로 해시를 키로 둔다(기기 안 vault별 유일).

    git은 반드시 `vault_sync._git` 게이트웨이로만 부른다 — 데몬은 콘솔 없는
    pythonw로 돌고, 이 함수는 매 tick(mutation 잠금 경로 계산)마다 불린다.
    bare `subprocess`로 git.exe를 spawn하면 Windows가 tick마다 새 콘솔 창을
    깜빡인다. 게이트웨이가 `CREATE_NO_WINDOW`(+`GIT_TERMINAL_PROMPT=0`) 하드닝의
    단일 소유자다(bare-spawn 재도입은 test_regression이 AST로 막는다)."""
    try:
        r = vault_sync._git(root, ["rev-parse", "--git-common-dir"], 10)
        if r.returncode == 0 and r.stdout.strip():
            d = Path(r.stdout.strip())
            if not d.is_absolute():
                d = root / d
            if d.is_dir():
                return d / name
    except Exception:  # noqa: BLE001 — git 부재·타임아웃 모두 임시 디렉터리로
        pass
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    stem = name.rsplit(".", 1)[0]
    return Path(tempfile.gettempdir()) / f"{stem}-{key}.lock"


def once(root: Path = ROOT) -> str:
    """ensure_branch → commit_local → pull(rebase) → push. vault_sync 자체 계약
    순서를 따른다. 충돌·거부는 삼키지 않고 상태 문자열로 표면화한다.

    working-tree를 건드리는 구간 전체를 **mutation 잠금** 아래 둔다 — update가
    파일을 반쯤 바꾼 순간에 데몬의 `git add -A`가 그 혼합 상태를 커밋·push하지
    못하게 한다(update와 공유하는 잠금). 잡혀 있으면 이번 tick을 건너뛴다.

    잠금을 얻어도 **미완료 트랜잭션 표식**(`.osk/txn/manifest.json`)이 남아 있으면
    거부한다 — update 프로세스가 죽으면 OS가 잠금을 풀지만 working tree에는
    half-applied 파일이 남는다. 표식이 사라지는 것은 updater의 복구가 끝났다는
    뜻이며, 그때까지 혼합 상태를 커밋·push하지 않는다."""
    if not vault_sync.is_git_repo(root):
        return "git 저장소 아님"
    mlock = open(_lock_path(root, "osk-mutation.lock"), "w")
    acquired = False
    try:
        try:
            lock_exclusive(mlock, blocking=False)
            acquired = True
        except OSError:
            return "locked"          # update가 mutation 중 — 다음 주기에 맡긴다
        if (root / ".osk" / "txn" / "manifest.json").is_file():
            return "pending-txn"     # 갱신이 죽어 half-applied — 복구 전엔 손대지 않는다
        return _once_locked(root)
    finally:
        if acquired:                 # 소유하지 않은 잠금은 풀지 않는다(Windows 안전)
            unlock(mlock)
        mlock.close()


def _once_locked(root: Path) -> str:
    # 동기화 대상은 main 고정이다. HEAD를 따라가면 어떤 세션이 잠깐 다른
    # 브랜치를 checkout해 둔 사이에 그 브랜치가 정본인 양 커밋·push된다.
    switched, st, detail = vault_sync.ensure_branch(root)
    if st != "ok":
        return f"브랜치 고정 실패 — 동기화하지 않았다: {detail}"
    if switched:
        print(f"sync: {detail}", file=sys.stderr)
    msg = f"sync: {datetime.now():%Y-%m-%d %H:%M:%S} (daemon)"
    ok, st, detail = vault_sync.commit_local(root, msg)
    if st != "ok":
        return f"commit 실패: {st} {detail}"
    if not vault_sync.has_remote(root):
        return "ok (로컬 커밋만 — 원격 없음)"
    changed, st, detail = vault_sync.pull(root)
    if st == "conflict":
        return f"pull 충돌 — 수동 개입 필요: {detail}"
    if st != "ok":
        return f"pull 실패: {st} {detail}"
    ok, st, detail = vault_sync.commit_push(root, msg)
    if st == "rejected":
        # commit_push 계약대로 호출부가 pull-rebase 후 한 번 재시도한다.
        # 그래도 거부되면(경합 지속) 다음 주기에 맡기고 상태로 표면화한다.
        changed, st2, d2 = vault_sync.pull(root)
        if st2 != "ok":
            return f"push 거부 후 pull 실패: {st2} {d2}"
        ok, st, detail = vault_sync.commit_push(root, msg)
    if st != "ok":
        return f"push 실패: {st} {detail}"
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=900, help="초 (기본 15분)")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if os.environ.get("SYNC_ENABLED", "").lower() not in ("1", "true", "yes"):
        sys.exit("sync 비활성 — SYNC_ENABLED=1 로 명시 활성화 (템플릿 계약: 키가 없으면 동기화하지 않는다)")
    lock = open(_lock_path(), "w")
    try:
        lock_exclusive(lock, blocking=False)
    except OSError:
        sys.exit("이미 실행 중인 sync_daemon이 있다 (singleton lock)")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    if a.once:
        print(once())
        return
    while not _stop:
        try:
            st = once()
            if st != "ok":
                print(f"sync 상태: {st}", file=sys.stderr)
        except Exception as e:
            print(f"sync 실패(다음 주기 재시도): {e}", file=sys.stderr)
        for _ in range(a.interval):
            if _stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
