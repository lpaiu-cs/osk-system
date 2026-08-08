"""osk.release — 정식 릴리스 선언 (정본 저장소 쪽).

구현 근거: 시행령 §10 6항(정식 릴리스는 사용자가 정본 저장소의 대화형
확인으로 선언하며, 파일 전체의 상태를 비준증빙으로 고정한다),
Mechanism §1-2 2항(release.json 형식·선언의 전제·커밋과 태그).

비준증빙 `release.json`은 저장소 루트에 두는, 릴리스에 담긴 전 파일
(자신 제외)의 경로→내용 해시 목록이다. 통치 문서는 특수한 노드이고
서명은 각 인스턴스 사용자의 **수용 기록**이지 정본의 비준·효력 요건이
아니다(헌법 3조 6항 · 시행령 §10 2항) — 정본의 비준은 이 증빙을 만드는
사용자의 대화형 확정이 담당한다. 에이전트는 보고 모드까지만 돌릴 수 있고,
`--apply`는 대화형 단말을 요구한다.

선언의 전제 (전부 fail-closed):
1. **깨끗한 작업 트리** — 증빙은 커밋과 일치해야 한다.
2. **검증기 PASS** — 깨진 트리에서 선언하지 않는다.
3. **비밀값 스캔** — 릴리스 전 파일을 훑는다 (secrets.py 자기 면제 동일).
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys
from pathlib import Path

from .core import ROOT, now_iso, sha256_file
from . import publish

ATTESTATION = "release.json"
VERSION_RE = r"^v\d+\.\d+\.\d+$"        # 릴리스·태그·updater 자동 탐색의 공통 계약


class ReleaseError(RuntimeError):
    """릴리스 중단 — 아무것도 쓰지 않는다."""


def _git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, timeout=timeout)


def tracked_files(root: Path) -> list[str]:
    """git 추적 파일 전수 — `-z`로 읽는다(비ASCII 경로, publish와 동일 이유)."""
    r = _git(root, "ls-files", "-z")
    if r.returncode != 0:
        raise ReleaseError(f"git ls-files 실패: {r.stderr.strip()}")
    return sorted(x for x in r.stdout.split("\0") if x.strip())


BLOB_MODES = ("100644", "100755")       # 정규 파일·실행 파일


def tree_hashes(root: Path, ref: str = "HEAD") -> dict[str, str]:
    """그 **커밋 트리의 실제 object**를 읽어 경로→sha256. working tree를 읽지
    않는다 — guard가 clean을 본 시점과 해시를 뜨는 시점 사이에 외부 프로세스가
    파일을 바꾸면 증빙이 커밋과 어긋나 공식 태그가 즉시 self-invalid가 된다(TOCTOU).

    `git archive`가 아니라 `ls-tree`+`cat-file`을 쓴다 — archive는 export-ignore로
    파일을 빼고 export-subst로 bytes를 바꾸며 symlink를 파일로 세지 않으므로,
    '릴리스에 담긴 전 파일'이라는 증빙의 계약과 조용히 어긋난다. 비정규 mode
    (symlink·submodule)는 지원 범위 밖이므로 선언 단계에서 거부한다."""
    r = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-z", ref],
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        raise ReleaseError(
            f"git ls-tree 실패({ref}): {r.stderr.decode('utf-8', 'ignore')[-300:]}")
    entries = []
    for rec in r.stdout.split(b"\0"):
        if not rec.strip():
            continue
        meta, _, path = rec.partition(b"\t")
        mode, typ, oid = meta.decode().split()
        p = path.decode("utf-8", "surrogateescape")
        if typ != "blob" or mode not in BLOB_MODES:
            raise ReleaseError(
                f"릴리스가 지원하지 않는 항목이다(mode={mode} type={typ}): {p} "
                f"— symlink·submodule은 증빙 계약 밖이므로 선언하지 않는다")
        entries.append((oid, p))
    if not entries:
        return {}
    # 한 프로세스로 전 blob을 읽는다: 응답은 `<oid> blob <size>\n<내용>\n`
    b = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
                       input=("\n".join(o for o, _ in entries) + "\n").encode(),
                       capture_output=True, timeout=300)
    if b.returncode != 0:
        raise ReleaseError(
            f"git cat-file 실패: {b.stderr.decode('utf-8', 'ignore')[-300:]}")
    out: dict[str, str] = {}
    buf, pos = b.stdout, 0
    for oid, p in entries:
        nl = buf.index(b"\n", pos)
        head = buf[pos:nl].decode().split()
        if len(head) != 3 or head[1] != "blob":
            raise ReleaseError(f"cat-file 응답 형식 위반: {head} ({p})")
        size = int(head[2])
        start = nl + 1
        out[p] = "sha256:" + hashlib.sha256(buf[start:start + size]).hexdigest()
        pos = start + size + 1          # 내용 뒤의 개행
    return out


def _git_show_bytes(root: Path, spec: str) -> bytes | None:
    """`git show <spec>`의 raw bytes — 없으면 None."""
    r = subprocess.run(["git", "-C", str(root), "show", spec],
                       capture_output=True, timeout=120)
    return r.stdout if r.returncode == 0 else None


def _rollback(root: Path, mine: str | None, had_att: bool,
              pre_att: bytes | None, ap: Path) -> tuple[bool, str]:
    """선언 실패 시 되돌린다 — (성공?, 사유). **자신이 만든 커밋만** 제거하며,
    그 사이 외부 커밋이 쌓였으면 ref를 건드리지 않는다(남의 커밋 보호). 되돌림에
    실패하면 성공을 주장하지 않는다."""
    if mine:                                  # 커밋까지 갔다면
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        if head != mine:
            return False, "그 사이 다른 커밋이 쌓여 릴리스 커밋을 되돌리지 않았다"
        r = _git(root, "reset", "--mixed", "-q", f"{mine}^")
        if r.returncode != 0:
            return False, f"reset 실패: {r.stderr.strip()[-200:]}"
    # 커밋 전에 실패했어도 우리가 stage한 증빙은 index에 남는다 — **경로를 한정해**
    # 그것만 되돌린다(전역 reset은 그 사이 남이 stage한 것까지 지운다).
    r = _git(root, "reset", "-q", "--", ATTESTATION)
    if r.returncode != 0:
        return False, f"증빙 unstage 실패: {r.stderr.strip()[-200:]}"
    try:                                      # 우리가 만든 증빙 파일만 원상
        if had_att and pre_att is not None:
            ap.write_bytes(pre_att)
        else:
            ap.unlink(missing_ok=True)
    except OSError as e:
        return False, f"증빙 파일 원상복구 실패: {e}"
    return True, ""


def build_attestation(root: Path, version: str) -> dict:
    """비준증빙 — **커밋된 트리**의 전 파일(자신 제외)의 경로→sha256
    (Mechanism §1-2 2항)."""
    files = {k: v for k, v in tree_hashes(root).items() if k != ATTESTATION}
    return {"version": version, "at": now_iso(), "files": files}


def _validate_at(root: Path) -> list[str]:
    """release는 **자신이 릴리스하는 트리**를 검증한다. validate는 core.ROOT
    전역에 묶여 있으므로(임포트 시점 고정), fixture_signature_lifecycle와
    같은 선례로 OSK_VAULT_ROOT를 건 별도 프로세스에서 돌린다 — 이렇게 해야
    guards의 세 전제가 모두 같은 root를 본다(비밀값·깨끗함·검증기 정합)."""
    engine = Path(__file__).resolve().parent.parent          # <repo>/_governance/_engine
    code = ("import json; from osk import validate; r = validate.run(); "
            "print(json.dumps({'v': r['verdict'], "
            "'f': [list(x)[0] for x in r['fail']]}))")
    env = dict(os.environ, OSK_VAULT_ROOT=str(root), PYTHONPATH=str(engine))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, timeout=120)
    if not r.stdout.strip():
        return [f"검증기 실행 실패: {r.stderr.strip()[-300:]}"]
    try:
        rep = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return [f"검증기 출력 파싱 실패: {r.stdout[-200:]!r}"]
    return [] if rep["v"] == "PASS" else [f"검증기 {rep['v']}: {'; '.join(rep['f'])}"]


def guards(root: Path) -> list[str]:
    errs = []
    r = _git(root, "status", "--porcelain", "-z")
    # release.json도 예외 없이 clean을 요구한다 — 면제하면 선언 전 로컬 수정·
    # untracked release.json이 있을 때 실패 롤백(reset --hard + clean)이 그
    # 선언 전 상태까지 지워 '원상복구'가 아니게 된다(P2). 완전 clean tree를
    # 요구하면 롤백 규칙이 단순·정확해진다.
    dirty = [e for e in r.stdout.split("\0") if e.strip()]
    if dirty:
        errs.append(f"작업 트리가 깨끗하지 않다 — 증빙은 커밋과 일치해야 한다: "
                    f"{[d[3:] for d in dirty[:5]]}")
    items = [(root / rel, rel) for rel in tracked_files(root)]
    errs += publish.guard_secrets(items)
    errs += _validate_at(root)
    return errs


def run(version: str, apply: bool = False, root: Path | None = None) -> dict:
    root = root or ROOT
    # 정확히 vX.Y.Z만 — updater의 자동 탐색(같은 정규식)이 인정하는 형식이어야
    # 릴리스가 영영 후보에서 누락되지 않고, git이 거부하는 태그도 원천 차단된다.
    if not re.match(VERSION_RE, version):
        raise ReleaseError(f"버전은 정확히 vX.Y.Z 형식이어야 한다: {version}")
    r = _git(root, "tag", "-l", version)
    if r.stdout.strip():
        raise ReleaseError(f"이미 선언된 버전이다 — 버전은 불변이다: {version}")
    errs = guards(root)
    if errs:
        raise ReleaseError("릴리스 전제 위반 — 선언하지 않았다:\n  "
                           + "\n  ".join(errs))
    att = build_attestation(root, version)
    prev = None
    ap = root / ATTESTATION
    if ap.exists():
        try:
            prev = json.loads(ap.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = None
    # 직전 릴리스 대비 added/changed/**removed** — 삭제는 updater가 인스턴스까지
    # 전파하므로(§1-2 4항), 선언 전 변화 요약에서 빠지면 안 된다(P3). prev/new
    # key의 합집합으로 본다.
    added = changed = removed = None
    if prev and isinstance(prev.get("files"), dict):
        pf, nf = prev["files"], att["files"]
        added = sorted(k for k in nf if k not in pf)
        changed = sorted(k for k in nf if k in pf and pf[k] != nf[k])
        removed = sorted(k for k in pf if k not in nf)
    out = {"ok": True, "applied": False, "version": version,
           "files": len(att["files"]),
           "prev_version": prev.get("version") if prev else None,
           "added": added, "changed": changed, "removed": removed}
    if not apply:
        return out
    # mutation 전체를 하나의 rollback 단위로 잡는다 — write·add·commit·재대조·tag
    # 어느 단계가 실패하든 선언 전으로 되돌려 '아무것도 쓰지 않는다'를 지킨다.
    # 되돌림은 **자신이 만든 커밋만** 대상이다: HEAD가 아직 그 커밋일 때에만
    # 그 부모로 `--mixed` 되돌리고, 그 사이 외부 커밋이 쌓였으면 손대지 않는다
    # (남의 커밋을 ref에서 떨어뜨리지 않는다). 되돌리지 못하면 성공을 주장하지
    # 않고 그 사실을 실패에 담는다(fail-closed).
    had_att = ap.exists()
    pre_att = ap.read_bytes() if had_att else None
    mine: str | None = None                 # 우리가 만든 릴리스 커밋 sha
    try:
        ap.write_text(json.dumps(att, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "--", ATTESTATION],
                       check=True, timeout=60)
        # `--no-verify`: commit hook이 다른 tracked 파일을 수정·stage해 커밋
        # 트리가 증빙과 어긋난 self-invalid 릴리스를 만드는 것을 막는다.
        # 릴리스 커밋은 기계적이므로 hook을 태울 이유가 없다.
        subprocess.run(["git", "-C", str(root), "commit", "-q", "--no-verify",
                        "-m", f"release: {version} — 비준증빙"],
                       check=True, timeout=60)
        mine = _git(root, "rev-parse", "HEAD").stdout.strip() or None
        # **태그 전 전수 재대조** — 커밋된 트리가 증빙과 정확히 일치하는지 본다.
        # 증빙 **자체의 bytes**도 대조한다: add 직후 다른 프로세스가 release.json만
        # 변조해 다시 stage하면 나머지 파일은 그대로라 존재 확인만으로는 통과한다.
        committed_att = _git_show_bytes(root, f"HEAD:{ATTESTATION}")
        if committed_att is None:
            raise ReleaseError("증빙이 릴리스 커밋에 담기지 않았다")
        if json.loads(committed_att.decode("utf-8")) != att:
            raise ReleaseError(
                "커밋된 증빙이 선언한 증빙과 다르다 — 선언 중단(증빙 변조?)")
        committed_tree = tree_hashes(root, "HEAD")
        committed_tree.pop(ATTESTATION, None)
        # 기준은 **커밋에 들어간 증빙**이다(메모리 사본이 아니라).
        want = json.loads(committed_att.decode("utf-8"))["files"]
        if committed_tree != want:
            diff = sorted(set(committed_tree) ^ set(want)) or [
                k for k in want if committed_tree.get(k) != want[k]]
            raise ReleaseError(
                f"커밋 트리가 증빙과 다르다 — 선언 중단(외부 수정 유입?): "
                f"{diff[:5]}")
        r = _git(root, "tag", version)
        if r.returncode != 0:
            raise ReleaseError(f"태그 실패: {r.stderr.strip()}")
    except (subprocess.SubprocessError, ReleaseError, OSError, ValueError) as e:
        undone, why = _rollback(root, mine, had_att, pre_att, ap)
        state = ("선언 전으로 원상복구했다" if undone
                 else f"**원상복구하지 못했다 — 수동 확인 필요**({why})")
        raise ReleaseError(f"릴리스 mutation 실패 — {state}: {e}")
    out.update(applied=True, tagged=version)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="osk-release",
        description="정식 릴리스 선언 — 기본은 보고, 선언은 --apply (대화형 전속)")
    ap.add_argument("--version", required=True, help="vX.Y.Z")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.apply and not sys.stdin.isatty():
        sys.exit("릴리스 선언은 사용자의 비준 행위다 — 대화형 단말에서만 한다"
                 " (시행령 §10 6항)")
    try:
        rep = run(a.version, a.apply)
    except ReleaseError as e:
        sys.exit(f"[중단] {e}")
    print(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
