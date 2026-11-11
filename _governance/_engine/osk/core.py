"""osk.core — 시각·식별자·해시·경로·명령줄의 공통 규약.

구현 근거: Mechanism §1(배치)·§2(식별과 시간). 노드가 무엇인지 모르는 작은 것들이다. 대장은 `osk.ledger`,
잠금과 판본 관문은 `osk.locks`, 꺼져도 남는 쓰기는 `osk.durable`에 있다.
"""
from __future__ import annotations
import functools, hashlib, os, random, re, shlex, string, subprocess, sys
from datetime import datetime
from pathlib import Path

from .layout import adapt_path
from .vault import Vault

B36 = string.digits + string.ascii_lowercase


@functools.cache
def _kst():
    """Mechanism §2 2항 — 표기 시간대는 KST 고정. tz 데이터는 처음 쓸 때 읽는다 — Windows에서는
    tzdata 패키지를 `importlib.resources`로 읽어 import에 20 ms 남짓 들고, 입력 훅은 대개 시각을
    적지 않는다(#192). 다른 모듈은 `core.KST`로 읽는다(`__getattr__`)."""
    from zoneinfo import ZoneInfo
    return ZoneInfo("Asia/Seoul")


def __getattr__(name: str):
    if name == "KST":
        return _kst()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


TS_FMT = "%Y-%m-%d %H:%M (KST)"          # Mechanism §2 2항 — frontmatter 시각
TS_RE = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} \(KST\)$"
# Mechanism §2 1항 — YYMMDD-ssss-rrrrrrrr. 무작위부는 8자이며, 4자인 구형
# id도 그대로 유효하다(길이가 다르므로 신·구가 충돌할 수 없다).
#
# 이것은 유효성 검사가 아니라 **판별자**다 — "이 문자열은 id인가 이름인가"를
# 가르는 자리로 여섯 곳에서 쓰인다(resolve·_score_key·live_locate·_as_links·
# node_checks.check_edges·read_node). 넓히면 id로 읽히는 노드 제목의 집합이 넓어지므로,
# 넓히는 폭은 4|8 두 값으로 한정한다.
ID_RE = r"^\d{6}-[0-9a-z]{4}-[0-9a-z]{4}(?:[0-9a-z]{4})?$"
RID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$"
CASE_RE = r"^CASE-\d{4}-\d+$"            # Mechanism §4 3항 — CASE-<연도>-<일련>
# Mechanism §2 4항 — drafter는 모델명(`fable-5`·`gpt-5.6-sol`) 또는 `user`·`agent`.
# 표면 스키마(mcp_server.Drafter)와 계약 검사가 **같은 식**을 쓴다 — 둘이 갈리면
# 표면이 거부한 값을 손 편집·내부 호출이 앉히고 검증기는 통과시킨다.
DRAFTER_RE = r"^[a-z][a-z0-9.\-]{0,39}$"
AUTHORS = ("user", "agent")              # Mechanism §2 4항


def now_kst() -> str:
    """frontmatter 시각 — 기기 시간대와 무관하게 KST로 적는다."""
    return datetime.now(_kst()).strftime(TS_FMT)


def now_iso() -> str:
    """대장 기록의 `at` — ISO 8601 유지 (Mechanism §2 2항 단서)."""
    return datetime.now(_kst()).replace(microsecond=0).isoformat()


def b36(n: int, width: int) -> str:
    s = ""
    while n:
        s = B36[n % 36] + s
        n //= 36
    return s.rjust(width, "0")


