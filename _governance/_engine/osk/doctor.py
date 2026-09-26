"""osk.doctor — 이 기기에서 하네스가 osk에 이어졌는지 읽기만 하며 점검한다.

호스트마다 따로 본다: MCP·훅이 등록됐는가(설정 파일), 훅이 실제로 불렸는가(이
기기의 실행 기록, `osk.harness.runs`), 훅 문맥이 모델에 닿았는가(세션 시작 뒤
overview), 판본이 확인한 범위인가, 구독 fork가 준비됐는가. 등록 파일만으로 실행을
판정하지 않는다 — 신뢰하지 않은 Codex 훅이나 새로 열지 않은 Claude Code 세션은 설정
파일에 있어도 돌지 않는다.

상태와 설정을 쓰지 않는다. 호스트 CLI에는 `--version`만 묻고, fork 판정은
`fork doctor`와 같은 `response_growth.check`다(인증 상태 조회는 하되 추론은 없다).
MCP 등록의 해석기는 한 번 띄워 판본과 서버가 import하는 패키지만 본다 — 이 명령을
돌리는 Python이 아니라 등록된 Python이 서버를 띄우기 때문이다.
실패(`fail`)는 설정이 osk를 띄우지 못하는 경우뿐이다 — 등록하지 않은 기능은 경고다.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import core
from . import harness as adapters
from .harness import base, runs

LEVELS = ("ok", "info", "warn", "fail")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _engine_dir() -> Path:
    """등록 명령이 가리켜야 할 엔진 — 이 vault 안의 사본이다. 훅과 MCP 서버는 자기 파일
    자리로 vault를 찾으므로, 다른 자리의 엔진을 부르는 등록은 이 vault가 아니다."""
    return core.ROOT / "_governance" / "_engine"


def _item(level: str, check: str, detail: str, fix: str | None = None) -> dict:
    return {"level": level, "check": check, "detail": detail, **({"fix": fix} if fix else {})}


def _kst(at) -> str:
    try:
        return datetime.fromtimestamp(at, core.KST).strftime(core.TS_FMT)
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _python() -> Path:
    """이 vault의 가상환경 Python — 등록 명령이 써야 하는 자리."""
    return core.ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _runnable(program: str) -> bool:
    if not program:
        return False
    return Path(program).is_file() if Path(program).is_absolute() else shutil.which(program) is not None


def _pip(prefix) -> str:
    """그 해석기에 엔진 의존성을 까는 한 줄."""
    engine = _engine_dir()
    return core.shell_join([*prefix, "-m", "pip", "install", "-r", engine / "requirements.txt",
                            "-c", engine / "constraints.txt"])


# 등록된 해석기가 서버를 띄울 수 있는가 — 판본, 그리고 `mcp_server.py`가 기동에 import하는 것.
_PROBE = ("import sys\n"
          "if sys.version_info < (3, 11):\n"
          "    print('Python %d.%d.%d' % sys.version_info[:3]); sys.exit(3)\n"
          "import zoneinfo, mcp.server.fastmcp, pydantic, yaml, rank_bm25\n"
          "zoneinfo.ZoneInfo('Asia/Seoul')\n")
_PROBED: dict[tuple, tuple[str, str] | None] = {}


def _probe(prefix) -> tuple[str, str] | None:
    """등록 명령의 해석기 부분으로 `_PROBE`를 돌린다. 되면 None, 안 되면 (종류, 사유) —
    종류는 `version`(3.11 미만)이나 `import`다. 같은 해석기는 한 번만 띄운다."""
    key = tuple(prefix)
    if key not in _PROBED:
        try:
            r = subprocess.run([*key, "-c", _PROBE], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW)
            last = ((r.stderr or r.stdout).strip().splitlines() or [f"종료 코드 {r.returncode}"])[-1]
            _PROBED[key] = (None if r.returncode == 0 else
                            ("version", r.stdout.strip()) if r.returncode == 3 else ("import", last))
        except (OSError, subprocess.TimeoutExpired) as exc:
            _PROBED[key] = ("import", f"{type(exc).__name__}: {exc}")
    return _PROBED[key]


def _engine(records: dict) -> list[dict]:
    items = []
    v = sys.version_info
    ok = v >= (3, 11)
    items.append(_item("ok" if ok else "fail", "Python", f"{v.major}.{v.minor}.{v.micro} — {sys.executable}",
                       None if ok else "엔진은 Python 3.11 이상이 필요하다"))
    if base.fold(str(Path(sys.prefix).resolve())) != base.fold(str((core.ROOT / ".venv").resolve())):
        items.append(_item("info", "가상환경", f"이 명령은 이 vault의 `.venv`가 아닌 {sys.prefix}에서 돈다",
                           f"훅·MCP 등록 명령은 {_python()}를 쓴다"))
    if importlib.util.find_spec("mcp") is None:
        items.append(_item("fail", "mcp 패키지", "이 Python에 `mcp`가 없다 — MCP 서버가 뜨지 않는다",
                           _pip([sys.executable])))
    else:
        items.append(_item("ok", "mcp 패키지", "설치됨"))
    try:
        from . import update_check
        rep = update_check.report()
        if rep.get("available"):
            items.append(_item("info", "릴리스", f"새 릴리스 {rep['latest']} (이 vault {rep['current']})",
                               '에이전트에게 "osk 업데이트해 줘"라고 요청한다'))
        else:
            detail = f"이 vault {rep.get('current') or '판본 미상'}"
            detail += f" · 마지막 확인 {rep['checked_at']}" if rep.get("checked_at") else ""
            detail += f" · 자동 확인 안 함: {rep['auto_off']}" if rep.get("auto_off") else ""
            items.append(_item("ok" if rep.get("current") else "info", "릴리스", detail))
    except Exception as exc:
        items.append(_item("info", "릴리스", f"판독 실패 — {type(exc).__name__}: {exc}"))
    for key, run in sorted(records["runs"].items()):
        if key.startswith("unknown/") and isinstance(run, dict):
            items.append(_item("info", "알 수 없는 호스트", f"훅 {key.split('/', 1)[1]}이 {_kst(run.get('at'))}에 "
                               "호스트를 알아보지 못한 채 불렸다 — 훅 입력을 읽지 못했거나 osk가 모르는 호스트다"))
    return items


def _mcp(adapter, servers: list[dict]) -> list[dict]:
    server = _engine_dir() / "mcp_server.py"
    fix = adapter.mcp_command(_python(), server) or None
    mine = [s for s in servers if base.mentions(s["tokens"], server)]
    if mine:
        out = []
        for s in mine:
            where, prefix = f"{s['name']} ({s['file']})", base.before(s["tokens"], server)
            if not _runnable(s["tokens"][0]):
                out.append(_item("fail", "MCP", f"{s['name']}의 Python이 없다: {s['tokens'][0]} ({s['file']})",
                                 fix))
            elif prefix and (bad := _probe(prefix)):
                kind, why = bad
                out.append(_item("fail", "MCP", f"{where} · 등록된 Python으로 서버가 뜨지 않는다 — {why}",
                                 fix if kind == "version" else _pip(prefix)))
            else:
                out.append(_item("ok", "MCP", where))
        return out
    other = next(((s, base.refers(s["tokens"], server.name)) for s in servers
                  if base.refers(s["tokens"], server.name)), None)
    if other:
        return [_item("warn", "MCP", f"{other[0]['name']}이 다른 vault를 가리킨다: {other[1]} "
                      f"({other[0]['file']})", fix)]
    files = ", ".join(str(f) for f in adapter.mcp_files())
    return [_item("warn", "MCP", f"이 vault의 MCP 서버가 등록되지 않았다({files})", fix)]


def _hook(adapter, event: str, hooks: list[dict], run: dict | None) -> dict:
    name = adapter.events[event]
    script = _engine_dir() / "scripts" / "hooks" / adapters.SCRIPTS[event]
    files = ", ".join(str(f) for f in adapter.hook_files())
    fix = (f"안내서(docs/GETTING-STARTED.md) {adapter.guide}단계대로 hooks.{name}에 "
           f"`{base.hook_line([_python(), script])}`를 등록한다")
    ran = f"마지막 실행 {_kst(run.get('at'))}" if run else ""
    same = [h for h in hooks if h["event"] == name]
    mine = [h for h in same if base.mentions(h["tokens"], script)]
    if mine:
        h = mine[0]
        where = f"등록({h['file']})"
        if not _runnable(h["tokens"][0]):
            return _item("fail", f"훅 {name}", f"{where} · 명령의 Python이 없다: {h['tokens'][0]}", fix)
        # 신뢰는 지금 등록의 것이고, 실행 기록은 호스트·사건별이라 옛 등록의 실행도 남는다.
        # 그래서 신뢰부터 본다 — 지난 실행이 지금 등록의 미신뢰를 가리지 않게.
        if adapter.trusted(h["file"], name, h["group"], h["index"]) is False:
            detail = (f"{where} · 지금 등록에 신뢰 기록이 없다 — {ran}은 이 등록이 신뢰된 증거가 아니다"
                      if run else f"{where} · 신뢰 기록도, 이 기기의 실행 기록도 없다")
            return _item("warn", f"훅 {name}", detail, adapter.reload)
        if run:
            return _item("ok", f"훅 {name}", f"{where} · {ran}")
        # 실행 기록은 기록을 남기는 판의 훅이 처음 불린 때부터 쌓인다.
        return _item("warn", f"훅 {name}", f"{where} · 이 기기의 실행 기록이 아직 없다", adapter.reload)
    if run:
        return _item("info", f"훅 {name}", f"문서의 등록 자리({files})에는 없지만 이 기기에서 불렸다 · {ran}"
                     " — 프로젝트 설정·플러그인 같은 다른 자리의 등록이다")
    other = next((base.refers(h["tokens"], script.name) for h in same
                  if base.refers(h["tokens"], script.name)), None)
    if other:
        return _item("warn", f"훅 {name}", f"다른 vault의 스크립트가 등록돼 있다: {other}", fix)
    return _item("warn", f"훅 {name}", f"등록되지 않았다({files})", fix)


def _delivery(start: dict | None, overview: dict) -> dict | None:
    session = start.get("session") if isinstance(start, dict) else None
    if not session:
        return None
    at, seen = start.get("at"), overview.get(session)
    if isinstance(at, (int, float)) and isinstance(seen, (int, float)) and seen >= at:
        return _item("ok", "전달", f"세션 시작({_kst(at)}) 뒤 overview(session={session})가 불렸다 — "
                     "훅 문맥이 모델에 닿았다고 본다")
    return _item("info", "전달", f"마지막 세션 시작({_kst(at)}, session={session}) 뒤 overview 기록이 없다 "
                 "— 훅 문맥이 모델에 닿았는지 알 수 없다",
                 '새 세션에서 에이전트에게 "osk 훅이 알려 준 세션 키가 뭐야?"라고 묻는다 — '
                 "저장소 이름으로 답해야 한다")


def _version(adapter, cli: str | None) -> dict:
    version, source = None, ""
    try:
        from . import integration
        path = integration.recent_transcript(adapter.name)
        if path:
            version, source = adapter.transcript_version(path), "최근 대화"
    except (OSError, ValueError):
        pass
    if not version and cli:
        try:
            r = subprocess.run([cli, "--version"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10, stdin=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW)
            version, source = adapter.parse_version(r.stdout.strip()), "PATH CLI"
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not version:
        return _item("info", "판본", f"알 수 없다 — 포착한 대화도 PATH의 `{adapter.cli}`도 판본을 주지 "
                     f"않았다 (검증 {adapter.verified})")
    if adapters.newer(version, adapter.verified):
        return _item("warn", "판본", f"{source} {version} — 검증한 {adapter.verified}보다 새 판이다. "
                     "훅 입출력이나 전사 형식이 바뀌었을 수 있다",
                     "포착·주입이 이상하면 이 판본과 함께 알린다")
    return _item("ok", "판본", f"{source} {version} (검증 {adapter.verified})")


def _fork(adapter) -> dict:
    from . import response_growth
    try:
        if not response_growth.configured(adapter.name):
            return _item("info", "fork", "구독 fork를 설정하지 않았다 — 세션 안에서 9·15턴 검토로 동작한다",
                         "켜려면 SETUP의 '대화별 검토 훅'대로 .osk/response-growth.json에 CLI 경로를 둔다")
        verdict = response_growth.check(adapter.name, None, None, os.getcwd())
    except (OSError, ValueError) as exc:
        return _item("warn", "fork", f"설정을 판정하지 못했다 — {exc}")
    if verdict["mode"] == "background":
        return _item("ok", "fork", "구독 fork 준비됨 — 최종 답변 Stop 9회마다 백그라운드 검토")
    return _item("warn", "fork", f"세션 안 검토로 돈다 — {verdict.get('reason')}",
                 f"근거는 `fork doctor --harness {adapter.name}`로 본다")


def _host(adapter, records: dict) -> dict:
    head = {"harness": adapter.name, "title": adapter.title}
    prefix = adapter.name + "/"
    ran = {k[len(prefix):]: v for k, v in records["runs"].items()
           if k.startswith(prefix) and isinstance(v, dict)}
    cli = shutil.which(adapter.cli)
    home = adapter.home()
    if not (home.is_dir() or ran or cli):
        return {**head, "in_use": False,
                "items": [_item("info", "설치", f"이 기기에서 쓴 흔적이 없다({home}) — 건너뛴다")]}
    servers, hooks, errors = adapter.registrations()
    items = [_item("warn", "설정 판독", e) for e in errors]
    items += _mcp(adapter, servers)
    items += [_hook(adapter, event, hooks, ran.get(event)) for event in adapters.SCRIPTS]
    delivery = _delivery(ran.get("start"), records["overview"])
    if delivery:
        items.append(delivery)
    items.append(_version(adapter, cli))
    if adapter.fork:
        items.append(_fork(adapter))
    return {**head, "in_use": True, "items": items}


def report(only: str | None = None) -> dict:
    """호스트별 점검 결과. `only`는 하네스 이름 하나로 좁힌다."""
    records = runs.read()
    engine = _engine(records)
    hosts = [_host(a, records) for a in adapters.ADAPTERS if only in (None, a.name)]
    items = engine + [i for h in hosts for i in h["items"]]
    counts = {level: sum(i["level"] == level for i in items) for level in LEVELS}
    return {"ok": not counts["fail"], "root": str(core.ROOT), "counts": counts,
            "engine": engine, "hosts": hosts}


def text(rep: dict) -> str:
    lines = [f"osk doctor — {rep['root']}"]

    def section(title, items):
        lines.append(title)
        for i in items:
            lines.append(f"  {i['level']:<4}  {i['check']} — {i['detail']}")
            if i.get("fix"):
                lines.append(f"        → {i['fix']}")
    section("엔진", rep["engine"])
    for h in rep["hosts"]:
        section(h["title"], h["items"])
    c = rep["counts"]
    lines.append(f"판정: 실패 {c['fail']} · 경고 {c['warn']} · 정보 {c['info']} · 정상 {c['ok']}")
    return "\n".join(lines) + "\n"
