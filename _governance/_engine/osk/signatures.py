"""osk.signatures — 서명 기록부.

구현 근거: 헌법 10조(서명은 사용자 전속·외부 기록·전체 바이트 상태),
시행령 §6(기록 요소·트랜잭션·최신 기록 판정), Mechanism §3(kind별 hash 의미·
기각 회복 순서 — restore 선기록 후 원자 교체, 실패는 언제나 미서명 쪽).

서명·해제의 발의는 사용자 전속이다. 이 모듈은 기록의 기계일 뿐 판단을
대행하지 않는다 (CLI가 사용자 확인을 강제한다).

대장·사건부에 적힌 **경로와 사건 번호는 신뢰 밖 입력**이다(다기기 병합으로
임의의 내용이 유입된다). 파일을 읽거나 쓰기 전에 core.resolve_in_root로
vault 안에 봉쇄하고 사건 번호는 형식으로 가둔다 — 해석 실패는 언제나
미서명·거부 쪽이다.
"""
from __future__ import annotations
import os, re, tempfile
from pathlib import Path

import yaml

from .core import (ROOT, LEDGER, SIGNATURES, CASE_RE, ledger_append, ledger_read,
                   sha256_file, causal_maxima, unresolved_nodes, posix_rel,
                   resolve_in_root)
from . import contract

KINDS = ("sign", "unsign", "restore")


def _rel_in_root(p: Path) -> str:
    """봉쇄 해석된 경로 → 대장에 적을 vault 상대 경로.

    대장은 다기기 병합 대상이므로 표기가 기기에 의존하면 안 된다 —
    `posix_rel`로 접는다(그러지 않으면 Windows에서 기록한 `path`가
    역슬래시가 되어 다른 기기의 `resolve_in_root`가 해석하지 못한다)."""
    return posix_rel(p, Path(os.path.realpath(ROOT)))


def _id_of(p: Path) -> str | None:
    """파일의 id — 계약 파서 한 갈래로만 읽는다. 정규식 파서를 따로 두면
    따옴표 표기처럼 계약은 통과하는 노드가 서명 판정에서만 미아가 된다."""
    try:
        i = contract.parse(p).id
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        return None
    return i or None


def locate_by_id(node_id: str) -> Path | None:
    """현재 vault에서 id로 노드 파일을 찾는다 — 경로 이동은 서명을 깨지
    않으므로(경로는 해시 대상 밖) 상태 판정은 id 해석이 정본이다."""
    from . import graph
    for p, _k in graph.iter_nodes():
        if _id_of(p) == node_id:
            return p
    return None


def records() -> list[dict]:
    return ledger_read(SIGNATURES)


def latest_by_node() -> dict[str, dict]:
    """노드 열거용 — 판정에는 쓰지 않는다(판정 정본은 causal_maxima)."""
    out: dict[str, dict] = {}
    for r in records():
        n = r.get("node")
        if n:
            out[n] = r
    return out


def status(node_id: str, path: Path | str | None = None) -> str:
    """'signed' | 'unsigned'. 판정 기록 = 그 노드의 **인과 극대**(Mechanism §3).
    극대가 여럿(병합 후 비교 불능 분기)이거나 구조 손상에 연루된 노드는
    보수적으로 미서명이며, 이후 모든 head를 조상으로 갖는 새 기록(사용자
    재서명 등)이 유일 극대가 되면 해소된다.

    파일 해석은 **id가 정본**이다: 어떤 경로를 쓰든 vault 안으로 봉쇄 해석한
    뒤 그 파일의 id가 node_id와 일치해야 하며(경로 재사용·탈출 방어),
    불일치·부재 시 id 전수 탐색으로 현재 위치를 찾는다."""
    recs = records()
    if node_id in unresolved_nodes(recs):
        return "unsigned"                 # 분기·순환·구조 손상 — fail-closed
    maxima = causal_maxima(recs, node_id)
    if len(maxima) != 1 or maxima[0].get("kind") not in ("sign", "restore"):
        return "unsigned"
    r = maxima[0]
    p = None
    for c in (path, r.get("path")):
        c = resolve_in_root(c) if c else None
        if c is not None and c.exists() and _id_of(c) == node_id:
            p = c
            break
    if p is None:
        p = locate_by_id(node_id)
        if p is None:
            return "unsigned"
    try:
        return "signed" if sha256_file(p) == r.get("hash") else "unsigned"
    except OSError:
        return "unsigned"


