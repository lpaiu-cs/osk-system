"""osk.search — 검색 3계약 중 작업 검색·열람 검색.

구현 근거: 헌법 11조 3항 — 작업 검색(모든 Space 연합, `_raw/`·Workbench 제외),
열람 검색(사용자에게 결과 노출), 권한 검사는 osk.authority(랭킹 금지)가 전담.
11조 4항·시행령 §7 4항 — summary 미리보기 `[[대상]](: summary)` 확장(파생 표시,
저장·권한 판정 대상 아님).

서명 제도가 폐지됐으므로 검색은 서명 여부를 표시하지 않는다 — 검색은 파일의
현재 상태(보호영역에서는 작업본)를 대상으로 하며, 결과로 권한을 추정하지
않는다(헌법 11조 3항).

랭킹: 관련성은 문자 bigram BM25 (rank_bm25)이고, 노출 순위는 거기에 최신성·중심성·
locality를 곱한다(시행령 §8 3항, 계수는 Mechanism §8 8항). 임베딩·데몬 인덱스는 후속 —
파일 정본에서 매 호출 재계산 가능해야 한다는 원칙(시행령 §11 1항)이 우선.
"""
from __future__ import annotations
import math
import re
from datetime import datetime

from rank_bm25 import BM25Okapi

from . import core
from .core import ROOT, TS_FMT, posix_rel
from . import graph

# 노출 순위의 계수(Mechanism §8 8항). 관련성에 상한 있는 가산을 곱하며, 모두 0이면 BM25 순서다.
W_RECENT, HALF_LIFE = 0.2, 30 * 86400   # 갓 고친 노드 +20%, 30일마다 반
W_CENTRAL = 0.2                          # 중심성의 log 비율 — 가장 중심인 노드 +20%
W_SCOPE, W_PRIMARY = 0.1, 0.2            # 결속 scope +10%, 주된 scope +20% — Domain·Person은 중립


def _age(updated, now: datetime) -> float | None:
    """frontmatter `updated`(KST)의 나이(초). 읽을 수 없으면 None — 최신성을 얹지 않는다."""
    try:
        return max(0.0, (now - datetime.strptime(str(updated), TS_FMT).replace(tzinfo=core.KST)).total_seconds())
    except ValueError:
        return None


def _tokens(text: str) -> list[str]:
    text = re.sub(r"[^\w가-힣]+", " ", text.lower())
    toks = []
    for w in text.split():
        if re.match(r"^[a-z0-9_]+$", w):
            toks.append(w)
        else:
            toks += [w[i:i + 2] for i in range(len(w) - 1)] or [w]
    return toks


