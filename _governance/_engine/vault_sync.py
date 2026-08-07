"""_engine/vault_sync.py — 데몬이 구동하는 git 동기화 헬퍼(이벤트 구동).

git은 서브프로세스 + 하드 타임아웃으로 격리하고, 요청 서빙 경로를 절대 블록하지 않게
호출부(sync_daemon)에서 스케줄한다. 충돌은 자동으로 해결하지 않고(rebase --abort) 상태로
표면화한다 — 사용자 데이터라 파괴적 자동 조치 금지.

동기화 대상은 `SYNC_BRANCH`(=main) **고정**이다. 원격 연산은 전부 `origin main`을
명시하며, HEAD가 다른 곳에 있으면 `ensure_branch`가 안전할 때만 되돌린다.
운용 문서는 docs/SETUP.md.

git 환경은 `LC_ALL=C`(영어 메시지 고정 → 출력 분류 안정화) + `GIT_TERMINAL_PROMPT=0`
(자격증명 프롬프트로 매달리지 않게)로 고정한다.

각 함수는 (ok_or_changed: bool, status: str, detail: str)를 반환한다.
status ∈ {"ok","conflict","rejected","error"}.
"""
import os
import subprocess

_GIT_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C", "GIT_TERMINAL_PROMPT": "0"}

# CREATE_NO_WINDOW: 데몬이 pythonw(콘솔 없음)로 돌 때, 콘솔 서브시스템인 git.exe를 spawn하면
# Windows가 매번 새 콘솔 창을 할당한다. sync 1회가 git을 여러 번 호출하므로 검은 콘솔이
# 여러 개 깜빡인다 → 무창 플래그로 억제. POSIX에선 0(무영향).
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _git(vault_root, args, timeout):
    return subprocess.run(
        ["git", "-C", str(vault_root), *args],
        capture_output=True, text=True, timeout=timeout, env=_GIT_ENV,
        creationflags=_NO_WINDOW,
    )


def is_git_repo(vault_root, timeout=10) -> bool:
    """vault_root가 저장소 **루트**일 때만 참. `--is-inside-work-tree`는 하위
    디렉터리도 통과시켜, OSK_VAULT_ROOT를 잘못 잡으면 상위 저장소 전체를
    커밋·push하게 된다."""
    try:
        r = _git(vault_root, ["rev-parse", "--show-toplevel"], timeout)
        top = r.stdout.strip()
        if r.returncode != 0 or not top:
            return False
        return os.path.realpath(top) == os.path.realpath(str(vault_root))
    except Exception:
        return False


def has_remote(vault_root, timeout=10) -> bool:
    try:
        r = _git(vault_root, ["remote"], timeout)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _head(vault_root, timeout):
    r = _git(vault_root, ["rev-parse", "HEAD"], timeout)
    return r.stdout.strip() if r.returncode == 0 else None


# 동기화 대상 브랜치는 **고정**이다. 데몬이 HEAD를 따라가면, 어떤 세션이
# 잠깐 다른 브랜치를 checkout해 둔 사이에 그 브랜치가 vault의 정본인 것처럼
# 커밋·push된다 — 정본이 조용히 갈라지는 경로다.
SYNC_BRANCH = "main"


def current_branch(vault_root, timeout=10):
    """현재 브랜치명. detached HEAD면 None."""
    r = _git(vault_root, ["symbolic-ref", "--quiet", "--short", "HEAD"], timeout)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _tracked_dirty(vault_root, timeout):
    """**추적 파일**의 수정·스테이징만 본다. 미추적 파일은 더러움으로 치지 않는다 —
    vault에서 미추적 파일은 대개 새로 생긴 노드이고, checkout은 그것을 그대로
    데리고 넘어가므로 main에서 커밋되는 것이 옳다. 반대로 추적 파일의 수정은
    누군가 그 브랜치에서 진행 중인 작업일 수 있어 옮기면 안 된다."""
    r = _git(vault_root, ["status", "--porcelain", "-z"], timeout)
    return any(e[:2] != "??" for e in r.stdout.split("\0") if e.strip())


def ensure_branch(vault_root, branch=SYNC_BRANCH, timeout=30):
    """동기화 전에 HEAD를 `branch`로 맞춘다 — 여기서 실패하면 동기화하지 않는다.

    이미 그 브랜치면 아무것도 하지 않는다. 다른 브랜치·detached라면 **추적
    파일이 수정돼 있지 않을 때만** 전환한다. 수정돼 있으면 전환하지 않고
    거부한다 — 배경 데몬이 남의 진행 중 작업을 다른 브랜치로 끌고 가거나
    stash로 감추는 것은 사용자 데이터에 대한 파괴적 자동 조치다(이 모듈의
    기본 원칙). 미추적 파일은 새 노드이므로 함께 넘어가는 것이 옳다."""
    try:
        cur = current_branch(vault_root, timeout)
        if cur == branch:
            return (False, "ok", "")
        where = f"'{cur}'" if cur else "detached HEAD"
        if _tracked_dirty(vault_root, timeout):
            return (False, "error",
                    f"{where}에 있고 추적 파일이 수정돼 있다 — {branch}로 전환하지 "
                    f"않았다. 진행 중인 작업일 수 있어 옮기지 않으며, 동기화 대상은 "
                    f"{branch} 고정이므로 이 상태로는 동기화하지 않는다")
        v = _git(vault_root, ["rev-parse", "--verify", "--quiet", branch], timeout)
        if v.returncode != 0:
            return (False, "error", f"로컬에 {branch} 브랜치가 없다 — 동기화하지 않았다")
        c = _git(vault_root, ["checkout", branch], timeout)
        if c.returncode != 0:
            return (False, "error",
                    f"{branch} 전환 실패: " + (c.stdout + c.stderr).strip()[-600:])
        return (True, "ok", f"{where} → {branch} 전환")
    except subprocess.TimeoutExpired:
        return (False, "error", f"git branch 확인 timeout ({timeout}s)")
    except Exception as e:  # noqa: BLE001
        return (False, "error", repr(e))


