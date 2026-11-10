"""osk.routing — 프로젝트 키와 scope의 결속.

구현 근거: Mechanism §6-2 6항(프로젝트 라우팅)·시행령 §3 10항(프로젝트 결속). 결속은
`_ledger/routing.jsonl`의 `bind`·`unbind`·`alias` 기록을 인과 순서로 접은 결과다.
노드 쓰기, scope 기억, 인용 기록, 훅이 같은 판정을 쓴다.
"""
from __future__ import annotations

import re

from . import graph, workspaces
from .core import (PERSON, RID_RE, ROUTING, SCOPE, _rid_key, causal_maxima, ledger_append, ledger_damage,
                   ledger_read, mutation_lock, resolve_one)
from .errors import WriteError

ALIAS_DEPTH = 8          # 별칭 사슬 추적 한계 — 순환·장난에 대한 보수적 상한

# 1회용 대화 id의 형태 — UUID(하이픈 유무 무관). 실제 프로젝트 키(`open-hwp`,
# `rhwp`, `lpaiu-cs/ltm-vault` 꼴)와는 겹치지 않는다.
EPHEMERAL_SESSION_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{32})$", re.I)


def ephemeral_session_errors(session: str | None) -> list[str]:
    """1회용 대화 id는 프로젝트 키가 아니다 (Mechanism §6-2 6항) — 형식 검사다.

    프로젝트 키는 훅이 작업 폴더에서 정한다. 대화마다 새로 나는 값을 키 자리에
    넣으면 그 대화가 끝나는 순간 아무도 그 키로 다시 오지 않아 대장에 죽은 행만
    쌓인다. 도구 설명만으로는 막히지 않는 것이 실측됐다(2026-08-24 관측:
    `= Scope/Arel-Wars-2`·`= Scope/gh-hint`). 형식이 어긋난 `space`를 조용히 버리지
    않는 `resolve_landing`의 규율과 같은 이유로 여기서도 거부한다.

    쓰기 경로의 **가장 앞**에서 부른다. 결속은 파일 쓰기 뒤에 오는 경로가
    있어(create_node) 거기서 막으면 부분 성공이 된다."""
    if not session or not EPHEMERAL_SESSION_RE.match(session.strip()):
        return []
    return [f"`{session}`은 1회용 대화 id다 — 프로젝트 키가 아니다. 키는 훅이 작업 "
            f"폴더에서 정한 값(`open-hwp`·`rhwp` 꼴)이며, 대화 id를 결속하면 아무도 그 "
            f"키로 다시 오지 않아 대장에 죽은 행만 남는다."]


def routing_errors(session: str | None) -> list[str]:
    """결속을 세우기 **전에** 라우팅 대장이 판독·기록 가능한지 본다.

    `resolve_session`은 판독 실패를 `None`으로 삼켜 "미결속"으로 진행한다. 그래서
    손상된 대장 위에서도 쓰기가 끝까지 가고, 마지막의 `bind_session`이
    `ledger_append`의 손상 거부에 걸려 죽는다 — 표면은 "쓰지 않았다"를 보고하는데
    파일은 남는 **부분 성공**이다(Mechanism §6-2 3항 위반, 실측 재현). 손상은
    언제나 외부 기원이므로(수동 복구·부분 병합) 여기서 fail-closed로 막는다.

    `ephemeral_session_errors`와 같은 자리에서 부른다 — 잠금 앞이든 안이든
    **첫 바이트를 쓰기 전**이면 된다."""
    if not session:
        return []                       # 결속을 세울 일이 없다
    try:
        recs = ledger_read(ROUTING)
    except Exception as e:
        return [f"세션 라우팅 대장을 읽지 못했다: {e} — 이 상태에서 쓰면 결속만 "
                f"실패하고 파일은 남는다. Mechanism §3 8항의 수동 복구가 먼저다"]
    dmg = ledger_damage(recs, ROUTING)
    if dmg:
        return ["세션 라우팅 대장이 손상됐다: " + "; ".join(dmg[:3])
                + " — 손상 위에 이력을 더 쌓지 않는다(Mechanism §3 2항). "
                  "수동 복구가 먼저다"]
    return []


