"""osk-system MCP 서버 — 외부 표면 (Mechanism §6-2).

이 표면은 프로세스 경계다 — 클라이언트는 여기 선언된 도구만 호출할 수 있으므로,
무엇을 선언하는지가 곧 그 클라이언트의 능력이다.

**권위 비노출**: 보호영역 권위(`protect`·`unprotect`·`approve`·`revert`)와
pin 기록은 노출하지 않는다. 지정·해제·승인·반려의 발의는 대화형 단말
전속이다(헌법 10조 1~2항). 쓰기 도구는 전부 `osk.write`의 단일 통로를 거치며,
그 통로는 승인 기록부와 pin 기록에 쓰지 않는다 — 검증기의 표면 세그먼트가 이
사실을 AST로 강제한다.

표면을 통한 쓰기는 보호영역 안에서도 평소처럼 작업본에 반영될 뿐이며,
승인본과의 차이는 변경집합으로 남는다. 그것을 승인본으로 만드는 것은 사용자의
승인뿐이다(§6-2 8항).
"""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402
# 도구 함수명이 모듈명을 가리지 않게 별칭으로 들여온다 — `def search(...)`가
# 모듈 전역의 `search`를 재결속하면 `search.Searcher`가 죽는다(7차 치명).
from osk import graph, raw, validate, write  # noqa: E402
from osk import search as search_mod  # noqa: E402
from osk.core import ROOT, sha256_file  # noqa: E402

# 계약이 정한 집합을 스키마가 그대로 든다 — 강제와 교육과 발견이 한 번에
# 이뤄진다(술어는 헌법 8조 5항, 충돌 유형은 Mechanism §4 3항의 목록이며,
# 그 개정이 있을 때만 이 줄이 함께 바뀐다).
Predicate: TypeAlias = Literal["derived-from", "conflicts"]
Edges: TypeAlias = dict[Predicate, str | list[str]]
CandidateType: TypeAlias = Literal["contradiction", "duplication",
                                   "competition", "delegation-overlap"]
Title: TypeAlias = Annotated[str, Field(min_length=1, max_length=120)]
Summary: TypeAlias = Annotated[str, Field(min_length=1, max_length=80)]
Drafter: TypeAlias = Annotated[str, Field(pattern=r"^[a-z][a-z0-9.\-]{0,39}$")]
# 기록 이름도 곧 파일명이다 — 상한은 Title과 같은 자리에서 같은 이유로 건다.
RawRecord: TypeAlias = Annotated[str, Field(min_length=1, max_length=120)]

# CREATE_NO_WINDOW — 콘솔 서브시스템 자식(git)에 창을 주지 않는다.
# `vault_sync._NO_WINDOW`와 같은 이유이고 같은 값이다(POSIX에선 0, 무영향).
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

mcp = FastMCP("osk-system")
_searcher = None
_fingerprint: str | None = None


def _vault_fingerprint() -> str:
    """전 노드의 (상대경로, mtime_ns, size) digest — 순수 이동·개명도 경로가
    바뀌므로 감지된다 (파일 정본에서 재계산 원칙)."""
    import hashlib
    h = hashlib.sha256()
    for p, _k in sorted(graph.iter_nodes(), key=lambda x: str(x[0])):
        try:
            st = p.stat()
        except OSError:
            continue   # 열거와 stat 사이의 삭제 — 다음 호출의 지문이 달라진다
        h.update(f"{p.relative_to(ROOT)}|{st.st_mtime_ns}|{st.st_size}\n".encode())
    return h.hexdigest()


def _s():
    global _searcher, _fingerprint
    fp = _vault_fingerprint()
    if _searcher is None or fp != _fingerprint:
        _searcher = search_mod.Searcher()
        _fingerprint = fp
    return _searcher


