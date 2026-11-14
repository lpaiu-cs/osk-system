"""회귀 수트의 한 갈래: 동기화 데몬 — rebase와 push, 정본 브랜치, 사용자의 미완료 git 작업.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from unittest import mock

from regression.support import check, ENGINE, rmtree_force, skip
from osk import core  # noqa: E402


# ── 8. sync: 순서·충돌 표면화 + lock 위치 + SYNC_ENABLED 게이트 ──────────
def approve_sync(repo):
    """`setup --apply --sync`가 이 기기에 적는 전송 대상 — 데몬은 적힌 fetch·push 대상으로만 보내고,
    기록이 없으면 커밋만 하고 전송을 보류한다(#254). vault처럼 `.osk/`는 추적하지 않는다."""
    import vault_sync
    url, pushes = vault_sync.remote_targets(repo)
    (repo / ".osk").mkdir(exist_ok=True)
    (repo / vault_sync.TARGETS).write_text(json.dumps({"origin": url, "push": pushes}),
                                           encoding="utf-8")
    with open(repo / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:
        f.write(".osk/\n")


def test_sync():
    import sync_daemon
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bare = td / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)

        def clone(name):
            d = td / name
            subprocess.run(["git", "clone", "-q", str(bare), str(d)], check=True)
            subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)
            return d
        A = clone("A")
        (A / "f.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q", "origin", "HEAD:main"], check=True)
        B = clone("B")
        approve_sync(B)
        # A가 원격을 전진시키고, B는 추적 파일이 dirty
        (A / "g.txt").write_text("remote\n")
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "remote-change"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q"], check=True)
        (B / "f.txt").write_text("base\nlocal\n")      # dirty (비충돌 라인)
        st = sync_daemon.once(B)
        ahead = subprocess.run(
            ["git", "-C", str(B), "rev-list", "--left-right", "--count",
             "HEAD...origin/main"], capture_output=True, text=True).stdout.split()
        check("sync once=ok (dirty+원격 전진 통합)", st == "ok", st)
        check("sync 후 ahead/behind 0", ahead == ["0", "0"], ahead)

        # 데몬 엔트리: lock은 <root>/.git/ 안, SYNC_ENABLED 없으면 기동 거부
        env = {k: v for k, v in os.environ.items() if k != "SYNC_ENABLED"}
        env["OSK_VAULT_ROOT"] = str(B)
        r = subprocess.run([sys.executable, str(ENGINE / "sync_daemon.py"), "--once"],
                           capture_output=True, text=True, env=env, timeout=60)
        check("SYNC_ENABLED 부재 → 기동 거부(기본 꺼짐, 템플릿 계약)",
              r.returncode != 0 and "비활성" in (r.stderr + r.stdout), r)
        env["SYNC_ENABLED"] = "1"
        r = subprocess.run([sys.executable, str(ENGINE / "sync_daemon.py"), "--once"],
                           capture_output=True, text=True, env=env, timeout=60)
        check("SYNC_ENABLED=1 → once 실행", r.returncode == 0 and "ok" in r.stdout,
              (r.returncode, r.stdout, r.stderr))
        check("lock은 실제 git 디렉터리 안(추적 트리 오염 없음)",
              (B / ".git/osk-sync.lock").exists()
              and not (B / "osk-sync.lock").exists()
              and not (ENGINE / "osk-sync.lock").exists())

        # worktree(.git이 디렉터리가 아니라 파일)에서도 추적 트리로 폴백하지 않는다
        wt = td / "B-wt"
        subprocess.run(["git", "-C", str(B), "worktree", "add", "-q",
                        "--detach", str(wt)], check=True)
        subprocess.run([sys.executable, str(ENGINE / "sync_daemon.py"), "--once"],
                       capture_output=True, text=True, timeout=60,
                       env=dict(env, OSK_VAULT_ROOT=str(wt)))
        stray = list(wt.rglob("osk-sync.lock")) + list(ENGINE.glob("osk-sync.lock"))
        check("worktree(.git이 파일)에서도 추적 트리에 lock을 만들지 않는다",
              not stray, stray)

        # 대장 동시 append는 교착되지 않는다 — union merge (.gitattributes)
        led = "00_Scope/Workbench/_ledger/signatures.jsonl"
        attrs = (ENGINE.parent.parent / ".gitattributes").read_text(encoding="utf-8")
        for repo in (A, B):
            (repo / ".gitattributes").write_text(attrs, encoding="utf-8")
        (A / led).parent.mkdir(parents=True, exist_ok=True)
        (A / led).write_text('{"rid":"base","kind":"sign"}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(A), "pull", "-q", "--rebase"], check=True)
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "ledger-base"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q"], check=True)
        check("B가 대장 기준선을 받는다", sync_daemon.once(B) == "ok")
        with open(A / led, "a", encoding="utf-8") as f:
            f.write('{"rid":"a-side","kind":"sign"}\n')
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "a-side"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q"], check=True)
        with open(B / led, "a", encoding="utf-8") as f:
            f.write('{"rid":"b-side","kind":"sign"}\n')
        st3 = sync_daemon.once(B)
        body = (B / led).read_text(encoding="utf-8")
        check("다기기 대장 동시 append가 교착되지 않는다(union merge)",
              st3 == "ok", st3)
        check("양쪽 기록이 모두 보존된다(한쪽이 소실되지 않는다)",
              "a-side" in body and "b-side" in body, body)
        check("병합 결과에 충돌 마커가 없다", "<<<<<<<" not in body)

        # 진짜 충돌은 상태 표면화
        subprocess.run(["git", "-C", str(A), "pull", "-q", "--rebase"], check=True)
        (A / "f.txt").write_text("conflict-remote\n")
        subprocess.run(["git", "-C", str(A), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(A), "commit", "-qm", "c2"], check=True)
        subprocess.run(["git", "-C", str(A), "push", "-q"], check=True)
        (B / "f.txt").write_text("conflict-local\n")
        st2 = sync_daemon.once(B)
        check("충돌은 삼키지 않고 표면화",
              "충돌" in st2 or "rejected" in st2 or "실패" in st2, st2)


def test_sync_graph_scale():
    """실제 rebase·push: 배율만 해결하고, 의미 있는 차이와 실패는 전체 원복한다."""
    import contextlib, io
    import sync_daemon, vault_sync

    path = ".obsidian/graph.json"
    base = '{"scale":1,"search":"all","showTags":true}\n'
    local = base.replace('"scale":1', '"scale":2')
    remote = base.replace('"scale":1', '"scale":3')
    cases = [
        ("scale", local, remote),
        ("cp949", local.replace("all", "한글"), remote.replace("all", "한글")),
        ("crlf", local.replace("\n", "\r\n"), remote.replace("\n", "\r\n")),
        ("crlf-settings", local.replace("all", "local").replace("\n", "\r\n"),
         remote.replace("\n", "\r\n")),
        ("invalid-utf8-local", local, remote),
        ("invalid-utf8-remote", local, remote),
        ("settings", local.replace('"all"', '"local"'), remote),
        ("json-types", local.replace("true", "false"), remote.replace("true", "0")),
        ("malformed", local.rstrip()[:-1], remote),
        ("duplicate", local.replace('"all"', '"lost","search":"all"'), remote),
        ("boolean-scale", local.replace('"scale":2', '"scale":true'), remote),
        ("infinite-scale", local.replace('"scale":2', '"scale":1e999'), remote),
        ("zero-scale", local.replace('"scale":2', '"scale":0'), remote),
        ("add-add", local, remote),
        ("delete-modify", None, remote),
        ("mixed-note", local, remote),
        ("later-note", local, remote),
        ("stage-error", local, remote),
    ]
    for name, left, right in cases:
        temp = Path(tempfile.mkdtemp(prefix="osk-graph-sync-"))
        repo, bare = temp / "기기 repo", temp / "origin.git"
        left_bytes = left.encode("utf-8") if left is not None else None
        right_bytes = right.encode("utf-8")
        if name == "invalid-utf8-local":
            left_bytes = left_bytes.replace(b"all", b"\xff")
        if name == "invalid-utf8-remote":
            right_bytes = right_bytes.replace(b"all", b"\xff")

        def git(*args, root=repo):
            r = vault_sync.run_git(root, list(args), 30)
            assert r.returncode == 0, (args, r.stdout, r.stderr)
            return r.stdout.strip()

        def commit(message):
            git("add", "-A")
            git("commit", "-qm", message)

        try:
            repo.mkdir()
            bare.mkdir()
            git("init", "-q", "--bare", "-b", "main", root=bare)
            git("init", "-q", "-b", "main")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "core.autocrlf", "false")
            git("config", "core.editor", "must-not-open-editor")
            git("remote", "add", "origin", str(bare))
            approve_sync(repo)
            graph_file = repo / path
            graph_file.parent.mkdir()
            if name != "add-add":
                graph_file.write_bytes(base.encode("utf-8"))
            note = repo / "충돌 노트.md"
            note.write_text("base\n", encoding="utf-8")
            commit("base")
            # Both devices previously synchronized this base. Without that
            # history, first-contact divergence is intentionally held for review.
            git("push", "-q", "origin", "main")
            git("branch", "remote-main")

            if left is None:
                graph_file.unlink()
            else:
                graph_file.write_bytes(left_bytes)
            (repo / "local.md").write_text("local note\n", encoding="utf-8")
            if name == "mixed-note":
                note.write_text("local\n", encoding="utf-8")
            commit("local graph and note")
            if name == "later-note":
                note.write_text("local\n", encoding="utf-8")
                commit("later local note conflict")
            else:
                (repo / "later.md").write_text("later local note\n", encoding="utf-8")
                commit("later local note")
            before = git("rev-parse", "HEAD"), git("ls-files", "--stage")

            git("checkout", "-q", "remote-main")
            graph_file.parent.mkdir(exist_ok=True)
            graph_file.write_bytes(right_bytes)
            (repo / "remote.md").write_text("remote note\n", encoding="utf-8")
            if name in ("mixed-note", "later-note"):
                note.write_text("remote\n", encoding="utf-8")
            commit("remote graph and note")
            git("push", "-q", "origin", "HEAD:main")
            remote_head = git("rev-parse", "refs/heads/main", root=bare)
            git("checkout", "-q", "main")

            original_git = vault_sync.run_git
            blob_modes = []

            def git_faults(root, args, timeout, **kwargs):
                if name == "stage-error" and args == ["add", "--", path]:
                    return subprocess.CompletedProcess(args, 1, "", "injected stage failure")
                if name == "cp949" and args[0] == "show":
                    blob_modes.append(kwargs.get("text", True))
                    # blob 읽기의 부모 Python 기본 디코더만 Windows CP949로 모사한다.
                    with mock.patch("subprocess._text_encoding", return_value="cp949"):
                        return original_git(root, args, timeout, **kwargs)
                return original_git(root, args, timeout, **kwargs)

            log = io.StringIO()
            with mock.patch.object(vault_sync, "run_git", side_effect=git_faults),\
                    contextlib.redirect_stderr(log):
                status = sync_daemon.once(repo)
            check(f"graph {name}: 진행 작업·미병합 index가 남지 않는다",
                  vault_sync._in_progress(repo, 10) is None)
            if name == "cp949":
                check("graph cp949: 두 blob 모두 로케일 디코더를 거치지 않는다",
                      blob_modes == [False, False], blob_modes)
            if name in ("scale", "cp949", "crlf"):
                check(f"graph {name}: 전체 동기화 성공", status == "ok", status)
                check(f"graph {name}: 로컬 원문을 그대로 보존",
                      graph_file.read_bytes() == left_bytes)
                check(f"graph {name}: 양쪽 노트와 후속 커밋을 보존",
                      all((repo / filename).is_file()
                          and (repo / filename).read_text() == body
                          for filename, body in (("local.md", "local note\n"),
                                                 ("remote.md", "remote note\n"),
                                                 ("later.md", "later local note\n"))))
                check(f"graph {name}: 원격까지 전달",
                      git("rev-parse", "HEAD") == git("rev-parse", "refs/heads/main", root=bare))
                check(f"graph {name}: 자동 해결을 로그로 알린다", path in log.getvalue())
                check(f"graph {name}: 다음 주기도 정상", sync_daemon.once(repo) == "ok")
            else:
                check(f"graph {name}: 수동 충돌로 보고", "pull 충돌" in status, status)
                expected_path = "충돌 노트.md" if name == "later-note" else path
                check(f"graph {name}: hint에 밀려 파일명이 사라지지 않는다",
                      expected_path in status, status)
                check(f"graph {name}: 앞선 자동 해결까지 HEAD·index 전체 원복",
                      before == (git("rev-parse", "HEAD"), git("ls-files", "--stage")))
                check(f"graph {name}: 로컬 파일 원문 보존",
                      graph_file.read_bytes() == left_bytes if left_bytes is not None
                      else not graph_file.exists())
                check(f"graph {name}: 원격 변경 금지",
                      git("rev-parse", "refs/heads/main", root=bare) == remote_head)
        finally:
            rmtree_force(temp)