def new_node_id(when: datetime | None = None) -> str:
    """생성 시 자동 부여 (Mechanism §2 1항). 날짜·경과초는 created와 같은
    KST 시계에서 뽑는다.

    구판은 여기서 **기존 id 전수와 대조**했다. 그 대조는 frontmatter를 전부
    열어야 성립하므로 25,000 노드에서 생성 하나가 전역 잠금을 6.4초 더 쥐었다
    (실측). 대조를 싸게 하는 길은 없었다 — "오늘 만든 것만 보자"도 어느 것이
    오늘 것인지가 파일 안에 있어 성립하지 않고, 파일 mtime은 clone·복원이
    덮어써 가장 필요할 때 가장 틀린다.

    그래서 담보를 **폭**으로 옮겼다. 앞 11자가 결정적이라 충돌은 같은 초
    안에서만 일어나는데, 무작위 8자(36**8 ≈ 2**41.4)면 같은 초에 23만 건을
    부여해도 충돌이 1%에 못 미친다. 남는 위험은 사후 전수 대조가 잡고, 겹친
    id를 만나면 표면이 읽기도 쓰기도 거부한다(v3.7.2).

    대조를 되살리고 싶어지면 먼저 이것을 보라: 중복 id의 실제 발생원은 이
    함수가 아니라 **파일 복제**(사본 만들기·복원·병합의 수동 해결)이며, 그
    경로는 여기를 거치지 않는다. 대조는 그것을 막지 못했다."""
    d = when or datetime.now(_kst())
    secs = d.hour * 3600 + d.minute * 60 + d.second
    return f"{d:%y%m%d}-{b36(secs, 4)}-{''.join(random.choices(B36, k=8))}"


def sha256_file(p: Path | str) -> str:
    return "sha256:" + hashlib.sha256(Path(p).read_bytes()).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


# Windows CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP — 콘솔 창 없이, 부모와 떨어져
_DETACHED = 0x08000200


def spawn_worker(vault: Vault, module: str, *, env: dict | None = None, stdin: bytes | None = None,
                 stderr=None) -> subprocess.Popen:
    """이 vault 엔진의 모듈을 **분리 프로세스**로 띄우고 기다리지 않는다 — 훅과
    표면은 곧바로 돌아간다. 부모가 끝나도 자식은 남는다(POSIX는 새 세션, Windows는
    새 프로세스 그룹이며 콘솔 창을 만들지 않는다).

    부모의 표준 입출력은 물려주지 않는다. stdio로 도는 MCP 표면의 파이프를 자식이
    쥐면 표면의 입력을 가로채거나 표면의 종료를 붙잡는다. `stdin`을 주면 그 바이트만
    넘기고 닫는다. `stderr`는 열린 파일을 주면 거기 쓰고, 주지 않으면 버린다."""
    child_env = {**os.environ, "OSK_VAULT_ROOT": str(vault.root),
                 "PYTHONPATH": str(Path(__file__).resolve().parents[1]), **(env or {})}
    proc = subprocess.Popen(
        [sys.executable, "-m", module],
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL if stderr is None else stderr,
        env=child_env, cwd=vault.root, shell=False, start_new_session=os.name != "nt",
        creationflags=_DETACHED if os.name == "nt" else 0)
    if stdin is not None:
        try:
            proc.stdin.write(stdin)
        finally:
            proc.stdin.close()
    return proc


_PS_BARE = re.compile(r"[\w\-./\\:=+%]+")


def shell_join(argv) -> str:
    """사람이나 에이전트가 제 터미널에 붙여 넣을 **한 줄 명령** — Windows는 PowerShell,
    그 밖은 POSIX 셸의 인용이다. 경로를 문자열에 이어 붙이면 공백 든 경로가 여러
    인자로 갈린다. 특수 문자가 없는 인자는 그대로 두고, 인용한 명령 이름 앞에는
    PowerShell의 호출 연산자 `&`를 둔다."""
    argv = [str(a) for a in argv]
    if os.name != "nt":
        return shlex.join(argv)
    quoted = [a if _PS_BARE.fullmatch(a) else "'" + a.replace("'", "''") + "'" for a in argv]
    return ("& " if quoted and quoted[0] != argv[0] else "") + " ".join(quoted)


def cli_command(vault: Vault, *args: str) -> str:
    """이 vault의 `osk.cli`를 부르는 **한 줄 명령** — 에이전트가 제 셸에 그대로
    붙인다. vault 루트와 엔진 자리를 명령 안에 실으므로 작업 폴더·`PYTHONPATH`와
    무관하다."""
    code = (f"import os,runpy,sys;os.environ['OSK_VAULT_ROOT']={str(vault.root)!r};"
            f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r});"
            "runpy.run_module('osk.cli',run_name='__main__')")
    return shell_join([sys.executable, "-c", code, *args])


