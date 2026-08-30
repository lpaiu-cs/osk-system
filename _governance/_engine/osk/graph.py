"""osk.graph — 공간 배치·참조 위상·중심성.

구현 근거: Mechanism §1(선언표·`_` 규칙), 헌법 8조(참조 위상·conflicts 예외,
Domain의 _raw 직접 참조 금지), 헌법 4조 5항(Workbench 예외),
헌법 11조 2항 + 시행령 §7 1항(중심성 산입: 노드 향 Link·derived-from,
conflicts·비노드 대상·Workbench 비산입).
"""
from __future__ import annotations
import os
import re
import stat
import time
from pathlib import Path

from .core import ROOT, LEDGER, ID_RE, resolve_in_root
from . import contract


def _load_cases() -> dict[str, dict]:
    """사건부 헤더 일괄 로드 — conflicts 적격 판정용."""
    from . import signatures as _sig
    out = {}
    cdir = LEDGER / "case"
    if cdir.exists():
        for f in sorted(cdir.glob("CASE-*.md")):
            c = _sig.parse_case(f)
            if c:
                out[f.stem] = c
    return out

NODE_SPACES = ("= Domain", "= Person", "= Scope")
W_LINK, W_DERIVED = 1.0, 3.0  # 계수는 mechanism 재량 — 초기값 (Link·derived-from)

# `Path.rglob("*.md")`은 Windows에서 **대소문자를 무시한다** — pathlib이
# `os.name != "nt"`로 대소문자 구분을 정하기 때문이다(실측: rglob이 `Upper.MD`·
# `Mixed.Md`를 함께 낸다). 순회를 손으로 짜면서 이것을 놓치면 그 노드가 색인에서
# 통째로 사라지고, `create_node`가 **다른 군집에 같은 이름을 거부 없이 만든다** —
# 표면이 스스로 전역 동명 중복을 만드는 것이며 검증기도 그것을 보지 못한다.
_MD_CASE_INSENSITIVE = os.name == "nt"


def _is_md(name: str) -> bool:
    return (name.lower() if _MD_CASE_INSENSITIVE else name).endswith(".md")


def _is_reparse(entry) -> bool:
    """이 항목이 **다른 곳을 가리킬 수 있는가** — 심볼릭 링크·정션·마운트 지점.

    `is_symlink()`만으로는 부족하다. CPython은 리파스 태그가
    `IO_REPARSE_TAG_SYMLINK`인 것만 심볼릭 링크로 보므로 **Windows 정션은
    거짓을 받는다**(실측: `is_dir(follow_symlinks=False)=True,
    is_symlink()=False`). 정션은 관리자 권한 없이 만들어지고, 그것을 따라
    내려가면 vault 밖 파일이 안쪽 경로 조각을 뒤집어쓴 채 노드로 색인되어
    쓰기 통로가 vault 밖에 쓰게 된다. 판정 불가는 봉쇄 쪽으로 넘어진다."""
    try:
        if entry.is_symlink():
            return True
    except OSError:
        return True
    if os.name != "nt":
        return False
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return True
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


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
_racy_margin_cache: int | None = None


