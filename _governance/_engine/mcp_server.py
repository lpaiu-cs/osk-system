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
import re
import sys
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402
# 도구 함수명이 모듈명을 가리지 않게 별칭으로 들여온다 — `def search(...)`가
# 모듈 전역의 `search`를 재결속하면 `search.Searcher`가 죽는다(7차 치명).
from osk import contract, epoch, graph, raw, rechecks, validate, write  # noqa: E402
# 도구명이 모듈명을 가린다 — search와 같은 이유로 별칭 import.
from osk import scope_memory as scope_memory_mod  # noqa: E402
from osk import search as search_mod  # noqa: E402
from osk.core import (DRAFTER_RE, ROOT, StaleEngineError,  # noqa: E402
                      posix_rel, sha256_bytes)

# 계약이 정한 집합을 스키마가 그대로 든다 — 강제와 교육과 발견이 한 번에
# 이뤄진다(술어는 헌법 8조 5항, 충돌 유형은 Mechanism §4 3항의 목록이며,
# 그 개정이 있을 때만 이 줄이 함께 바뀐다).
Predicate: TypeAlias = Literal["derived-from", "conflicts"]
Edges: TypeAlias = dict[Predicate, str | list[str]]
CandidateType: TypeAlias = Literal["contradiction", "duplication",
                                   "competition", "delegation-overlap"]
Title: TypeAlias = Annotated[str, Field(min_length=1, max_length=120)]
Summary: TypeAlias = Annotated[str, Field(min_length=1, max_length=80)]
Drafter: TypeAlias = Annotated[str, Field(pattern=DRAFTER_RE)]
# 기록 이름도 곧 파일명이다 — 상한은 Title과 같은 자리에서 같은 이유로 건다.
RawRecord: TypeAlias = Annotated[str, Field(min_length=1, max_length=120)]

mcp = FastMCP("osk-system")
_searcher = None
_index = None
_fingerprint: tuple[str, str] | None = None


def _vault_fingerprint() -> tuple[tuple[str, str], bool]:
    """전체·노드의 (상대경로, mtime_ns, size) digest 쌍과 관측 불확실 여부.
    순수 이동·개명도 경로가 바뀌므로 감지된다(파일 정본에서 재계산 원칙).
    비노드만 바뀌면 이름표를 새로 읽고 노드 파싱·검색기는 재사용한다.

    범위가 색인보다 좁으면 그 바깥의 변경이 캐시를 무효화하지 못하고(구판은
    노드만 봐서 `_raw`·대장의 변경을 놓쳤다), 넓으면 색인과 무관한 것이 캐시를
    버린다. 범위 판정은 `graph.index_signature`가 한 벌로 갖는다.

    둘째 값 `racy`가 참이면 채취 시각에 너무 가까운 파일이 있었다는 뜻이다 —
    그 창 안에서는 (mtime, 크기)가 변경을 구별하지 못하므로 지문이 같다고
    답하는 것이 곧 낡은 색인 위에서 읽는 것이 된다.

    지문 자체에 시각을 섞지는 **않는다.** 섞으면 캐시 키가 내용과 무관한 값이
    되어 창이 닫힌 뒤에도 무엇과도 맞지 않는다. 믿을지 말지는 호출부가 정한다."""
    import hashlib
    errors = []
    entries, racy = graph.index_signature(errors)
    h = hashlib.sha256()
    for rel, mtime_ns, size, kind, target in entries:
        h.update(f"{rel}|{mtime_ns}|{size}|{kind}|{target}\n".encode())
    full = h.hexdigest()
    if _fingerprint is not None and full == _fingerprint[0]:
        node_key = _fingerprint[1]
    else:
        nodes = hashlib.sha256()
        for rel, mtime_ns, size, kind, target in entries:
            if graph.is_node_home(kind):
                nodes.update(f"{rel}|{mtime_ns}|{size}|{kind}|{target}\n".encode())
        node_key = nodes.hexdigest()
    # Evidence still invalidates the name map. Only unchanged node contracts
    # and their BM25 can survive that refresh; uncertain scans reuse neither.
    return (full, node_key), racy or bool(errors)