# ── 8-2. 동기화 대상 브랜치 고정 ─────────────────────────────────────────
def test_sync_pins_main():
    """데몬은 HEAD를 따라가지 않는다 — 다른 브랜치가 checkout돼 있어도 정본은
    언제나 main이다. 깨끗하면 전환하고, 더러우면 아무것도 하지 않는다."""
    import sync_daemon, vault_sync
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bare = td / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        R = td / "R"
        subprocess.run(["git", "clone", "-q", str(bare), str(R)], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(R), "config", k, v], check=True)
        approve_sync(R)
        (R / "f.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(R), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(R), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(R), "push", "-q", "origin", "HEAD:main"],
                       check=True)
        subprocess.run(["git", "-C", str(R), "branch", "-M", "main"], check=True)
        check("전제: main 고정 상수", vault_sync.SYNC_BRANCH == "main")

        # ① 깨끗한 다른 브랜치 → main으로 전환하고 동기화한다
        subprocess.run(["git", "-C", str(R), "checkout", "-q", "-b", "side"],
                       check=True)
        (R / "onmain.txt").write_text("via daemon\n")
        st = sync_daemon.once(R)
        cur = vault_sync.current_branch(R)
        check("깨끗한 side 브랜치에서 main으로 전환 후 동기화", st == "ok", st)
        check("전환 결과 HEAD가 main", cur == "main", cur)
        on_main = subprocess.run(
            ["git", "-C", str(R), "ls-tree", "-r", "--name-only", "main"],
            capture_output=True, text=True).stdout
        check("커밋이 main에 실렸다", "onmain.txt" in on_main, on_main)
        side_has = subprocess.run(
            ["git", "-C", str(R), "ls-tree", "-r", "--name-only", "side"],
            capture_output=True, text=True).stdout
        check("side 브랜치는 건드리지 않았다", "onmain.txt" not in side_has, side_has)

        # ② 더러운 다른 브랜치 → 전환하지 않고 거부, 아무것도 쓰지 않는다
        subprocess.run(["git", "-C", str(R), "checkout", "-q", "side"], check=True)
        (R / "f.txt").write_text("base\n진행 중인 수정\n")   # 추적 파일 수정
        before = subprocess.run(["git", "-C", str(R), "rev-parse", "main"],
                                capture_output=True, text=True).stdout.strip()
        st2 = sync_daemon.once(R)
        check("더러운 비-main에서는 동기화를 거부한다",
              "브랜치 고정 실패" in st2, st2)
        check("거부 시 HEAD를 옮기지 않는다",
              vault_sync.current_branch(R) == "side", vault_sync.current_branch(R))
        check("거부 시 사용자의 미커밋 작업이 그대로 남는다",
              "진행 중인 수정" in (R / "f.txt").read_text(encoding="utf-8"))
        after = subprocess.run(["git", "-C", str(R), "rev-parse", "main"],
                               capture_output=True, text=True).stdout.strip()
        check("거부 시 main은 전진하지 않는다", before == after, (before, after))

        # ③ detached HEAD(깨끗)도 main으로 회복한다
        subprocess.run(["git", "-C", str(R), "checkout", "-q", "--", "f.txt"],
                       check=True)
        subprocess.run(["git", "-C", str(R), "checkout", "-q", "--detach"], check=True)
        check("detached HEAD 전제", vault_sync.current_branch(R) is None)
        st3 = sync_daemon.once(R)
        check("detached HEAD에서도 main으로 회복", st3 == "ok", st3)
        check("회복 후 HEAD가 main", vault_sync.current_branch(R) == "main")