def _probe_mtime_granularity(rounds: int = 40) -> int | None:
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
    고른다."""
    try:
        d = ROOT / ".osk"
        d.mkdir(parents=True, exist_ok=True)
        probe = d / "mtime-probe"
        deltas = []
        for _ in range(rounds):
            probe.write_bytes(b"a" * 64)
            a = probe.stat().st_mtime_ns
            probe.write_bytes(b"b" * 64)
            b = probe.stat().st_mtime_ns
            if b > a:
                deltas.append(b - a)
        probe.unlink(missing_ok=True)
    except OSError:
        return None
    return min(deltas) if deltas else None


def racy_margin_ns() -> int:
    """서명을 믿지 않을 시간 폭. 프로세스마다 한 번 재고 접어 둔다."""
    global _racy_margin_cache
    if _racy_margin_cache is None:
        g = _probe_mtime_granularity()
        _racy_margin_cache = (_RACY_CEIL_NS if g is None else
                              max(_RACY_FLOOR_NS, min(_RACY_CEIL_NS, g * 8)))
    return _racy_margin_cache


def space_of(path: Path) -> tuple:
    """경로 → 소속. ('domain', d) ('person', f) ('scope', s) ('workbench',)
    ('workbench-transit',) ('ledger',) ('raw', s) ('archive',) ('sources',)
    ('engine',) ('support',). vault 밖 경로는 소속이 없다 — ('support',)로
    보고 죽지 않는다(시행령 §11 — 어떤 입력에도 보류·보고).

    `resolve()`는 편의가 아니라 **봉쇄의 일부**다 — 심볼릭 링크나 `..`로 vault
    밖을 가리키는 경로가 `relative_to`에서 걸러진다. 그래서 외부에서 들어온
    경로에는 이것을 그대로 건다. 엔진 자신이 ROOT 아래를 열거하며 만든 조각은
    이미 봉쇄 안이므로 순회기는 `_space_of_parts`를 직접 부른다 — 파일당
    `resolve()`가 색인 구축 전체의 32%였다(v3.7.0 실측)."""
    try:
        rel = path.resolve().relative_to(ROOT)
    except (ValueError, OSError):
        return ("support",)
    return _space_of_parts(rel.parts)


def _space_of_parts(parts: tuple) -> tuple:
    """**ROOT 상대** 경로 조각 → 소속. `space_of`의 판정 본체이며, 봉쇄는 하지
    않는다 — 부르는 쪽이 이미 ROOT 아래임을 아는 경우에만 직접 쓴다."""
    if not parts:
        return ("support",)
    head = parts[0]
    if head == "= Domain":
        return ("domain", parts[1] if len(parts) > 2 else None)
    if head == "= Person":
        return ("person", parts[1] if len(parts) > 2 else None)
    if head == "_governance":
        # 통치 구획 — Space 밖의 상설 구획. 통치 문서·사료는 특수한 노드다
        # (헌법 3조 6항 — 검색·중심성 불산입, 표면 쓰기 거부, 명시 조회
        # 도달). `_engine/`은 그 하위의 엔진 구획이다.
        if "_engine" in parts:
            return ("engine",)
        return ("governance",)
    if head == "= Scope":
        s = parts[1] if len(parts) > 1 else None
        if s == "Workbench":
            if "_ledger" in parts:
                return ("ledger",)
            if "_raw" in parts:
                return ("raw", "Workbench")
            if "transit" in parts:
                return ("workbench-transit",)
            return ("workbench",)
        if "_raw" in parts:
            return ("raw", s)
        return ("scope", s)
    if head == "_sources":
        return ("sources",)
    if head == "_engine":
        return ("engine",)
    return ("support",)


def is_node_home(kind: tuple) -> bool:
    """노드 군집은 선언표의 `= ` Space 경로·transit과, 유일한 밑줄 예외인
    통치 구획뿐 (Mechanism §1 4항)."""
    return kind[0] in ("domain", "person", "scope", "workbench-transit",
                       "governance")


def _scan(root: Path, prefix: tuple, errors: list | None = None):
    """`root` 아래를 `os.scandir`로 훑어 (경로, ROOT 상대 조각)을 낸다.

    `Path.rglob`은 항목마다 `Path` 객체를 짓는다. 3.12의 pathlib은 순회 자체에
    이미 `scandir`을 쓰므로 rglob이 파일당 `stat`을 부르지는 않는다 — 실측으로
    20,000 파일 순회에 `os.stat` 1회다. 값이 비싼 것은 그렇게 만든 `Path`를
    **소비자가 다시 묻는** 자리다: 구판은 `space_of`의 `resolve()`로 파일당
    1회, 비노드 순회의 `is_file()`로 항목당 1회를 물었다(실측 20,000·20,050회).
    `scandir`은 이름·종류·stat을 한 번의 디렉토리 판독에서 함께 받으므로 그
    둘이 0회다. 조각을 내려가며 이어 붙이는 것이 요점이다 — 그래야 소비자가
    소속을 알기 위해 `resolve()`를 다시 부르지 않는다.

    **리파스 포인트 디렉토리는 내려가지 않는다**(`_is_reparse` 참조). 따라
    내려가면 vault 밖의 트리가 안쪽 경로 조각을 뒤집어쓴 채 색인에 들어와
    봉쇄가 무의미해지고, 순환 링크에서는 순회가 끝나지 않는다."""
    stack = [(root, prefix)]
    while stack:
        d, pref = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError as ex:
            # 삼키지 않는다. 구판은 여기서 `continue`했고, 그래서 디렉토리
            # 하나가 안 읽히면 이름 색인이 그 노드를 못 보고 **다른 군집에 같은
            # 이름이 거부 없이 만들어졌다**(실측 재현). 관측이 불완전하면
            # 유일성을 말할 수 없으므로, 그 사실을 위로 올려 쓰기가 거부하게
            # 한다(시행령 §11 — 실패는 보류·보고).
            if errors is not None:
                errors.append(f"열거 불가: {d} — {ex}")
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if not _is_reparse(e):
                        stack.append((Path(e.path), pref + (e.name,)))
                else:
                    yield e, pref + (e.name,)
            except OSError as ex:
                if errors is not None:
                    errors.append(f"항목 판독 불가: {d} — {ex}")
                continue


def iter_nodes(errors: list | None = None):
    # 통치 구획의 통치 문서·사료는 특수한 노드다(시행령 §10 1항) — 색인에
    # 있어야 명시 조회(read_node)가 도달하고 갱신 후 승인(수용 기록)이
    # 성립한다. `_engine`의 .md는 소속 판정이 ("engine",)으로 걸러낸다.
    #
    # 정렬을 유지하는 것은 취향이 아니다 — 동명 노드가 둘이면 색인에서 **뒤에
    # 오는 쪽이 이긴다.** 순서가 기기마다 다르면 같은 트리가 기기마다 다른
    # 노드를 가리키게 된다. 구판의 `sorted(rglob(...))`과 같은 키로 접는다.
    for base in NODE_SPACES + ("_governance",):
        root = ROOT / base
        if not root.exists():
            continue
        found = []
        for e, parts in _scan(root, (base,), errors):
            if not _is_md(e.name):
                continue
            # 리파스 파일만 옛 경로로 보낸다 — 그것이 vault 밖을 가리키면
            # `resolve()`가 걸러야 하고, 조각만 보는 판정은 못 본다.
            p = Path(e.path)
            found.append((p, space_of(p) if _is_reparse(e)
                          else _space_of_parts(parts)))
        for p, k in sorted(found, key=lambda x: x[0]):
            if is_node_home(k):
                yield p, k


def index_signature(errors: list | None = None):
    """`Index`가 읽는 것의 (상대경로, mtime_ns, 크기)와, 그중 채취 시각에 너무
    가까워 믿을 수 없는 항목이 있었는가.

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
    margin = racy_margin_ns()
    now = time.time_ns()
    lo, hi = now - margin, now + margin
    for base in NODE_SPACES + ("_governance", "_sources"):
        root = ROOT / base
        if not root.exists():
            continue
        for e, parts in _scan(root, (base,), errors):
            k = _space_of_parts(parts)
            if is_node_home(k):
                if not _is_md(e.name):
                    continue
            elif k[0] not in ("raw", "sources", "ledger"):
                continue
            try:
                st = e.stat()
            except OSError:
                continue   # 열거와 stat 사이의 삭제 — 다음 호출의 지문이 다르다
            # 창은 **양쪽**으로 닫는다. 아래만 보면 mtime이 미래인 파일 하나가
            # 영영 racy로 남아 읽기 캐시가 **영구히** 죽는다(실측: 20k에서
            # 80 ms → 6.8~9.2 s). 시계 역행·듀얼부팅 RTC·백업 복원·NAS 시계가
            # 전부 그 방아쇠다. 먼 미래의 시각은 그 자체로 안정적이라 다음
            # 쓰기와 구별되므로 의심할 이유가 없다.
            if lo <= st.st_mtime_ns <= hi:
                racy = True
            out.append(("/".join(parts), st.st_mtime_ns, st.st_size))
    out.sort()
    return out, racy


