"""osk.graph — 색인: 이름과 id를 파일로 잇는다.

구현 근거: Mechanism §1(선언표·`_` 규칙), 헌법 8조(참조 위상의 id 해석). 노드 파일을 훑어 이름·id·자리의
표를 만들고(`Index`), 참조 하나가 무엇을 가리키는지 푼다(`Index.resolve`). 파일의 자리를 정하는 것은
`placement`, 그래프 전체의 참조를 읽는 것(위상 검사·참조 보고·중심성)은 `topology`가 맡는다.
"""
from __future__ import annotations
import os
import re
import tempfile
import time
from pathlib import Path

from .core import ID_RE, resolve_in_root
from . import contract, placement, raw, roots, signatures as _sig
from .vault import Vault

# 바이트 속 id꼴 토큰 — `ID_RE`의 앵커 없는 판(동 id 후보표 전용, 판정 아님)
_ID_TOKEN = re.compile(rb"(?<![0-9a-z])\d{6}-[0-9a-z]{4}-[0-9a-z]{4}(?:[0-9a-z]{4})?(?![0-9a-z])")
_ID_TOKEN_CACHE: dict[Path, tuple[tuple[int, int], frozenset]] = {}


def load_cases(vault: Vault) -> dict[str, dict]:
    """사건부 헤더 일괄 로드 — conflicts 적격 판정용."""
    out = {}
    cdir = vault.ledger / "case"
    if cdir.exists():
        for f in sorted(cdir.glob("CASE-*.md")):
            c = _sig.parse_case(f)
            if c:
                out[f.stem] = c
    return out


# ── 서명의 racy 창 ──────────────────────────────────────────────────────
# 파일이 바뀌었는지 (경로, mtime, 크기)로 판정하는 것은 **시각 입도 안에서 눈이
# 먼다.** 실측: 같은 크기로 즉시 고쳐 쓰면 300회 중 215회(71.7%)가 같은 mtime을
# 받았고, 쓰기 직후 `time.time_ns() - mtime`이 0인 경우가 300회 중 244~266회였다.
# 채취 시각과 같은 눈금에 찍힌 파일은 그 뒤의 변경을 서명으로 구별할 수 없다.
#
# git이 racily-clean을 다루는 규율과 같게 막는다 — 채취 시각에 너무 가까운
# 항목이 하나라도 있으면 그 지문을 믿지 않는다. 대가는 그 창 동안 **읽기
# 호출마다** 색인을 다시 짓는 것이다(창 안에서는 구판보다 느리다 — 실측
# 1,038 ms 대 666 ms). 그러니 창은 필요한 만큼만 넓어야 한다.
#
# 그래서 **고르지 않고 잰다.** 구판 상수는 FAT 계열의 2초 입도를 상정한
# 추측이었는데, 이 기기에 FAT 볼륨도 네트워크 공유도 없어 그 값을 검증할 수
# 없었다 — 본 적 없는 파일시스템을 위해 매일 쓰는 파일시스템에 세금을 물린
# 셈이다. 프로세스마다 한 번 실제 눈금을 재고, 그 8배에 바닥·천장을 씌운다.
_RACY_FLOOR_NS = 10_000_000          # 10 ms — 이 기기 실측 눈금(~1 ms)의 10배
_RACY_CEIL_NS = 2_000_000_000        # 2 s — 못 쟀을 때의 보수적 폴백(FAT 입도)
_racy_margin_cache: dict[Path, int] = {}      # 저장소 루트 → 여유. 시험은 값을 고정하려고 이 표를 직접 채운다


