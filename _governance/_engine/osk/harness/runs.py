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

같은 사건이 두 자리에 등록되면 훅이 같은 입력으로 두 번 불린다. `first_call`이 두 번째
호출을 가려 한 번만 처리하게 하고, 그 사실을 남겨 `doctor`가 겹친 등록을 알린다.
"""
from __future__ import annotations

import json
import os
import sys
import time

from .. import core
from .._portalock import lock_exclusive, unlock

STATE = "osk-hook-runs.json"
# 비Git vault의 대체 자리는 이름의 stem으로 갈리므로(core.local_lock_path) 세 이름의 stem이 달라야 한다.
LOCK = "osk-hook-recording.lock"
CLAIMS = "osk-hook-claims.json"   # {"claims": {지문: at}, "duplicates": {"<호스트>/<사건>": at}}
KEEP = 64        # overview를 기억하는 세션 수
WAIT = 0.5       # 잠금을 기다리는 최대 시간(초)
# 두 등록의 호출은 함께 오거나(병렬) 앞 호출이 끝나자마자 온다(순차). 다음 턴은 전사가 자라
# 입력이 달라지므로 긴 창도 다음 턴을 가리지 않는다. 전사 크기를 잴 수 없는 입력은 같은
# 입력의 두 턴을 가르지 못하므로, 거의 함께 온 호출만 겹친 것으로 본다.
WINDOW = 120     # 같은 호출로 보는 시간(초)
BLIND = 5        # 전사 크기를 잴 수 없을 때의 창(초)


def _load(name: str, keys: tuple[str, ...]) -> dict:
    try:
        data = json.loads(core.local_lock_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: data[k] if isinstance(data.get(k), dict) else {} for k in keys}


def read() -> dict:
    """`{"runs": {"<호스트>/<사건>": {at, session, python}}, "overview": {세션: at}}`."""
    return _load(STATE, ("runs", "overview"))


def duplicates() -> dict:
    """`{"<호스트>/<사건>": at}` — 같은 호출이 두 번 들어와 한 번만 처리한 마지막 시각."""
    return _load(CLAIMS, ("claims", "duplicates"))["duplicates"]


def _update(change, name: str = STATE, keys: tuple[str, ...] = ("runs", "overview")) -> bool:
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
            data = _load(name, keys)
            change(data)
            core.atomic_write(core.local_lock_path(name),
                              json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        finally:
            unlock(f)
    return True


def first_call(host: str | None, event: str, env: dict) -> bool:
    """이 사건의 첫 호출인가. 같은 호스트·사건·입력(전사 크기 포함)이 창 안에 이미
    왔으면 거짓이다 — 두 자리에 등록된 훅의 두 번째 호출이다. 그때는 그 사실을 남겨
    `doctor`가 겹친 등록을 알린다. 판정이나 기록을 못 하면 참이다: 중복을 놓치는
    편이 호출을 잃는 편보다 낫다."""
    size = None
    path = env.get("transcript_path") if isinstance(env, dict) else None
    if isinstance(path, str) and path:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
    name = f"{host or 'unknown'}/{event}"
    key = core.sha256_bytes(json.dumps([name, env, size], ensure_ascii=False, sort_keys=True,
                                       default=str).encode("utf-8"))
    window = WINDOW if size is not None else BLIND
    now, first = time.time(), [True]

    def change(data):
        live = {k: v for k, v in data["claims"].items()
                if isinstance(v, (int, float)) and 0 <= now - v < WINDOW}
        at = live.get(key)
        if at is not None and now - at < window:
            first[0] = False
            data["duplicates"][name] = now
        else:
            live[key] = now
        data["claims"] = live
    try:
        _update(change, CLAIMS, ("claims", "duplicates"))
    except OSError:
        return True
    return first[0]


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
