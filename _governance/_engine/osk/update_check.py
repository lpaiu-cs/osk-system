"""osk.update_check — 새 정식 릴리스를 알린다. 받지도 적용하지도 않는다.

정본에는 태그 목록만 묻는다(`git ls-remote`) — vault의 내용은 보내지 않는다.
확인 결과와 알림 표식은 기기 로컬이다(`core.local_lock_path`): 동기화되지 않고
추적 트리에 들어가지 않는다. 비교할 판본은 동기화되는 갱신 저널에서 매번 다시
읽는다 — 다른 기기가 갱신해 그 저널이 들어오면, 확인을 다시 하지 않아도 알림이
사라진다.

훅과 표면은 확인을 기다리지 않는다. 지난 확인이 낡았으면 분리 프로세스
(`python -m osk.update_check`)를 띄우고, 이번 세션에는 지난 결과만 싣는다. 적용은
사용자가 요청할 때 `osk.update`의 확인 관문(Mechanism §1-2 3항)이 맡는다.

자동 확인과 알림은 끌 수 있다 — 환경 `OSK_UPDATE_CHECK=0`, `.osk/config.json`의
`"update_check": false`. 판본을 고정했거나(pin) 출처가 bundle이면 하지 않는다.
직접 부른 `osk.update --check`는 이 설정과 무관하게 묻는다.
"""
from __future__ import annotations
import json
import os
import subprocess
import time
from datetime import datetime

from . import core, update
from ._portalock import lock_exclusive, unlock

# 기기 로컬 자리. 비Git vault의 대체 자리는 이름의 stem으로 갈리므로
# (core.local_lock_path) 세 이름의 stem이 서로 달라야 한다.
STATE = "osk-release-check.json"     # 마지막 확인 {at, url, latest, error}
NOTICE = "osk-release-notice.json"   # 마지막 알림 {version, at}
LOCK = "osk-release-checking.lock"   # 한 번에 한 확인
CHECK_EVERY = 24 * 3600              # 성공한 확인이 유효한 시간(초)
RETRY_AFTER = 3600                   # 실패한 확인을 다시 하기까지(초)
# 같은 릴리스를 다시 알리기까지(초) — 매일 같은 시각에 여는 세션이 하루를
# 건너뛰지 않도록 하루보다 짧다.
NOTICE_EVERY = 20 * 3600
TIMEOUT = 20                         # 분리 프로세스의 태그 조회 한도(초)