def _probe_mtime_granularity(vault: Vault, rounds: int = 40) -> int | None:
    """이 파일시스템의 mtime 눈금 — 같은 파일을 연달아 고쳐 쓰며 시각이 얼마나
    벌어지는지 본다. 한 번도 벌어지지 않으면 눈금이 이 반복보다 굵다는 뜻이라
    `None`(=모름)을 낸다.

    자리는 `.osk/`다 — 기기 소유이고 추적되지 않으며 vault와 **같은 볼륨**이라
    재려는 그 파일시스템이다. 서명 범위 밖이므로 이 탐침이 다른 프로세스의
    지문을 흔들지 않는다.

    반복이 넉넉해야 한다 — 즉시 재기록의 약 70%는 눈금 안에 들어가 시각이
    전진하지 않으므로(실측), 몇 번으로는 눈금이 굵다고 오판하고 천장으로
    떨어진다. 40회면 NTFS에서 실패 확률이 무시할 만하고, 정말로 굵은
    파일시스템(FAT 2초)에서는 40회가 전부 같은 눈금에 들어 제대로 천장을
    고른다.

    탐침 파일은 **프로세스마다 유일**해야 한다. 구판은 고정 이름을 써서, 동시에
    뜬 서버들이 같은 파일을 번갈아 쓰고 지웠다 — 진 쪽은 남의 쓰기를 재거나
    `unlink`가 이미 사라진 파일에서 실패해 `None`(=모름)을 냈고, 그 `None`은
    racy 창 **2초**로 굳어 프로세스 수명 내내 남았다. 그 프로세스는 어떤 쓰기
    뒤에도 2초 동안 모든 읽기가 전수 재구축이 된다(실측: 동시 4개 중 절반이
    2초를 잡았고, 파일 mtime이 1.6초 전인데도 racy로 판정했다)."""
    probe = None
    try:
        d = vault.root / ".osk"
        d.mkdir(parents=True, exist_ok=True)
        fd, nm = tempfile.mkstemp(dir=str(d), prefix="mtime-probe-")
        os.close(fd)
        probe = Path(nm)
        deltas = []
        for _ in range(rounds):
            probe.write_bytes(b"a" * 64)
            a = probe.stat().st_mtime_ns
            probe.write_bytes(b"b" * 64)
            b = probe.stat().st_mtime_ns
            if b > a:
                deltas.append(b - a)
    except OSError:
        return None
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass                 # 남아도 `.osk/`는 추적되지 않는다
    return min(deltas) if deltas else None


def racy_margin_ns(vault: Vault) -> int:
    """서명을 믿지 않을 시간 폭. 저장소마다 한 번 재고 접어 둔다 — 시각의 눈금은 그 저장소가 놓인 볼륨의
    것이라, 한 저장소에서 잰 값을 다른 저장소에 쓰지 않는다."""
    if vault.root not in _racy_margin_cache:
        g = _probe_mtime_granularity(vault)
        _racy_margin_cache[vault.root] = (_RACY_CEIL_NS if g is None else
                                          max(_RACY_FLOOR_NS, min(_RACY_CEIL_NS, g * 8)))
    return _racy_margin_cache[vault.root]


def locate_by_id(vault: Vault, node_id: str) -> Path | None:
    """현재 vault에서 id로 노드 파일을 찾는다 — 경로 이동은 동일성을 깨지
    않으므로(경로는 상태) id 해석이 정본이다.

    같은 id가 둘 이상이면 **고르지 않고 `None`을 낸다.** 구판은 첫 일치에서
    멈춰, `by_id`(뒤가 이긴다)·`read_node`와 서로 다른 파일을 가리켰다 —
    id 해석의 동점 규칙이 세 갈래로 갈려 있었다. 지금 이 함수의 생산
    호출자는 없지만, 규칙이 갈린 채로 두면 다음 호출자가 그 함정을 다시
    연다. 서명 판정에서 `None`은 fail-closed(=`unsigned`)로 떨어진다."""
    hit = None
    for p, _k in placement.iter_nodes(vault):
        if _sig.id_of(p) == node_id:
            if hit is not None:
                return None       # 모호 — 표면이 임의로 한쪽을 택하지 않는다
            hit = p
    return hit


