"""osk.core — vault 루트·시각·식별자·대장 공통 규약.

구현 근거: Mechanism §1(배치)·§2(식별과 시간)·§3(대장 — 인과 DAG).
파일이 정본이고 엔진 상태는 전부 파일에서 재계산한다(시행령 §11 1항).

대장의 인과 모델 (Mechanism §3):
- 각 기록은 `parents`로 하나 이상의 직전 head를 참조한다. 평소에는 1개,
  다기기 병합 직후의 첫 기록은 모든 head를 참조해 분기를 봉합한다(병합 앵커).
- `parents` 도입 전의 유산 기록은 파일 순서를 인과 순서로 간주한다(단일 기기
  이력). 도입 사건(첫 parents 보유 기록 = 앵커)부터 명시 인과가 정본이며,
  **앵커 이후에는 파일 순서 추정을 쓰지 않는다** — parents 없는 기록은 고립
  루트로 강등한다(구 엔진이 다른 클론에서 쓴 행이 병합돼 들어와도 가짜 인과를
  얻지 못한다). 고립 루트도 head이므로 사용자의 새 봉합 기록이 해소한다.
- rid는 잠금 안에서 대장의 정본상 최대 rid로부터 단조 생성한다 — 물리적
  마지막 행이 아니라 최대값이 바닥이다.
- 판정은 rid 정렬이 아니라 인과 극대(causal maxima)로 한다. 같은 노드의
  극대 기록이 유일하지 않으면(비교 불능 분기 또는 순환) 보수적으로
  미확정이다. 이후 모든 head를 조상으로 갖는 새 기록(사용자의 봉합)이
  유일 극대가 되어 해소한다.

손상과 해소 가능성 (fail-closed의 두 갈래):
- **정규화로 해소 가능한 이상** — 자기 참조·미지 rid 참조·전방 참조(파일
  순서상 뒤를 가리키는 parents)는 간선을 잘라 고립 루트로 강등한다. 순환이
  원천 차단되고(항상 DAG), 새 봉합 기록으로 해소되는 길이 남는다.
- **구조 손상** — rid 부재·rid 형식 위반·rid 중복은 기록의 동일성 자체가
  깨진 상태다. 판정은 fail-closed(미확정)로 두되 `ledger_damage`가 이를
  표면화하고, 회복은 Mechanism §3 7항의 수동 복구가 담당한다. 이 상태에서는
  새 기록의 append도 거부한다(손상 위에 이력을 더 쌓지 않는다).
"""
from __future__ import annotations
import hashlib, json, os, random, re, string, tempfile, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import epoch
from ._portalock import lock_exclusive, unlock

B36 = string.digits + string.ascii_lowercase
KST = ZoneInfo("Asia/Seoul")          # Mechanism §2 2항 — 표기 시간대는 KST 고정


def vault_root() -> Path:
    env = os.environ.get("OSK_VAULT_ROOT")
    if env:
        return Path(env).resolve()
    # <vault>/_governance/_engine/osk/core.py — 4단계 위가 vault 루트다
    return Path(__file__).resolve().parent.parent.parent.parent


ROOT = vault_root()
LEDGER = ROOT / "= Scope/Workbench/_ledger"
SIGNATURES = LEDGER / "signatures.jsonl"
CANDIDATES = LEDGER / "case" / "candidates.jsonl"
PINS = LEDGER / "pins.jsonl"
VALIDATORS = LEDGER / "validators.jsonl"   # 검증기 활성화 기록 (Mechanism §6-1)
ROUTING = LEDGER / "routing.jsonl"       # 세션→scope 라우팅 (Mechanism §6-2 3항)
_MUTATION_LOCK_PATH: Path | None = None


def local_lock_path(name: str, root: Path | None = None) -> Path:
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
    r = Path(root) if root is not None else ROOT
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


def mutation_lock_path() -> Path:
    """working-tree 변경 상호배제 잠금의 경로 — 동기화(sync·update)가 잡는
    `osk-mutation.lock`과 **같은 파일**이다.

    이름을 공유해야 데몬의 `pull(rebase)`가 반려의 파괴 구간과 경합하지 않는다.
    프로세스마다 한 번만 해석한다 — 이 잠금은 모든 노드 쓰기가 잡는다."""
    global _MUTATION_LOCK_PATH
    if _MUTATION_LOCK_PATH is None:
        _MUTATION_LOCK_PATH = local_lock_path("osk-mutation.lock")
    return _MUTATION_LOCK_PATH


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

    def __enter__(self):
        path = mutation_lock_path()
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