def canonical_session(session: str | None,
                      recs: list[dict] | None = None) -> str | None:
    """프로젝트 키를 **정본 키**로 접는다 (Mechanism §6-2 6항).

    저장소·서버의 개명은 흔한 일이고(이 체계도 llm-vault → ltm-vault →
    osk-system을 거쳤다), 그때마다 세션이 다른 scope로 흩어지면 라우팅이
    이름의 역사에 인질이 된다. 별칭 기록(`kind: "alias"`)이 구 이름을 정본
    이름으로 접어 **어느 이름으로 들어오든 한 scope로 모이게** 한다.

    별칭도 인과 극대가 정본이다 — 분기·순환이면 접지 않고 원래 키를 쓴다."""
    if not session:
        return None
    if recs is None:
        try:
            recs = ledger_read(ROUTING)
        except Exception:
            return session
    seen = {session}
    cur = session
    for _ in range(ALIAS_DEPTH):
        r = resolve_one(recs, cur, "session")
        if not r or r.get("kind") != "alias":
            return cur
        nxt = r.get("canonical")
        if not nxt or nxt in seen:
            return session      # 순환은 접지 않는다 — 원래 입력 키로 남는다
        seen.add(nxt)
        cur = nxt
    return cur


def bindings(session: str | None, recs: list[dict] | None = None) -> dict:
    """프로젝트 키의 결속 집합 — `{"scopes": [...], "primary": str | None, "undetermined": bool}`
    (Mechanism §6-2 6항 · 시행령 §3 10항).

    결속은 집합이다. 그 키의 `bind`·`unbind` 기록을 rid 순으로 접는다 — `bind`는
    더하고 `unbind`는 뺀다. 주된 scope는 `primary`를 표시한 `bind`가 정한다. **v5.2
    이전의 기록에는 `primary`가 없다** — 그 시절 키는 scope 하나에만 결속됐고 그것이
    곧 저장소 자체의 활동이었으므로 주된 scope로 읽는다. 그 시절의 재결속은 **교체**였으므로
    (새 인과 극대가 새 결속) 옛 기록은 집합을 그 scope 하나로 바꾼다 — 더하면 개명 전 scope가
    결속에 되살아난다(`LLM-Second-Brain` → `osk-system` 실측). 새 기록은 `primary`를 늘
    적는다(집합에만 더하는 쓰기는 False). 인과 극대가 갈리고 그 주된 scope가 다르면
    미확정이다 — 착지를 명시하게 한다. 일회성 작업 폴더의 키는 결속하지 않는다."""
    out = {"scopes": [], "primary": None, "undetermined": False}
    if not session or workspaces.is_one_off(session):
        return out
    if recs is None:
        try:
            recs = ledger_read(ROUTING)
        except Exception:
            return out
    key = canonical_session(session, recs)
    rows = [r for r in recs if r.get("session") == key and r.get("kind") in ("bind", "unbind")
            and r.get("scope")]
    rows.sort(key=lambda r: _rid_key(r["rid"]) if re.match(RID_RE, str(r.get("rid"))) else (0, 0, ""))
    scopes, primary = [], None
    for r in rows:
        sc = str(r["scope"])
        if r["kind"] == "bind":
            if "primary" not in r:              # v5.2 이전 기록 — 재결속은 교체였다
                scopes = []
            if sc not in scopes:
                scopes.append(sc)
            if r.get("primary", True):
                primary = sc
        else:
            if sc in scopes:
                scopes.remove(sc)
            if primary == sc:
                primary = None
    # 미확정은 **지정 기록만으로** 판정한다 — 집합에만 더하는 결속은 지정 사이의 분기를 해소할
    # 권한이 없다. 극대를 모든 결속 기록에서 구하면 분기 뒤의 일반 결속이 유일 극대가 되어
    # 어느 쪽도 고르지 않은 채 rid 순서가 주된 scope를 정해 버린다(#191 리뷰).
    heads = {str(r.get("scope")) for r in causal_maxima(
        recs, key, field="session",
        candidate=lambda r: r.get("kind") == "bind" and r.get("scope") and r.get("primary", True))}
    if len(heads) > 1:
        out["undetermined"], primary = True, None
    out.update(scopes=scopes, primary=primary)
    return out


