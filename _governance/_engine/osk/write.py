"""osk.write — 노드 쓰기의 **단일 통로**.

구현 근거: Mechanism §6-2 3항(표면은 계약 검증의 강제 지점), 시행령 §1(노드
계약)·§3 4항(pin 대조)·§11(실패는 보류·보고), 헌법 8조(참조 위상)·12조
2항(충돌 후보 기록).

MCP 도구와 CLI가 **같은 이 통로**를 쓴다 — 쓰기 경로가 둘로 갈라지면 한쪽만
계약을 지키게 된다.

이 모듈은 승인 기록부와 pin 기록에 **결코 쓰지 않는다**(Mechanism §6-2 2항).
보호영역의 승인·반려는 사용자 전속(대화형 단말)이며, 표면 쓰기는 언제나
작업본에 반영될 뿐이다(§6-2 8항) — 검증기의 표면 세그먼트가 이를 AST로
강제한다.

동시성 (설계 rev.3 §4):
- v1은 **전역 단일 쓰기 잠금**이다. 노드 단위 잠금을 경로로 키잡으면 move와
  update가 같은 노드에 다른 잠금을 잡아 상호 배제가 깨지고, 이동 뒤 구 경로에
  파일이 부활해 쓰기 통로가 스스로 id 중복을 만든다. 쓰기 1회의 비-토큰 비용이
  40ms 미만이라 경합은 무시 가능하다 — 세분화는 경합이 실측될 때의 최적화다.
- **이름→파일 해석은 잠금 안에서 라이브 파일시스템으로** 한다(캐시 색인으로
  해석하면 낡은 경로에 작용한다).
- 잠금 안 재판독에서 부재·파손이면 거부한다 — 델타가 파손 파일을 "복구"하지
  않는다.

CAS (Mechanism §6-2 4항): `expect_hash`는 **본문 전체 치환**에 결속한다 —
보지 않은 상태를 덮지 않게 한다. 부분 변경(엣지 델타·summary)에는 요구하지
않는다. 거부 응답에 현재 해시를 담지 않는다 — 담으면 관측 증명이 연극이 된다.
"""
from __future__ import annotations
import json, os, re, tempfile, time, unicodedata
from pathlib import Path

import yaml

from .core import (ROOT, CANDIDATES, PINS, ROUTING, ID_RE, CASE_RE,
                   ledger_append, ledger_damage, ledger_read, mutation_lock,
                   new_node_id, now_kst, posix_rel, resolve_in_root,
                   resolve_one, sha256_bytes, sha256_file)
from . import approvals, contract, graph, signatures

GOVERNANCE = ("governance",)             # 표면 쓰기 제외 (설계 D8)
CANDIDATE_TYPES = ("contradiction", "duplication", "competition",
                   "delegation-overlap")   # Mechanism §4 3항 (lineage-fork 폐지)


class WriteError(ValueError):
    """계약 위반·거부. `violations`에 위반 목록을 담는다(부분 성공 없음)."""

    def __init__(self, message: str, violations: list[str] | None = None,
                 **extra):
        super().__init__(message)
        self.violations = violations or [message]
        self.extra = extra


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
_MAX_FILENAME_BYTES = 255

# 아래 둘은 파일명으로는 멀쩡하지만 **이 체계 자신의 Link 문법**을 깬다. 본문
# Link 파서가 `\[\[([^\]#|]+)`이라 `#`·`]`·`|`에서 대상명이 잘린다 — 그런 제목의
# 노드는 만들어지긴 해도 **아무도 링크로 가리킬 수 없다**(실측: `[[PR#1 판정]]`
# → 대상 `PR`). `|`는 위 파일명 집합이 이미 막으므로 여기서는 나머지 둘이다.
_LINK_BREAKING_CHARS = "#]"


def _title_errors(title: str) -> list[str]:
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
    if any(ord(c) < 32 for c in t):
        errs.append("제목에 제어문자를 쓸 수 없다")
    if t.startswith("."):
        errs.append(f"제목은 `.`으로 시작할 수 없다: {title!r}")
    if t.endswith((".", " ")):
        errs.append("제목은 `.`이나 공백으로 끝낼 수 없다 — Windows가 잘라낸다")
    # Windows의 DOS 장치명 판정은 장치명 뒤의 **공백을 무시**하고 그다음 `.`을 본다
    # — `COM1 .foo`도 장치명으로 해석된다(실측: 그 이름은 git add가 실패한다).
    if t.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED:
        errs.append(f"Windows 예약 장치명은 파일명이 될 수 없다: {t}")
    n = len(f"{t}.md".encode("utf-8"))
    if n > _MAX_FILENAME_BYTES:
        errs.append(
            f"제목이 너무 길다 — `.md`를 포함한 UTF-8 파일명이 {n}바이트로 "
            f"{_MAX_FILENAME_BYTES}바이트를 넘는다. ext4는 파일명을 **바이트**로 "
            f"제한하므로 Linux 기기에서 만들 수 없다(한글은 글자당 3바이트다)")
    return errs


def _portable_name_key(name: str) -> str:
    """**모든** 기기에서 같은 경로가 되는지 판정하는 파일명 동일성 키.

    NTFS·APFS는 대소문자를 구별하지 않고, macOS는 한글을 NFD로 저장하기도 한다.
    그래서 `path.exists()`처럼 **현재 OS에게** 물으면 Linux에서 `Example.md`와
    `example.md`가 둘 다 만들어지고, 그 트리를 Windows·macOS에서 체크아웃할 때
    두 경로가 충돌한다 — 한쪽만 워킹트리에 남는다.

    `contract.target_stem`은 링크 대상 해소용 키라서 여기 쓰지 않는다. 그쪽을
    접으면 Link가 대소문자를 무시하게 되는데, 그것은 다른 계약이다."""
    return unicodedata.normalize("NFC", name).casefold()


def _name_collision(dest_dir: Path, stem: str) -> str | None:
    """같은 군집에 이식성 기준으로 충돌하는 파일이 있으면 그 이름을 돌려준다.
    같은 이름 자신도 걸리므로 기존 존재 검사를 겸한다."""
    key = _portable_name_key(stem)
    for p in dest_dir.glob("*.md"):
        if _portable_name_key(p.stem) == key:
            return p.name
    return None


# 전역 변경 잠금은 core가 소유한다 — 보호영역 조작(approvals)이 같은 잠금을
# 써야 반려의 마지막 전제 확인과 첫 파괴 사이에 정상 쓰기가 끼어들지 않는다.
_Lock = mutation_lock


# ── 파일 해석·직렬화 ─────────────────────────────────────────────────────

def _live_locate(name: str, idx) -> Path | None:
    """이름 → 파일. **잠금 안에서 지은 색인**으로 해석한다.

    같은 이름이 둘 이상이면 **거부한다** — 읽기(색인)와 쓰기가 서로 다른 쪽을
    고르면 에이전트가 본 파일과 고쳐지는 파일이 달라진다. 표면 자신은 중복을
    만들 수 없으므로(create_node가 동명을 거부한다) 중복은 언제나 외부 기원이며,
    그렇기에 표면이 임의로 한쪽을 택하는 것이 더 나쁘다.

    **같은 id가 둘 이상일 때도 같다.** 이 방어가 이름에만 있고 id에는 없었다 —
    그래서 `read_node(id)`는 앞의 것을, 여기서는 뒤의 것을 골랐고, 사본은
    바이트가 같아 `expect_hash`까지 통과했다. 읽지 않은 파일이 갱신되는 경로였다.

    구판은 여기서 파일시스템을 **다시 훑었다.** 그 규율("이름→파일 해석은
    잠금 안에서 라이브 파일시스템으로")의 근거는 *"mcp_server의 fingerprint
    캐시 색인으로 해석하면 낡은 경로에 작용한다"*였는데, 쓰기 통로가 쓰는 것은
    그 캐시가 아니라 같은 잠금 안에서 방금 지은 색인이다. 규율의 뜻은 지키고
    중복만 없앤다 — 실측: 25,000 노드에서 순회 289 ms, 그리고 id 핸들로 부르면
    `_id_of`가 전 노드를 **캐시 없이** 다시 파싱해 5k에서도 이름 경로의
    5.8배였다(804 ms 대 139 ms). 색인의 `by_id`는 그 판독을 접어 두므로 뒤이은
    검증이 다시 파싱하지 않는다."""
    hits = idx.candidates(name)
    if not hits and re.match(ID_RE, str(name).strip()):
        # id 형태면 id로도 찾는다 — 쓰기 응답이 id를 돌려주므로 그것을 핸들로
        # 잡은 호출자에게 "노드 없음"은 틀린 진단이다(10차 ②)
        nid = str(name).strip()
        if nid in idx.dup_ids:
            # 이름 중복과 **같은 이유로** 거부한다. 여기서 한쪽을 고르면 읽기
            # 표면이 고른 쪽과 갈리는데, 사본은 바이트가 같아 CAS가 그것을
            # 막지 못한다 — 에이전트가 읽지 않은 파일이 갱신된다(재현 확인).
            raise WriteError(
                f"같은 id의 노드가 {len(idx.dup_ids[nid])}개다 — 어느 것인지 "
                f"정해지지 않아 고치지 않았다: {idx.dup_ids[nid]}")
        hit = idx.by_id.get(nid)
        hits = [hit[0]] if hit else []
    if len(hits) > 1:
        raise WriteError(
            f"같은 이름의 노드가 {len(hits)}개다 — 어느 것인지 정해지지 않아 "
            f"고치지 않았다: {[posix_rel(h, ROOT) for h in hits]}")
    return hits[0] if hits else None


# 옵시디언 태그 방어 (Mechanism §8 7항). `#1227` 같은 순수 숫자 참조는
# 옵시디언 태그가 아니지만(태그는 비숫자 문자 1개 이상 필요), 조사·가운뎃점이
# **붙는 순간**(`#1227은`·`#1227·1228`) 그 문자가 태그를 유효하게 만들어 의도
# 없는 태그가 태그판을 오염한다(실측 확인). 거부하는 대신 숫자 뒤에 공백
# 하나를 넣어 참조 의미를 보존한 채 태그화만 끊는다 — 규범이 아니라 옵시디언
# 네이티브 계약을 지키는 방어 기제다. 이어지는 문자의 판정은 옵시디언 태그
# 문자 집합을 따른다: 유니코드 문자·밑줄([^\W\d])과 `-`·`/`·`·`(U+00B7).
# 마침표·쉼표·괄호·공백·전각 대시 등은 태그를 끝내므로 건드리지 않는다.
_TAG_SPACE_RE = re.compile(r"(#\d+)(?=[^\W\d]|[-/·])")
_CODE_SPAN_RE = re.compile(r"(`[^`\n]*`)")
_FENCE_RE = re.compile(r"^ {0,3}```")


