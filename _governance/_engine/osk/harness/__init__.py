"""osk.harness — 하네스(osk를 부르는 호스트)마다 다른 것을 한 자리에 둔다.

누구인지(이름·판별), 대화 전사가 어디 있는지, 훅이 무엇을 내야 하는지, MCP·훅을
어디에 등록하고 무엇을 신뢰해야 하는지, 어느 판까지 확인했는지를 어댑터가 말한다.
포착(`osk.integration`), 훅 스크립트, `doctor`는 하네스 이름을 직접 가르지 않고 이
등록부에 묻는다. 새 호스트는 어댑터를 더하고, 지원하지 않는 기능은 비워 둔다.

깊은 구현은 제 모듈에 있다 — 전사 형식은 `osk.transcripts`, 구독 fork는
`osk.response_growth`·`osk.native_cli`, 성장 실행의 출력은 `osk.growth`. 어댑터는
fork를 지원하는지만 선언한다(`fork`).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import claude, codex
from .base import MCP_NAME, SCRIPTS, Adapter  # noqa: F401

ADAPTERS = (claude.ADAPTER, codex.ADAPTER)
NAMES = tuple(a.name for a in ADAPTERS)
# 호스트를 판별하지 못했을 때 쓰는 출력 — 두 A 호스트가 함께 받는 계약이다.
FALLBACK = ADAPTERS[0]
_BY_NAME = {a.name: a for a in ADAPTERS}


def get(name: str) -> Adapter:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(f"unknown harness: {name!r}") from None


def fork_names() -> tuple[str, ...]:
    return tuple(a.name for a in ADAPTERS if a.fork)


def session_id(env: dict) -> str | None:
    """훅 입력의 대화 ID — 없으면 어댑터가 환경에서 아는 자기 대화 ID."""
    return (env.get("session_id") or env.get("conversation_id")
            or next((s for s in (a.ambient_session() for a in ADAPTERS) if s), None))


def detect(env: dict, sid: str, path: str | None) -> str | None:
    """하네스 이름 — 명시한 이름, 어댑터가 전사를 읽지 않고 아는 것, 전사의 첫 식별
    행 순이다. 명시한 이름은 그대로 돌려주고 판정은 `integration._identity`가 한다.
    어댑터의 표지(환경의 대화 ID, 전사의 자리·형식)는 서로 배타적이다.

    전사를 읽지 못하면 올린다 — 판별 실패를 추측으로 덮지 않는다."""
    name = env.get("harness") or os.environ.get("OSK_HARNESS")
    if name:
        return name
    for adapter in ADAPTERS:
        if adapter.claims(sid, path):
            return adapter.name
    if path:
        with Path(path).open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                for adapter in ADAPTERS:
                    if adapter.claims_row(row):
                        return adapter.name
    return None


def host_of(env: dict) -> Adapter | None:
    """훅을 부른 호스트 — 실행 기록과 출력 형식에만 쓴다. 대화 ID가 없거나 판별하지
    못하거나 전사를 읽지 못하면 None이다(올리지 않는다)."""
    try:
        sid = session_id(env)
        return _BY_NAME.get(detect(env, sid, env.get("transcript_path"))) if sid else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def newer(version: str | None, verified: str) -> bool:
    """`version`이 확인한 판보다 새 판인가 — 판본 꼴이 아니면 거짓이다."""
    from ..native_cli import SEMVER, _order
    if not (version and re.fullmatch(SEMVER, version) and re.fullmatch(SEMVER, verified)):
        return False
    return _order(version) > _order(verified)
