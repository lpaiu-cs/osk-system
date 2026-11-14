"""osk.placement — 파일이 어디에 놓였는가 (헌법 8조, Mechanism §1).

경로만 보고 정한다: 어느 Space의 어느 구획인지, 노드가 살 자리인지, 허브인지, 인용 기록의 자리인지.
노드 파일을 훑는 것도 여기 있다. 내용은 읽지 않는다 — 이름과 id를 파일로 잇는 것은 색인(`graph.Index`)이다."""
from __future__ import annotations
import os
import stat
from pathlib import Path
from .core import adapt_path
from .vault import Vault


def node_spaces(vault: Vault) -> tuple[str, str, str]:
    """노드가 사는 세 공간의 폴더 이름."""
    return (vault.domain, vault.person, vault.scope)


# 인용 기록 구획(헌법 4조 3항). 새 기록은 루트 `_cited/`에 쓰고(`('raw', None)`), scope
# 아래의 옛 `_cited/`·`_raw/`와 Person의 `_cited/`도 같은 이름의 인용 기록으로
# 읽는다(Mechanism §9 5항).
RECORD_DIRS = ("_cited", "_raw")


def is_record_ref(ref: str) -> bool:
    """`_cited/…`·`…/_cited/…`·`…/_raw/…` 꼴 — 인용 기록 구획을 가리키는 표기인가. 위키링크로
    감싼 표기도 같은 좌표다(루트 좌표는 `[[` 바로 뒤에 `_cited/`가 온다)."""
    s = (ref or "").replace("\\", "/").strip()
    if s.startswith("[["):
        s = s[2:].lstrip()
    return any(f"/{d}/" in "/" + s for d in RECORD_DIRS)


# `.md` 확장자는 **어느 기기에서나** 대소문자를 무시한다. 구판은 pathlib을 따라
# `os.name == "nt"`일 때만 무시했고, 그래서 같은 트리의 `Note.MD`가 Windows에서는
# 노드, macOS(APFS도 대소문자 무시가 기본이다)·Linux에서는 비노드였다 — 한 vault를
# 동기화하는 기기들이 서로 다른 노드 집합을 보았다. 노드인지는 파일명이 정하고
# 파일명은 git이 기기마다 같게 옮기므로, 판정도 호스트가 아니라 이름에서 나온다.
# 놓치면 그 노드가 색인에서 사라지고 `create_node`가 **다른 군집에 같은 이름을
# 거부 없이 만든다**(v3.7.0 실측).
def is_md(name: str) -> bool:
    return name.lower().endswith(".md")


def _off_node(parts: tuple) -> bool:
    """노드 자리 **안의** 살림 구획인가 — 점 접두 조각(파일 포함)과 밑줄 접두
    디렉토리. 점 접두는 도구의 살림살이이고(`.obsidian`·`.trash`·AppleDouble
    `._x`), 밑줄 접두 구획에는 노드를 두지 않는다(Mechanism §1 4항). 구판은
    이것을 소비자마다 따로 보았다 — 보호영역(`region_tree.region_files`)은 점
    접두를 빼는데 색인은 넣어서, 승인되지 않은 `.hidden.md`가 기억으로 읽히고
    고쳐지는 동안 영역은 clean이었다. 판정은 여기 한 벌이다."""
    return bool(parts) and (parts[-1].startswith(".") or any(
        p.startswith((".", "_")) for p in parts[:-1]))


def is_reparse(entry) -> bool:
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


