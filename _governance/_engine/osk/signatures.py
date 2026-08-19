"""osk.signatures — 구체제 서명 기록부(사료)와 판독 헬퍼.

서명 제도는 폐지됐다(헌법 14조 6항 · Mechanism §3 9항). `signatures.jsonl`은
**사료로 동결**한다 — 새 기록을 추가하지 않으며 어떤 판정의 근거도 아니다.
새 체제의 권위는 보호영역의 승인본으로만 성립한다(osk.approvals).

이 모듈이 남기는 것은 두 가지다:
- 구체제 서명 기록부의 **판독**(사료 열람·감사): records()·latest_by_node().
- 서명과 무관한 **판독 헬퍼**: 파일의 id 해석(_id_of·locate_by_id), 사건 헤더
  파싱(parse_case). 이들은 write·graph·approvals가 공유한다.

대장·사건부에 적힌 **경로와 사건 번호는 신뢰 밖 입력**이다(다기기 병합).
파일을 읽기 전에 core.resolve_in_root로 vault 안에 봉쇄한다.
"""
from __future__ import annotations
from pathlib import Path

import yaml

from .core import SIGNATURES, ledger_read
from . import contract


def records() -> list[dict]:
    """구체제 서명 기록부 판독 — 사료 열람·감사 전용."""
    return ledger_read(SIGNATURES)


def latest_by_node() -> dict[str, dict]:
    """노드별 마지막 서명 기록(사료 요약). 판정에는 쓰지 않는다 — 서명은
    더 이상 효력의 근거가 아니다."""
    out: dict[str, dict] = {}
    for r in records():
        n = r.get("node")
        if n:
            out[n] = r
    return out


def _id_of(p: Path) -> str | None:
    """파일의 id — 계약 파서 한 갈래로만 읽는다. 정규식 파서를 따로 두면
    따옴표 표기처럼 계약은 통과하는 노드가 판정에서만 미아가 된다."""
    try:
        i = contract.parse(p).id
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        return None
    return i or None


def locate_by_id(node_id: str) -> Path | None:
    """현재 vault에서 id로 노드 파일을 찾는다 — 경로 이동은 동일성을 깨지
    않으므로(경로는 상태) id 해석이 정본이다."""
    from . import graph
    for p, _k in graph.iter_nodes():
        if _id_of(p) == node_id:
            return p
    return None


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
    """사건 파일 머리의 기계 판정 헤더(Mechanism §4 4항)를 구조적으로 파싱.
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