def _engine_rev(timeout: float = 5) -> str:
    """지금 도는 엔진의 판. 수트는 코드를 시험하지 **돌고 있는 프로세스**를
    시험하지 못하므로(8차의 영구 사각), 첫 호출에서 서버의 낡음이 보이게 한다.

    표면은 stdio 파이프 위에 서 있다. git에게 그 stdin을 물려주면 안 되고,
    시한이 지나 죽인 뒤에도 파이프를 쥔 손자(자격증명 도우미 등)가 남으면
    `subprocess.run`의 사후 `communicate()`가 **무기한** 막힌다 — 도구 호출이
    영영 돌아오지 않는다. stdin을 끊고, 콘솔을 주지 않고, 정리에도 시한을 건다.
    판 하나 읽자고 표면이 멈추는 일은 없어야 한다."""
    p = None
    try:
        p = subprocess.Popen(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, creationflags=_NO_WINDOW)
        out, _ = p.communicate(timeout=timeout)
        return out.strip() or "unknown"
    except Exception:
        if p is not None:
            try:
                p.kill()
                p.communicate(timeout=1)
            except Exception:  # noqa: BLE001 — 정리 실패까지 표면을 막지 않는다
                pass
        return "unknown"


def _prune_titles(s):
    """pydantic이 인자 이름을 되풀이해 넣는 **주석** `title`을 걷어낸다 —
    상주 예산의 28%인데 행동을 하나도 바꾸지 않는다.

    순진한 재귀 pop은 `properties` **안의 인자 이름** `title`까지 지워
    required가 properties를 벗어나는 자기모순을 만든다(10차 §2-3). 그래서
    스키마 노드에서만 벗기고 properties의 키는 건드리지 않는다."""
    if not isinstance(s, dict):
        return s
    out = {k: v for k, v in s.items() if k != "title"}
    if "properties" in out and isinstance(out["properties"], dict):
        out["properties"] = {k: _prune_titles(v)
                             for k, v in out["properties"].items()}
    for k in ("items", "additionalProperties", "not"):
        if k in out:
            out[k] = _prune_titles(out[k])
    for k in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if k in out and isinstance(out[k], list):
            out[k] = [_prune_titles(x) for x in out[k]]
    if "$defs" in out and isinstance(out["$defs"], dict):
        out["$defs"] = {k: _prune_titles(v) for k, v in out["$defs"].items()}
    return out


def _guard(fn, *a, **kw) -> dict:
    """쓰기 결과 또는 위반 목록. 부분 성공은 없다 — 실패면 아무것도 쓰지 않았다."""
    try:
        return fn(*a, **kw)
    except write.WriteError as e:
        return {"ok": False, "violations": e.violations, **e.extra}
    except Exception as e:                      # 죽지 않고 보고한다(시행령 §11)
        return {"ok": False, "violations": [f"{type(e).__name__}: {e}"]}


# ── 읽기 ─────────────────────────────────────────────────────────────────

@mcp.tool()
def search(query: str, k: Annotated[int, Field(ge=1, le=50)] = 8) -> list[dict]:
    """검색 — `query`의 어휘가 겹쳐야 걸린다(전 Space 연합, `_raw`·Workbench
    제외). 결과의 `title`이 그대로 다른 도구의 `name`이다. `summary`는 미리보기이니
    인용·판단은 `read_node` 뒤에 하고, 시기는 `updated`으로 걸러 필요한 것만
    펼쳐라. 0건이면 어휘를 바꿔 재질의한다."""
    return _s().view_search(query, k)


