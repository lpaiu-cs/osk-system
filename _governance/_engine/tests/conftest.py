"""pytest 어댑터 — check() 실패가 pytest에서 삼켜지지 않게 한다.

정식 러너(`python tests/test_regression.py`)의 `check()`는 실패를 FAIL
목록에 누적하고 계속 진행한다 — 실패 나열형 러너다. pytest는 예외 없이
끝난 함수를 전부 통과로 세므로, 이 어댑터가 없으면 check 실패가 조용히
사라진다. v3.2.0이 실제로 그렇게 나갔다: 첫-노드 규칙이 기존 시험 9건과
충돌했는데 pytest는 "103 passed"를 보고했다.

각 시험이 끝날 때 그 시험이 늘린 FAIL 구간을 assert로 표면화한다 —
정식 러너의 판정과 pytest의 판정이 같은 자로 재어진다."""
import pytest


@pytest.fixture(autouse=True)
def _surface_check_failures():
    import test_regression as t
    before = len(t.FAIL)
    yield
    fresh = t.FAIL[before:]
    assert not fresh, f"check 실패 {len(fresh)}건:\n" + "\n".join(fresh)