# ── 8-3. 데몬은 콘솔 없는 pythonw로 돈다 — git bare-spawn 재도입 금지 ──────
def test_daemon_no_bare_git_spawn():
    """검은 콘솔 재발 방지(2026-08-09). 데몬은 pythonw(콘솔 없음)로 돌아,
    직접 subprocess로 git.exe를 spawn하면 Windows가 tick마다 새 콘솔 창을
    할당한다. `_lock_path`가 이 실수를 저질렀고, v2.2.0이 그 함수를 매 tick
    호출로 승격하며 15분 주기 증상으로 표면화됐다. 데몬의 git은 전부
    vault_sync.run_git(=CREATE_NO_WINDOW 하드닝의 단일 소유자)를 거쳐야 한다 —
    이 계약을 AST로 고정해 새 호출부가 조용히 bare-spawn을 되살리지 못하게 한다."""
    import ast
    src = (ENGINE / "sync_daemon.py").read_text(encoding="utf-8")
    bare = [f"line {n.lineno}: subprocess.{n.func.attr}(...)"
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "subprocess"]
    check("sync_daemon은 subprocess로 직접 git을 spawn하지 않는다 "
          "(전부 vault_sync.run_git 게이트웨이 경유)", not bare, "; ".join(bare))


# ── 잠금 자리 해석의 세 형태 (worktree·submodule 포함) ──────────────────
def test_local_lock_path_git_shapes():
    """잠금 자리가 클론 형태마다 달라지면 같은 vault의 두 프로세스가 서로 다른
    파일을 잡아 상호배제가 조용히 깨진다 — `.git`이 디렉터리·파일(gitdir)·
    worktree(commondir)인 세 형태와 git 부재를 모두 같은 규칙으로 푼다."""
    import tempfile as _tf
    base = Path(_tf.mkdtemp(prefix="osk-shape-"))
    try:
        plain = base / "plain"; (plain / ".git").mkdir(parents=True)
        check("`.git` 디렉터리 → 그 안",
              core.local_lock_path("x.lock", plain) == plain / ".git" / "x.lock")
        # worktree: `.git`이 파일이고 그 대상 안에 commondir가 있다
        main_git = base / "main" / ".git"
        (main_git / "worktrees" / "wt").mkdir(parents=True)
        (main_git / "worktrees" / "wt" / "commondir").write_text("../..\n",
                                                                encoding="utf-8")
        wt = base / "wt"; wt.mkdir()
        (wt / ".git").write_text(
            f"gitdir: {main_git / 'worktrees' / 'wt'}\n", encoding="utf-8")
        check("worktree → 공용 git 디렉터리(commondir)",
              core.local_lock_path("x.lock", wt).resolve()
              == (main_git / "x.lock").resolve(),
              core.local_lock_path("x.lock", wt))
        # submodule 등: `.git` 파일이 commondir 없는 디렉터리를 가리킨다
        sub_git = base / "modules" / "s"; sub_git.mkdir(parents=True)
        sm = base / "sm"; sm.mkdir()
        (sm / ".git").write_text(f"gitdir: {sub_git}\n", encoding="utf-8")
        check("gitdir 파일 → 그 디렉터리",
              core.local_lock_path("x.lock", sm).resolve()
              == (sub_git / "x.lock").resolve())
        nogit = base / "nogit"; nogit.mkdir()
        p = core.local_lock_path("x.lock", nogit)
        check("git 부재 → 추적 트리 밖 임시 자리",
              not str(p).startswith(str(nogit)) and p.name.startswith("x-"), p)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_sync_pending_git_operations():
    """사용자의 미완료 git 작업·index를 데몬이 대신 완료하지 않는다."""
    import sync_daemon, vault_sync
    temp = Path(tempfile.mkdtemp(prefix="osk-sync-pending-"))
    repo, bare = temp / "repo", temp / "origin.git"

    def git(*args, expected=0, root=repo):
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                           text=True, timeout=30,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        assert r.returncode == expected, (args, r.stdout, r.stderr)
        return r.stdout

    def commit(body, message):
        (repo / "note.md").write_text(body, encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", message)

    try:
        repo.mkdir()
        bare.mkdir()
        git("init", "-q", "--bare", "-b", "main", root=bare)
        git("init", "-q", "-b", "main")
        git("config", "user.name", "fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "core.autocrlf", "false")
        commit("base\n", "base")
        git("checkout", "-qb", "side")
        commit("side\n", "side")
        git("checkout", "-q", "main")
        commit("main\n", "main")
        git("remote", "add", "origin", str(bare))
        approve_sync(repo)
        git("push", "-qu", "origin", "main")
        remote_head = git("rev-parse", "refs/heads/main", root=bare)
        for operation in ("merge", "cherry-pick", "revert", "rebase"):
            git(operation, "side", expected=1)
            before = (git("rev-parse", "HEAD"), git("ls-files", "--stage"),
                      (repo / "note.md").read_bytes())
            check(f"{operation}: 충돌 전제", bool(git("ls-files", "--unmerged")))
            check(f"{operation}: 데몬이 거부", "실패" in sync_daemon.once(repo))
            for call in (lambda: vault_sync.commit_local(repo, "must refuse"),
                         lambda: vault_sync.commit_push(repo, "must refuse"),
                         lambda: vault_sync.pull(repo)):
                check(f"{operation}: 직접 호출도 거부", call()[1] == "error")
            after = (git("rev-parse", "HEAD"), git("ls-files", "--stage"),
                     (repo / "note.md").read_bytes())
            check(f"{operation}: HEAD·index·내용 보존", before == after)
            check(f"{operation}: 원격 변경 없음",
                  git("rev-parse", "refs/heads/main", root=bare) == remote_head)
            # 충돌을 수동 stage해도 진행 표식이 남아 있으면 대신 완료하면 안 된다.
            git("add", "note.md")
            check(f"{operation}: stage된 충돌도 보류",
                  vault_sync.commit_local(repo, "must refuse staged")[1] == "error")
            git(operation, "--abort")
        # MERGE_HEAD를 잃은 미병합 index도 독립적으로 보호한다.
        git("merge", "side", expected=1)
        (repo / ".git/MERGE_HEAD").unlink()
        before_index = git("ls-files", "--stage")
        check("작업 표식 없는 미병합 index도 보류",
              vault_sync.commit_local(repo, "must refuse unmerged")[1] == "error"
              and git("ls-files", "--stage") == before_index)
        git("reset", "--hard", "-q", "HEAD")
        for name in ("rebase-apply", "sequencer", "BISECT_START"):
            marker = repo / ".git" / name
            marker.mkdir() if name != "BISECT_START" else marker.write_text("main\n")
            try:
                check(f"{name}: 충돌 없는 진행 작업도 보류",
                      vault_sync.ensure_branch(repo)[1] == "error"
                      and vault_sync.commit_local(repo, "must refuse pending")[1] == "error")
            finally:
                marker.rmdir() if marker.is_dir() else marker.unlink()
        original_git = vault_sync.run_git
        for bad in (["rev-parse", "--absolute-git-dir"],
                    ["ls-files", "--unmerged", "-z"], ["add", "-A"]):
            calls = []
            def fail_query(root, args, timeout):
                calls.append(args)
                return (subprocess.CompletedProcess(args, 128, "", "injected git error")
                        if args == bad else original_git(root, args, timeout))
            with mock.patch.object(vault_sync, "run_git", side_effect=fail_query):
                result = vault_sync.commit_local(repo, "must refuse git error")
            check(f"git 실패 {bad[0]}: 후속 commit 금지",
                  result[1] == "error" and not any(a[0] == "commit" for a in calls))
        git("checkout", "-q", "side")
        for bad in (["symbolic-ref", "--quiet", "--short", "HEAD"],
                    ["status", "--porcelain", "-z"]):
            with mock.patch.object(vault_sync, "run_git", side_effect=fail_query):
                result = vault_sync.ensure_branch(repo)
            check(f"git 실패 {bad[0]}: 브랜치 전환 금지",
                  result[1] == "error" and git("branch", "--show-current").strip() == "side")
        git("checkout", "-q", "main")
        check("정상 저장소는 계속 동기화", sync_daemon.once(repo) == "ok")
    finally:
        rmtree_force(temp)


def test_sync_git_unknown_is_not_absent():
    """git이 답하지 못한 것(timeout·실패)을 "저장소 아님"·"원격 없음"으로 접지 않고,
    낮은 등급으로 뜬 데몬도 git은 보통 등급으로 띄운다. 2026-10-12 실측: 예약 작업
    우선순위 7(BELOW_NORMAL)의 데몬이 CPU 포화에서 굶어 `rev-parse` 10초 timeout이
    수주간 "git 저장소 아님"으로, `remote` timeout이 "원격 없음"으로 기록됐다."""
    import sync_daemon, vault_sync
    temp = Path(tempfile.mkdtemp(prefix="osk-sync-unknown-"))
    repo, plain = temp / "repo", temp / "plain"
    try:
        repo.mkdir()
        plain.mkdir()
        vault_sync.run_git(repo, ["init", "-q", "-b", "main"], 30)
        vault_sync.run_git(repo, ["remote", "add", "origin", str(temp / "origin.git")], 30)
        check("저장소가 아닌 자리는 그대로 '저장소 아님'",
              sync_daemon.once(plain) == "git 저장소 아님")
        real = vault_sync.run_git
        for verb, fault in (("rev-parse", "timeout"), ("rev-parse", "error"),
                            ("remote", "timeout"), ("remote", "error")):
            def faulty(root, args, timeout, **kw):
                if args[0] != verb:
                    return real(root, args, timeout, **kw)
                if fault == "timeout":
                    raise subprocess.TimeoutExpired(["git", *args], timeout)
                return subprocess.CompletedProcess(args, 128, "", "fatal: detected dubious ownership")
            with mock.patch.object(vault_sync, "run_git", side_effect=faulty):
                try:
                    status = sync_daemon.once(repo)
                except (subprocess.TimeoutExpired, RuntimeError) as e:
                    status = e
            check(f"{verb} {fault}: 모름을 '없음'으로 접지 않는다",
                  isinstance(status, Exception), status)
        if os.name != "nt":
            skip("BELOW_NORMAL로 뜬 데몬의 git 등급", "Windows 우선순위 등급")
            return
        # 굶은 채 import하면 수트가 분 단위로 멈추므로, import 뒤에 등급을 낮춘다.
        # 등급은 데몬 자신이 아니라 _git이 띄운 git 프로세스에서 읽는다.
        code = ("import ctypes, subprocess, sys, vault_sync, sync_daemon\n"
                "k32 = ctypes.windll.kernel32\n"
                "k32.SetPriorityClass(ctypes.c_void_p(-1), 0x4000)\n"
                "seen = []\n"
                "class Spy(subprocess.Popen):\n"
                "    def __init__(self, *a, **k):\n"
                "        super().__init__(*a, **k)\n"
                "        seen.append(k32.GetPriorityClass(ctypes.c_void_p(int(self._handle))))\n"
                "subprocess.Popen = Spy\n"
                "sys.argv = ['sync_daemon.py', '--once']\n"
                "sync_daemon.main()\n"
                "print(sorted(set(seen)))\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=ENGINE, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           env=dict(os.environ, SYNC_ENABLED="1", OSK_VAULT_ROOT=str(plain),
                                    PYTHONIOENCODING="utf-8"))
        check("BELOW_NORMAL로 뜬 데몬도 git을 보통 등급(0x20)으로 띄운다",
              r.returncode == 0 and "[32]" in r.stdout, (r.returncode, r.stdout, r.stderr[-300:]))
    finally:
        rmtree_force(temp)
