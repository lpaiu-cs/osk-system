"""osk.contract — 노드 계약의 파싱과 검증.

구현 근거: 시행령 §1(필수 6필드·summary 80자 무링크·PE 표기),
Mechanism §2(id·시각 형식·drafter 단수·모델명·필드 순서).
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
import yaml

from .core import ID_RE, TS_RE

def edge_targets(value) -> list[str]:
    """PE 스칼라·목록을 **대상명 목록**으로 읽는다(시행령 §1 3항 · Mechanism
    §8 2항). 위키링크는 대상명(경로·stem)만, id 맨값은 스칼라 전체를 돌려준다 —
    소비자(`graph.resolve`)가 id는 id로, 그 밖은 경로/stem으로 해석한다.

    `Node.edges`와 쓰기 통로의 엣지 델타가 **같은 해석**을 쓰도록 여기 한 벌만
    둔다. 두 벌이면 조용히 갈라진다."""
    if value is None:
        return []
    out = []
    for x in (value if isinstance(value, list) else [value]):
        s = str(x).strip()
        m = re.search(r"\[\[([^\]#|]+)", s)
        if m:
            out.append(m.group(1).strip())   # 비노드/사건/노드 — 위키링크 대상명
        elif s:
            # Plain raw coordinates retain anchors in storage, not target lookup.
            out.append(s.split("#", 1)[0] if "/_raw/" in s else s)
    return out


def target_stem(name: str) -> str:
    """PE·Link 대상명의 **동일성 키**. 경로형 `[[00_Scope/W1/N]]`과 스템형
    `[[N]]`은 같은 대상이므로 마지막 요소의 stem으로 접는다 — 표기 차이가
    중복 등재나 무효한 제거로 새지 않게 한다.

    위키링크 괄호를 **여기서 벗긴다.** 구판은 벗기지 않아 같은 노드가
    `[[N]]`·`N`·`N]]` 세 키로 갈렸다 — 독스트링이 약속한 접기가 실제로는
    일어나지 않았다. 호출부가 `Node.edges`(이미 벗겨진 값)를 먹이는 자리에서는
    드러나지 않았지만, 호출자 입력은 벗겨지지 않은 채 들어온다. 그래서 맨
    이름으로 넣으면 중복 판정이 되고 `[[이름]]`으로 넣으면 안 되는 상태였다."""
    s = str(name).strip()
    m = re.search(r"\[\[([^\]#|]+)", s)
    if m:
        s = m.group(1).strip()
    s = s.rstrip("/").split("/")[-1]
    return s[:-3] if s.endswith(".md") else s


_LIST_ITEM_RE = re.compile(r" {0,3}(?:[-+*]|([0-9]{1,9})[.)])( +|$)")
_QUOTE_RE = re.compile(r" {0,3}> ?")
_FENCE_MARK_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_HEADING_RE = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")
_BREAK_RE = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")  # `* * *`·`---` — 목록 항목보다 먼저
_SETEXT_RE = re.compile(r" {0,3}(?:=+|-+)[ \t]*$")  # 문단 밑 `-`는 빈 항목이 아니라 제목 밑줄
# 문단을 끊는 HTML 블록 시작(1~6형). 7형(태그 하나뿐인 행)은 문단을 끊지 못한다.
_HTML_RE = re.compile(
    r" {0,3}(?:<(?:script|pre|style|textarea)(?:[ \t>]|$)|<!--|<\?|<![A-Za-z]|<!\[CDATA\["
    r"|</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup"
    r"|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset"
    r"|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol"
    r"|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr"
    r"|track|ul)(?:[ \t>]|/>|$))", re.I)
_HTML7_RE = re.compile(r" {0,3}</?[A-Za-z][A-Za-z0-9-]*(?:[ \t][^<>]*)?/?>[ \t]*$")
_TICKS_RE = re.compile(r"`+")
# 문단을 끊는 행 — 컨테이너를 못 채운 행이 이것이 아니면 게으른 연속행이다
_BLOCK_STARTS = (_BREAK_RE, _LIST_ITEM_RE, _HEADING_RE, _HTML_RE,
                 re.compile(r" {0,3}(?:>|`{3,}[^`]*$|~{3,})"))
_INDENTED_RE = re.compile(r"(?m)^(?: {4}| {0,3}\t)")


def _fence_mark(content: str):
    mark = _FENCE_MARK_RE.match(content)
    return mark if mark and (mark[1][0] != "`" or "`" not in mark[2]) else None


def md_lines(text: str):
    """본문을 행 단위로 훑어 `(offset, line, content, code)`를 낸다. content는
    컨테이너(목록 항목·인용 `>`)를 벗긴(탭은 4칸) 판독용 행, code는 그 행이 코드
    블록(펜스 행 포함·들여쓰기 코드)에 속하는지다.

    태그 방어·Link 추출·목차가 **이 판정 한 벌**을 쓴다. 셋이 갈리면 목차가
    코드로 보는 예시를 그래프는 Link로 세고 쓰기는 그 코드를 고친다(v3.22.1
    실측: `~~~`·들여쓰기·목록 속 펜스에서 `#123abc`가 `#123 abc`로 바뀌었다)."""
    # ponytail: HTML 블록 내부(빈 행까지의 원문)는 가리지 않는다 — 거기서 코드가
    # 갈리면 CommonMark 파서로 바꾼다.
    fence, fence_depth, stack, offset = "", 0, [], 0   # stack: 항목 내용 폭(int)·인용(">")
    para = empty = False
    for line in text.split("\n"):
        content = line.expandtabs(4)
        pos = matched = 0
        for c in stack:
            if c == ">":
                if not (m := _QUOTE_RE.match(content, pos)):
                    break
                pos = m.end()
            elif content[pos:].strip():
                if not content.startswith(" " * c, pos):
                    break
                pos += c
            matched += 1
        rest = content[pos:]
        tip = opened = code = False
        if fence and matched < fence_depth:
            fence = ""  # 닫히지 않은 펜스는 그것을 담은 컨테이너와 함께 끝난다
        if fence:
            code = True
            mark = _FENCE_MARK_RE.match(rest)
            if mark and mark[1][0] == fence[0] and len(mark[1]) >= len(fence) and not mark[2].strip():
                fence = ""
        elif not rest.strip():
            if matched < len(stack):
                del stack[matched:]   # 빈 행은 인용을 닫는다
            elif empty:
                stack.pop()   # 빈 항목은 빈 행을 만나면 닫힌다(항목은 빈 행 둘로 시작하지 못한다)
            para = empty = False
        elif para and matched < len(stack) and not any(r.match(rest) for r in _BLOCK_STARTS):
            pass   # 컨테이너를 못 채워도 문단의 게으른 연속행이면 닫지 않는다
        else:
            tip = para and matched == len(stack)   # 열린 문단을 이을 수 있는 행
            del stack[matched:]
            empty = False
            while not rest.startswith("    "):
                if m := _QUOTE_RE.match(rest):
                    stack.append(">")
                    rest, opened, empty = rest[m.end():], True, False
                    continue
                item = not _BREAK_RE.match(rest) and _LIST_ITEM_RE.match(rest)
                if not item:
                    break
                blank_start = not rest[item.end():].strip()
                # 문단을 끊는 첫 항목은 빈 항목·1 아닌 번호일 수 없다 — 문단의 글이다
                if tip and not opened and (blank_start or item[1] and int(item[1]) != 1):
                    break
                padding = len(item[2])   # 빈 채로 시작하는 항목의 내용 열은 표지 뒤 1칸이다
                width = item.start(2) + (padding if 1 <= padding <= 4 and not blank_start else 1)
                stack.append(width)
                rest, opened, empty = rest[width:], True, blank_start
            tip = tip and not opened
            if not rest.strip():
                para = False
            elif rest.startswith("    "):
                code, para = not tip, tip   # 들여쓰기 코드 — 문단을 끊지는 못한다
            elif mark := _fence_mark(rest):
                fence, fence_depth, code, para = mark[1], len(stack), True, False
            else:
                # 문단 글만 게으르게 이어진다 — 제목·구분선·HTML 블록 뒤 행은 잇지 못한다
                para = not ((tip and _SETEXT_RE.match(rest))
                            or any(r.match(rest) for r in (_HEADING_RE, _BREAK_RE, _HTML_RE))
                            or (not tip and _HTML7_RE.match(rest)))
        yield offset, line, rest, code
        offset += len(line) + 1


def split_code(text: str) -> list[str]:
    """`re.split`처럼 `[글, 코드, 글, …]`로 가른다 — 홀수 번째가 코드(코드
    블록 행, 같은 길이의 백틱 열로 닫히는 인라인 코드). 이어 붙이면 원문이다."""
    if "`" not in text and "~~~" not in text and not _INDENTED_RE.search(text):
        return [text]   # 코드가 설 자리가 없다 — 대부분의 본문은 행 순회를 건너뛴다
    cuts = []
    for offset, line, _content, code in md_lines(text):
        if code:
            cuts.append([offset, min(offset + len(line) + 1, len(text))])
            continue
        if "`" not in line:
            continue
        # ponytail: 인라인 코드는 한 행 안에서만 닫는다(문단을 넘는 스팬·백슬래시
        # 이스케이프는 글로 본다).
        ticks = [m.span() for m in _TICKS_RE.finditer(line)]
        i = 0
        while i < len(ticks):
            a, b = ticks[i]
            j = next((k for k in range(i + 1, len(ticks))
                      if ticks[k][1] - ticks[k][0] == b - a), None)
            if j is None:
                i += 1
                continue
            cuts.append([offset + a, offset + ticks[j][1]])
            i = j + 1
    out, pos = [], 0
    for a, b in cuts:
        if out and a == pos:
            out[-1] += text[a:b]     # 이어지는 코드 행은 한 구획으로
        else:
            out += [text[pos:a], text[a:b]]
        pos = b
    return out + [text[pos:]]


REQUIRED = ["id", "created", "updated", "author", "drafter", "summary"]
PREDICATES = ["derived-from", "conflicts"]  # 헌법 8조 5항 (2술어 체제)
ORDER = REQUIRED  # Mechanism §2 5항 — PE는 그 뒤 상호 순서 무관


@dataclass
class Node:
    path: Path
    meta: dict
    body: str
    fm_keys: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.meta.get("id", ""))

    def edges(self, predicate: str) -> list[str]:
        """PE 대상 목록. 두 표기를 함께 읽는다(시행령 §1 3항 · Mechanism §8 2항):
        노드 대상은 `id`를 **그대로**(`derived-from: 260802-114u-7lo3`),
        비노드 대상과 `conflicts`는 위키링크(`"[[경로#제목]]"`)로 쓴다.
        위키링크는 대상명(경로·stem)만, id형은 스칼라 전체를 돌려준다 —
        소비자(graph.resolve)가 id는 id로, 그 밖은 경로/stem으로 해석한다.

        해석은 `edge_targets`에 한 벌만 둔다 — 쓰기 통로의 엣지 델타가 저장
        표기에서 같은 목록을 얻어야 하기 때문이다."""
        return edge_targets(self.meta.get(predicate))

    def wikirefs(self) -> list[str]:
        """본문 Link(임베드 포함). 코드 구획(`split_code` — 펜스·들여쓰기
        코드·인라인 코드) 안의 [[...]] 예시는 Link가 아니다 — 제외한다.
        목차·태그 방어와 같은 판정이어야 코드 예시가 scope 경계 거부를 부르지
        않는다. 닫히지 않은 펜스는 본문 끝(또는 담은 목록 항목 끝)까지 코드다."""
        body = "".join(split_code(self.body)[::2])
        return [m.group(1).strip()
                for m in re.finditer(r"!?\[\[([^\]|]+)", body)]

    def wikilinks(self) -> list[str]:
        return [ref.split("#", 1)[0].strip() for ref in self.wikirefs()]

    def references(self) -> list[tuple[str, str]]:
        """Stored relation and complete reference; never erase a raw round anchor."""
        refs = [("Link", ref) for ref in self.wikirefs()]
        for relation in PREDICATES:
            value = self.meta.get(relation)
            for ref in value if isinstance(value, list) else [value]:
                if ref:
                    text = str(ref).strip()
                    if text.startswith("[[") and text.endswith("]]"):
                        text = text[2:-2]
                    refs.append((relation, text.split("|", 1)[0].strip()))
        return sorted(set(refs))


# libyaml(C) 로더가 있으면 그것을 바닥으로 쓴다 — 실 vault 1,811건 실측으로
# **값 차이 0·오류 차이 0**이고, 되풀이 측정의 최소값 기준 **9.2배**다(부하가
# 걸린 기기에서 0.68초 → 0.074초; 무부하에서는 더 짧다). 심의 문서
# `design-review-index-cache.md`는 같은 비교를 6배(0.302→0.050)로 적었으므로,
# 이 수를 인용할 때는 어느 조건의 값인지 함께 적어야 한다. 중복 키 생성자도
# C 로더에서 그대로 작동한다(실측).
#
# 폴백은 선택이 아니라 조건이다. `requirements.txt`는 `PyYAML`을 버전도 libyaml
# 동봉 여부도 묶지 않고 적으므로, 이 환경에 C 로더가 있다는 것이 다음 인터프리터
# 에도 있다는 뜻이 되지 못한다. "깔려 있다"를 전제로 쓰면 거기서 import가 죽는다
# — 이식성은 이 체계의 선언된 선호다.
#
# (정정 2026-08-30: 여기에는 한때 "살아 있는 `mcp_server.py` 24개 중 12개가
# `.venv`가 아닌 시스템 파이썬으로 돈다"가 근거로 적혀 있었다. 그것은 **프로세스
# 오독**이었다 — venv의 `python.exe`는 베이스 인터프리터를 자식으로 띄우는 런처
# 스텁이라 서버 하나가 두 프로세스로 보인다. 정확히 절반이었던 것이 짝을
# 이룬다는 신호였다. 폴백의 필요는 그대로지만 근거는 위의 것이다.)
_LoaderBase = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _StrictLoader(_LoaderBase):
    """중복 키를 last-wins로 삼키지 않는 로더 — `conflicts`를 두 번 쓰면
    앞 엣지가 조용히 사라진다. frontmatter의 중복 키는 오류다."""


class _PureStrictLoader(yaml.SafeLoader):
    """같은 규율의 **순수 파이썬** 로더. C 로더와 판정이 갈리는 입력을 구판과
    같게 다루기 위해서만 쓴다(`parse`의 탭 처리)."""


def _no_dup_keys(loader, node, deep=False):
    out = {}
    for k, v in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in out:
            raise yaml.constructor.ConstructorError(
                None, None, f"frontmatter 중복 키: {key}", k.start_mark)
        out[key] = loader.construct_object(v, deep=deep)
    return out


for _L in (_StrictLoader, _PureStrictLoader):
    _L.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_keys)


# frontmatter 흐름 컬렉션의 중첩 상한. C 로더는 깊은 중첩에서 **네이티브 스택
# 오버플로**로 프로세스를 죽인다 — 실측: 깊이 ~2,700에서 종료코드 127, 파이썬
# 트레이스백이 한 글자도 없다. 순수 로더의 `RecursionError`는 파이썬 예외라
# `except`가 잡아 그 파일을 broken으로 보냈지만, 네이티브 크래시는 표면의
# `_guard`도 검증기의 `guard`도 잡지 못한다 — 시행령 §11("실패는 보류·보고")이
# 파서 교체로 깨지는 자리다. 그리고 그런 파일은 vault 안에 있으므로 git으로
# 전 기기에 퍼져 모든 서버가 함께 죽고, 재기동해도 다시 죽는다.
#
# 그래서 로더에 넘기기 **전에** 여기서 센다. 값의 근거: 실 vault 1,811건의
# 최대 깊이가 3이고(노드 frontmatter는 평평하다 — 필수 6필드 + 술어 목록),
# 구판 순수 로더의 실측 경계는 493이었다. 32는 실사용의 10배이고 크래시
# 임계의 1/80이다. 33~493 구간은 구판이 받아들이던 자리이므로 이것은
# **조이는 변경**이며, 실 vault에 그 구간의 파일은 0건이다.
_MAX_FM_DEPTH = 32


def _flow_depth(text: str) -> int:
    """흐름 컬렉션(`[`·`{`)의 최대 중첩. 따옴표 안까지 세므로 실제보다 크게
    나올 수 있는데, 그 방향이 안전하다 — 문자열에 괄호를 32단 쌓은
    frontmatter는 어느 쪽으로 읽어도 노드 계약이 아니다."""
    d = mx = 0
    for c in text:
        if c in "[{":
            d += 1
            if d > mx:
                mx = d
        elif c in "]}":
            d -= 1
    return mx


def parse(path: Path | str) -> Node:
    p = Path(path)
    return parse_bytes(p, p.read_bytes())


def parse_bytes(path: Path | str, data: bytes) -> Node:
    """**주어진 바이트**에서 판독한다.

    경로가 아니라 바이트를 받는 갈래가 따로 있어야 하는 이유: 호출자가 판독한
    것과 해시한 것이 **같은 바이트임을 보장**할 수 있어야 한다. 표면의
    `read_node`가 캐시된 본문에 디스크에서 새로 잰 해시를 붙여 내보내는 바람에,
    호출자는 자기가 **읽지 않은 상태**의 해시를 받았고 그 해시가 CAS를 통과해
    외부 편집을 지웠다(실측 재현). 해시는 읽은 상태의 증거여야 한다
    (Mechanism §6-2 4항).

    줄바꿈은 `read_text`의 universal newlines와 같게 접는다 — 이 갈래로 읽은
    본문이 `parse(path)`로 읽은 것과 달라지면 CRLF 파일에서 두 경로의 판정이
    갈린다."""
    p = Path(path)
    t = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not t.startswith("---\n"):
        raise ValueError(f"frontmatter 없음: {p}")
    end = t.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"frontmatter 미종결: {p}")
    fm_text = t[4:end]
    depth = _flow_depth(fm_text)
    if depth > _MAX_FM_DEPTH:
        raise ValueError(
            f"frontmatter 중첩이 너무 깊다: {p} — 깊이 {depth}(상한 "
            f"{_MAX_FM_DEPTH}). 노드 frontmatter는 평평하다")
    # 파서를 바꾸면서 **거부되던 것이 통과하게** 두면 최적화가 아니라 계약의
    # 완화다. C 로더는 평문 스칼라 안의 탭(`summary: a<TAB>b`, `a:<TAB>b`)을
    # 통과시키는데 순수 로더는 ScannerError로 거부한다(실측). 그 입력만 순수
    # 로더에게 맡겨 구판과 **같은 판정·같은 메시지**를 받는다.
    #
    # 탭 전부를 거부하는 것은 과잉이었다 — 따옴표·블록(`|`)·접힘(`>`) 스칼라와
    # 주석 안의 탭은 두 로더가 **똑같이 통과**시키므로, 그것까지 막으면 구판이
    # 받아들이던 노드가 새로 broken이 된다. 실 vault 1,811건 중 탭이 든
    # frontmatter는 0건이라 이 갈래를 타는 비용도 사실상 0이다.
    loader = _PureStrictLoader if "\t" in fm_text else _StrictLoader
    try:
        meta = yaml.load(fm_text, Loader=loader) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter 파싱 실패: {p} — {e}") from e
    if not isinstance(meta, dict):
        raise ValueError(f"frontmatter가 매핑이 아님: {p}")
    keys = [ln.split(":")[0] for ln in fm_text.split("\n")
            if ln and not ln.startswith((" ", "-", "#"))]
    return Node(path=p, meta=meta, body=t[end + 5:], fm_keys=keys)


def validate(node: Node) -> list[str]:
    """계약 위반 목록. 빈 목록 = 통과."""
    errs = []
    m = node.meta
    for k in REQUIRED:
        if k not in m:
            errs.append(f"필수 필드 누락: {k}")
    if errs:
        return errs
    if node.fm_keys[:6] != ORDER:
        errs.append(f"필드 순서 위반: {node.fm_keys[:6]}")
    for k in node.fm_keys[6:]:
        if k not in PREDICATES:
            errs.append(f"계약 외 필드: {k}")
    if not re.match(ID_RE, str(m["id"])):
        errs.append(f"id 형식 위반: {m['id']}")
    for k in ("created", "updated"):
        if not re.match(TS_RE, str(m[k])):
            errs.append(f"{k} 시각 형식 위반: {m[k]}")
    ca = str(m["created"])
    if str(m["id"])[:6] != ca[2:4] + ca[5:7] + ca[8:10]:
        errs.append(f"id·created 시간 정합 위반: {m['id']} vs {ca[:10]}")
    s = m.get("summary")
    if s is None or not str(s).strip():
        errs.append("summary 부재·공백 (내용을 한 줄로 축약해야 한다)")
    else:
        s = str(s)
        if len(s) > 80:
            errs.append(f"summary {len(s)}자 (한도 80)")
        if "[[" in s:
            errs.append("summary에 Link·Predicate Edge 금지")
        if "\n" in s:
            errs.append("summary는 물리적 한 줄이어야 한다")
    if isinstance(m.get("drafter"), list):
        errs.append("drafter는 대표 기초자 하나(단수)")
    dr = str(m.get("drafter", ""))
    if ":" in dr:
        errs.append(f"drafter는 접두 없는 모델명: {dr} (Mechanism §2 4항)")
    elif dr in ("claude", "codex", "claude-code", "gemini-cli"):
        errs.append(f"drafter는 하네스명이 아니라 모델명: {dr}")
    stem = node.path.stem
    sid = str(m["id"])
    for pred in PREDICATES:
        for t in node.edges(pred):
            ts = t.strip().rstrip("/").split("/")[-1]
            ts = ts[:-3] if ts.endswith(".md") else ts
            if ts == stem or t == sid:   # id형·경로형 어느 표기든 자기 참조 적발
                errs.append(f"{pred}가 자기 자신을 가리킨다")  # 근거·충돌은 남과의 관계다
    if node.fm_keys and len(node.fm_keys) != len(set(node.fm_keys)):
        errs.append(f"frontmatter 중복 필드: {node.fm_keys}")
    return errs