def _read(name: str) -> dict:
    try:
        data = json.loads(core.local_lock_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(name: str, data: dict) -> None:
    core.atomic_write(core.local_lock_path(name),
                      json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _upstream() -> dict:
    """`osk.update`가 읽는 것과 같은 출처 설정(`.osk/config.json`의 upstream)."""
    return update.load_config().get("upstream", {})


def _url(up: dict) -> str:
    return up.get("url", update.DEFAULT_UPSTREAM)


def _kst(at) -> str | None:
    if not isinstance(at, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(at, core.KST).strftime(core.TS_FMT)
    except (OverflowError, OSError, ValueError):
        return None


def current() -> str | None:
    """이 vault의 판본 — 갱신 저널의 인과 극대. 기록이 없거나 정할 수 없으면
    (분기·손상) None이고, 그때는 비교하지 않는다."""
    try:
        return update.current_version()
    except (OSError, ValueError):
        return None


def newer(latest, cur) -> bool:
    """`latest`가 `cur`보다 새 정식 판본인가 — 둘 다 `vX.Y.Z`일 때만 참이다."""
    a, b = update._semver(latest), update._semver(cur)
    return bool(a and b and a > b)


def off_reason() -> str | None:
    """자동 확인·알림을 하지 않는 이유 — 하면 None."""
    if os.environ.get("OSK_UPDATE_CHECK") == "0":
        return "환경 OSK_UPDATE_CHECK=0"
    try:
        cfg = update.load_config()
    except update.UpdateError as e:
        return str(e)
    if cfg.get("update_check") is False:
        return ".osk/config.json의 update_check가 false다"
    up = cfg.get("upstream", {})
    if up.get("source", "git") != "git":
        return "갱신 출처가 git이 아니다 — bundle은 오프라인 반입이다"
    if up.get("pin"):
        return f"판본을 {up['pin']}에 고정했다(pin)"
    if current() is None:
        return "갱신 저널에서 판본을 정할 수 없다 — 릴리스 기준선이 없거나 저널이 갈라졌다"
    return None


def due(state: dict, url: str, now: float | None = None) -> bool:
    """확인할 때인가 — 지난 확인이 없거나, 출처가 바뀌었거나, 유효 시간이 지났다."""
    now = time.time() if now is None else now
    at = state.get("at")
    if state.get("url") != url or not isinstance(at, (int, float)):
        return True
    return not 0 <= now - at < (RETRY_AFTER if state.get("error") else CHECK_EVERY)


def check(*, unattended: bool = False) -> dict:
    """정본의 최신 정식 태그를 묻고 결과를 이 기기에 남긴다. 적용은 하지 않는다.

    `unattended`는 분리 프로세스의 확인이다 — 다른 확인이 진행 중이거나 방금
    끝났으면 곧바로 물러나고, 사람의 입력을 기다리지 않는다. 실패는 기록하되 지난
    성공의 `latest`는 지우지 않는다 — 잠깐 오프라인이라고 알림이 사라지지 않는다."""
    url = _url(_upstream())
    with open(core.local_lock_path(LOCK), "w") as f:
        try:
            lock_exclusive(f, blocking=not unattended)
        except OSError:
            return report()                  # 다른 확인이 돌고 있다
        try:
            prev = _read(STATE)
            if unattended and not due(prev, url):
                return report(prev)
            state = {"at": time.time(), "url": url, "error": None,
                     "latest": prev.get("latest") if prev.get("url") == url else None}
            try:
                state["latest"] = update.latest_release_tag(
                    url, timeout=TIMEOUT if unattended else 60, unattended=unattended)
            except (update.UpdateError, OSError, ValueError,
                    subprocess.TimeoutExpired) as e:
                state["error"] = f"{type(e).__name__}: {e}"[:300]
            _write(STATE, state)
        finally:
            unlock(f)
    return report(state)


def report(state: dict | None = None) -> dict:
    """마지막 확인과 지금 판본의 비교 — `osk.update --check`와 `osk.cli status`가
    싣는다. 네트워크에 닿지 않는다."""
    state = _read(STATE) if state is None else state
    cur, latest = current(), state.get("latest")
    out = {"current": cur, "latest": latest, "available": newer(latest, cur),
           "checked_at": _kst(state.get("at")), "error": state.get("error")}
    reason = off_reason()
    if reason:
        out["auto_off"] = reason
    return out


def available() -> dict | None:
    """알릴 새 릴리스 `{latest, current}` — 없거나 자동 확인을 끈 설치면 None."""
    if off_reason():
        return None
    state = _read(STATE)
    if state.get("url") != _url(_upstream()):
        return None                          # 다른 출처의 결과로는 알리지 않는다
    cur, latest = current(), state.get("latest")
    return {"latest": latest, "current": cur} if newer(latest, cur) else None


def ensure_fresh():
    """지난 확인이 낡았으면 분리 프로세스로 확인을 띄우고 기다리지 않는다. 띄운
    프로세스를 돌려준다 — 띄우지 않았으면 None."""
    if off_reason() or not due(_read(STATE), _url(_upstream())):
        return None
    return core.spawn_worker("osk.update_check")


def _claim(version: str) -> bool:
    """이 기기가 이 릴리스를 `NOTICE_EVERY` 안에 알리지 않았으면 표식을 남기고
    참이다. 훅(사용자 화면)과 표면(`overview`)이 이 표식 하나를 나눠 쓴다."""
    seen, now = _read(NOTICE), time.time()
    at = seen.get("at")
    if (seen.get("version") == version and isinstance(at, (int, float))
            and 0 <= now - at < NOTICE_EVERY):
        return False
    _write(NOTICE, {"version": version, "at": now})
    return True


def _how(latest: str) -> str:
    return (f"갱신은 사용자가 요청할 때만 한다. 이 vault를 여러 기기에서 쓰면 갱신은 한 "
            f"기기에서만 한다 — 다른 기기가 이미 갱신했으면 동기화 뒤 이 알림이 사라지고, "
            f"이 기기에서는 서버·데몬 재시작과 requirements.txt가 바뀌었을 때의 pip 재실행만 "
            f"한다. 요청을 받으면 "
            f"`{core.cli_command('update', '--to', latest, '--apply')}`를 실행한다. 첫 "
            f"실행은 변경집합만 내고 종료코드 2로 멈추니, 출력의 instruction대로 설명하고 "
            f"재승인을 받는다.")


def session_notice() -> tuple[str, str]:
    """(에이전트 문맥, 사용자 화면) — 알릴 새 릴리스가 있고 이 기기에서 아직 알리지
    않았을 때만. 사용자 화면의 문구는 모델 문맥이 아니다(훅의 `systemMessage`)."""
    avail = available()
    if not avail or not _claim(avail["latest"]):
        return "", ""
    latest, cur = avail["latest"], avail["current"]
    user = (f"osk-system 새 릴리스 {latest} (이 vault는 {cur}) — 적용하려면 에이전트에게 "
            f"\"osk 업데이트해 줘\"라고 요청한다.")
    agent = (f"[osk 새 릴리스 — {latest} · 이 vault {cur}] 사용자 화면에 같은 알림을 "
             f"띄웠다. 먼저 권하거나 실행하지 않는다. " + _how(latest))
    return agent, user


def surface() -> dict | None:
    """`overview`의 `update` 필드 — 알릴 새 릴리스가 있을 때만. 훅이 없는 하네스도
    이 길로 알게 된다: 이 기기에서 아직 알리지 않았으면 `notify`를 싣는다."""
    avail = available()
    if not avail:
        return None
    out = {**avail, "how": _how(avail["latest"])}
    if _claim(avail["latest"]):
        out["notify"] = "이 기기에서 아직 알리지 않았다 — 사용자에게 한 줄로 알린다"
    return out


def main() -> None:
    """분리 프로세스의 확인. 끈 설치이거나 아직 유효한 확인이 있으면 하지 않는다."""
    if off_reason() is None:
        check(unattended=True)


if __name__ == "__main__":
    main()
