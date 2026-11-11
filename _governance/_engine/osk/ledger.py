"""osk.ledger — 대장: 덧붙이기만 하는 기록과 그 인과 DAG (Mechanism §3).

파일이 정본이고 엔진 상태는 전부 파일에서 재계산한다(시행령 §11 1항).

대장의 인과 모델:
- 각 기록은 `parents`로 하나 이상의 직전 head를 참조한다. 평소에는 1개,
  다기기 병합 직후의 첫 기록은 모든 head를 참조해 분기를 봉합한다(병합 앵커).
- `parents` 도입 전의 유산 기록은 파일 순서를 인과 순서로 간주한다(단일 기기
  이력). 도입 사건(첫 parents 보유 기록 = 앵커)부터 명시 인과가 정본이며,
  **앵커 이후에는 파일 순서 추정을 쓰지 않는다** — parents 없는 기록은 고립
  루트로 강등한다(구 엔진이 다른 클론에서 쓴 행이 병합돼 들어와도 가짜 인과를
  얻지 못한다). 고립 루트도 head이므로 사용자의 새 봉합 기록이 해소한다.
- rid는 잠금 안에서 대장의 정본상 최대 rid로부터 단조 생성한다 — 물리적
  마지막 행이 아니라 최대값이 바닥이다.
- 판정은 rid 정렬이 아니라 인과 극대(causal maxima)로 한다. 같은 노드의
  극대 기록이 유일하지 않으면(비교 불능 분기 또는 순환) 보수적으로
  미확정이다. 이후 모든 head를 조상으로 갖는 새 기록(사용자의 봉합)이
  유일 극대가 되어 해소한다.

손상과 해소 가능성 (fail-closed의 두 갈래):
- **정규화로 해소 가능한 이상** — 자기 참조·미지 rid 참조·전방 참조(파일
  순서상 뒤를 가리키는 parents)는 간선을 잘라 고립 루트로 강등한다. 순환이
  원천 차단되고(항상 DAG), 새 봉합 기록으로 해소되는 길이 남는다.
- **구조 손상** — rid 부재·rid 형식 위반·rid 중복은 기록의 동일성 자체가
  깨진 상태다. 판정은 fail-closed(미확정)로 두되 `ledger_damage`가 이를
  표면화하고, 회복은 Mechanism §3 8항의 수동 복구가 담당한다. 이 상태에서는
  새 기록의 append도 거부한다(손상 위에 이력을 더 쌓지 않는다)."""
from __future__ import annotations
import json, os, random, re, time
from pathlib import Path
from . import core, durable
from ._portalock import lock_exclusive, unlock


# ── rid — 시각 48비트 + 시퀀스 12비트 (생성 단조 표식) ────────────────────

def rid_parts(rid: str) -> tuple[int, int]:
    h = rid.replace("-", "")
    return int(h[0:12], 16), int(h[12:16], 16) & 0x0FFF


def rid_key(rid: str) -> tuple[int, int, str]:
    ms, seq = rid_parts(rid)
    return (ms, seq, rid)


def _make_rid(ms: int, seq: int) -> str:
    rb = random.getrandbits(62)
    b = ms.to_bytes(6, "big") + ((0x7 << 12 | seq).to_bytes(2, "big")) \
        + ((0b10 << 62 | rb).to_bytes(8, "big"))
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _next_rid(max_rid: str | None) -> str:
    """대장의 정본상 최대 rid보다 반드시 큰 rid — 병합으로 물리 순서가
    섞여 있어도 최대값이 바닥이다."""
    now_ms = int(time.time() * 1000)
    if max_rid:
        last_ms, last_seq = rid_parts(max_rid)
        if now_ms > last_ms:
            return _make_rid(now_ms, 0)
        seq = last_seq + 1
        if seq > 0xFFF:
            return _make_rid(last_ms + 1, 0)
        return _make_rid(last_ms, seq)
    return _make_rid(now_ms, 0)


# ── 대장 읽기·손상 진단 ──────────────────────────────────────────────────

_LINE_SEP_ESC = str.maketrans({"\x85": r"\u0085", "\u2028": r"\u2028", "\u2029": r"\u2029"})


def _parse_lines(text: str, path: Path) -> list[dict]:
    # 행 경계는 "\n"뿐이다 — `splitlines()`는 U+2028·U+2029·U+0085에서도 끊는데,
    # 기록은 `ensure_ascii=False`라 그 문자가 필드 값에 날것으로 선다.
    out = []
    for i, line in enumerate(text.split("\n")):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"대장 손상(부분 행) {path}:{i+1} — 수동 복구 절차 필요") from e
        if not isinstance(rec, dict):
            raise ValueError(
                f"대장 손상(기록이 아닌 행) {path}:{i+1} — 수동 복구 절차 필요")
        out.append(rec)
    return out