def _idx():
    """공유 색인 캐시. racy 창 안에서는 지문을 **접어 두지 않는다** — 그 창의
    지문은 변경을 구별하지 못하므로 키로 삼으면 낡은 색인을 붙잡게 된다.
    창이 닫히면 그때의 지문이 정상 키가 된다."""
    global _index, _searcher, _fingerprint
    fp, racy = _vault_fingerprint()
    if _index is None or racy or fp != _fingerprint:
        if (_index is not None and _index.complete and not racy
                and _fingerprint is not None and fp[1] == _fingerprint[1]):
            _index.refresh_nonnode()
        else:
            _index = graph.Index()
            _searcher = None
        if not _index.complete:
            _searcher = None
        # 불완전 관측도 접어 두지 않는다 — 못 읽은 자리가 있는 색인을 키로
        # 붙잡으면 그 자리가 다시 읽히게 된 뒤에도 낡은 그림을 계속 쓴다.
        _fingerprint = None if (racy or not _index.complete) else fp
    return _index


def _s():
    """BM25는 검색할 때만 만든다. 조회의 중복 id 검사는 공유 Index에 남는다."""
    global _searcher
    idx = _idx()
    if _searcher is None:
        _searcher = search_mod.Searcher(idx)
    return _searcher


def _engine_state() -> dict:
    """표면이 내는 판 — 이 서버가 **적재한** 엔진과, 디스크가 그보다 새로운가.

    구판은 여기서 `git rev-parse HEAD`를 띄웠다. 그 수는 두 방향으로 거짓을
    말했다 — 데이터만 커밋해도 움직이고, 릴리스 없이 엔진을 고치면 안
    움직인다. 지금은 엔진 파일 자신을 재므로 git을 부르지 않고, 그래서 표면이
    stdio 파이프 위에서 자식 프로세스에 막히던 위험도 함께 사라졌다.

    `engine_stale`이 `None`이면 판정 불가다 — 그 상태에서 쓰기는 관문이
    거부한다(core._fence)."""
    try:
        return {"engine_rev": epoch.loaded(), "engine_stale": epoch.stale()}
    except epoch.EpochError:
        return {"engine_rev": epoch.loaded(), "engine_stale": None}


def _prune_titles(s):
    """pydantic이 인자 이름을 되풀이해 넣는 **주석** `title`을 걷어낸다 —
    상주 예산의 28%인데 행동을 하나도 바꾸지 않는다.

    순진한 재귀 pop은 `properties` **안의 인자 이름** `title`까지 지워
    required가 properties를 벗어나는 자기모순을 만든다(10차 §2-3). 그래서
    스키마 노드에서만 벗기고 properties의 키는 건드리지 않는다."""
    if not isinstance(s, dict):
        return s
    # Null defaults repeat optional/nullable fields; keep useful numeric defaults.
    out = {k: v for k, v in s.items()
           if k != "title" and not (k == "default" and v is None)}
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
    """쓰기 결과 또는 위반 목록. 저장 뒤 후속 작업 실패는 응답의 상태로 남긴다."""
    try:
        return fn(*a, **kw)
    except (write.WriteError, StaleEngineError) as e:
        return {"ok": False, "violations": e.violations, **e.extra}
    except Exception as e:                      # 죽지 않고 보고한다(시행령 §11)
        return {"ok": False, "violations": [f"{type(e).__name__}: {e}"]}


# ── 읽기 ─────────────────────────────────────────────────────────────────

@mcp.tool()
def search(query: str, k: Annotated[int, Field(ge=1, le=50)] = 8) -> list[dict]:
    """`query`로 전 Space 어휘 검색(`_raw`·Workbench 제외). 결과 `title`이 다른 도구의
    `name`. `summary`는 미리보기이며 인용·판단 전에 `read_node`로 확인한다."""
    return _s().view_search(query, k)


