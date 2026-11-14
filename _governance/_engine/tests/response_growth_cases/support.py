"""응답 뒤 검토(fork)의 수트의 바탕 — 임시 저장소를 세우고, 갈래들이 함께 쓰는 도우미를 둔다.

이 모듈을 읽는 것이 곧 격리다. 갈래 파일과 `tests/test_response_growth.py`은 엔진보다 이 모듈을 먼저 읽는다."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from osk import worker_preflight  # noqa: E402
from osk.vault import Vault  # noqa: E402
VAULT = Vault.open()

REAL_HOOK_READY = worker_preflight._codex_hook_ready  # setUp stands it in; one test drives the real query.