def _vault_md():
    """vault의 md 전수. 점(.)으로 시작하는 도구 구획(.git·.venv·.obsidian 등)은
    저장소의 살림살이지 vault의 배치가 아니다 — 순회에서 뺀다."""
    if not ROOT.exists():
        return
    for top in sorted(ROOT.iterdir()):
        if top.name.startswith(".") or top.name == "__pycache__":
            continue
        if top.is_dir():
            yield from sorted(top.rglob("*.md"))
        elif top.suffix == ".md":
            yield top


def layout_violations() -> list[str]:
    """노드형(frontmatter 보유) 파일은 선언표의 노드 군집에만 둘 수 있다
    (Mechanism §1 4항 — 통치 구획을 제외한 `_` 구획·Workbench 루트·비선언
    루트 디렉토리는 노드를 두지 않는다)."""
    errs = []
    for p in _vault_md():
        k = space_of(p)
        if is_node_home(k):
            continue
        if p.read_text(encoding="utf-8", errors="ignore").startswith("---\n"):
            errs.append(f"비노드 구획에 노드형 파일: {p.relative_to(ROOT)} ({k[0]})")
    return errs


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

    def __init__(self):
        # 관측이 불완전하면(디렉토리 하나라도 못 읽었으면) 유일성을 말할 수
        # 없다. 그 사실을 색인이 들고 있어야 쓰기가 거부할 수 있다.
        self.scan_errors: list[str] = []
        self._entries: list[tuple[Path, tuple]] = list(
            iter_nodes(self.scan_errors))
        self.names: dict[str, tuple[Path, tuple]] = {}
        self._by_name: dict[str, list[tuple[Path, tuple]]] = {}
        self.parsed: dict[Path, contract.Node] = {}
        self._failed: dict[Path, str] = {}
        self._all_parsed = False
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
        self.nonnode: dict[str, tuple] = {}
        for base in ("_sources", "= Scope", "= Person"):
            root = ROOT / base
            if not root.exists():
                continue
            found = []
            for e, parts in _scan(root, (base,), self.scan_errors):
                p = Path(e.path)
                k = space_of(p) if _is_reparse(e) else _space_of_parts(parts)
                if k[0] in ("raw", "sources", "ledger"):
                    found.append((p, k))
            for p, k in sorted(found, key=lambda x: x[0]):
                self.nonnode[p.stem] = (p, k)

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
        except Exception as e:
            # 판독 불가 파일(임시 메모 등)은 노드에서 분리해 소비자가 건너뛰게
            # 하고 별도로 보고한다 — 하나가 검증기·검색 전체를 죽이지 않는다
            # (시행령 §11 — 실패는 보류·보고).
            self._failed[p] = f"{p.relative_to(ROOT)}: {e}"
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
        for p, k in self._entries:
            if not self._readable(p):
                why = self._failed[p]
                broken[p.stem] = (f"{broken[p.stem]}; {why}"
                                  if p.stem in broken else why)
                continue
            if p.stem in nodes:
                dup.setdefault(p.stem, [
                    str(nodes[p.stem][0].relative_to(ROOT))]).append(
                    str(p.relative_to(ROOT)))
            nodes[p.stem] = (p, k)
            nid = self.parsed[p].id
            if nid:
                by_id[nid] = (p, k)
        self._nodes, self._by_id = nodes, by_id
        self._broken, self._dup_stems = broken, dup
        self._all_parsed = True

    @property
    def nodes(self) -> dict[str, tuple[Path, tuple]]:
        """판독되는 노드만 (stem → 경로·소속). 파손 파일은 여기 없다."""
        self.parse_all()
        return self._nodes

    @property
    def by_id(self) -> dict[str, tuple[Path, tuple]]:
        """id → 노드 (derived-from 해석). 동일성의 정본은 id다."""
        self.parse_all()
        return self._by_id

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
        **소속이 없어서**(1-튜플) `_topology_of`의 다음 줄 `tkind = r[1]`이
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
        하는 소비자(`_dangling_of`)가 자기 자신을 가리키는 Link를 dangling으로
        오보하지 않게 한다. 이름 색인은 생성 시점의 스냅숏이라 그냥 두면 방금
        만든 이름이 없는 것으로 읽힌다."""
        self.names.setdefault(path.stem, (path, kind))
        lst = self._by_name.setdefault(path.stem, [])
        if all(p != path for p, _k in lst):
            lst.append((path, kind))

    def retarget(self, src: Path, dst: Path, kind: tuple) -> None:
        """이동을 손에 든 색인에 반영한다. 안 하면 `_dangling_of`가 이동 **후**
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

    def node(self, path: Path) -> contract.Node:
        if path not in self.parsed:
            self.parsed[path] = contract.parse(path)
        return self.parsed[path]

    def resolve(self, name: str):
        """대상명 → ('node',소속) | ('nonnode',소속) | ('ambiguous',) |
        ('dangling',) | ('external',). 경로형([[= Scope/B/b]])은 경로로 우선
        해석한다 — 파일명 우회를 막는다. 다만 URL은 `/`가 있어도 경로가
        아니며, 경로 해석은 vault 안으로 봉쇄한다([[/etc/passwd]])."""
        if re.match(r"^https?://", name):
            return ("external",)
        if re.match(ID_RE, name):
            # derived-from 노드 대상 — id가 정본 동일성(경로·이름은 상태).
            # id는 frontmatter 안에 있으므로 이것만은 전수 판독을 부른다.
            hit = self.by_id.get(name)
            return ("node", hit[1]) if hit else ("dangling",)
        if "/" in name:
            p = resolve_in_root(name)
            if p is None:
                return ("dangling",)      # vault 밖 — 참조 대상이 아니다
            for cand in (p, p.with_suffix(".md")):
                if cand.exists():
                    k = space_of(cand)
                    return (("node", k) if is_node_home(k) else ("nonnode", k))
            return ("dangling",)
        # 이름 색인이 **후보들**을 주고, 그것만 열어 판정한다. 구판은 여기서
        # 전수 판독 결과(`dup_stems` → `nodes`)를 봤다 — 판정은 같고, 여는
        # 파일이 전부가 아니라 그 이름의 후보(보통 하나)뿐이다.
        #
        # 순서가 곧 규칙이다: 판독되는 것이 둘 이상이면 모호, 하나면 그것,
        # 하나도 없으면(전부 파손이거나 없음) 비노드로 떨어지고 마지막이
        # dangling이다. 구판의 `dup_stems → nodes → nonnode → dangling`과
        # 같은 사슬이며, 파손 파일은 여기서도 그 이름의 임자가 되지 못한다.
        cands = self._by_name.get(name)
        if cands:
            live = [(p, k) for p, k in cands if self._readable(p)]
            if len(live) > 1:
                return ("ambiguous",)
            if len(live) == 1:
                return ("node", live[0][1])
        if name in self.nonnode:
            return ("nonnode", self.nonnode[name][1])
        return ("dangling",)