@mcp.tool()
def read_node(name: str, view: str | None = None) -> dict:
    """`name`의 전문·`hash`(재작성 `expect_hash`). `view`는 `outline`(목차) 또는
    `시작:끝`(본문 0기반 문자, 끝 제외). 부분 열람은 전문 쓰기용 해시를 주지 않는다."""
    if view is not None and (not isinstance(view, str) or not re.fullmatch(
            r"outline|[0-9]{1,10}:[0-9]{1,10}", view)):
        return {"error": "view는 outline 또는 시작:끝(0기반 본문 문자, 끝 제외)이다"}
    idx = _idx()
    # 동명 노드는 **고르지 않는다** — id 갈래(아래)와 같은 규율이다. 구판은
    # 이름 갈래에만 이 방어가 없어, 쓰기 통로가 "어느 것인지 정해지지 않는다"고
    # 거부하는 상황에서 읽기는 조용히 한쪽을 돌려줬다(Mechanism §2 1항).
    matches, failures = idx.lookup_name(name)
    if len(matches) > 1:
        return {"error": f"같은 이름의 노드가 {len(matches)}개다 "
                         f"— 어느 것인지 정해지지 않는다: "
                         f"{[str(p.relative_to(ROOT)) for p, _k in matches]} (먼저 고쳐라)"}
    hit = matches[0] if matches else None
    if not hit:
        # 쓰기 응답은 id를 돌려준다 — 그것을 핸들로 잡은 호출자에게
        # "노드 없음"은 틀린 진단이다(10차 ②)
        #
        # 해석은 **쓰기와 같은 `by_id`**로 한다. 구판은 여기서 자체 순회로
        # **첫 일치**를 골랐고 `by_id`는 **마지막**을 골랐다 — 같은 id가 둘일 때
        # 읽기와 쓰기가 서로 다른 파일을 가리켰고, 사본은 바이트가 같아
        # `expect_hash`까지 통과해 **읽지 않은 파일이 갱신됐다**(재현 확인).
        # 동점 규칙을 하나로 두는 것으로는 부족하고, 겹쳤으면 고르지 않는다.
        import re as _re
        from osk.core import ID_RE as _ID
        nid = str(name).strip()
        if _re.match(_ID, nid):
            if nid in idx.dup_ids:
                return _dup_id_error(sorted(idx.dup_ids[nid]))
            h = idx.by_id.get(nid)
            if h:
                hit, name = h, h[0].stem
    # 이름으로 잡은 노드도 id가 겹쳤으면 내주지 않는다 — 제목이 다른 사본이
    # 이름으로 각자 읽혀 갈라졌다(Mechanism §2 1항, 2026-09-24 재현).
    twins = idx.id_twins(hit[0]) if hit else []
    if twins:
        return _dup_id_error(twins)
    if not hit:
        why = "; ".join(failures)
        return {"error": f"파싱 실패 — 수동 확인 필요: {why}" if why
                else f"노드 없음: {name}"}
    # 바이트를 **한 번** 읽고, 그 바이트에서 본문과 해시를 함께 만든다.
    #
    # 구판은 본문을 캐시된 `Node`에서, 해시는 디스크에서 새로 재어 붙였다.
    # 캐시가 낡으면(같은 크기로 고치고 mtime을 되돌리면 지문이 같아 racy 창
    # 밖이다) 호출자는 **읽지 않은 상태의 해시**를 받는다. 그 해시는 다음
    # `update_node`의 CAS 입력이므로 그대로 통과하고, 외부 편집이 조용히
    # 사라진다 — 실측으로 재현했다. 해시가 캐시된 바이트에 결속되면 캐시가
    # 낡더라도 다음 쓰기는 CAS에서 **안전하게 거부**된다(Mechanism §6-2 4항).
    try:
        raw = hit[0].read_bytes()
        n = contract.parse_bytes(hit[0], raw)
    except Exception as e:
        return {"error": f"파싱 실패 — 수동 확인 필요: {name} ({e})"}
    # 경로는 POSIX 표기로 낸다 — 도구마다 구분자가 갈리면 ref를 손으로
    # 조립하는 호출자가 혼선을 겪는다(감사 지적). 규칙·ref 표기도 전부 슬래시다.
    # `name`을 **먼저** 낸다. 구판은 `id`만 돌려줘서, 읽은 뒤 그 노드를 근거로
    # 달거나 고치려는 호출자의 손에 남는 것이 id뿐이었다 — 그래서 새 엔진으로도
    # 구형 id 표기 근거가 계속 태어났다(v3.7.4 직후 하루에 3간선). 손잡이는
    # 이름이고, id는 대장·서명·사건부의 동일성으로 남는다.
    h = sha256_bytes(raw)
    _SEEN[posix_rel(hit[0], ROOT)] = rechecks.state(raw)
    if view is not None:
        return {"name": hit[0].stem, "path": posix_rel(hit[0], ROOT), "id": n.id,
                "summary": str(n.meta.get("summary", "")),
                # Distinct from a CAS token: excerpts cannot authorize full replacement.
                "view_hash": "view:" + h, "partial": True,
                "body_chars": len(n.body), **_node_view(n.body, view)}
    return {"name": hit[0].stem, "path": posix_rel(hit[0], ROOT), "id": n.id,
            "meta": {k: str(v) for k, v in n.meta.items()},
            "hash": h,
            "body": n.body}


