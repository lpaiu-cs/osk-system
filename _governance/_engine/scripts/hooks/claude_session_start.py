"""Claude Code / Codex SessionStart 훅 — scope 기억·복구·정돈 주입.

등록(사용자 settings.json → hooks.SessionStart → command):
    <인스턴스>/.venv/Scripts/python.exe <인스턴스>/_governance/_engine/scripts/hooks/claude_session_start.py

stdin으로 하네스가 주는 JSON({cwd, session_id, source, …})을 받고, stdout의
hookSpecificOutput.additionalContext가 세션 문맥에 주입된다. 지시("CLAUDE.md에
쓰라")는 읽혀도 눈앞에 없으면 쓰이지 않는다는 것이 실측이라, 보여주는 일은
훅이 맡는다 — 보이지 않는 것은
통합되지 않는다.

프로젝트 키는 cwd가 속한 git 저장소의 **본 저장소 디렉터리 이름**이다. 워크트리
안에서도 본 저장소 이름으로 접힌다(`git-common-dir`의 부모) — 워크트리 이름은
세션마다 달라 키가 되지 못한다. 서브모듈·bare 저장소는 자기 이름을 받는다.
이름이 같은 **무관한** 저장소는 뿌리 커밋이 달라 파생 키(`<이름>-<뿌리 앞 8자>`)로
갈린다(`session_key`). 한 프로젝트는 여러 scope에 결속될 수 있고 그중 하나가 주된
scope다(시행령 §3 10항) — 훅은 결속된 scope 전부의 기억을 싣는다(주된 scope 먼저).
결속이 없거나 주된 scope가 없어도 `overview`로 착지를 확인하도록 안내한다. 아직 없는
착지를 훅이 대신 정하지 않는다.

**정돈도 같은 길로 싣는다**(Mechanism §9-3 1항). 세션이 곧 주기다 — 별도
스케줄러 없이, 결속이 선 세션이 시작되면 그 scope의 미처분 퇴출 항목 중 오래된
것부터 K개와 Workbench의 경유 노드를 함께 실어 첫 도구 호출에 처분을 함께
실으라고 지시한다. 벽이 아니다. 가장 오래된 항목이 N일을 넘으면 "밀렸다"를
**맨 앞**에 세운다(3항). 문안은 `osk.evictions`가 만든다 — 전용 세션의
프롬프트(`osk tidy prompt`)와 같은 말을 쓰기 위해서다.

자기 대화의 착지를 고정하고 완료 전사의 포착은 분리 작업자에게 넘기며, durable 검토
대기를 이어받는다. 재개는 카운터·대기를 지우지 않는다. 어떤 실패도 세션 시작을 막지 않지만 진단은 싣는다.

**보이는 만큼만 싣는다.** 호스트는 훅 문맥을 자른다(Claude Code는 1만 자를 넘으면 앞
2KB만, Kiro는 3천 자). 잘리면 뒤쪽 블록의 지시가 통째로 사라지므로, 호스트 예산 안에
블록 단위로 조립하고 넘치는 블록은 건수와 읽는 곳의 한 줄로 접는다(`osk.hook_text`).
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]        # …/_governance/_engine
sys.path.insert(0, str(ENGINE))

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# osk의 훅 사건 → 두 A 호스트의 사건 이름. 엔진을 들이지 못한 진단에만 쓴다.
_EVENTS = {"start": "SessionStart", "input": "UserPromptSubmit", "stop": "Stop"}
# 이 호출을 부른 호스트와 무엇을 냈는지 — 아무것도 내지 않은 호출의 끝(`finish`)이 본다.
_OUT = {"host": None, "written": False}


def emit_raw(data: str) -> None:
    sys.stdout.buffer.write(data.encode("utf-8"))
    sys.stdout.buffer.flush()
    _OUT["written"] = True


def finish() -> None:
    """아무것도 내지 않은 호출의 끝 — JSON 결과만 받는 호스트(Antigravity)에는 빈 객체를 낸다."""
    host = _OUT["host"]
    if not _OUT["written"] and host is not None and host.silence:
        emit_raw(host.silence)


def normalize(env: dict) -> dict:
    """호스트마다 다른 훅 입력을 공통 키로(`osk.harness.normalize`) — 실패하면 그대로 둔다."""
    try:
        from osk import harness
        return harness.normalize(env)
    except Exception:
        return env


def emit_context(event: str, text: str, system_message: str = "", host=None) -> None:
    """문맥 주입 — `event`는 osk의 사건(`start`·`input`)이고 형식은 호스트 어댑터가
    정한다(`osk.harness`). 호스트를 모르면 두 A 호스트가 함께 받는 JSON 계약으로 낸다
    — `[osk …]` 평문은 Codex에서 JSON으로 오인된다. `system_message`는 모델 문맥이
    아니라 **사용자 화면**에 경고로 뜬다(최상위 `systemMessage`).

    엔진을 들이지 못해도 진단은 나가야 하므로, 그때는 같은 계약을 여기서 쓴다.
    평문을 그대로 받는 호스트(Kiro)의 어댑터는 문자열을 돌려준다."""
    try:
        from osk import harness
        output = (host or harness.FALLBACK).hook_output(event, text, system_message)
    except Exception:
        output = {"hookSpecificOutput": {"hookEventName": _EVENTS[event], "additionalContext": text}}
        if system_message:
            output["systemMessage"] = system_message
    emit_raw(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))


def note_run(event: str, env: dict | None, key: str | None):
    """이 기기에서 이 훅이 불렸다는 기록(`osk.harness.runs`) — `doctor`가 등록과 대조한다.
    훅을 부른 호스트를 돌려준다(출력 형식). 판별·기록 실패는 훅을 막지 않는다."""
    host = None
    try:
        from osk import harness
        from osk.harness import runs
        host = harness.host_of(env) if isinstance(env, dict) else None
        _OUT["host"] = host
        runs.record_run(host.name if host else None, event, key)
    except Exception:
        pass
    return host


def first_call(event: str, env: dict, host) -> bool:
    """같은 사건이 두 자리에 등록돼 두 번 불렸으면 두 번째 호출은 거짓이다 — 호출자는
    아무것도 내지 않고 끝낸다(`osk.harness.runs.first_call`). 판정 실패는 호출을 막지 않는다."""
    try:
        from osk.harness import runs
        return runs.first_call(host.name if host else None, event, env)
    except Exception:
        return True


# git 한 번의 제한 — 훅 제한(설치 30초, 손으로 등록한 기기 10초)보다 짧아야 git이 멈췄을 때
# 훅이 먼저 끊기지 않는다(#192).
GIT_TIMEOUT = 4
# 작업 폴더의 저장소 판정(`_checkout`)은 git을 두세 번 띄워 부하에서 턴마다 2초 안팎이었다(#192).
# 판정은 거의 바뀌지 않으므로 폴더별로 이만큼 다시 쓴다(`_checkout_cached`).
CHECKOUT_TTL = 3600
_SLOW = []    # 시간을 넘긴 git 호출 — 그 판정은 캐시하지 않는다


def _git(cwd: str, *args: str, input: str | None = None) -> str | None:
    """git의 표준 출력. 실패(비영 종료·시간 초과)는 None — 빈 출력과 구별한다."""
    io = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT, creationflags=_NO_WINDOW, **io)
    except subprocess.TimeoutExpired:
        _SLOW.append(args[0])
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _shas(path: Path) -> list[str]:
    """캐시의 해시 목록. 읽을 수 없거나 쓰다 끊겼으면 빈 목록이다 — 끊긴 캐시는
    남의 동일성이 되므로 다시 잰다."""
    try:
        got = path.read_text(encoding="ascii").split()
    except (OSError, UnicodeError):
        return []
    return got if all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", x) for x in got) else []


def _checkout(cwd: str) -> tuple[str, list[str] | None, Path | None]:
    """(저장소 이름, 저장소 동일성, 공통 디렉터리). 동일성은 저장소가 공유하는 이력 — 브랜치와
    원격 추적 브랜치 — 의 뿌리 커밋 합집합이다. 어느 브랜치를 체크아웃했는지와
    무관하고, 같은 저장소의 사본끼리는 기기가 달라도 겹치며, 네트워크가 필요 없다.
    기기에만 있는 stash·notes는 넣지 않는다. Git 밖·커밋 없는 저장소·얕은 사본은
    None이다(얕은 사본의 뿌리는 경계라 깊이에 따라 바뀐다).

    사본마다 공통 디렉터리에 뿌리와 이미 걸은 끝점을 캐시한다. 끝점이 바뀌면 새로
    온 커밋만 걸어 뿌리를 더하므로(뒤에 받은 orphan 브랜치 등), 훅이 매 턴 이력을
    다시 걷지 않는다. 뿌리는 더해지기만 한다."""
    try:
        out = (_git(cwd, "rev-parse", "--git-common-dir", "--is-shallow-repository")
               or "").splitlines()
        if len(out) != 2:
            return Path(cwd).name, None, None
        gd = Path(out[0])
        gd = (gd if gd.is_absolute() else Path(cwd) / gd).resolve()
        # `.git`(워크트리 포함)은 본 저장소 폴더, 서브모듈(`.git/modules/<이름>`)과
        # bare(`shop.git`)는 자기 이름이다 — `modules`·bare의 부모로 접히지 않게.
        name = gd.parent.name if gd.name.startswith(".") else gd.name.removesuffix(".git")
        if out[1] != "false":
            return name, None, gd
        cache, walked = gd / "osk-repo-identity", gd / "osk-repo-identity.tips"
        roots = _shas(cache)
        seen = _shas(walked) if roots else []      # 뿌리가 없으면 처음부터 다시 걷는다
        tips = _git(cwd, "for-each-ref", "--format=%(objectname)", "refs/heads", "refs/remotes")
        new = sorted(set((tips or "").split()) - set(seen))
        if tips is not None and new:
            # ponytail: 첫 계산은 이력 전체를 걷는다(초대형 저장소는 수 초) — 이후엔 새 커밋만.
            found = _git(cwd, "rev-list", "--max-parents=0", "--ignore-missing", "--stdin",
                         input="".join(f"{t}\n" for t in new) + "".join(f"^{s}\n" for s in seen))
            if found is not None:
                grown = sorted(set(roots) | set(found.split()))
                try:
                    if grown != roots:
                        cache.write_text("".join(f"{x}\n" for x in grown), encoding="ascii")
                    walked.write_text("".join(f"{x}\n" for x in sorted(set(tips.split()))),
                                      encoding="ascii")
                except OSError:
                    pass
                roots = grown
        return name, roots or None, gd
    except Exception:
        return Path(cwd).name, None, None


def _stamp(gd: Path | None) -> list | None:
    """저장소 동일성 캐시 파일의 (크기, 수정 시각) — 바뀌면 폴더별 판정도 다시 잰다."""
    try:
        st = (gd / "osk-repo-identity").stat()
        return [st.st_size, st.st_mtime_ns]
    except (OSError, TypeError):
        return None


def _dotgit(cwd: str) -> list | None:
    """작업 폴더에서 위로 가장 가까운 `.git` — 폴더면 경로만, 파일(워크트리·서브모듈)이면 그
    크기·시각까지. git을 띄우지 않고 저장소가 새로 생기거나 다른 것으로 바뀐 것을 알아챈다."""
    d = os.path.realpath(cwd)
    while True:
        p = os.path.join(d, ".git")
        if os.path.isdir(p):
            return [p]
        try:
            st = os.stat(p)
            return [p, st.st_size, st.st_mtime_ns]
        except OSError:
            pass
        if os.path.dirname(d) == d:
            return None
        d = os.path.dirname(d)


def _checkout_cached(cwd: str) -> tuple[str, list[str] | None]:
    """`_checkout`을 작업 폴더별로 CHECKOUT_TTL 동안 다시 쓴다(기기 로컬 `osk-checkouts.json`).
    다시 쓰는 판정은 둘이다 — 위로 `.git`이 없는 폴더(`.git`이 생기면 바뀐다)와, 가장 가까운
    `.git`과 저장소의 동일성 캐시가 그대로인 저장소. 미확정(커밋 없음·얕은 사본)은 다시 잰다
    — 첫 커밋 뒤에도 이름만 쓰면 같은 이름의 남의 결속을 받는다(#196 리뷰). git이 시간을
    넘기면 지난 판정이 있으면 그것을 쓰고, 그 판정은 캐시하지 않는다."""
    from osk import core
    path, folder, now = core.local_lock_path("osk-checkouts.json"), os.path.normcase(os.path.abspath(cwd)), time.time()
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        table = {}
    table = table if isinstance(table, dict) else {}
    hit = table.get(folder)
    if not (isinstance(hit, dict) and isinstance(hit.get("name"), str)
            and isinstance(hit.get("repo"), (list, type(None)))):
        hit = None
    gd = Path(hit["gd"]) if hit and isinstance(hit.get("gd"), str) else None
    dotgit = _dotgit(cwd)
    if (hit and 0 <= now - hit.get("at", 0) < CHECKOUT_TTL and hit.get("dotgit") == dotgit
            and (dotgit is None or hit["repo"] and hit.get("stamp") is not None
                 and _stamp(gd) == hit["stamp"])):
        return hit["name"], hit["repo"]
    slow = len(_SLOW)
    name, repo, gd = _checkout(cwd)
    if len(_SLOW) > slow:
        return (hit["name"], hit["repo"]) if hit else (name, repo)
    table = {k: v for k, v in table.items() if isinstance(v, dict) and 0 <= now - v.get("at", 0) < CHECKOUT_TTL}
    table[folder] = {"name": name, "repo": repo, "gd": str(gd) if gd else None, "stamp": _stamp(gd),
                     "dotgit": dotgit, "at": now}
    try:
        core.atomic_write(path, json.dumps(table, ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass        # 캐시를 못 적으면 다음 훅이 다시 잰다
    return name, repo


def session_key(cwd: str) -> str:
    """프로젝트 키를 정하는 **유일한** 자리 — 세 훅이 모두 이것을 부른다.

    이름만으로는 무관한 두 저장소가 한 키를 나눠 가지므로, Git 저장소는 결속의
    소유자와 뿌리가 다르면 파생 키를 받는다(`write.repo_session`)."""
    name, repo = _checkout_cached(cwd)
    try:
        from osk import workspaces
        workspaces.enter(cwd)        # 이 프로세스의 결속 판정이 그 작업 폴더를 알게 한다(Mechanism §6-2 6항)
    except Exception:
        pass
    if not repo:
        return name
    try:
        from osk import write
        return write.repo_session(name, repo)
    except Exception:
        return name


def _memory_line(st: dict, key: str, space: str | None = None) -> str:
    """전문 대신 싣는 한 줄 — 이 대화가 같은 해시의 전문을 이미 받았을 때(§9-2 5항의 뜻:
    전문은 호출자가 저장본을 모를 때 싣는다). 해시가 바뀌면 다시 전문이다. `space`는 주된
    scope 밖의 결속 scope일 때 준다 — 읽는 호출도 그 space를 함께 준다."""
    from osk.core import SCOPE
    arg = json.dumps(key, ensure_ascii=False) + (f", space={json.dumps(space, ensure_ascii=False)}" if space else "")
    return (f"[osk scope 기억 — {SCOPE}/{st['scope']} · {st['chars']}/{st['limit']}자 · "
            f"여유 {st['limit'] - st['chars']}자 · hash {st['hash'][:19]}…] 이 대화에 앞서 실은 전문과 "
            f"같다. 요약을 고치기 전에 `scope_memory(session={arg})`로 현재 저장본을 읽는다. 대화 검토 시에는 "
            "자기 대화의 raw와 공유 기억을 함께 검토하고, 오래 쓸 지식은 search로 찾은 기존 Scope 노드 "
            "갱신을 우선해 출처·허브를 완성한다. 남길 것이 없으면 사유를 남긴다.")


def _memory_block(scope_memory, key: str, st: dict | None = None) -> str:
    """scope 기억 전문 — 비었으면 빈 문자열."""
    from osk.core import SCOPE
    st = st or scope_memory.read(key)
    text = (st.get("text") or "").strip()
    if not text:
        return ""
    # 케이던스는 실행 방식별 안내가 맡는다. 이 공유 블록은 저장 경계와
    # `edits` 계약만 가르쳐 Stop 실행기에 user 턴 재촉을 겹쳐 싣지 않는다.
    #
    # 프로젝트 키도 싣는다. 도구의 `session`은 이 값이어야 하는데 훅만 알고
    # 호출자는 몰라서 매번 지어냈고, 그 결속은 append-only로 영구히 쌓였다.
    return (
        f"[osk scope 기억 — {SCOPE}/{st['scope']} · "
        f"{st['chars']}/{st['limit']}자 · 여유 {st['limit'] - st['chars']}자]\n"
        f"모든 세션·기기가 공유하는 기억이다 — 세션 한정 상태를 적지 말 것.\n"
        f"{st['session_note']}\n"
        f"대화 검토 시에는 자기 대화와 현재 공유 기억을 함께 검토하라. "
        f"오래 쓸 지식은 search로 찾은 기존 Scope 노드 갱신을 우선하고 출처·허브를 "
        f"완성한다. 요약에 머물 내용은 그 다음 scope 기억에 반영하고, 남길 것이 "
        f"없으면 사유를 남긴다. 요약 수정은 `edits`로 "
        f"`[{{old_text, new_text}}, …]`를 **다음 도구 호출에 함께** 실어라. "
        f"앵커는 아래 전문에서 그대로 복사하고(공백·줄바꿈까지, 마지막 줄엔 "
        f"개행이 없다) 해시는 필요 없다. 전문을 통째로 갈 때만 `text`와 "
        f"아래 `hash`를 쓴다.\n"
        f"hash: {st['hash']}\n---\n{text}")


def _bootstrap(key: str, *, bound: bool, ident: tuple | None = None, workspace: dict | None = None,
               landing: str | None = None, bindings: dict | None = None) -> str:
    arg = json.dumps(key, ensure_ascii=False)
    workspace = workspace or {}
    bindings = bindings or {}
    others = [s for s in bindings.get("scopes", []) if s != bindings.get("primary")]
    # `cite_round(conversation=…)` names this conversation by the pair below.
    conversation = (f" · conversation={json.dumps(ident[0] + '/' + ident[1], ensure_ascii=False)}"
                    if ident else "")
    if bound:
        landing_note = "아래 scope 기억을 통합의 출발점으로 삼는다."
        if others:
            # 한 프로젝트가 여러 scope의 활동을 품는다(시행령 §3 10항) — 기본 착지는 주된 scope다
            landing_note += (f" 이 프로젝트의 결속은 주된 scope {bindings['primary']}와 {', '.join(others)}다 — "
                             "다른 scope의 활동은 create_node의 space에 그 scope를 준다.")
    elif others:
        landing_note = (f"이 프로젝트의 결속은 {', '.join(others)}이지만 주된 scope가 없다"
                        + ("(결속이 갈려 미확정)" if bindings.get("undetermined") else "")
                        + " — 노드를 쓸 때마다 space를 준다. 이 프로젝트 자체의 활동이 속한 scope는 대화 "
                          "검토의 소속 판정(review의 primary_scope)이나 사용자(`routing primary`)가 지정한다.")
    else:
        landing_note = ("아직 scope 결속이 없다. 착지를 추측하지 말고 overview의 군집에서 해당 프로젝트를 "
                        "확인한 뒤 scope_memory를 읽어라.")
    return (f"[osk 세션 시작 — session={arg}{conversation}]\n"
            f"이 세션에서 노드를 처음 쓰기 전에 `overview(session={arg})`를 한 번 불러 군집·허브 트리(`hub_tree`)·열린 사건·근거 "
            "재검토 후보를 확인하라. 새 노드는 허브 트리에서 주제를 덮는 가장 깊은 군집에 둔다(시행령 §3 8항). "
            "기억을 묻는 질문에는 `search`를 먼저 쓴다. " + landing_note
            + (" 이 작업 폴더는 일회성 작업 폴더라 프로젝트 키를 결속하지 않고 그 키에 선 결속도 따르지 "
               "않는다(Mechanism §6-2 6항) — 쓰기 도구에 session을 주지 말고, 노드를 쓸 때 space를 "
               "명시한다. 그 착지는 이 대화에만 적용된다." if workspace.get("one_off") else "")
            + (f" 이 대화는 `{landing}`에 착지해 있다 — 쓸 때 space에 그 경로를 준다."
               if landing and not bound else "")
            + (f" 작업 폴더 판별: {workspace['warning']}" if workspace.get("warning") else ""))


def _budget(env: dict, host=None) -> int | None:
    """그 호스트가 받는 훅 문맥의 글자 예산(`Adapter.hook_budget`)."""
    from osk import harness
    return (host or harness.host_of(env) or harness.FALLBACK).hook_budget


def _conversation(st: dict, keep: int):
    """대화별 통합 대기 — 접히면 같은 지시를 CLI `integration prompt`의 text로 읽는다."""
    from osk import core
    from osk.hook_text import Block
    n = len(st.get("pending_refs") or [])
    command = core.cli_command("integration", "prompt", "--harness", st["harness"],
                               "--conversation", st["conversation_id"])
    return Block("conversation", st["text"], keep,
                 label=f"대화 검토 대기 {n}라운드 — 이 턴에 검토한다" if n else "대화 검토 상태",
                 pull=f"`{command}`의 text")


def _organization(jobs: list, keep: int):
    from osk import organization
    from osk.hook_text import Block
    if not jobs:
        return Block("organization", "")
    j = jobs[0]
    return Block("organization", organization.hook_prompt(jobs), keep,
                 label=f"조직 검토 scope {j['scope']} 남은 구간 {j['coverage']['remaining']}/{j['coverage']['total']}",
                 pull="overview:organization")


def _organization_jobs(key: str) -> list:
    """세션 시작의 조직 검토 — 포착 대기와 따로 고른다(짧은 대화도 시작에서 착수할 수 있다)."""
    from osk import core, organization, write
    scopes = write.bound_scopes(key)     # 주된 scope부터, 없으면 결속 순서대로(§9-3 1항)
    if not scopes:
        return []
    with core.mutation_lock():
        return organization.pending(scopes, limit=1, record=True)


def _bindings_block(key: str):
    """이 프로젝트의 결속 검토(시행령 §3 10항) — 검토 대상이 아니면 빈 블록이다."""
    from osk import organization
    from osk.hook_text import Block
    try:
        return Block("bindings", organization.bindings_prompt(key), 65,
                     label="프로젝트 결속 검토", pull="overview:organization")
    except Exception as exc:
        return Block("bindings", f"[osk 결속 검토 판독 진단 — {type(exc).__name__}: {exc}. 본 작업은 계속한다.]")


def _start_organization(key: str):
    from osk.hook_text import Block
    try:
        return _organization(_organization_jobs(key), 60)
    except Exception as exc:    # 조직 검토를 못 골라도 시작의 경고·나머지는 싣는다
        return Block("organization", f"[osk 참조·조직 검토 판독 진단 — {type(exc).__name__}: {exc}. "
                                     "본 작업은 계속한다.]")


def _recovery(scope_memory, key: str):
    from osk.hook_text import Block
    return Block("recovery", scope_memory.recovery_block(key), 90,
                 label="scope 복구 대기", pull="overview:recovery")


def _memory(scope_memory, key: str, ident, *, gate: bool, keep: int, space: str | None = None):
    """scope 기억 블록. `gate`면 이 대화가 같은 해시의 전문을 이미 받았을 때 한 줄로 싣고,
    전문이 실제로 실렸을 때만 그 해시를 적는다(`integration.note_memory`). `space`는 주된 scope가
    아닌 결속 scope의 기억을 실을 때 준다(Mechanism §9-3 1항)."""
    from osk import integration
    from osk.hook_text import Block
    st = scope_memory.read(key, space)
    full = _memory_block(scope_memory, key, st)
    name = "memory" if space is None else f"memory:{st['scope']}"
    if not full:
        return Block(name, "")
    # 한 줄로 실어도 접힐 수 있다 — 그때도 접은 줄이 무엇을 어디서 읽을지 말해야 한다(Kiro 실측).
    arg = f"session={json.dumps(key, ensure_ascii=False)}" + (f", space={json.dumps(space, ensure_ascii=False)}" if space else "")
    pull = f"`scope_memory({arg})`"
    seen_as = st["scope"] if space else None     # 주된 scope 밖의 결속 scope는 제 해시를 따로 기억한다
    if gate and ident and integration.memory_seen(*ident, st["hash"], seen_as):
        return Block(name, _memory_line(st, key, space), keep, label="scope 기억(이 대화에 실은 전문과 같다)", pull=pull)
    return Block(name, full, keep, label=f"scope 기억 전문 {st['scope']}(여유 {st['limit'] - st['chars']}자)", pull=pull,
                 on_shown=(lambda: integration.note_memory(*ident, st["hash"], seen_as)) if ident else None)


def _bound_memories(scope_memory, key: str, ident, bindings: dict, *, keep: int, gate: bool = False):
    """주된 scope가 아닌 결속 scope들의 기억 — 주된 scope 다음에, 한도 안에서 싣는다(§9-3 1항)."""
    from osk.core import SCOPE
    from osk.hook_text import Block
    out = []
    for sc in bindings.get("scopes", []):
        if sc == bindings.get("primary"):
            continue
        try:
            out.append(_memory(scope_memory, key, ident, gate=gate, keep=keep, space=f"{SCOPE}/{sc}"))
        except Exception as exc:
            out.append(Block(f"memory:{sc}", f"[osk 공유 기억 판독 진단 — {sc}: {type(exc).__name__}: {exc}]"))
    return out


def _once(ident, text: str, view: dict, startup: bool) -> bool:
    """이어지는 대기·진단을 실을지 — 같은 글은 처음 한 번만이고 나머지는 `integration status`가
    보인다(#192). 시작은 새 문맥이라 다시 싣는다. 포착 중이 아니고 대기가 풀렸으면 잊는다."""
    from osk import integration
    shown = integration.notice_once(*ident, text, settled=not view.get("capture_pending"))
    return bool(text) and (shown or startup)


def capture_blocks(env: dict, key: str, *, startup: bool = False):
    """이 대화의 포착·검토 조각 — `([Block], (harness, 대화 ID) | None)`. 조립과 예산은
    호출자가 한다(`osk.hook_text`). None이면 이 대화의 사건이 아니다.

    훅은 전사를 읽지 않고 구독 CLI도 묻지 않는다 — 둘 다 부하에서 수 초라 훅 제한을
    넘겼다(#192). 저장된 커서로 세고, 새 턴의 포착과 CLI 판정은 분리 작업자가 한다
    (`response_growth.defer`). 세션 시작은 대화의 착지만 고정하고(`integration.hook_land`)
    마지막 포착 때의 대기를 싣는다 — 그 뒤의 턴은 작업자가 포착해 다음 입력부터 보인다."""
    from osk import integration, response_growth
    from osk.hook_text import Block
    ident = None
    try:
        if startup:
            captured = integration.hook_land(env, key)
            harness, sid, cadence = captured["harness"], captured["conversation_id"], None
        else:
            harness, sid, _ = integration.hook_source(env)
            # Always retain the input clock, even while Stop owns background scheduling.
            # Losing CLI auth must not start the fallback's overdue work at zero again.
            captured = cadence = integration.tick(harness, sid)
        ident = (harness, sid)
        selected = response_growth.route(env)
        try:
            response_growth.defer(env, key, probe_due=selected['probe_due'])
        except (OSError, ValueError):
            pass        # 다음 훅이나 Stop의 포착이 다시 넘긴다
        # 새 대화의 전사는 첫 턴 뒤에야 생긴다 — 그 사이의 '없다'는 진단이 아니다
        standing = captured['capture_error'] if captured['capture_recovery']['state'] != 'awaiting_input' else None
        if selected['mode'] in ('background', 'off'):
            # The fork's failure count's test (`_review_done`, kept as `reviewed`): a run that
            # finished this conversation's review but deferred its organization job is no wait (#228).
            failures = [r for r in (captured.get('response_growth_stop'), captured.get('response_growth', {}).get('last_result'))
                        if selected['mode'] == 'background' and r and not r.get('ok', True)
                        and not r.get('reviewed') and r.get('state') not in {'running', 'unavailable'}]
            error = standing or '; '.join(r.get('error') or r['state'] for r in failures)
            if _once(ident, error, captured, startup):
                return [Block("route", f"[osk 백그라운드 검토 대기 — {error}; 본 작업은 계속한다. 완료로 처리하지 않았다.]")], ident
            if selected['mode'] == 'off':
                # 끈 하네스: fork도 세션 안 9·15턴 검토도 없다. 포착과 대기는 그대로다.
                return [Block("route", f"[osk 대화 검토 꺼짐 — {selected['reason']}. 이 대화는 끝난 뒤 "
                                       "정기 성장 실행이 통합한다.]" if startup or selected['changed'] else "")], ident
            if selected['changed'] and not startup:
                return [Block("route", "[osk 검토 경로 복구 — 구독 CLI를 확인했다. 기존 계수·검토 대기를 유지하고 최종 답변 Stop 기준 실행으로 돌아간다.]")], ident
            return [Block("route", "[osk 대화 검토 — 최종 답변 Stop 9회마다 원대화와 같은 하네스·모델의 "
                                   "구독 fork가 자기 대화를 검토한다. 실행 결과는 integration status에서 확인한다.]"
                          if startup else "")], ident
        warning = Block("warning", f"[osk 검토 경고 — {selected['reason']}. 별도 fork 대신 이 세션에서 "
                                   "UserPromptSubmit 기준 9·15턴 통합을 수행한다. 검토 대기와 계수는 유지한다.]")
        if startup:
            if not captured["pending"]:
                return [warning, _start_organization(key)], ident
            st = integration.prompt(harness, sid, organization_in_text=False)
            return [warning, _conversation(st, 70), _organization(st["organization_jobs"], 60)], ident
        overdue_switch = selected['changed'] and cadence['unreviewed_prompts'] >= integration.SOFT
        if not cadence["due"] and not overdue_switch:
            # A stuck capture has no raw to review: one line once, not the whole review
            # every turn. The wait and the count stay (§9-3 1항).
            blocked = (f"[osk 포착 대기 — {standing}. 이 대화의 검토 대기와 계수는 "
                       "유지하고 9·15턴에는 상태와 함께 싣는다. 본 작업은 계속한다; 완료로 처리하지 "
                       "않았다. 처음 보면 사용자에게 한 번 알린다.]"
                       if _once(ident, standing or "", captured, startup) else "")
            return [warning if selected['changed'] else Block("warning", ""), Block("blocked", blocked)], ident
        lead = Block("lead", f"[osk 케이던스 — user 턴 {cadence['unreviewed_prompts']}] "
                     + ("이번엔 단독 턴이어도 된다. " if cadence["hard"] or
                        overdue_switch and cadence['unreviewed_prompts'] >= integration.HARD else
                        "다음 도구 호출에 함께 실어 검토하라 — 검토만을 위한 턴을 따로 쓰지 마라. "))
        st = integration.prompt(harness, sid, organization_in_text=False)
        blocks = [warning, lead, _conversation(st, 95), _organization(st["organization_jobs"], 60)]
    except integration.SubagentEvent:
        return None  # Not this subagent's conversation: no capture, route or root review text.
    except Exception as exc:
        blocks = [Block("diagnostic", f"[osk 포착·통합 진단 — {type(exc).__name__}: {exc}. 본 작업은 계속한다; 대기를 완료로 처리하지 않았다.]")]
    # Native capture failures must not hide an independently readable scope's
    # recovery instructions. SessionStart already emits shared memory below.
    if not startup:
        try:
            from osk import scope_memory, write
            bindings = write.bindings(key)
            if bindings["primary"]:
                recovery = _recovery(scope_memory, key)
                # 복구는 현재 엔트리를 정리하는 일이라 앵커를 베낄 전문이 그 자리에 있어야 한다
                # — 복구 대기 중에는 해시가 같아도 전문을 싣는다.
                blocks += [recovery, _memory(scope_memory, key, ident, gate=not recovery.text, keep=85)]
            # 주된 scope가 아닌 결속 scope의 기억도 — 주된 scope 다음에, 한도 안에서(§9-3 1항)
            blocks += _bound_memories(scope_memory, key, ident, bindings, gate=True, keep=75)
        except Exception as exc:
            blocks.append(Block("memory", f"[osk 공유 기억 판독 진단 — {type(exc).__name__}: {exc}]"))
    return blocks, ident


def capture_block(env: dict, key: str, *, startup: bool = False, budget: int | None = None) -> str | None:
    """입력 훅의 문맥 — 호스트의 예산 안에 블록 단위로 조립한다(`osk.hook_text`)."""
    from osk.hook_text import assemble
    found = capture_blocks(env, key, startup=startup)
    if found is None:
        return None
    text, _ = assemble(found[0], budget if budget is not None else _budget(env), session=key)
    return text


def _recheck_note(rechecks) -> str:
    """근거 재검토에서 본 세션이 알아야 할 것 — 사용자 검토를 기다리는 수정, 그리고
    정기 실행이 없을 때 쌓이는 후보(그 처리는 대화 검토 fork가 이 scope 몫을 맡는다).
    둘 다 없으면 아무것도 싣지 않는다."""
    try:
        from osk import growth
        status = growth.daily_status()
        daily = status["active"]
        if daily and not any(r.get("kind") == "recheck_review" for r in growth._records()):
            return ""
        items, pending = rechecks.candidates()
    except Exception as exc:
        return f"[osk 근거 재검토 판독 진단 — {type(exc).__name__}: {exc}]"
    if pending:
        return ""
    held = sum("escalated" in i for i in items)
    notes = []
    if held:
        notes.append(f"[osk 근거 재검토 — 사용자 검토 대기 {held}건. 재검토로 고친 수정이 그 "
                     "노드를 인용한 노드들까지 고치게 만든다. overview의 rechecks.escalated에서 "
                     "수정안을 보고 정한다.]")
    if not daily and len(items) > held and status["ran"]:
        # 돌았지만 선택 작업이 남았다 — 여러 번에 나눠 끝나는 정돈이 남는 것은 흔하다.
        # '돌지 않았다'고 쓰면 매일 도는 실행을 다시 켜라는 안내가 된다.
        from datetime import datetime
        at = datetime.fromisoformat(status["last"]["at"]).astimezone().strftime("%m-%d %H:%M")
        notes.append(f"[osk 근거 재검토 — 후보 {len(items) - held}건. 최신 정기 실행({at})이 선택 "
                     "작업을 다 끝내지 못해 대화 검토(fork)가 이 scope의 후보를 함께 맡는다. 본 "
                     "작업은 계속한다.]")
    elif not daily and len(items) > held:
        notes.append(f"[osk 근거 재검토 — 후보 {len(items) - held}건. 정기 실행이 최근 3일 안에 "
                     "돌지 않아 대화 검토(fork)가 이 scope의 후보를 맡는다. Domain의 후보는 정기 "
                     "실행이 맡으니 SETUP의 'Scope에서 Domain으로 정기 재검토'로 켠다. 본 작업은 "
                     "계속한다.]")
    return "\n".join(notes)


def _release_notice(host=None) -> tuple[str, str, object]:
    """(에이전트 문맥, 사용자 화면, 블록이 실렸을 때 부를 것) — 새 릴리스를 이 기기에서
    아직 알리지 않았을 때만. 확인은 기다리지 않는다: 지난 확인이 낡았으면 분리 프로세스로
    띄우고, 이번 세션에는 지난 결과만 싣는다(`osk.update_check`). 사용자 화면이 없는
    호스트는 블록이 실렸을 때만 알린 것으로 적는다."""
    try:
        from osk import update_check
        update_check.ensure_fresh()
        screen = host is None or host.screen
        agent, user = update_check.session_notice(screen=screen)
        return agent, user, (None if screen or not agent else update_check.mark_notified)
    except Exception as exc:
        return f"[osk 릴리스 확인 진단 — {type(exc).__name__}: {exc}; 본 작업은 계속한다.]", "", None


def main() -> None:
    if os.environ.get("OSK_GROWTH_WORKER") == "1":
        return  # maintenance evidence belongs to its run, not a new integration queue
    try:
        env = json.load(sys.stdin)
        if not isinstance(env, dict):
            raise ValueError("hook input must be a JSON object")
    except Exception as exc:
        note_run("start", None, None)
        emit_context("start", f"[osk 훅 입력 판독 진단 — {type(exc).__name__}: {exc}; 본 작업은 계속한다.]")
        return
    env = normalize(env)
    cwd = env.get("cwd") or os.getcwd()

    host = None
    try:
        from osk import scope_memory, write, evictions, rechecks
        key = session_key(cwd)
        host = note_run("start", env, key)
        if not first_call("start", env, host):
            return
        try:
            from osk import core
            # 대장 행도 작업 트리의 변경이다 — 판독부터 쓰기까지 변경 잠금 안에서 한다(정기 실행과 같다).
            # 그 사이 다른 세션의 노드 쓰기가 끼면 낡은 판독으로 이어 적어 방금 닫은 쌍을 다시 연다.
            with core.mutation_lock():
                rechecks.ensure_baseline(carry=True)
        except Exception:
            pass    # 다음 쓰기가 다시 적는다 — 못 적으면 근거가 후보로 남을 뿐이다
        from osk.hook_text import Block, assemble
        recheck = Block("recheck", _recheck_note(rechecks), 50, label="근거 재검토 안내(overview의 rechecks)")
        found = capture_blocks(env, key, startup=True)
        if found is None:
            return
        captured, ident = found
        notice, shown, notified = _release_notice(host)
        notice = Block("notice", notice, 40, label="새 릴리스 알림(overview의 update)",
                       on_shown=notified)
        try:
            bindings = write.bindings(key)
        except Exception:
            bindings = {"scopes": [], "primary": None, "undetermined": False}
        scope = bindings["primary"]
        try:
            from osk import integration
            seen = integration.status(*ident) if ident else {}
        except Exception:
            seen = {}           # 판별을 못 읽어도 시작 안내는 싣는다
        bootstrap = Block("bootstrap", _bootstrap(key, bound=bool(scope), ident=ident, workspace=seen.get("workspace"),
                                                  landing=seen.get("space"), bindings=bindings))
        try:
            recovery = _recovery(scope_memory, key)
        except Exception:
            recovery = Block("recovery", "[osk scope 복구 표식을 읽지 못했다 — CLI status로 확인하라]")
        # 주된 scope가 아닌 결속 scope의 기억도 싣는다 — 주된 scope 다음, 한도 안에서(§9-3 1항)
        others = _bound_memories(scope_memory, key, ident, bindings, keep=75)
        if not scope:
            blocks = [bootstrap, notice, recheck, recovery, *others, *captured, _bindings_block(key)]
        else:
            try:
                mem = _memory(scope_memory, key, ident, gate=False, keep=95)
            except Exception as exc:
                mem = Block("memory", f"[osk 공유 기억 판독 진단 — {type(exc).__name__}: {exc}]")
            banner, tidy = Block("banner", ""), Block("tidy", "")
            try:
                parts = evictions.hook_parts(scope, sys.executable, str(ENGINE))
                banner = Block("banner", parts["banner"])
                tidy = Block("tidy", parts["block"], 80, label=parts["label"], pull="overview:tidy")
            except Exception as exc:
                tidy = Block("tidy", f"[osk 정돈 판독 진단 — {type(exc).__name__}: {exc}]")
            # 순서가 조문이다(§9-3 3항) — 밀림 경고가 맨 앞, 기억, 정돈 블록. 넘치면 블록째 접는다(1항).
            blocks = [banner, bootstrap, notice, recheck, recovery, mem, *others, *captured, tidy,
                      _bindings_block(key)]
        out, _ = assemble(blocks, _budget(env, host), session=key)
        if not out:
            return
        emit_context("start", out, shown, host)
    except Exception as exc:
        emit_context("start", f"[osk 세션 시작 진단 — {type(exc).__name__}: {exc}; 본 작업은 계속한다.]",
                     host=host)


if __name__ == "__main__":
    main()
    finish()