def topology_check(idx: Index) -> list[str]:
    """참조 위상 검증 (헌법 8조 3항 + 4조 5항 + conflicts 예외)."""
    errs = []
    cases = None
    for stem, (p, kind) in idx.nodes.items():
        n = idx.node(p)
        targets = [(t, "pe", pred) for pred in contract.PREDICATES
                   for t in n.edges(pred)]
        targets += [(t, "link", None) for t in n.wikilinks()]
        for name, tclass, pred in targets:
            r = idx.resolve(name)
            if pred == "conflicts":
                # 사건 표지의 예외 — 열린(docketed) 사건 또는 존치 판결 결속 노드뿐
                if cases is None:
                    cases = _load_cases()
                if r[0] == "nonnode" and r[1][0] == "ledger":
                    # 헌법 12조 5항 — 사건을 참조하는 것은 **당사자 노드**뿐이다
                    case = cases.get(name) or {}
                    parties = [str(x) for x in (case.get("parties") or [])]
                    if str(case.get("case_no")) != name \
                            or str(case.get("status")) != "docketed":
                        errs.append(
                            f"conflicts 대상 부적격(열린 사건 아님): {stem} → {name}")
                    elif n.id not in parties:
                        errs.append(
                            f"conflicts 대상 부적격(사건의 당사자 아님): {stem} → {name}")
                    continue
                if r[0] == "node":
                    tp = idx.nodes.get(name)
                    back = idx.node(tp[0]).edges("conflicts") if tp else []
                    my_id = n.id
                    other_id = idx.node(tp[0]).id if tp else None
                    bound = any(
                        # 판결은 사용자가 내린다 — 미종결 사건에 verdict를
                        # 선기입해 결속을 얻을 수 없다(restore의 ⓑ와 대칭)
                        str(c.get("status")) == "adjudicated"
                        and str(c.get("verdict")) == "존치"
                        and my_id in [str(x) for x in (c.get("parties") or [])]
                        and other_id in [str(x) for x in (c.get("parties") or [])]
                        for c in cases.values())
                    if stem in back and bound:
                        continue  # 존치 상호 치환 + 실재 존치 사건 결속
                    errs.append(
                        f"conflicts 존치 결속 실패: {stem} → {name} "
                        f"(상호={stem in back}, 존치 사건={bound})")
                    continue
                errs.append(f"conflicts 대상 부적격: {stem} → {name} ({r[0]})")
                continue
            if pred == "derived-from" and not re.match(ID_RE, name) \
                    and r[0] in ("node", "ambiguous"):
                # 노드 근거의 동일성은 id다 — 경로·이름은 상태이므로 이동·개명에
                # 끊어지고, 같은 이름이 재사용되면 다른 노드를 가리킨다
                # (Mechanism §8 2항 · 헌법 8조 5항).
                errs.append(f"derived-from 노드 대상은 id로 단다: {stem} → {name}")
                continue
            if r[0] in ("external", "dangling"):
                continue  # 외부·미해석 — 소속 제한 없음 (dangling은 경고로 별도 보고)
            if r[0] == "ambiguous":
                errs.append(f"모호 참조(동명 노드 중복): {stem} → {name}")
                continue
            tkind = r[1]
            if kind[0] == "domain" and tkind[0] == "raw":
                errs.append(f"Domain의 _raw 직접 참조: {stem} → {name}")
                continue
            if kind[0] == "scope":
                ok = (
                    (tkind[0] == "scope" and tkind[1] == kind[1])
                    or (tkind[0] == "raw" and tkind[1] == kind[1])
                    or tkind[0] in ("domain", "person", "workbench-transit",
                                    "sources", "governance")
                )
                if not ok:
                    errs.append(
                        f"작업 상태는 근거로 쓰지 않는다: {stem} → {name} "
                        f"(Workbench 계약 4.2)" if tkind[0] == "workbench"
                        else f"scope 간 직접 참조: [{kind[1]}] {stem} → {name} {tkind}")
    return errs


