"""osk.naming — 제목이 곧 파일명일 때의 이름 규칙.

구현 근거: Mechanism §2 3항(파일명은 제목이다)·§9 5항(기록 이름은 노드 제목과 같은 이식성 규칙을 받는다).
노드 제목, 군집 이름, 인용 기록 이름이 같은 규칙을 쓴다. 파일을 고치지 않는다 — 판정만 한다.
"""
from __future__ import annotations

import unicodedata
from pathlib import Path

from .errors import WriteError

# 제목은 곧 파일명이다. 인스턴스가 여러 기기에서 같은 트리를 git으로 공유하면,
# **한쪽에서만 만들 수 있는 이름**은 다른 쪽의 체크아웃을 통째로 막는다 — 콜론이
# 든 노드 하나 때문에 Windows에서 `git reset --hard origin/main`이 그 파일 하나가
# 아니라 **전량** `invalid path`로 실패한 사례가 있다. 소비자 쪽에서 우회하지 않고,
# 이름이 만들어지는 이 자리에서 막는다.
_BAD_TITLE_CHARS = '<>:"|?*\\/'
# MS "Naming Files, Paths, and Namespaces"의 열거 + CreateFile "Consoles"가
# 콘솔 장치로 지정하는 `CONIN$`·`CONOUT$`. COM0·LPT0은 어느 쪽에도 없어 넣지 않는다.
# 위첨자 ¹²³은 Windows가 **숫자로 읽어** COM#·LPT#로 취급한다는 문서 근거로 넣었다.
#
# 실측(Windows 11 + git 2.x): `CON.md`·`NUL.md`·`COM1.md`·`CONIN$.md`는 디스크에는
# 만들어져도 `git add`가 `No such file or directory`로 실패하며, **그 하나가 add
# 전체를 rc=128로 중단시킨다** — 콜론 사고와 같은 폭발 반경이다. `COM0.md`와
# `LPT¹.md`는 이 조합에서는 통과했다(위첨자 차단은 문서 근거의 예방적 조치다).
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10)),
                 "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}

# 파일명 한 구성요소의 상한. ext4는 255 **바이트**, NTFS는 255 **문자**라 단위가
# 다르다 — 한글 제목은 문자 수로는 여유로워도 UTF-8 바이트로는 넘칠 수 있다
# (한글 85자 + `.md` = 258바이트). 표면 스키마의 `max_length=120`은 문자 단위라
# 이 제약을 표현하지 못한다.
MAX_FILENAME_BYTES = 255

# 아래 둘은 파일명으로는 멀쩡하지만 **이 체계 자신의 Link 문법**을 깬다. 본문
# Link 파서가 `\[\[([^\]#|]+)`이라 `#`·`]`·`|`에서 대상명이 잘린다 — 그런 제목의
# 노드는 만들어지긴 해도 **아무도 링크로 가리킬 수 없다**(실측: `[[PR#1 판정]]`
# → 대상 `PR`). `|`는 위 파일명 집합이 이미 막으므로 여기서는 나머지 둘이다.
_LINK_BREAKING_CHARS = "#]"


