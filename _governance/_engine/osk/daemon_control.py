"""osk.daemon_control — 갱신이 적용되는 동안 동기화 데몬을 세워 둔다 (Mechanism §1-2 7항).

데몬이 도는 채로 엔진 파일을 바꾸면 옛 코드가 새 파일 위에서 돈다. 데몬을 찾아 멈추고, 동기화
잠금을 쥔 채 적용한 뒤, 멈춘 데몬을 다시 띄운다."""
from __future__ import annotations
import base64, os, signal, subprocess, time
from contextlib import contextmanager
from pathlib import Path
from .locks import mutation_lock_path as core_mutation_lock_path
from ._portalock import lock_exclusive, unlock
from . import update_state
from . import sync_status
from .vault import Vault


@contextmanager
def exclusive(path: Path, busy: str):
    """비차단 배타 잠금 — 경합이면 `busy` 사유로 중단한다. **획득한 경우에만**
    해제한다(소유하지 않은 구간의 unlock은 Windows에서 실제 해제 연산이라
    원래 결과를 덮는 오류를 낸다)."""
    f = open(path, "w")
    ok = False
    try:
        try:
            lock_exclusive(f, blocking=False)
            ok = True
        except OSError:
            raise update_state.UpdateError(busy)
        yield
    finally:
        if ok:
            unlock(f)
        f.close()


def sync_lock_path(vault: Vault) -> Path:
    """엔진·데몬과 **공유하는 mutation 잠금** — 데몬 싱글턴 잠금이 아니다.
    노드 쓰기·보호영역 조작·데몬의 working-tree 구간이 모두 이 하나를 잡으므로,
    update가 이걸 잡으면 그 사이 누구도 혼합 상태를 만들거나 커밋·push하지
    못한다. 자리의 정의는 엔진에 한 벌뿐이다(locks.mutation_lock_path)."""
    return core_mutation_lock_path(vault)


DAEMON_RESTART_WAIT = 30   # 초 — 멈춘 데몬이 다시 보일 때까지 기다리는 한도


def _daemon_script(vault: Vault) -> Path:
    return vault.root / "_governance" / "_engine" / "sync_daemon.py"


def _powershell(script: str) -> str:
    """인용 규칙을 거치지 않도록 EncodedCommand로 넘기고 UTF-8로 받는다."""
    code = "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + script
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                        base64.b64encode(code.encode("utf-16-le")).decode()],
                       capture_output=True, timeout=120)
    return r.stdout.decode("utf-8", "replace")


def daemon_pids(vault: Vault) -> list[int]:
    """이 vault의 `sync_daemon.py`를 **절대 경로로** 실행 중인 프로세스들.
    예약 작업·launchd·systemd 예시가 모두 절대 경로로 띄운다. Windows의 venv
    pythonw는 실제 인터프리터를 자식으로 띄우므로 둘 다 걸린다."""
    target = str(_daemon_script(vault))
    if os.name == "nt":
        rows = _powershell("Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
                           "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        fold = lambda s: s.replace("/", "\\").lower()
        pairs = [line.partition("\t")[::2] for line in rows.splitlines()]
    else:
        rows = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                              text=True, timeout=60).stdout
        fold = lambda s: s
        pairs = [line.strip().partition(" ")[::2] for line in rows.splitlines()]
    return [int(pid) for pid, cmd in pairs
            if pid.strip().isdigit() and fold(target) in fold(cmd)]


def kill_processes(pids: list[int]) -> None:
    for pid in pids:
        if os.name == "nt":          # /T: venv 런처와 자식 인터프리터를 함께
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=60)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def lock_within(f, seconds: float, busy: str) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            return lock_exclusive(f, blocking=False)
        except OSError:
            if time.monotonic() > deadline:
                raise update_state.UpdateError(busy)
            time.sleep(0.05)


def _start_daemon(vault: Vault) -> dict:
    """멈춘 데몬을 다시 띄우고 돌아왔는지 보고한다. Windows는 그 데몬을 띄우는
    예약 작업을 실행하고, launchd KeepAlive·systemd Restart=always는 잠금이
    풀리면 스스로 다시 띄운다."""
    via = "launchd KeepAlive / systemd Restart=always"
    if os.name == "nt":
        path = str(_daemon_script(vault)).lower().replace("'", "''")
        task = _powershell(
            "$p = '__P__'\n"
            "$t = Get-ScheduledTask | Where-Object { (($_.Actions | ForEach-Object "
            "{ \"$($_.Execute) $($_.Arguments)\" }) -join ' ').ToLower().Replace('/', '\\')"
            ".Contains($p) } | Select-Object -First 1\n"
            "if ($t) { Start-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath; "
            "$t.TaskPath + $t.TaskName }".replace("__P__", path)).strip()
        if not task:
            return {"restarted": False,
                    "note": "이 vault의 데몬을 띄우는 예약 작업이 없다 — 데몬을 직접 띄운다"}
        via = f"scheduled task {task}"
    deadline = time.monotonic() + DAEMON_RESTART_WAIT
    while time.monotonic() < deadline:
        if daemon_pids(vault):
            return {"restarted": True, "via": via}
        time.sleep(2)
    return {"restarted": False, "via": via,
            "note": f"{DAEMON_RESTART_WAIT}초 안에 데몬이 돌아오지 않았다 — 데몬을 직접 띄운다"}


@contextmanager
def daemon_stopped(vault: Vault, info: dict):
    """적용 내내 데몬 싱글턴(osk-sync.lock)을 쥔다. 이 vault의 데몬이 쥐고
    있으면 **mutation 잠금을 먼저 잡고** 그 데몬을 끝낸다 — 그러면 데몬은
    작업 트리 변경 구간 안에 있지 않고, Git 전송 중 강제 종료는 데몬이 이미
    견딘다(SETUP 동기화 데몬). 풀리는 즉시 싱글턴을 넘겨받아, 서비스 관리자가
    먼저 띄운 구판 데몬이 적용 도중 끼어들지 못하게 한다. 끝나면(실패해도)
    멈춘 데몬을 다시 띄우고 결과를 `info`에 남긴다."""
    f = open(sync_status.lock_path(vault.root, "osk-sync.lock"), "w")
    held = False
    try:
        try:
            lock_exclusive(f, blocking=False)
            held = True
        except OSError:
            pids = daemon_pids(vault)
            if not pids:
                raise update_state.UpdateError(
                    "동기화 데몬 잠금이 잡혀 있는데 이 vault의 sync_daemon.py 프로세스를 "
                    "찾지 못했다 — 데몬을 멈춘 뒤 다시 실행한다")
            m = open(sync_lock_path(vault), "w")
            try:
                lock_within(m, 120, "작업 트리 변경 잠금이 2분 넘게 풀리지 않는다 — "
                                     "잠시 후 다시 실행한다")
                try:
                    kill_processes(pids)
                    info["stopped"] = pids
                    lock_within(f, 30, "데몬을 멈췄는데 싱글턴 잠금이 풀리지 않는다 — "
                                        "다시 실행한다")
                    held = True
                finally:
                    unlock(m)
            finally:
                m.close()
        yield
    finally:
        if held:
            unlock(f)
        f.close()
        if "stopped" in info:
            info.update(_start_daemon(vault))