# 이 세션(서버 프로세스)이 `read_node`로 읽은 판 — 경로 → 그때 본문의 상태
# (`rechecks.state`). 부분 열람도 판을 고정하므로 넣는다. 근거를 다시 대어 재검토를
# 닫을 때 읽은 주장 그대로인지 보는 데만 쓴다(Mechanism §4-1) — 응답에 싣지 않으며
# CAS 증거(`expect_hash`)가 아니다. 부분 열람이 전문 치환을 허가하지 않는 규율은 그대로다.
_SEEN: dict[str, str] = {}


def _dup_id_error(paths: list[str]) -> dict:
    return {"error": f"같은 id의 노드가 {len(paths)}개다 — 어느 것인지 정해지지 "
                     f"않는다: {paths}. {graph.DUP_ID_ADVICE}"}


def _node_view(body: str, view: str) -> dict:
    if view != "outline":
        start, requested_end = map(int, view.split(":"))
        if not 0 <= start < requested_end or start >= len(body):
            return {"error": "범위를 벗어났다 — 0 <= 시작 < 끝, 시작 < body_chars"}
        end = min(requested_end, len(body), start + 4000)
        return {"body": body[start:end], "start": start, "end": end,
                "next_view": f"{end}:{requested_end}" if end < min(requested_end, len(body)) else None}
    # ponytail: ATX headings only, first 40 labels (80 chars each). Range paging
    # covers heading-free/long bodies; add a Markdown parser only if richer outlines matter.
    headings = []
    # Code regions come from the same scanner as link extraction and tag defense.
    for offset, _line, content, code, _cont in contract.md_lines(body):
        heading = not code and re.match(r" {0,3}(#{1,6})(?:[ \t]+|$)(.*)", content)
        if heading:
            headings.append({"title": re.sub(r"[ \t]+#+[ \t]*$", "", heading[2])[:80],
                             "level": len(heading[1]), "start": offset})
            if len(headings) > 40:
                break
    for i, heading in enumerate(headings[:40]):
        end = headings[i + 1]["start"] if i + 1 < len(headings) else len(body)
        heading["view"] = f"{heading['start']}:{end}"
    return {"headings": headings[:40], "outline_truncated": len(headings) > 40,
            "range_help": "0:body_chars 범위를 최대 4000자씩 반환. next_view로 계속 읽고 view_hash가 바뀌면 목차부터 재확인."}


@mcp.tool()
def read_raw(ref: str | None = None, space: str | None = None,
             max_chars: Annotated[int, Field(ge=200, le=100000)] = 6000,
             view: Literal["review", "full"] = "review", query: str | None = None) -> dict:
    """대화 원료 회상: ref=경로#N, 번호 없으면 목차, space=기록 목록.
    기본 review: 발화·답변 선별 ≤6000자. query=원본 AND 검색, full=포렌식.
    생략≠무가치. 전량 이어읽지 않는다. hash는 원본 출처."""
    if ref:
        return _guard(raw.read_round, ref, max_chars, view, query)
    if space:
        return _guard(raw.list_records, space)
    return {"ok": False, "violations": [
        "`ref`(라운드 좌표 `경로#N`) 또는 `space`(`00_Scope/이름`) 중 "
        "하나를 준다 — 좌표를 모르면 `space`로 기록 목록부터 본다"]}


