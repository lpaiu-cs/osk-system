"""osk.locks — 잠금과 판본 관문.

쓰기를 한 줄로 세우는 저장소 잠금(`mutation_lock`), 이 기기에서만 겹치지 않으면 되는 일의 잠금(`device_lock`),
동기화되지 않는 자리에 두는 잠금 파일의 경로(`local_lock_path`). 저장소 잠금은 들어설 때 엔진의 판본을
확인한다 — 뜬 뒤에 디스크의 엔진이 바뀐 프로세스는 쓰지 못한다(`StaleEngineError`, `osk.epoch`)."""
from __future__ import annotations
import functools, hashlib, tempfile
from contextlib import contextmanager
from pathlib import Path
from . import epoch
from .vault import Vault
from ._portalock import lock_exclusive, unlock


def local_lock_path(name: str, root: Path) -> Path:
    """기기 로컬 잠금 파일의 경로 — **추적 트리 밖으로만** 고른다.

    엔진의 primitive다. 동기화는 이 체계의 필수 구성요소가 아니라 쓰는 사람만
    쓰는 편의 모듈이므로, 엔진이 자기 잠금 자리를 알기 위해 그쪽을 부르지
    않는다(의존은 반대 방향이다 — 동기화가 이 함수를 쓴다).

    자리는 git 디렉터리를 **파일시스템으로 읽어** 정한다(git 실행 없음 —
    데몬 tick마다 subprocess를 띄우지 않고 Windows 콘솔 깜빡임도 없다):
    `<root>/.git`이 디렉터리면 그것, 파일이면 `gitdir:` 대상(그 안에
    `commondir`가 있으면 worktree이므로 그쪽을 따라간다). git이 없으면 루트
    경로 해시를 키로 한 임시 디렉터리다(기기 안 vault별 유일).

    추적 트리 안에 두지 않는 이유: `git add -A`에 딸려 들어가고, checkout이
    그 pathname의 inode를 갈면 "같은 경로 = 같은 mutex" 전제가 깨진다."""
    r = Path(root)
    dot = r / ".git"
    try:
        if dot.is_dir():
            return dot / name
        if dot.is_file():
            head = dot.read_text(encoding="utf-8").strip()
            if head.startswith("gitdir:"):
                g = Path(head.split(":", 1)[1].strip())
                if not g.is_absolute():
                    g = (r / g).resolve()
                common = g / "commondir"
                if common.is_file():
                    c = Path(common.read_text(encoding="utf-8").strip())
                    g = c if c.is_absolute() else (g / c).resolve()
                if g.is_dir():
                    return g / name
    except OSError:
        pass                                  # 판독 불가 → 임시 디렉터리로
    key = hashlib.sha256(str(r).encode("utf-8")).hexdigest()[:16]
    stem = name.rsplit(".", 1)[0]
    return Path(tempfile.gettempdir()) / f"{stem}-{key}.lock"


@functools.cache
def mutation_lock_path(vault: Vault) -> Path:
    """working-tree 변경 상호배제 잠금의 경로 — 동기화(sync·update)가 잡는
    `osk-mutation.lock`과 **같은 파일**이다.

    이름을 공유해야 데몬의 `pull(rebase)`가 반려의 파괴 구간과 경합하지 않는다.
    저장소마다 한 번만 해석한다 — 이 잠금은 모든 노드 쓰기가 잡는다."""
    return local_lock_path("osk-mutation.lock", vault.root)


class StaleEngineError(RuntimeError):
    """이 프로세스가 적재한 엔진이 디스크의 것과 다르다 — 변경을 막았다.

    `write.WriteError`와 같은 모양(`violations`·`extra`)을 갖는다. 표면의
    거부 렌더러가 한 벌로 두 가지를 다루게 하기 위함이며, 호출자에게는 둘 다
    "아무것도 쓰지 않았다"로 같다."""

    def __init__(self, message: str, violations: list[str] | None = None):
        super().__init__(message)
        self.violations = [message] + list(violations or [])
        self.extra: dict = {}