def title_errors(title: str) -> list[str]:
    """제목이 동기화 대상 **모든** 기기에서 파일명이 될 수 있는가.

    검사 대상은 **원본 문자열**이다. 여기서 `strip()`을 하면 검사 대상이 실제로
    파일명이 되는 문자열(`dest_dir / f"{title}.md"`)과 갈라져, 후행 공백과 양끝
    제어문자가 검사를 통과한 뒤 파일명에 그대로 들어간다 — 검사는 `foo`를 보고
    파일은 `foo .md`가 된다."""
    t = title or ""
    if not t.strip():
        return ["부적격 제목: 비어 있다"]
    errs = []
    if t != t.strip():
        errs.append("제목 앞뒤에 공백을 둘 수 없다 — 그대로 파일명이 되는데 "
                    "Windows는 후행 공백을 조용히 잘라낸다")
    bad = sorted({c for c in t if c in _BAD_TITLE_CHARS})
    if bad:
        errs.append(
            f"제목에 쓸 수 없는 문자: {' '.join(bad)} — 제목이 곧 파일명이라 "
            f"Windows에서 만들 수 없고, 그 기기의 체크아웃 전체를 막는다 "
            f"(`/`는 `·`로, `:`는 `_`로 바꿔 쓴다)")
    breaking = sorted({c for c in t if c in _LINK_BREAKING_CHARS})
    if breaking:
        errs.append(
            f"제목에 쓸 수 없는 문자: {' '.join(breaking)} — 파일명으로는 되지만 "
            f"본문 Link 파서가 `[[제목#헤딩]]`·`[[제목|별칭]]` 문법 때문에 여기서 "
            f"대상명을 자른다. 이 제목으로 만든 노드는 아무도 링크로 가리킬 수 "
            f"없다")
    if any(ord(c) < 32 or c in "\x85\u2028\u2029" for c in t):
        errs.append("제목에 제어문자·줄 구분 문자를 쓸 수 없다")
    if t.startswith("."):
        errs.append(f"제목은 `.`으로 시작할 수 없다: {title!r}")
    if t.endswith((".", " ")):
        errs.append("제목은 `.`이나 공백으로 끝낼 수 없다 — Windows가 잘라낸다")
    # Windows의 DOS 장치명 판정은 장치명 뒤의 **공백을 무시**하고 그다음 `.`을 본다
    # — `COM1 .foo`도 장치명으로 해석된다(실측: 그 이름은 git add가 실패한다).
    if t.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED:
        errs.append(f"Windows 예약 장치명은 파일명이 될 수 없다: {t}")
    n = len(f"{t}.md".encode("utf-8"))
    if n > MAX_FILENAME_BYTES:
        errs.append(
            f"제목이 너무 길다 — `.md`를 포함한 UTF-8 파일명이 {n}바이트로 "
            f"{MAX_FILENAME_BYTES}바이트를 넘는다. ext4는 파일명을 **바이트**로 "
            f"제한하므로 Linux 기기에서 만들 수 없다(한글은 글자당 3바이트다)")
    return errs


def portable_name_key(name: str) -> str:
    """**모든** 기기에서 같은 경로가 되는지 판정하는 파일명 동일성 키.

    NTFS·APFS는 대소문자를 구별하지 않고, macOS는 한글을 NFD로 저장하기도 한다.
    그래서 `path.exists()`처럼 **현재 OS에게** 물으면 Linux에서 `Example.md`와
    `example.md`가 둘 다 만들어지고, 그 트리를 Windows·macOS에서 체크아웃할 때
    두 경로가 충돌한다 — 한쪽만 워킹트리에 남는다.

    `contract.target_stem`은 링크 대상 해소용 키라서 여기 쓰지 않는다. 그쪽을
    접으면 Link가 대소문자를 무시하게 되는데, 그것은 다른 계약이다."""
    return unicodedata.normalize("NFC", name).casefold()


def name_collision(dest_dir: Path, stem: str, suffix: str | None = ".md", *, unique: bool = False) -> str | None:
    """같은 군집에 이식성 기준으로 충돌하는 파일이 있으면 그 이름을 돌려준다.
    같은 이름 자신도 걸리므로 기존 존재 검사를 겸한다."""
    key = portable_name_key(stem)
    hit = None
    for p in dest_dir.glob("*"):
        matches = p.is_dir() if suffix is None else p.suffix.lower() == suffix
        if matches and portable_name_key(p.name if suffix is None else p.stem) == key:
            if not unique:
                return p.name
            if hit is not None:
                raise WriteError(f"이식성 이름 충돌 — 정본을 고를 수 없다: {dest_dir / stem}")
            hit = p.name
    return hit
