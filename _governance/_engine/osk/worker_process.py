"""osk.worker_process — 엔진이 띄우는 바깥 작업자의 프로세스.

명령이 뜰 수 있는지 에이전트를 띄우지 않고 확인하고(`check_command`), 이 실행이 만든 프로세스
트리만 멈춘다(`stop_tree`). 작업자에게 무엇을 시킬지는 모른다."""
from __future__ import annotations
import os
from pathlib import Path
import signal
import subprocess
import shutil
from . import native_cli
from .vault import Vault


def _tree(table: dict[int, tuple[int, str]], born: dict[int, int]) -> dict[int, int]:
    """born ({pid: start}) with every process of the snapshot that descends from it. A process
    older than its parent only names a dead parent's reused pid (10-23: Taskmgr.exe)."""
    from .native_cli import process_created
    born, frontier = dict(born), list(born)
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, _) in table.items():
            if ppid == parent and pid not in born and (created := process_created(pid) or 0) >= born[parent]:
                born[pid] = created
                frontier.append(pid)
    return born


def _running(tree: dict[int, int]) -> bool:
    """Whether the tree, or a descendant it started after its snapshot, still runs. Termination is
    asynchronous, so allow it about five seconds; a pid counts only with its own start time."""
    import time
    from .native_cli import process_created, process_table
    for _ in range(50):
        now = process_table()
        if now and not any(pid in now and process_created(pid) == start
                           for pid, start in _tree(now, tree).items()):
            return False
        time.sleep(0.1)
    return True


def stop_tree(proc: subprocess.Popen) -> str | None:
    """Kill only the process tree/group created by this runner; report uncertain cleanup."""
    error = None
    try:
        if os.name == "nt":
            from .native_cli import process_created, process_table
            table = process_table()
            start = process_created(proc.pid) if proc.pid in table else None
            tree = _tree(table, {proc.pid: start}) if start else {}
            killed = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                    capture_output=True, timeout=30, shell=False,
                                    creationflags=0x08000000)
            # /T는 잎부터 죽인다. 자식과 함께 끝나는 부모(venv 런처)는 제 차례 전에 이미 없어
            # taskkill이 실패를 낸다 — 죽이기 전 트리나 그 뒤 트리가 낳은 자손이 아직 돌 때만
            # 정리 실패다(#231 리뷰: 루트가 자식을 낳고 먼저 끝나면 taskkill은 아무것도 못 죽인다).
            # ponytail: 부모가 보이기 전에 죽은 손주는 걸음에서 빠진다(taskkill /T도 같다) —
            # 작업자가 이중 fork를 하면 Popen 때부터 Job Object로 묶는다.
            if killed.returncode and (not tree or _running(tree)):
                error = "process-tree cleanup was not confirmed: " + killed.stderr.decode(errors="replace")
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = "process-tree cleanup failed: " + str(exc)
    try:
        if proc.poll() is None:
            proc.kill()        # fallback contains the direct process; keep tree uncertainty visible
        proc.wait(timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = "direct process termination was not confirmed: " + str(exc)
    return error


def check_command(vault: Vault, command: list[str], *, follow_desktop_update: bool = True) -> dict:
    """Resolve argv[0] without starting an agent or changing vault state. Later arguments may be
    empty: an empty value is a real argument (Claude's `--tools ""` turns every built-in tool off)."""
    if not isinstance(command, list) or not command or not isinstance(command[0], str) or not command[0] or any(
            not isinstance(arg, str) or "\0" in arg for arg in command):
        raise ValueError("command must be a nonempty argv list")
    program = command[0]
    try:
        if follow_desktop_update:
            program = native_cli.resolve(program)
    except (OSError, ValueError) as exc:
        return {"ok": False, "state": "invalid_command", "executable": None, "violations": [str(exc)]}
    if "/" in program or "\\" in program:
        program = str(vault.root / program)
    search_path = None
    if os.name == "posix":
        # exec searches PATH after chdir(cwd); Windows searches from the caller.
        search_path = os.pathsep.join(str(vault.root / entry) for entry in os.get_exec_path())
    executable = shutil.which(program, path=search_path)
    if not executable:
        return {"ok": False, "state": "invalid_command", "executable": None,
                "violations": ["Growth executable is unavailable: " + command[0]]}
    executable = str(Path(executable).absolute())  # Keep venv/launcher symlink entrypoints intact.
    # Windows starts a batch file (an npm `claude.cmd`) through cmd.exe, which reads the line
    # list2cmdline wrote by its own rules: it toggles quotes on `\"` too, so a JSON argument can
    # leave a path's `&` outside quotes and split the command. Refuse what cmd would change.
    acted = (_cmd_acts_on(subprocess.list2cmdline([executable, *command[1:]]))
             if os.name == "nt" and Path(executable).suffix.lower() in (".bat", ".cmd") else None)
    if acted:
        return {"ok": False, "state": "invalid_command", "executable": executable,
                "violations": [f"Growth executable is a batch file: cmd.exe would act on {acted!r} in "
                               f"its arguments and change them — use the native CLI (.exe): {executable}"]}
    return {"ok": True, "state": "ready", "executable": executable, "violations": []}


def _cmd_acts_on(line: str) -> str | None:
    """The first character cmd.exe would act on in a command line: an operator or escape outside
    quotes, an expansion or line break anywhere. None when cmd passes the line on unchanged."""
    quoted = False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch in "%!\r\n" or not quoted and ch in "&|<>^":
            return ch
    return None
