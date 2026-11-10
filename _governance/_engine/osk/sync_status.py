"""osk.sync_status — 동기화 데몬의 잠금 자리와 마지막 주기의 기록.

데몬(`sync_daemon.py`)은 진입점이다. 갱신·설치·점검이 데몬이 쥔 잠금과 데몬이 남긴 결과를 읽으려고
진입점을 import하지 않게, 둘이 함께 쓰는 것을 여기에 둔다. 자리는 엔진이 정한다(`core.local_lock_path`).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import core


def lock_path(root: Path, name: str = "osk-sync.lock") -> Path:
    """잠금 파일의 경로 — 자리는 **엔진이 정한다**(`core.local_lock_path`).
    `name`으로 잠금을 구분한다: `osk-sync.lock`은 데몬 싱글턴(한 기기 한 데몬),
    `osk-mutation.lock`은 엔진의 working-tree 변경 잠금과 **같은 파일**이다.

    동기화는 이 체계의 필수 구성요소가 아니라 쓰는 사람만 쓰는 편의 모듈이므로,
    잠금 자리를 여기서 따로 정하면 엔진과 갈라져 상호배제가 조용히 깨진다 —
    정의는 엔진에 한 벌만 둔다. 엔진의 해석기는 git을 **실행하지 않고**
    파일시스템으로 읽으므로, 매 tick마다 git.exe를 띄우던 비용과 Windows
    콘솔 깜빡임도 함께 사라진다."""
    return core.local_lock_path(name, root)


# 마지막 주기의 결과 — 기기 로컬(`core.local_lock_path`). 실패가 데몬 로그에만 묻히면 사용자는 백업이
# 멈춘 줄 모른다(v5.4의 전송 보류가 그 예다). 세션 시작 훅과 doctor가 `trouble`로 읽는다.
STATUS = "osk-sync-status.json"
STALE = 24 * 3600                       # 이보다 오래 성공하지 못했거나 돌지 않았으면 멈춘 것이다


def last(root: Path) -> dict | None:
    """마지막 주기의 기록 `{at, status, since}` — 없으면 이 기기는 데몬을 쓰지 않는다."""
    try:
        data = json.loads(lock_path(root, STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("at"), (int, float)) else None


def _when(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%m-%d %H:%M")


def trouble(root: Path, now: float | None = None) -> str | None:
    """동기화가 멈췄으면 그 사유 한 줄, 아니면 None. 전송 보류는 사람이 setup으로 풀어야 하므로
    곧바로, 그 밖의 실패는 하루 넘게 이어졌을 때, 기록이 하루 넘게 낡았으면 데몬이 돌지 않는 것이다."""
    rec = last(root)
    if not rec:
        return None
    now = time.time() if now is None else now
    if now - rec["at"] > STALE:
        return f"데몬이 {_when(rec['at'])} 뒤로 돌지 않았다"
    status, since = str(rec.get("status", "")), rec.get("since")
    if status.startswith("전송 보류"):
        return status
    if isinstance(since, (int, float)) and now - since > STALE:
        return f"{_when(since)}부터 동기화하지 못했다 — 마지막 결과: {status}"
    return None


def record(root: Path, status: str) -> None:
    # ok가 아니면 무엇이든(잠금 경합·미완료 트랜잭션 포함) 실패가 이어진 시작을 잰다. 일시적인 것은
    # 다음 ok가 지우고, 하루를 넘겨야 알리므로(`trouble`) 경합이 경고가 되지는 않는다.
    prev = last(root) or {}
    now = time.time()
    since = None if status.startswith("ok") else (prev.get("since") or now)
    try:
        core.atomic_write(lock_path(root, STATUS),
                          json.dumps({"at": now, "status": status, "since": since},
                                     ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass        # 기록을 못 남겨도 동기화는 돈다 — 보이지 않을 뿐이다