def _fence() -> None:
    """이 프로세스가 낡았으면 아무것도 쓰지 못하게 한다.

    **잠금을 잡은 직후**에 판정한다 — 잠금 밖에서 재면 재고 나서 잡기까지
    사이에 갱신이 끼어들 수 있고, 그러면 판정은 통과했는데 쓰기는 낡은
    코드가 하는 창이 열린다.

    갱신(`update`)과 동기화(`sync`)는 이 관문을 지나지 않는다. 그 둘은 잠금
    파일만 공유할 뿐 이 클래스를 쓰지 않으며, 그래야 한다 — 엔진을 교체하는
    당사자를 낡음으로 막으면 갱신에서 빠져나올 길이 없다."""
    # 판은 **한 번만** 잰다. 두 번 재면 그 사이에 갱신이 끝났을 때 거부문이
    # `적재판 X ≠ 디스크판 X`처럼 같은 값 둘을 찍어 스스로를 반박한다.
    try:
        loaded = epoch.loaded()
        if loaded == epoch.UNKNOWN:
            raise epoch.EpochError(
                "이 프로세스는 시작할 때 자기 판을 잡지 못했다 — 낡았는지 "
                "판정할 수 없다")
        disk = epoch.on_disk()
    except epoch.EpochError as e:
        raise StaleEngineError(
            "엔진 판을 확인하지 못했다 — 쓰지 않았다",
            [str(e),
             "엔진 파일을 읽을 수 있는지 확인한 뒤 이 서버를 재기동하고 같은 "
             "요청을 그대로 다시 보내라. 판을 모르는 채로 쓰는 것이 이 관문이 "
             "막으려는 일이므로, 모름은 통과가 아니라 거부다."]) from e
    if loaded == disk:
        return
    raise StaleEngineError(
        "이 프로세스는 낡은 엔진으로 돌고 있다 — 쓰지 않았다",
        [f"적재판 {loaded} ≠ 디스크판 {disk}. 이 프로세스가 뜬 뒤 엔진이 "
         f"교체됐다(갱신 또는 다른 기기의 변경을 받은 pull). 이 서버를 "
         f"재기동한 뒤 같은 요청을 그대로 다시 보내라 — 낡은 코드가 쓰면 "
         f"폐지된 규칙을 단 노드가 조용히 태어나고, 어느 프로세스가 그랬는지 "
         f"소급 확인이 불가능하다."])


class mutation_lock:
    """엔진이 내는 **모든 working-tree 변경**을 직렬화하는 잠금 — 노드
    쓰기(write)·보호영역 조작(approvals)·동기화(sync·update)가 같은 잠금을 쓴다.

    다른 잠금을 쓰면, 반려가 마지막으로 전제를 확인한 뒤 파일을 덮기까지 사이에
    정상 쓰기나 데몬의 `pull(rebase)`가 끼어들어 **검토하지 않은 변경이
    파괴된다**. 잠금 순서는 언제나 이 잠금 → 대장 잠금(`ledger_append`)이다
    (모든 모듈이 같은 순서라 교착이 없다). 다른 기기에서 사람이 직접 부른
    `git pull`만 이 잠금 밖이며, 그것은 `expect`·전제 재확인이 맡는다.

    **판본 관문이 여기 선다.** 잠금이 모든 의미론적 변경의 유일한 길목이므로,
    관문을 여기 두면 쓰는 쪽이 그것을 잊을 수 없다 — 호출부마다 걸면 새 쓰기
    통로가 생길 때 빠뜨리는 것이 언제나 가능해진다."""

    def __init__(self, vault: Vault):
        self.vault = vault

    def __enter__(self):
        path = mutation_lock_path(self.vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "w")
        lock_exclusive(self._f)
        try:
            _fence()
        except BaseException:
            # `__enter__`가 던지면 `with`는 `__exit__`를 부르지 않는다 —
            # 여기서 풀지 않으면 관문에 걸릴 때마다 잠금이 새어 다음 호출이
            # 영영 막힌다.
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        # 해제가 던져도 핸들은 닫는다 — 안 닫으면 예외 종류가 바뀌어 호출부가
        # 재기동 안내 대신 errno를 받고, 핸들도 참조계수에 기대게 된다.
        try:
            unlock(self._f)
        finally:
            self._f.close()
        return False


@contextmanager
def device_lock(vault: Vault):
    """기기 로컬 설정(`.osk/`)을 읽고 고치는 동안 mutation 잠금을 쥔다. 동기화 데몬은 이 잠금 안에서
    rebase하고 그 rebase가 바꾼 `.osk/` 파일을 rebase 전 바이트로 되돌린다(`vault_sync.rebase_snapshot`)
    — 그 사이에 읽은 설정은 다른 기기의 판본일 수 있고, 쓴 설정은 되돌리기에 덮인다. 노드 쓰기가
    아니므로 판본 관문(`_fence`)은 세우지 않는다."""
    path = mutation_lock_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        lock_exclusive(f)
        try:
            yield
        finally:
            unlock(f)
