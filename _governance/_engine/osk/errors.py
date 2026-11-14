"""osk.errors — 오류의 종류 (코드 규범 7항).

거부(`WriteError`)와 손상(`LedgerDamage`)은 서로 다른 형이다. 읽지 못한 것 — 다시 하면 되는 일시적 실패 — 은
`OSError`이고, 낡은 엔진은 `core.StaleEngineError`다. 받는 쪽은 문구가 아니라 형으로 가른다.

구현 근거: Mechanism §6-2 3항 — 검증에 실패한 요청은 기록하지 않고 위반 목록을 돌려준다. §3 8항 — 손상된 대장은
수동으로 복구한다. 이름 규칙·인용 기록·결속 같은 아래층도 같은 거부를 올리므로, 쓰기 통로(`write`)가 아니라 여기에 둔다.
"""
from __future__ import annotations


class WriteError(ValueError):
    """계약 위반·거부. `violations`에 위반 목록을 담는다(부분 성공 없음)."""

    def __init__(self, message: str, violations: list[str] | None = None,
                 **extra):
        super().__init__(message)
        self.violations = violations or [message]
        self.extra = extra


class LedgerDamage(ValueError):
    """대장의 손상 — 해석할 수 없는 행, 기록이 아닌 행, UTF-8이 아닌 바이트, rid의 부재·형식 위반·중복.

    다시 읽어도 낫지 않는다. 수동 복구가 필요하다(Mechanism §3 8항). `ValueError`의 아래 형이라 거부와 함께
    받던 곳은 그대로 받고, 손상만 가려 받으려는 곳은 이 형을 받는다."""
