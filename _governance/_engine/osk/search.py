"""osk.search — 검색 3계약 중 작업 검색·열람 검색.

구현 근거: 헌법 11조 3항 — 작업 검색(모든 Space 연합, `_raw/`·Workbench 제외),
열람 검색(사용자에게 결과 노출), 권한 검사는 osk.authority(랭킹 금지)가 전담.
11조 4항·시행령 §7 4항 — summary 미리보기 `[[대상]](: summary)` 확장(파생 표시,
저장·권한 판정 대상 아님).

서명 제도가 폐지됐으므로 검색은 서명 여부를 표시하지 않는다 — 검색은 파일의
현재 상태(보호영역에서는 작업본)를 대상으로 하며, 결과로 권한을 추정하지
않는다(헌법 11조 3항).

v0 랭킹: 문자 bigram BM25 (rank_bm25). 임베딩·데몬 인덱스는 후속 —
파일 정본에서 매 호출 재계산 가능해야 한다는 원칙(시행령 §11 1항)이 우선.
"""
from __future__ import annotations
import re

from rank_bm25 import BM25Okapi

from .core import ROOT, posix_rel
from . import graph


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
        for stem, p, kind, n in parsed:
            if kind[0] in ("workbench-transit", "governance"):
                continue  # 검색 제외 — Workbench(헌법 11조 3항·4조 5항)와
                          # 통치 구획(시행령 §10 1항 — 명시 조회로 도달한다)
            toks = _tokens(f"{stem} {n.meta.get('summary','')} {n.body}")
            self.paths.append((stem, p, n))
            self.tokens.append(set(toks))
            corpus.append(toks)
        self.bm25 = BM25Okapi(corpus) if corpus else None

    def work_search(self, query: str, k: int = 8) -> list[dict]:
        if not self.bm25:
            return []
        q = _tokens(query)
        qset = set(q)
        scores = self.bm25.get_scores(q)
        # 탈락 기준은 점수 부호가 아니라 질의 토큰 겹침 — 정확히 문서 절반에
        # 나오는 항은 BM25 idf가 0이라 점수로 거르면 일치가 전멸한다
        adjusted = [(float(s), (stem, p, n))
                    for s, (stem, p, n), toks in zip(scores, self.paths, self.tokens)
                    if qset & toks]
        ranked = sorted(adjusted, key=lambda x: -x[0])[:k]
        out = []
        for score, (stem, p, n) in ranked:
            out.append({
                "title": stem,
                "path": posix_rel(p, ROOT),
                "id": n.id,
                "summary": str(n.meta.get("summary", "")),
                "updated": str(n.meta.get("updated", "")),
                "score": round(float(score), 3),
            })
        return out

    def view_search(self, query: str, k: int = 8) -> list[dict]:
        """열람 검색 — 사용자에게 결과를 노출하는 검색이다(헌법 11조 3항).
        서명 표시가 폐지됐으므로 작업 검색과 같은 결과를 돌려준다. `title`은
        **노드 이름 그대로** 둔다 — 다른 도구가 `name`으로 받는 값이다."""
        return self.work_search(query, k)

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