def ledger_read(path: Path) -> list[dict]:
    """파일 순서 그대로 반환한다. 판정의 정본은 인과(parents)이며,
    유산 구간(앵커 이전)은 파일 순서가 인과다."""
    if not path.exists():
        return []
    return _parse_lines(path.read_text(encoding="utf-8"), path)


def ledger_damage(records: list[dict], path: Path | str = "") -> list[str]:
    """기록의 **동일성**이 깨진 구조 손상 목록 — rid 부재·형식 위반·중복.
    정규화로 흡수하면 안 되는(해소를 새 기록에 맡길 수 없는) 이상이며,
    Mechanism §3 8항의 수동 복구 대상이다. 빈 목록이면 건전."""
    out, seen = [], {}
    where = f"{path}:" if path else "행"
    for i, r in enumerate(records):
        rid = r.get("rid")
        if rid is None:
            out.append(f"{where}{i+1} rid 부재 (node={r.get('node')})")
            continue
        if not re.match(core.RID_RE, str(rid)):
            out.append(f"{where}{i+1} rid 형식 위반: {rid}")
            continue
        if rid in seen:
            out.append(f"{where}{i+1} rid 중복: {rid} (앞선 행 {seen[rid]+1})")
        seen[rid] = i
    return out


def damaged_nodes(records: list[dict]) -> set[str]:
    """구조 손상에 연루된 노드 — 판정은 fail-closed(미확정)."""
    bad, seen = set(), {}
    for i, r in enumerate(records):
        rid, node = r.get("rid"), r.get("node")
        if rid is None or not re.match(core.RID_RE, str(rid)):
            if node:
                bad.add(node)
            continue
        if rid in seen:
            for j in (seen[rid], i):
                if records[j].get("node"):
                    bad.add(records[j]["node"])
        seen[rid] = i
    return bad


# ── 인과 DAG ─────────────────────────────────────────────────────────────

def effective_parents(records: list[dict]) -> dict[str, list[str]]:
    """rid → 부모 rid 목록. **항상 비순환**으로 정규화한다.

    - 앵커(첫 parents 보유 기록) 이전의 유산 기록만 파일 순서를 인과로
      간주한다. 앵커 이후의 parents 부재 기록은 고립 루트다.
    - parents 원소 중 자기 자신·미지 rid·파일 순서상 뒤(전방 참조)는
      잘라낸다 — 손상이 순환을 만들어 기록을 판정에서 소거하는 것을 막고,
      잘린 기록은 head로 남아 새 기록으로 봉합된다.
    - 구조 손상 기록(rid 부재·형식 위반)은 DAG에 넣지 않는다. 그 노드는
      damaged_nodes가 fail-closed로 잡는다.
    """
    anchor = ledger_anchor_index(records)
    order = {}
    for i, r in enumerate(records):
        rid = r.get("rid")
        if rid is not None and re.match(core.RID_RE, str(rid)) and rid not in order:
            order[rid] = i
    out: dict[str, list[str]] = {}
    prev_rid = None
    for i, r in enumerate(records):
        rid = r.get("rid")
        if rid is None or not re.match(core.RID_RE, str(rid)):
            continue
        if isinstance(r.get("parents"), list):
            out[rid] = [p for p in r["parents"]
                        if isinstance(p, str) and p and p != rid
                        and p in order and order[p] < i]
        elif anchor is None or i < anchor:
            out[rid] = [prev_rid] if prev_rid else []   # 유산 구간만 파일 순서
        else:
            out[rid] = []                               # 앵커 이후 = 고립 루트
        prev_rid = rid
    return out


def heads(records: list[dict]) -> list[str]:
    """어느 기록의 부모도 아닌 rid 목록 — 병합·손상 직후에는 둘 이상이다."""
    par = effective_parents(records)
    referenced = {p for ps in par.values() for p in ps}
    return [rid for rid in par if rid not in referenced]