@mcp.tool()
def overview(session: str | None = None) -> dict:
    """구조 조망. **첫 쓰기 전에 한 번** 부른다. `clusters`=허브 있는 군집 경로
    (`create_node.space`), `open_cases`=`conflicts` 사건 번호,
    `broken`=검색에서 빠진 파손 파일. `session`을 주면 현재 결속 `session_scope`도 반환한다."""
    idx = _idx()
    out = {
        "clusters": write._cluster_names(),
        "open_cases": write._open_cases(),
        "broken": sorted(getattr(idx, "broken", None) or {}),
        "nodes": len(idx.nodes),
        **_engine_state(),
    }
    try:
        rc = rechecks.report(idx)
    except Exception as e:                      # 조망은 죽지 않는다(시행령 §11)
        rc = {"error": f"{type(e).__name__}: {e}"}
    if rc:
        out["rechecks"] = rc
    if session:
        # 별칭 해소 결과(`canonical_session`)는 싣지 않는다 — Mechanism §6-2
        # 6항이 "이름의 정본을 정하는 것은 사용자의 일이므로 별칭은 표면에
        # 노출하지 않는다"고 못박는다. 착지 판단에는 `session_scope`로 족하다.
        out["session_scope"] = write.resolve_session(session)
    return out


@mcp.tool()
def run_validators() -> dict:
    """전역 검증(중복 id·위상·대장 손상·파싱 실패). 보고만 하며 수정하지 않는다."""
    return validate.run()


# ── 쓰기 (osk.write 단일 통로) ───────────────────────────────────────────

@mcp.tool()
def create_node(title: Title, summary: Summary, body: str, drafter: Drafter,
                session: str | None = None, space: str | None = None,
                edges: Edges | None = None, settle: str | None = None,
                distill: dict | None = None) -> dict:
    """생성. 전역 유일 `title`=파일명=`name`, `body`=본문 전문.
    `space`는 전체 군집 경로(`00_Scope/W1`); 모르면 `overview`.
    `session`은 저장소명 같은 고정 키(대화 id 금지). 첫 성공에 영구 결속;
    이후 `space` 생략. `bound_scope`는 새 결속만 보고한다.
    `edges`: `derived-from` 근거는 노드 제목, raw `경로#N`, 그 밖 비노드 `[[경로#제목]]`,
    `conflicts`는 열린 사건 번호(`CASE-2026-1`).
    `settle`=보존한 evict rid. 처분 결과: `settlement`.
    증류는 `distill={key,sources,hub}`: 고정 작업 키·근거 ref 목록·기존 허브.
    `distillation.status=complete`까지 같은 요청으로 재시도한다."""
    if distill is not None:
        from osk import distillation
        return _guard(distillation.create_node, distill, title=title,
                      summary=summary, body=body, drafter=drafter,
                      session=session, space=space, edges=edges, settle=settle)
    return _guard(write.create_node, title, summary, body, drafter,
                  session, space, edges, settle)


@mcp.tool()
def update_node(name: str, body: str | None = None,
                expect_hash: str | None = None,
                summary: Summary | None = None,
                add_edges: Edges | None = None,
                remove_edges: Edges | None = None,
                old_text: str | None = None,
                new_text: str | None = None, settle: str | None = None,
                distill: dict | None = None) -> dict:
    """`name`의 같은 주장·조건을 정정한다. 독립 주장은 분화한다.
    `old_text`→`new_text`는 유일 앵커 치환(해시 불필요).
    전문 `body`는 `expect_hash` 필수. 엣지는 선-읽기 없는 델타.
    `dangling`=대상 없는 링크. `settle`=이 본문 갱신으로 보존한 evict rid;
    처분 결과: `settlement`. `distill`은 `create_node`와 같다.
    저장 후 연결만 재개: `distill={resume:키}`."""
    if distill is not None:
        from osk import distillation
        return _guard(distillation.update_node, distill, name=name, body=body,
                      expect_hash=expect_hash, summary=summary, add_edges=add_edges,
                      remove_edges=remove_edges, old_text=old_text,
                      new_text=new_text, settle=settle)
    return _guard(write.update_node, name, body, expect_hash, summary,
                  add_edges, remove_edges, old_text, new_text, settle, _seen=_SEEN)


