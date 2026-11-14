"""회귀 수트의 한 갈래: 프로젝트 키 — 작업 폴더에서 정하는 키와 저장소의 정체.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import json, os, subprocess, sys, tempfile
from pathlib import Path
from unittest import mock

from regression.support import check, ENGINE, ROOT, RUN_TMP, VAULT, _w
from osk import core, routing, write  # noqa: E402


# ── 21. 1회용 대화 id는 프로젝트 키가 아니다 (Mechanism §6-2 6항) ──────────
def test_ephemeral_session_key():
    """프로젝트 키는 훅이 작업 폴더에서 정한다. 대화마다 새로 나는 값을 키 자리에
    넣으면 아무도 그 키로 다시 오지 않아 대장에 죽은 행만 남는다. 도구 설명이
    금지하고 있었으나 설명은 강제가 아니었고, 실측으로 두 건이 굳었다(2026-08-24).
    세 쓰기 표면 모두에서 막고, **막는 자리는 파일 쓰기 앞**이어야 한다 — 결속은
    뒤에 오는 경로가 있어 뒤에서 막으면 파일만 남는다."""
    from osk import raw, scope_memory as sm
    U = "2df63e9c-b6fd-4499-8d2a-f53c4921e243"          # 실측된 그 형태
    U32 = "2df63e9cb6fd44998d2af53c4921e243"            # 하이픈 없는 UUID
    before = VAULT.routing.read_text(encoding="utf-8") if VAULT.routing.exists() else ""

    r = _w(write.create_node, VAULT, "1회용 키 시험", "s", "본문", "fable-5",
           session=U, space="00_Scope/W1")
    check("create_node가 UUID 세션을 거부", r.get("ok") is False, r)
    v = " ".join(r.get("violations", []))
    check("거부가 1회용 id임을 지목", "1회용 대화 id" in v, v)
    check("거부가 대안을 알려준다", "작업 폴더에서 정한" in v, v)
    check("거부는 노드를 남기지 않는다",
          not (ROOT / "00_Scope/W1/1회용 키 시험.md").exists())

    check("하이픈 없는 UUID도 거부",
          _w(write.create_node, VAULT, "1회용 키 시험2", "s", "본문", "fable-5",
             session=U32, space="00_Scope/W1").get("ok") is False)

    rr = _w(raw.append_rounds, VAULT, U, "rec", [{"user": "q", "agent": "a"}],
            space="00_Scope/W1")
    check("인용 기록 쓰기도 거부", rr.get("ok") is False, rr)
    check("raw 거부는 원인을 프로젝트 키로 지목",
          "1회용 대화 id" in " ".join(rr.get("violations", [])), rr)

    rs = _w(sm.replace, VAULT, U, "- 엔트리", None, "00_Scope/W1")
    check("scope_memory도 거부", rs.get("ok") is False, rs)

    after = VAULT.routing.read_text(encoding="utf-8") if VAULT.routing.exists() else ""
    check("거부는 라우팅 대장에 결속을 남기지 않는다", before == after)

    # 실제 세션 키는 그대로 통과해야 한다 — 과잉 거부는 이 관문의 실패다.
    for good in ("open-hwp", "rhwp", "aw2", "lpaiu-cs/ltm-vault",
                 "causal-spacetime", "illustratorAI"):
        check(f"실제 키 `{good}`는 통과", write.ephemeral_session_errors(good) == [])
    check("빈 세션은 이 관문이 관여하지 않는다",
          write.ephemeral_session_errors(None) == [])


def test_session_key_repo_identity():
    """폴더 이름이 같은 **무관한** 저장소는 한 결속을 나누지 않는다.

    v3.22까지 키는 폴더 이름뿐이라 `a/api`가 세운 결속을 `b/api`가 이어받아 남의
    기억을 주입받고 자기 대화를 남의 `_raw`에 포착했다. 서브모듈은 모두
    `modules`, bare 저장소의 워크트리는 bare의 부모 이름을 받았다."""
    import importlib, uuid
    sys.path.insert(0, str(ENGINE / "scripts/hooks"))
    hook = importlib.import_module("claude_session_start")
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="osk-repokey-", dir=RUN_TMP)))
    g = lambda *a, cwd=None: subprocess.run(
        ["git", "-c", "user.name=x", "-c", "user.email=x@x.invalid",
         "-c", "protocol.file.allow=always", *a],
        cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)

    def repo(path, msg):
        path.mkdir(parents=True)
        g("init", "-q", str(path))
        g("commit", "-q", "--allow-empty", "-m", msg, cwd=path)
        return g("rev-parse", "HEAD", cwd=path).stdout.decode().strip()

    a, b, loose = base / "a/api", base / "b/api", base / "c/Downloads"
    ra, rb = repo(a, "alpha 뿌리"), repo(b, "beta 뿌리")
    loose.mkdir(parents=True)
    VAULT.routing.unlink(missing_ok=True)
    rows = lambda: [r for r in core.ledger_read(VAULT.routing) if r.get("session") == "api"]

    check("Git 밖 폴더는 전과 같이 폴더 이름", hook.session_key(VAULT, str(loose)) == "Downloads")
    check("결속 전에는 두 저장소 모두 폴더 이름이고 아무것도 쓰지 않는다",
          hook.session_key(VAULT, str(a)) == hook.session_key(VAULT, str(b)) == "api" and not rows())
    check("동일성은 사본마다 캐시된다",
          (a / ".git/osk-repo-identity").is_file()
          and (a / ".git/osk-repo-identity").read_text(encoding="ascii").split() == [ra])
    write.bind_session(VAULT, "api", "W1")                  # 표면의 첫 쓰기 — 동일성 모름
    held, real_append = [], write.ledger_append

    def spy(*args, **kw):
        # 소유 행도 working-tree 변경이다 — 데몬의 commit→rebase와 같은 잠금을 잡는다.
        with open(core.mutation_lock_path(VAULT), "w") as fh:
            try:
                core.lock_exclusive(fh, blocking=False)
                core.unlock(fh)
                held.append(False)
            except OSError:
                held.append(True)
        return real_append(*args, **kw)
    with mock.patch.object(routing, "ledger_append", spy):
        check("무소유 결속은 처음 쓰는 저장소가 소유한다", hook.session_key(VAULT, str(a)) == "api")
    check("소유 행은 변경 잠금 안에서 쓴다", held == [True], held)
    check("소유는 한 행", [r.get("repo") for r in rows()] == [None, [ra]], rows())
    hook.session_key(VAULT, str(a))
    check("소유 뒤 재사용은 쓰지 않는다", len(rows()) == 2)
    kb = hook.session_key(VAULT, str(b))
    check("무관한 같은 이름 저장소는 파생 키", kb == f"api-{rb[:8]}", kb)
    check("파생 키는 남의 결속을 받지 않는다", write.resolve_session(VAULT, kb) is None)
    check("비켜 선 저장소는 행을 쓰지 않는다", len(rows()) == 2)
    far = base / "device2/api"
    g("clone", "-q", str(a), str(far))
    check("같은 저장소의 다른 자리(다른 기기)는 같은 키·같은 결속",
          hook.session_key(VAULT, str(far)) == "api" and len(rows()) == 2
          and write.resolve_session(VAULT, "api") == "W1")
    cache = a / ".git/osk-repo-identity"
    cache.write_text(ra[:10], encoding="ascii")     # 쓰다 끊긴 캐시
    check("잘린 동일성 캐시는 다시 재어 제 키를 지킨다",
          hook.session_key(VAULT, str(a)) == "api" and cache.read_text(encoding="ascii").split() == [ra]
          and len(rows()) == 2, cache.read_text(encoding="ascii"))

    # 훅 전 경로 — SessionStart 주입과 UserPromptSubmit 포착이 남의 scope에 닿지 않는다.
    from osk import scope_memory as sm
    marker = "ALPHA-ONLY-" + uuid.uuid4().hex[:8]
    memory = ROOT / "00_Scope/Workbench/_scope_memory/W1.md"
    prior = memory.read_bytes() if memory.exists() else None
    sm.replace(VAULT, "api", f"- {marker}", sm.read(VAULT, "api")["hash"])
    home = base / "home"
    (home / ".claude").mkdir(parents=True)
    env = {**os.environ, "OSK_VAULT_ROOT": str(ROOT), "PYTHONPATH": str(ENGINE),
           "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex")}
    sid = str(uuid.uuid4())
    tpath = home / f"{sid}.jsonl"
    secret = "BETA-PRIVATE-" + uuid.uuid4().hex[:8]
    tpath.write_text("".join(json.dumps(x) + "\n" for x in [
        {"type": "user", "sessionId": sid, "uuid": "u-1", "isSidechain": False,
         "message": {"role": "user", "content": secret}},
        {"type": "assistant", "sessionId": sid, "uuid": "a-1", "isSidechain": False,
         "message": {"role": "assistant", "id": "m-1", "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": "답"}]}}]), encoding="utf-8")

    def run(script, event, cwd, conversation=sid):
        p = {"cwd": str(cwd), "session_id": conversation, "harness": "claude", "source": "startup",
             "transcript_path": str(tpath), "hook_event_name": event, "prompt": "다음"}
        out = subprocess.run([sys.executable, str(ENGINE / "scripts/hooks" / script)],
                             input=json.dumps(p).encode(), capture_output=True, env=env,
                             cwd=str(cwd), timeout=120)
        return out.stdout.decode("utf-8", "replace")

    started = run("claude_session_start.py", "SessionStart", b)
    check("b의 SessionStart는 a의 기억을 주입하지 않고 파생 키를 알린다",
          marker not in started and f'session=\\"{kb}\\"' in started, started[:600])
    check("a의 SessionStart는 제 기억을 받는다",
          marker in run("claude_session_start.py", "SessionStart", a, str(uuid.uuid4())))
    run("claude_prompt_submit.py", "UserPromptSubmit", b)
    leaked = [p for p in (ROOT / "00_Scope/W1").rglob("*")
              if p.is_file() and secret in p.read_text(encoding="utf-8", errors="ignore")]
    check("b의 대화는 a의 scope에 포착되지 않는다", not leaked, leaked)
    g("commit", "-q", "--allow-empty", "-m", "둘째", cwd=a)
    shallow = base / "shallow/api"
    g("clone", "-q", "--depth", "1", a.as_uri(), str(shallow))
    check("얕은 사본은 동일성이 없어 전과 같이 이름만 쓴다",
          hook.session_key(VAULT, str(shallow)) == "api" and len(rows()) == 2
          and not (shallow / ".git/osk-repo-identity").exists())
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    write.bind_session(VAULT, kb, "W2")
    check("파생 키는 제 결속으로 간다",
          hook.session_key(VAULT, str(b)) == kb and write.resolve_session(VAULT, kb) == "W2")

    # 폴더별 캐시(#192)는 미확정 판정을 다시 쓰지 않는다 — Git 밖이던 폴더가 저장소가 되거나
    # 저장소 안에 저장소가 새로 생기면 다음 훅이 제 키를 받는다(#196 리뷰).
    head = lambda p: g("rev-parse", "HEAD", cwd=p).stdout.decode().strip()
    late = base / "late/api"
    late.mkdir(parents=True)
    hook.session_key(VAULT, str(late))                     # Git 밖 판정이 캐시된다
    g("init", "-q", str(late))
    hook.session_key(VAULT, str(late))                     # 커밋 없는 저장소 — 동일성 미확정
    g("commit", "-q", "--allow-empty", "-m", "늦은 뿌리", cwd=late)
    got = hook.session_key(VAULT, str(late))
    check("Git 밖이던 폴더는 첫 커밋 뒤 바로 제 키 — 남이 소유한 이름의 결속을 받지 않는다",
          got == f"api-{head(late)[:8]}", got)
    (late / ".git").rename(late / ".git-old")
    g("init", "-q", str(late))
    g("commit", "-q", "--allow-empty", "-m", "바뀐 뿌리", cwd=late)
    got = hook.session_key(VAULT, str(late))
    check("같은 자리의 저장소를 바꾸면 새 동일성", got == f"api-{head(late)[:8]}", got)
    mono = base / "mono/app"
    repo(mono, "모노 뿌리")
    inner = mono / "pkg"
    inner.mkdir()
    check("저장소 안 하위 폴더는 바깥 저장소의 키", hook.session_key(VAULT, str(inner)) == "app")
    g("init", "-q", str(inner))
    g("commit", "-q", "--allow-empty", "-m", "안쪽 뿌리", cwd=inner)
    got = hook.session_key(VAULT, str(inner))
    check("그 안에 새로 생긴 저장소는 자기 키", got == "pkg", got)

    # 서브모듈과 bare 저장소의 워크트리는 자기 이름을 받는다(`modules`·부모 아님).
    lib = base / "seed/lib"
    repo(lib, "lib 뿌리")
    for sup in ("sa", "sb"):
        repo(base / sup, f"{sup} 뿌리")
        g("submodule", "add", "-q", str(lib), f"vendor-{sup}", cwd=base / sup)
    for name in ("shop", "blog"):
        seed = base / "seed" / name
        repo(seed, f"{name} 뿌리")
        g("clone", "-q", "--bare", str(seed), str(base / "src" / f"{name}.git"))
        g("worktree", "add", "-q", str(base / "src" / name / "main"),
          cwd=base / "src" / f"{name}.git")
    got = [hook.session_key(VAULT, str(p)) for p in (base / "sa/vendor-sa", base / "sb/vendor-sb",
                                              base / "src/shop/main", base / "src/blog/main")]
    check("서브모듈·bare 워크트리는 자기 저장소 이름", got == ["vendor-sa", "vendor-sb", "shop", "blog"], got)

    # 두 기기의 동시 소유 — 병합(union)이 무소유 결속 아래 두 소유 행을 남긴다.
    VAULT.routing.unlink(missing_ok=True)
    legacy = write.bind_session(VAULT, "api", "W1")
    ms = core.rid_parts(legacy["rid"])[0]

    def claim(rid_ms, who):
        with open(VAULT.routing, "a", encoding="utf-8") as f:
            f.write(json.dumps({"rid": core._make_rid(rid_ms, 0), "parents": [legacy["rid"]],
                                "kind": "bind", "session": "api", "scope": "W1",
                                "repo": [who], "at": core.now_iso()}) + "\n")
    claim(ms + 20, rb)          # 파일 순서가 아닌 rid가 소유를 가린다
    claim(ms + 10, ra)
    check("같은 scope의 분기는 미확정이 아니다 (v5.2)", write.resolve_session(VAULT, "api") == "W1")
    order = [hook.session_key(VAULT, str(b)), hook.session_key(VAULT, str(a)), hook.session_key(VAULT, str(b))]
    check("rid가 작은 소유가 이기고 순서와 무관하다", order == [kb, "api", kb], order)
    check("소유자가 한 행으로 봉합한다",
          write.resolve_session(VAULT, "api") == "W1" and len(rows()) == 4, rows())
    # 같은 저장소를 두 기기가 동시에 소유한 분기도 봉합된다.
    VAULT.routing.unlink(missing_ok=True)
    legacy = write.bind_session(VAULT, "api", "W1")
    ms = core.rid_parts(legacy["rid"])[0]
    claim(ms + 10, ra)
    claim(ms + 20, ra)
    check("같은 저장소의 동시 소유도 풀린다",
          hook.session_key(VAULT, str(far)) == "api" and write.resolve_session(VAULT, "api") == "W1")
    # 구판 기기의 판독 — 새 필드는 손상도 판정 변화도 아니다.
    check("repo 필드는 대장 손상이 아니다",
          core.ledger_damage(core.ledger_read(VAULT.routing)) == [])
    VAULT.routing.unlink(missing_ok=True)
    if prior is None:
        memory.unlink(missing_ok=True)
    else:
        memory.write_bytes(prior)


def test_session_key_identity_ignores_checkout():
    """저장소 동일성은 처음 훅이 돈 브랜치의 뿌리가 아니다.

    main과 orphan `gh-pages`가 있는 저장소에서 한 사본은 `gh-pages`에서, 다른
    사본은 main에서 처음 훅을 돌려도 둘은 같은 저장소다. 한 브랜치만 받은 사본이
    뒤에 다른 브랜치를 받으면 캐시가 그 뿌리를 더한다."""
    import importlib
    sys.path.insert(0, str(ENGINE / "scripts/hooks"))
    hook = importlib.import_module("claude_session_start")
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="osk-repoid-", dir=RUN_TMP)))
    g = lambda *a, cwd=None: subprocess.run(
        ["git", "-c", "user.name=x", "-c", "user.email=x@x.invalid", *a],
        cwd=cwd, check=True, capture_output=True, stdin=subprocess.DEVNULL)
    head = lambda p: g("rev-parse", "HEAD", cwd=p).stdout.decode().strip()
    origin = base / "origin/site"
    origin.mkdir(parents=True)
    g("init", "-q", "-b", "main", str(origin))
    g("commit", "-q", "--allow-empty", "-m", "본문 뿌리", cwd=origin)
    r_main = head(origin)
    g("checkout", "-q", "--orphan", "gh-pages", cwd=origin)
    g("commit", "-q", "--allow-empty", "-m", "페이지 뿌리", cwd=origin)
    r_pages = head(origin)
    g("checkout", "-q", "main", cwd=origin)
    both = sorted([r_main, r_pages])

    a, b, c = base / "devA/site", base / "devB/site", base / "devC/site"
    g("clone", "-q", str(origin), str(a))
    g("checkout", "-q", "gh-pages", cwd=a)          # A는 gh-pages에서 처음 훅을 돌린다
    g("clone", "-q", str(origin), str(b))           # B는 main에서
    VAULT.routing.unlink(missing_ok=True)
    rows = lambda: [r for r in core.ledger_read(VAULT.routing) if r.get("session") == "site"]
    ia, ib = hook._checkout(str(a))[1], hook._checkout(str(b))[1]
    check("동일성은 체크아웃한 브랜치와 무관한 뿌리 합집합이다", ia == ib == both, (ia, ib))
    write.bind_session(VAULT, "site", "W1")
    check("gh-pages에서 처음 돈 사본이 소유한다",
          hook.session_key(VAULT, str(a)) == "site" and [r.get("repo") for r in rows()] == [None, both],
          rows())
    check("main에서 도는 다른 사본도 같은 키·같은 결속",
          hook.session_key(VAULT, str(b)) == "site" and write.resolve_session(VAULT, "site") == "W1"
          and len(rows()) == 2, rows())

    g("clone", "-q", "--single-branch", "-b", "main", str(origin), str(c))
    check("한 브랜치만 받은 사본은 그 뿌리만 안다", hook._checkout(str(c))[1] == [r_main])
    check("그래도 겹치므로 같은 키다", hook.session_key(VAULT, str(c)) == "site" and len(rows()) == 2)
    g("fetch", "-q", "origin", "gh-pages:refs/remotes/origin/gh-pages", cwd=c)
    check("뒤에 받은 orphan 브랜치의 뿌리가 캐시에 더해진다",
          hook._checkout(str(c))[1] == both
          and (c / ".git/osk-repo-identity").read_text(encoding="ascii").split() == both)

    (b / "scratch.txt").write_text("임시", encoding="utf-8")
    g("stash", "-q", "-u", cwd=b)                   # -u의 미추적 커밋은 부모가 없는 뿌리다
    check("기기에만 있는 stash의 뿌리는 동일성에 들지 않는다", hook._checkout(str(b))[1] == both)
    g("commit", "-q", "--allow-empty", "-m", "다음", cwd=b)
    check("새 커밋은 뿌리를 바꾸지 않고 걸은 끝점만 옮긴다",
          hook._checkout(str(b))[1] == both
          and head(b) in (b / ".git/osk-repo-identity.tips").read_text(encoding="ascii").split())
    VAULT.routing.unlink(missing_ok=True)


def test_derived_session_key_is_stable():
    """배정된 파생 키는 뿌리가 더해져도 옮겨지지 않는다.

    이름을 남이 소유한 저장소는 가장 작은 뿌리로 파생 키를 받는다. 그 뒤 해시가 더
    작은 orphan 브랜치를 받아 뿌리가 늘어도, 이미 결속된 파생 키를 계속 쓴다."""
    other, m, g = "a" * 40, "f2c23286" + "0" * 32, "64cad3f2" + "0" * 32
    grown = sorted([g, m])
    VAULT.routing.unlink(missing_ok=True)
    rows = lambda k: [r for r in core.ledger_read(VAULT.routing) if r.get("session") == k]
    write.bind_session(VAULT, "site", "W1")
    check("다른 저장소가 이름을 소유한다",
          write.repo_session(VAULT, "site", [other]) == "site" and rows("site")[-1].get("repo") == [other])
    k1 = write.repo_session(VAULT, "site", [m])
    check("이름이 남의 것이면 가장 작은 뿌리의 파생 키", k1 == "site-f2c23286", k1)
    (ROOT / "00_Scope/W2").mkdir(exist_ok=True)
    write.bind_session(VAULT, k1, "W2")                     # 표면의 첫 쓰기 — 동일성 모름
    got = write.repo_session(VAULT, "site", grown)
    check("소유 기록 전에 뿌리가 늘어도 이미 결속된 파생 키를 쓴다", got == k1, got)
    check("그 파생 키를 이 저장소가 소유한다",
          [r.get("repo") for r in rows(k1)] == [None, grown], rows(k1))
    check("뿌리가 는 뒤 다시 불러도 같은 키이고 행을 더 쓰지 않는다",
          write.repo_session(VAULT, "site", grown) == k1 and len(rows(k1)) == 2)
    check("한 뿌리만 아는 다른 사본도 같은 파생 키", write.repo_session(VAULT, "site", [m]) == k1)
    check("결속은 그대로다", write.resolve_session(VAULT, k1) == "W2")
    check("새로 가장 작아진 뿌리의 키는 생기지 않는다", not rows("site-64cad3f2"))

    # 소유 행을 못 적어도(읽기 전용 대장·공유 위반) 판정한 파생 키를 쓴다.
    VAULT.routing.unlink(missing_ok=True)
    write.bind_session(VAULT, "site", "W1")
    write.repo_session(VAULT, "site", [other])
    kb = f"site-{m[:8]}"
    write.bind_session(VAULT, kb, "W2")                     # 표면의 첫 쓰기 — 소유 기록 전
    before = VAULT.routing.read_bytes()
    with mock.patch.object(routing, "ledger_append", side_effect=PermissionError("읽기 전용")):
        try:
            got = write.repo_session(VAULT, "site", [m])
        except Exception as e:
            got = e
    check("소유 행을 못 적어도 판정한 파생 키를 쓴다 — 남이 소유한 이름으로 물러서지 않는다",
          got == kb, got)
    check("못 적은 행은 남지 않는다", VAULT.routing.read_bytes() == before)
    check("다음 호출이 소유를 적는다",
          write.repo_session(VAULT, "site", [m]) == kb and [r.get("repo") for r in rows(kb)] == [None, [m]],
          rows(kb))
    VAULT.routing.unlink(missing_ok=True)
