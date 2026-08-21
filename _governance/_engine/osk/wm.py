"""osk.wm — 작업 기억 (working memory).

구현 근거: Workbench 계약 2.1(작업 상태 — 노드가 아닌 임시 데이터, 작업 종료 시
보존 판별), 헌법 9조 3항("일회적 작업 상태는 Workbench 계약에 맡기고" + 보존
판별의 기준), 시행령 §11 1항(파일이 정본).

**상한은 저장 용량의 제한이 아니라 승격의 문턱이다.** 상한이 없으면 작업 기억이
자라고, 자라는 작업 기억은 노드를 만들 이유를 없앤다 — 필요한 것이 이미 거기 다
있기 때문이다. 그러면 지식은 영원히 주변부에 머물고 헌법 2조("연결을 통해
중심부로 자라난다")가 작동하지 않는다. 넘치는 순간이 곧 "이건 이 세션의 것이
아니다"라는 신호이고, 그 신호가 노드화를 부른다.

그래서 초과는 **거부한다.** 자동 절단·자동 요약을 두면 그 신호가 조용히 소비되어
아무 일도 일어나지 않는다. 에러 자체가 통합 트리거이므로 별도 요약 파이프라인이
없고, 통합할 때마다 자리값 못하는 엔트리가 퇴출되므로 decay 스케줄러도 없다.
"""
from __future__ import annotations
import os
from pathlib import Path

from .core import ROOT, mutation_lock, posix_rel, sha256_bytes
from . import graph, secrets, write

# 계수는 mechanism이 정한다(시행령 서문). 한 세션의 배울 점을 적기에는 충분하고
# 두 세션어치를 쌓아두기에는 모자란 크기여야 압력이 선다.
LIMIT = 1500

_CONFINE = ("작업 기억은 scope당 하나이므로 한 세션의 것을 다른 scope로 "
            "번지게 하지 않는다 —")

# 이 문장이 응답에 늘 실린다. 호출자의 기본 성향은 "지우면 안 된다"라서, 퇴출이
# 정상임을 말해 주지 않으면 쌓기만 하고 상한이 압력이 아니라 벽이 된다.
EVICTION_NOTE = ("엔트리는 자리를 다툰다. 자리값을 못하는 엔트리는 지운다 — "
                 "지우는 것이 정상이며 유실이 아니다. 정리로도 모자라면 그때 "
                 "기존 노드를 갱신하거나 새 노드로 증류하고 근거를 배선한다. "
                 "순서가 반대면 자리값 못하는 것까지 노드가 된다.")


def wm_dir() -> Path:
    return ROOT / "= Scope" / "Workbench" / "_wm"


def wm_path(scope: str) -> Path:
    """`= Scope/Workbench/_wm/<scope>.md`. scope의 지도가 scope 밖에 사는 것은
    scope 디렉토리를 노드만으로 깔끔히 두기 위해서이고, 접근이 어차피 엔진을
    지나므로 물리 자리가 사용성을 좌우하지 않는다."""
    return wm_dir() / f"{scope}.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _state(scope: str, text: str, **extra) -> dict:
    """성공·실패와 무관하게 **늘 같은 모양**을 돌려준다 — 전문과 잔여가 매 응답에
    실려야 호출자가 넘치기 전에 스스로 정리한다. 보이지 않는 것은 통합되지 않는다."""
    return {"scope": scope, "path": posix_rel(wm_path(scope), ROOT),
            "text": text, "chars": len(text), "limit": LIMIT,
            "remaining": LIMIT - len(text),
            "hash": sha256_bytes(text.encode("utf-8")),
            "eviction": EVICTION_NOTE, **extra}


def _landing(session: str, space: str | None) -> tuple[str, str | None]:
    """`(scope, bound)` — 첫 쓰기가 결속을 세워야 하므로 결속 여부도 함께 낸다."""
    scope, bound = write.resolve_landing(session, space, _CONFINE)
    if not scope:
        raise write.WriteError(
            "착지 미정 — 아무것도 하지 않았다",
            [f"세션 `{session}`의 scope 결속이 없다. `space`를 주면 그 자리의 "
             f"작업 기억을 쓴다. 가능한 space: {graph.space_list()}"])
    if scope not in graph.scope_names():
        raise write.WriteError(
            "결속이 가리키는 scope가 없다 — 아무것도 하지 않았다",
            [f"세션 `{session}`은 `= Scope/{scope}`에 결속돼 있으나 그 scope가 "
             f"없다. 가능한 space: {graph.space_list()}"])
    return scope, bound


def read(session: str, space: str | None = None) -> dict:
    """그 scope의 작업 기억 전문. 결속이 없고 `space`도 없으면 거부한다."""
    scope, _ = _landing(session, space)     # 읽기는 결속을 세우지 않는다
    return {"ok": True, **_state(scope, _read(wm_path(scope)))}


