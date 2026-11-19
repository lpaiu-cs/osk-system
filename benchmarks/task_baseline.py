"""과업으로 재는 기준선(#19) — 고정 과업을 고정 저장소에서 에이전트에게 시키고, 표면만으로 끝냈는지 센다.

    python benchmarks/task_baseline.py <작업 폴더> --model <모델> [--ref v6.0.0] [--repeat 3] [--task 이름 ...]
                                       [--prelude 30]

- 저장소: 잴 판(`--ref`)의 트리를 풀고, `task_fixture.json`의 노드를 그 판의 표면으로 심는다. 과업마다 그
  사본에서 돈다 — 앞 실행이 남긴 것을 뒤 실행이 보지 않는다. 대화 검토는 끈다(`.osk/response-growth.json`).
  검토 대행을 정하지 않은 저장소는 시작 훅이 세션 안의 검토를 지시하며 검토할 노드의 이름을 싣고, 정한
  저장소는 종료 훅이 분리 작업자를 띄운다 — 앞의 것은 과업의 답을 미리 보이고, 뒤의 것은 끝난 저장소를 고친다.
- 에이전트: Claude Code를 사람 없이 한 번 띄운다(정기 성장 실행이 쓰는 명령 `growth_argv`). 주는 것은 그
  저장소의 MCP 도구와 훅뿐이다. 내장 도구는 꺼져 있어, 표면 밖으로 돌아가는 길이 없다 — 표면이 모자라면
  과업이 끝나지 않는다. 이 기기의 사용자 설정·플러그인·지침 파일(CLAUDE.md)은 싣지 않는다 — 사용자 설정을
  실으면 그 기기에 등록된 다른 저장소의 osk 훅이 이 실행에서도 돈다.
- 과업: 사용자가 하는 말 한 번이다. 무엇을 불러야 하는지는 말해 주지 않는다.
- `recall-correction`은 사본의 승인 규칙을 먼저 정정하고 새 세션에 판단을 맡긴다. 첫 줄의 진행·대기와
  본문 열람·결정 번호를 따로 기록한다. 다른 과업의 바탕에는 옛 규칙을 유지한다.
- 긴 대화(`--prelude N`): 과업의 말 앞에 기억과 무관한 턴 N개를 같은 세션에서 먼저 준다(`filler`) — 대화가
  길어지면 에이전트가 검색을 덜 하는지 보려는 것이다. 채점은 마지막 턴(과업)의 도구 호출만 본다. 앞 턴에서
  부른 도구의 수는 따로 낸다(`prelude_calls`). 실행 폴더와 보고서의 이름에 N이 붙는다.
- `startup-rule`은 사본에 서로 다른 설치 저널과 과거 요약을 심는다. 제목의 첫 표지로 현행·과거·기타를
  가르고, 실제 시작 훅에 추가된 설치판 줄의 글자 수와 마지막 result가 보고한 입력 사용량을 따로 낸다.
- 채점: 끝난 저장소의 파일과 실행 기록으로 한다(`CHECKS`). 엔진을 읽지 않는다.
- 내는 것: 과업마다 끝낸 횟수, 검사별 통과, 도구 호출 수, 거부의 수와 종류, 넘긴 거부, 걸린 시간.

읽을 때의 한계:

- 모델의 사용량이 든다. 같은 과업도 실행마다 다르다 — 횟수가 적으면 수의 폭이 넓다.
- 끝난 실행은 다시 돌지 않는다(`runs/<과업>-<번>/stream.jsonl`이 있으면 그것을 읽는다). 그래서 바탕에 판·모델·
  과업 파일의 해시를 적어 두고(`base/commit`), 하나라도 다르면 그 작업 폴더를 받지 않는다.
- 훅이 띄우는 분리 작업자는 끈다(`OSK_HOOK_WORKER=0`) — 실행이 끝난 뒤에 저장소를 고치는 것이 없게 한다.
- 실행의 전사는 Claude Code의 전사 폴더에서 실행 폴더로 옮긴다 — 남겨 두면 지난 기록을 읽는 계기
  (`recall_shadow.py`)가 그것을 사용자의 대화로 센다. 도중에 끊은 실행의 전사는 그 폴더에 남는다.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(Path(__file__).with_name("task_fixture.json").read_text(encoding="utf-8"))
ENGINE = "_governance/_engine"
GIT = ("git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid")
DATED = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0900", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0900"}
SCRUB = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OSK_VAULT_ROOT")     # 구독 로그인만 쓰고, 저장소는 실행의 사본이다
QUIET = {"OSK_UPDATE_CHECK": "0", "OSK_HOOK_WORKER": "0", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}
LIMIT = 900                      # 실행 하나의 제한(초)

WATCH = "배포 뒤 10분은 오류율 화면을 본다"
STAGING = "스테이징 DB 마이그레이션은 수동 승인 뒤에 돈다"
JITTER = "재시도 간격에는 지터를 넣는다"
COMMIT = "커밋 제목은 영역 이름으로 시작한다"
WEDNESDAY = "배포는 수요일에 하지 않는다"
SECURITY = "00_Scope/harbor/보안 점검"
# 긴 대화의 앞 턴 — 저장소의 지식과 무관하고, 도구 없이 답할 수 있는 말이다. 짝수 번째 턴에는 긴 붙여넣기가 붙는다.
ASIDES = ("다음 문장을 절반 길이로 줄여 줘: 회의는 예정보다 길어졌지만 결론은 처음 안과 크게 다르지 않았다.",
          "{n} 곱하기 17은 얼마야? 수만 답해 줘.",
          "'일정을 조정하다'를 더 쉬운 말로 바꾼 표현 세 가지만 알려 줘.",
          "다음 낱말을 가나다순으로 늘어놓아 줘: 창문, 가위, 모자, 바람, 다리.",
          "섭씨 {n}도는 화씨로 몇 도야? 수만 답해 줘.")


def inside(action: str, vault: Path, python: str) -> None:
    """과업용 저장소의 엔진으로 도는 쪽 — 내용을 심거나(`seed`), 에이전트를 띄울 명령과 훅을 낸다(`launch`)."""
    sys.path.insert(0, str(vault / ENGINE))
    if action == "launch":
        from osk.harness import base
        from osk.harness.claude import ADAPTER
        hooks = {name: [ADAPTER.hook_group(event, ADAPTER.hook_command(
            [Path(python).as_posix(), (vault / ENGINE / "scripts/hooks" / base.SCRIPTS[event]).as_posix()]))]
            for event, name in ADAPTER.events.items()}
        argv = ADAPTER.growth_argv("claude", python, str(vault / ENGINE / "mcp_server.py"), str(vault))
        print(json.dumps({"argv": argv, "hooks": hooks}))
        return
    import mcp_server as surface
    if action == "recall-correction":
        note = surface.read_node(name=STAGING)
        result = surface.update_node(name=STAGING, expect_hash=note["hash"], **FIXTURE["tasks"][action]["correction"])
        if not result.get("ok"):
            raise SystemExit(f"정정 준비 실패: {result}")
        return
    from osk import routing
    from osk.vault import Vault
    session = FIXTURE["session"]
    if action == "startup-rule":
        from osk import core, update_state
        case = FIXTURE["tasks"][action]
        note = surface.read_node(COMMIT)
        updated = surface.update_node(COMMIT, body=case["body"], summary=case["summary"], expect_hash=note["hash"])
        memory = surface.scope_memory(session)
        stored = surface.scope_memory(session, text=memory["text"] + "\n" + case["memory"], expect_hash=memory["hash"])
        if not updated.get("ok") or not stored.get("ok"):
            raise SystemExit(f"판별 규약 준비 실패: {updated}, {stored}")
        core.ledger_append(update_state.journal_path(Vault.open()), {"kind": "done", "version": case["installed"]})
        return
    for node in FIXTURE["nodes"]:
        surface.search(query=node["title"], session=session)        # 표면은 검색하지 않은 세션의 새 노드를 받지 않는다
        for _ in range(2):                                           # 최상위 군집은 같은 요청을 다시 보내야 선다
            out = surface.create_node(**node, drafter="fixture", session=session)
            if out.get("ok", True):
                break
        else:
            raise SystemExit(f"심지 못했다: {node['title']} — {out}")
    routing.set_primary(Vault.open(), session, FIXTURE["scope"].rsplit("/", 1)[-1], "fixture")
    surface.scope_memory(session=session, text=FIXTURE["scope_memory"])
    from osk import response_growth                                  # 대화 검토를 끈다 — 모듈 문서의 "저장소"
    response_growth.config_path(Vault.open()).parent.mkdir(exist_ok=True)
    response_growth.config_path(Vault.open()).write_text(json.dumps({"claude": False}), encoding="utf-8")
    verdict = surface.run_validators()
    if verdict["verdict"] != "PASS":
        raise SystemExit(f"심은 저장소가 검증을 지나지 못했다: {verdict['fail']}")


def _sh(*argv, cwd=None, env=None, stdin=None) -> bytes:
    return subprocess.run(argv, cwd=cwd, env=env, input=stdin, capture_output=True, check=True).stdout


def _inside(action: str, vault: Path, python: str) -> bytes:
    env = {**{k: v for k, v in os.environ.items() if k not in SCRUB}, **QUIET,
           "OSK_VAULT_ROOT": str(vault), "PYTHONUTF8": "1"}
    return _sh(python, str(Path(__file__).resolve()), "--inside", action, str(vault), python, env=env, cwd=vault)


def stamp(commit: str, model: str) -> str:
    """바탕의 표시 — 잴 판, 모델, 과업 파일의 sha256 앞 12자. 같아야 지난 실행을 다시 쓴다."""
    digest = hashlib.sha256(Path(__file__).with_name("task_fixture.json").read_bytes()).hexdigest()[:12]
    return f"{commit} {model} {digest}"


def build(work: Path, ref: str, python: str, model: str) -> None:
    """잴 판의 트리와 고정 내용으로 저장소와 프로젝트 폴더를 세운다 — 실행마다 이것을 베낀다."""
    vault, project, mark = work / "base/vault", work / "base" / FIXTURE["session"], work / "base/commit"
    want = stamp(_sh("git", "-C", str(ROOT), "rev-parse", f"{ref}^{{commit}}").decode().strip(), model)
    if mark.exists():                        # 다 세운 바탕만 이 표시를 가진다
        if mark.read_text(encoding="utf-8") != want:
            raise SystemExit(f"이 작업 폴더의 바탕은 다른 판·모델·과업 내용이다 — 그것마다 다른 폴더에서 잰다: {work}")
        return
    project.mkdir(parents=True)
    (project / "README.md").write_text(f"# {FIXTURE['session']}\n", encoding="utf-8")
    tarfile.open(fileobj=io.BytesIO(_sh("git", "-C", str(ROOT), "archive", ref))).extractall(vault)
    for folder in (vault, project):          # 운영 저장소처럼 git 저장소다. 프로젝트의 뿌리 커밋은 늘 같다 — 훅이 같은 키를 준다
        _sh(*GIT, "init", "-q", "-b", "main", cwd=folder)
        _commit(folder, "처음")
    _inside("seed", vault, python)
    _commit(vault, "fixture")
    mark.write_text(want, encoding="utf-8")


def _commit(folder: Path, message: str) -> None:
    _sh(*GIT, "add", "-A", cwd=folder)
    _sh(*GIT, "commit", "-q", "-m", message, cwd=folder, env={**os.environ, **DATED})


def filler(count: int) -> list[str]:
    """과업 앞에 둘 무관한 턴 — 번호만으로 정해진다. 짝수 번째에는 지어낸 기록 40줄을 붙여 문맥을 키운다."""
    turns = []
    for n in range(1, count + 1):
        text = ASIDES[n % len(ASIDES)].format(n=n + 10)
        if n % 2 == 0:
            lines = [f"{line:02d} 창고-{(n * 7 + line) % 9} 상자 {(n * 31 + line * 17) % 400}개 "
                     f"대기 {(n * 13 + line * 29) % 97}분" for line in range(1, 41)]
            text = "\n".join(["아래 기록에서 대기가 가장 긴 줄의 번호만 알려 줘.", *lines])
        turns.append(text)
    return turns


def run(work: Path, task: str, turn: int, python: str, model: str, prelude: int | None = None,
        *, recall_shadow: bool = False) -> Path:
    """과업 하나를 새 사본에서 한 번 — 실행 기록(`stream.jsonl`)과 끝난 저장소를 실행 폴더에 남긴다. `prelude`를
    주면 무관한 턴들을 같은 세션에 먼저 넣는다(`_converse`)."""
    here = work / "runs" / (f"{task}-{turn}" if prelude is None else f"{task}-p{prelude}-{turn}")
    if (here / "stream.jsonl").exists():
        return here
    if here.exists():
        raise SystemExit(f"끝나지 않은 실행의 폴더가 있다 — 지우고 다시 돈다: {here}")
    shutil.copytree(work / "base", here)
    vault = here / "vault"
    if task == "recall-correction":
        _inside(task, vault, python)  # 새 세션은 정정 대화를 보지 않고 저장된 결정만 읽는다.
    recipe = json.loads(_inside("launch", vault, python))
    if task == "startup-rule":
        _inside(task, vault, python)
    argv, at = recipe["argv"], recipe["argv"].index("--settings") + 1
    argv[at] = json.dumps({**json.loads(argv[at]), "hooks": recipe["hooks"]})
    argv += ["--model", model, "--setting-sources", "project"]       # 사용자 설정(훅·플러그인)을 싣지 않는다
    env = {**{k: v for k, v in os.environ.items() if k not in SCRUB}, **QUIET}
    if recall_shadow:
        env.update(OSK_RECALL_SHADOW="1", OSK_HOOK_WORKER="1")
    said, folder = FIXTURE["tasks"][task]["prompt"], here / FIXTURE["session"]
    if prelude is None:
        done = subprocess.run(argv, input=said.encode("utf-8"), capture_output=True, cwd=folder, env=env, timeout=LIMIT)
        stream = done.stdout
        (here / "stderr.txt").write_bytes(done.stderr)
    else:
        stream = _converse(argv + ["--input-format", "stream-json"], [*filler(prelude), said], folder, env,
                           LIMIT * (1 + prelude // 10), here / "stderr.txt")
    _keep_transcript(stream, here)
    (here / "stream.jsonl").write_bytes(stream)
    return here


def _ends_turn(row: bytes) -> bool:
    try:
        return json.loads(row).get("type") == "result"
    except (ValueError, AttributeError):
        return False


def _commit_rule(answer: str) -> str:
    lines = [line.strip() for line in answer.splitlines() if line.strip() and not line.strip().startswith("```")]
    if len(lines) != 1:
        return "기타"
    prefix, _, subject = lines[0].partition(":")
    if not subject.strip("`*_ "):
        return "기타"
    return {"계정응답": "현재", "회원검사": "과거"}.get(prefix.strip("`*_ "), "기타")


def _version_line_chars(row: dict) -> int | None:
    """OSK가 실제로 보낸 설치판 한 줄(개행 포함). 구판은 0, OSK 시작 문맥을 못 읽으면 None."""
    try:
        context = json.loads(row["output"])["hookSpecificOutput"]["additionalContext"]
    except (KeyError, TypeError, ValueError):
        return None
    lines = context.splitlines(keepends=True) if isinstance(context, str) else []
    for i, line in enumerate(lines):
        if line.startswith("[osk 세션 시작 —"):
            field = lines[i + 1] if i + 1 < len(lines) else ""
            return len(field) if field.startswith("OSK 설치판(갱신 저널 기준):") else 0
    return None


def _converse(argv: list, texts: list, folder: Path, env: dict, limit: int, errors: Path) -> bytes:
    """말을 하나씩 준다 — 턴이 끝난 것(`result`)을 보고 다음 말을 넣는다. 한꺼번에 넣으면 Claude Code가 밀린
    말들을 한 턴으로 묶는다(말 셋을 넣었더니 턴이 둘이었다). 제한을 넘기면 죽이고, 그때까지의 기록을 낸다."""
    got = []
    with errors.open("wb") as err, subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                                    cwd=folder, env=env) as proc:
        timer = threading.Timer(limit, proc.kill)
        timer.start()
        try:
            for text in texts:
                line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
                for row in proc.stdout:
                    got.append(row)
                    if _ends_turn(row):
                        break
                else:
                    break                                   # 출력이 끝났다 — 프로세스가 끝났거나 죽었다
            proc.stdin.close()
            got += proc.stdout.readlines()
        except OSError:                                     # 죽은 프로세스의 입력에 썼다 — 그때까지의 기록을 낸다
            pass
        finally:
            timer.cancel()
    return b"".join(got)


def _keep_transcript(stream: bytes, here: Path) -> None:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    found = re.search(rb'"session_id":\s*"([0-9a-f\-]{36})"', stream)
    for path in home.glob(f"projects/*/{found[1].decode()}.jsonl") if found else ():
        shutil.move(path, here / "transcript.jsonl")
        if not any(left.is_file() for left in path.parent.rglob("*")):      # 빈 기억 폴더만 남는다
            shutil.rmtree(path.parent)


def _text(content) -> str:
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    try:                                      # 도구의 응답은 JSON이다 — 한글이 그대로 보이게 다시 쓴다
        return json.dumps(json.loads(content), ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content or "")


def record(stream: str, *, startup: bool = False) -> dict:
    """실행 기록에서 도구 호출과 그 응답, 마지막 답, 걸린 것을 뽑는다. 턴이 여럿이면(긴 대화) 마지막 턴의 호출만
    남기고, 앞 턴들의 호출 수와 끝난 턴의 수를 따로 낸다."""
    calls, by_id, last, ends = [], {}, {}, []
    version_chars = None
    for line in stream.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (startup and isinstance(row, dict) and row.get("subtype") == "hook_response"
                and row.get("hook_event") == "SessionStart"):
            chars = _version_line_chars(row)
            if chars is not None:
                version_chars = chars
        content = (row.get("message") or {}).get("content") if isinstance(row, dict) else None
        for block in content if isinstance(content, list) else ():
            if block.get("type") == "tool_use":
                by_id[block["id"]] = {"tool": block["name"].rsplit("__", 1)[-1], "input": block.get("input") or {},
                                      "output": "", "rejected": False}
                calls.append(by_id[block["id"]])
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in by_id:
                call = by_id[block["tool_use_id"]]
                call["output"] = _text(block.get("content"))
                call["rejected"] = bool(block.get("is_error")
                                        or re.match(r'\{"(ok": false|error":)', call["output"]))
        if isinstance(row, dict) and row.get("type") == "result":
            last = row
            ends.append(len(calls))
    before = ends[-2] if len(ends) > 1 else 0
    result = {"calls": calls[before:], "prelude_calls": before, "turns_done": len(ends),
              "answer": last.get("result") or "", "turns": last.get("num_turns"),
              "seconds": round((last.get("duration_ms") or 0) / 1000), "finished": last.get("subtype") == "success"}
    if startup:
        usage = last.get("usage") or {}
        result["startup"] = {"rule": _commit_rule(result["answer"]), "version_line_chars": version_chars,
                             "reported_input": {k: usage.get(k) for k in
                                                ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}}
    return result


def nodes(vault: Path) -> dict:
    """저장소의 노드 — 제목 → (군집의 경로, 글). 파일만 본다."""
    return {path.stem: (path.parent.relative_to(vault).as_posix(), path.read_text(encoding="utf-8"))
            for top in ("00_Scope", "00_Domain", "00_Person") for path in (vault / top).rglob("*.md")
            if "Workbench" not in path.parts}


def _reached(title: str, calls: list) -> bool:
    return any(title in call["output"] or call["input"].get("name") == title for call in calls)


def _refine(before, after, new, changed, run):
    text = after.get(WATCH, ("", ""))[1]
    return {"그 노드를 고쳤다": "0.5%" in text, "새 노드를 만들지 않았다": not new,
            "옛 규칙의 문장을 남기지 않았다": "1%를 넘으면" not in text}       # 옛 값을 이력으로 적는 것은 된다


def _update(before, after, new, changed, run):
    text = after.get(STAGING, ("", ""))[1]
    return {"그 노드를 고쳤다": "dry-run" in text.lower(), "새 노드를 만들지 않았다": not new,
            "옛 규칙의 문장을 남기지 않았다": "눌러야 돈다" not in text}


def _record(before, after, new, changed, run):
    made = [text for space, text in new.values() if "24시간" in text]
    return {"새 노드를 남겼다": bool(made),
            "예약 군집에 두었다": any(space == "00_Scope/harbor/예약" and "24시간" in text for space, text in new.values()),
            "근거를 적었다": any("# 관측" in text for text in made)}


def _startup_rule(before, after, new, changed, run):
    return {"현재 규약으로 제목을 썼다": run["startup"]["rule"] == "현재"}


def _reject(before, after, new, changed, run):
    return {"새 군집에 허브를 세웠다": new.get("보안 점검", ("",))[0] == SECURITY,
            "노드를 그 군집에 두었다": any(space == SECURITY and "취약점" in text and title != "보안 점검"
                                  for title, (space, text) in new.items())}


def _domain(before, after, new, changed, run):
    kept = [text for space, text in {**new, **changed}.values() if space.startswith("00_Scope") and "웹훅" in text]
    return {"Scope에 남겼다": bool(kept), "Domain의 노드에 기댔다": any(f"[[{JITTER}]]" in text for text in kept)}


def _recall_apply(before, after, new, changed, run):
    return {"규약의 노드에 닿았다": _reached(COMMIT, run["calls"]),
            "규약대로 썼다": bool(re.search(r"계정\s*:", run["answer"]))}


def _recall_ask(before, after, new, changed, run):
    return {"그 노드에 닿았다": _reached(WEDNESDAY, run["calls"]), "기억으로 답했다": "정산" in run["answer"]}


def _recall_correction(before, after, new, changed, run):
    return {"정정한 본문을 읽었다": any(c["tool"] == "read_node" and not c["rejected"] and "STG-42" in c["output"]
                                  for c in run["calls"]),
            "현재 절차를 적용했다": _decision(run["answer"]) == "진행",
            "결정 번호를 댔다": "STG-42" in run["answer"]}


def _decision(answer: str) -> str:
    first = answer.strip().partition("\n")[0].strip(" *\r")
    return first if first in ("진행", "대기") else "미판정"


CHECKS = {"refine": _refine, "update": _update, "record": _record, "reject": _reject, "domain": _domain,
          "recall-apply": _recall_apply, "recall-ask": _recall_ask, "recall-correction": _recall_correction}

CHECKS["startup-rule"] = _startup_rule


def _why(call: dict) -> str:
    try:
        data = json.loads(call["output"])
        text = (data.get("violations") or [data.get("error") or ""])[0]
    except (ValueError, AttributeError):
        text = call["output"]
    return f'{call["tool"]}: {text[:40]}'


def score(task: str, before: dict, after: dict, run: dict) -> dict:
    """실행 하나의 채점 — 검사가 모두 참이면 끝낸 것이다. 거부는 도구가 받지 않은 호출이고, 넘긴 거부는
    그 뒤에 같은 도구의 호출이 받아들여진 거부다."""
    new = {title: node for title, node in after.items() if title not in before}
    changed = {title: node for title, node in after.items() if title in before and node != before[title]}
    checks, calls = CHECKS[task](before, after, new, changed, run), run["calls"]
    refused = [at for at, call in enumerate(calls) if call["rejected"]]
    kinds = [_why(calls[at]) for at in refused]
    passed =sum(any(later["tool"] == calls[at]["tool"] and not later["rejected"] for later in calls[at + 1:])
                 for at in refused)
    return {"completed": run["finished"] and all(checks.values()), "checks": checks, "calls": len(calls),
            "rejections": kinds, "rejections_passed": passed, "repeated_rejections": len(kinds) - len(set(kinds)),
            "turns": run["turns"], "seconds": run["seconds"], "tools": [call["tool"] for call in calls],
            "prelude_calls": run["prelude_calls"], "turns_done": run["turns_done"],
            **({"decision": _decision(run["answer"])} if task == "recall-correction" else {})}


def summary(scores: list) -> dict:
    """한 과업의 실행들을 수로."""
    kinds = collections.Counter(kind for s in scores for kind in s["rejections"])
    return {"runs": len(scores), "completed": sum(s["completed"] for s in scores),
            "checks": {name: sum(s["checks"][name] for s in scores) for name in scores[0]["checks"]},
            "calls": sorted(s["calls"] for s in scores), "rejections": sum(kinds.values()),
            "rejections_passed": sum(s["rejections_passed"] for s in scores),
            "runs_with_a_rejection": sum(bool(s["rejections"]) for s in scores),
            "repeated_rejections": sum(s["repeated_rejections"] for s in scores),
            "rejection_kinds": dict(kinds.most_common()), "seconds": sorted(s["seconds"] for s in scores),
            "searched": sum("search" in s["tools"] for s in scores),
            "prelude_calls": sorted(s["prelude_calls"] for s in scores),
            "turns_done": sorted(s["turns_done"] for s in scores)}


def main() -> int:
    if sys.argv[1:2] == ["--inside"]:
        inside(sys.argv[2], Path(sys.argv[3]), sys.argv[4])
        return 0
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("work", type=Path, help="실행 폴더 — 짧은 경로에 둔다(Windows의 경로 길이)")
    parser.add_argument("--model", required=True)
    parser.add_argument("--ref", default="HEAD", help="잴 판 — 태그나 커밋")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--task", action="append", choices=list(FIXTURE["tasks"]))
    parser.add_argument("--python", default=sys.executable, help="MCP 서버와 훅을 돌릴 파이썬(mcp가 깔린 것)")
    parser.add_argument("--prelude", type=int, default=None, help="과업 앞에 둘 무관한 턴의 수 — 긴 대화로 잰다")
    parser.add_argument("--recall-shadow", action="store_true", help="입력 훅의 후보 측정도 켜고 별도 폴더에 기록한다")
    args = parser.parse_args()
    work = args.work.resolve()
    if args.recall_shadow:
        work = work / "recall-shadow"
    build(work, args.ref, args.python, args.model)
    if args.recall_shadow and not (work / "base/vault" / ENGINE / "osk/recall_probe.py").is_file():
        parser.error("지정한 --ref는 입력 훅의 후보 측정을 지원하지 않는다")
    before, report = nodes(work / "base/vault"), {}
    for task in args.task or FIXTURE["tasks"]:
        scores = []
        for turn in range(1, args.repeat + 1):
            here = run(work, task, turn, args.python, args.model, args.prelude, recall_shadow=args.recall_shadow)
            stream = (here / "stream.jsonl").read_text(encoding="utf-8", errors="replace")
            result = record(stream, startup=task == "startup-rule")
            scores.append(score(task, before, nodes(here / "vault"), result))
            if task == "startup-rule":
                scores[-1]["startup"] = result["startup"]
            if args.prelude is not None and scores[-1]["turns_done"] != args.prelude + 1:
                scores[-1]["completed"] = False             # 앞 턴을 다 돌지 못했다 — 잰 조건이 아니다
            print(f"{task}-{turn}: {'끝냄' if scores[-1]['completed'] else '못 끝냄'} {scores[-1]['checks']} "
                  f"호출 {scores[-1]['calls']} 거부 {len(scores[-1]['rejections'])}", file=sys.stderr)
        report[task] = {**summary(scores), "each": scores}
    name = "report.json" if args.prelude is None else f"report-p{args.prelude}.json"
    (work / name).write_text(json.dumps({"ref": args.ref, "model": args.model, "prelude": args.prelude,
                                         "tasks": report},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({task: {k: v for k, v in r.items() if k != "each"} for task, r in report.items()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
