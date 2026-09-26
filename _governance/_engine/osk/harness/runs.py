"""osk.harness.runs — 이 기기에서 훅이 실제로 불렸는지, 세션 시작 뒤 overview가 따라왔는지.

등록 파일은 훅이 불린다는 것을 증명하지 못한다. 호스트가 신뢰하지 않았거나(Codex),
세션을 새로 열지 않았거나(Claude Code), 등록이 문서 밖의 자리에 있을 수 있다. 그래서
훅이 불릴 때마다 호스트·사건별 마지막 시각을 남기고 `doctor`가 등록과 대조한다.

문맥이 모델까지 닿았는지는 직접 알 수 없다. 세션 시작 훅은 `overview(session=…)`를
부르라고 싣으므로, 그 세션 키의 overview가 그 뒤에 불렸으면 닿았다고 짐작한다.

기록은 기기 로컬(`core.local_lock_path`)이며 동기화되지 않는다. 호스트 이름·세션 키·
그 훅을 돌린 Python 경로와 시각만 적고 대화 내용은 적지 않는다. 기록은 잠금을 잠깐만
기다리고, 못 적으면 거짓을 돌려준다 — 훅과 표면을 막지 않는다.

구독 fork·정기 실행(`OSK_GROWTH_WORKER=1`)이 부른 훅과 표면은 적지 않는다. 그 실행의
증거는 그 실행의 것이고, 적으면 사용자 세션의 전달을 fork의 `overview`가 대신 증언한다.
"""
from __future__ import annotations

import json
import os
import sys
import time

from .. import core
from .._portalock import lock_exclusive, unlock

STATE = "osk-hook-runs.json"
# 비Git vault의 대체 자리는 이름의 stem으로 갈리므로(core.local_lock_path) STATE와 stem이 달라야 한다.
LOCK = "osk-hook-recording.lock"
KEEP = 64        # overview를 기억하는 세션 수
WAIT = 0.5       # 잠금을 기다리는 최대 시간(초)


def read() -> dict:
    """`{"runs": {"<호스트>/<사건>": {at, session, python}}, "overview": {세션: at}}`."""
    try:
        data = json.loads(core.local_lock_path(STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: data[k] if isinstance(data.get(k), dict) else {} for k in ("runs", "overview")}


def _update(change) -> bool:
    if os.environ.get("OSK_GROWTH_WORKER") == "1":
        return False
    with open(core.local_lock_path(LOCK), "w") as f:
        deadline = time.monotonic() + WAIT
        while True:
            try:
                lock_exclusive(f, blocking=False)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.02)
        try:
            data = read()
            change(data)
            core.atomic_write(core.local_lock_path(STATE),
                              json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        finally:
            unlock(f)
    return True


def record_run(host: str | None, event: str, session: str | None) -> bool:
    """훅 하나가 불렸다. 호스트를 알아보지 못했으면 `unknown`으로 적는다."""
    entry = {"at": time.time(), "session": session, "python": sys.executable}

    def change(data):
        data["runs"][f"{host or 'unknown'}/{event}"] = entry
    return _update(change)


def record_overview(session: str) -> bool:
    """그 세션 키로 overview가 불렸다. 오래된 세션부터 잊는다."""
    at = time.time()

    def change(data):
        seen = data["overview"]
        seen[session] = at
        stamp = {k: v if isinstance(v, (int, float)) else 0 for k, v in seen.items()}
        for key in sorted(stamp, key=stamp.get)[:-KEEP]:
            del seen[key]
    return _update(change)
