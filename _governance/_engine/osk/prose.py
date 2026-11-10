"""osk.prose — 본문을 저장될 형태로 접는다.

구현 근거: Mechanism §8 7항(옵시디언 태그 방어). 노드 본문과 scope 기억이 같은 변환을 쓰고,
앵커 대조도 같은 변환을 따른다 — 쓰는 자리와 읽는 자리가 같은 형태를 봐야 한다.
"""
from __future__ import annotations

import re

from . import contract

# 옵시디언 태그 방어 (Mechanism §8 7항). `#1227` 같은 순수 숫자 참조는
# 옵시디언 태그가 아니지만(태그는 비숫자 문자 1개 이상 필요), 조사·가운뎃점이
# **붙는 순간**(`#1227은`·`#1227·1228`) 그 문자가 태그를 유효하게 만들어 의도
# 없는 태그가 태그판을 오염한다(실측 확인). 거부하는 대신 숫자 뒤에 공백
# 하나를 넣어 참조 의미를 보존한 채 태그화만 끊는다 — 규범이 아니라 옵시디언
# 네이티브 계약을 지키는 방어 기제다. 이어지는 문자의 판정은 옵시디언 태그
# 문자 집합을 따른다: 유니코드 문자·밑줄([^\W\d])과 `-`·`/`·`·`(U+00B7).
# 마침표·쉼표·괄호·공백·전각 대시 등은 태그를 끝내므로 건드리지 않는다.
_TAG_SPACE_RE = re.compile(r"(#\d+)(?=[^\W\d]|[-/·])")


def space_numeric_tags(text: str) -> str:
    """본문의 `#<숫자>` 직결 태그화를 공백 삽입으로 끊는다.

    코드 구획(펜스·들여쓰기 코드·인라인)은 옵시디언이 태그로 읽지 않으므로
    건드리지 않는다 — 위험이 없는 코드를 고치면 그건 방어가 아니라 변조다.
    구획 판정은 `wikilinks()`·목차와 같은 `contract.split_code`를 쓴다."""
    return "".join(seg if i % 2 else _TAG_SPACE_RE.sub(r"\1 ", seg)
                   for i, seg in enumerate(contract.split_code(text)))


def norm_newlines(text: str) -> str:
    """`\\r\\n`·`\\r`을 `\\n`으로 — **파서가 읽을 형태**로 접는다.

    `contract.parse_bytes`가 판독 시 이미 접으므로(universal newlines), 접지
    않고 쓰면 쓴 바이트를 되읽을 수 없다: `new_text`에 CRLF를 담아 앵커 편집을
    하면 파일에는 `\\r`이 들어가는데 다음 `read_node`의 본문에는 없어, 같은
    `old_text`로 다시 부르면 "앵커가 본문에 없다"가 된다(실측 재현). 쓰는 자리가
    읽는 자리와 같은 형태를 쓰면 그 창이 닫힌다."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def norm_body(body: str) -> str:
    """`_render`가 쓸 형태로 접는다 — 변경 여부는 **쓰일 형태**로 판정해야
    앞뒤 공백 차이가 헛 변경으로 잡히지 않고, 태그 방어(위)도 쓰일 형태에
    이미 반영되어 같은 입력의 재전송이 헛 변경을 만들지 않는다."""
    return space_numeric_tags(norm_newlines(body).lstrip("\n").rstrip())
