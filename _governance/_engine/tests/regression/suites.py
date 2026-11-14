"""회귀 수트의 한 갈래: 따로 도는 수트 — 격리 시험 모듈을 자식 프로세스로 돌린다.

러너(`tests/test_regression.py`)가 정해진 순서로 돌린다. 따로 돌리는 파일이 아니다."""
from __future__ import annotations
import os, subprocess, sys
from unittest import mock

from regression.support import check, ENGINE, FAIL, PASS, SKIP, skip


SUITE_SKIP = 77     # 격리 수트가 "환경 때문에 돌지 못했다"를 알리는 종료 코드


def _suite(label, name, timeout=180, *, required=False):
    """격리 수트 하나를 돌려 한 줄로 판정한다. 시간 초과·기동 실패도 그 수트의
    FAIL로 남긴다 — 예외가 러너로 새면 뒤의 수트가 조용히 실행되지 않는다.
    종료 코드 77은 SKIP이다. 필수 수트면 실행되지 않은 것도 FAIL이다."""
    try:
        proc = subprocess.run([sys.executable, "-B", str(ENGINE / "tests" / name)],
                              capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except Exception as e:
        out = (getattr(e, "stdout", None) or b"") + (getattr(e, "stderr", None) or b"")
        check(label, False, f"{e!r}\n" + out.decode("utf-8", errors="replace")[-6000:])
        return
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    if proc.returncode == SUITE_SKIP and not required:
        skip(label, (out.strip().splitlines() or [""])[-1])
        return
    check(label, proc.returncode == 0, out[-6000:])


def test_sync_network_subprocess():
    _suite("동기화 네트워크·적용 잠금·고정 SHA의 프로세스 경계", "test_sync_network.py")


def test_upgrade_matrix_subprocess():
    """구 릴리스가 만들고 쓴 vault를 그 릴리스 자신의 updater로 후보에 올린다
    (v3.20.1 사고의 경로). full을 요청했으면 이력 부재도 실패다."""
    _suite("업그레이드 경로 행렬(구 릴리스 vault → 자기 updater → 후보)",
           "test_upgrade_matrix.py", timeout=1500,
           required=os.environ.get("OSK_MATRIX") == "full")


def test_release_workflow_subprocess():
    _suite("정식 발행은 검증한 SHA를 태그로 공개한다", "test_release_workflow.py")


def test_architecture_subprocess():
    """구조 규범(docs/CODE-STANDARD.md)의 실측이 기준선과 같다 — 위반이 늘어도, 줄이고 기준선을 낮추지
    않아도 실패다. 엔진을 import하지 않는 `ast` 시험이라 엔진이 깨져 있어도 돈다."""
    _suite("구조 규범: 층·지연 import·밑줄 접근·넓은 예외·함수 크기가 기준선과 같다", "test_architecture.py")


def test_surface_snapshot_subprocess():
    """MCP 도구의 설명·스키마와 `osk` CLI의 명령·옵션이 기록(`surface_snapshot.json`)과 전문으로 같다.
    구조 개편이 표면을 건드리지 않았음을 본다 — 일부러 바꾸는 PR은 기록을 함께 고친다."""
    _suite("표면 고정: MCP 도구와 CLI가 기록과 같다", "test_surface_snapshot.py")


def test_vault_subprocess():
    """저장소는 값이다(docs/CODE-STANDARD.md 4항) — 한 프로세스에서 임시 저장소 둘을 열어 폴더 이름과 대장
    자리가 서로 섞이지 않음을 본다. `osk.core`를 거치지 않는다."""
    _suite("저장소 값: 한 프로세스에서 연 임시 저장소 둘이 섞이지 않는다", "test_vault.py")


GROWTH_SUITES = ("test_distillation.py", "test_integration.py", "test_codex_forks.py", "test_integration_recovery.py", "test_growth.py", "test_growth_daily.py", "test_growth_status.py", "test_response_growth.py", "test_retrieval.py", "test_organization.py", "test_hidden_raw.py", "test_raw_view.py", "test_space_layout.py", "test_update_review.py", "test_update_notice.py", "test_harness.py", "test_setup.py", "test_services.py")


def test_growth_loop_subprocesses():
    for name in GROWTH_SUITES:
        # The 72-case capture suite exceeds 180s on hosted Windows runners
        # while still progressing. Keep a finite budget for its durable I/O.
        # The 77-case growth suite did the same (PR #327; about 110s on a dev machine).
        timeout = 300 if os.name == "nt" and name in ("test_integration.py", "test_growth.py") else 180
        _suite(f"성장 경로 격리 수트: {name}", name, timeout=timeout)


def test_suite_timeout_is_isolated():
    """한 격리 수트의 시간 초과는 그 수트의 FAIL이고, 뒤의 수트는 계속 돈다."""
    first, rest = GROWTH_SUITES[0], GROWTH_SUITES[1:]

    def run(cmd, **kw):
        if cmd[-1].endswith(first):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"), output=b"partial")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    n_pass, n_fail = len(PASS), len(FAIL)
    error = None
    with mock.patch("subprocess.run", side_effect=run):
        try:
            test_growth_loop_subprocesses()
        except Exception as e:
            error = e
    passed, failed = PASS[n_pass:], FAIL[n_fail:]
    del PASS[n_pass:], FAIL[n_fail:]
    check("수트 시간 초과가 러너 밖으로 새지 않는다", error is None, repr(error))
    check("시간 초과는 그 수트 이름의 FAIL 한 줄",
          len(failed) == 1 and first in failed[0] and "partial" in failed[0], failed)
    check("뒤의 수트는 전부 계속 실행된다",
          [x.rsplit(": ", 1)[-1] for x in passed] == list(rest), passed)


def test_suite_skip_is_not_pass():
    """격리 수트의 종료 코드 77은 SKIP 한 줄이다 — 통과로도 실패로도 세지 않는다."""
    done = subprocess.CompletedProcess([], SUITE_SKIP, b"...\nSKIP: no release history\n", b"")
    counts = len(PASS), len(FAIL), len(SKIP)
    with mock.patch("subprocess.run", return_value=done):
        _suite("skip-probe", "test_upgrade_matrix.py", timeout=5)
    skipped = SKIP[counts[2]:]
    del SKIP[counts[2]:]
    check("종료 코드 77은 사유와 함께 SKIP으로만 남는다",
          (len(PASS), len(FAIL)) == counts[:2]
          and skipped == ["skip-probe — SKIP: no release history"], skipped)
    counts = len(PASS), len(FAIL), len(SKIP)
    with mock.patch("subprocess.run", return_value=done), mock.patch.dict(os.environ, {"OSK_MATRIX": "full"}):
        test_upgrade_matrix_subprocess()
    failed = FAIL[counts[1]:]
    del FAIL[counts[1]:]
    check("full 행렬이 실행되지 않으면 SKIP이 아니라 실패다",
          len(failed) == 1 and "no release history" in failed[0]
          and (len(PASS), len(SKIP)) == (counts[0], counts[2]), failed)
