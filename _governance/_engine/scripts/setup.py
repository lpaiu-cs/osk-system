"""osk 설치 명령 — 에이전트 프롬프트·마법사·수동 문서가 모두 이것을 부른다.

시스템 Python(3.11 이상)으로 vault의 `.venv`와 의존성을 갖춘 뒤 venv의 엔진
(`osk.setup`)에 넘긴다. 엔진은 의존성 없이는 import되지 않는다(Windows의 시간대 데이터
등). venv를 만들고 의존성을 설치하는 것은 `--apply`·`--interactive`일 때뿐이다 — 계획만
보는 호출은 아무것도 바꾸지 않는다. pip의 출력은 표준 오류로 보낸다: 표준 출력은
엔진이 내는 JSON 하나다.

vault 루트에서:
    python _governance/_engine/scripts/setup.py                 계획을 본다
    python _governance/_engine/scripts/setup.py --apply         확인을 요청한다 → 확인 뒤 한 번 더
    python _governance/_engine/scripts/setup.py --interactive   단말에서 확인받아 적용한다
    python _governance/_engine/scripts/setup.py --uninstall --apply
    python _governance/_engine/scripts/setup.py doctor          연결을 점검한다(읽기만)
"""
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
# 엔진(core.ROOT)과 같은 규칙 — 환경이 vault를 가리키면 그것, 아니면 이 파일의 자리.
ROOT = Path(os.environ.get("OSK_VAULT_ROOT") or ENGINE.parents[1])
# 서버가 기동할 때 import하는 것 — doctor의 등록 해석기 점검(_PROBE)과 같은 기준이다.
PROBE = ("import sys; assert sys.version_info >= (3, 11); import mcp.server.fastmcp, pydantic, "
         "yaml, rank_bm25; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Seoul')")


def venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ready(py: Path) -> bool:
    if not py.is_file():
        return False
    try:
        return subprocess.run([str(py), "-c", PROBE], capture_output=True, timeout=120,
                              stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def prepare(py: Path) -> None:
    """`.venv`를 만들고(없으면) 의존성을 설치한다 — CI가 검증한 판(constraints.txt)으로."""
    if not py.is_file():
        print(f"osk: {ROOT / '.venv'}를 만든다", file=sys.stderr)
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    cmd = [str(py), "-m", "pip", "install", "-r", str(ENGINE / "requirements.txt")]
    if (ENGINE / "constraints.txt").is_file():
        cmd += ["-c", str(ENGINE / "constraints.txt")]
    print("osk: 의존성을 설치한다", file=sys.stderr)
    if subprocess.run(cmd, stdout=sys.stderr, stdin=subprocess.DEVNULL).returncode != 0 or not ready(py):
        sys.exit(f"osk: 의존성을 갖추지 못했다 — 위 pip 출력을 보고 `{' '.join(cmd)}`를 다시 실행한다")


def main() -> int:
    if sys.version_info < (3, 11):
        sys.exit("osk에는 Python 3.11 이상이 필요하다 — 3.11 이상의 python으로 다시 실행한다")
    args = sys.argv[1:]
    py = venv_python()
    if not ready(py):
        if not {"--apply", "--interactive"} & set(args):
            state = "incomplete" if py.is_file() else "missing"
            out = {"ok": False, "venv": str(ROOT / ".venv"), "state": state,
                   "next": "--apply로 부르면 .venv를 만들고 의존성을 설치한 뒤 계획을 보인다"}
            sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            return 0
        prepare(py)
    env = {**os.environ, "OSK_VAULT_ROOT": str(ROOT), "PYTHONPATH": str(ENGINE), "PYTHONUTF8": "1"}
    module = ["osk.cli", "doctor", *args[1:]] if args[:1] == ["doctor"] else ["osk.setup", *args]
    return subprocess.run([str(py), "-m", *module], env=env, cwd=str(ROOT)).returncode


if __name__ == "__main__":
    sys.exit(main())
