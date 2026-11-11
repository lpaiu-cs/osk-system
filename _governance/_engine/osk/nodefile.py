"""osk.nodefile — 노드 파일의 해석과 직렬화.

이름을 살아 있는 파일로 풀고(`live_locate`), frontmatter와 본문을 디스크의 글자로 만든다. 쓰기
통로(`write`)와 그 아래의 저장 모듈(`scope_memory`·`evictions`)이 함께 쓴다.

`write_file`은 노드와 기억 파일이 디스크에 닿는 한 자리다 — 시험이 쓰기 실패를 여기서 흉내 낸다."""
from __future__ import annotations
import json, re
from pathlib import Path
from .core import ID_RE, posix_rel
from .durable import atomic_write
from . import contract, graph
from .errors import WriteError
from .prose import norm_body, norm_newlines
from .vault import Vault


def live_locate(vault: Vault, name: str, idx) -> Path | None:
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
    `id_of`가 전 노드를 **캐시 없이** 다시 파싱해 5k에서도 이름 경로의
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
            _refuse_dup_id(sorted(idx.dup_ids[nid]))
        hit = idx.by_id.get(nid)
        hits = [hit[0]] if hit else []
    if len(hits) > 1:
        raise WriteError(
            f"같은 이름의 노드가 {len(hits)}개다 — 어느 것인지 정해지지 않아 "
            f"고치지 않았다: {[posix_rel(h, vault.root) for h in hits]}")
    # 이름으로 잡은 노드도 id가 겹쳤으면 고르지 않는다. 사본은 제목이 달라
    # (`Alpha`·`Alpha restored`) 위의 동명 거부를 비켜 가고, 각자 이름으로
    # 고쳐져 조용히 갈라졌다 — id 핸들만 거부하던 구판(2026-09-24 재현).
    twins = idx.id_twins(hits[0]) if hits else []
    if twins:
        _refuse_dup_id(twins)
    return hits[0] if hits else None


def _refuse_dup_id(paths: list[str]):
    msg = (f"같은 id의 노드가 {len(paths)}개다 — 어느 것인지 정해지지 않아 "
           f"고치지 않았다: {paths}")
    raise WriteError(msg, [msg, graph.DUP_ID_ADVICE])


# 문단은 제목·목록 항목·표 행, 또는 빈 줄 뒤의 줄에서 시작한다. 노드 본문은 대개 한 줄짜리
# 항목이라 빈 줄만 세면 절 하나가 통째로 한 문단이 된다.
_PARAGRAPH = re.compile(r"[ \t]*(?:#{1,6}[ \t]|[-*+][ \t]|\d+[.)][ \t]|\|)")


WINDOW_CHARS = 4000     # `read_node` 범위 열람 한 번과 같은 상한


def anchor_windows(body: str, quote: str, limit: int = 5) -> tuple[int, list[tuple[int, int]]]:
    """`quote`가 본문에 나오는 횟수와, 앞 `limit`곳의 창 — 그 문단과 앞뒤 한 문단씩(#273).

    창은 그 한 곳(앞뒤 공백·줄바꿈은 빼고)을 품으므로 유일한 앵커로 그대로 `old_text`가 된다 —
    쓰기는 유일성을 다시 확인한다.
    세부를 빼낸 자리의 연결어는 앞뒤 문단에 걸려 있으므로, 전문을 읽지 않는 큰 노드의
    앵커 편집은 이 창을 싣는다. 이웃 문단은 창이 `WINDOW_CHARS` 안일 때만 붙인다."""
    # ponytail: 4000자를 넘는 한 문단은 이웃 없이 그 문단만 준다(볼트 노드 줄 중 3개). 문장 단위
    # 창은 그런 문단이 늘 때 넣는다.
    quote = norm_newlines(quote)
    if not quote:
        return 0, []
    starts, pos, blank = [], 0, True
    for line in body.splitlines(keepends=True):
        if line.strip():
            if blank or _PARAGRAPH.match(line):
                starts.append(pos)
            blank = False
        else:
            blank = True
        pos += len(line)
    ends = starts[1:] + [len(body)]
    spans = [(s, s + len(body[s:e].rstrip())) for s, e in zip(starts, ends)]
    hits = [m.start() for m in re.finditer(re.escape(quote), body)]
    # 창은 인용 앞뒤의 공백·줄바꿈에서 끝나지 않는다 — 고친 판(new_text)이 그것을 흔히 빠뜨려 두 문단이
    # 붙는다(#273 큰 노드 시험, 2만 자 문단).
    lead, core = len(quote) - len(quote.lstrip()), len(quote.strip())
    windows = []
    for a in hits[:limit]:
        a, b = (a + lead, a + lead + core) if core else (a, a + len(quote))
        i = next((k for k, (_s, e) in enumerate(spans) if e > a), len(spans))
        j = next((k for k in range(len(spans) - 1, -1, -1) if spans[k][0] < b), -1)
        lo, hi = (min(a, spans[i][0]), max(b, spans[j][1])) if i <= j else (a, b)
        if i > 0 and hi - spans[i - 1][0] <= WINDOW_CHARS:
            lo = spans[i - 1][0]
        if j + 1 < len(spans) and spans[j + 1][1] - lo <= WINDOW_CHARS:
            hi = spans[j + 1][1]
        windows.append((lo, hi))
    return len(hits), windows


def frontmatter(meta: dict) -> str:
    """계약 순서대로 frontmatter를 직렬화한다 (Mechanism §2 5항)."""
    lines = ["---"]
    for k in contract.ORDER:
        lines.append(f"{k}: {_scalar(meta[k])}")
    for k in contract.PREDICATES:
        if k in meta and meta[k] not in (None, [], ""):
            lines.append(f"{k}: {_edge_value(k, meta[k])}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render(meta: dict, body: str) -> bytes:
    return (frontmatter(meta) + "\n" + norm_body(body) + "\n").encode()


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


write_file = atomic_write          # 시험이 이 이름을 가로챈다 — 호출부는 그대로


def require_complete(idx) -> None:
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