def dangling_refs(idx: Index) -> list[str]:
    """미해석 참조 목록 — 위반이 아니라 경고(탐색 링크는 자유)."""
    out = []
    for stem, (p, kind) in idx.nodes.items():
        n = idx.node(p)
        for t in set(n.wikilinks()):
            if idx.resolve(t)[0] == "dangling":
                out.append(f"{stem} → {t}")
    return sorted(out)


def centrality(idx: Index) -> dict[str, float]:
    """중심성 = 개정 비용의 근사(들어오는 의존 가중합). 노드 간 참조만 산입.
    Workbench(헌법 4조 5항)와 통치 구획(시행령 §10 1항)은 출발·도착 모두
    불산입이다."""
    EXCL = ("workbench-transit", "governance")
    score: dict[str, float] = {s: 0.0 for s in idx.nodes}
    for stem, (p, kind) in idx.nodes.items():
        if kind[0] in EXCL:
            continue
        n = idx.node(p)
        for t in n.edges("derived-from"):
            r = idx.resolve(t)
            if r[0] == "node" and r[1][0] not in EXCL:
                score[_score_key(idx, t)] = score.get(_score_key(idx, t), 0.0) + W_DERIVED
        for t in n.wikilinks():
            r = idx.resolve(t)
            if r[0] == "node" and r[1][0] not in EXCL:
                score[_score_key(idx, t)] = score.get(_score_key(idx, t), 0.0) + W_LINK
    return score