def resolve_session(session: str | None) -> str | None:
    """프로젝트 키 → 주된 scope(기본 착지). 없거나 미확정이면 None — fail-closed."""
    return bindings(session)["primary"]


def bound_scopes(session: str | None) -> list[str]:
    """결속 scope를 주된 scope부터, 없으면 결속 순서대로(Mechanism §9-3 1항) — 조직 검토와
    기억 주입이 scope를 고르는 순서다."""
    b = bindings(session)
    return [s for s in b["scopes"] if s == b["primary"]] + [s for s in b["scopes"] if s != b["primary"]]


def bind_session(session: str, scope: str, reason: str = "", *,
                 primary: bool = True) -> dict:
    """프로젝트→scope 결속 기록. `primary=True`(기본)는 주된 scope의 지정(표시 기록)이고,
    `primary=False`는 집합에만 더하는 결속이다 — 쓰기 경로는 언제나 후자다. 쓰기는 주된
    scope를 정하지 않는다(Mechanism §6-2 6항). 별칭은 정본 키로 접어 기록하므로 구 이름으로
    들어와도 한 자리에 모인다. 재결속도 **새 행 append**다(append-only)."""
    # 키는 `strip()`한 값으로 적는다 — `ephemeral_session_errors`가 이미
    # 다듬은 값으로 검사하므로, 여기서 원문을 쓰면 `" open-hwp"`가 `open-hwp`와
    # 다른 결속으로 앉아 다음 세션이 그 기억에 닿지 못한다.
    session = (session or "").strip()
    return ledger_append(ROUTING, {
        "kind": "bind", "session": canonical_session(session) or session,
        "scope": scope, "primary": bool(primary), "reason": reason or "최초 작업에서 확정"})


def unbind_session(session: str, scope: str, reason: str = "") -> dict:
    """결속 해제 기록 — 집합에서 뺀다. 주된 scope였으면 주된 scope도 비운다."""
    session = (session or "").strip()
    return ledger_append(ROUTING, {
        "kind": "unbind", "session": canonical_session(session) or session,
        "scope": scope, "reason": reason or "결속 해제"})


def set_primary(session: str, scope: str, reason: str = "") -> dict:
    """주된 scope의 지정 — 그 프로젝트 자체의 활동이 속한 scope로 확인됐을 때만 부른다
    (대화 검토의 소속 판정이 결정 묶음으로 내어 실행기가 적용하거나 사용자가 한다 —
    Mechanism §6-2 6항). 지정은 결속도 겸한다."""
    if scope not in graph.scope_names():
        raise WriteError("없는 scope — 지정하지 않았다",
                         [f"`{scope}`는 scope가 아니다. 가능한 space: {graph.space_list()}"])
    return bind_session(session, scope, reason or "주된 scope 지정", primary=True)


def bind_after_write(result: dict, session: str, scope: str, reason: str = "") -> bool:
    """파일을 **쓴 뒤**의 결속 — 집합에 없는 scope만 더하고 주된 scope는 정하지 않는다
    (Mechanism §6-2 6항). 실패를 쓰기 실패로 올리지 않고 영수증으로 남긴다.

    올리면 표면은 `ok:false`를 내는데 파일은 서 있다 — 호출자는 잃은 줄 알고
    재시도하고, 그 재시도는 이름 충돌·중복 라운드로 거부된다(v4 감사 실측:
    공유 위반으로 라우팅 대장 append가 실패한 경우). 대장 append는 기록된 뒤에도
    (fsync) 실패할 수 있으므로 "미기록"이 아니라 "확인 불가"다 —
    `evictions._after_node_write`의 처분 영수증과 같은 규율. 일회성 작업 폴더의 키는
    결속하지 않는다 — 그 착지는 그 대화에만 적용된다(Mechanism §6-2 6항)."""
    if workspaces.is_one_off(session):
        result["binding"] = {
            "state": "not_bound", "scope": scope,
            "note": "일회성 작업 폴더의 키라 결속하지 않았다 — 다음 쓰기에도 space를 준다"}
        return False
    if scope in bindings(session)["scopes"]:
        return False                            # 이미 결속에 있다 — 새 결속이 아니다
    try:
        bind_session(session, scope, reason or "착지를 명시한 쓰기가 결속에 더함", primary=False)
        return True
    except Exception as e:
        result["binding"] = {
            "state": "unconfirmed", "scope": scope, "error": f"{type(e).__name__}: {e}",
            "note": (f"쓴 것은 저장됐다 — 다시 보내지 마라. 프로젝트 결속은 확인하지 "
                     f"못했다: 다음 쓰기에 같은 session과 space `{SCOPE}/{scope}`를 "
                     f"함께 주면 결속에 더해진다.")}
        return False


