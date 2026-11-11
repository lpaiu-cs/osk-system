"""osk.doctor — 이 기기에서 하네스가 osk에 이어졌는지 읽기만 하며 점검한다.

호스트마다 따로 본다: MCP·훅이 등록됐는가(설정 파일), 훅이 실제로 불렸는가(이
기기의 실행 기록, `osk.harness.runs`), 훅 문맥이 모델에 닿았는가(세션 시작 뒤
overview), 판본이 확인한 범위인가, 구독 fork가 준비됐는가. 등록 파일만으로 실행을
판정하지 않는다 — 신뢰하지 않은 Codex 훅이나 새로 열지 않은 Claude Code 세션은 설정
파일에 있어도 돌지 않는다.

이 vault의 노드가 지금 체제의 표기를 따르는지도 본다(호환성·일괄 이행). 옮기는 것은
`--migrate`(`migrate`)뿐이다.

그 밖에는 상태와 설정을 쓰지 않는다. 호스트 CLI에는 `--version`만 묻고, fork 판정은
`fork doctor`와 같은 `response_growth.check`다(인증 상태 조회는 하되 추론은 없다).
MCP 등록의 해석기는 한 번 띄워 판본과 서버가 import하는 패키지만 본다 — 이 명령을
돌리는 Python이 아니라 등록된 Python이 서버를 띄우기 때문이다.
실패(`fail`)는 설정이 osk를 띄우지 못하는 경우뿐이다 — 등록하지 않은 기능은 경고다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import (core, evictions, evidence_migration as em, growth_daily, raw, rechecks, response_growth, scope_memory,
               validate, conversation_capture, conversation_state, durable, locks)
from . import harness as adapters
from .harness import base, runs
from .vault import Vault

LEVELS = ("ok", "info", "warn", "fail")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DUP_RECENT = 7 * 24 * 3600   # 중복 호출을 알리는 기간(초) — 등록을 고친 뒤에는 사라진다
_ONE = ("같은 호출에 함께 걸리는 등록을 하나로 합친다(SessionStart는 matcher의 원인이 겹치지 않게 "
        "나눠도 된다) — osk는 같은 호출을 한 번만 처리하지만 호스트는 매번 두 번 띄운다")


def engine_dir(vault: Vault) -> Path:
    """등록 명령이 가리켜야 할 엔진 — 이 vault 안의 사본이다. 훅과 MCP 서버는 자기 파일
    자리로 vault를 찾으므로, 다른 자리의 엔진을 부르는 등록은 이 vault가 아니다."""
    return vault.root / "_governance" / "_engine"


def _item(level: str, check: str, detail: str, fix: str | None = None) -> dict:
    return {"level": level, "check": check, "detail": detail, **({"fix": fix} if fix else {})}


def _kst(at) -> str:
    try:
        return datetime.fromtimestamp(at, core.KST).strftime(core.TS_FMT)
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def venv_python(vault: Vault) -> Path:
    """이 vault의 가상환경 Python — 등록 명령이 써야 하는 자리."""
    return vault.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _runnable(program: str) -> bool:
    if not program:
        return False
    return Path(program).is_file() if Path(program).is_absolute() else shutil.which(program) is not None


def _pip(vault: Vault, prefix) -> str:
    """그 해석기에 엔진 의존성을 까는 한 줄."""
    engine = engine_dir(vault)
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


def _engine(vault: Vault, records: dict) -> list[dict]:
    items = []
    v = sys.version_info
    ok = v >= (3, 11)
    items.append(_item("ok" if ok else "fail", "Python", f"{v.major}.{v.minor}.{v.micro} — {sys.executable}",
                       None if ok else "엔진은 Python 3.11 이상이 필요하다"))
    if base.fold(str(Path(sys.prefix).resolve())) != base.fold(str((vault.root / ".venv").resolve())):
        items.append(_item("info", "가상환경", f"이 명령은 이 vault의 `.venv`가 아닌 {sys.prefix}에서 돈다",
                           f"훅·MCP 등록 명령은 {venv_python(vault)}를 쓴다"))
    if importlib.util.find_spec("mcp") is None:
        items.append(_item("fail", "mcp 패키지", "이 Python에 `mcp`가 없다 — MCP 서버가 뜨지 않는다",
                           _pip(vault, [sys.executable])))
    else:
        items.append(_item("ok", "mcp 패키지", "설치됨"))
    try:
        from . import update_check
        rep = update_check.report(vault)
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
    try:
        from . import sync_status
        rec = sync_status.last(vault.root)
        if rec:
            trouble = sync_status.trouble(vault.root)
            items.append(_item("warn" if trouble else "ok", "동기화 데몬",
                               trouble or f"마지막 주기 {_kst(rec['at'])} — {rec.get('status')}",
                               "에이전트용 안내 docs/OPERATE-AGENT.md의 Check the connection 절을 따른다 — 전송 "
                               "보류는 `setup.py --apply --sync`를 두 단계로, 돌지 않는 데몬은 그 등록을 다시 "
                               "시작한다" if trouble else None))
    except Exception as exc:
        items.append(_item("info", "동기화 데몬", f"판독 실패 — {type(exc).__name__}: {exc}"))
    for key, run in sorted(records["runs"].items()):
        if key.startswith("unknown/") and isinstance(run, dict):
            items.append(_item("info", "알 수 없는 호스트", f"훅 {key.split('/', 1)[1]}이 {_kst(run.get('at'))}에 "
                               "호스트를 알아보지 못한 채 불렸다 — 훅 입력을 읽지 못했거나 osk가 모르는 호스트다"))
    return items


def _mcp(vault: Vault, adapter, servers: list[dict]) -> list[dict]:
    server = engine_dir(vault) / "mcp_server.py"
    fix = adapter.mcp_command(venv_python(vault), server) or None
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
                                 fix if kind == "version" else _pip(vault, prefix)))
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


def _hook(vault: Vault, adapter, event: str, hooks: list[dict], run: dict | None) -> dict:
    name = adapter.events[event]
    script = engine_dir(vault) / "scripts" / "hooks" / adapters.SCRIPTS[event]
    files = ", ".join(str(f) for f in adapter.hook_files())
    try:
        fix = (f"안내서(docs/GETTING-STARTED.md) {adapter.guide}단계대로 hooks.{name}에 "
               f"`{adapter.hook_command([venv_python(vault).as_posix(), script.as_posix()])}`를 등록한다")
    except ValueError as e:
        fix = str(e)
    ran = f"마지막 실행 {_kst(run.get('at'))}" if run else ""
    same = [h for h in hooks if h["event"] == name]
    mine = [h for h in same if base.mentions(h["tokens"], script)]
    if mine:
        h = mine[0]
        where = f"등록({h['file']})"
        # 등록이 여럿이어도 matcher의 원인이 서로 배타적이면(SessionStart의 startup·resume)
        # 한 호출에 하나만 돈다 — 함께 불리는 원인이 있을 때만 중복이다.
        fires = [adapter.fires_on(event, m.get("matcher")) for m in mine]
        shared = sorted({s for k, a in enumerate(fires) for b in fires[k + 1:] for s in a & b})
        if len(mine) > 1:
            places = ", ".join(sorted({str(m["file"]) for m in mine}))
            if not shared:
                where = f"등록 {len(mine)}곳({places}) — matcher의 원인이 겹치지 않는다"
            else:
                where = f"같은 사건이 {len(mine)}곳에 등록돼 있다({places})"
                if shared != ["*"]:
                    where += f" — {'·'.join(shared)}에 함께 불린다"
        if not _runnable(h["tokens"][0]):
            return _item("fail", f"훅 {name}", f"{where} · 명령의 Python이 없다: {h['tokens'][0]}", fix)
        # 신뢰는 지금 등록의 것이고, 실행 기록은 호스트·사건별이라 옛 등록의 실행도 남는다.
        # 그래서 신뢰부터 본다 — 지난 실행이 지금 등록의 미신뢰를 가리지 않게.
        if adapter.trusted(h["file"], name, h["group"], h["index"]) is False:
            detail = (f"{where} · 지금 등록에 신뢰 기록이 없다 — {ran}은 이 등록이 신뢰된 증거가 아니다"
                      if run else f"{where} · 신뢰 기록도, 이 기기의 실행 기록도 없다")
            return _item("warn", f"훅 {name}", detail, adapter.reload)
        if shared:
            return _item("warn", f"훅 {name}", f"{where} · {ran}" if run else where, _ONE)
        limit = h.get("timeout")
        if isinstance(limit, (int, float)) and limit < base.HOOK_TIMEOUT:
            setup = core.shell_join([str(venv_python(vault)), str(engine_dir(vault) / "scripts" / "setup.py"), "--interactive"])
            return _item("warn", f"훅 {name}", f"{where} · 제한이 {limit}초다 — 설치는 {base.HOOK_TIMEOUT}초를 쓴다. "
                         "부하가 높으면 훅이 끝나기 전에 끊긴다", f"`{setup}`가 이 등록을 설치의 한 줄로 바꾼다")
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
                 '새 세션에서 에이전트에게 "osk 훅이 알려 준 프로젝트 키가 뭐야?"라고 묻는다 — '
                 "저장소 이름으로 답해야 한다")


def _version(vault: Vault, adapter, cli: str | None) -> dict:
    version, source = None, ""
    try:
        path = conversation_capture.recent_transcript(vault, adapter.name)
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
        asked = f"포착한 대화도 PATH의 `{adapter.cli}`도" if adapter.cli else "판본을 물을 CLI가 없고 포착한 대화도"
        return _item("info", "판본", f"알 수 없다 — {asked} 판본을 주지 않았다 (검증 {adapter.verified})")
    if adapters.newer(version, adapter.verified):
        return _item("warn", "판본", f"{source} {version} — 검증한 {adapter.verified}보다 새 판이다. "
                     "훅 입출력이나 전사 형식이 바뀌었을 수 있다",
                     "포착·주입이 이상하면 이 판본과 함께 알린다")
    return _item("ok", "판본", f"{source} {version} (검증 {adapter.verified})")


def _fork(vault: Vault, adapter) -> dict:
    try:
        executable = response_growth.configured(vault, adapter.name)
        if executable is False:
            return _item("info", "fork", "대화별 검토를 껐다 — fork도 세션 안 9·15턴 검토도 없고, "
                         "끝난 대화는 정기 실행이 통합한다")
        if not executable:
            return _item("info", "fork", "구독 fork를 설정하지 않았다 — 세션 안에서 9·15턴 검토로 동작한다",
                         "켜려면 SETUP의 '대화별 검토 훅'대로 .osk/response-growth.json에 CLI 경로를 둔다")
        verdict = response_growth.check(vault, adapter.name, None, None, os.getcwd())
    except (OSError, ValueError) as exc:
        return _item("warn", "fork", f"설정을 판정하지 못했다 — {exc}")
    if verdict["mode"] == "background":
        return _item("ok", "fork", "구독 fork 준비됨 — 최종 답변 Stop 9회마다 백그라운드 검토")
    return _item("warn", "fork", f"세션 안 검토로 돈다 — {verdict.get('reason')}",
                 f"근거는 `fork doctor --harness {adapter.name}`로 본다")


def _host(vault: Vault, adapter, records: dict) -> dict:
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
    items += _mcp(vault, adapter, servers)
    items += [_hook(vault, adapter, event, hooks, ran.get(event)) for event in adapters.SCRIPTS]
    now = time.time()
    for event in adapters.SCRIPTS:
        at = records.get("duplicates", {}).get(prefix + event)
        if isinstance(at, (int, float)) and 0 <= now - at < DUP_RECENT:
            items.append(_item("warn", f"중복 호출 {adapter.events[event]}",
                               f"같은 호출이 두 번 들어와 한 번만 처리했다(마지막 {_kst(at)}) — 등록이 겹친다",
                               _ONE + ". 설정 파일에 한 곳뿐이면 프로젝트 설정·플러그인 같은 다른 자리를 본다"))
    delivery = _delivery(ran.get("start"), records["overview"])
    if delivery:
        items.append(delivery)
    items.append(_version(vault, adapter, cli))
    if adapter.fork:
        items.append(_fork(vault, adapter))
    return {**head, "in_use": True, "items": items}


def _migrations(vault: Vault) -> list[tuple[str, object, object]]:
    """호환성·일괄 이행 — (이름, 옮길 대상, 적용). 체제가 바뀌어 옛 표기·자리를 옮겨야 하면 여기에 더한다
    (2026-11-09 사용자 결정): 점검은 `osk doctor`가 읽기만 하고, 적용은 `osk doctor --migrate` 하나가 확인을
    받은 뒤 이 순서로 한다. 대상은 옮길 것의 정체(자리·해시) 목록이고 확인은 그 목록에 묶인다. 근거 절
    이행이 기록 이관보다 먼저다 — 라운드 날짜를 기록의 지금 경로에 대한 git blame으로 정하므로, 먼저
    옮긴 기록은 거기서 추적되지 않아 그 항목이 보류된다. 앞 이행이 만든 표기는 뒤 이행이 받는다."""
    return [("비노드 derived-from → 관측 줄 (v5.4)",
             lambda: [[n["path"], n["before"]] for n in em.plan(vault)["nodes"] if n["new"]],
             lambda targets: em.apply(vault, expect=targets)),
            ("옛 자리의 인용 기록 → 루트 `_cited/` (v5.2)",
             lambda: [[m["from"], m["hash"]] for m in raw.consolidate(vault)["files"]],
             lambda targets: raw.consolidate(vault, apply=True, expect=targets)),
            ("관측 절 제목 `## 근거` → `# 관측` (v5.6)",
             lambda: [[it["path"], it["before"]] for it in em.heading_plan(vault) if it["new"] is not None],
             lambda targets: em.heading_apply(vault, expect=targets))]


MIGRATE_FIX = "`osk doctor --migrate`로 옮긴다 — 첫 실행은 계획만 보이고, 확인 뒤 1시간 안의 같은 실행이 적용한다"


def _vault(vault: Vault) -> list[dict]:
    items = []
    for name, targets, _apply in _migrations(vault):
        try:
            n = len(targets())
        except Exception as exc:
            items.append(_item("info", name, f"판독 실패 — {type(exc).__name__}: {exc}"))
            continue
        items.append(_item("warn", name, f"옮길 대상 {n}개", MIGRATE_FIX) if n
                     else _item("ok", name, "옮길 것이 없다"))
    return items


CONFIRM_TICKET = "osk-doctor-confirmation.json"
CONFIRM_WITHIN = 3600
MIGRATE_INSTRUCTION = ("여기서 멈추고 사용자에게 옮길 기록과 이행할 노드를 설명한 뒤 명시적 확인을 받는다. "
                       "확인 뒤 같은 명령을 1시간 안에 한 번 다시 실행하면 적용한다. 계획이 그 사이 달라지면 "
                       "새 확인을 요구한다.")


def migrate(vault: Vault) -> tuple[dict, int]:
    """남은 이행을 적용한다 — (결과, 종료 코드). 이행은 자료 이관이라 사용자가 계획을 본 뒤에 적용한다
    (Mechanism §1 4항·§5 4항·§9 5항). 첫 실행은 이행마다 옮길 대상과 `approval_required`를 내고 2로 끝난다.
    1시간 안에 같은 계획으로 다시 실행하면 순서대로 적용하고, 이행마다 전수 목록과 이행 기록을 남긴다.
    계획은 이 vault 자리와 대상의 정체로 짓는다 — worktree는 확인표 자리를 함께 쓰고(공통 git 디렉터리),
    개수만으로는 다른 대상으로 바뀐 계획을 가르지 못한다. 적용 전에 옛 정의로 적은 재검토 기록을 지금
    정의로 이어 적는다 — 그러지 않으면 바이트가 바뀐 노드의 기록이 옛 정의로 맞지 않아 그 쌍이 후보가 된다."""
    plan = {"root": str(vault.root),
            "migrations": [[name, found] for name, targets, _ in _migrations(vault) if (found := targets())]}
    if not plan["migrations"]:
        return {"ok": True, "applied": []}, 0
    review_id = core.sha256_bytes(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode())
    ticket = locks.local_lock_path(CONFIRM_TICKET, vault.root)
    try:
        pending = json.loads(ticket.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pending = {}
    if (not isinstance(pending, dict) or pending.get("review_id") != review_id
            or not isinstance(pending.get("at"), (int, float))
            or not 0 <= time.time() - pending["at"] < CONFIRM_WITHIN):
        durable.atomic_write(ticket, json.dumps({"review_id": review_id, "at": time.time()}).encode())
        return {"ok": False, "approval_required": True, "review_id": review_id, "plan": plan,
                "instruction": MIGRATE_INSTRUCTION}, 2
    ticket.unlink(missing_ok=True)   # 한 번만 — 실패한 적용도 새 확인을 요구한다
    rechecks.ensure_baseline(vault, carry=True)
    confirmed = dict(plan["migrations"])
    after: dict[str, str] = {}   # 이 실행의 앞선 이행이 쓴 노드 → 결과 해시. 뒤 이행은 그것을 예상값으로 받는다
    done = []
    for name, _targets, apply in _migrations(vault):
        if name in confirmed:
            result = apply([[p, after.get(p, h)] for p, h in confirmed[name]])
            after.update(result.pop("after", {}))
            done.append({"migration": name, **result})
    return {"ok": True, "applied": done}, 0


def report(vault: Vault, only: str | None = None) -> dict:
    """호스트별 점검 결과. `only`는 하네스 이름 하나로 좁힌다."""
    records = {**runs.read(vault), "duplicates": runs.duplicates(vault)}
    engine = _engine(vault, records)
    content = _vault(vault)
    hosts = [_host(vault, a, records) for a in adapters.ADAPTERS if only in (None, a.name)]
    items = engine + content + [i for h in hosts for i in h["items"]]
    counts = {level: sum(i["level"] == level for i in items) for level in LEVELS}
    return {"ok": not counts["fail"], "root": str(vault.root), "counts": counts,
            "engine": engine, "vault": content, "hosts": hosts}


def _section(lines: list, title: str, items: list) -> None:
    lines.append(title)
    for i in items:
        lines.append(f"  {i['level']:<4}  {i['check']} — {i['detail']}")
        if i.get("fix"):
            lines.append(f"        → {i['fix']}")


def _verdict(c: dict) -> str:
    return f"판정: 실패 {c['fail']} · 경고 {c['warn']} · 정보 {c['info']} · 정상 {c['ok']}"


def text(rep: dict) -> str:
    lines = [f"osk doctor — {rep['root']}"]
    _section(lines, "엔진", rep["engine"])
    _section(lines, "vault 이행", rep["vault"])
    for h in rep["hosts"]:
        _section(lines, h["title"], h["items"])
    lines.append(_verdict(rep["counts"]))
    return "\n".join(lines) + "\n"


# ── 저장소 점검 (`doctor --vault`) ─────────────────────────────────────────────
# 연결이 아니라 vault의 내용을 본다 — 판이 오르며 옛 형식이 된 것(이전), 검증기가 잡는 형식 문제,
# 정기 검토가 닫지 못하고 남긴 일(잔재), 대화 검토 적체. 읽기만 한다 — 이전은 `--migrate`(위)가 하고,
# 판단이 드는 나머지는 고칠 자리를 알린다.
_EDIT = "노드를 read_node로 읽고 update_node로 고친다"


def _first(errs: list, n: int = 3) -> str:
    return " / ".join(map(str, errs[:n])) + (f" 외 {len(errs) - n}건" if len(errs) > n else "")


def _vault_items(vault: Vault, read_tools) -> list[dict]:
    """vault 내용 점검의 항목 — 남은 이행(`_vault`), 기록 충돌, 검증기, 잔재, 적체. 읽기만 한다."""
    rep = validate.run(vault, read_tools)
    items = _vault(vault)
    for c in raw.consolidate(vault)["conflicts"]:
        items.append(_item("warn", "기록 충돌", f"{c['path']} — {c['reason']}",
                           "어느 사본이 정본인지 사용자와 정한다 — 엔진은 덮거나 합치지 않는다"))

    for f in rep["fail"]:
        for name, errs in f.items():
            items.append(_item("fail", f"검증 {name}", f"{len(errs)}건 — {_first(errs)}", _EDIT))
    for s in rep["skipped"]:
        for name, why in s.items():
            items.append(_item("info", f"검증 {name}", str(why)))
    warnings = rep.get("warnings") or {}
    dangling = warnings.get("dangling_refs") or []
    if dangling:
        items.append(_item("warn", "끊긴 참조", f"{len(dangling)}건 — {_first(dangling, 2)}", _EDIT))
    if not (rep["fail"] or dangling):
        items.append(_item("ok", "형식", f"검증 {len(rep['pass'])}종 통과"))

    rc = (warnings.get("rechecks") or {}).get("count") or 0
    if rc:
        items.append(_item("warn", "근거 재검토", f"후보 {rc}건이 닫히지 않았다",
                           "`osk.cli rechecks`의 후보마다 노드와 근거를 읽고, 맞으면 update_node(rechecked=[대상])로 "
                           "닫는다 — cascade거나 다음 노드까지 고치게 되는 수정은 사용자에게 올린다"))
    blocked = conversation_state.blocked(vault, limit=1000)
    if blocked["count"]:
        gone = sum(c["error"].startswith("FileNotFoundError") for c in blocked["conversations"])
        items.append(_item("warn", "포착 막힘", f"대화 {blocked['count']}건"
                           + (f" — 그중 {gone}건은 원본 전사가 이 기기에서 지워졌다" if gone else "")
                           + f" · 최근 오류: {blocked['conversations'][0]['error'][:160]}",
                           "잃은 것은 없다 — 검토는 9·15턴에 계속된다. 오류를 사용자에게 보인다"))
    for r in scope_memory.recovery_status(vault):
        items.append(_item("warn", "scope 복구 대기", f"{r['session']} → {r['scope'] or '착지 미정'} · {r['since']}부터",
                           f"프로젝트 {r['session']}의 새 세션을 열면 시작 훅이 복구 절차를 싣는다"))
    for scope, s in sorted(evictions.status(vault).items()):
        items.append(_item("warn" if s["overdue"] else "info", "정돈 대기",
                           f"{scope} — 미처분 {s['unsettled']}건, 가장 오래된 것 {s['oldest_days']}일",
                           f"`osk.cli tidy prompt --scope {scope}`의 프롬프트로 정돈 세션을 연다"))
    try:
        b = growth_daily.backlog(vault)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        items.append(_item("info", "대화 검토 적체", f"세지 못했다 — {type(exc).__name__}: {exc}"))
    else:
        if b["conversations"]:
            unit = (f"최근 Scope 묶음 {b['measured_batches']}개 실측 평균" if b["measured_batches"]
                    else "실측이 없어 첫 추정")
            items.append(_item("info", "대화 검토 적체",
                               f"끝난 대화 {b['conversations']}개·{b['turns']}턴 — Scope 묶음 약 {b['batches']}개, "
                               f"예상 약 {b['tokens']:,} 토큰(묶음당 {b['unit_tokens']:,}, {unit})",
                               growth_daily.BACKLOG_COMMAND))
    for region, state in sorted((rep.get("protected_regions") or {}).items()):
        if state != "clean":
            items.append(_item("info", "보호영역", f"{region} — {state}",
                               "사용자가 대화형 단말에서 `approve`나 `revert`로 처리한다"))
    return items


def vault(vault: Vault, read_tools) -> tuple[dict, int]:
    """(보고, 종료 코드) — 읽기만 한다. 실패가 있으면 1이다. 남은 이행의 적용은 `migrate`가 한다.
    `read_tools`는 검증기에 건넬 표면의 도구 목록이다(`validate.run`)."""
    items = _vault_items(vault, read_tools)
    counts = {level: sum(i["level"] == level for i in items) for level in LEVELS}
    rep = {"ok": not counts["fail"], "root": str(vault.root), "counts": counts, "items": items}
    return rep, 0 if rep["ok"] else 1


def vault_text(rep: dict) -> str:
    lines = [f"osk doctor --vault — {rep['root']}"]
    _section(lines, "저장소", rep["items"])
    lines.append(_verdict(rep["counts"]))
    return "\n".join(lines) + "\n"
