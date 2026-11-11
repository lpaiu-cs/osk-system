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
from .core import DOMAIN, PERSON, SCOPE
import json, os, re, tempfile, time, unicodedata
from pathlib import Path

from .core import (ROOT, CANDIDATES, PINS, ID_RE, CASE_RE, RID_RE, rid_key, causal_maxima, ledger_append,
                   ledger_damage, ledger_read, mutation_lock, new_node_id, now_kst, posix_rel,
                   resolve_in_root, resolve_one, sha256_bytes, sha256_file, atomic_write)
from . import approvals, contract, evictions, graph, rechecks, signatures, workspaces, evidence, raw
from .contract import as_list, stored_edges
from .errors import WriteError
from .naming import MAX_FILENAME_BYTES, name_collision, portable_name_key, title_errors
from .prose import norm_body, norm_newlines, space_numeric_tags
# 결속·라우팅은 `routing`에 산다. 위층과 시험이 `write.<이름>`으로 부르는 동안 이 이름들을 남긴다.
from .routing import (ALIAS_DEPTH, EPHEMERAL_SESSION_RE, alias_session, bind_after_write, bind_session,
                      bindings, bound_scopes, canonical_session, ephemeral_session_errors, repo_session,
                      resolve_landing, resolve_session, routing_errors, set_primary, unbind_session)
from .core import VAULT

GOVERNANCE = ("governance",)             # 표면 쓰기 제외 (설계 D8)
CANDIDATE_TYPES = ("contradiction", "duplication", "competition",
                   "delegation-overlap")   # Mechanism §4 3항 (lineage-fork 폐지)


# ── 파일 해석·직렬화 ─────────────────────────────────────────────────────

def live_locate(name: str, idx) -> Path | None:
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
            f"고치지 않았다: {[posix_rel(h, ROOT) for h in hits]}")
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


def _render(meta: dict, body: str) -> bytes:
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


_atomic_write = atomic_write          # 시험이 이 이름을 가로챈다 — 호출부는 그대로


# ── 결속 ─────────────────────────────────────────────────────────────────

def _is_cluster(kind: tuple) -> bool:
    """소속의 둘째 원소가 실제 군집 이름인가. Space 루트 직속 파일은
    `space_of`가 파일명을 군집 이름 자리에 넣으므로(`('scope','x.md')`)
    그것을 군집으로 오인하지 않는다 — 노드는 군집 안에만 둔다."""
    if kind[0] == "workbench-transit":
        return True
    return len(kind) > 1 and bool(kind[1]) and not str(kind[1]).endswith(".md")


def _require_node_cluster(kind: tuple, dest: str) -> None:
    """노드를 둘 자리인가 — 생성·이동·군집 이동이 같은 판정을 쓴다."""
    _reject_governance(kind)
    if not graph.is_node_home(kind) or not _is_cluster(kind):
        raise WriteError(
            f"노드를 둘 수 없는 구획이다: {dest} {kind} — 노드는 군집 안에 둔다. "
            f"Space 루트 직속과 `_`·`.` 접두 구획은 노드 자리가 아니다 "
            f"(Mechanism §1 4항)")


def _reject_governance(kind: tuple) -> None:
    if kind[:1] == GOVERNANCE:
        raise WriteError(
            "통치 구획은 표면 쓰기 대상이 아니다 — 통치 문서의 개정은 정본 "
            "저장소에서 하고 갱신으로 도달한다 (헌법 3조 6항·시행령 §10 1항)")


def _pinned(*targets: str, subtree: str | None = None) -> bool:
    """대상 또는 이동할 하위 군집의 pin·미확정 여부. 대장은 한 번만 읽는다."""
    try:
        recs = ledger_read(PINS)
    except Exception:
        return True             # 판독 실패는 보수적으로 '고정됨' — fail-closed
    if ledger_damage(recs, PINS):
        return True
    wanted = {t.rstrip("/") for t in targets}
    prefix = subtree.rstrip("/") + "/" if subtree else None
    keys = {r.get("target") for r in recs if r.get("target")}
    for k in keys:
        if str(k).rstrip("/") not in wanted and not (prefix and str(k).startswith(prefix)):
            continue
        r = resolve_one(recs, k, "target")
        # 분기·손상은 unpin이 아니다(Mechanism §3 1·2항).
        if r is None or r.get("kind") != "unpin":
            return True
    return False


def cluster_names() -> list[str]:
    """노드를 둘 수 있는 군집 경로 — **거부 응답에만** 실어 보낸다(상주 비용 0).
    표면에 열거 도구가 없으므로 실패하는 순간에 주소를 가르치는 것이 이 목록의
    일이다(10차 ②의 완화분 — 해결은 `overview`).

    하위 군집도 **함께 낸다** — 분화(시행령 §3 7항)로 생긴 자리를 못 보면
    에이전트가 거기에 노드를 둘 방법이 없다. 하위인지는 **허브의 존재**로
    가른다: 폴더와 동명인 노드가 있어야 군집이며, 없으면 그냥 폴더다.
    """
    out = set()

    def walk(d: Path, prefix: str) -> None:
        # DirEntry already knows the type on Windows. Do not stat every node
        # just to find the few directories.
        with os.scandir(d) as entries:
            dirs = [Path(e.path) for e in entries if e.is_dir()]
        for sub in sorted(dirs):
            k = graph.space_of(VAULT, sub / "x.md")
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
            if graph.hub_file(sub):
                out.add(path)
                walk(sub, path)

    for space in (SCOPE, DOMAIN, PERSON):
        d = ROOT / space
        if d.is_dir():
            walk(d, space)
    if (ROOT / SCOPE / "Workbench/transit").is_dir():
        out.add(f"{SCOPE}/Workbench/transit")
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
    return local_lock_path("osk-cluster-ack.json", VAULT.root)


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
    roots = {(ROOT / SCOPE).resolve(), (ROOT / DOMAIN).resolve(),
             (ROOT / PERSON).resolve()}
    top_level = parent in roots
    if not top_level and not graph.hub_file(parent):
        raise WriteError(
            f"선언되지 않은 군집이다: {dest}. 신설은 Space 루트 바로 아래이거나 "
            f"**허브가 있는 군집 안**이어야 한다(Mechanism §1 2항 · 시행령 §3 7항). "
            f"`{parent.name}`에 먼저 동명 허브 노드를 만들면 그 안에 분화할 수 "
            f"있다. 지금 쓸 수 있는 군집: {', '.join(cluster_names()) or '없음'}")
    # 확인 표식·디렉토리를 남기기 **전에** 자리를 본다 — 노드 자리가 아닌 곳
    # (`_misc`·`.notes`)은 물어볼 것도 없이 거부다.
    _require_node_cluster(graph.space_of(VAULT, dest_dir / "x.md"), dest)
    name_errs = title_errors(dest_dir.name)
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
         f"그 신설이 시행령이 맡긴 것인지 확인한 뒤 **같은 요청을 그대로 다시** "
         f"보내라(1시간 안의 재시도는 통과한다) — 노드를 써서 세션의 소속 scope를 "
         f"여는 것은 시행령 §3 9항, 주기 처리가 domain을 여는 것은 §3 1항을 따른다. "
         f"노드를 옮겨 scope를 여는 것과 그 밖의 최상위 군집 신설은 사용자의 허락을 "
         f"확인한 뒤 다시 보낸다(Mechanism §6-2 3항). 기존 군집을 쓰려던 것이면 "
         f"여기서 골라라: {', '.join(cluster_names()) or '없음'}"])


