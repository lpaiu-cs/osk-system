"""osk.signatures — 구체제 서명 기록부(사료)와 판독 헬퍼.

서명 제도는 폐지됐다(헌법 14조 6항 · Mechanism §3 9항). `signatures.jsonl`은
**사료로 동결**한다 — 새 기록을 추가하지 않으며 어떤 판정의 근거도 아니다.
새 체제의 권위는 보호영역의 승인본으로만 성립한다(osk.approvals).

이 모듈이 남기는 것은 두 가지다:
- 구체제 서명 기록부의 **판독**(사료 열람·감사): records()·latest_by_node().
- 서명과 무관한 **판독 헬퍼**: 파일의 id 해석(id_of), 사건 헤더
  파싱(parse_case). 이들은 write·graph·validate가 공유한다.

대장·사건부에 적힌 **경로와 사건 번호는 신뢰 밖 입력**이다(다기기 병합).
파일을 읽기 전에 core.resolve_in_root로 vault 안에 봉쇄한다.
"""
from __future__ import annotations
import functools
from pathlib import Path

from .core import ledger_read
from . import contract
from .vault import Vault


def records(vault: Vault) -> list[dict]:
    """구체제 서명 기록부 판독 — 사료 열람·감사 전용."""
    return ledger_read(vault.signatures)


def latest_by_node(vault: Vault) -> dict[str, dict]:
    """노드별 마지막 서명 기록(사료 요약). 판정에는 쓰지 않는다 — 서명은
    더 이상 효력의 근거가 아니다."""
    out: dict[str, dict] = {}
    for r in records(vault):
        n = r.get("node")
        if n:
            out[n] = r
    return out


def id_of(p: Path) -> str | None:
    """파일의 id — 계약 파서 한 갈래로만 읽는다. 정규식 파서를 따로 두면
    따옴표 표기처럼 계약은 통과하는 노드가 판정에서만 미아가 된다."""
    import yaml
    try:
        i = contract.parse(p).id
    except (OSError, ValueError, AttributeError, yaml.YAMLError):
        return None
    return i or None


# 로더는 처음 판독할 때 만든다 — yaml의 import는 20 ms 남짓이라 사건을 읽지 않는
# 프로세스(입력 훅)가 치르지 않게 한다(#192).
@functools.cache
def _strict_loader() -> type:
    import yaml

    class _StrictLoader(yaml.SafeLoader):
        """중복 키를 오류로 만드는 로더 — 뒤에 오는 줄이 판정 필드를 조용히
        덮어쓰는 것(마지막 값 승리)을 막는다."""

    _StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)
    return _StrictLoader


def _no_dup_mapping(loader, node):
    import yaml
    keys = set()
    for k, _v in node.value:
        key = loader.construct_object(k)
        if key in keys:
            raise yaml.YAMLError(f"중복 키: {key}")
        keys.add(key)
    return loader.construct_mapping(node)


def parse_case(case_path: Path) -> dict | None:
    """사건 파일 머리의 기계 판정 헤더(Mechanism §4 4항)를 구조적으로 파싱.
    헤더는 첫 빈 줄까지다. frontmatter가 아니므로 `---`로 감싸지 않는다 —
    감싼 파일은 대장 구획의 노드형 파일로 배치 검사가 잡는다. 중복 키는 거부한다."""
    try:
        text = case_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if text.startswith("---\n"):
        return None
    head = text.split("\n\n", 1)[0]
    import yaml
    try:
        data = yaml.load(head, Loader=_strict_loader())
    except (yaml.YAMLError, TypeError, RecursionError):      # 깊이 겹친 머리는 순수 로더가 RecursionError를 낸다
        return None
    return data if isinstance(data, dict) else None