class _StrictLoader(yaml.SafeLoader):
    """중복 키를 오류로 만드는 로더 — 뒤에 오는 줄이 판정 필드를 조용히
    덮어쓰는 것(마지막 값 승리)을 막는다."""


def _no_dup_mapping(loader: _StrictLoader, node):
    keys = set()
    for k, _v in node.value:
        key = loader.construct_object(k)
        if key in keys:
            raise yaml.YAMLError(f"중복 키: {key}")
        keys.add(key)
    return loader.construct_mapping(node)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


def parse_case(case_path: Path) -> dict | None:
    """사건 파일 머리의 기계 판정 헤더(Mechanism §4 3항)를 구조적으로 파싱.
    헤더가 '---'로 열리면 frontmatter 규약(계약과 같은 종결자), 아니면 유산
    관용(첫 빈 줄까지)을 쓴다. 어느 쪽이든 중복 키는 거부한다."""
    try:
        text = case_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end < 0:
            return None
        head = text[4:end]
    else:
        head = text.split("\n\n", 1)[0]
    try:
        data = yaml.load(head, Loader=_StrictLoader)
    except (yaml.YAMLError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def sign(path: Path | str, reason: str, node_id: str) -> dict:
    """서명 등재. 입건된 사건의 당사자는 판결 전까지 재서명되지 않는다
    (헌법 12조 5항 후단) — 심의 중인 상태를 서명으로 굳히지 못하게 한다."""
    from . import graph
    p = resolve_in_root(path)
    if p is None:
        raise ValueError(f"vault 밖 경로는 서명할 수 없다: {path}")
    case_no = graph.open_case_of(node_id)
    if case_no:
        raise ValueError(
            f"열린 사건 {case_no}의 당사자다 — 판결 전까지 재서명 불가"
            f" (헌법 12조 5항)")
    return ledger_append(SIGNATURES, {
        "kind": "sign", "node": node_id, "path": _rel_in_root(p),
        "hash": sha256_file(p), "reason": reason})


def unsign(node_id: str, reason: str) -> dict:
    """해제 — hash는 해제되는 서명의 해시(그 sign 기록과 일치해야 한다).
    판정은 인과 극대: 분기·손상 상태면 해제 불가 — 해소가 먼저다."""
    recs = records()
    if node_id in unresolved_nodes(recs):
        raise ValueError(f"인과 분기·구조 손상 상태 — 해소 후 해제: {node_id}")
    maxima = causal_maxima(recs, node_id)
    r = maxima[0] if maxima else None
    if not r or r.get("kind") not in ("sign", "restore"):
        raise ValueError(f"유효 서명 없음: {node_id}")
    return ledger_append(SIGNATURES, {
        "kind": "unsign", "node": node_id, "path": r.get("path"),
        "hash": r.get("hash"), "reason": reason})


def restore_for_dismissal(node_id: str, restored_bytes: bytes, case_no: str) -> dict:
    """기각 회복 (Mechanism §3 6항 · 시행령 §9 4항). 사건부 구조적 결속:
    ⓐ사건 번호 형식 + 파일 실재 + case_no=파일명 ⓑstatus=adjudicated
    ⓒverdict=기각 ⓓparties의 정확한 원소 ⓔ사건의 pre_sign[node] rid = 대장의
    인과 극대 rid ⓕ헤더 필수 필드·시각 정합 ⓖ교체 대상은 vault 안의 그 노드
    파일 — 전부 통과해야 restore 선기록 → 원자 교체.
    어느 단계 실패도 미서명 쪽으로 남는다."""
    from datetime import datetime as _dt
    from .core import sha256_bytes
    if not re.match(CASE_RE, str(case_no)):
        raise ValueError(f"ⓐ 사건 번호 형식 위반(Mechanism §4 3항): {case_no}")
    case_p = LEDGER / "case" / f"{case_no}.md"
    if not case_p.exists():
        raise ValueError(f"ⓐ 사건 없음: {case_no}")
    case = parse_case(case_p)
    if not case:
        raise ValueError(f"ⓐ 사건 헤더 파싱 불가: {case_no}")
    if str(case.get("case_no")) != case_no:
        raise ValueError(f"ⓐ case_no 불일치: 파일명={case_no} 헤더={case.get('case_no')}")
    if str(case.get("status")) != "adjudicated":
        raise ValueError(f"ⓑ 미종결 사건(status={case.get('status')}) — 회복 불가")
    if str(case.get("verdict")) != "기각":
        raise ValueError(f"ⓒ 판결이 기각이 아님: {case.get('verdict')}")
    parties = case.get("parties") or []
    if node_id not in [str(x) for x in parties]:
        raise ValueError(f"ⓓ {node_id}는 당사자가 아님: {parties}")
    for req in ("docketed_at", "verdict_at", "applied", "schema_version"):
        if req not in case:
            raise ValueError(f"ⓕ 사건 헤더 필드 누락: {req}")
    if int(case.get("schema_version", 0)) != 1:
        raise ValueError(f"ⓕ schema_version 미지원: {case.get('schema_version')}")
    try:
        d_at = _dt.fromisoformat(str(case["docketed_at"]))
        v_at = _dt.fromisoformat(str(case["verdict_at"]))
    except ValueError as e:
        raise ValueError(f"ⓕ 사건 시각 형식 오류: {e}")
    if v_at < d_at:
        raise ValueError("ⓕ verdict_at이 docketed_at보다 앞선다")
    recs_all = records()
    hist = [r for r in recs_all if r.get("node") == node_id]
    if not hist:
        raise ValueError("① 위반: 입건 직전 유효 서명이 없다")
    if node_id in unresolved_nodes(recs_all):
        raise ValueError("인과 분기·구조 손상 상태 — 자동 회복 불가, 해소가 먼저다")
    maxima = causal_maxima(recs_all, node_id)
    pre = maxima[0] if maxima and maxima[0].get("kind") in ("sign", "restore") else None
    if pre is None:
        raise ValueError("③ 위반: 유효 서명이 현재 상태가 아님(해제 등) — 자동 회복 불가")
    pre_map = case.get("pre_sign") or {}
    if str(pre_map.get(node_id)) != pre["rid"]:
        raise ValueError(f"ⓔ 사건의 pre_sign rid 불일치: 사건={pre_map.get(node_id)} 대장={pre['rid']}")
    # pre_sign rid = 현재 인과 극대라는 동일성이 '사이 기록 없음'을 이미
    # 보증한다. 남는 결속은 벽시계 정합뿐이고, 그 비교는 rid 상위 비트(논리
    # 시계 — 다른 기기의 빠른 시계가 하나만 섞여도 부풀려진다)가 아니라
    # 기록의 at으로 한다.
    try:
        p_at = _dt.fromisoformat(str(pre.get("at", "")))
    except ValueError:
        raise ValueError("ⓔ 직전 유효 서명 기록의 at 형식 오류 — 회복 불가")
    if (p_at.tzinfo is None) != (d_at.tzinfo is None):
        raise ValueError("ⓔ 시각 비교 불능(시간대 표기 불일치) — 회복 불가")
    if p_at > d_at:
        raise ValueError("ⓔ 직전 유효 서명이 입건 시점보다 뒤다")
    if sha256_bytes(restored_bytes) != pre["hash"]:
        raise ValueError("② 위반: 복원 상태가 직전 유효 서명 해시와 불일치")
    found = locate_by_id(node_id)
    target = resolve_in_root(found if found is not None else pre.get("path") or "")
    if target is None:
        raise ValueError(f"ⓖ 교체 대상이 vault 밖이다 — 회복 불가: {pre.get('path')}")
    if target.exists() and _id_of(target) != node_id:
        raise ValueError(f"ⓖ 그 경로는 {node_id}의 파일이 아니다 — 덮어쓰지 않는다: {target}")
    if not target.parent.is_dir():
        raise ValueError(f"ⓖ 교체 대상 디렉터리 부재: {target.parent}")
    rec = ledger_append(SIGNATURES, {
        "kind": "restore", "node": node_id, "path": _rel_in_root(target),
        "hash": pre["hash"], "reason": "사건 기각 회복", "case": case_no})
    fd, tmp = tempfile.mkstemp(dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(restored_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)  # 원자 교체
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return rec