def open_cases() -> list[str]:
    """열린(docketed) 사건 번호 — conflicts 거부에 실어 보낸다."""
    return sorted(no for no, c in graph.load_cases(VAULT).items()
                  if str(c.get("status")) == "docketed")


WORKBENCH_STATE = ("Workbench의 작업 상태(scope 기억 포함)는 근거 또는 권위의 출처로 참조하지 "
                   "않는다(Workbench 계약 4.2). 근거는 그 지식이 나온 곳이다.")


def _check_edges(edges: dict | None, idx, *, adding: bool = True) -> list[str]:
    """술어와 **대상 값의 형**을 함께 본다. 스키마(표면)와 통로(여기) 이중으로
    거는 이유는 CLI·Bash 경유가 스키마를 통과하지 않기 때문이다 — 검증은
    통로에, 교육은 스키마에(10차 정정 ①).

    `derived-from`은 노드만 받는다(헌법 제8조 5항). 더하는 비노드 대상은 거부하고 근거 절로
    보낸다 — 빼는 것(`adding=False`)은 받는다. 이행 전의 옛 비노드 대상을 지울 수 있어야 한다.

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
                target = inner.split("|", 1)[0].strip()
                found = idx.resolve(target.split("#", 1)[0].strip())
                if "/" in inner and idx.resolve(inner)[0] == "node":
                    errs.append(
                        f"derived-from의 노드 근거에 경로 표기는 쓰지 않는다: {t} — "
                        f"경로는 이동에 끊어진다. 그 노드의 **제목**을 쓰라")
                elif adding and found[0] == "nonnode" and found[1][0] == "workbench":
                    # 근거 절로 옮겨도 안 된다 — 진단은 계약 4.2가 맡는다
                    errs.append(f"작업 상태는 근거로 쓰지 않는다: {t} — {WORKBENCH_STATE}")
                elif adding and found[0] != "node" and (
                        found[0] in ("nonnode", "external") or evidence.kind(target) != "name"):
                    # 인용 기록·라운드 좌표·URL·별칭 좌표·경로는 모양으로, 이름은 해석으로 가른다
                    errs.append(
                        f"derived-from은 노드만 받는다: {t} — 비노드 근거는 노드 끝 `# 관측` 절에 "
                        f"`- 날짜 · `좌표` · 지지|조건|반례 — 보인 것` 한 줄로 적는다(헌법 제8조 5·6항). "
                        f"인용 라운드의 좌표는 cite_round·read_cited가 주는 coordinate다")
                continue
            if pred == "conflicts" and not re.match(CASE_RE, t.strip()):
                # 존치 상호 치환은 표면으로 만들 수 없다(닭-달걀) — 에이전트가 달 수
                # 있는 conflicts는 열린 사건 참조뿐이다 (설계 rev.3 §5)
                opened = open_cases()
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
    kind = graph.space_of(VAULT, path)
    for pred in contract.PREDICATES:
        for t in node.edges(pred):
            errs += _topology_of(idx, kind, path.stem, t, pred, node.id)
    for t in node.wikilinks():
        errs += _topology_of(idx, kind, path.stem, t, None, node.id)
    for t in evidence.coordinates(body):
        errs += _topology_of(idx, kind, path.stem, t, rechecks.EVIDENCE, node.id)
    return errs


def _topology_of(idx, kind, stem, name, pred, node_id=None) -> list[str]:
    if pred == rechecks.EVIDENCE:
        # 근거 절의 좌표도 참조다(헌법 제8조 2·3항). 인용 기록은 모양으로 가린다 — 줄인 좌표는
        # 경로가 아니라 색인이 해석하지 못한다. vault의 비노드 파일은 Link와 같은 규칙을 지나고,
        # URL·외부 루트는 vault 밖이다.
        k = evidence.kind(name)
        if k == "round":
            return ([f"Domain의 인용 기록 직접 참조(근거 절): {stem} → {name} — 근거는 노드로 "
                     f"증류해 derived-from으로 참조한다(헌법 8조 3항)"] if kind[0] == "domain" else [])
        if k != "path":
            return []
        name = name.split("#", 1)[0].strip()
    if pred == "conflicts":
        # 사건 표지는 참조 위상의 예외다(헌법 8조). 표면에서는 열린 사건에
        # 당사자로서 다는 것만 허용한다 — 전역 topology_check와 같은 의미론.
        case = graph.load_cases(VAULT).get(name)
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
        if r[0] == "dangling" and kind[0] == "domain" and graph.is_record_ref(str(name)):
            return [f"Domain의 인용 기록 직접 참조: {stem} → {name} — 대상이 아직 "
                    f"없어도 그 자리는 인용 기록 구획이다. 근거는 노드로 증류해 "
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
        return [f"작업 상태는 근거로 쓰지 않는다: {stem} → {name} — {WORKBENCH_STATE}"]
    if kind[0] == "domain" and tkind[0] == "raw":
        return [f"Domain의 인용 기록 직접 참조: {stem} → {name}"]
    if kind[0] == "scope":
        # 비노드(인용 기록·원자료·대장)에는 소속 제약이 없다(헌법 8조 3항, 2026-10-13 개정).
        # 대장은 Workbench 아래에 있어도 작업 상태가 아니라 비노드 제도 기록이다(Workbench 계약 2.3)
        ok = ((tkind[0] == "scope" and tkind[1] == kind[1])
              or tkind[0] in ("raw", "domain", "person", "workbench-transit",
                              "sources", "governance", "ledger"))
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
    node = meta if isinstance(meta, contract.Node) else\
        contract.Node(path=path, meta=meta, body=body)
    return sorted({r["ref"].split("#", 1)[0] for r in graph.reference_review(VAULT, node, idx)
                   if r["resolution"] == "dangling"})


CLAIM_GUIDANCE = (
    "기존 노드 갱신은 같은 주장과 적용 조건을 정정할 때 우선한다. 같은 프로젝트·절차의 다음 단계라는 "
    "이유만으로 한 노드에 합치지 않는다. 독립적으로 검증·재사용할 결론은 기존 또는 새 노드에 보존한다. "
    "노드의 분할은 대상에 결론·조건·근거를 먼저 보존하고 되읽은 뒤 허브 양쪽을 연결하고, 마지막에 원문의 "
    "해당 구간만 짧은 결론과 링크로 접는다. 사실과 변경 이유를 보존하되 중복 문단을 영구히 쌓지 않는다. "
)


def size_feedback(path, body: str) -> dict:
    """Reading-cost hint, not a storage cap or permission to discard knowledge."""
    hub = graph.is_hub(path)
    # ponytail: fixed triage thresholds; calibrate from review costs if they become noisy.
    if len(body) <= (6000 if hub else 12000):
        return {}
    return {"organization_advice": {
        "body_chars": len(body), "hub": hub, "read_view": "outline",
        "instruction": CLAIM_GUIDANCE + "길이는 삭제·강제 분할의 근거가 아니다. 목차와 필요한 절부터 읽고, "
                       "고칠 자리는 read_node(view=고칠 문장)으로 앞뒤 문단째 받아 그 body를 old_text로 고친다. "
                       "현재 결론·적용 조건·근거를 한 곳에 유지하라. 단계별 실행 일지를 계속 덧붙이지 말고 "
                       "레포의 실행 상세는 레포 기록을 인용하라. 허브에는 탐색 경로를 남기고 "
                       "독립적으로 재사용되는 주장만 기존 또는 별도 노드로 증류하라. "
                       "정정 전후의 판단과 출처는 보존하며 미검토 부분은 보류한다."}}


def _edge_report(node) -> dict:
    """응답의 엣지 — 저장된 근거를 **좌표째** 싣는다. `Node.edges`는 해석용이라
    raw 라운드 앵커를 버린다. 그래서 한 기록의 다른 라운드들이 같은 경로로
    되풀려 보였다(2026-09-25 실측: `#42`·`#34`·`#41`이 같은 경로 셋으로 보고됐다)."""
    refs = node.references()
    return {p: [r for rel, r in refs if rel == p] for p in contract.PREDICATES}


def _reference_feedback(path, meta, body, idx, previous=None) -> dict:
    node = contract.Node(path=path, meta=meta, body=body)
    refs = graph.reference_review(VAULT, node, idx, previous)
    out = {"dangling": sorted({r["ref"].split("#", 1)[0] for r in refs
                               if r["resolution"] == "dangling"})}
    if refs:
        out["reference_review"] = {
            "status": "pending", "items": refs,
            "instruction": "내용은 저장됐다. 참조 검토는 미결이다. 노드 근거는 search/read_node의 정확한 제목으로 "
                           "derived-from에, 비노드 근거는 관측 절의 좌표로 적는다 — 인용 라운드는 coordinate, "
                           "vault 원료는 루트 기준 경로, 외부 루트는 별칭 좌표, 그 밖은 확인한 URL이다. "
                           "원료는 지식 노드나 허브가 아니다. 빈 가짜 노드를 만들지 말라. "
                           "organization plan --scope <scope>가 다음 검토로 이어받는다."}
    return {**out, **size_feedback(path, body)}


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
             "— 해시가 필요 없다. `read_node(view=고칠 문장)`이 그 자리를 앞뒤 문단째 "
             "주고, 그 `body`가 곧 `old_text`다. 전문을 통째로 갈아야 하면 `read_node`로 지금 "
             "본문을 읽고 그 응답의 `hash`를 그대로 넣어라."])
    if expect_hash and sha256_file(path) != expect_hash:
        raise WriteError(
            "그 사이 노드가 변경됐다 — 다시 읽고 재시도하라 (CAS 불일치)")


# ── 도구 ─────────────────────────────────────────────────────────────────

def create_node(title: str, summary: str, body: str, drafter: str,
                session: str | None = None, space: str | None = None,
                edges: dict | None = None, settle: str | None = None) -> dict:
    """Create an ordinary node under the shared mutation lock, then hang it in its place."""
    with mutation_lock(VAULT):
        idx = graph.Index(VAULT)             # 생성과 자리 걸기가 이 한 벌을 쓴다
        return _place(create_node_locked(title, summary, body, drafter, session,
                                          space, edges, settle, _idx=idx), idx)


def _place(out: dict, idx) -> dict:
    """새 노드를 같은 작업 안에서 자리에 건다(시행령 §3 8항) — 착지 군집의 허브부터 최상위
    허브까지 각 층이 아래 고리를 Link로 가리키게 한다. 증류와 같은 배선이며, 허브 본문 끝에
    `- [[제목]]` 줄을 더한다. 갈래 안의 정리는 조직 검토가 맡는다. 노드를 먼저 세웠으므로 허브
    쓰기가 실패하면 노드는 그대로 두고, 아직 아래 고리를 가리키지 않는 허브를 `hub_links`로
    알린다. 최상위 허브는 이을 위가 없고, Workbench는 자체 계약이라 걸지 않는다. 증류 쓰기는
    `create_node_locked`를 직접 부르고 자기 배선을 한다."""
    if not out.get("ok"):
        return out
    path = ROOT / out["path"]
    if (graph.space_of(VAULT, path)[0] not in ("scope", "domain", "person")
            or graph.is_hub(path) and len(path.parent.relative_to(ROOT).parts) <= 2):
        return out
    from . import distillation      # distillation이 write를 부르므로 쓸 때 읽는다
    try:
        placed = distillation.node_placement(path, idx, repair=True)
        out["placed"] = [Path(h["path"]).stem for h in placed["hubs"]]
    except (WriteError, OSError, ValueError) as e:
        out["hub_links"] = _unwired(path, idx)
        out["placement_error"] = list(dict.fromkeys([str(e), *getattr(e, "violations", [])]))
    return out


def _unwired(path: Path, idx) -> list[dict]:
    """아래 고리를 Link로 가리키지 않는 허브 — `move_nodes`의 `hub_links`와 같은 꼴이다. 허브
    없는 층에서 멈춘다. 그 자리는 검증기가 hubless로 알린다."""
    out, child = [], path
    d = path.parent.parent if graph.is_hub(path) else path.parent
    while len(d.relative_to(ROOT).parts) >= 2 and (hub := graph.hub_file(d)) is not None:
        try:
            linked = child in {live_locate(r, idx) for r in contract.parse(hub).wikilinks()}
        except (WriteError, OSError, ValueError):
            linked = False
        if not linked:
            out.append({"hub": hub.stem, "add": [child.stem]})
        child, d = hub, d.parent
    return out


def create_node_locked(title: str, summary: str, body: str, drafter: str,
                session: str | None = None, space: str | None = None,
                edges: dict | None = None, settle: str | None = None, *, _before_write=None,
                _identity=None, _legacy_raw=False, _idx=None) -> dict:
    """노드 생성. id·시각은 **서버 전속**이고 author는 `agent` 고정이다(D5).
    space가 없으면 세션 라우팅으로 착지를 정하고, 라우팅이 없으면 space를
    요구한 뒤 성공 시 그 scope로 세션을 확정한다."""
    # 이 쓰기가 쓰는 색인은 **하나**다 (v3.7.0). 구판은 한 번의 생성에서
    # 세 벌을 지었다 — 계약 검사·이름 유일성·검증·dangling이 각자 지었고,
    # 그래서 체감 비용이 단가의 3배였다. 잠금 안이라 그 사이에 파일이
    # 바뀌지 않으므로 한 벌이면 족하다. 자리 걸기(`_place`)도 같은 벌을 받는다.
    idx = graph.Index(VAULT) if _idx is None else _idx
    require_complete(idx)
    if settle is not None:
        evictions.require_evict(VAULT, settle)
    errs = (_check_edges(edges, idx) + title_errors(title)
            + ephemeral_session_errors(session) + routing_errors(VAULT, session))
    if errs:
        raise WriteError("계약 위반 — 쓰지 않았다", errs)

    # 기본 착지는 프로젝트의 주된 scope다. 결속에 없는 scope를 명시해도 거부하지 않는다 —
    # 한 프로젝트가 여러 scope의 활동을 품고, 성공한 쓰기가 그 scope를 결속에 더한다
    # (Mechanism §6-2 6항 · 시행령 §3 10항). Domain·Person 착지는 결속과 무관하다.
    bound = resolve_session(VAULT, session)
    dest = space or (f"{SCOPE}/{bound}" if bound else None)
    if not dest:
        bound_set = bindings(VAULT, session)["scopes"]
        raise WriteError(
            "착지가 정해지지 않았다 — space를 지정하라. "
            f"지금 쓸 수 있는 군집: {', '.join(cluster_names()) or '없음'}. "
            + ("이 세션은 일회성 작업 폴더라 결속하지 않는다 — 쓸 때마다 space를 준다"
               if workspaces.is_one_off(VAULT, session) else
               (f"이 프로젝트의 결속은 {', '.join(bound_set)}이지만 주된 scope가 없다 — "
                f"쓸 때마다 space를 준다. 주된 scope의 지정은 대화 검토나 사용자가 한다"
                if bound_set else
                "session(훅이 준 프로젝트 키)도 함께 주면 그 scope가 결속에 더해진다. "
                "주된 scope는 대화 검토의 소속 판정이나 사용자가 지정한다")))
    dest_dir = resolve_in_root(VAULT, dest)
    if dest_dir is None or not dest_dir.is_dir():
        # 신설 후보 — 막는 대신 한 번 묻는다. 통과하면 디렉토리가 생긴다.
        # (구판은 "신설은 사용자 발의다"라며 전면 거부했으나 그 문구는
        # 규범 무근거였다 — 헌법은 형성의 자동화를 기본으로 둔다.)
        dest_dir = _new_cluster_gate(dest, dest_dir, "이 쓰기가")
    path = dest_dir / f"{title}.md"
    kind = graph.space_of(VAULT, path)      # 소속은 노드 파일 경로로 판정한다
    _require_node_cluster(kind, dest)
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
    pkey = portable_name_key(title)
    gclash = next((s for s in idx.names if portable_name_key(s) == pkey),
                  None)
    if gclash is not None:
        raise WriteError(
            f"이식성 기준으로 같은 이름의 노드가 이미 있다: {gclash} — "
            f"대소문자나 유니코드 정규화만 다른 이름은 NTFS·APFS에서 같은 "
            f"경로가 된다. 한 군집으로 모이면 한쪽이 덮인다")
    clash = name_collision(dest_dir, title)
    if clash is not None:
        raise WriteError(
            f"같은 군집에 이미 있는 이름이다: {clash} — 대소문자나 유니코드 "
            f"정규화만 다른 이름은 NTFS·APFS에서 **같은 경로**가 되어 그 기기의 "
            f"체크아웃에서 충돌한다(한쪽만 남는다)")

    now = _identity["created"] if _identity else now_kst()
    meta = {"id": _identity["id"] if _identity else new_node_id(),
            "created": now, "updated": now,
            "author": "agent", "drafter": drafter, "summary": summary}
    for pred, tg in (edges or {}).items():
        meta[pred] = _as_links(pred, _merge_edges([], tg, idx), legacy_raw=_legacy_raw)
    data, errs = _validate_render(path, meta, body, idx)
    if errs:
        raise WriteError("계약·위상 위반 — 쓰지 않았다", errs)
    if settle is not None and not norm_body(contract.parse_bytes(path, data).body):
        raise WriteError("보존할 본문이 없다 — 쓰지도 처분하지도 않았다")

    _baseline(idx)
    if _before_write is not None:
        _before_write(path, data)
    _atomic_write(path, data)
    # 방금 쓴 노드를 손에 든 색인의 **이름 색인**에 등재한다 —
    # `_dangling_of`가 보는 것은 쓰기 **후**의 상태여야 한다. 구판은 여기서
    # 색인을 새로 지어 그 상태를 얻었다. 등재하지 않으면 자기 자신을
    # 가리키는 Link가 dangling으로 잘못 실려, 오타가 아닌 것을 오타라고
    # 알리게 된다. 판독은 `resolve`가 그때 한 번 한다(파일은 이미 있다).
    idx.register_new(path, kind)
    # 결속은 **scope일 때만** — Domain/Person에 결속하면 자동 라우팅이
    # 존재하지 않는 `Scope/<이름>`을 가리켜 그 키가 벽돌이 된다(7차 중대 C).
    # 주된 scope가 있어도 다른 scope의 쓰기는 집합에 더한다(시행령 §3 10항) —
    # 이미 결속에 있는 scope는 `bind_after_write`가 거른다.
    bound_now, receipt = None, {}
    if session and kind[0] == "scope":
        # 결속 값은 **scope 이름**이지 말단 디렉토리명이 아니다. 구판은
        # `dest_dir.name`을 썼고, 그래서 하위 군집(`Scope/W1/Sub`)에서
        # 처음 쓴 세션이 존재하지 않는 `Sub`에 묶였다 — 그 뒤 `append_raw`는
        # `Scope/Sub/_raw/`라는 유령 scope를 만들고, 노드 생성은 최상위
        # 신설 관문으로 갔다. 깊이는 갈래이지 소속이 아니며(Mechanism §1
        # 2항), 소속은 `space_of`가 이미 정확히 말해 준다.
        if bind_after_write(VAULT, receipt, session, kind[1]):
            bound_now = kind[1]         # 실제로 결속했을 때만 보고한다
    result = {"ok": True, "name": title,
            "path": posix_rel(path, ROOT), "id": meta["id"],
            "new_hash": sha256_bytes(data),
            "bound_scope": bound_now, **receipt,
            **_reference_feedback(path, meta, body, idx)}
    result.update(rechecks.after_write(VAULT, idx, path, meta))
    return evictions.after_node_write(VAULT, result, settle, "node", title)


def _baseline(idx) -> None:
    """재검토 기준선(`rechecks.ensure_baseline`). 적지 못해도 쓰기를 막지 않는다 —
    그 근거들이 후보로 남을 뿐이다."""
    try:
        rechecks.ensure_baseline(VAULT, idx)
    except Exception:
        pass


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
# 이미 파싱돼 있어 **무료**다. 노드를 쓰기 전에 불리므로 쓰는 세션은 본다. 검색만 하는 세션은 모른다(#200).
#
# 관문을 되살리고 싶어지면 조건은 하나다: **전수 판독이 이미 일어난 자리**에
# 두어야 한다. 쓰기 통로는 그런 자리가 아니다.


def edge_key(target: str, idx) -> tuple[str, str]:
    """노드는 해석된 제목과 절 앵커, 비노드 근거는 전체 경로와 앵커로 구별한다.
    id·제목·경로 어느 표기든 같은 노드의 같은 범위면 같은 키다. 해석되지 않는 이름만 표기
    규칙(`contract.target_stem`)으로 접는다 — 제목에 든 `.md`를 떼지 않기 위해서다."""
    s = raw.canonical_ref(VAULT, target).strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2]
    path, sep, anchor = s.split("|", 1)[0].strip().partition("#")
    path = path.strip().replace("\\", "/")
    if "/" in path or sep:
        kind = idx.resolve(path)[0]
        if kind == "external":
            # URL은 확장자까지 주소다 — `…/Makefile`과 `…/Makefile.md`는 다른 근거다.
            # `.md` 생략은 vault 안 경로의 표기 규칙이다(`resolve`가 `p`와 `p.md`를 본다).
            return path, anchor
        if kind != "node":
            return path.removesuffix(".md"), anchor
    hit = idx.locate(path) if path else None
    if hit and graph.is_node_home(hit[1]):
        return hit[0].stem, anchor.strip()
    return contract.target_stem(path), anchor.strip()


def _id_title(s: str, idx) -> str | None:
    """id 맨값이 가리키는 노드의 제목. 해석되지 않거나 모호하면 None."""
    if re.match(ID_RE, s) and s not in idx.dup_ids and s in idx.by_id:
        return idx.by_id[s][0].stem
    return None


def _titled(t, idx):
    """새로 받은 노드 대상의 id 맨값을 제목 위키링크로 적는다(Mechanism §8 2항).
    해석되지 않거나 모호한 id는 받은 그대로 둔다 — dangling·모호로 보고된다."""
    title = _id_title(str(t).strip(), idx)
    return f"[[{title}]]" if title else t


def _merge_edges(cur: list[str], add, idx) -> list[str]:
    """저장 목록 `cur` 뒤에 `add` 가운데 아직 없는 근거(`edge_key`)만 잇는다.

    엣지를 더하는 자리는 모두 이 한 벌을 지난다 — 생성, 갱신의 추가, 그리고 그 둘을
    부르는 증류. 구판은 갱신의 추가만 새 대상을 걸렀다. 그래서 생성은 받은 만큼
    적었다(distill의 `sources`와 `edges`가 같은 라운드를 줄 때). 같은 기록의
    **다른 라운드**는 앵커가 달라 다른 근거로 남는다.

    `cur`는 키로 줄이지 않는다. 키는 "이미 있는가"를 묻는 데만 쓴다. 저장 목록을
    키로 줄이면 해석 규칙이 달라진 자리마다 저장된 근거를 조용히
    지우게 된다. 저장 목록의 되풀이는 바이트가 같을 때만 접는다(`update_node_locked`)."""
    seen = {edge_key(t, idx) for t in cur}
    out = list(cur)
    for t in as_list(add):
        t = _titled(t, idx)
        k = edge_key(t, idx)
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _as_links(pred: str, targets, *, legacy_raw: bool = False) -> str | list:
    """입력 대상을 저장 표기로 접는다 — 맨값은 위키링크로 감싸고, 이미
    위키링크면 그대로 둔다. raw 좌표와 `derived-from`의 id는 감싸지 않는다.

    v3.7.3부터 노드 근거의 정본 표기는 **제목 위키링크**이므로, 제목을 맨값으로
    받으면 여기서 `[[제목]]`이 된다. id 맨값을 그대로 두는 것은 **구형 표기의
    호환**이다(§8 2항 — 계속 해석한다).

    이 함수는 id를 제목으로 바꾸지 않는다. `add_edges`·`remove_edges` 경로에서
    **이미 저장된 간선까지** 다시 접는 자리라, 바꾸면 호출자가 요청하지 않은
    간선이 바뀐다. 새로 받은 id는 `_merge_edges`가 `_titled`로 먼저 제목으로
    적는다."""
    out = []
    for t in as_list(targets):
        s = str(t).strip()
        if (not legacy_raw and pred == "derived-from" and graph.is_record_ref(s)
                and not re.match(r"^(?:\[\[\s*)?https?://", s)):
            out.append(raw.canonical_ref(VAULT, s))
        elif s.startswith("[[") or (pred == "derived-from" and re.match(ID_RE, s)):
            out.append(s)
        else:
            out.append(f"[[{s}]]")
    return out[0] if len(out) == 1 else out


def update_node(name: str, body: str | None = None,
                expect_hash: str | None = None, summary: str | None = None,
                add_edges: dict | None = None, remove_edges: dict | None = None,
                old_text: str | None = None, new_text: str | None = None,
                settle: str | None = None, rechecked: list | None = None, *,
                _seen: dict | None = None) -> dict:
    """Apply an ordinary node update under the shared mutation lock.

    `rechecked` names the `derived-from` targets whose recheck this call completes.
    `_seen` is the surface's record of the bodies its caller read (node id →
    `rechecks.state`); a recorded read must still match for a recheck to close
    (Mechanism §4-1)."""
    with mutation_lock(VAULT):
        return update_node_locked(name, body, expect_hash, summary, add_edges,
                                   remove_edges, old_text, new_text, settle, rechecked,
                                   _seen=_seen)


def update_node_locked(name: str, body: str | None = None,
                expect_hash: str | None = None, summary: str | None = None,
                add_edges: dict | None = None,
                remove_edges: dict | None = None,
                old_text: str | None = None,
                new_text: str | None = None, settle: str | None = None,
                rechecked: list | None = None, *, _before_write=None,
                _stamp=None, _legacy_raw=False, _seen=None, _idx=None) -> dict:
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
        # (`norm_body`), CRLF 앵커를 그대로 대조하면 호출자가 방금 넣은 문장도
        # 못 찾는다. 접는 자리를 한 벌로 두어 대조와 치환이 갈리지 않게 한다.
        old_text = norm_newlines(old_text)
        new_text = norm_newlines(new_text or "")
        if not old_text:
            raise WriteError(
                "`old_text`가 비어 있다 — 쓰지 않았다",
                ["빈 앵커는 본문의 모든 자리에 맞으므로 고칠 자리를 가리키지 "
                 "못한다. 바꿀 대목을 그대로 넣어라."])
        if old_text == new_text:
            raise WriteError(
                "`old_text`와 `new_text`가 같다 — 쓰지 않았다",
                ["바뀌는 것이 없다. 고칠 내용을 `new_text`에 담아라."])
    # 이 쓰기가 쓰는 색인은 하나다. 같은 잠금 안의 허브 배선은 앞 쓰기의 색인을 넘긴다.
    idx = graph.Index(VAULT) if _idx is None else _idx
    require_complete(idx)
    if settle is not None:
        evictions.require_evict(VAULT, settle)
    errs = _check_edges(add_edges, idx) + _check_edges(remove_edges, idx, adding=False)
    if errs:
        raise WriteError("계약 위반 — 쓰지 않았다", errs)
    path = live_locate(name, idx)
    if path is None or not path.is_file():
        raise WriteError(f"노드 없음: {name}")
    kind = graph.space_of(VAULT, path)
    _reject_governance(kind)
    if settle is not None:
        evictions.require_target(path.stem, idx)
    try:
        n = contract.parse(path)
    except Exception as e:
        raise WriteError(f"파손된 노드다 — 수동 확인이 먼저다: {name} ({e})")

    _cas(path, expect_hash, body is not None)   # 위반 시 raise
    # 근거 재검토(Mechanism §4-1) — 쓰기 전의 쌍과 완료 상태, 이 호출이 `rechecked`에 적은 근거.
    # 잴 수 있는 쌍이 아닌 항목은 쓰기를 막지 않고 응답에 비춘다(헌법 1조 3항).
    _baseline(idx)
    rc_before = rechecks.pairs(idx, n.meta)
    rc_pre = rechecks.state(path.read_bytes())  # 검토자가 읽었어야 할 노드의 본문
    rc_prior = rechecks.complete_keys(VAULT, idx, path, n.meta) if rc_before else set()
    rc_again, rc_stray = set(), []
    for ref in as_list(rechecked):
        t = rechecks.target(str(ref), idx)
        if t and t[0] in rc_before and t[1] is not None:
            rc_again.add(t[0])
        else:
            rc_stray.append(str(ref))
    rc_note = {"recheck_unmatched": {"targets": rc_stray, "why": (
        "이 노드의 derived-from에 상태를 잴 수 있는 근거로 없다 — 닫을 쌍이 없다")}} if rc_stray else {}
    if old_text is not None:
        # **유일성이 안전 계약의 전부다.** 여러 곳에 맞으면 어디를 고칠지
        # 호출자가 정한 바가 없고, 아무 곳이나 고르는 것은 조용히 틀린
        # 자리를 고치는 길이다. 하네스 `Edit` 실측에서 유일성 위반은
        # 5,454건 중 5건(0.09%)이라 이 계약은 실무에서 거의 공짜다.
        hits, windows = anchor_windows(n.body, old_text)
        if hits == 0 and (spaced := space_numeric_tags(old_text)) != old_text:
            # 저장본은 태그 방어로 `#숫자` 뒤를 띄웠다(`norm_body`). 호출자가 방금 쓴 글 그대로의
            # 앵커도 같은 변환으로 맞춘다(#292). 원문부터 보는 것은 조각이 코드 구획 한가운데서
            # 시작하면 변환의 구획 판정이 틀릴 수 있어서다.
            old_text = spaced
            hits, windows = anchor_windows(n.body, spaced)
        if hits != 1:
            raise WriteError(
                ("앵커가 본문에 없다 — 쓰지 않았다" if hits == 0 else
                 f"앵커가 본문에 {hits}곳 맞는다 — 쓰지 않았다"),
                ([f"`old_text`가 `{name}`의 본문에 나오지 않는다. 노드가 "
                  f"그 사이 바뀌었을 수 있다 — `read_node`로 지금 본문을 "
                  f"보고 그대로 복사해 넣어라. 공백·줄바꿈까지 일치해야 한다. "
                  f"큰 노드는 `read_node(view=고칠 문장)`이 그 자리를 앞뒤 문단째 준다."]
                 if hits == 0 else
                 [f"어느 자리를 고칠지 정해지지 않는다. 앞뒤 줄을 함께 "
                  f"넣어 앵커를 **유일하게** 만들어라. 맞는 자리의 앞뒤 문단은 "
                  f"`views`를 `read_node(view=…)`로 읽는다."]),
                **({"views": [f"{a}:{b}" for a, b in windows]} if hits else {}))
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
    #
    # 제거가 먼저다. id와 제목은 같은 근거이므로(`edge_key`), 저장된 id 표기를
    # 빼고 제목으로 다시 대는 호출이 뒤의 제거에 지워지지 않는다. 같은 근거를
    # 함께 빼고 대면 남는다 — 근거를 조용히 잃지 않는다.
    for pred, tg in (remove_edges or {}).items():
        drop = {edge_key(t, idx) for t in as_list(tg)}
        cur = stored_edges(meta.get(pred))
        keep = [t for t in cur if edge_key(t, idx) not in drop]
        if len(keep) != len(cur):
            changed = True
            if keep:
                meta[pred] = _as_links(pred, keep, legacy_raw=_legacy_raw)
            else:
                meta.pop(pred, None)
    for pred, tg in (add_edges or {}).items():
        cur = stored_edges(meta.get(pred))            # 저장 표기 그대로
        merged = _merge_edges(cur, tg, idx)  # 한 호출 안의 중복도 한 번만 앉는다
        if len(merged) != len(cur):
            meta[pred] = _as_links(pred, merged, legacy_raw=_legacy_raw)
            changed = True
    # 저장 목록에서 바이트가 같은 되풀이는 이 쓰기에서 접는다 — 위의 추가·제거가 옛
    # 표기를 정규 표기로 다시 적어 겹친 것까지. 접기만으로는 쓰지 않는다(`changed` 불변).
    # ponytail: 바이트 비교만 한다(해소가 없어 공짜이고 실패하지 않는다). 키로 접으면
    # raw 근거마다 해소 비용(Windows 실측 ~7 ms)을 내고, 키가 거친 자리에서 서로 다른
    # 근거를 지운다.
    for pred in contract.PREDICATES:
        cur = stored_edges(meta.get(pred))
        if len(set(cur)) != len(cur):
            meta[pred] = list(dict.fromkeys(cur))
    new_body = n.body if body is None else body
    if body is not None and norm_body(body) != norm_body(n.body):
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
    if not changed and settle is None:
        # 변경이 없으면 쓰지 않는다 — 내용이 그대로인데 updated만
        # 갱신하면 "상태가 변경될 때 갱신한다"(시행령 §1 4항)에 어긋난다
        res = {"ok": True, "no_change": True, "name": name,
               "path": posix_rel(path, ROOT), "id": n.id,
               "new_hash": sha256_file(path),
               "edges": _edge_report(n),
               **_reference_feedback(path, n.meta, n.body, idx, n), **rc_note}
        if rc_again:     # 바꿀 것 없이 점검을 적었다 — 점검 완료(unchanged)
            res.update(rechecks.after_write(VAULT, idx, path, n.meta, before=rc_before.keys(),
                                            prior=rc_prior, rechecked=rc_again,
                                            changed=False, pre=rc_pre, seen=_seen))
        return res
    if not only_conflicts:
        meta["updated"] = _stamp or now_kst()

    data, errs = _validate_render(path, meta, new_body, idx)
    if errs:
        raise WriteError("계약·위상 위반 — 쓰지 않았다", errs)
    if settle is not None:
        saved_body = norm_body(contract.parse_bytes(path, data).body)
        if not saved_body or saved_body == norm_body(n.body):
            raise WriteError("보존할 본문이 없거나 변경되지 않았다 — 쓰지도 처분하지도 않았다", [
                "settle은 이 호출이 보존한 evict의 rid다. 이미 노드에 보존된 내용이면 "
                "본문을 확인한 뒤 CLI tidy settle로 기록하라."])
    if _before_write is not None:
        _before_write(path, data)
    _atomic_write(path, data)
    out = {"ok": True, "name": name, "path": posix_rel(path, ROOT),
           "id": n.id, "new_hash": sha256_bytes(data),
           "updated_kept": only_conflicts,
           "edges": _edge_report(contract.Node(path=path, meta=meta, body=new_body)),
           **_reference_feedback(path, meta, new_body, idx, n), **rc_note}
    out.update(rechecks.after_write(VAULT, idx, path, meta, before=rc_before.keys(),
                                    prior=rc_prior, rechecked=rc_again,
                                    pre=rc_pre, seen=_seen))
    # 쓰기 직전 본문을 읽었던 호출자는 방금 쓴 본문도 안다 — 쓰기 응답의 해시를 다음
    # `expect_hash`로 잇는 것과 같은 규율이다. 읽지 않았으면 잇지 않는다.
    if _seen is not None and _seen.get(n.id) == rc_pre:
        _seen[n.id] = rechecks.state(data)
    if old_text is None and body is not None:
        # 전문 치환은 긴 글을 다시 쓰다 흘릴 수 있다. 무엇이 빠졌는지 비추면 선의의 에이전트는
        # 스스로 되살린다(헌법 1조 3항). 앵커 편집은 지울 글을 호출자가 `old_text`에 직접
        # 적었으므로 비출 것이 없다.
        dropped = _dropped(n.body, norm_body(new_body))
        if dropped:
            out["dropped"] = dropped
    if replaced_summary is not None:
        out["replaced_summary"] = replaced_summary
    return evictions.after_node_write(VAULT, out, settle, "merged", path.stem)


def _dropped(old_body: str, new_body: str) -> list[str]:
    """옛 본문의 단위(문단·목록 항목·제목) 가운데 새 본문에 글자 그대로 없는 것.
    고쳐 쓴 단위도 옛 판이 나온다 — 고친 것인지 흘린 것인지는 읽는 쪽이 가른다.
    많으면 앞의 10개를 300자까지만 싣고 나머지는 개수로 알린다."""
    from . import distillation      # distillation이 write를 부르므로 쓸 때 읽는다
    gone = [t for t in distillation.segment_texts(old_body) if t not in new_body]
    shown = [t if len(t) <= 300 else t[:299] + "…" for t in gone[:10]]
    return shown + ([f"… 외 {len(gone) - 10}개"] if len(gone) > 10 else [])


def _plan_move(name: str, dest_dir: Path, dest_space: str, idx):
    """이동 하나의 **검사만** 수행하고 (원본, 목표, 노드)를 낸다 — 아무것도
    쓰지 않는다.

    `move_nodes`가 전부-아니면-전무이려면 **검사를 전부 먼저** 돌려야 한다.
    검사와 실행이 한 덩이면 절반 옮긴 상태가 남고, 그 상태는 허브 배선이
    어중간해 사람이 무엇을 되돌릴지 알기 어렵다."""
    path = live_locate(name, idx)
    if path is None or not path.is_file():
        # 결과를 함께 말한다 — "전부 아니면 전무"를 문안으로만 약속하면 받는
        # 쪽이 못 믿고 확인 질의를 한 번 더 한다(표면 감사 실측).
        raise WriteError(f"노드 없음: {name} — 아무것도 옮기지 않았다")
    _reject_governance(graph.space_of(VAULT, path))
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
    dst_kind = graph.space_of(VAULT, target)   # 소속은 노드 파일 경로로 판정한다
    _require_node_cluster(dst_kind, dest_space)
    clash = name_collision(dest_dir, path.stem)
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
    if _pinned(posix_rel(path.parent, ROOT) + "/",
               posix_rel(dest_dir, ROOT) + "/", n.id):
        raise WriteError(
            "pin으로 고정됐거나 pin 판정이 미확정이다 — 자동 재배정에서 제외된다 "
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
    approvals.record_move(VAULT, n.id, path, target)
    os.replace(path, target)
    # 손에 든 색인은 이동 **전** 경로를 쥐고 있다. 갱신하지 않으면 아래
    # `_dangling_of`가 옛 경로를 열려다 실패해 이 노드의 자기링크를
    # dangling으로 오보한다 — `create_node`가 `register_new`로 막는 것과
    # 같은 실패이고, 오타가 아닌 것을 오타라고 알리게 된다.
    idx.retarget(path, target, graph.space_of(VAULT, target))


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
        hub = graph.hub_file(h)
        if hub is None:
            continue
        try:
            n = idx.node(hub)
        except Exception:
            continue
        gone = sorted({contract.target_stem(t) for t in n.wikilinks()} & moved)
        if gone:
            out.append({"hub": hub.stem, "remove": gone})
    dhub = graph.hub_file(dest)
    if dhub:
        try:
            have = {contract.target_stem(t) for t in idx.node(dhub).wikilinks()}
            need = sorted(moved - have)
        except Exception:
            need = sorted(moved)
        if need:
            out.append({"hub": dhub.stem, "add": need})
    return out


def _move_topology(plans, stale, idx) -> list[str]:
    """이동 **후**의 참조 위상 중 이 이동이 **새로 만드는** 위반.

    소속은 노드 파일의 경로로 정해지므로(`space_of`) 옮긴 노드에서 나가는
    참조와 그 노드로 들어오는 참조의 판정이 함께 바뀐다. 구판은 이것을 보지
    않고 `ok`를 냈다 — 검증기는 FAIL(scope 간 Link·derived-from, Domain의
    `_raw` 근거)이었고 그 노드들의 무관한 다음 쓰기가 거부됐다. 같은 간선을
    `create_node`·`update_node`는 처음부터 거부한다(2026-09-24 재현).

    이미 있던 위반은 이 이동의 몫이 아니므로 세지 않는다(설계 D10 — 남이 만든
    위반 때문에 내 쓰기가 막히면 안 된다). 출발지 허브의 Link는 `hub_links`의
    `remove`가 닫으므로 뺀다. 들어오는 참조의 후보는 바이트에서 고르고
    (`mentioning`), 판정은 판독한 노드로 한다 — 전수 판독을 부르지 않는다."""
    after = {src: graph.space_of(VAULT, dst) for src, dst, _n in plans}
    moved = {src: n for src, _dst, n in plans if after[src] != graph.space_of(VAULT, src)}
    if not moved:
        return []                     # 소속이 그대로면 판정도 그대로다
    new_kind = {}
    for src, n in moved.items():
        new_kind[src.stem] = after[src]
        if n.id:
            new_kind[n.id] = after[src]

    gone = {Path(os.path.realpath(src)) for src in after}

    class _After:                     # `_topology_of`가 색인에 묻는 것은 해석뿐이다
        @staticmethod
        def resolve(name):
            r = idx.resolve(name)
            if r[0] != "node":
                return r
            if "/" in name:
                # 경로형은 옛 자리를 가리킨다 — 이동 뒤에는 끊긴 참조(경고)다.
                # 옛 소속으로 판정하면 함께 옮기는 두 노드 사이의 경로형 Link가
                # scope 간 위반으로 오판돼 이동이 거부됐다.
                p = resolve_in_root(VAULT, name)
                return ("dangling",) if p is not None and {p, p.with_suffix(".md")} & gone else r
            return ("node", new_kind[name]) if name in new_kind else r

    hub_keep = {h["hub"]: set(h["remove"]) for h in stale if "remove" in h}
    out = []
    for p in dict.fromkeys([*moved, *idx.mentioning(new_kind)]):
        if not idx._readable(p):
            continue                  # 판독 불가 파일은 참조를 말하지 않는다
        n = idx.parsed[p]
        k0 = graph.space_of(VAULT, p)
        k1 = after.get(p, k0)
        refs = [(t, pred) for pred in contract.PREDICATES for t in n.edges(pred)]
        refs += [(t, None) for t in n.wikilinks()]
        refs += [(t, rechecks.EVIDENCE) for t in evidence.coordinates(n.body)]
        for t, pred in refs:
            if pred is None and contract.target_stem(t) in hub_keep.get(p.stem, ()):
                continue
            errs = _topology_of(_After, k1, p.stem, t, pred, n.id)
            if errs and not _topology_of(idx, k0, p.stem, t, pred, n.id):
                # 쓰기용 꼬리("옮기는 중이면 먼저 옮기고…")는 여기서 거꾸로 읽힌다
                out += [e.split(" — ", 1)[0] for e in errs]
    return list(dict.fromkeys(out))   # Link와 derived-from이 같은 줄을 낸다


def move_node(name: str, dest_space: str) -> dict:
    """군집 재배정. 이동은 바이트 불변이라 CAS가 없다(경로는 상태, 동일성은
    id). pin된 군집은 출발·도착 어느 쪽이든 거부한다(시행령 §3 4항)."""
    r = move_nodes([name], dest_space)
    if not r["ok"]:
        return r
    one = r["moved"][0]
    return {"ok": True, "name": one["name"], "id": one["id"],
            "path": one["path"], "new_hash": one["new_hash"],
            "moved_from": one["moved_from"], "dangling": one["dangling"],
            "hub_links": r["hub_links"]}


def move_nodes(names: list[str], dest_space: str) -> dict:
    """노드 여럿을 한 군집으로 옮긴다 — 검사 실패는 전부 거부, I/O 중단은 이어서 복구.

    분화(시행령 §3 7항)의 실제 모양이 이것이다: 한 갈래를 통째로 하위 군집에
    내린다. 하나씩 부르면 잠금과 색인을 N번 짓고(v3.7.0이 세운 "쓰기 1회 =
    색인 1회"의 반대), 중간에 하나가 거부되면 절반만 내려간 상태가 남는다.

    검사를 **전부 먼저** 돌리고 하나라도 걸리면 아무것도 쓰지 않는다. 그 뒤의
    실행은 개별 `os.replace`라 원자적이지만 묶음은 아니다 — 실행 중 OSError는
    이미 옮긴 것을 되돌리지 않고 그대로 보고한다(자료는 어느 쪽에도 남아 있고,
    이동은 바이트 불변이라 되돌리기가 다시 옮기는 일이다)."""
    if not names:
        raise WriteError("옮길 노드가 없다")
    with mutation_lock(VAULT):
        idx = graph.Index(VAULT)                     # 이 이동이 쓰는 색인은 하나다
        require_complete(idx)
        dest_dir = resolve_in_root(VAULT, dest_space)
        if dest_dir is None or not dest_dir.is_dir():
            dest_dir = _new_cluster_gate(dest_space, dest_dir, "이 이동이")
        # 중복 판정은 **해석된 경로**로 한다. 이름 문자열로만 보면 같은 노드를
        # 제목과 id로 두 번 줬을 때 통과해, 첫 rename 뒤 둘째가 부재로 죽는다 —
        # 이미 옮긴 것은 남으므로 "전부 아니면 전무"가 깨진다.
        #
        # 그리고 **이식성 키**로도 본다. 대소문자·정규화만 다른 두 노드를 한
        # 군집으로 옮기면 목적지 검사(`_plan_move`의 `name_collision`)는 아직
        # 그 파일이 없으니 통과시키고, `os.replace`가 둘째로 첫째를 덮는다 —
        # 노드 하나가 소실되는데 응답은 `ok:true`였다(실측 재현).
        seen, seen_keys, plans = set(), {}, []
        for name in names:
            plan = _plan_move(name, dest_dir, dest_space, idx)
            src = plan[0]
            if src in seen:
                raise WriteError(f"같은 노드를 두 번 옮길 수 없다: {name}")
            seen.add(src)
            key = portable_name_key(src.stem)
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
        # 최상위 군집을 건너는 것 자체는 막지 않는다 — 노드 하나를 다른 scope로
        # 재배정하는 것은 정당한 행위다. 막는 것은 이동이 **새로 만드는** 참조
        # 위반이다(`_move_topology`). 출발지 허브의 Link만은 예외로 두고
        # `hub_links`의 `remove`로 닫는다 — 이동 전에 지우면 허브 검사가 고아를
        # 부르고, 이동 뒤 한 번의 편집으로 닫히는 것이 알려진 순서다.
        #
        # 건넜다는 사실은 침묵하지 않는다. 표면 감사에서 감사자가 이 이동을 하고
        # `ok:true`를 받은 뒤 검증기 FAIL로 알게 됐다.
        bad = _move_topology(plans, stale, idx)
        if bad:
            raise WriteError(
                "옮기면 참조 위상이 깨진다 — 아무것도 옮기지 않았다", bad + [
                    "소속은 경로가 정하므로(헌법 8조 3항) 이 참조들은 이동 뒤 "
                    "위반이 되고, 그 노드들의 무관한 다음 쓰기까지 거부된다. "
                    "옮기기 전에 걷어내라(`update_node`의 `remove_edges`·"
                    "`old_text`). 함께 가야 할 노드면 같은 `names`에 넣는다. "
                    "scope를 넘어 공유할 지식이면 domain 노드로 증류해 경유한다"])
        dtop = dest_dir.relative_to(ROOT).parts[:2]
        crossed = sorted(p.stem for p, _t, _n in plans
                         if p.relative_to(ROOT).parts[:2] != dtop)
        out = []
        from . import organization
        move_key = organization.record_move(plans, stale, idx)
        for path, target, n in plans:
            before = sha256_file(path)
            try:
                _apply_move(path, target, n, idx)
            except (OSError, WriteError) as exc:
                return {"ok": False, "moved": out, "count": len(out),
                        "dest": posix_rel(dest_dir, ROOT), "hub_links": stale,
                        "move_key": move_key, "reason": str(exc),
                        "remaining": [q.stem for q, t, _n in plans if q.is_file()],
                        "instruction": "일부 이동 뒤 중단됐다. ID·현재 위치·hash를 읽고 remaining만 재개하라. "
                                       "허브 배선까지 organization 검토의 미결로 유지한다."}
            out.append({"name": path.stem, "id": n.id,
                        "path": posix_rel(target, ROOT), "new_hash": before,
                        "moved_from": posix_rel(path, ROOT),
                        "dangling": _dangling_of(target, n.meta, n.body, idx)})
            organization.checkpoint_moves(idx)
        r = {"ok": True, "moved": out, "count": len(out),
             "dest": posix_rel(dest_dir, ROOT), "hub_links": stale, "move_key": move_key}
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
    with mutation_lock(VAULT):
        idx = graph.Index(VAULT)
        require_complete(idx)
        src = live_locate(name, idx)
        if src is None or not src.is_file():
            raise WriteError(f"노드 없음: {name}")
        if not graph.is_hub(src):
            raise WriteError(
                f"군집이 아니다: {name} — 군집의 허브(폴더와 동명인 노드)만 "
                f"군집째 옮길 수 있다. 노드 하나를 옮기려면 `move_nodes`를 쓴다")
        sdir = src.parent
        _reject_governance(graph.space_of(VAULT, src))
        ddir = resolve_in_root(VAULT, dest_parent)
        if ddir is None or not ddir.is_dir():
            raise WriteError(
                f"목적지 군집이 없다: {dest_parent} — 재편은 이미 있는 군집 "
                f"안으로만 한다(먼저 그 군집을 만든다)")
        _reject_governance(graph.space_of(VAULT, ddir / "x.md"))
        _require_node_cluster(graph.space_of(VAULT, ddir / sdir.name / "x.md"),
                              dest_parent)
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
        if not graph.hub_file(ddir)\
                and ddir.parent.resolve() not in {
                    (ROOT / s).resolve() for s in graph.node_spaces(VAULT)}:
            raise WriteError(
                f"허브 없는 군집 안으로는 옮길 수 없다: {dest_parent}")
        target = ddir / sdir.name
        if target.exists():
            raise WriteError(
                f"목적지에 같은 이름의 군집이 이미 있다: {posix_rel(target, ROOT)} "
                f"— 하위 군집 이름은 전역에서 유일하다")
        inside = [(p, k) for p, k in idx.nodes.values() if sdir in p.parents]
        if _pinned(posix_rel(sdir, ROOT), posix_rel(ddir, ROOT),
                   *(idx.node(p).id for p, _k in inside),
                   subtree=posix_rel(sdir, ROOT)):
            raise WriteError(
                "pin으로 고정됐거나 pin 판정이 미확정인 군집·노드가 있다 "
                "— 사용자 발의로만 옮긴다 (시행령 §3 4항)")
        for p, _k in inside:
            approvals.record_move(VAULT, idx.node(p).id, p,
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
    with mutation_lock(VAULT):
        if type not in CANDIDATE_TYPES:
            raise WriteError(f"미정의 충돌 유형: {type} (Mechanism §4 3항)")
        idx = graph.Index(VAULT)
        require_complete(idx)
        parties = []
        for nm in nodes:
            # `live_locate`를 쓴다 — 동명·동 id면 **고르지 않고 거부**한다
            # (Mechanism §2 1항). 구판은 `idx.nodes.get`으로 뒤에 오는 쪽을
            # 조용히 골라, 쓰기 통로가 거부하는 모호성을 이 통로가 사건부에
            # 박제했다. id 핸들도 여기서 함께 해석된다.
            p = live_locate(nm, idx)
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
