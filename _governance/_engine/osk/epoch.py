"""osk.epoch — 이 프로세스가 뜬 시각의 **엔진 판**.

수트는 디스크의 코드를 시험하지 **돌고 있는 프로세스**를 시험하지 못한다.
이 체계는 그 사각에서 두 번 다쳤다 — 구획을 이관한 뒤에도 갱신 전에 뜬
서버들이 구판 코드를 메모리에 쥔 채 구 경로에 계속 써서 하루치 기록 5건이
갈라졌고, 술어 체계를 전환한 지 14시간 지나 만들어진 노드가 폐지된 술어를
달고 태어났다. 판정 기록은 그래서 이렇게 적었다: *"갱신·이관의 완료는 파일이
아니라 프로세스로 판정한다."* 그 판정을 **실행 규칙**으로 만드는 것이 이
모듈이다.

판의 재료는 저장소 HEAD가 아니라 **엔진 파일 자신의 내용**이다. HEAD는 두
방향으로 거짓을 말한다 — 데이터만 커밋해도 움직이고(엔진은 그대로인데 새
판으로 보인다), 릴리스 없이 엔진을 직접 고치면 움직이지 않는다(엔진이
바뀌었는데 옛 판으로 보인다). 둘 다 이 저장소에서 실측됐다. 그리고 이 재료는
비용이 저장소 크기가 아니라 **엔진 크기**에 묶인다 — 노드가 12개든 20,000개든
판을 재는 값이 같음을 세 규모에서 확인했다(26 파일 · 418 KB · 3~5 ms).

**"적재한 것"과 같지는 않다.** 이 프로세스가 실제로 import한 모듈만이 아니라
`_engine`의 `.py` 전부를 센다 — 서버가 결코 들이지 않는 `sync_daemon.py`나
`scripts/*.py`를 고쳐도 낡음으로 판정되어 쓰기가 막힌다. 보수적으로 틀리는
쪽이라 그대로 두지만, 범위가 필요보다 넓다는 것은 적어 둔다.

적재판은 **import 시각에 한 번** 잡는다. 그 순간 인터프리터가 방금 읽은
바이트이므로, 이 프로세스가 실제로 무엇을 실행하고 있는지에 가장 가깝다.
다시 재지 않는다 — 다시 재면 디스크를 따라가 버려서 낡음이 사라진다.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# `osk/epoch.py` → `osk/` → `_engine/`. ROOT(vault)와 무관하게 잡는다 —
# 판은 엔진의 성질이지 저장소의 성질이 아니고, 수트가 vault를 임시 디렉토리로
# 옮겨 세워도 도는 엔진은 같은 것이어야 한다. (실 배치에서 `ENGINE_ROOT`는
# `<vault>/_governance/_engine`이므로 ROOT **아래**다 — 무관하다는 것은
# 위치가 아니라 판정이 vault 내용에 흔들리지 않는다는 뜻이다.)
ENGINE_ROOT = Path(__file__).resolve().parent.parent

# 판에서 빼는 구획. `tests`는 **판정을 위해** 뺀다 — 시험을 고쳐도 엔진의
# 행동은 그대로이고, 넣으면 수트를 손댈 때마다 살아 있는 서버가 통째로 낡은
# 것이 된다. 나머지(`__pycache__`·가상환경·`.git`)는 판정이 아니라 **순회
# 비용**의 문제다: 아래가 `.py`만 모으므로 빼지 않아도 판은 같지만, 빼지
# 않으면 매 mutation마다 그 트리를 훑는다.
_SKIP_DIRS = frozenset({"__pycache__", "tests", ".venv", "venv", ".git",
                        ".pytest_cache"})

UNKNOWN = "unknown"


class EpochError(RuntimeError):
    """판을 재지 못했다 — 낡았는지 **말할 수 없다**. 모르는 채로 쓰는 것이
    이 모듈이 막으려는 바로 그 일이므로, 호출자는 이것을 거부로 다룬다."""


def _raise(err: OSError):
    raise err


def engine_files() -> list[Path]:
    """판에 산입하는 파일 — 엔진의 `.py` 전부.

    정렬은 `_engine` 상대경로의 POSIX 표기 기준이다. 디렉토리 열거 순서는
    파일시스템마다 다르므로, 정렬하지 않으면 같은 코드가 기기마다 다른 판을
    낸다.

    **열거 실패를 삼키지 않는다.** `os.walk`의 기본값(`onerror=None`)은 어떤
    오류든 조용히 건너뛰는 것이고, 그러면 파일 0개가 나와 빈 바이트열의
    해시(`e3b0c44298fc`)가 **정상 판 행세**를 한다. 적재판과 디스크판이 둘 다
    그 값이 되므로 그 프로세스는 영원히 "안 낡음"이 되고 관문이 통째로
    no-op가 된다 — 이 모듈이 선언한 fail-closed의 정반대다. 그래서 열거
    실패도, 결과가 빈 것도 오류로 올린다."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ENGINE_ROOT, onerror=_raise):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                out.append(Path(dirpath) / f)
    if not out:
        raise OSError(f"엔진 `.py`를 하나도 찾지 못했다: {ENGINE_ROOT} — "
                      f"판을 잴 수 없다")
    return sorted(out, key=lambda p: p.relative_to(ENGINE_ROOT).as_posix())


def _digest() -> str:
    """파일 집합의 판. 경로를 내용과 함께 넣어야 **파일의 추가·삭제·개명**도
    판을 바꾼다 — 내용만 이으면 모듈 하나가 통째로 사라져도 같은 판이 된다.
    구분자를 넣는 것은 경계 없이 이으면 서로 다른 집합이 같은 바이트열이 되기
    때문이다."""
    h = hashlib.sha256()
    for p in engine_files():
        h.update(p.relative_to(ENGINE_ROOT).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


def _measure() -> str:
    try:
        return _digest()
    except OSError:
        return UNKNOWN


# import 시각의 판 — 이 프로세스의 신분이다.
_LOADED = _measure()


def loaded() -> str:
    """이 프로세스가 뜬 시각의 판. 재지 못했으면 `UNKNOWN`이다."""
    return _LOADED


def on_disk() -> str:
    """지금 디스크에 있는 판. 잴 수 없으면 거부한다 — 엔진 파일을 읽지 못하는
    상황은 정상이 아니고(갱신은 잠금 안에서 돌므로 이 자리와 겹치지 않는다),
    그런 자리에서 모르는 채 쓰는 것보다 멈추는 편이 싸다."""
    d = _measure()
    if d == UNKNOWN:
        raise EpochError(
            f"엔진 파일을 읽지 못해 디스크의 판을 잴 수 없다: {ENGINE_ROOT}")
    return d


def stale() -> bool:
    """이 프로세스가 낡았는가 — 적재판과 디스크판이 다른가."""
    if _LOADED == UNKNOWN:
        raise EpochError(
            "이 프로세스는 시작할 때 자기 판을 잡지 못했다 — 낡았는지 판정할 "
            "수 없다")
    return _LOADED != on_disk()