@mcp.tool()
def read_node(name: str) -> dict:
    """노드 전문 읽기 — 본문 전체가 오므로 비싸다(평균 1.4k 토큰). 인용이나
    본문 재작성 직전에만 부르고, 해시만 알려고 부르지 마라 — 쓰기 응답이
    `new_hash`로 준다. `name`은 노드 제목이며 `id`로도 찾는다. `hash`는
    `expect_hash`에 그대로 넣는다."""
    idx = _s().idx
    hit = idx.nodes.get(name)
    if not hit:
        # 쓰기 응답은 id를 돌려준다 — 그것을 핸들로 잡은 호출자에게
        # "노드 없음"은 틀린 진단이다(10차 ②)
        import re as _re
        from osk.core import ID_RE as _ID
        if _re.match(_ID, str(name).strip()):
            for stem, (p, _k) in idx.nodes.items():
                try:
                    if idx.node(p).id == str(name).strip():
                        hit, name = (p, _k), stem
                        break
                except Exception:
                    continue
    if not hit:
        why = (getattr(idx, "broken", None) or {}).get(name)
        return {"error": f"파싱 실패 — 수동 확인 필요: {why}" if why
                else f"노드 없음: {name}"}
    try:
        n = idx.node(hit[0])
    except Exception as e:
        return {"error": f"파싱 실패 — 수동 확인 필요: {name} ({e})"}
    return {"path": str(hit[0].relative_to(ROOT)), "id": n.id,
            "meta": {k: str(v) for k, v in n.meta.items()},
            "hash": sha256_file(hit[0]),
            "body": n.body}


@mcp.tool()
def read_raw(ref: str | None = None, space: str | None = None,
             max_chars: Annotated[int, Field(ge=200, le=100000)] = 20000) -> dict:
    """세션 기록의 **명시 회상** — `_raw/`는 검색에 걸리지 않으니 좌표로 연다.
    `ref`는 노드 `derived-from`에 든 `[[경로#N]]` 그대로이며 그 라운드 전문이
    온다. `#N` 없이 경로만 주면 그 기록의 목차(번호·미리보기)가, `ref` 대신
    `space`(`"= Scope/W1"` 꼴)를 주면 그 scope의 기록 목록이 온다. 긴 라운드는
    `max_chars`에서 잘리며 `truncated`로 알린다."""
    if ref:
        return _guard(raw.read_round, ref, max_chars)
    if space:
        return _guard(raw.list_records, space)
    return {"ok": False, "violations": [
        "`ref`(라운드 좌표 `[[경로#N]]`) 또는 `space`(`= Scope/이름`) 중 "
        "하나를 준다 — 좌표를 모르면 `space`로 기록 목록부터 본다"]}


@mcp.tool()
def overview(session: str | None = None) -> dict:
    """구조 조망 — 무엇이 있고 어디에 둘 수 있는가. **첫 쓰기 전에 한 번** 부르면
    착지를 추측하지 않아도 된다. `clusters`는 노드를 둘 수 있는 군집 경로(그대로
    `space`에 넣는다), `open_cases`는 `conflicts`에 쓸 수 있는 사건 번호,
    `broken`은 검색에 잡히지 않는 파손 파일, `engine_rev`는 지금 도는 엔진의
    판이다(저장소보다 오래됐으면 서버를 재기동하라). `session`을 주면 그 키의
    현재 결속(`session_scope`)을 함께 돌려준다."""
    idx = _s().idx
    out = {
        "clusters": write._cluster_names(),
        "open_cases": write._open_cases(),
        "broken": sorted(getattr(idx, "broken", None) or {}),
        "nodes": len(idx.nodes),
        "engine_rev": _engine_rev(),
    }
    if session:
        out["session_scope"] = write.resolve_session(session)
        out["session_canonical"] = write.canonical_session(session)
    return out


@mcp.tool()
def run_validators() -> dict:
    """검증기 수트 실행. 쓰기는 그 노드의 나가는 참조만 보므로 전역 상태
    (중복 id·위상·대장 손상·파싱 실패 노드)는 이 도구로만 보인다.
    보고 전용이니 고치는 것은 호출자의 일이다."""
    return validate.run()


# ── 쓰기 (osk.write 단일 통로) ───────────────────────────────────────────