def replace(session: str, text: str, expect_hash: str | None = None,
            space: str | None = None) -> dict:
    """작업 기억을 **전체 치환**한다.

    부분 추가 API를 두지 않는 이유: 계약이 "전문을 돌려주고 통합 후 재시도"이므로
    호출자는 언제나 전문을 손에 쥐고 온다. 부분 추가를 열면 상한에 닿는 쪽이
    엔진이 되고, 무엇을 버릴지는 엔진이 알 수 없다.

    `expect_hash`는 기존 내용이 있을 때 필수다(Mechanism §6-2 4항의 규율) —
    아래 pull이 다른 기기의 통합을 끌어올 수 있으므로 보지 않은 상태를 덮는 일이
    실제로 일어난다."""
    with mutation_lock():
        scope, bound = _landing(session, space)
        p = wm_path(scope)

        # pull이 상한 판정보다 **앞선다.** 그러지 않으면 각 기기가 자기 사본
        # 기준으로 상한을 지키고 합치면 두 배가 된다. 다만 최선 노력이다 —
        # 오프라인에서 못 쓰게 만드는 것보다, 뒤늦은 충돌을 통합 신호로 받는 편이
        # 이 설계와 일관된다(충돌 처리도 초과와 같은 통합 요구다).
        sync = _pull()
        cur = _read(p)

        if cur and expect_hash is None:
            raise write.WriteError(
                "expect_hash 없음 — 쓰지 않았다",
                ["기존 내용이 있다. 지금 상태를 읽고 그 `hash`를 `expect_hash`로 "
                 "함께 보내라 — 보지 않은 상태를 덮지 않기 위해서다."],
                **_state(scope, cur, sync=sync))
        cur_hash = sha256_bytes(cur.encode("utf-8"))
        if cur and expect_hash != cur_hash:
            raise write.WriteError(
                "상태가 어긋났다 — 쓰지 않았다",
                [f"`expect_hash`가 현재 상태와 다르다. 다른 기기의 통합이 "
                 f"들어왔을 수 있다 — 아래 전문 위에서 다시 통합하라."],
                **_state(scope, cur, sync=sync))

        # 비밀값 필터의 적용 지점이 여기다. 전사는 vault 밖이라 필터가 닿지
        # 않지만, 작업 기억은 vault 안이고 에이전트가 쓴다 — 요약에 섞이면
        # 그대로 커밋된다.
        filtered, hits = secrets.filter_text(text)
        filtered = filtered.strip() + ("\n" if filtered.strip() else "")

        if len(filtered) > LIMIT:
            raise write.WriteError(
                "상한 초과 — 쓰지 않았다",
                [f"{len(filtered)}자로 상한 {LIMIT}자를 {len(filtered) - LIMIT}자 "
                 f"넘는다. **순서대로** 하라 — (1) 작업 기억에서 자리값 못하는 "
                 f"엔트리를 먼저 정리하라. (2) 그래도 모자라면, 남길 값어치가 "
                 f"있는 것을 **기존 노드에 갱신**하거나 새 노드로 증류하고 "
                 f"**근거를 배선하라**(착지는 `= Scope/{scope}`). 그 뒤 남은 "
                 f"것으로 다시 보내라."],
                **_state(scope, cur, sync=sync, rejected_chars=len(filtered)))

        p.parent.mkdir(parents=True, exist_ok=True)
        write._atomic_write(p, filtered.encode("utf-8"))
        # 첫 쓰기가 결속을 세운다 — `raw`와 같은 규율이다. 안 그러면 다음
        # 호출이 `space` 없이는 착지를 못 찾아 전부 막힌다(실측).
        if not bound:
            write.bind_session(session, scope, "첫 작업 기억 쓰기에서 확정")
        sync = {**sync, **_push(scope)}
        return {"ok": True, **_state(scope, filtered, sync=sync,
                                     filtered=sorted(set(hits)))}


# ── 동기화 (최선 노력) ───────────────────────────────────────────────────
# 작업 기억은 md라 `_ledger/*.jsonl`의 union merge 규칙이 걸리지 않는다. 그래서
# 다기기 동시 편집의 git 충돌은 남는다 — 그 충돌은 상한 초과와 같은 처리를
# 받는다(전문을 돌려주고 통합을 요구한다). 충돌이 곧 통합 신호다.

def _sync_ready() -> bool:
    """동기화는 **쓰는 사람만 쓰는 편의 모듈**이다(`core.local_lock_path` 각서).
    그래서 데몬과 같은 열쇠(`SYNC_ENABLED`)로 잠근다 — 켜지 않은 사람의 저장소에서
    작업 기억 쓰기가 `git add -A` + push를 일으키면, 그 사람이 손대던 다른 변경까지
    함께 커밋된다. 켜지 않았으면 아무것도 하지 않는다."""
    if os.environ.get("SYNC_ENABLED", "").lower() not in ("1", "true", "yes"):
        return False
    import vault_sync as vs          # 엔진 루트 모듈 (osk 패키지 밖) — update.py와 같은 형태
    return vs.is_git_repo(ROOT) and vs.has_remote(ROOT)


def _pull() -> dict:
    """pull은 **상한 판정보다 앞선다**(계약 §3.5). 각 기기가 자기 사본 기준으로
    상한을 지키면 합쳐서 두 배가 되기 때문이다. 다만 최선 노력이다 — 오프라인에서
    못 쓰게 만드는 것보다, 뒤늦은 충돌을 통합 신호로 받는 편이 이 설계와 일관된다."""
    try:
        if not _sync_ready():
            return {"pull": "skipped"}
        import vault_sync as vs
        return {"pull": vs.pull(ROOT)}
    except Exception as e:                      # 동기화 실패가 쓰기를 막지 않는다
        return {"pull": f"error: {type(e).__name__}"}


def _push(scope: str) -> dict:
    try:
        if not _sync_ready():
            return {"push": "skipped"}
        import vault_sync as vs
        return {"push": vs.commit_push(ROOT, f"wm: {scope} 작업 기억 갱신")}
    except Exception as e:
        return {"push": f"error: {type(e).__name__}"}