TS_FMT = "%Y-%m-%d %H:%M (KST)"          # Mechanism §2 2항 — frontmatter 시각
TS_RE = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} \(KST\)$"
ID_RE = r"^\d{6}-[0-9a-z]{4}-[0-9a-z]{4}$"  # Mechanism §2 1항 — YYMMDD-ssss-rrrr
RID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$"
CASE_RE = r"^CASE-\d{4}-\d+$"            # Mechanism §4 3항 — CASE-<연도>-<일련>


def now_kst() -> str:
    """frontmatter 시각 — 기기 시간대와 무관하게 KST로 적는다."""
    return datetime.now(KST).strftime(TS_FMT)


def now_iso() -> str:
    """대장 기록의 `at` — ISO 8601 유지 (Mechanism §2 2항 단서)."""
    return datetime.now(KST).replace(microsecond=0).isoformat()


def b36(n: int, width: int) -> str:
    s = ""
    while n:
        s = B36[n % 36] + s
        n //= 36
    return s.rjust(width, "0")


def new_node_id(existing: set[str], when: datetime | None = None) -> str:
    """생성 시 자동 부여. 유일성의 담보는 전수 중복 검증(Mechanism §2 1항).
    날짜·경과초는 created와 같은 KST 시계에서 뽑는다."""
    d = when or datetime.now(KST)
    secs = d.hour * 3600 + d.minute * 60 + d.second
    while True:
        i = f"{d:%y%m%d}-{b36(secs, 4)}-{''.join(random.choices(B36, k=4))}"
        if i not in existing:
            return i


def sha256_file(p: Path | str) -> str:
    return "sha256:" + hashlib.sha256(Path(p).read_bytes()).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def posix_rel(p: Path, relative_to: Path) -> str:
    r"""경로를 **기기 비의존 표기**의 상대 경로 문자열로 — `resolve_in_root`의 역방향.

    `str(Path)`는 OS 표기를 낸다. Windows에서는 구분자가 역슬래시(`_engine\osk`)로
    나오는데, 이 체계의 규칙은 전부 슬래시로 쓰인다(매니페스트 `DENY _engine/`,
    pin 대상 `= Scope/W2/`, 대장의 `path`). 그래서 OS 표기를 그대로 비교에 넘기면
    규칙이 **조용히 안 걸린다** — 실측으로 Windows에서 DENY 8개가 0건을 제외했고
    `move_node`의 pin 거부가 통과했다.

    트리는 기기 사이에서 공유되므로 키도 기록도 기기에 의존하면 안 된다.
    비교에 쓰거나 대장에 적는 경로는 전부 이 함수를 거친다."""
    return p.relative_to(relative_to).as_posix()


def resolve_in_root(rel_or_abs: str | Path) -> Path | None:
    """대장·사건부에 적힌 경로 문자열을 **vault 안으로 봉쇄** 해석한다.
    대장은 다기기 병합으로 임의의 내용이 유입될 수 있는 신뢰 밖 입력이므로,
    `..`·절대 경로·심볼릭 링크로 루트를 벗어나면 None(=해석 실패)이다.
    실패는 호출부에서 언제나 거부·불일치 쪽으로 처리한다."""
    try:
        p = Path(rel_or_abs)
        cand = p if p.is_absolute() else ROOT / p
        real = Path(os.path.realpath(cand))
        root = Path(os.path.realpath(ROOT))
        if real != root:
            real.relative_to(root)      # 벗어나면 ValueError
        return real
    except (ValueError, OSError, TypeError):
        return None


# ── rid — 시각 48비트 + 시퀀스 12비트 (생성 단조 표식) ────────────────────

def _rid_parts(rid: str) -> tuple[int, int]:
    h = rid.replace("-", "")
    return int(h[0:12], 16), int(h[12:16], 16) & 0x0FFF


def _rid_key(rid: str) -> tuple[int, int, str]:
    ms, seq = _rid_parts(rid)
    return (ms, seq, rid)


def _make_rid(ms: int, seq: int) -> str:
    rb = random.getrandbits(62)
    b = ms.to_bytes(6, "big") + ((0x7 << 12 | seq).to_bytes(2, "big")) \
        + ((0b10 << 62 | rb).to_bytes(8, "big"))
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _next_rid(max_rid: str | None) -> str:
    """대장의 정본상 최대 rid보다 반드시 큰 rid — 병합으로 물리 순서가
    섞여 있어도 최대값이 바닥이다."""
    now_ms = int(time.time() * 1000)
    if max_rid:
        last_ms, last_seq = _rid_parts(max_rid)
        if now_ms > last_ms:
            return _make_rid(now_ms, 0)
        seq = last_seq + 1
        if seq > 0xFFF:
            return _make_rid(last_ms + 1, 0)
        return _make_rid(last_ms, seq)
    return _make_rid(now_ms, 0)


