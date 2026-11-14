"""osk.prose — 본문을 저장될 형태로 접는다.

구현 근거: Mechanism §8 7항(옵시디언 태그 방어). 노드 본문과 scope 기억이 같은 변환을 쓰고,
앵커 대조도 같은 변환을 따른다 — 쓰는 자리와 읽는 자리가 같은 형태를 봐야 한다.

본문을 보존 확인의 단위(문단·목록 항목·제목)로 가르는 것도 여기 있다(`segment_texts`). 쓰기의
되먹임과 증류의 영수증이 같은 단위를 쓴다.
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
    """`nodefile.render`가 쓸 형태로 접는다 — 변경 여부는 **쓰일 형태**로 판정해야
    앞뒤 공백 차이가 헛 변경으로 잡히지 않고, 태그 방어(위)도 쓰일 형태에
    이미 반영되어 같은 입력의 재전송이 헛 변경을 만들지 않는다."""
    return space_numeric_tags(norm_newlines(body).lstrip("\n").rstrip())


_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d+[.)])\s")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
_HEADING_MARKS = re.compile(r"^ {0,3}#{1,6}(?:\s+|$)|\s+#+\s*$")   # 여는 표식과 닫는 `#`열


def segment_texts(body: str, headings: bool = True) -> list[str]:
    """보존 확인의 단위 — 빈 줄로 나뉜 문단이고, 목록은 항목 하나씩, 제목은 줄 하나씩이다.
    항목을 하나씩 재는 것은 목록에 한 줄 덧붙인 증류가 같은 목록의 다른 항목이 고쳐질 때마다
    깨지지 않게 하려는 것이다. 제목도 단위다 — 증류한 결론이 제목에만 있으면 문단만 재는
    영수증은 그 제목이 지워져도 보존으로 남는다(#138 리뷰 P2). 제목은 표식(`#`)을 떼고 재어
    단계만 바꾼 것은 잃은 것으로 보지 않는다."""
    out, cur = [], []
    for block in re.split(r"\n[ \t]*\n", body):
        for line in block.splitlines():
            if _HEADING.match(line) or (_ITEM.match(line) and cur):
                out.append(cur)
                cur = []
            if _HEADING.match(line):
                if headings:
                    out.append([_HEADING_MARKS.sub("", line)])
            else:
                cur.append(line)
        out.append(cur)
        cur = []
    texts = ("\n".join(s).strip() for s in out)
    return [t for t in texts if t]
