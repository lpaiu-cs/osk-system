"""osk.update_source — 릴리스를 받고 비준증빙과 대조한다 (Mechanism §1-2 3항).

출처는 전송만 다르고 검증은 같다: git 태그로 받든 묶음 파일로 받든, 받은 트리가 그 릴리스의
비준증빙(`release.json`)과 같은지 본다. 무엇을 적용할지는 모른다."""
from __future__ import annotations
import json, os, re, shutil, subprocess, tarfile, tempfile
from pathlib import Path
from .core import sha256_file, resolve_within
from . import update_state


def semver(tag) -> tuple[int, int, int] | None:
    """`vX.Y.Z`(VERSION_RE)의 정수 세 쌍 — 그 형식이 아니면 None. 판본의 순서는
    문자열이 아니라 이 값으로 잰다(`v4.10.0`이 `v4.9.0`보다 새롭다)."""
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag) if isinstance(tag, str) else None
    return tuple(int(g) for g in m.groups()) if m else None


def latest_release_tag(url: str, *, timeout: float = 60,
                       unattended: bool = False) -> str | None:
    """정본의 정식 릴리스 태그 중 최신 semver(`vX.Y.Z`) — 없으면 None.
    갱신의 기본은 브랜치 HEAD가 아니라 태그다(Mechanism §1-2 3항) — HEAD를
    받으면 릴리스 이후의 개발 커밋이 딸려 와 attestation과 어긋난다.

    `unattended`는 사람이 없는 확인(`update_check`의 분리 프로세스)이다.
    - 사람에게 묻지 않는다. 저장된 자격 증명은 쓰되, 없으면 조회 실패로 끝난다.
      git은 터미널에 묻기 전에 `GIT_ASKPASS`, `core.askPass`, `SSH_ASKPASS` 순으로
      인증 도우미를 띄우므로 `GIT_TERMINAL_PROMPT=0`만으로는 창이 뜬다. 두 환경
      변수를 걷어 내고 `-c core.askPass=`로 빈 도우미를 준다. git은 빈 도우미를
      실행하지 않고 `SSH_ASKPASS`로 넘어가지도 않는다. Git Credential Manager는
      `GCM_INTERACTIVE=never`면 창을 띄우지 않는다.
    - Windows에서 콘솔 창을 만들지 않는다. 창 없는 부모가 콘솔 프로그램인 git을
      띄우면 Windows가 새 콘솔을 할당한다(vault_sync가 데몬의 검은 창을 막는 규율).
    - 출력은 파이프가 아니라 임시 파일로 받는다. Windows의 `subprocess.run`은 시한을
      넘긴 자식을 죽인 뒤 파이프를 시한 없이 다시 비우는데, git이 남긴 손자(자격 증명
      도우미)가 파이프를 쥐면 그 대기가 끝나지 않는다 — overview 무기한 행과 같은
      경로다. 끝나지 않는 확인은 확인 잠금을 영영 쥔다."""
    query = ["ls-remote", "--tags", "--refs", url]
    if unattended:
        code, stdout, stderr = git_unattended(query, timeout=timeout)
    else:
        r = subprocess.run(["git", *query], capture_output=True, text=True, timeout=timeout)
        code, stdout, stderr = r.returncode, r.stdout, r.stderr
    if code != 0:
        raise update_state.UpdateError(f"정본 태그 조회 실패({url}): {stderr.strip()[-200:]}")
    best = None
    for line in stdout.splitlines():
        name = line.rsplit("refs/tags/", 1)[-1].strip()
        key = semver(name)
        if key and (best is None or key > best[0]):
            best = (key, name)
    return best[1] if best else None