def alias_session(alt: str, canonical: str, reason: str = "") -> dict:
    """구 이름 → 정본 이름 별칭. 개명 이력을 대장에 남기는 일이며 MCP 표면에
    노출하지 않는다 — 이름의 정본을 정하는 것은 사용자의 일이다."""
    return ledger_append(ROUTING, {
        "kind": "alias", "session": alt, "canonical": canonical,
        "reason": reason or "개명 이력"})


def _repo_owner(recs: list[dict], key: str) -> dict | None:
    """그 키의 **첫 소유 기록** — `repo`를 적은 결속 중 rid가 가장 작은 것.
    한 번 선 소유는 뒤의 행(구판이 쓴 무소유 재결속 포함)이 바꾸지 않는다."""
    rows = [r for r in recs if r.get("session") == key and r.get("kind") == "bind"
            and isinstance(r.get("repo"), list) and re.match(RID_RE, str(r.get("rid")))]
    return min(rows, key=lambda r: _rid_key(r["rid"]), default=None)


def _derived_key(recs: list[dict], name: str, repo: list[str]) -> str:
    """이름을 남이 소유할 때 이 저장소가 쓰는 파생 키 `<이름>-<뿌리 앞 8자>`.

    배정된 파생 키는 옮겨지지 않는다 — 뒤에 받은 브랜치가 뿌리를 더해 가장 작은
    뿌리가 바뀌어도 그대로다. 그래서 ① 내 뿌리와 겹치는 소유자가 있는 파생 키,
    ② 소유 기록 전이라도 내 뿌리로 만든 파생 키 중 이미 결속된 것, ③ 둘 다
    없을 때만 가장 작은 뿌리로 만든 새 키 순으로 고른다."""
    mine, pat = set(repo), re.compile(re.escape(name) + r"-[0-9a-f]{8}")
    owned = {}
    for k in {r.get("session") for r in recs if pat.fullmatch(str(r.get("session", "")))}:
        o = _repo_owner(recs, k)
        if o and set(o["repo"]) & mine:
            owned[k] = _rid_key(o["rid"])
    if owned:
        return min(owned, key=owned.get)
    bound = {r.get("session") for r in recs if r.get("kind") == "bind"}
    for root in repo:                                   # 정렬된 뿌리 — 어느 기기에서나 같은 순서
        k = f"{name}-{root[:8]}"
        if k in bound and _repo_owner(recs, k) is None:
            return k
    return f"{name}-{repo[0][:8]}"


def _repo_claim(recs: list[dict], name: str, repo: list[str]) -> tuple[str, dict | None]:
    """(이 저장소의 프로젝트 키, 새로 적을 결속 행 또는 None)."""
    key = canonical_session(name, recs) or name
    owner, out = _repo_owner(recs, key), name
    if owner and not set(owner["repo"]) & set(repo):
        out = key = _derived_key(recs, name, repo)      # 남의 키 — 파생 키로 비켜 선다
        owner = _repo_owner(recs, key)
        if owner and not set(owner["repo"]) & set(repo):
            return out, None                            # 파생 키마저 남의 것(앞 8자 충돌)
    maxima = causal_maxima(recs, key, field="session")
    scopes = {r.get("scope") for r in maxima if r.get("kind") == "bind"}
    # 무소유 결속은 소유하고(첫 사용 신뢰), 내 소유 키가 같은 scope로 분기했으면
    # (두 기기의 동시 소유) 봉합한다. scope가 갈린 분기는 지금처럼 미확정으로 둔다.
    if (len(scopes) == 1 and all(r.get("kind") == "bind" for r in maxima)
            and (owner is None or len(maxima) > 1)):
        # 소유 행은 결속을 되풀이할 뿐 주된 scope를 바꾸지 않는다 — 극대의 표시를 그대로 잇는다
        return out, {"kind": "bind", "session": key, "scope": scopes.pop(),
                     "primary": all(r.get("primary", True) for r in maxima),
                     "repo": repo, "reason": "저장소 동일성 기록"}
    return out, None