@mcp.tool()
def create_node(title: Title, summary: Summary, body: str, drafter: Drafter,
                session: str | None = None, space: str | None = None,
                edges: Edges | None = None) -> dict:
    """노드 생성 — `title`이 곧 파일명이자 다른 도구의 `name`이며 전역 유일이고,
    `body`는 본문 전문이다.
    `space`는 군집의 전체 경로(`"= Scope/W1"` 꼴, 맨 이름 `W1`은 거부)이니
    모르면 `overview`를 먼저 보라. `session`은 저장소 이름처럼 세션이 바뀌어도
    같은 값이며 첫 성공이 그 키를 영구 결속한다 — 1회용 대화 id를 넣지 마라.
    `edges`의 `derived-from`은 근거를 가리킨다 — 노드 근거는 그 `id`
    (`260802-114u-7lo3` 꼴)로, 비노드 근거는 `[[경로]]`·`[[경로#제목]]`로 준다.
    `conflicts`는 열린 사건 번호(`CASE-2026-1` 꼴)만 받는다. 본문의 `[[링크]]`도
    검사 대상이라 다른 scope의 노드는 직접 가리킬 수 없다."""
    return _guard(write.create_node, title, summary, body, drafter,
                  session, space, edges)


@mcp.tool()
def update_node(name: str, body: str | None = None,
                expect_hash: str | None = None,
                summary: Summary | None = None,
                add_edges: Edges | None = None,
                remove_edges: Edges | None = None) -> dict:
    """`name`이 가리키는 노드의 본문·summary·엣지 수정. 엣지는 델타라 현재
    상태에 적용되니 선-읽기가 필요 없다. **확인하려고 먼저 읽지 마라** — 그냥
    시도하면 거부가 짧게 필요한 것을 알려준다. `expect_hash`는 `body`를 보낼
    때(언제나 전문 치환이다) 필수이며, 방금 쓴 응답의 `new_hash`를 그대로 이어
    쓸 수 있다. 응답의 `dangling`은 대상이 아직 없는 링크이니 오타인지
    확인하라."""
    return _guard(write.update_node, name, body, expect_hash, summary,
                  add_edges, remove_edges)


@mcp.tool()
def move_node(name: str, dest_space: str) -> dict:
    """`name`이 가리키는 노드의 군집 재배정 — `dest_space`는 `create_node`의
    `space`와 같은 형식이다(`overview`의 `clusters`를 그대로 쓴다). 이동은
    바이트를 바꾸지 않는다(경로는 상태, 동일성은 id). pin된 군집은 출발·도착
    어느 쪽이든 거부한다."""
    return _guard(write.move_node, name, dest_space)


@mcp.tool()
def record_candidate(type: CandidateType,
                     nodes: Annotated[list[str], Field(min_length=2)],
                     reason: str = "") -> dict:
    """충돌 후보 상정 — 사용자 심의를 요청한다. 같은 근거가 이미 있으면 기존
    기록을 돌려주므로 재호출이 안전하다. `nodes`는 서로 다른 둘 이상의 노드
    이름이다."""
    return _guard(write.record_candidate, type, nodes, reason)


@mcp.tool()
def append_raw(session: str, record: RawRecord, user: str, agent: str,
               space: str | None = None) -> dict:
    """세션 기록 append — 이 대화의 한 라운드(`user` 발화와 그에 딸린 `agent`
    응답)를 그 scope의 불변 기록에 잇는다. `record`는 **대화 하나의 이름**이라
    한 대화 내내 같은 값을 쓴다(`2026-08-21-undo-buffer` 꼴). 라운드 번호는
    엔진이 매기며, 응답의 `round_ref`를 그대로 `create_node`의 `derived-from`에
    넣으면 근거가 배선된다. `session`·`space`는 `create_node`와 같다. 기록은
    append만 되고 고쳐지지 않으며, `filtered`가 비지 않았으면 비밀값이 치환된
    것이다."""
    return _guard(raw.append_round, session, record, user, agent, space)


def _apply_prune() -> None:
    """등록된 도구의 스키마에서 주석 title을 1회 걷어낸다."""
    mgr = getattr(mcp, "_tool_manager", None)
    for tool in (mgr._tools.values() if mgr else []):
        if isinstance(getattr(tool, "parameters", None), dict):
            tool.parameters = _prune_titles(tool.parameters)


_apply_prune()


if __name__ == "__main__":
    mcp.run()