# ── 대장 읽기·손상 진단 ──────────────────────────────────────────────────

def _parse_lines(text: str, path: Path) -> list[dict]:
    out = []
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"대장 손상(부분 행) {path}:{i+1} — 수동 복구 절차 필요") from e
        if not isinstance(rec, dict):
            raise ValueError(
                f"대장 손상(기록이 아닌 행) {path}:{i+1} — 수동 복구 절차 필요")
        out.append(rec)
    return out


def ledger_read(path: Path) -> list[dict]:
    """파일 순서 그대로 반환한다. 판정의 정본은 인과(parents)이며,
    유산 구간(앵커 이전)은 파일 순서가 인과다."""
    if not path.exists():
        return []
    return _parse_lines(path.read_text(encoding="utf-8"), path)


def ledger_damage(records: list[dict], path: Path | str = "") -> list[str]:
    """기록의 **동일성**이 깨진 구조 손상 목록 — rid 부재·형식 위반·중복.
    정규화로 흡수하면 안 되는(해소를 새 기록에 맡길 수 없는) 이상이며,
    Mechanism §3 7항의 수동 복구 대상이다. 빈 목록이면 건전."""
    out, seen = [], {}
    where = f"{path}:" if path else "행"
    for i, r in enumerate(records):
        rid = r.get("rid")
        if rid is None:
            out.append(f"{where}{i+1} rid 부재 (node={r.get('node')})")
            continue
        if not re.match(RID_RE, str(rid)):
            out.append(f"{where}{i+1} rid 형식 위반: {rid}")
            continue
        if rid in seen:
            out.append(f"{where}{i+1} rid 중복: {rid} (앞선 행 {seen[rid]+1})")
        seen[rid] = i
    return out


def damaged_nodes(records: list[dict]) -> set[str]:
    """구조 손상에 연루된 노드 — 판정은 fail-closed(미확정)."""
    bad, seen = set(), {}
    for i, r in enumerate(records):
        rid, node = r.get("rid"), r.get("node")
        if rid is None or not re.match(RID_RE, str(rid)):
            if node:
                bad.add(node)
            continue
        if rid in seen:
            for j in (seen[rid], i):
                if records[j].get("node"):
                    bad.add(records[j]["node"])
        seen[rid] = i
    return bad


# ── 인과 DAG ─────────────────────────────────────────────────────────────

def effective_parents(records: list[dict]) -> dict[str, list[str]]:
    """rid → 부모 rid 목록. **항상 비순환**으로 정규화한다.

    - 앵커(첫 parents 보유 기록) 이전의 유산 기록만 파일 순서를 인과로
      간주한다. 앵커 이후의 parents 부재 기록은 고립 루트다.
    - parents 원소 중 자기 자신·미지 rid·파일 순서상 뒤(전방 참조)는
      잘라낸다 — 손상이 순환을 만들어 기록을 판정에서 소거하는 것을 막고,
      잘린 기록은 head로 남아 새 기록으로 봉합된다.
    - 구조 손상 기록(rid 부재·형식 위반)은 DAG에 넣지 않는다. 그 노드는
      damaged_nodes가 fail-closed로 잡는다.
    """
    anchor = ledger_anchor_index(records)
    order = {}
    for i, r in enumerate(records):
        rid = r.get("rid")
        if rid is not None and re.match(RID_RE, str(rid)) and rid not in order:
            order[rid] = i
    out: dict[str, list[str]] = {}
    prev_rid = None
    for i, r in enumerate(records):
        rid = r.get("rid")
        if rid is None or not re.match(RID_RE, str(rid)):
            continue
        if isinstance(r.get("parents"), list):
            out[rid] = [p for p in r["parents"]
                        if isinstance(p, str) and p and p != rid
                        and p in order and order[p] < i]
        elif anchor is None or i < anchor:
            out[rid] = [prev_rid] if prev_rid else []   # 유산 구간만 파일 순서
        else:
            out[rid] = []                               # 앵커 이후 = 고립 루트
        prev_rid = rid
    return out


def heads(records: list[dict]) -> list[str]:
    """어느 기록의 부모도 아닌 rid 목록 — 병합·손상 직후에는 둘 이상이다."""
    par = effective_parents(records)
    referenced = {p for ps in par.values() for p in ps}
    return [rid for rid in par if rid not in referenced]