def _space_numeric_tags(text: str) -> str:
    """본문의 `#<숫자>` 직결 태그화를 공백 삽입으로 끊는다.

    코드 구획(펜스·인라인)은 옵시디언이 태그로 읽지 않으므로 건드리지
    않는다 — 위험이 없는 코드를 고치면 그건 방어가 아니라 변조다. 펜스
    경계 판정은 `wikilinks()`와 같은 규칙(행 머리 ```)을 쓴다."""
    out, in_fence = [], False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = _CODE_SPAN_RE.split(line)
        out.append("".join(
            seg if i % 2 else _TAG_SPACE_RE.sub(r"\1 ", seg)
            for i, seg in enumerate(parts)))
    return "\n".join(out)


def _norm_newlines(text: str) -> str:
    """`\\r\\n`·`\\r`을 `\\n`으로 — **파서가 읽을 형태**로 접는다.

    `contract.parse_bytes`가 판독 시 이미 접으므로(universal newlines), 접지
    않고 쓰면 쓴 바이트를 되읽을 수 없다: `new_text`에 CRLF를 담아 앵커 편집을
    하면 파일에는 `\\r`이 들어가는데 다음 `read_node`의 본문에는 없어, 같은
    `old_text`로 다시 부르면 "앵커가 본문에 없다"가 된다(실측 재현). 쓰는 자리가
    읽는 자리와 같은 형태를 쓰면 그 창이 닫힌다."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _norm_body(body: str) -> str:
    """`_render`가 쓸 형태로 접는다 — 변경 여부는 **쓰일 형태**로 판정해야
    앞뒤 공백 차이가 헛 변경으로 잡히지 않고, 태그 방어(위)도 쓰일 형태에
    이미 반영되어 같은 입력의 재전송이 헛 변경을 만들지 않는다."""
    return _space_numeric_tags(_norm_newlines(body).lstrip("\n").rstrip())


def _render(meta: dict, body: str) -> bytes:
    """계약 순서대로 frontmatter를 직렬화한다 (Mechanism §2 5항)."""
    lines = ["---"]
    for k in contract.ORDER:
        lines.append(f"{k}: {_scalar(meta[k])}")
    for k in contract.PREDICATES:
        if k in meta and meta[k] not in (None, [], ""):
            lines.append(f"{k}: {_edge_value(k, meta[k])}")
    lines.append("---")
    return ("\n".join(lines) + "\n\n" + _norm_body(body) + "\n").encode()


def _scalar(v):
    """JSON 문자열은 유효한 YAML 스칼라다 — 따옴표·백슬래시를 손으로 감싸면
    표면이 스스로 파싱 불가 노드를 만든다(7차 중대 A)."""
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(str(x), ensure_ascii=False)
                               for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def _edge_value(pred: str, v) -> str:
    """Predicate Edge 값의 표기 (시행령 §1 3항 · Mechanism §8 2항):
    `derived-from`의 노드 대상(id)은 **맨값**으로, 비노드 대상과 `conflicts`의
    위키링크는 따옴표로 감싼 스칼라로 쓴다. id는 계약이 문자열로 해석하며,
    맨값·따옴표 어느 쪽으로 되읽어도 같은 문자열이라 왕복이 안정적이다."""
    def one(x):
        s = str(x).strip()
        if pred == "derived-from" and re.match(ID_RE, s):
            return s                                   # id — 맨값
        return json.dumps(s, ensure_ascii=False)       # 위키링크 — 따옴표
    items = v if isinstance(v, list) else [v]
    if len(items) == 1:
        return one(items[0])
    return "[" + ", ".join(one(x) for x in items) + "]"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── 결속 ─────────────────────────────────────────────────────────────────

def _is_cluster(kind: tuple) -> bool:
    """소속의 둘째 원소가 실제 군집 이름인가. Space 루트 직속 파일은
    `space_of`가 파일명을 군집 이름 자리에 넣으므로(`('scope','x.md')`)
    그것을 군집으로 오인하지 않는다 — 노드는 군집 안에만 둔다."""
    if kind[0] == "workbench-transit":
        return True
    return len(kind) > 1 and bool(kind[1]) and not str(kind[1]).endswith(".md")


def _reject_governance(kind: tuple) -> None:
    if kind[:1] == GOVERNANCE:
        raise WriteError(
            "통치 구획은 표면 쓰기 대상이 아니다 — 통치 문서의 개정은 정본 "
            "저장소에서 하고 갱신으로 도달한다 (헌법 3조 6항·시행령 §10 1항)")


def _pinned(target: str) -> bool:
    """군집이 pin으로 고정돼 있는가 (시행령 §3 4·5항 · Mechanism §6 2항)."""
    try:
        recs = ledger_read(PINS)
    except Exception:
        return True             # 판독 실패는 보수적으로 '고정됨' — fail-closed
    keys = {r.get("target") for r in recs if r.get("target")}
    for k in keys:
        r = resolve_one(recs, k, "target")
        if r and r.get("kind") == "pin" and str(k).rstrip("/") == target.rstrip("/"):
            return True
    return False


def _cluster_names() -> list[str]:
    """노드를 둘 수 있는 군집 경로 — **거부 응답에만** 실어 보낸다(상주 비용 0).
    표면에 열거 도구가 없으므로 실패하는 순간에 주소를 가르치는 것이 이 목록의
    일이다(10차 ②의 완화분 — 해결은 `overview`).

    하위 군집도 **함께 낸다** — 분화(시행령 §3 7항)로 생긴 자리를 못 보면
    에이전트가 거기에 노드를 둘 방법이 없다. 하위인지는 **허브의 존재**로
    가른다: 폴더와 동명인 노드가 있어야 군집이며, 없으면 그냥 폴더다.
    """
    out = set()

    def walk(d: Path, prefix: str) -> None:
        for sub in sorted(d.iterdir()):
            if not sub.is_dir() or sub.name.startswith((".", "_")):
                continue
            k = graph.space_of(sub / "x.md")
            if not (graph.is_node_home(k) and _is_cluster(k)
                    and k[:1] != GOVERNANCE):
                continue
            # 허브가 있어야 군집이다 — 허브 없는 자리는 **싣지 않는다.**
            # 구판은 실었고, 그래서 이 목록이 "노드를 둘 수 있는 군집"이라는
            # 자기 설명과 어긋났다: 신설 관문을 지났으나 뒤이은 검사에 거부돼
            # 남은 빈 디렉토리가 목록에 오르고, 거기 쓰려 하면 "첫 노드는
            # 허브"로 다시 거부된다(표면 감사 실측 — 처음 오는 자를 두 번
            # 헛걸음시킨다). 허브를 세우는 첫 쓰기는 이 목록을 거치지 않고
            # 관문이 안내하므로 잃는 것이 없다.
            path = f"{prefix}/{sub.name}"
            if (sub / f"{sub.name}.md").is_file():
                out.add(path)
                walk(sub, path)

    for space in ("= Scope", "= Domain", "= Person"):
        d = ROOT / space
        if d.is_dir():
            walk(d, space)
    if (ROOT / "= Scope/Workbench/transit").is_dir():
        out.add("= Scope/Workbench/transit")
    return sorted(out)


# ── 군집 신설 관문 (Mechanism §6-2 3항) ──────────────────────────────────

# 확인 표식의 유효 시간. 관문의 목적은 차단이 아니라 "오류 → 사용자 확인 →
# 재시도"의 한 왕복이며, 그 왕복은 분 단위다. 너무 길면 잊힌 표식이 뒷날의
# 다른 요청을 무확인 통과시키고, 너무 짧으면 확인을 받아 오는 사이에 만료된다.
_ACK_TTL = 3600.0


def _ack_file() -> Path:
    """표식은 **기기 로컬**이다(추적 트리 밖 — `git add -A`에 딸려 가지 않고
    다른 기기와 공유되지 않는다). 확인은 지금 이 대화의 일이지 저장소의
    상태가 아니다."""
    from .core import local_lock_path
    return local_lock_path("osk-cluster-ack.json")


def _read_ack() -> dict:
    try:
        d = json.loads(_ack_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}   # 없음·손상 = 표식 없음 — 관문이 한 번 더 물을 뿐이다


def _write_ack(d: dict) -> None:
    try:
        _ack_file().write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass        # 표식을 못 남겨도 쓰기를 막지 않는다 — 1차 거부가 반복될 뿐


def _new_cluster_gate(dest: str, dest_dir: Path | None, doing: str) -> Path:
    """선언되지 않은 군집 — **2단계 확인 관문** (Mechanism §6-2 3항).

    신설을 막지 않는다 — 규범은 군집 형성의 자동화를 기본으로 두므로(헌법
    5조 4항·6조 9항) 여기의 일은 **한 번 묻는 것**이다. 1차 요청은 신설임을
    알리며 거부하고, 같은 군집에 대한 재시도는 통과시킨다. 선의의 에이전트는
    오류 원인이 돌아오면 사용자 허락을 확인한 뒤 같은 요청을 다시 보낸다 —
    그 한 왕복이 관문의 전부이며, 보안 경계가 아니라 확인 지점이다.

    통과하면 디렉토리를 만들어 돌려준다. 이후 검사(동명 등)가 거부해 빈
    디렉토리가 남을 수 있는데, git은 빈 디렉토리를 추적하지 않으므로
    무해하고 다음 시도에서는 선언된 군집으로 보인다."""
    if dest_dir is None:
        raise WriteError(f"군집 경로가 vault를 벗어난다: {dest}")
    # 신설이 설 수 있는 자리는 둘이다 — Space 루트 바로 아래(최상위 군집),
    # 그리고 **이미 선언된 군집 안**(하위 군집). 뒤엣것이 시행령 §3 7항의
    # 분화다: 한 허브가 지는 주제가 갈리면 하위 허브로 분화하고 상위 허브는
    # 하위 허브를 참조한다. 구판은 하위를 막았고, 그래서 규범에 있는 분화를
    # 표면으로 수행할 길이 없었다.
    parent = dest_dir.parent.resolve()
    roots = {(ROOT / "= Scope").resolve(), (ROOT / "= Domain").resolve(),
             (ROOT / "= Person").resolve()}
    top_level = parent in roots
    if not top_level and not (parent / f"{parent.name}.md").is_file():
        raise WriteError(
            f"선언되지 않은 군집이다: {dest}. 신설은 Space 루트 바로 아래이거나 "
            f"**허브가 있는 군집 안**이어야 한다(Mechanism §1 2항 · 시행령 §3 7항). "
            f"`{parent.name}`에 먼저 동명 허브 노드를 만들면 그 안에 분화할 수 "
            f"있다. 지금 쓸 수 있는 군집: {', '.join(_cluster_names()) or '없음'}")
    name_errs = _title_errors(dest_dir.name)
    if name_errs:
        raise WriteError("군집 이름 부적격 — 이름이 곧 디렉토리명이다", name_errs)
    # 하위 군집에는 **2단계 확인을 붙이지 않는다.** 관문의 일은 "이거 신설인데
    # 맞습니까"를 한 번 묻는 것인데, 하위 군집은 그 물음의 답이 이미 나와 있다 —
    # 부모 허브가 존재한다는 것이 그 갈래를 사용자가 안다는 증거이고, 분화는
    # 시행령 §3 7항이 권장하는 정상 행위다. 게다가 "첫 노드는 허브"가 어차피
    # 한 왕복을 만들므로, 관문까지 붙이면 왕복이 둘이 된다.
    if not top_level:
        dest_dir.mkdir(parents=True, exist_ok=True)
        return dest_dir
    key = posix_rel(dest_dir, ROOT)
    now = time.time()
    pending = {k: v for k, v in _read_ack().items()
               if isinstance(v, (int, float)) and now - v < _ACK_TTL}
    if key in pending:
        del pending[key]
        _write_ack(pending)
        dest_dir.mkdir(parents=True, exist_ok=True)
        return dest_dir
    pending[key] = now
    _write_ack(pending)
    raise WriteError(
        "새 군집 신설 — 1차 확인 거부 (쓰지 않았다)",
        [f"`{dest}`는 아직 없는 군집이라 {doing} **새 군집을 만든다**. "
         f"사용자와 합의된 신설인지 확인하라 — 허락을 확인했으면 **같은 "
         f"요청을 그대로 다시** 보내라(1시간 안의 재시도는 통과한다). 기존 "
         f"군집을 쓰려던 것이면 여기서 골라라: "
         f"{', '.join(_cluster_names()) or '없음'}"])


def _open_cases() -> list[str]:
    """열린(docketed) 사건 번호 — conflicts 거부에 실어 보낸다."""
    return sorted(no for no, c in graph._load_cases().items()
                  if str(c.get("status")) == "docketed")


def _check_edges(edges: dict | None, idx) -> list[str]:
    """술어와 **대상 값의 형**을 함께 본다. 스키마(표면)와 통로(여기) 이중으로
    거는 이유는 CLI·Bash 경유가 스키마를 통과하지 않기 때문이다 — 검증은
    통로에, 교육은 스키마에(10차 정정 ①).

    `idx`는 호출부가 잠금 안에서 **한 번 지어** 내려보낸다 (v3.7.0). 기본값을
    두지 않는 것은 규율이다 — 기본값이 있으면 새 호출부가 색인을 또 짓는 것이
    언제나 가능해지고, 증폭은 조용히 돌아온다."""
    errs = []
    for pred, targets in (edges or {}).items():
        if pred not in contract.PREDICATES:
            errs.append(f"계약 외 술어: {pred} "
                        f"(쓸 수 있는 것: {', '.join(contract.PREDICATES)})")
            continue
        for t in (targets if isinstance(targets, list) else [targets]):
            if not isinstance(t, str) or not t.strip():
                errs.append(f"엣지 대상은 비어 있지 않은 문자열이어야 한다: "
                            f"{pred} → {t!r}")
                continue
            if pred == "derived-from" and not re.match(ID_RE, t.strip()):
                # 노드 근거는 **제목 위키링크**로 단다(Mechanism §8 2항).
                #
                # 경로 표기는 쓰지 않는다. 근거로 삼았던 "경로·이름은 상태라
                # 끊어진다"는 이름에 대해서는 사실이 아니다 — `move_node`는
                # 디렉토리만 바꾸고 이름 색인은 경로와 무관하다. 오히려
                # **경로형이 이동에 끊어진다**(resolve의 `/` 갈래는 그 자리에
                # 파일이 없으면 dangling). 게다가 `contract.target_stem`이
                # 경로형과 이름형을 같은 키로 접으므로 resolve 밖에서는
                # 구별되지도 않는다. 제목은 전역에서 유일하니 경로가 필요 없다.
                s = t.strip()
                inner = s[2:-2].strip() if s.startswith("[[") and s.endswith("]]") else s
                if "/" in inner and idx.resolve(inner)[0] == "node":
                    errs.append(
                        f"derived-from의 노드 근거에 경로 표기는 쓰지 않는다: {t} — "
                        f"경로는 이동에 끊어진다. 그 노드의 **제목**을 쓰라 "
                        f"(비노드 근거만 [[경로#앵커]])")
                continue
            if pred == "conflicts" and not re.match(CASE_RE, t.strip()):
                # 존치 상호 치환은 표면으로 만들 수 없다(닭-달걀) — 에이전트가 달 수
                # 있는 conflicts는 열린 사건 참조뿐이다 (설계 rev.3 §5)
                opened = _open_cases()
                errs.append(
                    f"conflicts는 열린 사건 번호(CASE-<연도>-<일련>)만 표면에서 달 수 "
                    f"있다: {t} — 지금 열린 사건: "
                    f"{', '.join(opened) if opened else '없음'} "
                    f"(노드 간 존치 상호 치환은 판결 적용 절차의 일이다)")
    return errs


def _validate_render(path: Path, meta: dict, body: str,
                     idx) -> tuple[bytes, list[str]]:
    """**렌더된 바이트를 되읽어** 검증한다 — 쓰려던 것이 아니라 쓸 것을
    검증해야 한다(7차 중대 A). 직렬화가 깨뜨린 것은 dict 검사로는 안 보인다."""
    data = _render(meta, body)
    fd, tmp = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            back = contract.parse(Path(tmp))
        except Exception as e:
            return data, [f"직렬화 왕복 실패 — 쓰지 않았다: {e}"]
        # 되읽은 노드에 **목적지 경로**를 씌운다 — contract.validate가 stem을 보므로
        # 임시 파일명을 그대로 두면 stem에 걸린 규칙이 통째로 무력화된다(7차 계열)
        back = contract.Node(path=path, meta=back.meta, body=back.body,
                             fm_keys=back.fm_keys)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # 왕복 비교는 **논리값**으로 한다 — Predicate Edge의 단일 원소 리스트
    # `[x]`와 스칼라 `x`는 대상이 하나로 같다. _edge_value가 단일 목록을
    # 맨값으로 접으므로, 표현 차이(리스트↔스칼라)를 불일치로 오판하면 손으로
    # 쓴 `derived-from: [<id>]` 노드가 무관한 갱신마다 거부된다.
    def _norm(m):
        out = {}
        for k, v in m.items():
            if k in contract.PREDICATES and isinstance(v, list) and len(v) == 1:
                v = v[0]
            out[k] = str(v)
        return out
    if _norm(back.meta) != _norm(meta):
        return data, [f"직렬화 왕복 불일치 — 쓰지 않았다: {sorted(back.meta)} vs {sorted(meta)}"]
    return data, _validate_node(path, back, body, idx)


def _validate_node(path: Path, node, body: str, idx) -> list[str]:
    """계약 + 그 노드에서 **나가는 참조**의 위상. 전역 검사는 하지 않는다 —
    남이 만든 위반 때문에 내 쓰기가 막히면 안 된다 (설계 D10)."""
    errs = list(contract.validate(node))
    kind = graph.space_of(path)
    for pred in contract.PREDICATES:
        for t in node.edges(pred):
            errs += _topology_of(idx, kind, path.stem, t, pred, node.id)
    for t in node.wikilinks():
        errs += _topology_of(idx, kind, path.stem, t, None, node.id)
    return errs


def _topology_of(idx, kind, stem, name, pred, node_id=None) -> list[str]:
    if pred == "conflicts":
        # 사건 표지는 참조 위상의 예외다(헌법 8조). 표면에서는 열린 사건에
        # 당사자로서 다는 것만 허용한다 — 전역 topology_check와 같은 의미론.
        case = graph._load_cases().get(name)
        if not case:
            return [f"실재하지 않는 사건 참조: {stem} → {name}"]
        if str(case.get("status")) != "docketed":
            return [f"열린 사건이 아니다: {stem} → {name} ({case.get('status')})"]
        if node_id not in [str(x) for x in (case.get("parties") or [])]:
            return [f"그 사건의 당사자가 아니다: {stem} → {name} (헌법 12조 5항)"]
        return []
    r = idx.resolve(name)
    if r[0] in ("external", "dangling"):
        # 대상이 아직 없어도 **경로가 소속을 말한다.** 헌법 8조 3항의 금지는
        # 파일의 실재를 조건으로 걸지 않는데, 구판은 dangling을 먼저 걸러
        # "없는 `_raw` 라운드"를 Domain 노드에 다는 것이 통과했다(그 파일이
        # 생기는 순간 검증기 FAIL이 된다).
        if (r[0] == "dangling" and kind[0] == "domain"
                and "/_raw/" in str(name).replace("\\", "/")):
            return [f"Domain의 _raw 직접 참조: {stem} → {name} — 대상이 아직 "
                    f"없어도 그 자리는 `_raw` 구획이다. 근거는 노드로 증류해 "
                    f"참조한다(헌법 8조 3항)"]
        return []                       # dangling은 경고이지 위반이 아니다
    if r[0] == "ambiguous":
        return [f"모호 참조(같은 이름 또는 같은 id의 노드가 여럿): {stem} → {name}"]
    tkind = r[1]
    # 작업 상태는 **소속과 무관하게** 근거가 되지 못한다 — Workbench 계약 4.2의
    # 주어에는 소속 제한이 없다("Workbench의 작업 상태(비노드)는 근거 또는
    # 권위의 출처로 참조하지 않는다"). 구판은 이 검사가 아래 `scope` 갈래 **안**에
    # 있어 Domain·Person 노드가 작업 상태·scope 기억을 근거로 걸 수 있었다.
    if tkind[0] == "workbench":
        return [f"작업 상태는 근거로 쓰지 않는다: {stem} → {name} — "
                f"Workbench의 작업 상태(scope 기억 포함)는 근거 또는 "
                f"권위의 출처로 참조하지 않는다(Workbench 계약 4.2). "
                f"근거는 그 지식이 나온 곳이다."]
    if kind[0] == "domain" and tkind[0] == "raw":
        return [f"Domain의 _raw 직접 참조: {stem} → {name}"]
    if kind[0] == "scope":
        ok = ((tkind[0] == "scope" and tkind[1] == kind[1])
              or (tkind[0] == "raw" and tkind[1] == kind[1])
              or tkind[0] in ("domain", "person", "workbench-transit",
                              "sources", "governance"))
        if not ok:
            # (작업 상태 갈래는 소속과 무관하므로 위로 올라갔다.)
            # 무엇을 하라는 말이 없으면 받는 쪽이 scope를 바꿔 보다 두 번
            # 거부당한다(표면 감사 실측). 옮기는 중이면 순서가 답이다 —
            # 노드를 먼저 옮기고 그 다음에 잇는다. 아니면 domain 경유다.
            return [f"scope 간 직접 참조: [{kind[1]}] {stem} → {name} {tkind} — "
                    f"옮기는 중이면 노드를 먼저 옮기고 그 다음에 이어라. "
                    f"scope를 넘어 공유할 지식이면 domain 노드로 증류해 경유한다"
                    f"(헌법 8조 3항)"]
    return []


def _dangling_of(path: Path, meta: dict, body: str, idx) -> list[str]:
    """그 노드의 미해석 참조 — 위반이 아니라 경고. 응답에 실어 에이전트가
    조용히 dangling을 쌓지 않게 한다(`list_nodes` 제거의 부작용 차단)."""
    node = meta if isinstance(meta, contract.Node) else \
        contract.Node(path=path, meta=meta, body=body)
    out = []
    for t in set(node.wikilinks()) | {t for p in contract.PREDICATES
                                      for t in node.edges(p)}:
        if idx.resolve(t)[0] == "dangling":
            out.append(t)
    return sorted(out)


def _cas(path: Path, expect_hash: str | None, body_given: bool) -> None:
    """CAS는 **본문 전체 치환**에 결속한다 (Mechanism §6-2 4항). 서명이
    폐지됐으므로 부분 변경(엣지 델타·summary)에는 expect_hash를 요구하지
    않는다 — 보호영역의 승인/반려는 별도 표면(대화형 단말)의 일이다.
    거부 응답에 현재 해시를 담지 않는다 — 관측 증명이 연극이 되지 않게."""
    if expect_hash:
        # 응답은 언제나 `sha256:<hex>`를 주지만, 접두를 떼어 보내는 호출자가
        # 있다. 그대로 비교하면 "그 사이 노드가 변경됐다"는 **거짓 진단**이 되어
        # 멀쩡한 상태를 다시 읽게 만든다 — 값이 같은지는 여전히 그대로 본다.
        expect_hash = expect_hash.strip()
        if re.fullmatch(r"[0-9a-fA-F]{64}", expect_hash):
            expect_hash = "sha256:" + expect_hash.lower()
    if body_given and not expect_hash:
        # **값을 주지 않는다.** 주면 읽지 않고도 통과할 수 있어 "해시는 관측의
        # 증거"(Mechanism §6-2 4항)가 연극이 된다 — 실제로 거부문의 해시를 그대로
        # 되보내는 것으로 전문 덮기가 성립했다.
        #
        # 구판이 값을 실은 것은 표면 감사의 지적("이름만 주고 값을 안 주면
        # `read_node`로만 빠져나올 수 있는데 그 도구는 그걸 금한다") 때문이었다.
        # 그 곤경은 실재했으나, v3.10.0이 **앵커 편집**을 열어 답이 달라졌다 —
        # 부분 편집이면 해시가 아예 필요 없다. 전문을 통째로 갈아야 하는 경우는
        # 읽는 것이 옳고, 그때의 `read_node`는 "해시만 알려고" 부르는 것이 아니다.
        raise WriteError(
            "본문 전체 치환에는 읽은 상태의 해시(expect_hash)가 필요하다 — "
            "쓰지 않았다",
            ["고칠 자리가 정해져 있으면 `old_text`/`new_text` 앵커 편집을 쓰라 "
             "— 해시가 필요 없다. 전문을 통째로 갈아야 하면 `read_node`로 지금 "
             "본문을 읽고 그 응답의 `hash`를 그대로 넣어라."])
    if expect_hash and sha256_file(path) != expect_hash:
        raise WriteError(
            "그 사이 노드가 변경됐다 — 다시 읽고 재시도하라 (CAS 불일치)")


# ── 세션 라우팅 (Mechanism §6-2 3항) ─────────────────────────────────────

ALIAS_DEPTH = 8          # 별칭 사슬 추적 한계 — 순환·장난에 대한 보수적 상한

# 1회용 대화 id의 형태 — UUID(하이픈 유무 무관). 실제 세션 키(`open-hwp`,
# `rhwp`, `lpaiu-cs/ltm-vault` 꼴)와는 겹치지 않는다.
EPHEMERAL_SESSION_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{32})$", re.I)


def ephemeral_session_errors(session: str | None) -> list[str]:
    """1회용 대화 id는 세션 키가 될 수 없다 (Mechanism §6-2 6항).

    세션 키는 **첫 성공이 영구 결속**한다. 그래서 대화마다 새로 나는 값을
    넣으면 그 대화가 끝나는 순간 그 scope의 기억으로 돌아올 길이 사라진다 —
    다음 세션은 다른 id로 오기 때문이다. 결속은 남지만 아무도 그 키로 다시
    오지 않으므로, 대장에는 죽은 행만 쌓인다.

    도구 설명이 이미 금지하고 있었으나 **설명은 강제가 아니었고**, 실측으로
    두 건이 굳었다(2026-08-24 관측: `= Scope/Arel-Wars-2`·`= Scope/gh-hint`).
    형식이 어긋난 `space`를 조용히 버리지 않는 `resolve_landing`의 규율과
    같은 이유로 여기서도 거부한다 — 버리면 호출자는 왜 다음 세션이 기억을
    잃는지 영영 모른다.

    쓰기 경로의 **가장 앞**에서 부른다. `bind_session`은 파일 쓰기 뒤에
    오는 경로가 있어(raw·create_node) 거기서 막으면 부분 성공이 된다."""
    if not session or not EPHEMERAL_SESSION_RE.match(session.strip()):
        return []
    return [f"`{session}`은 1회용 대화 id다 — 세션 키로 쓸 수 없다. 키는 "
            f"**저장소 이름처럼 세션이 바뀌어도 같은 값**이어야 한다"
            f"(`open-hwp`·`rhwp` 꼴). 첫 성공이 그 키를 영구 결속하므로, "
            f"대화마다 새로 나는 값을 주면 다음 세션이 이 scope의 기억에 "
            f"닿지 못한다."]


def routing_errors(session: str | None) -> list[str]:
    """결속을 세우기 **전에** 라우팅 대장이 판독·기록 가능한지 본다.

    `resolve_session`은 판독 실패를 `None`으로 삼켜 "미결속"으로 진행한다. 그래서
    손상된 대장 위에서도 쓰기가 끝까지 가고, 마지막의 `bind_session`이
    `ledger_append`의 손상 거부에 걸려 죽는다 — 표면은 "쓰지 않았다"를 보고하는데
    파일은 남는 **부분 성공**이다(Mechanism §6-2 3항 위반, 실측 재현). 손상은
    언제나 외부 기원이므로(수동 복구·부분 병합) 여기서 fail-closed로 막는다.

    `ephemeral_session_errors`와 같은 자리에서 부른다 — 잠금 앞이든 안이든
    **첫 바이트를 쓰기 전**이면 된다."""
    if not session:
        return []                       # 결속을 세울 일이 없다
    try:
        recs = ledger_read(ROUTING)
    except Exception as e:
        return [f"세션 라우팅 대장을 읽지 못했다: {e} — 이 상태에서 쓰면 결속만 "
                f"실패하고 파일은 남는다. Mechanism §3 8항의 수동 복구가 먼저다"]
    dmg = ledger_damage(recs, ROUTING)
    if dmg:
        return ["세션 라우팅 대장이 손상됐다: " + "; ".join(dmg[:3])
                + " — 손상 위에 이력을 더 쌓지 않는다(Mechanism §3 2항). "
                  "수동 복구가 먼저다"]
    return []


def canonical_session(session: str | None,
                      recs: list[dict] | None = None) -> str | None:
    """세션 키를 **정본 키**로 접는다 (Mechanism §6-2 6항).

    저장소·서버의 개명은 흔한 일이고(이 체계도 llm-vault → ltm-vault →
    osk-system을 거쳤다), 그때마다 세션이 다른 scope로 흩어지면 라우팅이
    이름의 역사에 인질이 된다. 별칭 기록(`kind: "alias"`)이 구 이름을 정본
    이름으로 접어 **어느 이름으로 들어오든 한 scope로 모이게** 한다.

    별칭도 인과 극대가 정본이다 — 분기·순환이면 접지 않고 원래 키를 쓴다."""
    if not session:
        return None
    if recs is None:
        try:
            recs = ledger_read(ROUTING)
        except Exception:
            return session
    seen = {session}
    cur = session
    for _ in range(ALIAS_DEPTH):
        r = resolve_one(recs, cur, "session")
        if not r or r.get("kind") != "alias":
            return cur
        nxt = r.get("canonical")
        if not nxt or nxt in seen:
            return session      # 순환은 접지 않는다 — 원래 입력 키로 남는다
        seen.add(nxt)
        cur = nxt
    return cur


def resolve_session(session: str | None) -> str | None:
    """세션 키 → scope 이름. 별칭을 접은 뒤 판정하며, 인과 극대가 유일할 때만
    확정이고 분기(다기기 동시 최초-확정)면 미확정으로 두어 `space`를 요구한다
    — fail-closed."""
    if not session:
        return None
    try:
        recs = ledger_read(ROUTING)
    except Exception:
        return None
    key = canonical_session(session, recs)
    r = resolve_one(recs, key, "session")
    return r.get("scope") if r and r.get("kind") == "bind" else None


def bind_session(session: str, scope: str, reason: str = "") -> dict:
    """세션→scope 확정. 별칭은 정본 키로 접어 기록하므로 구 이름으로 들어와도
    한 자리에 모인다. 재바인딩도 **새 행 append**다(append-only 유지 — 새
    인과 극대가 새 바인딩)."""
    # 키는 `strip()`한 값으로 적는다 — `ephemeral_session_errors`가 이미
    # 다듬은 값으로 검사하므로, 여기서 원문을 쓰면 `" open-hwp"`가 `open-hwp`와
    # 다른 결속으로 앉아 다음 세션이 그 기억에 닿지 못한다.
    session = (session or "").strip()
    return ledger_append(ROUTING, {
        "kind": "bind", "session": canonical_session(session) or session,
        "scope": scope, "reason": reason or "최초 작업에서 확정"})


def alias_session(alt: str, canonical: str, reason: str = "") -> dict:
    """구 이름 → 정본 이름 별칭. 개명 이력을 대장에 남기는 일이며 MCP 표면에
    노출하지 않는다 — 이름의 정본을 정하는 것은 사용자의 일이다."""
    return ledger_append(ROUTING, {
        "kind": "alias", "session": alt, "canonical": canonical,
        "reason": reason or "개명 이력"})


def resolve_landing(session: str, space: str | None,
                    confine_note: str) -> tuple[str | None, str | None]:
    """세션·space → 착지 scope. `(scope, bound)`를 돌려준다.

    scope별 구획을 갖는 모든 표면(`_raw/`·작업 기억)이 같은 판정을 쓴다. 규율은
    셋이고 **순서가 있다** — 형식 → 실재 → 결속. 셋 다 어긋난 경우 가장 먼저
    고칠 수 있는 것을 지목한다.

    1. 형식이 어긋난 `space`를 **조용히 버리지 않는다.** 버리면 안 준 것과 같아져
       호출부가 "결속이 없다"고 엉뚱한 원인을 지목하고, 받는 쪽은 결속이 멀쩡한데도
       세션 키를 바꿔가며 헤맨다(감사에서 실측된 오진).
    2. 없는 scope는 유효 목록과 함께 거부한다.
    3. 결속이 선 세션에 **그와 다른 scope**를 주는 것도 오류다 — 한 세션은 한
       scope에 속한다(헌법 4조 3항 · Workbench 계약 2.4). 무엇이 번지는지는
       표면마다 다르므로 `confine_note`가 그 이유를 싣는다.

    결속이 없고 `space`도 없으면 `(None, None)` — 호출부가 fail-closed로 다룬다."""
    bound = resolve_session(session)
    if not space:
        return bound, bound
    scope = graph.scope_of_space(space)
    if not scope:
        raise WriteError(
            "space 표기 아님 — 쓰지 않았다",
            [f"`{space}`는 space 표기가 아니다 — `= Scope/<이름>` **두 마디**로 "
             f"준다. `overview`의 `clusters`에는 `= Person/…`이나 더 깊은 경로도 "
             f"섞여 있으니 그대로 옮기지 마라. 가능한 space: {graph.space_list()}"])
    if scope not in graph.scope_names():
        raise WriteError(
            "없는 scope — 쓰지 않았다",
            [f"`{space}`는 scope가 아니다. 가능한 space: {graph.space_list()}"])
    if bound and scope != bound:
        raise WriteError(
            "결속과 어긋나는 착지 — 쓰지 않았다",
            [f"세션 `{session}`은 `= Scope/{bound}`에 결속돼 있다. {confine_note} "
             f"결속대로 쓰려면 `space`를 빼라."])
    return scope, bound


# ── 도구 ─────────────────────────────────────────────────────────────────

def create_node(title: str, summary: str, body: str, drafter: str,
                session: str | None = None, space: str | None = None,
                edges: dict | None = None) -> dict:
    """노드 생성. id·시각은 **서버 전속**이고 author는 `agent` 고정이다(D5).
    space가 없으면 세션 라우팅으로 착지를 정하고, 라우팅이 없으면 space를
    요구한 뒤 성공 시 그 scope로 세션을 확정한다."""
    with _Lock():
        # 이 쓰기가 쓰는 색인은 **하나**다 (v3.7.0). 구판은 한 번의 생성에서
        # 세 벌을 지었다 — 계약 검사·이름 유일성·검증·dangling이 각자 지었고,
        # 그래서 체감 비용이 단가의 3배였다. 잠금 안이라 그 사이에 파일이
        # 바뀌지 않으므로 한 벌이면 족하다.
        idx = graph.Index()
        _require_complete(idx)
        errs = (_check_edges(edges, idx) + _title_errors(title)
                + ephemeral_session_errors(session) + routing_errors(session))
        if errs:
            raise WriteError("계약 위반 — 쓰지 않았다", errs)

        bound = resolve_session(session)
        dest = space or (f"= Scope/{bound}" if bound else None)
        if dest and bound:
            # 결속이 선 세션에 **다른 scope**를 착지로 주는 요청은 거부한다
            # (Mechanism §6-2 6항 — 한 세션은 한 scope에 속한다, 헌법 4조 3항).
            # `_raw`·scope 기억은 `resolve_landing`이 이미 이 규율을 지키는데
            # 노드 생성만 판정하고 거부하지 않았다. Domain·Person 착지는
            # 결속과 무관하므로 건드리지 않는다.
            dkind = graph.space_of(ROOT / dest / "x.md")
            if dkind[0] == "scope" and dkind[1] != bound:
                raise WriteError(
                    "결속과 어긋나는 착지 — 쓰지 않았다",
                    [f"세션 `{session}`은 `= Scope/{bound}`에 결속돼 있다. 한 "
                     f"세션은 한 scope에 속하므로 다른 scope로 착지하지 않는다 "
                     f"— 결속대로 쓰려면 `space`를 빼라. 전역 지식이면 "
                     f"`= Domain/…`·`= Person/…`이 열려 있다."])
        if not dest:
            raise WriteError(
                "착지가 정해지지 않았다 — space를 지정하라. "
                f"지금 쓸 수 있는 군집: {', '.join(_cluster_names()) or '없음'}. "
                "session도 함께 주면(저장소 이름처럼 세션이 바뀌어도 같은 값) "
                "그 scope로 결속되어 다음부터 space 없이 착지한다")
        dest_dir = resolve_in_root(dest)
        if dest_dir is None or not dest_dir.is_dir():
            # 신설 후보 — 막는 대신 한 번 묻는다. 통과하면 디렉토리가 생긴다.
            # (구판은 "신설은 사용자 발의다"라며 전면 거부했으나 그 문구는
            # 규범 무근거였다 — 헌법은 형성의 자동화를 기본으로 둔다.)
            dest_dir = _new_cluster_gate(dest, dest_dir, "이 쓰기가")
        # 새 군집의 첫 노드는 **동명 허브 노드**다 (시행령 §3 6항). 신설
        # 관문을 지나 방금 생겼든 이미 비어 있든, 허브 없이 출발한 군집은
        # 이름뿐인 통이 된다 — 무엇인지 서술하는 노드가 먼저다. 이동·재배정
        # 형성은 이 검사를 받지 않고(§3 1항의 주기 처리 보호) 검증기 보고가
        # 채움을 독촉한다. Workbench 구획은 자체 계약이라 제외.
        if (title != dest_dir.name and "Workbench" not in dest_dir.parts
                and not any(dest_dir.glob("*.md"))):
            raise WriteError(
                "새 군집의 첫 노드는 허브 노드다 — 쓰지 않았다",
                [f"`{dest_dir.name}` 군집이 비어 있다. 먼저 군집과 동명의 "
                 f"허브 노드 `{dest_dir.name}`을(를) 만들어 이 군집이 무엇인지 "
                 f"서술하고, 그 다음 이 노드를 만들어 허브에서 닿게 하라 "
                 f"(헌법 3조 8항 · 시행령 §3 6항)"])
        path = dest_dir / f"{title}.md"
        kind = graph.space_of(path)      # 소속은 노드 파일 경로로 판정한다
        _reject_governance(kind)
        if not graph.is_node_home(kind) or not _is_cluster(kind):
            raise WriteError(
                f"노드를 둘 수 없는 구획이다: {dest} {kind} — 노드는 군집 안에 둔다"
                f" (Space 루트 직속 불가, Mechanism §1 4항)")

        # 이름 색인 하나로 끝난다 — 구판의 `nodes ∪ broken`과 **같은 집합**임이
        # 1,809·10,000 노드 양쪽에서 차집합 공집합으로 검증됐다(심의 실측).
        # 이 검사 때문에 전 노드를 열 이유가 없다.
        if title in idx.names:
            raise WriteError(
                f"같은 이름의 노드가 이미 있다: {title} — 생성하면 중복 후보가 된다")
        # 전역 유일성도 **이식성 키**로 본다. 정확 일치만 보면 `Foo`와 `foo`가
        # 다른 군집에 함께 서고, 그 둘을 한 군집으로 옮기는 순간 대소문자를
        # 접는 파일시스템에서 한쪽이 조용히 덮인다(`move_nodes`). 제목이
        # 전역에서 유일하다는 계약(Mechanism §8 2항)은 "모든 기기에서"여야 한다.
        pkey = _portable_name_key(title)
        gclash = next((s for s in idx.names if _portable_name_key(s) == pkey),
                      None)
        if gclash is not None:
            raise WriteError(
                f"이식성 기준으로 같은 이름의 노드가 이미 있다: {gclash} — "
                f"대소문자나 유니코드 정규화만 다른 이름은 NTFS·APFS에서 같은 "
                f"경로가 된다. 한 군집으로 모이면 한쪽이 덮인다")
        clash = _name_collision(dest_dir, title)
        if clash is not None:
            raise WriteError(
                f"같은 군집에 이미 있는 이름이다: {clash} — 대소문자나 유니코드 "
                f"정규화만 다른 이름은 NTFS·APFS에서 **같은 경로**가 되어 그 기기의 "
                f"체크아웃에서 충돌한다(한쪽만 남는다)")

        now = now_kst()
        meta = {"id": new_node_id(),
                "created": now, "updated": now,
                "author": "agent", "drafter": drafter, "summary": summary}
        for pred, tg in (edges or {}).items():
            meta[pred] = _as_links(pred, tg)
        data, errs = _validate_render(path, meta, body, idx)
        if errs:
            raise WriteError("계약·위상 위반 — 쓰지 않았다", errs)

        _atomic_write(path, data)
        # 방금 쓴 노드를 손에 든 색인의 **이름 색인**에 등재한다 —
        # `_dangling_of`가 보는 것은 쓰기 **후**의 상태여야 한다. 구판은 여기서
        # 색인을 새로 지어 그 상태를 얻었다. 등재하지 않으면 자기 자신을
        # 가리키는 Link가 dangling으로 잘못 실려, 오타가 아닌 것을 오타라고
        # 알리게 된다. 판독은 `resolve`가 그때 한 번 한다(파일은 이미 있다).
        idx.register_new(path, kind)
        # 결속은 **scope일 때만** — Domain/Person에 결속하면 자동 라우팅이
        # 존재하지 않는 `= Scope/<이름>`을 가리켜 그 키가 벽돌이 된다(7차 중대 C)
        bound_now = None
        if session and not bound and kind[0] == "scope":
            # 결속 값은 **scope 이름**이지 말단 디렉토리명이 아니다. 구판은
            # `dest_dir.name`을 썼고, 그래서 하위 군집(`= Scope/W1/Sub`)에서
            # 처음 쓴 세션이 존재하지 않는 `Sub`에 묶였다 — 그 뒤 `append_raw`는
            # `= Scope/Sub/_raw/`라는 유령 scope를 만들고, 노드 생성은 최상위
            # 신설 관문으로 갔다. 깊이는 갈래이지 소속이 아니며(Mechanism §1
            # 2항), 소속은 `space_of`가 이미 정확히 말해 준다.
            bind_session(session, kind[1])
            bound_now = kind[1]             # 실제로 결속했을 때만 보고한다
        return {"ok": True, "name": title,
                "path": posix_rel(path, ROOT), "id": meta["id"],
                "new_hash": sha256_bytes(data),
                "bound_scope": bound_now,
                "dangling": _dangling_of(path, meta, body, idx)}


def _require_complete(idx) -> None:
    """관측이 불완전하면 쓰지 않는다.

    이름·id의 유일성은 **전체를 봐야만** 말할 수 있다. 디렉토리 하나가 안
    읽혔는데 쓰면, 그 안의 이름을 못 본 채 "없다"고 판정하는 것이다 — 실측으로
    재현했다: W1 열거를 막고 `create_node`를 부르니 W2에 같은 이름이 거부 없이
    만들어졌다. 시행령 §11이 요구하는 것은 그 자리에서 **보류하고 보고**하는
    것이지 조용히 지나가는 것이 아니다."""
    if idx.complete:
        return
    raise WriteError(
        "vault를 전부 관측하지 못했다 — 쓰지 않았다",
        idx.scan_errors + [
            "이름과 id의 유일성은 전체를 봐야 말할 수 있다. 못 읽은 자리가 "
            "있으면 그 안의 이름을 못 본 채 '없다'고 판정하게 된다. 권한·잠금을 "
            "확인하고 다시 보내라."])


# 파손 파일에 대한 생성 관문은 **두지 않는다.**
#
# v3.7.2까지는 `_existing_ids` 안에 그런 관문이 있었고 근거는 id였다 — "그
# 파일의 id를 모르면 새 id가 겹치지 않는다고 증명할 수 없다". 근거는 §4-1이
# 지웠지만 관문 자체는 다른 일을 하고 있었다: 파손 파일이 오래 남지 못하게
# 하는 **강제 함수**. 그것을 제자리에 다시 세우려 했으나 값이 맞지 않는다 —
# "파손이 있는가"를 물으려면 `idx.broken`을 봐야 하고, 그것은 전수 판독을
# 강제한다. §4-1이 없앤 6.4초를 관문 하나에 도로 내는 셈이다.
#
# 대신 알림에 기댄다. `overview`가 이미 `broken`을 싣고, 그 자리에서는 색인이
# 이미 파싱돼 있어 **무료**다. 세션 시작마다 돌므로 오래 숨지 않는다.
#
# 관문을 되살리고 싶어지면 조건은 하나다: **전수 판독이 이미 일어난 자리**에
# 두어야 한다. 쓰기 통로는 그런 자리가 아니다.


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def _stored_edges(v) -> list[str]:
    """frontmatter에 **저장된 표기 그대로**의 대상 목록.

    `contract.edge_targets`와 다르다 — 그쪽은 대상을 해소하려고 `[[…#앵커]]`의
    `#` 뒤를 버린다(해석용으로는 옳다). 그 손실된 목록을 다시 저장 목록으로 쓰면
    엣지를 하나 더하거나 빼는 것만으로 **남아 있던 라운드 좌표가 전부 지워진다**:
    `[[…/_raw/rec#3]]` → `[[…/_raw/rec]]`. 근거에서 증거로 가는 길이 그렇게
    끊긴다(시행령 §1 3항이 `_raw/` 참조에 라운드 제목을 요구한다).

    그래서 델타는 **여기서 읽고** 동일성 비교만 `target_stem`으로 한다."""
    if v is None or v == "" or v == []:
        return []
    return [str(x) for x in _as_list(v)]


def _as_links(pred: str, targets) -> str | list:
    """입력 대상을 저장 표기로 접는다 — 맨값은 위키링크로 감싸고, 이미
    위키링크면 그대로 둔다. `derived-from`의 id 맨값만 감싸지 않는다.

    v3.7.3부터 노드 근거의 정본 표기는 **제목 위키링크**이므로, 제목을 맨값으로
    받으면 여기서 `[[제목]]`이 된다. id 맨값을 그대로 두는 것은 **구형 표기의
    호환**이다(§8 2항 — 계속 해석한다).

    id 입력을 제목으로 **정규화하지 않는다.** 정규화하려면 id→노드 해석이
    필요하고 그것은 전수 판독을 부르는데, 이 함수는 `add_edges`·`remove_edges`
    경로에서 **이미 저장된 간선까지** 다시 접는 자리라 호출자가 요청하지 않은
    간선을 바꾸게 된다. 구형 표기의 이관은 별도 작업이다."""
    out = []
    for t in _as_list(targets):
        s = str(t).strip()
        if s.startswith("[[") or (pred == "derived-from" and re.match(ID_RE, s)):
            out.append(s)
        else:
            out.append(f"[[{s}]]")
    return out[0] if len(out) == 1 else out


def update_node(name: str, body: str | None = None,
                expect_hash: str | None = None, summary: str | None = None,
                add_edges: dict | None = None,
                remove_edges: dict | None = None,
                old_text: str | None = None,
                new_text: str | None = None) -> dict:
    """본문·summary·엣지 수정. 엣지는 **델타**이므로 서버가 잠금 안에서 현재
    상태에 적용한다 — 낡은 읽기가 앞선 갱신을 덮는 일이 구조적으로 없다.

    `old_text`/`new_text`는 본문의 **앵커 편집**이며 같은 규율을 본문으로
    넓힌 것이다. 앵커는 "고칠 자리를 봤다"는 증거이므로 "전체를 봤다"는
    증거(`expect_hash`)를 요구하지 않는다 — §6-2 4항이 부분 변경을 그렇게
    갈라 둔다. 전문 치환(`body`)은 그대로 해시를 요구한다.

    실측이 이 경로를 부른 이유: 본문을 실은 `update_node` 78건이 그 도구
    발화 비용의 85%를 썼는데 연속 재작성 간 유사도 중앙값이 0.84였다 —
    국소 편집에 전문을 다시 뱉고 있었다. 하네스 `Edit`의 실측 대조에서
    부분 편집은 전문 치환의 0.119배이고 실패율은 오히려 낮다(2.2%)."""
    # 인자 검사는 잠금 **밖**에서 — 형태가 틀린 요청이 잠금을 잡을 이유가 없다.
    if (old_text is None) != (new_text is None):
        raise WriteError(
            "앵커 편집은 `old_text`와 `new_text`를 함께 받는다 — 쓰지 않았다",
            ["지울 때는 `new_text`에 빈 문자열을 준다. 한쪽만으로는 무엇을 "
             "무엇으로 바꾸는지 정해지지 않는다."])
    if old_text is not None:
        if body is not None:
            raise WriteError(
                "`body`와 앵커 편집은 함께 쓸 수 없다 — 쓰지 않았다",
                ["`body`는 전문 치환이고 앵커는 부분 편집이다. 둘을 함께 주면 "
                 "어느 쪽이 이겼는지 응답으로 구분되지 않는다. 하나만 보내라."])
        # 앵커도 **파서가 읽을 형태**로 접는다 — 본문은 CRLF 없이 저장되므로
        # (`_norm_body`), CRLF 앵커를 그대로 대조하면 호출자가 방금 넣은 문장도
        # 못 찾는다. 접는 자리를 한 벌로 두어 대조와 치환이 갈리지 않게 한다.
        old_text = _norm_newlines(old_text)
        new_text = _norm_newlines(new_text or "")
        if not old_text:
            raise WriteError(
                "`old_text`가 비어 있다 — 쓰지 않았다",
                ["빈 앵커는 본문의 모든 자리에 맞으므로 고칠 자리를 가리키지 "
                 "못한다. 바꿀 대목을 그대로 넣어라."])
        if old_text == new_text:
            raise WriteError(
                "`old_text`와 `new_text`가 같다 — 쓰지 않았다",
                ["바뀌는 것이 없다. 고칠 내용을 `new_text`에 담아라."])
    with _Lock():
        idx = graph.Index()                     # 이 쓰기가 쓰는 색인은 하나다
        _require_complete(idx)
        errs = _check_edges(add_edges, idx) + _check_edges(remove_edges, idx)
        if errs:
            raise WriteError("계약 위반 — 쓰지 않았다", errs)
        path = _live_locate(name, idx)
        if path is None or not path.is_file():
            raise WriteError(f"노드 없음: {name}")
        kind = graph.space_of(path)
        _reject_governance(kind)
        try:
            n = contract.parse(path)
        except Exception as e:
            raise WriteError(f"파손된 노드다 — 수동 확인이 먼저다: {name} ({e})")

        _cas(path, expect_hash, body is not None)   # 위반 시 raise
        if old_text is not None:
            # **유일성이 안전 계약의 전부다.** 여러 곳에 맞으면 어디를 고칠지
            # 호출자가 정한 바가 없고, 아무 곳이나 고르는 것은 조용히 틀린
            # 자리를 고치는 길이다. 하네스 `Edit` 실측에서 유일성 위반은
            # 5,454건 중 5건(0.09%)이라 이 계약은 실무에서 거의 공짜다.
            hits = n.body.count(old_text)
            if hits != 1:
                raise WriteError(
                    ("앵커가 본문에 없다 — 쓰지 않았다" if hits == 0 else
                     f"앵커가 본문에 {hits}곳 맞는다 — 쓰지 않았다"),
                    ([f"`old_text`가 `{name}`의 본문에 나오지 않는다. 노드가 "
                      f"그 사이 바뀌었을 수 있다 — `read_node`로 지금 본문을 "
                      f"보고 그대로 복사해 넣어라. 공백·줄바꿈까지 일치해야 한다."]
                     if hits == 0 else
                     [f"어느 자리를 고칠지 정해지지 않는다. 앞뒤 줄을 함께 "
                      f"넣어 앵커를 **유일하게** 만들어라."]))
            body = n.body.replace(old_text, new_text, 1)
        meta = dict(n.meta)
        replaced_summary = None
        changed = False
        if summary is not None and str(meta.get("summary")) != summary:
            replaced_summary = str(meta.get("summary"))
            meta["summary"] = summary
            changed = True
        # 두 루프 모두 **누적된 `meta`**에서 현재값을 읽는다. 구판은 각자
        # `n.edges(pred)`로 **원본**을 다시 읽었고, 그래서 같은 술어에 add와
        # remove를 함께 주면 remove가 "add가 없었던 것처럼" 계산한 값으로
        # meta를 덮었다 — 원본에 지울 것 하나뿐이었으면 술어를 통째로 `pop`해
        # **방금 추가한 근거까지 사라졌다.** 그러면서 `ok: true`를 냈다.
        #
        # "근거를 A에서 B로 바꾼다"는 드문 호출이 아니라 이관·오타 수정·근거
        # 갱신의 자연스러운 표현이다. 실제로 v3.7.3 이관에서 두 번 걸렸고,
        # 두 번째는 이 결함을 재현해 기록한 직후였다 — 알고도 피해지지 않았다.
        for pred, tg in (add_edges or {}).items():
            cur = _stored_edges(meta.get(pred))            # 저장 표기 그대로
            have = {contract.target_stem(x) for x in cur}  # 표기 차이는 같은 대상
            new = []
            for t in _as_list(tg):
                k = contract.target_stem(t)
                if k in have:
                    continue
                have.add(k)          # 한 호출 안의 중복도 한 번만 앉는다
                new.append(t)
            if new:
                meta[pred] = _as_links(pred, cur + new)
                changed = True
        for pred, tg in (remove_edges or {}).items():
            drop = {contract.target_stem(t) for t in _as_list(tg)}
            cur = _stored_edges(meta.get(pred))
            keep = [t for t in cur if contract.target_stem(t) not in drop]
            if len(keep) != len(cur):
                changed = True
                if keep:
                    meta[pred] = _as_links(pred, keep)
                else:
                    meta.pop(pred, None)
        new_body = n.body if body is None else body
        if body is not None and _norm_body(body) != _norm_body(n.body):
            changed = True
        extra_keys = [k for k in meta
                      if k not in contract.ORDER and k not in contract.PREDICATES]
        if extra_keys:
            # 손으로 넣은 필드를 조용히 지우지 않는다 — 직렬화가 계약 필드만
            # 쓰므로 통과시키면 무경고 소실이 된다(7차 경미 F)
            raise WriteError(
                f"계약 밖 필드가 있는 노드다 — 표면으로 고칠 수 없다: {extra_keys}",
                [f"계약 외 필드: {k}" for k in extra_keys])
        # conflicts 표지의 부착·원상 제거는 updated을 갱신하지 않는다
        # (시행령 §1 4항 — 헌법 3조 4항의 예외)
        only_conflicts = (body is None and summary is None
                          and not (add_edges or {}).keys() - {"conflicts"}
                          and not (remove_edges or {}).keys() - {"conflicts"}
                          and bool(add_edges or remove_edges))
        if not changed:
            # 변경이 없으면 쓰지 않는다 — 내용이 그대로인데 updated만
            # 갱신하면 "상태가 변경될 때 갱신한다"(시행령 §1 4항)에 어긋난다
            return {"ok": True, "no_change": True, "name": name,
                    "path": posix_rel(path, ROOT), "id": n.id,
                    "new_hash": sha256_file(path),
                    "edges": {p: n.edges(p) for p in contract.PREDICATES},
                    "dangling": _dangling_of(path, n.meta, n.body, idx)}
        if not only_conflicts:
            meta["updated"] = now_kst()

        data, errs = _validate_render(path, meta, new_body, idx)
        if errs:
            raise WriteError("계약·위상 위반 — 쓰지 않았다", errs)
        _atomic_write(path, data)
        out = {"ok": True, "name": name, "path": posix_rel(path, ROOT),
               "id": n.id, "new_hash": sha256_bytes(data),
               "updated_kept": only_conflicts,
               "edges": {p: contract.Node(path=path, meta=meta,
                                          body=new_body).edges(p)
                         for p in contract.PREDICATES},
               "dangling": _dangling_of(path, meta, new_body, idx)}
        if replaced_summary is not None:
            out["replaced_summary"] = replaced_summary
        return out


def _plan_move(name: str, dest_dir: Path, dest_space: str, idx):
    """이동 하나의 **검사만** 수행하고 (원본, 목표, 노드)를 낸다 — 아무것도
    쓰지 않는다.

    `move_nodes`가 전부-아니면-전무이려면 **검사를 전부 먼저** 돌려야 한다.
    검사와 실행이 한 덩이면 절반 옮긴 상태가 남고, 그 상태는 허브 배선이
    어중간해 사람이 무엇을 되돌릴지 알기 어렵다."""
    path = _live_locate(name, idx)
    if path is None or not path.is_file():
        # 결과를 함께 말한다 — "전부 아니면 전무"를 문안으로만 약속하면 받는
        # 쪽이 못 믿고 확인 질의를 한 번 더 한다(표면 감사 실측).
        raise WriteError(f"노드 없음: {name} — 아무것도 옮기지 않았다")
    _reject_governance(graph.space_of(path))
    # 허브 노드는 이름이 군집에 결박한다 (시행령 §3 6항) — 밖으로 나가면
    # 군집이 허브를 잃고, 도착지에는 남의 군집 이름을 단 노드가 앉는다.
    # 하위 허브도 같은 식으로 걸린다(`graph.is_hub`) — 하위 군집의 재편은
    # 허브만 옮기는 일이 아니라 폴더째 옮기는 일이므로 `move_cluster`가 맡는다.
    if graph.is_hub(path):
        raise WriteError(
            "허브 노드는 군집 밖으로 이동할 수 없다 — 옮기지 않았다",
            [f"`{name}`은 `= …/{path.parent.name}` 군집의 동명 허브 노드다"
             f"(시행령 §3 6항). 군집째 옮기려면 `move_cluster`를 쓴다"])
    target = dest_dir / path.name
    dst_kind = graph.space_of(target)   # 소속은 노드 파일 경로로 판정한다
    _reject_governance(dst_kind)
    if not graph.is_node_home(dst_kind) or not _is_cluster(dst_kind):
        raise WriteError(
            f"노드를 둘 수 없는 구획이다: {dest_space} {dst_kind} —"
            f" 노드는 군집 안에 둔다 (Space 루트 직속 불가)")
    clash = _name_collision(dest_dir, path.stem)
    if clash is not None:
        raise WriteError(
            f"목적지에 이미 있는 이름이다: {clash} — 대소문자나 유니코드 "
            f"정규화만 달라도 NTFS·APFS에서 같은 경로가 된다")
    try:
        n = contract.parse(path)
    except Exception as e:
        raise WriteError(f"파손된 노드다 — 수동 확인이 먼저다: {name} ({e})")
    # pin은 **군집 경로 또는 노드 id**를 대상으로 한다(Mechanism §6 1항 ·
    # 시행령 §3 4항 "필요하면 개별 노드의 배치에도 붙일 수 있다"). 구판은
    # 디렉토리만 대조해 노드 pin이 이동에서 조용히 무시됐다. 판독을 위로
    # 옮겨 id를 손에 쥔 뒤 함께 본다.
    if (_pinned(posix_rel(path.parent, ROOT) + "/")
            or _pinned(posix_rel(dest_dir, ROOT) + "/")
            or _pinned(n.id)):
        raise WriteError(
            "pin으로 고정됐다 — 자동 재배정에서 제외된다 "
            "(시행령 §3 4항). 사용자 발의로만 옮긴다")
    return path, target, n


def _apply_move(path: Path, target: Path, n, idx) -> None:
    """검사를 통과한 이동 하나를 수행한다. 바이트 불변 — `updated` 갱신 없음."""
    # 마지막 방어선 — 목적지가 이미 차 있으면 덮지 않는다. 계획 단계의 검사는
    # 계획을 세울 때의 파일시스템을 보므로, **이 묶음의 앞선 이동이 방금 만든**
    # 파일은 보지 못한다. 대소문자를 접는 파일시스템에서 `Foo.md`가 놓인 자리에
    # `foo.md`를 rename하면 `os.replace`가 조용히 덮는다.
    if target.exists():
        raise WriteError(
            f"목적지가 이미 차 있다: {posix_rel(target, ROOT)} — 덮지 않았다 "
            f"(대소문자·정규화만 다른 이름이 같은 경로가 되는 파일시스템이다)")
    # 이동을 이동으로 기록한다(시행령 §6 4항) — 기록이 없으면 반려가 이동을
    # 추가+삭제로 보아 노드를 지우거나 복제한다. 실패한 이동의 잔행은
    # 무해하므로(그 자리에 그 id가 없으면 안 쓰인다) 이동 **전에** 적는다.
    approvals.record_move(n.id, path, target)
    os.replace(path, target)
    # 손에 든 색인은 이동 **전** 경로를 쥐고 있다. 갱신하지 않으면 아래
    # `_dangling_of`가 옛 경로를 열려다 실패해 이 노드의 자기링크를
    # dangling으로 오보한다 — `create_node`가 `register_new`로 막는 것과
    # 같은 실패이고, 오타가 아닌 것을 오타라고 알리게 된다.
    idx.retarget(path, target, graph.space_of(target))


def _hub_links(srcs: set, dest: Path, moved: set, idx) -> list:
    """이동으로 손봐야 할 허브를 **양쪽 다** 보고한다 — 뺄 것(`remove`)과
    더할 것(`add`).

    도달은 본문 Link로만 성립하므로(v3.6.0), 노드가 다른 군집으로 가면 출발지
    허브의 Link는 죽은 줄이 되고 **도착지 허브에는 그 노드로 가는 줄이 없다.**
    기계가 대신 고칠 수 없다 — 항해는 큐레이션이라 어느 갈래에 걸지가
    판단이다(시행령 §3 7항). 그래서 고치지 않고 알린다.

    구판은 **출발지만** 알렸다. 표면 감사에서 감사자가 그 안내대로 출발지
    링크를 지웠더니 그 노드가 도착지에서 고아가 됐고, 허브 검사가 비활성이라
    `verdict`는 PASS였다 — **안내를 따르면 vault가 나빠지는데 아무도 말해
    주지 않았다.** 절반만 아는 안내는 안 하느니만 못하다.

    허브의 손잡이는 **제목**으로 낸다 — 이 표면의 다른 모든 손잡이가 제목인데
    여기만 경로를 내면 호출자가 변환을 스스로 해야 한다(감사 지적)."""
    out = []
    for h in sorted(srcs):
        hub = h / f"{h.name}.md"
        if not hub.is_file():
            continue
        try:
            n = idx.node(hub)
        except Exception:
            continue
        gone = sorted({contract.target_stem(t) for t in n.wikilinks()} & moved)
        if gone:
            out.append({"hub": hub.stem, "remove": gone})
    dhub = dest / f"{dest.name}.md"
    if dhub.is_file():
        try:
            have = {contract.target_stem(t) for t in idx.node(dhub).wikilinks()}
            need = sorted(moved - have)
        except Exception:
            need = sorted(moved)
        if need:
            out.append({"hub": dhub.stem, "add": need})
    return out


def move_node(name: str, dest_space: str) -> dict:
    """군집 재배정. 이동은 바이트 불변이라 CAS가 없다(경로는 상태, 동일성은
    id). pin된 군집은 출발·도착 어느 쪽이든 거부한다(시행령 §3 4항)."""
    r = move_nodes([name], dest_space)
    one = r["moved"][0]
    return {"ok": True, "name": one["name"], "id": one["id"],
            "path": one["path"], "new_hash": one["new_hash"],
            "moved_from": one["moved_from"], "dangling": one["dangling"],
            "hub_links": r["hub_links"]}


def move_nodes(names: list[str], dest_space: str) -> dict:
    """노드 여럿을 한 군집으로 옮긴다 — **전부 아니면 전무**.

    분화(시행령 §3 7항)의 실제 모양이 이것이다: 한 갈래를 통째로 하위 군집에
    내린다. 하나씩 부르면 잠금과 색인을 N번 짓고(v3.7.0이 세운 "쓰기 1회 =
    색인 1회"의 반대), 중간에 하나가 거부되면 절반만 내려간 상태가 남는다.

    검사를 **전부 먼저** 돌리고 하나라도 걸리면 아무것도 쓰지 않는다. 그 뒤의
    실행은 개별 `os.replace`라 원자적이지만 묶음은 아니다 — 실행 중 OSError는
    이미 옮긴 것을 되돌리지 않고 그대로 보고한다(자료는 어느 쪽에도 남아 있고,
    이동은 바이트 불변이라 되돌리기가 다시 옮기는 일이다)."""
    if not names:
        raise WriteError("옮길 노드가 없다")
    with _Lock():
        idx = graph.Index()                     # 이 이동이 쓰는 색인은 하나다
        _require_complete(idx)
        dest_dir = resolve_in_root(dest_space)
        if dest_dir is None or not dest_dir.is_dir():
            dest_dir = _new_cluster_gate(dest_space, dest_dir, "이 이동이")
        # 중복 판정은 **해석된 경로**로 한다. 이름 문자열로만 보면 같은 노드를
        # 제목과 id로 두 번 줬을 때 통과해, 첫 rename 뒤 둘째가 부재로 죽는다 —
        # 이미 옮긴 것은 남으므로 "전부 아니면 전무"가 깨진다.
        #
        # 그리고 **이식성 키**로도 본다. 대소문자·정규화만 다른 두 노드를 한
        # 군집으로 옮기면 목적지 검사(`_plan_move`의 `_name_collision`)는 아직
        # 그 파일이 없으니 통과시키고, `os.replace`가 둘째로 첫째를 덮는다 —
        # 노드 하나가 소실되는데 응답은 `ok:true`였다(실측 재현).
        seen, seen_keys, plans = set(), {}, []
        for name in names:
            plan = _plan_move(name, dest_dir, dest_space, idx)
            src = plan[0]
            if src in seen:
                raise WriteError(f"같은 노드를 두 번 옮길 수 없다: {name}")
            seen.add(src)
            key = _portable_name_key(src.stem)
            if key in seen_keys:
                raise WriteError(
                    f"한 번에 옮길 수 없는 이름 충돌이다: {seen_keys[key]} / "
                    f"{src.stem} — 대소문자나 유니코드 정규화만 다른 이름은 "
                    f"NTFS·APFS에서 같은 경로가 되어 한쪽이 덮인다. 아무것도 "
                    f"옮기지 않았다")
            seen_keys[key] = src.stem
            plans.append(plan)
        srcs = {p.parent for p, _t, _n in plans}
        moved_stems = {p.stem for p, _t, _n in plans}
        stale = _hub_links(srcs - {dest_dir}, dest_dir, moved_stems, idx)
        # 최상위 군집을 건너면 **알린다** — 거부하지는 않는다.
        #
        # 노드 하나를 다른 scope로 재배정하는 것은 정당한 행위이고(구판
        # `move_node`가 늘 하던 일이다), 실제 위반은 출발지 허브가 계속
        # 가리키는 것이라 `hub_links`의 `remove`를 따르면 해소된다. 그래서
        # `move_cluster`처럼 막지 않는다 — 거기는 폴더 아래 전부의 소속이
        # 한꺼번에 바뀌어 되돌릴 수 없이 무더기 위반이 되지만, 여기는 한 노드씩
        # 이고 안내로 닫힌다.
        #
        # 다만 침묵하면 안 된다. 표면 감사에서 감사자가 이 이동을 하고 `ok:true`
        # 를 받은 뒤 검증기 FAIL로 알게 됐다 — 응답이 그 사실을 말하지 않았다.
        dtop = dest_dir.relative_to(ROOT).parts[:2]
        crossed = sorted(p.stem for p, _t, _n in plans
                         if p.relative_to(ROOT).parts[:2] != dtop)
        out = []
        for path, target, n in plans:
            before = sha256_file(path)
            _apply_move(path, target, n, idx)
            out.append({"name": path.stem, "id": n.id,
                        "path": posix_rel(target, ROOT), "new_hash": before,
                        "moved_from": posix_rel(path, ROOT),
                        "dangling": _dangling_of(target, n.meta, n.body, idx)})
        r = {"ok": True, "moved": out, "count": len(out),
             "dest": posix_rel(dest_dir, ROOT), "hub_links": stale}
        if crossed:
            r["crossed_scope"] = {
                "nodes": crossed,
                "why": "최상위 군집을 건넜다 — 출발지 허브가 이 노드들을 계속 "
                       "가리키면 scope 간 직접 참조가 되어 검증기가 막는다"
                       "(헌법 8조 3항). `hub_links`의 `remove`를 먼저 하라"}
        return r


def move_cluster(name: str, dest_parent: str) -> dict:
    """하위 군집을 **폴더째** 옮긴다 — 재편(승격·강등·자리 바꾸기).

    `name`은 하위 군집의 이름(= 그 폴더명 = 그 허브의 이름)이고 `dest_parent`는
    새 부모 군집의 경로다. 폴더가 통째로 가므로 **그 아래의 하위 군집도 함께
    간다** — 재귀 처리가 따로 필요 없다.

    **같은 최상위 군집 안에서만** 옮긴다. `_space_of_parts`가 소속을 `parts[1]`
    로 정하므로 최상위를 건너면 그 안 노드 **전부의 scope가 한꺼번에 바뀌고**,
    그러면 기존 참조가 무더기로 헌법 8조 3항 위반이 된다. scope를 건너는 것은
    재편이 아니라 **증류**이며(헌법 8조 3항 — "scope 사이에 공유되는 지식은
    domain 노드로 증류하여 경유한다") 다른 문으로 간다.

    노드는 바이트가 안 바뀌고 소속도 안 바뀐다(최상위가 같으므로). 그래서
    위상 검사를 다시 돌릴 필요가 없다 — 기존 참조가 전부 그대로 유효하다.
    이동 대장은 노드마다 남긴다(시행령 §6 4항 — 반려가 이동을 추가+삭제로
    오독하지 않게)."""
    with _Lock():
        idx = graph.Index()
        _require_complete(idx)
        src = _live_locate(name, idx)
        if src is None or not src.is_file():
            raise WriteError(f"노드 없음: {name}")
        if not graph.is_hub(src):
            raise WriteError(
                f"군집이 아니다: {name} — 군집의 허브(폴더와 동명인 노드)만 "
                f"군집째 옮길 수 있다. 노드 하나를 옮기려면 `move_nodes`를 쓴다")
        sdir = src.parent
        _reject_governance(graph.space_of(src))
        ddir = resolve_in_root(dest_parent)
        if ddir is None or not ddir.is_dir():
            raise WriteError(
                f"목적지 군집이 없다: {dest_parent} — 재편은 이미 있는 군집 "
                f"안으로만 한다(먼저 그 군집을 만든다)")
        _reject_governance(graph.space_of(ddir / "x.md"))
        srel, drel = sdir.relative_to(ROOT).parts, ddir.relative_to(ROOT).parts
        if srel[:2] != drel[:2]:
            raise WriteError(
                f"최상위 군집을 건널 수 없다: {posix_rel(sdir, ROOT)} → "
                f"{dest_parent}",
                ["그 안 노드 전부의 소속이 한꺼번에 바뀌어 기존 참조가 무더기로 "
                 "위반이 된다(헌법 8조 3항). scope 사이의 지식 이동은 재편이 "
                 "아니라 **증류**다 — domain 노드로 증류하여 경유한다"])
        if ddir == sdir.parent:
            raise WriteError(f"이미 그 자리다: {posix_rel(sdir, ROOT)}")
        if sdir in ddir.parents or sdir == ddir:
            raise WriteError(
                f"자기 안으로 옮길 수 없다: {posix_rel(sdir, ROOT)} → {dest_parent}")
        if not (ddir / f"{ddir.name}.md").is_file() \
                and ddir.parent.resolve() not in {
                    (ROOT / s).resolve() for s in graph.NODE_SPACES}:
            raise WriteError(
                f"허브 없는 군집 안으로는 옮길 수 없다: {dest_parent}")
        target = ddir / sdir.name
        if target.exists():
            raise WriteError(
                f"목적지에 같은 이름의 군집이 이미 있다: {posix_rel(target, ROOT)} "
                f"— 하위 군집 이름은 전역에서 유일하다")
        if _pinned(posix_rel(sdir, ROOT) + "/") or _pinned(posix_rel(ddir, ROOT) + "/"):
            raise WriteError(
                "pin으로 고정된 군집이다 — 사용자 발의로만 옮긴다 (시행령 §3 4항)")
        inside = [(p, k) for p, k in idx.nodes.values() if sdir in p.parents]
        for p, _k in inside:
            approvals.record_move(idx.node(p).id, p,
                                  target / p.relative_to(sdir))
        os.replace(sdir, target)          # 같은 볼륨 — 원자적 rename
        for p, k in inside:
            idx.retarget(p, target / p.relative_to(sdir), k)
        return {"ok": True, "cluster": name,
                "path": posix_rel(target, ROOT),
                "moved_from": posix_rel(sdir, ROOT),
                "nodes": len(inside),
                "hub_links": _hub_links(
                    {sdir.parent}, ddir, {sdir.name}, idx)}


def record_candidate(type: str, nodes: list[str], reason: str = "") -> dict:
    """충돌 후보 상정 (헌법 12조 2항). 같은 근거(basis)의 후보·각하가 이미
    있으면 append하지 않고 기존 기록을 돌려준다 — 12조 2항 후단의 중복 금지와
    12조 3항의 각하 억제가 여기서 함께 닫힌다. 각하는 사용자 전속이다."""
    with _Lock():
        if type not in CANDIDATE_TYPES:
            raise WriteError(f"미정의 충돌 유형: {type} (Mechanism §4 3항)")
        idx = graph.Index()
        _require_complete(idx)
        parties = []
        for nm in nodes:
            # `_live_locate`를 쓴다 — 동명·동 id면 **고르지 않고 거부**한다
            # (Mechanism §2 1항). 구판은 `idx.nodes.get`으로 뒤에 오는 쪽을
            # 조용히 골라, 쓰기 통로가 거부하는 모호성을 이 통로가 사건부에
            # 박제했다. id 핸들도 여기서 함께 해석된다.
            p = _live_locate(nm, idx)
            if p is None or not p.is_file():
                raise WriteError(f"당사자 노드 없음: {nm} — 근거가 성립하지 않는다")
            try:
                parties.append((idx.node(p).id, sha256_file(p)))
            except Exception as e:
                raise WriteError(f"당사자 판독 실패: {nm} ({e})")
        # 같은 노드를 제목과 id로 두 번 줘도 근거 키는 하나여야 한다 — 안 그러면
        # 같은 쌍의 후보가 다른 `basis`로 두 번 앉아 중복 억제가 빗나간다.
        parties = sorted(set(parties))
        if len({i for i, _ in parties}) < 2:
            raise WriteError(
                "충돌은 **서로 다른** 둘 이상의 당사자를 요한다 — 자기 자신과의"
                " 충돌은 성립하지 않는다")
        basis = _basis(type, parties)
        recs = ledger_read(CANDIDATES)
        for r in recs:
            if r.get("basis") == basis and r.get("basis_version") == 1:
                return {"ok": True, "deduped": True, "rid": r.get("rid"),
                        "kind": r.get("kind"), "basis": basis,
                        "note": "같은 근거의 기록이 이미 있다 (헌법 12조 2·3항)"}
        rec = ledger_append(CANDIDATES, {
            "kind": "candidate", "basis": basis, "basis_version": 1,
            "type": type, "nodes": [i for i, _ in parties], "reason": reason})
        return {"ok": True, "deduped": False, "rid": rec["rid"],
                "basis": basis, "note": "사용자 심의 대상으로 상정됐다"}


def _basis(type: str, parties: list[tuple[str, str]]) -> str:
    """정렬된 당사자 id + 유형 + 검사 당시 상태 해시 (Mechanism §4 2항).
    상태 해시가 '무엇이 달라지면 다시 물어도 되는가'의 답이다 — 없으면 각하가
    영구 봉인이 된다."""
    key = "v1|" + type + "|" + ",".join(sorted(f"{i}@{h}" for i, h in parties))
    return sha256_bytes(key.encode())
