"""훅 문맥의 조립 — 호스트가 받는 길이 안에 블록 단위로 싣는다(Mechanism §9-3 1항).

Claude Code는 1만 자를 넘는 훅 문맥을 파일로 빼고 모델에 앞 2KB만 보이고, Kiro는
3천 자에서 자른다(호스트별 예산은 `Adapter.hook_budget`). 잘리면 뒤쪽 블록의 지시가
통째로 사라진다. 그래서 예산을 넘으면 우선순위가 낮은 블록부터 **통째로** 접고,
접은 블록은 끝의 한 줄에 건수와 조회 경로로 남긴다. 블록 중간은 자르지 않는다.
접었다는 사실은 처분이나 검토 완료가 아니다 — 대기는 그대로 남는다."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

SEP = "\n\n"
KEEP = 100          # 접지 않는 블록 — 세션 키·밀림 경고·케이던스처럼 짧고 늘 필요한 것


@dataclass(eq=False)
class Block:
    name: str
    text: str
    keep: int = KEEP    # 낮을수록 먼저 접는다
    label: str = ""     # 접었을 때 끝 줄에 남는 말 — 건수·나이와 할 일
    pull: str = ""      # 접은 내용을 돌려주는 곳 — `overview:<섹션>` 또는 명령
    on_shown: Callable[[], None] | None = None  # 전문이 실제로 실렸을 때(예: 기억 해시 기록)


def _pulls(folded: list[Block], session: str | None) -> list[str]:
    sections = [b.pull.split(":", 1)[1] for b in folded if b.pull.startswith("overview:")]
    out = [f"`overview(session={json.dumps(session, ensure_ascii=False)}, "
           f"include={json.dumps(sections, ensure_ascii=False)})`"] if sections else []
    return out + [b.pull for b in folded if b.pull and not b.pull.startswith("overview:")]


def fold_line(folded: list[Block], budget: int, session: str | None = None) -> str:
    if not folded:
        return ""
    labels = [b.label or b.name for b in folded]
    pulls = _pulls(folded, session)
    return (f"[osk 접음 — 훅 한도 {budget:,}자 안에 싣지 못한 것: " + " · ".join(labels)
            + (". 읽는 곳: " + " · ".join(pulls) if pulls else "") + "]")


def assemble(blocks: list[Block], budget: int | None, *,
             session: str | None = None) -> tuple[str, set[str]]:
    """(문맥, 접은 블록 이름). 순서는 받은 그대로다 — 밀림 경고가 맨 앞이라는 조문의
    순서(§9-3 3항)를 호출자가 정한다."""
    live = [b for b in blocks if b.text]
    folded: list[Block] = []
    place = {id(b): i for i, b in enumerate(blocks)}

    def render() -> str:
        shown = [b.text for b in live]
        kept = sorted(folded, key=lambda b: place[id(b)])
        return SEP.join(shown + ([fold_line(kept, budget, session)] if kept else []))

    text = render()
    if budget:
        order = sorted((b for b in live if b.keep < KEEP),
                       key=lambda b: (b.keep, -place[id(b)]))
        for b in order:
            if len(text) <= budget:
                break
            live.remove(b)
            folded.append(b)
            text = render()
        if len(text) > budget:
            # ponytail: 접지 않는 블록만으로 한도를 넘는 경우의 안전판 — 실측 최대는 1천 자 남짓.
            cut = text.rfind("\n", 0, budget - 24)
            return text[:max(cut, 0)] + "\n[osk 생략 — 훅 한도]", {b.name for b in folded}
    for b in live:
        if b.on_shown:
            try:
                b.on_shown()
            except Exception:
                pass    # 기록을 못 하면 다음 검토 턴이 전문을 다시 실을 뿐이다
    return text, {b.name for b in folded}