class Searcher:
    def __init__(self, idx: graph.Index | None = None):
        self.idx = idx or graph.Index()
        self.paths, self.tokens, corpus = [], [], []
        # 파싱 실패 노드는 검색에서 빼고 목록으로 보고한다 — 파일 하나가
        # 검색 전체를 멈추지 않는다 (시행령 §11 — 실패는 보류·보고)
        self.broken: dict[str, str] = dict(getattr(self.idx, "broken", None) or {})
        parsed = []
        for stem, (p, kind) in self.idx.nodes.items():
            try:
                n = self.idx.node(p)
            except Exception as e:
                self.broken[stem] = str(e)
                continue
            parsed.append((stem, p, kind, n))
        self.scope_of = []  # 검색 대상마다 scope 이름 — locality의 단서(Domain·Person은 None)
        for stem, p, kind, n in parsed:
            if kind[0] in ("workbench-transit", "governance"):
                continue  # 검색 제외 — Workbench(헌법 11조 3항·4조 5항)와
                          # 통치 구획(시행령 §10 1항 — 명시 조회로 도달한다)
            toks = _tokens(f"{stem} {n.meta.get('summary','')} {n.body}")
            self.paths.append((stem, p, n))
            self.scope_of.append(kind[1] if kind[0] == "scope" and len(kind) > 1 else None)
            self.tokens.append(set(toks))
            corpus.append(toks)
        self.bm25 = BM25Okapi(corpus) if corpus else None
        # 최신성·중심성은 질의와 무관하다. 허브는 갈래의 입구라 얹지 않는다 — 본문이 배선마다
        # 바뀌고(시행령 §7 2항이 그 변경을 상태 변경으로 보지 않는다) 구성 노드의 Link가 모인다.
        central = graph.centrality(self.idx)
        knowledge = [(stem, n) for stem, p, n in self.paths if not graph.is_hub(p)]
        top = math.log1p(max((central.get(stem, 0.0) for stem, _ in knowledge), default=0.0))
        now = datetime.now(core.KST)
        self.static = []
        for stem, p, n in self.paths:
            boost = 0.0
            if not graph.is_hub(p):
                if top:
                    boost += W_CENTRAL * math.log1p(central.get(stem, 0.0)) / top
                age = _age(n.meta.get("updated"), now)
                if age is not None:
                    boost += W_RECENT * 0.5 ** (age / HALF_LIFE)
            self.static.append(boost)

    def work_search(self, query: str, k: int = 8, local: dict | None = None) -> list[dict]:
        """`local`은 `write.bindings`의 결속(`scopes`·`primary`)이다 — 그 scope의 노드를 앞으로
        당기는 순위 자질이며, 검색 관문이 아니다(헌법 11조 3항)."""
        if not self.bm25:
            return []
        q = _tokens(query)
        qset = set(q)
        title_query = query.strip().casefold()
        scores = self.bm25.get_scores(q)
        scopes, primary = set((local or {}).get("scopes") or ()), (local or {}).get("primary")
        factors = [1 + b + (W_PRIMARY if sc and sc == primary else W_SCOPE if sc in scopes else 0.0)
                   for b, sc in zip(self.static, self.scope_of)]
        # 탈락 기준은 점수 부호가 아니라 질의 토큰 겹침 — 정확히 문서 절반에
        # 나오는 항은 BM25 idf가 0이라 점수로 거르면 일치가 전멸한다. 문서가 적은 vault에서는
        # 흔한 항의 idf가 음수라 점수도 음수다 — 그때는 나눠서 같은 쪽(위)으로 당긴다.
        adjusted = [(float(s) * f if s >= 0 else float(s) / f, f, (stem, p, n))
                    for s, f, (stem, p, n), toks in zip(scores, factors, self.paths, self.tokens)
                    if qset & toks or stem.casefold() == title_query]
        # A known title is a handle, not a BM25 length-normalization contest. The rest follow
        # the exposure score; the factor breaks the ties BM25 leaves, a zero IDF among them.
        ranked = sorted(adjusted, key=lambda x: (
            x[2][0] != query.strip(), x[2][0].casefold() != title_query, -x[0], -x[1]))[:k]
        out = []
        for score, _, (stem, p, n) in ranked:
            out.append({
                "title": stem,
                "path": posix_rel(p, ROOT),
                "id": n.id,
                "summary": str(n.meta.get("summary", "")),
                "updated": str(n.meta.get("updated", "")),
                "score": round(float(score), 3),
            })
        return out

    def view_search(self, query: str, k: int = 8, local: dict | None = None) -> list[dict]:
        """열람 검색 — 사용자에게 결과를 노출하는 검색이다(헌법 11조 3항).
        서명 표시가 폐지됐으므로 작업 검색과 같은 결과를 돌려준다. `title`은
        **노드 이름 그대로** 둔다 — 다른 도구가 `name`으로 받는 값이다."""
        return self.work_search(query, k, local)

    def expand_preview(self, text: str) -> str:
        """`[[대상]]` → `[[대상]](: summary)` 파생 표시."""
        def rep(m):
            name = m.group(1).strip()
            hit = self.idx.nodes.get(name)
            if not hit:
                return m.group(0)
            try:
                s = str(self.idx.node(hit[0]).meta.get("summary", "")).strip()
            except Exception:
                return m.group(0)   # 파싱 실패 대상은 확장하지 않는다(표시는 파생)
            return f"{m.group(0)}(: {s})" if s else m.group(0)
        return re.sub(r"\[\[([^\]#|]+)\]\]", rep, text)