def posix_rel(p: Path, relative_to: Path) -> str:
    r"""경로를 **기기 비의존 표기**의 상대 경로 문자열로 — `resolve_in_root`의 역방향.

    `str(Path)`는 OS 표기를 낸다. Windows에서는 구분자가 역슬래시(`_engine\osk`)로
    나오는데, 이 체계의 규칙은 전부 슬래시로 쓰인다(매니페스트 `DENY _engine/`,
    pin 대상 `Scope/W2/`, 대장의 `path`). 그래서 OS 표기를 그대로 비교에 넘기면
    규칙이 **조용히 안 걸린다** — 실측으로 Windows에서 DENY 8개가 0건을 제외했고
    `move_node`의 pin 거부가 통과했다.

    트리는 기기 사이에서 공유되므로 키도 기록도 기기에 의존하면 안 된다.
    비교에 쓰거나 대장에 적는 경로는 전부 이 함수를 거친다."""
    return p.relative_to(relative_to).as_posix()


def resolve_in_root(vault: Vault, rel_or_abs: str | Path) -> Path | None:
    """대장·사건부에 적힌 경로 문자열을 **vault 안으로 봉쇄** 해석한다.
    대장은 다기기 병합으로 임의의 내용이 유입될 수 있는 신뢰 밖 입력이므로,
    `..`·절대 경로·심볼릭 링크로 루트를 벗어나면 None(=해석 실패)이다.
    실패는 호출부에서 언제나 거부·불일치 쪽으로 처리한다."""
    try:
        p = Path(rel_or_abs)
        if p.is_absolute() and p.is_relative_to(vault.root):
            p = Path(adapt_path(p.relative_to(vault.root).as_posix(), vault.space_roots))
        elif not p.is_absolute():
            p = Path(adapt_path(str(p), vault.space_roots))
        cand = p if p.is_absolute() else vault.root / p
        real = Path(os.path.realpath(cand))
        root = Path(os.path.realpath(vault.root))
        if real != root:
            real.relative_to(root)      # 벗어나면 ValueError
        return real
    except (ValueError, OSError, TypeError):
        return None


def resolve_within(base: Path, rel: str) -> Path | None:
    """rel을 base 안으로 봉쇄한 **정규 절대 경로** — 아니면 None. release 증빙
    key와 (다기기 병합되는) 저널 path는 **신뢰 밖 입력**이므로, 어느 I/O 전에도
    이 봉쇄를 통과한다. 두 겹으로 막는다: ①`.`/`..` segment·절대경로를 문자열
    단계에서 거부(정규화 전 판정 우회 차단 — `docs/../00_Scope/`로 바닥 재진입
    금지) ②남은 심볼릭 재배치는 realpath로 흡수해 base 안인지 확인. 반환값의
    base-상대(canonical)에만 floor·I/O를 걸어야 한다."""
    try:
        p = Path(rel)
        if not p.parts or p.is_absolute()\
                or any(seg in ("..", ".") for seg in p.parts):
            return None
        broot = Path(os.path.realpath(base))
        real = Path(os.path.realpath(base / p))
        if real == broot:
            return None                          # base 자신은 파일 대상이 아니다
        real.relative_to(broot)                  # 벗어나면 ValueError
        return real
    except (ValueError, OSError, TypeError):
        return None


def canon_rel(base: Path, rel: str) -> str | None:
    """봉쇄된 canonical base-상대 경로(posix) — 탈출·재진입·**경로 정체성 훼손**은
    None. floor·I/O 판정은 raw 문자열이 아니라 이 canonical 경로에 건다. realpath가
    lexical 경로와 다르면(경로 구성요소에 symlink) 다른 프레임워크 파일로 write가
    재지정된 것이므로 거부한다 — symlink 탈출만이 아니라 ROOT **내부** alias도 막는다
    (예: `docs/SETUP.md -> _engine/osk/core.py`). 정션과 (Windows에서는) 대소문자만 바뀐 이름도
    realpath가 다른 자리를 내므로 같이 걸린다 — 갱신과 반려가 이 계약을 공유한다."""
    p = resolve_within(base, rel)
    if p is None:
        return None
    try:
        canon = p.relative_to(Path(os.path.realpath(base))).as_posix()
    except ValueError:
        return None
    if canon != Path(rel).as_posix():            # lexical ≠ realpath → symlink 재지정
        return None
    return canon