def index_signature(vault: Vault, errors: list | None = None):
    """`Index`가 읽는 것의 (상대경로, mtime_ns, 크기, 소속, 링크 목적지)와,
    그중 채취 시각에 너무 가까워 믿을 수 없는 항목이 있었는가.

    범위가 색인과 **같아야** 한다. 좁으면 그 바깥의 변경이 캐시를 무효화하지
    못하고(구판의 지문은 노드만 봐서 `_raw`·대장의 변경을 놓쳤다), 넓으면
    색인과 무관한 것이 캐시를 버린다 — 한때 `_governance` 전체를 훑어 엔진
    소스 28개와 `__pycache__` 38개가 산입됐고, 그래서 바이트코드가 다시
    써지기만 해도 읽기 캐시가 통째로 날아갔다. 여기서는 색인이 실제로 읽는 것,
    곧 **노드 군집의 `.md`와 비노드로 등재되는 raw·sources·대장 파일**만 센다.

    `stat`은 `scandir`이 디렉토리 판독에서 이미 받아 온 것을 쓴다 — 항목마다
    다시 묻지 않는다(Windows 기준. Linux의 `DirEntry.stat()`은 항목마다 실제
    `stat`을 부르므로 그쪽에서는 절감이 없다)."""
    out = []
    racy = False
    margin = racy_margin_ns(vault)
    now = time.time_ns()
    lo, hi = now - margin, now + margin
    for base in placement.node_spaces(vault) + ("_governance", "_sources"):
        root = vault.root / base
        if not root.exists():
            continue
        for e, parts in placement.scan(root, (base,), errors):
            reparse = placement.is_reparse(e)
            p = Path(e.path) if reparse else None
            k = placement.space_of(vault, p) if reparse else placement.space_of_parts(vault, parts)
            if placement.is_node_home(k):
                if base == "_sources" or not placement.is_md(e.name):
                    continue
            elif k[0] not in ("raw", "sources", "ledger"):
                continue
            try:
                st = e.stat()
                # Equal metadata does not mean equal contents after retargeting.
                target = str(p.resolve()) if reparse else ""
            except OSError:
                continue   # 열거와 stat 사이의 삭제 — 다음 호출의 지문이 다르다
            # 창은 **양쪽**으로 닫는다. 아래만 보면 mtime이 미래인 파일 하나가
            # 영영 racy로 남아 읽기 캐시가 **영구히** 죽는다(실측: 20k에서
            # 80 ms → 6.8~9.2 s). 시계 역행·듀얼부팅 RTC·백업 복원·NAS 시계가
            # 전부 그 방아쇠다. 먼 미래의 시각은 그 자체로 안정적이라 다음
            # 쓰기와 구별되므로 의심할 이유가 없다.
            if lo <= st.st_mtime_ns <= hi:
                racy = True
            out.append(("/".join(parts), st.st_mtime_ns, st.st_size, k, target))
    out.sort()
    return out, racy