@mcp.tool()
def move_nodes(names: list[str], dest_space: str) -> dict:
    """군집 재배정 — 중단 시 남은 것 복구. `names`는 노드 제목 목록이고
    (하나여도 목록으로) `dest_space`는 `create_node`의 `space`와 같다. 없는
    군집이면 만들어지되 허브는 없다. 허브는 거부하니 군집째는 `move_cluster`다.
    응답의 `hub_links`가 **양쪽** 허브에서 뺄 것(`remove`)과 더할 것(`add`)을
    준다 — 둘 다 해야 도달이 산다. `crossed_scope`가 오면 먼저 읽어라."""
    return _guard(write.move_nodes, names, dest_space)


@mcp.tool()
def move_cluster(name: str, dest_parent: str) -> dict:
    """하위 군집을 폴더째 옮긴다 — `name`은 그 군집(=허브)의 이름, `dest_parent`
    는 새 부모(`clusters`). 아래 군집도 함께 가며 최상위는 못 건넌다.
    `hub_links`는 `move_nodes`와 같다."""
    return _guard(write.move_cluster, name, dest_parent)


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
    """`user` 발화와 `agent` 응답 한 라운드를 불변 기록에 잇는다.
    `session`=고정 저장소 키, `record`=이 대화 내내 같은 기록 이름.
    `space`는 `00_Scope/<이름>` 두 마디이며 결속 뒤 생략.
    엔진이 번호를 매긴 `round_ref`를 `derived-from`으로 쓴다.
    `filtered`는 치환된 비밀값 종류다."""
    return _guard(raw.append_round, session, record, user, agent, space)


@mcp.tool()
def scope_memory(session: str, text: str | None = None,
                 expect_hash: str | None = None,
                 space: str | None = None,
                 edits: list[dict] | None = None) -> dict:
    """scope 기억 — 그 scope에서 지금 살아 있는 **배울 점**. 모든 세션과
    기기가 **같은 것을 본다** — 이 세션에만 유효한 작업 상태는 적지 않는다.
    아무것도 없이 부르면 읽는다. 쓰기는 **`edits`가 기본** —
    `[{old_text,new_text},…]`, 앵커는 본문에 정확히 한 번, 전부 아니면 전무,
    상한은 순결과에만, 해시 불요. `text`는 전체 치환이라 `expect_hash` 필수.
    `session`은 `create_node`와 같은 값, `space`는 `00_Scope/<이름>` **두 마디**만.
    상한 1500자."""
    if text is None and edits is None:
        return _guard(scope_memory_mod.read, session, space)
    return _guard(scope_memory_mod.replace, session, text, expect_hash, space,
                  edits)


def _apply_prune() -> None:
    """Reject unknown arguments before dispatch and prune schema annotations.

    FastMCP 내부(`_tool_manager._tools`·`fn_metadata.arg_model`)에 기댄다. 그
    자리가 바뀐 mcp 판에서 조용히 건너뛰면 오타 인자가 버려진 채 나머지만
    적용된다 — 기동에서 죽는다(fail-closed)."""
    tools = getattr(getattr(mcp, "_tool_manager", None), "_tools", None)
    if not isinstance(tools, dict) or not tools:
        raise RuntimeError("FastMCP 도구 목록(_tool_manager._tools)을 찾지 못했다 — "
                           "미지 인자 거부를 걸 수 없어 기동하지 않는다. "
                           "requirements.txt의 mcp 판을 확인하라")
    for name, tool in tools.items():
        model = getattr(getattr(tool, "fn_metadata", None), "arg_model", None)
        if model is None or not isinstance(getattr(tool, "parameters", None), dict):
            raise RuntimeError(f"도구 `{name}`의 인자 모델을 찾지 못했다 — 미지 인자 "
                               f"거부를 걸 수 없어 기동하지 않는다")
        # FastMCP otherwise drops misspelled edits while applying valid fields.
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)
        tool.parameters = _prune_titles(model.model_json_schema(by_alias=True))
        if tool.parameters.get("additionalProperties") is not False:
            raise RuntimeError(f"도구 `{name}`에 미지 인자 거부가 걸리지 않았다 — "
                               f"기동하지 않는다")


_apply_prune()


if __name__ == "__main__":
    mcp.run()