def space_of(vault: Vault, path: Path) -> tuple:
    """경로 → 소속. ('domain', d) ('person', f) ('scope', s) ('workbench',)
    ('workbench-transit',) ('ledger',) ('raw', s) ('archive',) ('sources',)
    ('engine',) ('support',). vault 밖 경로는 소속이 없다 — ('support',)로
    보고 죽지 않는다(시행령 §11 — 어떤 입력에도 보류·보고).

    `resolve()`는 편의가 아니라 **봉쇄의 일부**다 — 심볼릭 링크나 `..`로 vault
    밖을 가리키는 경로가 `relative_to`에서 걸러진다. 그래서 외부에서 들어온
    경로에는 이것을 그대로 건다. 엔진 자신이 ROOT 아래를 열거하며 만든 조각은
    이미 봉쇄 안이므로 순회기는 `space_of_parts`를 직접 부른다 — 파일당
    `resolve()`가 색인 구축 전체의 32%였다(v3.7.0 실측)."""
    try:
        if path.is_relative_to(vault.root):
            path = vault.root / adapt_path(path.relative_to(vault.root).as_posix(), vault.space_roots)
        rel = path.resolve().relative_to(vault.root)
    except (ValueError, OSError):
        return ("support",)
    return space_of_parts(vault, rel.parts)


def space_of_parts(vault: Vault, parts: tuple) -> tuple:
    """**ROOT 상대** 경로 조각 → 소속. `space_of`의 판정 본체이며, 봉쇄는 하지
    않는다 — 부르는 쪽이 이미 ROOT 아래임을 아는 경우에만 직접 쓴다.

    노드 자리로 판정된 경로라도 살림 구획(`_off_node`)을 지나면 비노드다.
    `_raw`·`_ledger`·`_engine`처럼 이름이 정해진 구획은 그보다 먼저 제 소속을
    받으므로(`_raw/.records`는 그대로 raw) 이 판정이 건드리지 않는다."""
    kind = _place_of_parts(vault, parts)
    if is_node_home(kind) and (_off_node(parts[1:]) or (
            len(parts) == 2 and kind[0] != "governance" and is_md(parts[1]))):
        return ("support",)       # 노드는 군집 안에만 둔다 — Space 루트 바로 아래는 아니다
    return kind


def _place_of_parts(vault: Vault, parts: tuple) -> tuple:
    if not parts:
        return ("support",)
    head = parts[0]
    if head == RECORD_DIRS[0]:
        # 루트 `_cited/` — Space 밖의 상설 비노드 구획(헌법 4조 3항). 소속 scope가 없다.
        return ("raw", None)
    if head == vault.domain:
        return ("domain", parts[1] if len(parts) > 2 else None)
    if head == vault.person:
        if len(parts) > 1 and parts[1] == RECORD_DIRS[0]:
            # 소속 scope 없이 Person 노드를 남긴 세션의 인용 기록(Mechanism §1 2항 · 시행령 §2 8항)
            return ("raw", vault.person)
        return ("person", parts[1] if len(parts) > 2 else None)
    if head == "_governance":
        # 통치 구획 — Space 밖의 상설 구획. 통치 문서·사료는 특수한 노드다
        # (헌법 3조 6항 — 검색·중심성 불산입, 표면 쓰기 거부, 명시 조회
        # 도달). `_engine/`은 그 하위의 엔진 구획이다.
        if "_engine" in parts:
            return ("engine",)
        return ("governance",)
    if head == vault.scope:
        s = parts[1] if len(parts) > 1 else None
        if s == "Workbench":
            if "_ledger" in parts:
                return ("ledger",)
            if any(d in parts for d in RECORD_DIRS):
                return ("raw", "Workbench")
            if "transit" in parts:
                return ("workbench-transit",)
            return ("workbench",)
        if any(d in parts for d in RECORD_DIRS):
            return ("raw", s)
        return ("scope", s)
    if head == "_sources":
        return ("sources",)
    if head == "_engine":
        return ("engine",)
    return ("support",)