def _score_key(idx: "Index", t: str) -> str:
    """중심성 점수는 노드 **stem**으로 키잡는다(score 초기화가 stem이므로).
    대상 표기를 그 노드의 stem으로 접는다 — id형은 by_id로, 경로형·stem형은
    마지막 요소 stem으로. 경로형(`[[= Scope/X/design]]`)을 raw 문자열로 키잡으면
    참조된 노드가 유입 중심성을 통째로 놓친다."""
    if re.match(ID_RE, t) and t in idx.by_id:
        return idx.by_id[t][0].stem
    return contract.target_stem(t)


# ── scope 이름과 space 표기 ──────────────────────────────────────────────
# 배치가 정본이므로(시행령 §3 3항) scope 목록은 디렉토리에서 읽는다. `raw`와
# `wm`이 같은 판정을 쓰므로 여기 한 벌만 둔다 — 두 벌이면 조용히 갈라진다.

def scope_names() -> list[str]:
    """`= Scope/` 아래의 scope 이름. Workbench도 하나의 scope다(헌법 4조 5항)."""
    d = ROOT / "= Scope"
    return sorted(x.name for x in d.iterdir()
                  if x.is_dir() and not x.name.startswith(".")) if d.is_dir() else []


def space_list() -> str:
    """거부 메시지에 싣는 유효 space 목록 — 틀린 값을 준 호출자가 다음에 무엇을
    써야 하는지 그 자리에서 알 수 있어야 한다."""
    return ", ".join(f"= Scope/{s}" for s in scope_names())


def scope_of_space(space: str) -> str | None:
    """`"= Scope/<이름>"` → `<이름>`. 맨 이름은 접지 않는다 — `create_node`가
    맨 이름을 거부하는 것과 같은 규율이며, 같은 표면에서 같은 인자가 다른
    관대함을 가지면 호출자가 규칙을 하나로 배우지 못한다."""
    parts = [x for x in (space or "").strip().strip("/").split("/") if x]
    return parts[1] if len(parts) == 2 and parts[0] == "= Scope" else None