def ancestors(rid: str, par: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(par.get(rid, []))
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(par.get(x, []))
    return seen


def causal_maxima(records: list[dict], value: str,
                  par: dict[str, list[str]] | None = None,
                  field: str = "node",
                  candidate=None) -> list[dict]:
    """같은 키를 가진 기록 중 인과 극대(다른 기록의 조상이 아닌 것). 유일하면
    그것이 판정 기록, 그 밖에는 비교 불능 — fail-closed.

    `field`로 키를 고른다: 서명 기록부는 `node`, 라우팅 대장은 `session`.

    `candidate(r)`를 주면 그 술어를 만족하는 기록만 **후보**로 삼되, 조상 관계는
    여전히 **전체 기록의 DAG**로 계산한다. 후보를 목록에서 미리 빼고 부르면 그
    기록이 맡고 있던 parents 간선이 함께 끊겨(effective_parents가 미지 parent를
    자른다) 남은 기록들이 서로 비교 불능인 거짓 분기가 된다 — 걸러야 할 것은
    후보 자격이지 인과 사슬이 아니다."""
    par = effective_parents(records) if par is None else par
    mine = [r for r in records if r.get(field) == value and r.get("rid") in par]
    if candidate is not None:
        mine = [r for r in mine if candidate(r)]
    rids = {r["rid"] for r in mine}
    anc = {r["rid"]: ancestors(r["rid"], par) for r in mine}
    return [r for r in mine
            if not any(r["rid"] in anc[o] for o in rids if o != r["rid"])]


def unresolved_nodes(records: list[dict], field: str = "node") -> set[str]:
    """판정이 성립하지 않는 키 — 인과 극대가 유일하지 않거나(분기·순환 잔재)
    구조 손상에 연루된 것. 판정은 보수적으로 미확정."""
    par = effective_parents(records)
    keys = {r.get(field) for r in records if r.get(field)}
    out = set(damaged_nodes(records)) if field == "node" else set()
    for n in keys:
        if n in out:
            continue
        if len(causal_maxima(records, n, par, field)) != 1:
            out.add(n)
    return out


def resolve_one(records: list[dict], value: str, field: str) -> dict | None:
    """인과 극대가 유일할 때만 그 기록을 돌려준다 — 그 밖에는 None(미확정)."""
    maxima = causal_maxima(records, value, None, field)
    return maxima[0] if len(maxima) == 1 else None


def ledger_anchor_index(records: list[dict]) -> int | None:
    """새 계약(명시 parents)의 도입 지점 — 첫 parents 보유 기록의 색인.
    이후 기록은 스키마 검증 대상이다(유산 구간은 호환 경계)."""
    for i, r in enumerate(records):
        if isinstance(r.get("parents"), list):
            return i
    return None


def ledger_append(path: Path, record: dict, expect=None) -> dict:
    """모든 `_ledger/` jsonl 공통 (Mechanism §3):
    잠금 → 전체 판독 → **구조 손상이면 거부** → parents = 현재 head 전부
    (병합 봉합) → rid = 정본 최대 rid로부터 단조 생성 → 행 단위 원자
    append·fsync(대장을 처음 만들었으면 그 디렉터리 엔트리까지).

    `expect`는 **잠금 안에서** 방금 읽은 기록으로 전제조건을 다시 보는 선택적
    검사다 — `expect(records)`가 문자열을 돌려주면 그것을 사유로 거부한다.
    호출부가 잠금 밖에서 검사하고 append 하는 사이에 다른 기기의 기록이
    동기화로 들어오면, 그것을 못 본 행이 그 기록의 **인과 자식**으로 붙어
    분기가 stale로 드러나지 않고 조용히 대체한다(그리고 행에 적은 전제가
    거짓 진술이 된다). 검사와 append를 같은 잠금에 두어야 그 창이 닫힌다."""
    return ledger_extend(path, [record], expect)[0]


def ledger_extend(path: Path, new: list[dict], expect=None) -> list[dict]:
    """`ledger_append`의 여러 기록판 — 한 잠금·한 fsync로 차례로 잇는다.
    첫 기록은 현재 head 전부를, 다음 기록은 바로 앞 기록을 `parents`로 받는다."""
    if not new:
        return []
    for record in new:
        record.setdefault("at", core.now_iso())
    durable.mkdirs_durable(path.parent)
    with open(path, "a+", encoding="utf-8") as f:
        lock_exclusive(f)
        try:
            f.seek(0)
            text = f.read()
            records = _parse_lines(text, path)
            dmg = ledger_damage(records, path)
            if dmg:
                raise ValueError(
                    f"대장 손상 — 수동 복구 절차 필요 (Mechanism §3 8항): "
                    + "; ".join(dmg[:5]))
            if expect is not None:
                why = expect(records)
                if why:
                    raise ValueError(why)
            last = max((r["rid"] for r in records if r.get("rid")),
                       key=rid_key, default=None)
            hs, rows = heads(records), []
            for record in new:
                record["rid"] = last = _next_rid(last)
                record["parents"], hs = hs, [last]
                # 줄 구분 문자는 이스케이프해 쓴다 — 같은 JSON 값이고, `splitlines()`로
                # 판독하는 구판 기기가 동기화로 받은 이 행에서 대장 손상을 보지 않는다.
                rows.append(json.dumps(record, ensure_ascii=False).translate(_LINE_SEP_ESC))
            # 판독을 통과했으니 개행 없는 꼬리는 완결 기록이다(찢긴 꼬리는 위에서
            # 손상으로 거부된다) — 개행을 채우지 않으면 새 기록이 그 행에 붙는다.
            lead = "\n" if text and not text.endswith("\n") else ""
            f.write(lead + "\n".join(rows) + "\n")
            f.flush()
            os.fsync(f.fileno())
            if not text:
                # 이번에 처음 만든 대장 — 행만 fsync하면 이름 자체가 전원 차단에
                # 유실돼, 이 기록을 전제로 한 다음 단계가 근거 없이 남는다.
                durable.fsync_dir(path.parent)
        finally:
            unlock(f)
    return new