def is_hub(path: Path) -> bool:
    """허브 노드인가 — **군집 폴더와 이름이 같은 노드**가 그 군집의 허브다
    (헌법 3조 8항 · 시행령 §3 6항). 최상위든 하위든 같은 식이다.

    허브는 노드의 **종이 아니라 자리**이므로 frontmatter에 표식을 두지 않는다
    (시행령 §3 7항 — "하위 허브는 별도의 종이 아니라 그 역할을 맡은 보통
    노드"). 배치가 정본이라는 원칙(§3 3항)에서 읽어 내는 것이지 새로 선언하는
    것이 아니다. 이 판정이 서야 검증기가 층을 가로질러 도달을 셀 수 있다."""
    return path.parent.name == path.stem


def hub_file(d: Path) -> Path | None:
    """군집 `d`의 허브 파일, 없으면 None. 확장자는 대소문자를 가리지 않는다
    (Mechanism §1 4항) — 대소문자를 가리는 파일시스템의 `<이름>.MD`도 허브다."""
    p = d / f"{d.name}.md"
    if p.is_file():
        return p
    try:
        with os.scandir(d) as it:
            for e in it:
                if is_md(e.name) and e.name[:-3] == d.name and e.is_file():
                    return Path(e.path)
    except OSError:
        pass
    return None


def is_node_home(kind: tuple) -> bool:
    """노드 군집은 선언표의 `= ` Space 경로·transit과, 유일한 밑줄 예외인
    통치 구획뿐 (Mechanism §1 4항)."""
    return kind[0] in ("domain", "person", "scope", "workbench-transit",
                       "governance")


def scan(root: Path, prefix: tuple, errors: list | None = None):
    """`root` 아래를 `os.scandir`로 훑어 (경로, ROOT 상대 조각)을 낸다.

    `Path.rglob`은 항목마다 `Path` 객체를 짓는다. 3.12의 pathlib은 순회 자체에
    이미 `scandir`을 쓰므로 rglob이 파일당 `stat`을 부르지는 않는다 — 실측으로
    20,000 파일 순회에 `os.stat` 1회다. 값이 비싼 것은 그렇게 만든 `Path`를
    **소비자가 다시 묻는** 자리다: 구판은 `space_of`의 `resolve()`로 파일당
    1회, 비노드 순회의 `is_file()`로 항목당 1회를 물었다(실측 20,000·20,050회).
    `scandir`은 이름·종류·stat을 한 번의 디렉토리 판독에서 함께 받으므로 그
    둘이 0회다. 조각을 내려가며 이어 붙이는 것이 요점이다 — 그래야 소비자가
    소속을 알기 위해 `resolve()`를 다시 부르지 않는다.

    **리파스 포인트 디렉토리는 내려가지 않는다**(`is_reparse` 참조). 따라
    내려가면 vault 밖의 트리가 안쪽 경로 조각을 뒤집어쓴 채 색인에 들어와
    봉쇄가 무의미해지고, 순환 링크에서는 순회가 끝나지 않는다."""
    # 시작 Space도 자식 디렉터리와 같은 봉쇄를 거친다.
    if is_reparse(root):
        if errors is not None:
            errors.append(f"열거 불가: 리파스 포인트인 시작 구획 — {root}")
        return
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
                    if not is_reparse(e):
                        stack.append((Path(e.path), pref + (e.name,)))
                else:
                    yield e, pref + (e.name,)
            except OSError as ex:
                if errors is not None:
                    errors.append(f"항목 판독 불가: {d} — {ex}")
                continue


