"""osk.setup — 이 vault를 이 기기의 하네스에 잇는다(Mechanism §1-2 8항).

설치의 세 경로 — 에이전트에게 붙여 넣는 프롬프트(docs/INSTALL-AGENT.md), 선택
마법사(`--interactive`), 수동 문서 — 가 모두 이 모듈을 부른다. 부트스트랩
(`scripts/setup.py`)이 시스템 Python으로 `.venv`와 의존성을 갖춘 뒤 venv의 엔진으로
여기에 넘긴다.

하는 일:
  · 릴리스 기준선 — 기록이 없으면 `osk.update`로 이 vault가 받은 판(`release.json`)을
    기준선으로 적는다. 그 갱신의 계획(`review_id`)이 이 계획의 확인에 묶인다.
  · MCP 서버 — 호스트 CLI로 등록한다. CLI가 PATH에 없으면 사람이 할 명령으로 남긴다.
  · 훅 — 호스트의 훅 설정 파일에 세 훅을 병합한다.
  · 고를 때만(`--fork`·`--schedule`·`--sync`) — 백그라운드 fork가 부를 CLI
    (`.osk/response-growth.json`), 정기 실행(`.osk/growth-command.json`과 운영체제의 매일
    작업), 동기화 데몬(운영체제의 상시 서비스). 운영체제 등록은 `osk.services`가 한다.

vault 밖에는 사용자가 확인한 계획의 osk 등록 항목만 쓴다. 원래 파일은 옆에
`<이름>.osk-backup-<시각>`으로 백업하고, 이 vault의 osk 항목만 더하거나 바꾸거나 걷어
낸다 — 명령이 이 vault의 스크립트·서버를 부르는지로 알아본다. 다시 실행해도 항목을
겹쳐 만들지 않는다. 설치 경로를 따로 기록하지 않고 지금 설정만 본다. 대상 파일은
SETUP이 열거한다.

확인은 `osk.update`와 같다. `--apply`의 첫 호출은 계획과 `approval_required`를 내고
종료코드 2로 멈춘다. 사용자가 확인한 뒤 같은 명령을 1시간 안에 다시 부르면 계획이
그대로일 때만 적용한다. 할 일이 없으면 확인 없이 끝난다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import core, services, update
from . import harness as adapters
from .doctor import _engine_dir, _python
from .harness import base

TICKET = "osk-setup-confirmation.json"
CONFIRM_WITHIN = 3600
CHANGES = ("add", "replace", "remove")
AT = "09:00"                      # 정기 실행의 기본 시각
_AT = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
INSTRUCTION = ("여기서 멈추고 사용자에게 이 계획을 설명한 뒤 명시적 확인을 받는다 — 기준선과 "
               "통치 구획 보호, 호스트별 MCP·훅 변경, 고른 기능(fork CLI·정기 실행의 명령과 "
               "시각·동기화 데몬의 origin), 백업 자리, 사용자가 할 일(human). 확인 "
               "전에는 다시 실행하지 않는다. 확인 뒤 같은 명령을 1시간 안에 한 번 다시 실행하면 "
               "적용한다. 계획이 그 사이 달라지면 새 확인을 요구한다.")


class SetupError(Exception):
    pass


def _script(event: str) -> Path:
    return _engine_dir() / "scripts" / "hooks" / base.SCRIPTS[event]


def _server() -> Path:
    return _engine_dir() / "mcp_server.py"


def _command(event: str) -> str:
    """훅 설정에 넣을 한 줄 — doctor가 등록을 읽는 규칙으로 인용한다. 경로는 `/`로
    쓴다: 호스트가 어느 셸로 돌리든(PowerShell·cmd·bash) 같은 파일을 가리킨다."""
    return base.hook_line([_python().as_posix(), _script(event).as_posix()])


def hosts(only: list[str] | None = None) -> list:
    """이을 호스트 — 이름을 주면 그것만, 아니면 이 기기에서 쓰는 흔적(설정 폴더나
    PATH의 CLI)이 있는 호스트."""
    if only:
        return [adapters.get(name) for name in dict.fromkeys(only)]
    return [a for a in adapters.ADAPTERS if a.home().is_dir() or shutil.which(a.cli)]


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise SetupError(f"{path}: JSON 객체가 아니다")
    return data


def _hooks(adapter, uninstall: bool) -> dict:
    """훅 설정 파일 하나의 변경 — 사건별 조치(`keep`·`add`·`replace`·`remove`·`absent`)와,
    바뀔 때의 새 내용(`content`, 보고에는 싣지 않는다)."""
    files = adapter.hook_files()
    if not files:
        return {"file": None, "events": {}, "changed": False}
    path = files[0]
    try:
        data = _read_json(path)
        table = data.get("hooks", {})
        if not isinstance(table, dict):
            raise SetupError(f"{path}: `hooks`가 JSON 객체가 아니다")
        _servers, registered, _errors = adapter.registrations()
    except (OSError, ValueError, SetupError) as e:
        return {"file": str(path), "error": f"{type(e).__name__}: {e}", "events": {}, "changed": False}
    new, events, notes = dict(table), {}, set()
    for event, name in adapter.events.items():
        script = _script(event)
        groups = table.get(name) if isinstance(table.get(name), list) else []
        kept, mine = [], 0
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list) or not entries:
                kept.append(group)
                continue
            rest = [h for h in entries
                    if not (isinstance(h, dict) and base.mentions(base.command_tokens(h), script))]
            mine += len(entries) - len(rest)
            if rest:
                kept.append(group if len(rest) == len(entries) else {**group, "hooks": rest})
            other = next((o for h in rest if isinstance(h, dict)
                          for o in [base.refers(base.command_tokens(h), script.name)] if o), None)
            if other:
                notes.add(f"{name}: 다른 vault의 osk 훅이 함께 돈다({other}) — 손대지 않는다")
        for h in registered:
            if h["event"] == name and base.fold(str(h["file"])) != base.fold(str(path)) \
                    and base.mentions(h["tokens"], script):
                notes.add(f"{name}: {h['file']}에도 이 vault의 훅이 등록돼 있다 — 한 곳으로 줄인다"
                          "(osk는 같은 호출을 한 번만 처리한다)")
        desired = adapter.hook_group(event, _command(event))
        if uninstall:
            action, result = ("remove" if mine else "absent"), kept
        elif mine == 1 and desired in groups:
            action, result = "keep", groups
        else:
            action, result = ("replace" if mine else "add"), kept + [desired]
        events[name] = action
        if result:
            new[name] = result
        else:
            new.pop(name, None)
    out = {"file": str(path), "events": events,
           "changed": any(a in CHANGES for a in events.values())}
    if notes:
        out["notes"] = sorted(notes)
    if out["changed"]:
        content = {k: v for k, v in data.items() if k != "hooks"}
        if new:
            content["hooks"] = new
        out["content"] = content
    return out


def _mcp(adapter, uninstall: bool) -> dict:
    """MCP 등록의 조치 — 호스트 CLI로 실행할 명령(`run`)이나, CLI가 없을 때 사람이 할
    명령(`manual`). 제 것인지는 CLI가 실제로 고치는 파일(`mcp_target`)의 등록으로만
    가린다. 다른 파일에서 찾은 등록으로 CLI를 부르면, 그 파일이 아닌 곳에 있는 남의
    등록이 바뀐다."""
    server, python, target = _server(), _python(), adapter.mcp_target()
    try:
        servers, _hooks, errors = adapter.registrations()
    except (OSError, ValueError) as e:
        return {"action": "error", "error": f"{type(e).__name__}: {e}"}
    mcp_errors = [e for e in errors if any(str(f) in e for f in adapter.mcp_files())]
    if mcp_errors:
        return {"action": "error", "error": "; ".join(mcp_errors)}
    ours = {base.fold(str(target))} if target else set()
    named = [s for s in servers if s["name"] == base.MCP_NAME and base.fold(str(s["file"])) in ours]
    mine = [s for s in named if base.mentions(s["tokens"], server)]
    want = [base.fold(python.as_posix())]
    same = [s for s in mine if [base.fold(t) for t in base.before(s["tokens"], server)] == want]
    out: dict = {"file": str(target) if target else None}
    elsewhere = sorted({str(s["file"]) for s in servers
                        if s["name"] == base.MCP_NAME and base.fold(str(s["file"])) not in ours})
    if elsewhere:
        out["notes"] = [f"{f}에도 {base.MCP_NAME} 등록이 있다 — setup은 CLI가 고치는 파일만 다룬다"
                        for f in elsewhere]
    if uninstall:
        out["action"], runs = ("remove", [adapter.mcp_remove_argv()]) if mine else ("absent", [])
    elif len(named) == 1 and same:
        out["action"], runs = "keep", []
    else:
        out["action"] = "replace" if named else "add"
        runs = ([adapter.mcp_remove_argv()] if named else []) + \
            [adapter.mcp_argv(python.as_posix(), server.as_posix())]
        others = [s["tokens"] for s in named if s not in mine]
        if others:
            out["replaces"] = [" ".join(t) for t in others]
    if runs:
        out["files"] = [str(target)] if target and target.is_file() else []
        if shutil.which(adapter.cli):
            out["run"] = runs
        else:
            out["manual"] = " ; ".join(core.shell_join(argv) for argv in runs)
    return out


def _native_cli(adapter) -> str | None:
    """fork·정기 실행이 부를 네이티브 CLI — 데스크톱 앱이 둔 CLI가 있으면 가장 새로 설치된
    것이다(판이 바뀌면 `native_cli`가 같은 설치의 새 판을 따라간다). 없으면 PATH의 CLI."""
    found = sorted((p for p in adapter.cli_candidates() if p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    path = found[0] if found else shutil.which(adapter.cli)
    return Path(path).as_posix() if path else None


def _synced(path: Path) -> bool:
    """그 파일이 Git에 실려 다른 기기로 가는가 — `.osk/`를 무시하지 않는 옛 vault의 함정이다."""
    try:
        r = subprocess.run(["git", "-C", str(core.ROOT), "check-ignore", "-q", str(path)],
                           capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                           creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 1          # 0 무시된다 · 1 무시되지 않는다 · 128 저장소가 아니다


def _fork(targets: list, uninstall: bool) -> dict:
    """백그라운드 fork가 부를 CLI(`.osk/response-growth.json`) — 호스트마다 `keep`·`add`·
    `replace`·`remove`, CLI를 찾지 못하면 `manual`. 적힌 경로가 살아 있으면 그대로 둔다 —
    사용자가 고른 CLI일 수 있다."""
    from . import response_growth
    path = response_growth.CONFIG
    out: dict = {"file": str(path), "entries": {}}
    try:
        current = _read_json(path)
    except (OSError, ValueError, SetupError) as e:
        return {**out, "error": f"{type(e).__name__}: {e}"}
    for adapter in targets:
        have = current.get(adapter.name)
        if not adapter.fork or (uninstall and have is None):
            continue
        if uninstall:
            out["entries"][adapter.name] = {"action": "remove", "path": have}
        elif isinstance(have, str) and Path(have).is_file():
            out["entries"][adapter.name] = {"action": "keep", "path": have}
        else:
            cli = _native_cli(adapter)
            out["entries"][adapter.name] = ({"action": "replace" if have else "add", "path": cli}
                                            if cli else {"action": "manual"})
    if not uninstall and out["entries"] and _synced(path):
        out["notes"] = [f"{path}이 Git에서 빠지지 않는다 — 이 기기의 CLI 경로가 다른 기기로 "
                        "간다. `.gitignore`에 `.osk/`를 둔다"]
    return out


def _schedule(manager, harness: str | None, at: str, uninstall: bool) -> dict:
    """정기 실행 — 에이전트 명령 파일(`.osk/growth-command.json`)과 운영체제의 매일 작업.
    명령 파일이 이미 있으면 그대로 쓴다(사용자가 고친 명령일 수 있다). 없으면 고른
    하네스가 내는 무인 명령을 만든다 — 어댑터가 내지 않으면 사용자가 만든다."""
    from . import growth
    file = core.ROOT / ".osk" / "growth-command.json"
    out: dict = {"command_file": str(file), "at": at}
    if uninstall:
        out["task"] = services.plan(manager, "growth", None, True)
        return out
    if not _AT.fullmatch(at):
        return {**out, "error": f"시각은 24시간제 HH:MM이다 — {at!r}"}
    if file.is_file():
        try:
            argv = json.loads(file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            return {**out, "error": f"명령 파일을 읽지 못했다 — {e}"}
        out["command"] = {"action": "keep", "argv": argv}
    else:
        if not harness:
            return {**out, "error": "정기 실행에 쓸 하네스를 고른다 — 예: `--schedule claude`"}
        adapter = adapters.get(harness)
        cli = _native_cli(adapter)
        if not cli:
            return {**out, "error": f"{adapter.title} CLI를 찾지 못했다 — 설치한 뒤 다시 실행한다"}
        argv = adapter.growth_argv(cli, _python().as_posix(), _server().as_posix(), core.ROOT.as_posix())
        if not argv:
            return {**out, "error": f"{adapter.title}의 정기 실행 명령은 setup이 만들지 않는다 — 명령 "
                                    "파일을 직접 만든 뒤 다시 실행한다(SETUP 'Scope에서 Domain으로 "
                                    "정기 재검토')"}
        out["command"] = {"action": "add", "argv": argv, "harness": harness}
    try:
        checked = growth.check_command(argv)
    except ValueError as e:
        checked = {"ok": False, "violations": [str(e)]}
    if not checked["ok"]:
        return {**out, "error": "명령을 쓸 수 없다 — " + "; ".join(checked["violations"])}
    out["task"] = services.plan(manager, "growth", services.job("growth", command_file=file, at=at), False)
    notes = []
    if out["task"].get("action") == "add" and growth.daily_active():
        notes.append("최근 3일 안에 정기 실행이 돌았다 — 다른 기기에 등록돼 있으면 한 곳에만 둔다"
                     "(결과는 대장으로 모든 기기가 나눈다)")
    if out["command"]["action"] == "add" and _synced(file):
        notes.append(f"{file}이 Git에서 빠지지 않는다 — `.gitignore`에 `.osk/`를 둔다")
    if notes:
        out["notes"] = notes
    return out


def _canonical(url: str) -> bool:
    """origin이 공개 정본인가 — HTTPS·SSH 표기, `.git`, 대소문자를 가리지 않는다."""
    def norm(u: str) -> str:
        u = re.sub(r"\.git$", "", u.strip().rstrip("/").lower())
        return re.sub(r"^[a-z+]+://", "", u).split("@")[-1].replace(":", "/")
    return norm(url) == norm(update.DEFAULT_UPSTREAM)


def _sync_checks() -> tuple[str | None, list[str], list[str]]:
    """동기화 데몬의 전제 — 저장소 루트, 로컬 `main`, 개인 원격 `origin`, 묻지 않는 push.
    (fetch 주소, push 대상들, 오류). push 대상은 `pushurl`과 `pushInsteadOf`를 푼 실제 전송
    자리다 — fetch 주소만 보면 다른 곳으로 가는 push를 확인하지 못한다."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(core.ROOT), *args], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    try:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode or base.fold(top.stdout.strip()) != base.fold(str(core.ROOT)):
            return None, [], ["vault가 Git 저장소의 루트가 아니다 — 데몬은 저장소 루트에서만 돈다"]
        url = git("remote", "get-url", "origin").stdout.strip() or None
        pushes = [u.strip() for u in git("remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
                  if u.strip()] if url else []
        main = git("rev-parse", "--verify", "--quiet", "refs/heads/main").returncode == 0
        errors = [] if main else ["로컬 `main` 브랜치가 없다 — 데몬은 `main`만 동기화한다"]
        public = [u for u in dict.fromkeys([url, *pushes]) if u and _canonical(u)]
        if not url:
            errors.append("`origin`이 없다 — 개인 저장소를 origin으로 둔다(시작 안내서 1단계)")
        elif public:
            errors.append("origin의 fetch·push 대상에 공개 정본 저장소가 있다(" + ", ".join(public)
                          + ") — 개인 저장소만 둔다")
        elif main:
            code, out, err = update.git_unattended(
                ["-C", str(core.ROOT), "push", "--dry-run", "--porcelain", "origin", "main"], timeout=90)
            # 원격이 앞서 있으면 거절(`[rejected]`)되지만 인증은 통과했다 — 데몬은 먼저 rebase한다.
            if code and "[rejected]" not in out:
                errors.append("묻지 않고 push하지 못했다 — 자격 증명 도우미·토큰·ssh-agent를 갖춘다: "
                              + (err or out).strip()[-200:])
        return url, pushes, errors
    except (OSError, subprocess.SubprocessError) as e:
        return None, [], [f"git을 실행하지 못했다 — {e}"]


def _sync(manager, uninstall: bool) -> dict:
    """동기화 데몬 — 전제를 확인한 뒤 운영체제의 상시 서비스로 둔다. push 대상은 계획에 실려
    확인 대상이 된다."""
    if uninstall:
        return {"task": services.plan(manager, "sync", None, True)}
    url, pushes, errors = _sync_checks()
    if errors:
        return {"origin": url, "push": pushes, "error": "; ".join(errors)}
    return {"origin": url, "push": pushes,
            "task": services.plan(manager, "sync", services.job("sync"), False)}


def _release() -> str:
    try:
        version = json.loads((core.ROOT / update.ATTESTATION).read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SetupError(f"{update.ATTESTATION}에서 판을 읽지 못했다 — 릴리스 태그를 clone했는가: {e}")
    if not update._semver(version):
        raise SetupError(f"{update.ATTESTATION}의 판이 정식 릴리스가 아니다: {version!r}")
    return version


def _baseline(uninstall: bool) -> dict | None:
    """릴리스 기준선 — 기록이 없으면 이 vault가 받은 판으로 적는 갱신의 계획."""
    if uninstall:
        return None
    try:
        current = update.current_version()
    except (OSError, ValueError) as e:
        return {"state": "error", "error": f"갱신 저널을 읽지 못했다: {e}"}
    if current:
        return {"state": "recorded", "current": current}
    try:
        version = _release()
        rep = update.run(ref=version)
    except (SetupError, update.UpdateError, OSError) as e:
        return {"state": "error", "error": str(e)}
    return {"state": "record", "version": version, "review_id": rep["review_id"],
            "files": rep.get("files"), "rebaseline": len(rep.get("rebaseline") or []),
            "add": len(rep.get("add") or []), "update": len(rep.get("update") or []),
            "conflict": len(rep.get("conflict") or []),
            "governance": (rep.get("governance") or {}).get("protect")}


def _human(items: list[dict], baseline: dict | None, uninstall: bool, extras: dict) -> list[str]:
    steps = []
    for item in items:
        adapter, mcp, hooks = adapters.get(item["harness"]), item["mcp"], item["hooks"]
        if mcp.get("manual"):
            steps.append(f"{adapter.title}: `{mcp['manual']}`를 실행한다 — `{adapter.cli}` CLI가 PATH에 없다")
        if hooks.get("changed") and adapter.trust and not uninstall:
            steps.append(adapter.trust)
        if hooks.get("changed") or mcp.get("action") in CHANGES:
            steps.append(f"{adapter.title}의 세션을 새로 연다 — 훅과 MCP는 세션을 시작할 때 읽힌다")
    if not items and not uninstall:
        steps.append("이 기기에서 Claude Code·Codex의 흔적(설정 폴더·PATH의 CLI)을 찾지 못했다 — "
                     "설치한 뒤 다시 실행하거나 --harness로 고른다")
    if baseline and baseline.get("state") == "record":
        steps.append("기준선 기록(`00_Scope/Workbench/_ledger/update.jsonl`)을 커밋한다")
    if not uninstall:
        steps += _extras_human(extras)
    if not uninstall and items:
        steps.append("새 세션을 연 뒤 `setup.py doctor`로 연결을 확인한다")
    return steps


def _extras_human(extras: dict) -> list[str]:
    steps = []
    fork = extras.get("fork") or {}
    for name, e in fork.get("entries", {}).items():
        adapter = adapters.get(name)
        if e["action"] == "manual":
            steps.append(f"{adapter.title} fork: 네이티브 CLI를 찾지 못했다 — `{fork['file']}`에 "
                         f"`{name}` CLI의 절대 경로를 적는다(시작 안내서 '백그라운드 fork 검토')")
        elif e["action"] in ("add", "replace"):
            steps.append(f"{adapter.title} fork: `{e['path']} {adapter.login}`로 구독 로그인하고 "
                         f"`osk.cli fork doctor --harness {name}`로 확인한다")
    s = extras.get("schedule") or {}
    if s.get("command", {}).get("harness"):
        adapter = adapters.get(s["command"]["harness"])
        steps.append(f"정기 실행은 {adapter.title}의 구독 로그인으로 돈다 — "
                     f"`{s['command']['argv'][0]} {adapter.login}`")
    if s.get("task", {}).get("action") in ("add", "replace"):
        steps.append("정기 실행을 한 번 직접 돌려 결과를 확인한다: `" + core.shell_join(
            [str(_python()), str(services.script("growth")), "--command-file", s["command_file"],
             "--limit", "1"]) + "`")
    y = extras.get("sync") or {}
    if y.get("task", {}).get("action") in ("add", "replace"):
        steps.append("origin의 push 대상(" + ", ".join(y.get("push") or [y["origin"]])
                     + ")이 모두 비공개 저장소인지 확인한다 — 데몬은 vault 전체를 커밋해 그곳에 올린다")
    if any((extras.get(k) or {}).get("task", {}).get("backend") == "systemd"
           and extras[k]["task"].get("action") in ("add", "replace") for k in ("schedule", "sync")):
        steps.append("로그아웃한 뒤에도 돌게 하려면 `loginctl enable-linger $USER`를 실행한다")
    return steps


def _extras_changes(extras: dict) -> bool:
    fork = extras.get("fork") or {}
    return (any(e["action"] in CHANGES for e in fork.get("entries", {}).values())
            or any((extras.get(k) or {}).get("task", {}).get("action") in CHANGES
                   for k in ("schedule", "sync"))
            or (extras.get("schedule") or {}).get("command", {}).get("action") == "add")


def _extras_errors(extras: dict) -> list[str]:
    names = {"fork": "fork", "schedule": "정기 실행", "sync": "동기화 데몬"}
    return [f"{names[k]}: {e}" for k, part in extras.items()
            for e in (part.get("error"), part.get("task", {}).get("error")) if e]


def plan(only: list[str] | None = None, uninstall: bool = False, *, fork: bool = False,
         schedule: str | None = None, at: str = AT, sync: bool = False) -> dict:
    """계획 — 호스트의 MCP·훅과 기준선, 고른 기능(fork·정기 실행·동기화). 해제에서 기능을
    고르면 그 기능만 걷는다. 아무것도 고르지 않으면 이 vault의 osk 등록을 다 걷는다 —
    `--harness`로 호스트를 고른 해제는 그 호스트의 것(fork 설정 포함)만이다."""
    features = fork or schedule is not None or sync
    everything = uninstall and not features
    items = [{"harness": a.name, "title": a.title, "mcp": _mcp(a, uninstall),
              "hooks": _hooks(a, uninstall)} for a in (hosts(only) if everything or not uninstall else [])]
    baseline = _baseline(uninstall)
    extras: dict = {}
    if fork or everything:
        pool = (hosts(only) if not uninstall else
                [adapters.get(n) for n in dict.fromkeys(only)] if only else list(adapters.ADAPTERS))
        extras["fork"] = _fork(pool, uninstall)
    system = not only and everything
    manager = services.backend() if schedule is not None or sync or system else None
    if schedule is not None or system:
        extras["schedule"] = _schedule(manager, schedule or None, at, uninstall)
    if sync or system:
        extras["sync"] = _sync(manager, uninstall)
    changes = (bool(baseline and baseline.get("state") == "record")
               or any(i["hooks"].get("changed") or i["mcp"].get("run") for i in items)
               or _extras_changes(extras))
    errors = [f"{i['title']} {part}: {i[part]['error']}" for i in items for part in ("mcp", "hooks")
              if i[part].get("error")] + _extras_errors(extras)
    if baseline and baseline.get("state") == "error":
        errors.append(f"기준선: {baseline['error']}")
    # 확인 대상은 osk 조치다 — 설정 파일의 다른 내용은 쓰기 직전에 최신으로 다시 읽으므로
    # (`_write_hooks`) 그것이 바뀌었다고 다시 확인받을 일은 없다.
    identity = {"root": str(core.ROOT), "python": str(_python()), "uninstall": uninstall,
                "items": _shown(items), "baseline": baseline, **extras}
    return {"ok": not errors, "root": str(core.ROOT), "python": str(_python()),
            "uninstall": uninstall, "baseline": baseline, "hosts": items, **extras,
            "human": _human(items, baseline, uninstall, extras), "changes": changes,
            **({"errors": errors} if errors else {}),
            "review_id": core.sha256_bytes(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                      separators=(",", ":")).encode("utf-8"))}


def _shown(items: list[dict]) -> list[dict]:
    """계획 항목에서 새 파일 내용(`content`)을 뺀 것 — 보고와 확인의 대상이다."""
    return [{**i, "hooks": {k: v for k, v in i["hooks"].items() if k != "content"}} for i in items]


def report(p: dict) -> dict:
    """사람과 에이전트에게 보이는 계획 — 새 파일 내용은 싣지 않는다."""
    return {**p, "hosts": _shown(p["hosts"])}


def _backup(path: Path, stamp: str) -> str | None:
    if not path.is_file():
        return None
    dest = path.with_name(f"{path.name}.osk-backup-{stamp}")
    shutil.copy2(path, dest)
    return str(dest)


def _record_baseline(b: dict) -> dict:
    """확인한 기준선 계획을 적용한다. 사용자가 확인한 이 setup 계획이 그 갱신 계획의
    `review_id`를 담았으므로, 그것을 갱신의 확인표에 적고 적용을 한 번만 부른다. 갱신은
    잠금 안에서 계획을 다시 세워 그 `review_id`일 때만 적용한다. 그 사이 계획이
    바뀌었으면 적용하지 않고, 사용자에게 보이지 않은 새 계획의 확인표도 남기지 않는다."""
    version = b["version"]
    try:
        update.confirm(b["review_id"])
        rep = update.run(ref=version, apply=True)
    except update.UpdateError as e:
        update.withdraw()
        return {"ok": False, "error": str(e)}
    if rep.get("approval_required"):
        update.withdraw()
        return {"ok": False, "error": "기준선 계획이 확인 뒤 바뀌었다 — 적용하지 않았다. "
                                      "setup을 다시 계획해 확인받는다"}
    return {"ok": bool(rep.get("ok")), "current": update.current_version(),
            "governance_protected": rep.get("governance_protected")}


def _write_hooks(adapter, approved: dict, uninstall: bool, stamp: str) -> dict:
    """훅 설정을 쓴다 — 계획 때의 사본이 아니라 쓰기 직전의 최신 파일에 osk 조치만
    병합한다. 확인을 기다리거나 앞 단계를 적용하는 사이 다른 도구나 사용자가 고친 것
    (권한 제한·다른 훅)을 지우지 않기 위해서다. 확인한 osk 조치(`events`)가 그 사이
    달라졌으면 쓰지 않는다. 읽는 동안 파일이 바뀌면 다시 읽는다."""
    path = Path(approved["file"])
    step = {"step": f"{adapter.name} hooks", "file": str(path)}
    for _attempt in range(3):
        before = path.read_bytes() if path.is_file() else None
        fresh = _hooks(adapter, uninstall)
        if fresh.get("error"):
            return {**step, "ok": False, "error": fresh["error"]}
        if fresh["events"] != approved["events"]:
            return {**step, "ok": False, "error": "확인 뒤 osk 훅 항목이 바뀌었다 — 쓰지 않았다. "
                                                  "setup을 다시 계획해 확인받는다"}
        if not fresh["changed"]:
            return {**step, "ok": True, "events": fresh["events"]}
        if (path.read_bytes() if path.is_file() else None) != before:
            continue
        backup = _backup(path, stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        core.atomic_write(path, (json.dumps(fresh["content"], ensure_ascii=False, indent=2)
                                 + "\n").encode("utf-8"))
        return {**step, "ok": True, "events": fresh["events"], **({"backup": backup} if backup else {})}
    return {**step, "ok": False, "error": "쓰는 동안 설정 파일이 계속 바뀌었다 — 쓰지 않았다"}


def _apply(p: dict) -> dict:
    stamp = datetime.now(core.KST).strftime("%Y%m%d-%H%M%S")
    done, backups, ok = [], [], True
    if p["baseline"] and p["baseline"].get("state") == "record":
        result = _record_baseline(p["baseline"])
        done.append({"step": "baseline", **result})
        ok &= result["ok"]
    for item in p["hosts"]:
        adapter, mcp, hooks = adapters.get(item["harness"]), item["mcp"], item["hooks"]
        if mcp.get("run"):
            # 확인한 명령이 지금도 같은 명령인지 다시 본다 — 그 사이 다른 등록이 생겼으면
            # 확인한 적 없는 등록을 바꾸게 된다.
            now = _mcp(adapter, p["uninstall"])
            if (now.get("action"), now.get("run")) != (mcp.get("action"), mcp.get("run")):
                done.append({"step": f"{adapter.name} mcp", "ok": False,
                             "error": "확인 뒤 MCP 등록이 바뀌었다 — 실행하지 않았다. "
                                      "setup을 다시 계획해 확인받는다"})
                ok = False
                mcp = {}
        if mcp.get("run"):
            backups += [b for f in mcp["files"] for b in [_backup(Path(f), stamp)] if b]
            cli = shutil.which(adapter.cli)
            for argv in mcp["run"]:
                try:
                    r = subprocess.run([cli or argv[0], *argv[1:]], capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=120,
                                       stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
                    code, output = r.returncode, (r.stdout + r.stderr).strip()[-400:]
                except (OSError, subprocess.SubprocessError) as e:
                    code, output = None, f"{type(e).__name__}: {e}"
                # 옛 등록을 걷는 명령은 그 등록이 다른 범위에 있으면 실패할 수 있다 — 뒤의 등록이 판정한다.
                removing = argv == adapter.mcp_remove_argv() and mcp["action"] == "replace"
                done.append({"step": f"{adapter.name} mcp", "command": core.shell_join(argv),
                             "ok": code == 0 or removing, "returncode": code, "output": output})
                ok &= code == 0 or removing
        if hooks.get("changed"):
            try:
                result = _write_hooks(adapter, hooks, p["uninstall"], stamp)
            except OSError as e:
                result = {"step": f"{adapter.name} hooks", "file": hooks["file"], "ok": False,
                          "error": f"{type(e).__name__}: {e}"}
            backups += [result.pop("backup")] if result.get("backup") else []
            done.append(result)
            ok &= result["ok"]
    for result in _apply_extras(p, stamp):
        backups += result.pop("backups", [])
        done.append(result)
        ok &= result["ok"]
    return {"ok": ok, "applied": True, "uninstall": p["uninstall"], "steps": done,
            "backups": backups, "human": p["human"]}


def _write_fork(approved: dict, uninstall: bool, stamp: str) -> dict:
    """fork 설정을 쓴다 — 확인한 호스트의 항목만 더하거나 걷고, 다른 키는 그대로 둔다."""
    path = Path(approved["file"])
    step = {"step": "fork CLI", "file": str(path)}
    now = _fork([adapters.get(n) for n in approved["entries"]], uninstall)
    if ({n: e["action"] for n, e in now.get("entries", {}).items()}
            != {n: e["action"] for n, e in approved["entries"].items()}):
        return {**step, "ok": False, "error": "확인 뒤 fork 설정이 바뀌었다 — 쓰지 않았다. "
                                              "setup을 다시 계획해 확인받는다"}
    data = _read_json(path)
    for name, e in approved["entries"].items():
        if e["action"] in ("add", "replace"):
            data[name] = e["path"]
        elif e["action"] == "remove":
            data.pop(name, None)
    backup = _backup(path, stamp)
    if data:
        path.parent.mkdir(parents=True, exist_ok=True)
        core.atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    else:
        path.unlink(missing_ok=True)
    return {**step, "ok": True, **({"backups": [backup]} if backup else {})}


def _write_command(approved: dict) -> dict:
    """정기 실행의 명령 파일을 만든다 — 그 사이 생겼으면 덮지 않는다."""
    path = Path(approved["command_file"])
    step = {"step": "growth command", "file": str(path)}
    if path.exists():
        return {**step, "ok": False, "error": "확인 뒤 명령 파일이 생겼다 — 덮지 않았다. "
                                              "setup을 다시 계획해 확인받는다"}
    path.parent.mkdir(parents=True, exist_ok=True)
    core.atomic_write(path, (json.dumps(approved["command"]["argv"], ensure_ascii=False, indent=2)
                             + "\n").encode("utf-8"))
    return {**step, "ok": True}


def _stop_daemon() -> list[int]:
    """이 vault의 데몬을 멈춘다 — 작업 스케줄러의 작업은 데몬을 띄우고 끝나므로 작업을 지워도
    데몬은 남는다. 갱신과 같은 규율로 작업 트리 변경 잠금을 먼저 잡는다."""
    from ._portalock import unlock
    pids = update._daemon_pids()
    if pids:
        with open(update._sync_lock_path(), "w") as lock:
            update._lock_within(lock, 120, "작업 트리 변경 잠금이 2분 넘게 풀리지 않는다 — "
                                           "잠시 후 다시 실행한다")
            try:
                update._kill(pids)
            finally:
                unlock(lock)
    return pids


def _apply_extras(p: dict, stamp: str) -> list[dict]:
    """고른 기능을 적용한다 — fork 설정, 정기 실행(명령 파일 → 작업), 동기화 데몬."""
    out, uninstall = [], p["uninstall"]
    fork = p.get("fork") or {}
    if any(e["action"] in CHANGES for e in fork.get("entries", {}).values()):
        try:
            out.append(_write_fork(fork, uninstall, stamp))
        except (OSError, ValueError, SetupError) as e:
            out.append({"step": "fork CLI", "ok": False, "error": f"{type(e).__name__}: {e}"})
    manager = services.backend() if "schedule" in p or "sync" in p else None
    for kind, key in (("growth", "schedule"), ("sync", "sync")):
        part = p.get(key)
        if not part:
            continue
        if kind == "growth" and part.get("command", {}).get("action") == "add":
            out.append(_write_command(part))
            if not out[-1]["ok"]:
                continue
        if part.get("task", {}).get("action") not in CHANGES:
            continue
        job = None if uninstall else (services.job(kind, command_file=Path(part["command_file"]),
                                                   at=part["at"]) if kind == "growth" else services.job(kind))
        try:
            result = services.apply(manager, kind, job, part["task"], uninstall, stamp)
            if kind == "sync" and uninstall and result["ok"] and os.name == "nt":
                result["stopped"] = _stop_daemon()
        except (OSError, ValueError, subprocess.SubprocessError, update.UpdateError) as e:
            result = {"step": f"{kind} ({manager.name})", "ok": False, "error": f"{type(e).__name__}: {e}"}
        out.append(result)
    return out


def run(*, apply: bool = False, uninstall: bool = False, only: list[str] | None = None,
        fork: bool = False, schedule: str | None = None, at: str = AT,
        sync: bool = False) -> tuple[dict, int]:
    """(보고, 종료코드). 계획은 0, 확인 요청은 2, 적용 실패는 1."""
    p = plan(only, uninstall, fork=fork, schedule=schedule, at=at, sync=sync)
    if not apply:
        return report(p), 0
    if not p["ok"]:
        return {**report(p), "applied": False}, 1
    if not p["changes"]:
        return {**report(p), "applied": False, "note": "할 일이 없다 — 이미 이어져 있다"}, 0
    ticket = core.local_lock_path(TICKET)
    try:
        pending = json.loads(ticket.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pending = {}
    if (not isinstance(pending, dict) or pending.get("review_id") != p["review_id"]
            or not isinstance(pending.get("at"), (int, float))
            or not 0 <= time.time() - pending["at"] < CONFIRM_WITHIN):
        core.atomic_write(ticket, json.dumps({"review_id": p["review_id"], "at": time.time()}).encode())
        return {**report(p), "ok": False, "approval_required": True, "instruction": INSTRUCTION}, 2
    ticket.unlink(missing_ok=True)   # 한 번만 — 실패한 적용도 새 확인을 요구한다
    result = _apply(p)
    return result, 0 if result["ok"] else 1


def text(rep: dict) -> str:
    """단말에 보이는 요약."""
    lines = []
    b = rep.get("baseline")
    if b:
        lines.append({"recorded": f"기준선: 기록돼 있다({b.get('current')})",
                      "record": f"기준선: {b.get('version')}을 적는다 — 관리 파일 {b.get('files')}개, "
                                f"통치 구획 보호 {b.get('governance')}",
                      "error": f"기준선: 계획하지 못했다 — {b.get('error')}"}[b["state"]])
    for h in rep.get("hosts", []):
        mcp, hooks = h["mcp"], h["hooks"]
        lines.append(f"{h['title']}: MCP {mcp.get('action')}"
                     + (f" ({mcp['error']})" if mcp.get("error") else ""))
        if hooks.get("file"):
            lines.append(f"  훅 {hooks['file']}: " + ", ".join(f"{k} {v}" for k, v in hooks["events"].items())
                         + (f" ({hooks['error']})" if hooks.get("error") else ""))
        lines += [f"  참고: {n}" for n in hooks.get("notes", [])]
    fork = rep.get("fork")
    if fork:
        lines.append(f"fork CLI {fork['file']}: " + (", ".join(
            f"{n} {e['action']}" + (f" ({e['path']})" if e.get("path") else "")
            for n, e in fork.get("entries", {}).items()) or "대상 없음"))
    for key, title in (("schedule", "정기 실행"), ("sync", "동기화 데몬")):
        part = rep.get(key)
        if not part:
            continue
        task = part.get("task", {})
        lines.append(f"{title}: {task.get('action', '-')}" + (f" {task['id']}" if task.get("id") else "")
                     + (f" — {task['command']}" if task.get("command") else ""))
        if part.get("command"):
            lines.append(f"  명령 파일 {part['command_file']}: {part['command']['action']} — "
                         + core.shell_join(part["command"]["argv"]))
        if part.get("origin"):
            lines.append(f"  origin: {part['origin']} — push: {', '.join(part.get('push') or [])}")
        lines += [f"  걷는다: {i}" for i in task.get("remove", [])]
    for part in (rep.get("fork"), rep.get("schedule"), rep.get("sync")):
        for n in (part or {}).get("notes", []) + (part or {}).get("task", {}).get("notes", []):
            lines.append(f"참고: {n}")
    for step in rep.get("steps", []):
        lines.append(f"{'완료' if step['ok'] else '실패'}: {step['step']}"
                     + (f" — {step.get('error') or step.get('output')}" if not step["ok"] else ""))
    lines += [f"백업: {b}" for b in rep.get("backups", [])]
    lines += [f"할 일: {s}" for s in rep.get("human", [])]
    lines += [f"오류: {e}" for e in rep.get("errors", [])]
    return "\n".join(lines)


def _interactive(uninstall: bool, only: list[str] | None, choices: dict) -> int:
    """마법사 — 고를 기능을 묻고, 계획을 보여 주고, 단말에서 확인받아 적용한다(선택 경로)."""
    if not sys.stdin.isatty():
        sys.exit("중단 — --interactive는 대화형 단말에서 쓴다. 에이전트는 --apply 두 단계를 쓴다")
    if not uninstall and not (choices["fork"] or choices["schedule"] is not None or choices["sync"]):
        yes = lambda q: input(q).strip().lower() == "y"
        choices["fork"] = yes("백그라운드 fork 검토를 켤까요? 대화의 답변 9회마다 구독으로 검토를 "
                              "돌립니다 [y/N] ")
        pick = input("매일 한 번 도는 정기 실행을 등록할까요? 쓸 하네스(claude·codex)를 적거나 "
                     "Enter로 건너뜁니다: ").strip().lower()
        choices["schedule"] = pick if pick in adapters.fork_names() else None
        choices["sync"] = yes("Git 동기화 데몬을 등록할까요? origin이 개인 저장소여야 합니다 [y/N] ")
    p = plan(only, uninstall, **choices)
    print(text(report(p)))
    if not p["ok"] or not p["changes"]:
        print("할 일이 없다." if p["ok"] else "계획에 오류가 있어 적용하지 않는다.")
        return 0 if p["ok"] else 1
    if input("이 계획을 적용합니까? [y/N] ").strip().lower() != "y":
        print("중단")
        return 1
    result = _apply(p)
    print(text(result))
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="setup", description="osk를 이 기기의 하네스에 잇는다")
    ap.add_argument("--apply", action="store_true",
                    help="계획을 적용한다 — 첫 호출은 확인을 요청하고 멈춘다")
    ap.add_argument("--uninstall", action="store_true", help="이 vault의 osk 등록만 걷어 낸다")
    ap.add_argument("--harness", action="append", choices=adapters.NAMES,
                    help="이 호스트만 잇는다(여러 번 쓸 수 있다)")
    ap.add_argument("--interactive", action="store_true",
                    help="계획을 보여 주고 단말에서 확인받아 적용한다")
    ap.add_argument("--fork", action="store_true",
                    help="백그라운드 fork 검토가 부를 CLI를 적는다(.osk/response-growth.json)")
    ap.add_argument("--schedule", nargs="?", const="", metavar="HARNESS",
                    help="매일 도는 정기 실행을 등록한다 — 명령 파일이 없으면 그 하네스로 만든다")
    ap.add_argument("--at", default=AT, help=f"정기 실행 시각(24시간제 HH:MM, 기본 {AT})")
    ap.add_argument("--sync", action="store_true",
                    help="Git 동기화 데몬을 상시 서비스로 등록한다(origin이 개인 저장소여야 한다)")
    a = ap.parse_args(argv)
    if a.schedule and a.schedule not in adapters.fork_names():
        ap.error(f"--schedule은 {', '.join(adapters.fork_names())} 중 하나다")
    choices = {"fork": a.fork, "schedule": a.schedule, "at": a.at, "sync": a.sync}
    if a.interactive:
        if a.apply:
            ap.error("--interactive와 --apply는 함께 쓰지 않는다")
        return _interactive(a.uninstall, a.harness, choices)
    out, code = run(apply=a.apply, uninstall=a.uninstall, only=a.harness, **choices)
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return code


if __name__ == "__main__":
    sys.exit(main())