def _in_rebase(vault_root, timeout) -> bool:
    """진행 중 rebase 판별 — .git/rebase-{merge,apply}의 존재로, 로케일 무관."""
    for name in ("rebase-merge", "rebase-apply"):
        r = _git(vault_root, ["rev-parse", "--git-path", name], timeout)
        if r.returncode == 0 and os.path.isdir(
                os.path.join(str(vault_root), r.stdout.strip())):
            return True
    return False


def commit_local(vault_root, message, timeout=60):
    """add -A → commit(변경 없으면 'nothing to commit'을 ok로 처리). push는 하지 않는다.
    pull 전에 로컬을 먼저 commit해 두면 autostash 없이 rebase가 깔끔히 처리한다.
    진행 중 rebase에서는 커밋을 거부한다 — 그대로 add -A 하면 충돌 마커를 커밋한다."""
    try:
        if _in_rebase(vault_root, timeout):
            return (False, "error",
                    "rebase 진행 중 — 충돌 마커를 커밋하지 않는다(수동 개입 필요)")
        _git(vault_root, ["add", "-A"], timeout)
        c = _git(vault_root, ["commit", "-m", message], timeout)
        if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr).lower():
            return (False, "error", (c.stdout + c.stderr).strip()[-600:])
        return (True, "ok", "")
    except subprocess.TimeoutExpired:
        return (False, "error", f"git commit timeout ({timeout}s)")
    except Exception as e:  # noqa: BLE001
        return (False, "error", repr(e))


def pull(vault_root, timeout=60):
    """fetch + rebase. 호출 전 로컬 변경은 commit_local로 커밋돼 있어야 한다(autostash 미사용).
    HEAD가 바뀌면 changed=True. 충돌이면 rebase --abort로 원복하고 ("conflict")로 표면화한다.
    abort가 실패하면 저장소가 충돌 상태로 남으므로 ("error")로 올린다 — 다음 주기가
    충돌 마커를 커밋하지 않게 하기 위해서다."""
    try:
        before = _head(vault_root, timeout)
        # 원격·브랜치를 **명시**한다. 인자 없는 pull은 현재 브랜치의 upstream을
        # 따르므로, upstream이 잘못 걸려 있으면 엉뚱한 브랜치를 정본에 섞는다.
        r = _git(vault_root, ["pull", "--rebase", "origin", SYNC_BRANCH], timeout)
        if r.returncode != 0:
            out = (r.stdout + r.stderr)
            low = out.lower()
            if (_in_rebase(vault_root, timeout)
                    or any(s in low for s in ("conflict", "could not apply"))):
                ab = _git(vault_root, ["rebase", "--abort"], timeout)
                if ab.returncode != 0 or _in_rebase(vault_root, timeout):
                    return (False, "error",
                            ("rebase --abort 실패 — 저장소가 충돌 상태다(수동 개입 필요): "
                             + (ab.stdout + ab.stderr).strip())[-600:])
                return (False, "conflict", out.strip()[-600:])
            return (False, "error", out.strip()[-600:])
        after = _head(vault_root, timeout)
        return (before != after, "ok", "")
    except subprocess.TimeoutExpired:
        return (False, "error", f"git pull timeout ({timeout}s)")
    except Exception as e:  # noqa: BLE001
        return (False, "error", repr(e))


def commit_push(vault_root, message, timeout=60):
    """commit_local → push. push가 non-fast-forward로 거부되면 ("rejected")로 표면화
    (호출부가 pull-rebase 후 재시도한다)."""
    committed, status, detail = commit_local(vault_root, message, timeout)
    if status != "ok":
        return (False, status, detail)
    try:
        # refspec 명시 — 밀어 넣는 곳이 언제나 origin의 SYNC_BRANCH다.
        p = _git(vault_root, ["push", "origin", f"{SYNC_BRANCH}:{SYNC_BRANCH}"], timeout)
        if p.returncode != 0:
            out = (p.stdout + p.stderr)
            low = out.lower()
            if "rejected" in low or "non-fast-forward" in low or "fetch first" in low:
                return (False, "rejected", out.strip()[-600:])
            return (False, "error", out.strip()[-600:])
        return (True, "ok", "")
    except subprocess.TimeoutExpired:
        return (False, "error", f"git push timeout ({timeout}s)")
    except Exception as e:  # noqa: BLE001
        return (False, "error", repr(e))