def git_unattended(args: list[str], *, timeout: float) -> tuple[int, str, str]:
    """사람에게 묻지 않는 git — (종료코드, 표준 출력, 표준 오류). 규율은
    `latest_release_tag`의 `unattended`와 같다(인증 도우미·창·파이프)."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_ASKPASS", "SSH_ASKPASS")}
    env.update(LC_ALL="C", LANG="C", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        r = subprocess.run(
            ["git", "-c", "core.askPass=", *args], stdin=subprocess.DEVNULL,
            stdout=out, stderr=err, timeout=timeout, env=env,
            creationflags=0x08000000 if os.name == "nt" else 0)
        out.seek(0)
        err.seek(0)
        return (r.returncode, out.read().decode("utf-8", "replace"),
                err.read().decode("utf-8", "replace"))


def tag_exists(url: str, tag: str) -> bool:
    """정본에 `refs/tags/<tag>`가 실제로 있는가 — 명시 ref가 브랜치로
    릴리스 경계를 우회하는 것을 막는다(`--branch`는 태그·브랜치 모두 받는다)."""
    r = subprocess.run(["git", "ls-remote", "--tags", "--refs", url,
                        f"refs/tags/{tag}"], capture_output=True, text=True,
                       timeout=60)
    return r.returncode == 0 and bool(r.stdout.strip())


def fetch_git(url: str, tag: str, dest: Path) -> Path:
    """정본의 그 **태그가 가리키는 정확한 커밋**을 얕게 받는다. `clone --branch`는
    동명 브랜치(`refs/heads/<tag>`)를 대신 고를 수 있으므로 쓰지 않는다 —
    `refs/tags/<tag>`를 명시 fetch해 FETCH_HEAD(태그의 peeled 커밋)에 detached
    checkout한다. 이렇게 해야 태그 경계가 브랜치로 우회되지 않는다."""
    d = dest / "tree"
    def _g(*a, **k):
        r = subprocess.run(["git", *a], capture_output=True, text=True,
                           timeout=300, **k)
        if r.returncode != 0:
            raise update_state.UpdateError(f"정본 fetch 실패({url} {tag}): "
                              f"{r.stderr.strip()[-300:]}")
        return r
    _g("init", "-q", str(d))
    # 체크아웃 바이트를 **인스턴스의 git 설정에서 떼어 낸다.** 증빙의 해시는
    # 정본이 git object(LF)에서 뜬 값인데, 임시 저장소는 전역·시스템 설정을
    # 물려받으므로 `core.autocrlf=true`(Git for Windows 설치 기본값)인 기기에서는
    # 체크아웃이 CRLF가 되어 **모든 텍스트 파일이 해시 불일치**로 중단된다 —
    # 메시지가 "해시 불일치"라 위조·손상으로 오인된다(실측: 루트 파일 6개).
    # 로컬 설정이 전역·시스템을 이기므로 여기서 못박으면 기기와 무관해진다.
    _g("-C", str(d), "config", "core.autocrlf", "false")
    _g("-C", str(d), "config", "core.eol", "lf")
    _g("-C", str(d), "fetch", "-q", "--depth", "1", url,
       f"refs/tags/{tag}")                        # FETCH_HEAD = 태그의 커밋
    _g("-C", str(d), "checkout", "-q", "--detach", "FETCH_HEAD")
    return d


def fetch_bundle(src: str, dest: Path) -> Path:
    """로컬 반입 — 디렉터리 또는 tar(.tar/.tar.gz/.tgz). **snapshot**을 만든다:
    디렉터리 bundle을 원본 그대로 반환하면 검증 후 write 때까지 같은 파일을 다시
    읽어 TOCTOU가 열린다(검증 직후 원본이 바뀌면 미검증 bytes 적용). tar처럼
    temp tree로 복사해, 이후 검증·plan·write가 전부 이 snapshot만 보게 한다."""
    p = Path(src).expanduser()
    if p.is_dir():
        out = dest / "tree"
        shutil.copytree(p, out, symlinks=True,   # symlink는 보존 → _within이 거른다
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        return out
    if p.is_file() and p.suffix in (".tar", ".gz", ".tgz"):
        out = dest / "tree"
        out.mkdir()
        with tarfile.open(p) as tf:
            tf.extractall(out, filter="data")   # 경로 탈출 차단
        inner = [d for d in out.iterdir() if d.is_dir()]
        return inner[0] if len(inner) == 1 and not any(
            f.is_file() for f in out.iterdir()) else out
    raise update_state.UpdateError(f"bundle이 아니다(디렉터리·tar만): {src}")


# ── 비준증빙 대조 ────────────────────────────────────────────────────────
def load_release(tree: Path) -> dict:
    ap = tree / update_state.ATTESTATION
    if not ap.exists():
        raise update_state.UpdateError("정본 릴리스가 아니다 — 비준증빙(release.json)이 없다"
                          " (시행령 §10 6항)")
    try:
        rel = json.loads(ap.read_text(encoding="utf-8"))
    except ValueError as e:
        raise update_state.UpdateError(f"비준증빙 판독 실패: {e}")
    if not isinstance(rel.get("files"), dict) or not rel.get("version"):
        raise update_state.UpdateError("비준증빙 형식 위반 — version·files가 필요하다")
    if not re.match(update_state.VERSION_RE, str(rel["version"])):
        raise update_state.UpdateError(f"비준증빙 version 형식 위반(vX.Y.Z): {rel['version']}")
    return rel


def verify_attestation(tree: Path, rel: dict) -> list[str]:
    """전수 대조 — 증빙의 파일이 다 있고 해시가 맞다. 적용은 오직 증빙이
    모는 파일(`rel["files"]`)만 하고 그 하나하나를 해시로 검증하므로, 증빙
    밖에 있는 디스크 파일은 적용 자체가 되지 않는다 — 트리에 딸린 미추적
    부산물(pyc·.DS_Store)로 갱신을 막지 않는다(그건 안전이 아니라 오탐이다).

    증빙 key는 신뢰 밖 입력이다 — I/O 전에 tree 안으로 봉쇄한다(경로 탈출 차단)."""
    errs = []
    for p, h in sorted(rel["files"].items()):
        f = resolve_within(tree, p)
        if f is None:
            errs.append(f"증빙 경로가 트리 밖으로 벗어난다(경로 봉쇄 실패): {p}")
        elif not f.is_file():
            errs.append(f"증빙의 파일이 없다: {p}")
        elif sha256_file(f) != h:
            errs.append(f"해시 불일치: {p}")
    return errs
