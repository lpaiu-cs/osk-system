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
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import core, update
from . import harness as adapters
from .doctor import _engine_dir, _python
from .harness import base

TICKET = "osk-setup-confirmation.json"
CONFIRM_WITHIN = 3600
CHANGES = ("add", "replace", "remove")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
INSTRUCTION = ("여기서 멈추고 사용자에게 이 계획을 설명한 뒤 명시적 확인을 받는다 — 기준선과 "
               "통치 구획 보호, 호스트별 MCP·훅 변경, 백업 자리, 사용자가 할 일(human). 확인 "
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
    명령(`manual`)."""
    server, python = _server(), _python()
    try:
        servers, _hooks, errors = adapter.registrations()
    except (OSError, ValueError) as e:
        return {"action": "error", "error": f"{type(e).__name__}: {e}"}
    mcp_errors = [e for e in errors if any(str(f) in e for f in adapter.mcp_files())]
    if mcp_errors:
        return {"action": "error", "error": "; ".join(mcp_errors)}
    named = [s for s in servers if s["name"] == base.MCP_NAME]
    mine = [s for s in named if base.mentions(s["tokens"], server)]
    want = [base.fold(python.as_posix())]
    same = [s for s in mine if [base.fold(t) for t in base.before(s["tokens"], server)] == want]
    out: dict = {}
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
        out["files"] = [str(f) for f in adapter.mcp_files() if f.is_file()]
        if shutil.which(adapter.cli):
            out["run"] = runs
        else:
            out["manual"] = " ; ".join(core.shell_join(argv) for argv in runs)
    return out


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


def _human(items: list[dict], baseline: dict | None, uninstall: bool) -> list[str]:
    steps = []
    for item in items:
        adapter, mcp, hooks = adapters.get(item["harness"]), item["mcp"], item["hooks"]
        if mcp.get("manual"):
            steps.append(f"{adapter.title}: `{mcp['manual']}`를 실행한다 — `{adapter.cli}` CLI가 PATH에 없다")
        if hooks.get("changed") and adapter.trust and not uninstall:
            steps.append(adapter.trust)
        if hooks.get("changed") or mcp.get("action") in CHANGES:
            steps.append(f"{adapter.title}의 세션을 새로 연다 — 훅과 MCP는 세션을 시작할 때 읽힌다")
    if not items:
        steps.append("이 기기에서 Claude Code·Codex의 흔적(설정 폴더·PATH의 CLI)을 찾지 못했다 — "
                     "설치한 뒤 다시 실행하거나 --harness로 고른다")
    if baseline and baseline.get("state") == "record":
        steps.append("기준선 기록(`00_Scope/Workbench/_ledger/update.jsonl`)을 커밋한다")
    if not uninstall and items:
        steps.append("새 세션을 연 뒤 `setup.py doctor`로 연결을 확인한다")
    return steps


def plan(only: list[str] | None = None, uninstall: bool = False) -> dict:
    items = [{"harness": a.name, "title": a.title, "mcp": _mcp(a, uninstall),
              "hooks": _hooks(a, uninstall)} for a in hosts(only)]
    baseline = _baseline(uninstall)
    changes = (bool(baseline and baseline.get("state") == "record")
               or any(i["hooks"].get("changed") or i["mcp"].get("run") for i in items))
    errors = [f"{i['title']} {part}: {i[part]['error']}" for i in items for part in ("mcp", "hooks")
              if i[part].get("error")]
    if baseline and baseline.get("state") == "error":
        errors.append(f"기준선: {baseline['error']}")
    identity = {"root": str(core.ROOT), "python": str(_python()), "uninstall": uninstall,
                "items": items, "baseline": baseline}
    return {"ok": not errors, "root": str(core.ROOT), "python": str(_python()),
            "uninstall": uninstall, "baseline": baseline, "hosts": items,
            "human": _human(items, baseline, uninstall), "changes": changes,
            **({"errors": errors} if errors else {}),
            "review_id": core.sha256_bytes(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                      separators=(",", ":")).encode("utf-8"))}


def report(p: dict) -> dict:
    """사람과 에이전트에게 보이는 계획 — 새 파일 내용은 싣지 않는다."""
    hosts_ = [{**i, "hooks": {k: v for k, v in i["hooks"].items() if k != "content"}} for i in p["hosts"]]
    return {**p, "hosts": hosts_}


def _backup(path: Path, stamp: str) -> str | None:
    if not path.is_file():
        return None
    dest = path.with_name(f"{path.name}.osk-backup-{stamp}")
    shutil.copy2(path, dest)
    return str(dest)


def _record_baseline(b: dict) -> dict:
    """확인한 기준선 계획을 적용한다 — 갱신의 확인 관문을 같은 계획으로 두 번 지난다.
    확인은 이 setup 계획의 확인이 대신한다: 그 계획이 이 `review_id`를 담았다."""
    version = b["version"]
    try:
        if update.run(ref=version)["review_id"] != b["review_id"]:
            return {"ok": False, "error": "기준선 계획이 확인 뒤 바뀌었다 — setup을 다시 계획한다"}
        first = update.run(ref=version, apply=True)
        rep = update.run(ref=version, apply=True) if first.get("approval_required") else first
    except update.UpdateError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": bool(rep.get("ok")), "current": update.current_version(),
            "governance_protected": rep.get("governance_protected")}


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
            path = Path(hooks["file"])
            try:
                backup = _backup(path, stamp)
                backups += [backup] if backup else []
                path.parent.mkdir(parents=True, exist_ok=True)
                core.atomic_write(path, (json.dumps(hooks["content"], ensure_ascii=False, indent=2)
                                         + "\n").encode("utf-8"))
                done.append({"step": f"{adapter.name} hooks", "file": str(path), "ok": True,
                             "events": hooks["events"]})
            except OSError as e:
                done.append({"step": f"{adapter.name} hooks", "file": str(path), "ok": False,
                             "error": f"{type(e).__name__}: {e}"})
                ok = False
    return {"ok": ok, "applied": True, "uninstall": p["uninstall"], "steps": done,
            "backups": backups, "human": p["human"]}


def run(*, apply: bool = False, uninstall: bool = False, only: list[str] | None = None) -> tuple[dict, int]:
    """(보고, 종료코드). 계획은 0, 확인 요청은 2, 적용 실패는 1."""
    p = plan(only, uninstall)
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
    for step in rep.get("steps", []):
        lines.append(f"{'완료' if step['ok'] else '실패'}: {step['step']}"
                     + (f" — {step.get('error') or step.get('output')}" if not step["ok"] else ""))
    lines += [f"백업: {b}" for b in rep.get("backups", [])]
    lines += [f"할 일: {s}" for s in rep.get("human", [])]
    lines += [f"오류: {e}" for e in rep.get("errors", [])]
    return "\n".join(lines)


def _interactive(uninstall: bool, only: list[str] | None) -> int:
    """마법사 — 계획을 보여 주고 단말에서 확인받아 적용한다(선택 경로)."""
    if not sys.stdin.isatty():
        sys.exit("중단 — --interactive는 대화형 단말에서 쓴다. 에이전트는 --apply 두 단계를 쓴다")
    p = plan(only, uninstall)
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
    a = ap.parse_args(argv)
    if a.interactive:
        if a.apply:
            ap.error("--interactive와 --apply는 함께 쓰지 않는다")
        return _interactive(a.uninstall, a.harness)
    out, code = run(apply=a.apply, uninstall=a.uninstall, only=a.harness)
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return code


if __name__ == "__main__":
    sys.exit(main())