def iter_nodes(vault: Vault, errors: list | None = None, dirents: dict | None = None):
    # 통치 구획의 통치 문서·사료는 특수한 노드다(시행령 §10 1항) — 색인에
    # 있어야 명시 조회(read_node)가 도달하고 갱신 후 승인(수용 기록)이
    # 성립한다. `_engine`의 .md는 소속 판정이 ("engine",)으로 걸러낸다.
    #
    # 정렬을 유지하는 것은 취향이 아니다 — 동명 노드가 둘이면 색인에서 **뒤에
    # 오는 쪽이 이긴다.** 순서가 기기마다 다르면 같은 트리가 기기마다 다른
    # 노드를 가리키게 된다. 구판의 `sorted(rglob(...))`과 같은 키로 접는다.
    for base in node_spaces(vault) + ("_governance",):
        root = vault.root / base
        if not root.exists():
            continue
        found = []
        for e, parts in scan(root, (base,), errors):
            if not is_md(e.name):
                continue
            # 리파스 파일만 옛 경로로 보낸다 — 그것이 vault 밖을 가리키면
            # `resolve()`가 걸러야 하고, 조각만 보는 판정은 못 본다.
            p = Path(e.path)
            found.append((p, space_of(vault, p) if is_reparse(e)
                          else space_of_parts(vault, parts)))
            if dirents is not None:
                dirents[p] = e        # 디렉토리 판독의 stat — 동 id 후보표가 쓴다
        for p, k in sorted(found, key=lambda x: x[0]):
            if is_node_home(k):
                yield p, k


def _vault_md(vault: Vault):
    """vault의 md 전수. 점(.)으로 시작하는 도구 구획(.git·.venv·.obsidian 등)은
    저장소의 살림살이지 vault의 배치가 아니다 — 순회에서 뺀다."""
    if not vault.root.exists():
        return
    for top in sorted(vault.root.iterdir()):
        if top.name.startswith(".") or top.name == "__pycache__":
            continue
        if top.is_dir():
            yield from sorted(p for p in top.rglob("*")
                              if is_md(p.name) and p.is_file())
        elif is_md(top.name):
            yield top


def layout_violations(vault: Vault) -> list[str]:
    """노드형(frontmatter 보유) 파일은 선언표의 노드 군집에만 둘 수 있다
    (Mechanism §1 4항 — 통치 구획을 제외한 `_` 구획·Workbench 루트·비선언
    루트 디렉토리는 노드를 두지 않는다)."""
    errs = []
    for p in _vault_md(vault):
        k = space_of(vault, p)
        if is_node_home(k):
            continue
        if p.read_text(encoding="utf-8", errors="ignore").startswith("---\n"):
            errs.append(f"비노드 구획에 노드형 파일: {p.relative_to(vault.root)} ({k[0]})")
    return errs


# ── scope 이름과 space 표기 ──────────────────────────────────────────────
# 배치가 정본이므로(시행령 §3 3항) scope 목록은 디렉토리에서 읽는다. `raw`와
# `wm`이 같은 판정을 쓰므로 여기 한 벌만 둔다 — 두 벌이면 조용히 갈라진다.
def scope_names(vault: Vault) -> list[str]:
    """`00_Scope/` 아래의 scope 이름. Workbench도 하나의 scope다(헌법 4조 5항).
    살림 구획(`_off_node` — `.x`·`_x`)은 scope가 아니다."""
    d = vault.root / vault.scope
    return sorted(x.name for x in d.iterdir()
                  if x.is_dir() and space_of_parts(vault,
                      (vault.scope, x.name, "x.md")) != ("support",)) if d.is_dir() else []


def space_list(vault: Vault) -> str:
    """거부 메시지에 싣는 유효 space 목록 — 틀린 값을 준 호출자가 다음에 무엇을
    써야 하는지 그 자리에서 알 수 있어야 한다."""
    return ", ".join(f"{vault.scope}/{s}" for s in scope_names(vault))


def scope_of_space(vault: Vault, space: str) -> str | None:
    """`"00_Scope/<이름>"` → `<이름>`. 맨 이름은 접지 않는다 — `create_node`가
    맨 이름을 거부하는 것과 같은 규율이며, 같은 표면에서 같은 인자가 다른
    관대함을 가지면 호출자가 규칙을 하나로 배우지 못한다."""
    parts = [x for x in adapt_path((space or "").strip().strip("/"), vault.space_roots).split("/") if x]
    return parts[1] if len(parts) == 2 and parts[0] == vault.scope else None
