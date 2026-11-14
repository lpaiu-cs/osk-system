"""회귀 수트의 바탕 — 임시 mini-vault를 세우고, 시험들이 함께 쓰는 도우미를 둔다.

이 모듈을 읽는 것이 곧 격리다: `OSK_VAULT_ROOT`와 임시 폴더를 이 실행의 자리로 돌린 뒤에 엔진을 읽는다.
그래서 갈래 파일은 엔진보다 이 모듈을 먼저 읽는다. 결과는 `PASS`·`FAIL`·`SKIP`에 쌓이고, 러너
(`tests/test_regression.py`)가 센다."""
from __future__ import annotations
import dataclasses, json, os, shutil, stat, sys, tempfile
from pathlib import Path


ENGINE = Path(__file__).resolve().parents[2]
_TMP = tempfile.TemporaryDirectory(prefix="osk-regr-")
MINI = Path(_TMP.name) / "mini-vault"
os.environ["OSK_VAULT_ROOT"] = str(MINI)   # osk import 전에 — 전 모듈이 mini를 본다
# 픽스처는 기기의 git 기본 브랜치에 기대지 않는다 — 전역 설정이 없는 CI 러너는
# `master`를 쓴다. 일부러 낯선 이름을 주어, `-b main` 없는 init이 어디서든 드러나게 한다.
# 자동 정비(`maintenance run --auto`·`gc --auto`)도 끈다 — 커밋 뒤 분리 실행된
# 정비가 픽스처의 `.git/objects`를 고치는 동안 수트가 그 트리를 복사·삭제하면
# 파일이 사라져 죽는다(macOS CI 실측: `maintenance.lock`·`bitmap-ref-tips_*` 부재).
_n = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
_cfg = (("init.defaultBranch", "osk-fixture-default"),
        ("maintenance.auto", "false"), ("gc.auto", "0"))
os.environ["GIT_CONFIG_COUNT"] = str(_n + len(_cfg))
for _i, (_k, _v) in enumerate(_cfg, _n):
    os.environ[f"GIT_CONFIG_KEY_{_i}"] = _k
    os.environ[f"GIT_CONFIG_VALUE_{_i}"] = _v
sys.path.insert(0, str(ENGINE))
# git 없는 vault의 잠금·상태 자리(core.local_lock_path)는 임시 디렉터리다 — 시험
# vault마다 osk-integration-* 따위가 실 임시 디렉터리에 쌓였다. 이 실행과 자식
# 프로세스의 임시 자리를 _TMP 안으로 돌리고(env 상속), 끝에서 _TMP째 지운다.
# 둘 다 돌려야 한다 — 부모와 자식(훅)이 다른 자리를 보면 상태를 못 나눈다.
REAL_TMP = Path(tempfile.gettempdir())
_LEAK_BEFORE = {p.name for p in REAL_TMP.glob("osk-integration-*.json")}
RUN_TMP = Path(_TMP.name) / "tmp"
RUN_TMP.mkdir()
for _k in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_k] = str(RUN_TMP)
tempfile.tempdir = str(RUN_TMP)
# 새 릴리스 확인(osk.update_check)은 정본에 태그를 묻는 분리 프로세스를 띄운다.
# 수트는 네트워크에 기대지 않고 자식 프로세스를 남기지 않는다 — 격리 수트도 이
# 값을 물려받으며, 확인 자체를 시험하는 test_update_notice.py만 걷어 낸다.
os.environ["OSK_UPDATE_CHECK"] = "0"
# 입력·시작 훅은 포착과 구독 CLI 판정을 분리 작업자에게 넘긴다(#192). 수트의 훅은 작업자를
# 띄우지 않는다 — 실행 자리가 지워진 뒤에도 남는 자식이 없고, 작업자의 몫은 시험이 직접 부른다.
os.environ["OSK_HOOK_WORKER"] = "0"

from osk import core, validate, write  # noqa: E402
from osk.vault import Vault  # noqa: E402
VAULT = Vault.open()


def _vault_at(root):
    """mini-vault와 배치가 같고 루트만 다른 저장소 값 — 다른 저장소를 세워 보는 시험이 쓴다."""
    return dataclasses.replace(VAULT, root=Path(root).resolve())

validate.make_mini_vault(VAULT, MINI)
ROOT = VAULT.root
assert ROOT == MINI.resolve(), f"격리 실패 — 실 vault를 가리킨다: {ROOT}"

PASS, FAIL, SKIP = [], [], []

# POSIX mode(그룹·기타 비트, 실행 비트)를 파일시스템이 보존하는가. Windows는
# 읽기 전용 비트 하나만 다루므로 `chmod(0o600)`이 `0o666`으로 돌아온다 — 그
# 값에 건 단언은 그 기기에서 **참일 수 없다.** 통과로 세면 거짓 신호이므로
# 건너뛴 것으로 센다(시행령 §11 — 실패는 보류·보고).
POSIX_MODE = os.name != "nt"