def _ancestors(rid: str, par: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(par.get(rid, []))
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(par.get(x, []))
    return seen


def causal_maxima(records: list[dict], value: str,
                  par: dict[str, list[str]] | None = None,
                  field: str = "node",
                  candidate=None) -> list[dict]:
    """같은 키를 가진 기록 중 인과 극대(다른 기록의 조상이 아닌 것). 유일하면
    그것이 판정 기록, 그 밖에는 비교 불능 — fail-closed.

    `field`로 키를 고른다: 서명 기록부는 `node`, 라우팅 대장은 `session`.

    `candidate(r)`를 주면 그 술어를 만족하는 기록만 **후보**로 삼되, 조상 관계는
    여전히 **전체 기록의 DAG**로 계산한다. 후보를 목록에서 미리 빼고 부르면 그
    기록이 맡고 있던 parents 간선이 함께 끊겨(effective_parents가 미지 parent를
    자른다) 남은 기록들이 서로 비교 불능인 거짓 분기가 된다 — 걸러야 할 것은
    후보 자격이지 인과 사슬이 아니다."""
    par = effective_parents(records) if par is None else par
    mine = [r for r in records if r.get(field) == value and r.get("rid") in par]
    if candidate is not None:
        mine = [r for r in mine if candidate(r)]
    rids = {r["rid"] for r in mine}
    anc = {r["rid"]: _ancestors(r["rid"], par) for r in mine}
    return [r for r in mine
            if not any(r["rid"] in anc[o] for o in rids if o != r["rid"])]


def unresolved_nodes(records: list[dict], field: str = "node") -> set[str]:
    """판정이 성립하지 않는 키 — 인과 극대가 유일하지 않거나(분기·순환 잔재)
    구조 손상에 연루된 것. 판정은 보수적으로 미확정."""
    par = effective_parents(records)
    keys = {r.get(field) for r in records if r.get(field)}
    out = set(damaged_nodes(records)) if field == "node" else set()
    for n in keys:
        if n in out:
            continue
        if len(causal_maxima(records, n, par, field)) != 1:
            out.add(n)
    return out


def resolve_one(records: list[dict], value: str, field: str) -> dict | None:
    """인과 극대가 유일할 때만 그 기록을 돌려준다 — 그 밖에는 None(미확정)."""
    maxima = causal_maxima(records, value, None, field)
    return maxima[0] if len(maxima) == 1 else None


def ledger_anchor_index(records: list[dict]) -> int | None:
    """새 계약(명시 parents)의 도입 지점 — 첫 parents 보유 기록의 색인.
    이후 기록은 스키마 검증 대상이다(유산 구간은 호환 경계)."""
    for i, r in enumerate(records):
        if isinstance(r.get("parents"), list):
            return i
    return None


def ledger_append(path: Path, record: dict, expect=None) -> dict:
    """모든 `_ledger/` jsonl 공통 (Mechanism §3):
    잠금 → 전체 판독 → **구조 손상이면 거부** → parents = 현재 head 전부
    (병합 봉합) → rid = 정본 최대 rid로부터 단조 생성 → 행 단위 원자
    append·fsync.

    `expect`는 **잠금 안에서** 방금 읽은 기록으로 전제조건을 다시 보는 선택적
    검사다 — `expect(records)`가 문자열을 돌려주면 그것을 사유로 거부한다.
    호출부가 잠금 밖에서 검사하고 append 하는 사이에 다른 기기의 기록이
    동기화로 들어오면, 그것을 못 본 행이 그 기록의 **인과 자식**으로 붙어
    분기가 stale로 드러나지 않고 조용히 대체한다(그리고 행에 적은 전제가
    거짓 진술이 된다). 검사와 append를 같은 잠금에 두어야 그 창이 닫힌다."""
    record.setdefault("at", now_iso())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        lock_exclusive(f)
        try:
            f.seek(0)
            records = _parse_lines(f.read(), path)
            dmg = ledger_damage(records, path)
            if dmg:
                raise ValueError(
                    f"대장 손상 — 수동 복구 절차 필요 (Mechanism §3 7항): "
                    + "; ".join(dmg[:5]))
            if expect is not None:
                why = expect(records)
                if why:
                    raise ValueError(why)
            record["rid"] = _next_rid(
                max((r["rid"] for r in records if r.get("rid")),
                    key=_rid_key, default=None))
            record["parents"] = heads(records)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            unlock(f)
    return record