class Index:
    """파일명(stem) → (경로, 소속). 참조 해석과 중심성의 기반.

    **이름 색인은 즉시, 계약 색인은 묻는 것만 짓는다** (v3.7.0).

    구판은 생성 시점에 전 노드를 열어 frontmatter를 파싱했다. 그런데 쓰기가
    색인에 묻는 것은 대부분 "이 이름이 있는가"이고, 그 답은 **디렉토리 항목에
    이미 적혀 있다** — 파일을 열 필요가 없다. 실측(1,811 노드, 부하 있는 기기의
    최소값): 전량 판독 0.42초 대 이름만 0.05초. 구판 `Index()` 전체는
    1.5~2.4초였다 — 순수 파이썬 YAML과 파일당 `resolve()`가 그 차의 대부분이다.

    그래서 둘로 가른다.

    - **이름 색인** (`names`·`_by_name`·`nonnode`) — `scandir`에서 나온다.
      파일을 열지 않는다. 다만 이것도 **생성 시점의 스냅숏**이다: 그 뒤의
      변경은 지문(`index_signature`)이 무효화할 때까지 보이지 않고, 그래서
      쓰기 통로가 방금 만든 이름을 손으로 등재한다(`register_new`).
    - **계약 색인** (`nodes`·`by_id`·`broken`·`dup_stems`) — 파일을 열어야
      안다. 이름 하나를 물으면 **그 후보만** 열고(`resolve`), 전수를 물으면
      그때 전량을 짓는다.

    전수 판독을 요구하는 것은 id 유일성 대조(Mechanism §2 1항)와 전역
    검증·검색이다. 그것들은 성질상 전수라서 값을 치르는 것이 맞고, 나머지가
    같이 치르던 것이 구판의 낭비였다.

    **이름 해석의 판정은 구판과 같다.** `nodes`가 판독되는 파일만 담는다는
    성질도, 동명 판정이 판독에 성공한 노드끼리만 선다는 성질도 그대로다 —
    한때 이름만으로 동명을 세다가 파손 파일 하나가 멀쩡한 참조를 `모호`로
    만들었고, 그 `모호`에는 소속이 없어 뒤따르는 헌법 8조 검사가 아예 제기되지
    않았다(독립 검토 4인 확인)."""

    def __init__(self, vault: Vault):
        # 관측이 불완전하면(디렉토리 하나라도 못 읽었으면) 유일성을 말할 수
        # 없다. 그 사실을 색인이 들고 있어야 쓰기가 거부할 수 있다.
        self.vault = vault
        self.scan_errors: list[str] = []
        self._dirents: dict = {}
        self._entries: list[tuple[Path, tuple]] = list(
            placement.iter_nodes(vault, self.scan_errors, self._dirents))
        self.names: dict[str, tuple[Path, tuple]] = {}
        self._by_name: dict[str, list[tuple[Path, tuple]]] = {}
        self.parsed: dict[Path, contract.Node] = {}
        self._failed: dict[Path, str] = {}
        self._all_parsed = False
        self._id_tokens: dict[str, list[Path]] | None = None
        for p, k in self._entries:
            self.names[p.stem] = (p, k)
            self._by_name.setdefault(p.stem, []).append((p, k))
        # 비노드 파일(원자료·대장·raw)도 대상 해석용으로 등재. 구판은 여기서
        # **2,132개를 훑어 170개를 등재**하며(실 vault 1,811 노드 기준) 항목마다
        # `is_file()`을, 노드가 아닌 항목에는 `resolve()`까지 불렀다 — 순회
        # 자체는 필요하지만 그 두 시스템콜은 아니다. 정렬은 구판에 없었으나
        # (파일시스템 순서 = 기기마다 다름) 여기서 세운다: 동명 비노드가 둘이면
        # 뒤에 오는 쪽이 이기므로 순서가 판정이다.
        #
        # 노드 이름으로 거르지 **않는다.** 한때 `names`로 걸렀는데, 그러면
        # 파손 노드와 이름이 겹치는 원자료가 등재되지 못해 `resolve`가
        # `('nonnode',…)` 대신 `('dangling',)`을 냈고, 헌법 8조의 Domain→`_raw`
        # 금지가 fail-open했다. 거르지 않아도 `resolve`가 판독 성공 노드를
        # 먼저 보므로 판정은 구판과 같다 — 순서가 곧 우선순위다.
        self.refresh_nonnode()

    def refresh_nonnode(self) -> None:
        """Refresh evidence names without discarding unchanged node contracts.

        The caller must have observed the same complete, non-racy node signature.
        Scan errors remain on the Index so a partial refresh is never cached.
        """
        self.nonnode: dict[str, tuple] = {}
        # 외부 루트 선언(`roots.jsonl`)도 대장이라 같이 낡는다 — 처음 묻는 별칭이 다시 읽는다
        self._roots: dict | None = None
        for base in ("_sources", self.vault.scope, self.vault.person):
            root = self.vault.root / base
            if not root.exists():
                continue
            found = []
            for e, parts in placement.scan(root, (base,), self.scan_errors):
                p = Path(e.path)
                k = placement.space_of(self.vault, p) if placement.is_reparse(e) else placement.space_of_parts(self.vault, parts)
                if k[0] in ("raw", "sources", "ledger"):
                    found.append((p, k))
            for p, k in sorted(found, key=lambda x: x[0]):
                self.nonnode[p.stem] = (p, k)
                self.nonnode[p.name] = (p, k)   # `![[diagram.png]]` — 옵시디언 표기

    # ── 계약 색인 — 묻는 것만 짓는다 ─────────────────────────────────
    def _readable(self, p: Path) -> bool:
        """그 파일 **하나만** 판독해 노드인지 본다. 성공·실패 모두 기억하므로
        같은 색인 위에서 같은 파일을 두 번 열지 않는다."""
        if p in self.parsed:
            return True
        if p in self._failed:
            return False
        try:
            self.parsed[p] = contract.parse(p)
            return True
        except (OSError, ValueError) as e:
            # 판독 불가 파일(임시 메모 등)은 노드에서 분리해 소비자가 건너뛰게
            # 하고 별도로 보고한다 — 하나가 검증기·검색 전체를 죽이지 않는다
            # (시행령 §11 — 실패는 보류·보고).
            self._failed[p] = f"{p.relative_to(self.vault.root)}: {e}"
            return False

    def parse_all(self) -> None:
        """전수 판독 — `nodes`·`by_id`·`broken`이 이것을 강제한다. 그 셋을
        묻는 것은 곧 전수 조사를 묻는 것이므로, 값을 이 자리에서 한 번
        명시적으로 치르고 결과를 접어 둔다(같은 색인 위에서 두 번 안 짓는다).

        접는 순서는 구판의 판독 순서 그대로다 — 동명·동 id가 둘이면 **뒤에
        오는 쪽이 이긴다.** 이 규칙이 바뀌면 같은 트리가 다른 노드를 가리킨다."""
        if self._all_parsed:
            return
        nodes: dict[str, tuple[Path, tuple]] = {}
        by_id: dict[str, tuple[Path, tuple]] = {}
        broken: dict[str, str] = {}
        dup: dict[str, list[str]] = {}
        dupid: dict[str, list[str]] = {}
        for p, k in self._entries:
            if not self._readable(p):
                why = self._failed[p]
                broken[p.stem] = (f"{broken[p.stem]}; {why}"
                                  if p.stem in broken else why)
                continue
            if p.stem in nodes:
                dup.setdefault(p.stem, [
                    str(nodes[p.stem][0].relative_to(self.vault.root))]).append(
                    str(p.relative_to(self.vault.root)))
            nodes[p.stem] = (p, k)
            nid = self.parsed[p].id
            if nid:
                # id 중복도 **센다.** 세지 않으면 뒤에 오는 쪽이 조용히 이기고,
                # 읽기 표면은 앞에 오는 쪽을 골라 서로 다른 파일을 가리킨다 —
                # 사본은 바이트가 같으므로 CAS도 통과해, 에이전트가 읽지 않은
                # 파일이 갱신된다(재현 확인). 동일성의 정본이 겹쳤다는 것은
                # 표면이 임의로 한쪽을 택할 일이 아니라 사람이 고칠 일이다.
                if nid in by_id:
                    # 경로는 POSIX 표기 — 이 목록은 `read_node`의 오류로 MCP
                    # 표면까지 나간다(도구마다 구분자가 갈리면 호출자가 혼선).
                    dupid.setdefault(nid, [
                        by_id[nid][0].relative_to(self.vault.root).as_posix()]).append(
                        p.relative_to(self.vault.root).as_posix())
                by_id[nid] = (p, k)
        self._nodes, self._by_id = nodes, by_id
        self._broken, self._dup_stems = broken, dup
        self._dup_ids = dupid
        self._all_parsed = True

    @property
    def nodes(self) -> dict[str, tuple[Path, tuple]]:
        """판독되는 노드만 (stem → 경로·소속). 파손 파일은 여기 없다."""
        self.parse_all()
        return self._nodes

    @property
    def by_id(self) -> dict[str, tuple[Path, tuple]]:
        """id → 노드 (derived-from 해석). 동일성의 정본은 id다.

        겹친 id는 여기서 **뒤에 오는 쪽이 이긴다.** 그 승패에 기대지 마라 —
        겹쳤는지는 `dup_ids`로 먼저 묻고, 겹쳤으면 표면은 고르지 말고 거부한다.
        """
        self.parse_all()
        return self._by_id

    @property
    def dup_ids(self) -> dict[str, list[str]]:
        """**판독에 성공한** 노드끼리의 동 id 중복 (id → 경로 목록).

        `dup_stems`의 id판이며 같은 이유로 판독 실패 파일을 세지 않는다.
        중복 id는 표면이 만들 수 없으므로 언제나 외부 기원이다 — 파일 복제
        (옵시디언 "사본 만들기"), 백업 복원, 병합 충돌의 수동 해결. id 생성기의
        무작위부를 늘려도 이 경로는 `new_node_id`를 거치지 않으므로 남는다.
        """
        self.parse_all()
        return self._dup_ids

    @property
    def broken(self) -> dict[str, str]:
        """판독 실패 (stem → 사유). 같은 이름이 여럿 깨졌으면 사유를 잇는다."""
        self.parse_all()
        return self._broken

    @property
    def dup_stems(self) -> dict[str, list[str]]:
        """**판독에 성공한** 노드끼리의 동명 중복 (stem → 경로 목록).

        판독 실패 파일을 여기 세면 안 된다. 세면 남이 흘린 임시 메모 하나가
        멀쩡한 노드로 가는 참조를 `('ambiguous',)`로 만드는데, 그 판정에는
        **소속이 없어서**(1-튜플) `node_checks.topology_of`의 다음 줄 `tkind = r[1]`이
        서지 못한다 — 그 뒤의 헌법 8조 검사(Domain의 `_raw` 참조·scope 간 직접
        참조)가 아예 제기되지 않는다. 값싼 신호 셋을 얻고 비싼 신호 하나를 잃는
        교환이었고, 독립 검토 4인이 같은 자리를 지목했다."""
        self.parse_all()
        return self._dup_stems

    @property
    def complete(self) -> bool:
        """이 색인이 vault **전부**를 관측했는가. 거짓이면 이름·id 유일성을
        말할 수 없다 — 쓰기는 거부하고 읽기 캐시는 접어 두지 않는다."""
        return not self.scan_errors

    def candidates(self, name: str) -> list[Path]:
        """그 이름을 가진 노드 파일 **전부**(판독 여부 무관).

        쓰기 통로가 이름을 파일로 해석할 때 쓴다. 구판은 그 자리에서
        파일시스템을 다시 훑었는데, 그 금지의 근거는 *"mcp_server의 fingerprint
        캐시 색인으로 해석하면 낡은 경로에 작용한다"*였다 — 쓰기 통로가 쓰는
        것은 캐시가 아니라 **잠금 안에서 방금 지은** 색인이므로 그 자체가
        라이브 패스다. 같은 잠금 안에서 같은 디렉토리를 두 번 읽는 것은 순수
        중복이었다(실측: 25,000 노드에서 289 ms)."""
        return [p for p, _k in self._by_name.get(name, [])]

    def register_new(self, path: Path, kind: tuple) -> None:
        """방금 쓴 노드를 손에 든 색인에 등재한다 — 쓰기 **후** 상태를 봐야
        하는 소비자(`node_checks.dangling_of`)가 자기 자신을 가리키는 Link를 dangling으로
        오보하지 않게 한다. 이름 색인은 생성 시점의 스냅숏이라 그냥 두면 방금
        만든 이름이 없는 것으로 읽힌다."""
        self.names.setdefault(path.stem, (path, kind))
        lst = self._by_name.setdefault(path.stem, [])
        if all(p != path for p, _k in lst):
            lst.append((path, kind))

    def retarget(self, src: Path, dst: Path, kind: tuple) -> None:
        """이동을 손에 든 색인에 반영한다. 안 하면 `node_checks.dangling_of`가 이동 **후**
        옛 경로를 열려다 실패해 그 노드의 자기링크를 dangling으로 오보한다 —
        `create_node`에서 `register_new`가 막는 것과 같은 실패다."""
        stem = src.stem
        lst = self._by_name.get(stem)
        if lst is not None:
            self._by_name[stem] = [(dst, kind) if p == src else (p, k)
                                   for p, k in lst]
        cur = self.names.get(stem)
        if cur is not None and cur[0] == src:
            self.names[stem] = (dst, kind)
        # 옛 경로를 쥔 후보표는 동 id를 오보한다. 후보표는 `_entries`에서 다시
        # 지어지고, Windows의 디렉토리 판독 stat은 옛 경로에도 캐시된 값을 내므로
        # (파일을 열지 않고 접어 둔 토큰이 맞는다) 항목과 stat도 함께 옮긴다.
        self._entries = [(dst, kind) if p == src else (p, k) for p, k in self._entries]
        self._dirents.pop(src, None)
        self._id_tokens = None

    def _node_bytes(self):
        """노드 파일 전부의 바이트 — 판독(YAML) 없이. 못 여는 파일은 판독도
        실패하므로 노드가 아니다(`_readable`과 같은 판정)."""
        for p, _k in self._entries:
            try:
                yield p, p.read_bytes()
            except OSError:
                continue

    def mentioning(self, tokens) -> list[Path]:
        """바이트에 `tokens` 중 하나라도 나오는 노드 파일 — **후보**일 뿐이다.
        참조인지는 부르는 쪽이 판독해서 가린다. 파일을 전부 열지만 YAML을
        풀지 않으므로 전수 판독(`parse_all`)보다 싸다."""
        keys = [str(t).encode() for t in tokens if t]
        return [p for p, data in self._node_bytes() if any(k in data for k in keys)]

    def _id_token_sets(self):
        """파일마다 바이트 속 id꼴 토큰. 디렉토리 판독의 (mtime, 크기)가 같으면
        프로세스에 접어 둔 것을 쓴다 — 이름 쓰기마다 전 노드를 열면 2k 노드에서
        쓰기 한 번이 수백 ms 늘었다(실측). racy 창 안의 파일은 접지 않는다."""
        # ponytail: 캐시는 지운 경로를 비우지 않는다(프로세스 수명 동안 vault
        # 크기에 비례). 장수 프로세스에서 문제가 되면 색인 구축 때 솎는다.
        margin, now = racy_margin_ns(self.vault), time.time_ns()
        for p, _k in self._entries:
            try:
                e = self._dirents.get(p)
                st = e.stat() if e is not None else p.stat()
                key = (st.st_mtime_ns, st.st_size)
                hit = _ID_TOKEN_CACHE.get(p)
                if hit is None or hit[0] != key:
                    hit = (key, frozenset(t.decode() for t in _ID_TOKEN.findall(p.read_bytes())))
                    if abs(now - key[0]) > margin:
                        _ID_TOKEN_CACHE[p] = hit
            except OSError:
                continue              # 못 여는 파일은 판독도 실패한다 — 노드가 아니다
            yield p, hit[1]

    def id_twins(self, path: Path) -> list[str]:
        """`path` 노드와 id가 같은 **판독되는** 노드 전부(자신 포함, POSIX 경로) —
        겹치지 않으면 빈 목록. `dup_ids`의 한 id판이다.

        이름 핸들은 그 이름의 후보만 연다(전수 판독 없음). 그래서 이름으로 잡은
        노드의 id가 사본과 겹쳐도 보이지 않았고, 두 사본이 이름으로 각자 읽히고
        고쳐져 조용히 갈라졌다(2026-09-24 재현) — Mechanism §2 1항은 동 id면
        읽기도 쓰기도 거부한다. 후보표는 바이트의 id꼴 토큰에서 한 번 짓고
        (근거로 id를 적은 노드도 후보가 되지만 판독이 걸러낸다), 판정은 계약
        파서로 한다 — 정규식이 판정하면 따옴표 표기 같은 노드가 미아가 된다.
        """
        if not self._readable(path) or not self.parsed[path].id:
            return []
        nid = self.parsed[path].id
        if self._all_parsed:
            same = [self.vault.root / s for s in self.dup_ids.get(nid, [])]
        else:
            if self._id_tokens is None:
                self._id_tokens = {}
                for p, toks in self._id_token_sets():
                    for t in toks:
                        self._id_tokens.setdefault(t, []).append(p)
            same = [p for p in self._id_tokens.get(nid, ())
                    if self._readable(p) and self.parsed[p].id == nid]
        # 같은 파일로 가는 파일 링크도 한 노드를 두 자리에 두는 중복 소속이다 —
        # 사본과 같이 거부한다(Mechanism §2 1항).
        others = [p for p in same if p != path]
        return sorted(q.relative_to(self.vault.root).as_posix() for q in [path, *others]) if others else []

    def node(self, path: Path) -> contract.Node:
        if path not in self.parsed:
            self.parsed[path] = contract.parse(path)
        return self.parsed[path]

    def lookup_name(self, name: str) -> tuple[list, list[str]]:
        """Read only this name's candidates; broken files are not name owners."""
        live, errors = [], []
        for p, kind in self._by_name.get(name, []):
            if self._readable(p):
                live.append((p, kind))
            else:
                errors.append(self._failed[p])
        return live, errors

    def locate(self, name: str):
        """`resolve`와 같은 순서로 대상 파일 하나를 찾는다 — (경로, 소속). 외부 주소·
        모호·dangling이면 None. 제목은 파일 이름에서 `.md`만 뗀 것이므로, 제목에 든
        `.md`(`README.md`)도 제목의 일부로 찾는다."""
        if re.match(r"^https?://", name):
            return None
        if re.match(ID_RE, name):
            return None if name in self.dup_ids else self.by_id.get(name)
        if "/" in name:
            p = resolve_in_root(self.vault, name)
            for cand in ((p, p.with_suffix(".md")) if p is not None else ()):
                if cand.is_file():
                    return cand, placement.space_of(self.vault, cand)
            return None
        live, _errors = self.lookup_name(name)
        if len(live) == 1:
            return live[0]
        return None if live else self.nonnode.get(name)

    def resolve(self, name: str):
        """대상명 → ('node',소속) | ('nonnode',소속) | ('ambiguous',) |
        ('dangling',) | ('external',). 경로형([[00_Scope/B/b]])은 경로로 우선
        해석한다 — 파일명 우회를 막는다. 다만 URL은 `/`가 있어도 경로가
        아니며, 경로 해석은 vault 안으로 봉쇄한다([[/etc/passwd]]). 외부 루트의 별칭
        좌표(`docs:a/b.md`)도 `/`가 있어도 경로가 아니다 — 선언된 별칭이면 URL처럼 바깥이고,
        아니면 끊김이다. 파일이 있는지는 근거 절 검사가 본다(Mechanism §6-1 4항)."""
        if re.match(r"^https?://", name):
            return ("external",)
        alias = roots.split(name)
        if alias:
            if self._roots is None:
                try:
                    self._roots = roots.declarations(self.vault)
                except (OSError, ValueError):
                    return ("dangling",)    # 대장 손상 — 선언을 확인하지 못한 별칭은 끊김이다
            return ("external",) if roots.alias_problem(alias[0], self._roots) is None else ("dangling",)
        if placement.is_record_ref(name):
            try:
                return ("nonnode", placement.space_of(self.vault, raw.raw_file(self.vault, name.split("#", 1)[0])))
            except (OSError, ValueError):
                return ("dangling",)
        if re.match(ID_RE, name):
            # derived-from 노드 대상 — id가 정본 동일성(경로·이름은 상태).
            # id는 frontmatter 안에 있으므로 이것만은 전수 판독을 부른다.
            #
            # 동명이 모호이면 동 id도 모호다. 이름 갈래가 `('ambiguous',)`를
            # 내는 자리에서 id 갈래만 조용히 한쪽을 고르면, 같은 트리가 무엇을
            # 가리키는지가 파일 정렬 순서에 달린다.
            if name in self.dup_ids:
                return ("ambiguous",)
            hit = self.by_id.get(name)
            return ("node", hit[1]) if hit else ("dangling",)
        if "/" in name:
            p = resolve_in_root(self.vault, name)
            if p is None:
                return ("dangling",)      # vault 밖 — 참조 대상이 아니다
            for cand in (p, p.with_suffix(".md")):
                if cand.exists():
                    k = placement.space_of(self.vault, cand)
                    return (("node", k) if placement.is_node_home(k) else ("nonnode", k))
            return ("dangling",)
        # 이름 색인이 **후보들**을 주고, 그것만 열어 판정한다. 구판은 여기서
        # 전수 판독 결과(`dup_stems` → `nodes`)를 봤다 — 판정은 같고, 여는
        # 파일이 전부가 아니라 그 이름의 후보(보통 하나)뿐이다.
        #
        # 순서가 곧 규칙이다: 판독되는 것이 둘 이상이면 모호, 하나면 그것,
        # 하나도 없으면(전부 파손이거나 없음) 비노드로 떨어지고 마지막이
        # dangling이다. 구판의 `dup_stems → nodes → nonnode → dangling`과
        # 같은 사슬이며, 파손 파일은 여기서도 그 이름의 임자가 되지 못한다.
        live, _errors = self.lookup_name(name)
        if len(live) > 1:
            return ("ambiguous",)
        if len(live) == 1:
            return ("node", live[0][1])
        if name in self.nonnode:
            return ("nonnode", self.nonnode[name][1])
        return ("dangling",)


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


# 동 id 거부의 다음 행동 — 읽기·쓰기·후보 상정이 같은 말을 한다.
DUP_ID_ADVICE = (
    "id가 겹친 사본은 복제·백업 복원·동기화 충돌에서 온다(표면은 id를 겹쳐 "
    "만들지 않는다). 같은 파일로 가는 파일 링크도 한 노드를 두 자리에 두는 중복 "
    "소속이다. 충돌 후보(`record_candidate`)가 아니라 동일성 사고라 표면으로 "
    "고치지 않는다 — 링크면 링크를 지워 한 자리만 남기고, 사본이면 사용자가 두 "
    "파일을 비교해 남길 쪽을 정하고 다른 쪽의 고유한 내용을 옮긴 뒤 그 파일을 "
    "vault 밖으로 치운다. 그때까지 둘 다 이름으로도 id로도 읽거나 고치지 않는다")
