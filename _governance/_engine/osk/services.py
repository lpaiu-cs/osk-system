"""운영체제 서비스 등록 — 설치 도구(`osk.setup`)가 이 vault의 정기 실행과 동기화 데몬을
이 기기의 서비스 관리자에 잇는다(Mechanism §1-2 8항). Windows는 작업 스케줄러, macOS는
launchd LaunchAgent, Linux는 systemd 사용자 유닛이다.

  · 이 vault의 등록인지는 명령이 이 vault의 스크립트(`growth_run.py`·`sync_daemon.py`)를
    부르는지로 가린다. 옛 안내서의 이름(`osk-domain-growth`·`osk-sync-daemon`, 예시의
    `com.example.ltm-vault-daemon`·`ltm-vault-daemon`)으로 손수 만든 등록도 명령이 이
    vault 안의 파일을 부르면 이 vault의 것으로 본다 — 그대로 두고 새로 만들면 같은 일이
    두 번 돈다.
  · 새 등록의 이름에는 vault 경로의 해시를 단다 — 한 기기에 vault가 여럿이어도 겹치지
    않는다. 이 vault의 옛 등록은 걷어 내고 그 이름으로 새로 만든다(`replace`).
  · 바꾸거나 걷어 내기 전에 원래 정의를 `~/.osk-system/backups/`에 남긴다. 서비스 관리자의
    폴더(LaunchAgents 등)에 두면 관리자가 백업까지 읽을 수 있다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import core
from .harness import base

KINDS = ("growth", "sync")
LEGACY = {"growth": {"osk-domain-growth"},
          "sync": {"osk-sync-daemon", "com.example.ltm-vault-daemon", "ltm-vault-daemon"}}
GROWTH_ARGS = ("--limit", "3", "--timeout", "600")
# 정기 실행 한 번의 상한 — 에이전트 시한(600초)에 포착·계획의 여유를 더한다.
GROWTH_MINUTES = 30
LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def tag() -> str:
    """이 vault의 등록 이름에 다는 값 — 경로 표기(구분자·대소문자)와 무관하다."""
    return hashlib.sha256(base.fold(str(core.ROOT)).encode("utf-8")).hexdigest()[:8]


def script(kind: str) -> Path:
    from .doctor import _engine_dir
    engine = _engine_dir()
    return engine / "scripts" / "growth_run.py" if kind == "growth" else engine / "sync_daemon.py"


def interpreter() -> Path:
    """등록이 부를 Python — Windows는 창을 띄우지 않는 `pythonw.exe`다."""
    from .doctor import _python
    python = _python()
    quiet = python.with_name("pythonw.exe")
    return quiet if os.name == "nt" and quiet.is_file() else python


def job(kind: str, *, command_file: Path | None = None, at: str | None = None) -> dict:
    """등록할 일 — 해석기·인자·시각(정기 실행)."""
    args = [str(script(kind))]
    if kind == "growth":
        args += ["--command-file", str(command_file), *GROWTH_ARGS]
    return {"python": str(interpreter()), "args": args, "at": at}


def owns(kind: str, name: str, tokens: list[str]) -> bool:
    """그 등록이 이 vault의 `kind`인가."""
    if base.mentions(tokens, script(kind)):
        return True
    inside = base.fold(str(core.ROOT)) + "/"
    return name in LEGACY[kind] and any(base.fold(t).startswith(inside) for t in tokens)


def backups() -> Path:
    return Path.home() / ".osk-system" / "backups"


def _keep(source: Path, name: str, stamp: str) -> str:
    dest = backups() / f"{name}.osk-backup-{stamp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return str(dest)


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
                          creationflags=_NO_WINDOW)


def _text(data) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


class Backend:
    """서비스 관리자 하나. `entries`는 이 vault와 관련될 수 있는 등록 — 각 행은 `id`,
    `name`, 명령 `tokens`, 비교할 `definition`을 싣는다."""
    name = ""

    def __init__(self, run=None):
        self._run = run or _run

    def ident(self, kind: str) -> str:
        raise NotImplementedError

    def entries(self) -> list[dict]:
        raise NotImplementedError

    def definition(self, kind: str, job: dict):
        raise NotImplementedError

    def install(self, kind: str, job: dict, stamp: str) -> dict:
        raise NotImplementedError

    def remove(self, entry: dict, stamp: str) -> dict:
        raise NotImplementedError

    def _call(self, argv: list[str], *, check: bool = True) -> str:
        r = self._run(argv)
        out, err = _text(r.stdout), _text(r.stderr)
        if check and r.returncode:
            raise OSError(f"{' '.join(argv[:3])} 실패({r.returncode}): {(err or out).strip()[-300:]}")
        return out


# ── Windows: 작업 스케줄러 ────────────────────────────────────────────────
_LIST = r"""
$root = '__ROOT__'
$rows = @(Get-ScheduledTask | ForEach-Object {
  $acts = @($_.Actions | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskExecAction' } |
    ForEach-Object { [pscustomobject]@{ execute = [string]$_.Execute; arguments = [string]$_.Arguments;
                                        workdir = [string]$_.WorkingDirectory } })
  $text = (($acts | ForEach-Object { $_.execute + ' ' + $_.arguments + ' ' + $_.workdir }) -join ' ')
  if ($text.ToLower().Replace('/', '\').Contains($root)) {
    [pscustomobject]@{ path = [string]$_.TaskPath; name = [string]$_.TaskName; actions = $acts;
      triggers = @($_.Triggers | ForEach-Object { [pscustomobject]@{
        kind = [string]$_.CimClass.CimClassName; start = [string]$_.StartBoundary } });
      battery = [bool]$_.Settings.DisallowStartIfOnBatteries;
      limit = [string]$_.Settings.ExecutionTimeLimit }
  }
})
ConvertTo-Json -InputObject $rows -Depth 6 -Compress
"""

_REGISTER = r"""
$s = '__SPEC__' | ConvertFrom-Json
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $s.execute -Argument $s.arguments -WorkingDirectory $s.workdir
if ($s.at) { $trigger = New-ScheduledTaskTrigger -Daily -At $s.at }
else { $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes([int]$s.minutes))
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $s.name -TaskPath '\' -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Description $s.description -Force | Out-Null
if ($s.start) { Start-ScheduledTask -TaskName $s.name -TaskPath '\' }
"""


def _ps_literal(text: str) -> str:
    return text.replace("'", "''")


def _win_tokens(execute: str, arguments: str) -> list[str]:
    """작업 동작의 토큰 — 실행 호스트와 무관하게 Windows 명령줄 규칙으로 가른다."""
    try:
        tokens = shlex.split(f"{execute} {arguments}", posix=False)
    except ValueError:
        tokens = f"{execute} {arguments}".split()
    return [t.strip('"') for t in tokens]


class TaskScheduler(Backend):
    name = "task-scheduler"

    def ident(self, kind: str) -> str:
        return f"osk-{kind}-{tag()}"

    def _ps(self, script: str) -> str:
        code = ("$ErrorActionPreference = 'Stop'\n"
                "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + script)
        return self._call(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                           base64.b64encode(code.encode("utf-16-le")).decode()])

    # 네 호출만 PowerShell에 닿는다 — 시험은 이 넷을 바꿔 끼운다.
    def _list(self) -> list[dict]:
        root = str(core.ROOT).replace("/", "\\").lower()
        return json.loads(self._ps(_LIST.replace("__ROOT__", _ps_literal(root))) or "[]")

    def _register(self, spec: dict) -> None:
        self._ps(_REGISTER.replace("__SPEC__", _ps_literal(json.dumps(spec))))

    def _export(self, path: str, name: str) -> str:
        return self._ps(f"Export-ScheduledTask -TaskName '{_ps_literal(name)}' -TaskPath '{_ps_literal(path)}'")

    def _unregister(self, path: str, name: str) -> None:
        self._ps(f"Unregister-ScheduledTask -TaskName '{_ps_literal(name)}' "
                 f"-TaskPath '{_ps_literal(path)}' -Confirm:$false")

    def entries(self) -> list[dict]:
        out = []
        for row in self._list():
            actions = [{"execute": a.get("execute", ""), "arguments": a.get("arguments", ""),
                        "workdir": a.get("workdir", "")} for a in row.get("actions") or []]
            triggers = row.get("triggers") or []
            trigger = None
            if len(triggers) == 1:
                at = re.search(r"T(\d\d:\d\d)", triggers[0].get("start") or "")
                daily = triggers[0].get("kind") == "MSFT_TaskDailyTrigger"
                trigger = [triggers[0].get("kind"), at.group(1) if daily and at else None]
            out.append({"id": row["path"] + row["name"], "name": row["name"], "path": row["path"],
                        "tokens": [t for a in actions for t in _win_tokens(a["execute"], a["arguments"])],
                        "definition": {"actions": actions, "trigger": trigger,
                                       "battery": bool(row.get("battery")), "limit": row.get("limit")}})
        return out

    def _spec(self, kind: str, job: dict) -> dict:
        if kind == "growth":
            execute, arguments = job["python"], subprocess.list2cmdline(job["args"])
        else:
            # 작업에는 작업별 환경 변수가 없다 — `cmd.exe`가 켜는 키를 세우고 데몬을 떼어 띄운다.
            # `1&&`는 붙여 쓴다: 앞에 공백이 있으면 그 공백까지 값이 된다(GETTING-STARTED).
            execute = "cmd.exe"
            arguments = '/c set SYNC_ENABLED=1&& start "" "{}" "{}"'.format(job["python"], job["args"][0])
        return {"name": self.ident(kind), "execute": execute, "arguments": arguments,
                "workdir": str(core.ROOT), "at": job["at"],
                "minutes": GROWTH_MINUTES if kind == "growth" else 0,
                "description": f"osk-system {kind} — {core.ROOT}", "start": kind == "sync"}

    def definition(self, kind: str, job: dict) -> dict:
        s = self._spec(kind, job)
        return {"actions": [{"execute": s["execute"], "arguments": s["arguments"], "workdir": s["workdir"]}],
                "trigger": (["MSFT_TaskDailyTrigger", s["at"]] if kind == "growth"
                            else ["MSFT_TaskLogonTrigger", None]),
                "battery": False, "limit": f"PT{s['minutes']}M" if s["minutes"] else "PT0S"}

    def install(self, kind: str, job: dict, stamp: str) -> dict:
        spec = self._spec(kind, job)
        self._register(spec)
        return {"id": "\\" + spec["name"], "installed": True, **({"started": True} if spec["start"] else {})}

    def remove(self, entry: dict, stamp: str) -> dict:
        dest = backups() / f"{entry['name']}.osk-backup-{stamp}.xml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self._export(entry["path"], entry["name"]), encoding="utf-8")
        self._unregister(entry["path"], entry["name"])
        return {"id": entry["id"], "removed": True, "backup": str(dest)}


# ── macOS: launchd LaunchAgent ───────────────────────────────────────────
class Launchd(Backend):
    name = "launchd"

    def folder(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents"

    def ident(self, kind: str) -> str:
        return f"com.osk-system.{kind}.{tag()}"

    def _domain(self) -> str:
        return f"gui/{os.getuid() if hasattr(os, 'getuid') else 0}"

    def entries(self) -> list[dict]:
        out = []
        for file in sorted(self.folder().glob("*.plist")):
            try:
                data = plistlib.loads(file.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if not isinstance(data, dict):
                continue
            args = data.get("ProgramArguments")
            tokens = [a for a in args if isinstance(a, str)] if isinstance(args, list) else (
                [data["Program"]] if isinstance(data.get("Program"), str) else [])
            label = data["Label"] if isinstance(data.get("Label"), str) else file.stem
            out.append({"id": label, "name": label, "tokens": tokens, "file": str(file), "definition": data})
        return out

    def definition(self, kind: str, job: dict) -> dict:
        log = str(Path.home() / "Library" / "Logs" / f"osk-{kind}-{tag()}.log")
        env = {"PATH": LAUNCHD_PATH, "OSK_VAULT_ROOT": str(core.ROOT)}
        data = {"Label": self.ident(kind), "ProgramArguments": [job["python"], *job["args"]],
                "WorkingDirectory": str(core.ROOT), "EnvironmentVariables": env,
                "StandardOutPath": log, "StandardErrorPath": log, "ProcessType": "Background"}
        if kind == "sync":
            env["SYNC_ENABLED"] = "1"
            data.update(RunAtLoad=True, KeepAlive=True)
        else:
            hour, minute = job["at"].split(":")
            data["StartCalendarInterval"] = {"Hour": int(hour), "Minute": int(minute)}
        return data

    def install(self, kind: str, job: dict, stamp: str) -> dict:
        data = self.definition(kind, job)
        file = self.folder() / f"{data['Label']}.plist"
        file.parent.mkdir(parents=True, exist_ok=True)
        core.atomic_write(file, plistlib.dumps(data))
        self._call(["launchctl", "bootout", f"{self._domain()}/{data['Label']}"], check=False)
        self._call(["launchctl", "bootstrap", self._domain(), str(file)])
        return {"id": data["Label"], "installed": True, "file": str(file)}

    def remove(self, entry: dict, stamp: str) -> dict:
        file = Path(entry["file"])
        backup = _keep(file, file.name, stamp)
        self._call(["launchctl", "bootout", f"{self._domain()}/{entry['id']}"], check=False)
        file.unlink(missing_ok=True)
        return {"id": entry["id"], "removed": True, "backup": backup}


# ── Linux: systemd 사용자 유닛 ────────────────────────────────────────────
def _sd_quote(arg: str) -> str:
    """ExecStart의 인자 하나 — systemd는 `%`(지정자)와 `$`(환경 변수)를 먼저 푼다."""
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _sd_value(text: str) -> str:
    return text.replace("%", "%%")


class Systemd(Backend):
    name = "systemd"

    def folder(self) -> Path:
        config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(config) / "systemd" / "user"

    def ident(self, kind: str) -> str:
        return f"osk-{kind}-{tag()}"

    def entries(self) -> list[dict]:
        out = []
        for file in sorted(self.folder().glob("*.service")):
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            line = next((l.partition("=")[2] for l in text.splitlines()
                         if l.strip().startswith("ExecStart=")), "")
            try:
                tokens = shlex.split(line.strip().lstrip("-@:+!"))
            except ValueError:
                tokens = line.split()
            tokens = [t.replace("%%", "%").replace("$$", "$") for t in tokens]   # _sd_quote의 역
            files = {file.name: text}
            timer = file.with_suffix(".timer")
            if timer.is_file():
                files[timer.name] = timer.read_text(encoding="utf-8")
            out.append({"id": file.stem, "name": file.stem, "tokens": tokens,
                        "files": [str(file.parent / n) for n in files], "definition": files})
        return out

    def definition(self, kind: str, job: dict) -> dict:
        unit, root = self.ident(kind), _sd_value(str(core.ROOT))
        start = " ".join(_sd_quote(a) for a in [job["python"], *job["args"]])
        env = f'Environment="OSK_VAULT_ROOT={root}"\n'
        if kind == "sync":
            service = ("[Unit]\nDescription=osk-system sync daemon (git only)\n"
                       "After=network-online.target\nWants=network-online.target\n\n"
                       f"[Service]\nType=simple\nWorkingDirectory={root}\n{env}"
                       f"Environment=SYNC_ENABLED=1\nExecStart={start}\nRestart=always\nRestartSec=5\n\n"
                       "[Install]\nWantedBy=default.target\n")
            return {f"{unit}.service": service}
        service = ("[Unit]\nDescription=osk-system daily growth run\n\n"
                   f"[Service]\nType=oneshot\nWorkingDirectory={root}\n{env}"
                   f"ExecStart={start}\nTimeoutStartSec={GROWTH_MINUTES}min\n")
        timer = ("[Unit]\nDescription=osk-system daily growth run\n\n"
                 f"[Timer]\nOnCalendar=*-*-* {job['at']}:00\nPersistent=true\nUnit={unit}.service\n\n"
                 "[Install]\nWantedBy=timers.target\n")
        return {f"{unit}.service": service, f"{unit}.timer": timer}

    def install(self, kind: str, job: dict, stamp: str) -> dict:
        files = self.definition(kind, job)
        self.folder().mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            core.atomic_write(self.folder() / name, text.encode("utf-8"))
        unit = self.ident(kind) + (".timer" if kind == "growth" else ".service")
        self._call(["systemctl", "--user", "daemon-reload"])
        self._call(["systemctl", "--user", "enable", "--now", unit])
        return {"id": self.ident(kind), "installed": True, "files": [str(self.folder() / n) for n in files]}

    def remove(self, entry: dict, stamp: str) -> dict:
        backup = [_keep(Path(f), Path(f).name, stamp) for f in entry["files"]]
        units = sorted((Path(f).name for f in entry["files"]), key=lambda n: not n.endswith(".timer"))
        self._call(["systemctl", "--user", "disable", "--now", *units], check=False)
        for f in entry["files"]:
            Path(f).unlink(missing_ok=True)
        self._call(["systemctl", "--user", "daemon-reload"], check=False)
        return {"id": entry["id"], "removed": True, "backup": backup}


def backend(run=None) -> Backend | None:
    """이 기기의 서비스 관리자 — 없으면 None(수동 절차를 따른다)."""
    if os.name == "nt":
        return TaskScheduler(run)
    if sys.platform == "darwin":
        return Launchd(run)
    return Systemd(run) if shutil.which("systemctl") else None


def display(job: dict) -> str:
    return core.shell_join([job["python"], *job["args"]])


def plan(manager: Backend | None, kind: str, job: dict | None, uninstall: bool) -> dict:
    """등록 하나의 조치 — `keep`·`add`·`replace`·`remove`·`absent`. 이 vault의 옛 등록은
    `remove`에 싣고, 이 vault를 부르지만 osk 등록이 아닌 것은 `notes`로 알린다."""
    if manager is None:
        return {"action": "error", "error": "이 기기의 서비스 관리자(작업 스케줄러·launchd·"
                                            "systemd)를 찾지 못했다 — SETUP의 수동 절차를 따른다"}
    out: dict = {"backend": manager.name, "id": manager.ident(kind)}
    try:
        rows = manager.entries()
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {**out, "action": "error", "error": f"등록을 읽지 못했다: {e}"}
    mine = [r for r in rows if owns(kind, r["name"], r["tokens"])]
    inside = base.fold(str(core.ROOT)) + "/"
    others = [r["id"] for r in rows if not any(owns(k, r["name"], r["tokens"]) for k in KINDS)
              and any(base.fold(t).startswith(inside) for t in r["tokens"])]
    if others:
        out["notes"] = [f"{i}: 이 vault를 부르는 다른 등록이 있다 — 건드리지 않는다" for i in others]
    if uninstall:
        out["action"] = "remove" if mine else "absent"
    elif (len(mine) == 1 and mine[0]["name"] == manager.ident(kind)
          and mine[0]["definition"] == manager.definition(kind, job)):
        out["action"] = "keep"
    else:
        out["action"] = "replace" if mine else "add"
    if mine and out["action"] != "keep":
        out["remove"] = [r["id"] for r in mine]
    if job and not uninstall:
        out["command"] = display(job) + (f" — 매일 {job['at']}" if job.get("at") else " — 로그온할 때")
    return out


def apply(manager: Backend, kind: str, job: dict | None, approved: dict, uninstall: bool,
          stamp: str) -> dict:
    """확인한 조치를 한다 — 실행 직전에 다시 계획해, 확인한 것과 다르면 하지 않는다."""
    step = {"step": f"{kind} ({manager.name})"}
    now = plan(manager, kind, job, uninstall)
    if (now.get("action"), now.get("remove")) != (approved.get("action"), approved.get("remove")):
        return {**step, "ok": False, "error": "확인 뒤 등록이 바뀌었다 — 하지 않았다. "
                                              "setup을 다시 계획해 확인받는다"}
    done, backups_ = [], []
    rows = {r["id"]: r for r in manager.entries()}
    for ident in now.get("remove", []):
        result = manager.remove(rows[ident], stamp)
        done.append(result["id"])
        backups_ += result["backup"] if isinstance(result["backup"], list) else [result["backup"]]
    if not uninstall and now["action"] in ("add", "replace"):
        done.append(manager.install(kind, job, stamp)["id"])
    return {**step, "ok": True, "action": now["action"], "done": done,
            **({"backups": backups_} if backups_ else {})}