def skip(name, why=""):
    """환경 제약으로 **수행하지 않은** 검사. 통과에 세지 않는다.

    구판은 이런 자리를 `check("…(skip)", True)`로 적어 통과 수를 부풀렸다 —
    "1018 통과"가 무엇을 실제로 시험했는지 말해 주지 못하는 이유의 하나였다."""
    SKIP.append(f"{name} — {why}" if why else name)


def rmtree_force(p):
    """읽기 전용 파일까지 지운다.

    Windows에서 `.git/objects`의 blob은 읽기 전용이라 기본 `shutil.rmtree`가
    `PermissionError(WinError 5)`로 죽는다 — 그 예외가 시나리오를 통째로
    중단시켜 뒤의 검사들이 실행되지 않았다."""
    def _onerr(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass
    shutil.rmtree(p, onerror=_onerr)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(
        f"{name}{' — ' + str(detail) if detail and not cond else ''}")


def wipe_sig():
    VAULT.signatures.unlink(missing_ok=True)


def node_text(nid: str, summary="회귀 시험", body="본문", extra=""):
    return (f"---\nid: {nid}\ncreated: 2026-08-02 16:00 (KST)\n"
            f"updated: 2026-08-02 16:00 (KST)\nauthor: agent\ndrafter: agent\n"
            f'summary: "{summary}"\n{extra}---\n\n{body}\n')


def _raises(fn):
    def run():
        try:
            fn()
            return False
        except ValueError:
            return True
    return run


# ── 14g. 쓰기 통로 — 계약 강제·CAS 서명 결속·델타·라우팅 ────────────────
def _w(fn, *a, **kw):
    # 표면의 `_guard`와 같은 집합을 잡는다 — 수트가 잡는 것이 표면이 잡는 것보다
    # 좁으면 거부가 수트에서만 예외로 터져 시험이 통로를 대변하지 못한다.
    try:
        return fn(*a, **kw)
    except (write.WriteError, core.StaleEngineError) as e:
        return {"ok": False, "violations": e.violations, **e.extra}


def _unhang(hub, name):
    """자리 걸기(`write._place`)가 허브 끝에 더한 `- [[이름]]` 줄을 뺀다 — 허브가 그 노드를
    가리키지 않는 상태를 전제로 하는 시험이 쓴다. 그 상태는 이제 허브 편집으로만 생긴다."""
    return _w(write.update_node, VAULT, hub, old_text=f"\n\n- [[{name}]]", new_text="")


def _append_round(session, record, user, agent, space=None):
    """라운드 하나 — 인용 통로(`raw.append_rounds`)의 단수형. 표면은 `cite_round`로만 쓴다."""
    from osk import raw
    r = raw.append_rounds(VAULT, session, record, [{"user": user, "agent": agent}], space)
    return {"ok": True, "path": r["path"], "index": r["indices"][0],
            "round_ref": r["round_refs"][0], "filtered": r["filtered"],
            **({"binding": r["binding"]} if "binding" in r else {})}


def _rounds_of(session, record):
    """세션 기록의 라운드 수 — 결속된 scope의 그 기록을 센다."""
    from osk import raw
    path = raw.record_path(VAULT, write.resolve_session(VAULT, session), record)
    return len(raw.rounds(raw.read_exact(path))) if path.exists() else 0


def _hook_context(raw, event):
    """하네스처럼 wire JSON을 먼저 해석한 뒤 기존 본문 계약을 검사한다."""
    if not raw:
        return ""
    output = json.loads(raw)["hookSpecificOutput"]
    check("훅 JSON의 이벤트가 실행한 훅과 같다", output["hookEventName"] == event, output)
    return output["additionalContext"]

# ── v3.7.0 색인 재설계의 회귀 시험 ──────────────────────────────────────
# 이 절의 시험들은 **뮤테이션 검출률로 잰다.** 통과 건수는 보호의 근거가 되지
# 못한다는 것이 실측으로 드러났다 — 구현에 결함 17종을 심었을 때 처음 판의
# 시험은 11종을 하나도 잡지 못했다. 그래서 각 단언은 "무엇을 망가뜨리면 이것이
# 실패하는가"를 먼저 정하고 짰다.

_FIXED_PAST = 1_700_000_000.0        # 고정 과거 시각 — 아래 `_age_all` 참조


def _age_all(ts=_FIXED_PAST):
    """모든 파일의 mtime을 **고정된** 과거로 민다 — racy 가드를 끄고 서명
    자체를 시험할 수 있게 한다.

    시각이 고정이어야 한다. 호출마다 `time.time() - 3600`처럼 새로 계산하면
    파일을 하나도 건드리지 않아도 mtime이 전부 바뀌어 지문이 달라지고, 그러면
    지문에서 경로를 지우든 크기를 지우든 범위를 좁히든 **모든 단언이 공허하게
    통과한다**(실측으로 확인된 결함)."""
    for root, _dirs, files in os.walk(ROOT):
        for f in files:
            try:
                os.utime(os.path.join(root, f), (ts, ts))
            except OSError:
                pass
