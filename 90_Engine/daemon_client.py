"""90_Engine/daemon_client.py — vault 데몬 디스커버리 + 경량 HTTP 클라이언트.

mcp_server(프록시)와 vault_daemon이 **같은 포트 계산**을 쓰도록 공유한다. 무거운 의존성
(fastapi/uvicorn) 없이 stdlib만 사용하므로 프록시 import 비용이 작다. See docs/DAEMON_DESIGN.md.
"""
import os
import sys
import json
import time
import hashlib
import subprocess
import urllib.request
from pathlib import Path


def daemon_port(vault_db) -> int:
    """vault DB 경로 기반 결정적 포트. hash()는 프로세스마다 salt가 달라 쓰면 안 되므로
    hashlib로 계산 → 프록시와 데몬이 동일 포트에 합의한다."""
    env = os.environ.get("DAEMON_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass  # 잘못된 값이면 결정적 포트로 폴백
    h = int(hashlib.md5(str(Path(vault_db).resolve()).encode("utf-8")).hexdigest(), 16)
    return 40000 + (h % 2000)


def base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def health(port: int, timeout: float = 1.0):
    """데몬 /health 응답(dict) 또는 None."""
    try:
        with urllib.request.urlopen(base(port) + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def get(port: int, path: str, timeout: float = 30.0):
    with urllib.request.urlopen(base(port) + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(port: int, path: str, payload: dict, timeout: float = 120.0):
    req = urllib.request.Request(
        base(port) + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _daemon_python(script_dir) -> str:
    """데몬을 띄울 인터프리터. 두 가지를 동시에 만족해야 한다:
    (1) **venv 의존성(duckdb/fastapi)이 있는** 인터프리터일 것 — 아니면 import에서 즉사.
    (2) Windows에서 **콘솔 창을 띄우지 않을 것**(pythonw.exe = GUI 서브시스템).

    sys.executable/sys.prefix를 쓰면 안 되는 이유: 프록시가 venv 런처로 기동돼도 Windows에선
    base Python으로 redirect돼 실제 실행 컨텍스트의 sys.prefix가 base를 가리키는 경우가 있다
    (이 기기 실측: 프록시 자식 프로세스 = base Python312, sys.prefix = base → deps 없음 →
    데몬 즉사 → 매 read마다 재spawn → 검은 콘솔 반복). 그래서 sys.* 대신 **스크립트 위치**에서
    venv를 도출한다: vault_daemon.py는 `<repo>/90_Engine/`에 있고 venv는 `<repo>/.venv`라
    `script_dir.parent/.venv`가 실행 인터프리터와 무관하게 항상 옳다. 그 venv의 pythonw.exe는
    GUI 서브시스템이라 콘솔을 절대 할당하지 않으면서도 venv prefix/deps를 그대로 갖는다.
    See handoff/DAEMON_SPAWN_FIX.md."""
    root = Path(script_dir).parent  # <repo>/90_Engine -> <repo>
    if os.name == "nt":
        cands = [
            root / ".venv" / "Scripts" / "pythonw.exe",  # 1순위: 콘솔 무창 + venv deps
            root / ".venv" / "Scripts" / "python.exe",   # 폴백: deps는 있으나 콘솔 위험
            Path(sys.prefix) / "Scripts" / "pythonw.exe",
            Path(sys.prefix) / "Scripts" / "python.exe",
        ]
    else:
        cands = [
            root / ".venv" / "bin" / "python",
            Path(sys.prefix) / "bin" / "python",
        ]
    for c in cands:
        if c.exists():
            return str(c)
    return sys.executable


def ensure_daemon(vault_db, script_dir, env=None, wait: float = 20.0):
    """데몬이 떠 있으면 port 반환. 아니면 detached로 기동하고 /health ready까지 폴링.
    실패하면 None(프록시는 read 직접 폴백 / write 에러)."""
    port = daemon_port(vault_db)
    if health(port):
        return port
    script = str(Path(script_dir) / "vault_daemon.py")
    # 관측성: 기본은 조용(DEVNULL). DAEMON_DEBUG면 데몬 stdout/stderr를 spawn 로그로 캡처해
    # import-단계 즉사(예: 'duckdb 미설치')가 드러나게 한다 — 무음 폴백이 데몬 완전 실패를
    # 가리지 않도록. See handoff/DAEMON_SPAWN_FIX.md §6.
    spawn_log = (open(Path(script_dir) / "daemon.spawn.log", "ab")
                 if os.environ.get("DAEMON_DEBUG") else None)
    try:
        sink = spawn_log if spawn_log else subprocess.DEVNULL
        kwargs = dict(stdout=sink, stderr=sink, stdin=subprocess.DEVNULL,
                      env=env or os.environ, close_fds=True)
        if os.name == "nt":  # Windows: 부모와 분리 + 콘솔 무창
            # DETACHED_PROCESS(0x8): 부모 콘솔 미상속. CREATE_NEW_PROCESS_GROUP(0x200): 신호 격리.
            # CREATE_NO_WINDOW(0x8000000): console-subsystem 폴백(python.exe)일 때도 창 억제.
            # 1순위 인터프리터는 pythonw.exe(GUI 서브시스템)라 본질적으로 콘솔이 없다.
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
        else:  # POSIX: 새 세션으로 분리
            kwargs["start_new_session"] = True
        # 콘솔 무창 venv 인터프리터로 spawn(script_dir 기준 도출; _daemon_python 도큐 참조).
        subprocess.Popen([_daemon_python(script_dir), script], **kwargs)
    except Exception:
        return None
    finally:
        if spawn_log:
            spawn_log.close()
    deadline = time.time() + wait
    while time.time() < deadline:
        if health(port):
            return port
        time.sleep(0.3)
    return None
