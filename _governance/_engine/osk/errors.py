"""osk.errors — 표면이 호출자에게 돌려주는 거부.

구현 근거: Mechanism §6-2 3항 — 검증에 실패한 요청은 기록하지 않고 위반 목록을 돌려준다.
이름 규칙·인용 기록·결속 같은 아래층도 같은 거부를 올리므로, 쓰기 통로(`write`)가 아니라 여기에 둔다.
"""
from __future__ import annotations


class WriteError(ValueError):
    """계약 위반·거부. `violations`에 위반 목록을 담는다(부분 성공 없음)."""

    def __init__(self, message: str, violations: list[str] | None = None,
                 **extra):
        super().__init__(message)
        self.violations = violations or [message]
        self.extra = extra