def repo_session(name: str, repo: list[str]) -> str:
    """저장소 폴더 이름 + 저장소 동일성(정렬된 뿌리 커밋) → 그 저장소의 프로젝트 키.

    폴더 이름만 키로 쓰면 무관한 두 저장소(`C:/a/api`·`D:/b/api`)가 한 결속을
    나눠 가져 서로의 기억을 주입받고 서로의 대화를 포착했다. 결속 행에 `repo`를
    적고, 그 키의 첫 소유자와 뿌리가 겹치지 않는 저장소는 파생 키
    `<이름>-<뿌리 앞 8자>`를 쓴다 — 뿌리는 어느 기기·사본에서나 같으므로 파생
    키도 같다. 두 기기의 동시 소유는 병합 뒤 rid가 작은 쪽이 소유자다.

    쓰기는 판정과 같은 잠금 안에서 다시 확인한다(`ledger_append`의 `expect`).
    대장 행도 working-tree 변경이므로 `mutation_lock` 안에서 쓴다 — 데몬의
    commit→rebase 사이에 끼면 rebase가 멈추거나 `--abort`가 행을 지운다."""
    key, row = _repo_claim(ledger_read(ROUTING), name, repo)
    if row:
        def same(recs):
            k, r = _repo_claim(recs, name, repo)
            return None if k == key and r and r["scope"] == row["scope"] else "소유 판정이 바뀌었다"
        try:
            with mutation_lock():
                ledger_append(ROUTING, row, expect=same)
        except ValueError:                          # 판정이 그사이 바뀌었다 — 다시 판정한다
            try:
                key = _repo_claim(ledger_read(ROUTING), name, repo)[0]
            except Exception:
                pass
        except Exception:
            # 소유 행을 못 적어도(읽기 전용 대장·공유 위반·낡은 엔진) 판정한 키를
            # 쓴다 — 행은 다음 훅이 다시 적는다. 원래 이름으로 물러서면 남이 소유한
            # 이름의 기억이 주입된다.
            pass
    return key


def resolve_landing(session: str, space: str | None,
                    confine_note: str) -> tuple[str | None, str | None]:
    """프로젝트 키·space → 착지 scope. `(scope, primary)`를 돌려준다.

    scope별 구획을 갖는 표면(scope 기억)이 같은 판정을 쓴다. 규율은 둘이고 **순서가
    있다** — 형식 → 실재.

    1. 형식이 어긋난 `space`를 **조용히 버리지 않는다.** 버리면 안 준 것과 같아져
       호출부가 "결속이 없다"고 엉뚱한 원인을 지목하고, 받는 쪽은 결속이 멀쩡한데도
       키를 바꿔가며 헤맨다(감사에서 실측된 오진).
    2. 없는 scope는 유효 목록과 함께 거부한다.

    결속에 없는 scope를 명시한 쓰기는 거부하지 않는다 — 한 프로젝트가 여러 scope의
    활동을 품는다(Mechanism §6-2 6항 · 시행령 §3 10항). `confine_note`는 호환 인자다.
    주된 scope가 없고 `space`도 없으면 `(None, None)` — 호출부가 fail-closed로 다룬다."""
    bound = resolve_session(session)
    if not space:
        return bound, bound
    scope = graph.scope_of_space(space)
    if not scope:
        raise WriteError(
            "space 표기 아님 — 쓰지 않았다",
            [f"`{space}`는 space 표기가 아니다 — `{SCOPE}/<이름>` **두 마디**로 "
             f"준다. `overview`의 `clusters`에는 `{PERSON}/…`이나 더 깊은 경로도 "
             f"섞여 있으니 그대로 옮기지 마라. 가능한 space: {graph.space_list()}"])
    if scope not in graph.scope_names():
        raise WriteError(
            "없는 scope — 쓰지 않았다",
            [f"`{space}`는 scope가 아니다. 가능한 space: {graph.space_list()}"])
    return scope, bound
