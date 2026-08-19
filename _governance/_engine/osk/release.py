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
import argparse, hashlib, json, os, re, subprocess, sys, tempfile
from pathlib import Path

from .core import ROOT, now_iso
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


# 지원 mode는 정규 파일 하나뿐이다. 증빙은 **내용 해시만** 싣고 갱신·복구도
# mode를 실어 나르지 않으므로, `100755`를 받아들이면 실행 비트가 조용히 사라지고
# (updater의 원자 교체는 새 파일을 만든다) content가 같은 mode 변경은 아예
# 감지되지 않는다. 계약을 넓히는 대신 지원 범위를 좁혀 선언 단계에서 거부한다.
BLOB_MODES = ("100644",)


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


def build_attestation(root: Path, version: str, ref: str = "HEAD") -> dict:
    """비준증빙 — **그 커밋 트리**의 전 파일(자신 제외)의 경로→sha256
    (Mechanism §1-2 2항). `ref`는 선언 전체가 공유하는 고정 스냅샷이다."""
    files = {k: v for k, v in tree_hashes(root, ref).items()
             if k != ATTESTATION}
    return {"version": version, "at": now_iso(), "files": files}


def _validate_at(root: Path) -> list[str]:
    """release는 **자신이 릴리스하는 트리**를 그 트리의 **엔진으로** 검증한다.

    데이터만 스냅샷을 보고 검증기 코드는 원본 작업 트리에서 import하면, 선언
    도중 외부가 엔진을 고쳤을 때 "스냅샷을 그 스냅샷의 규칙으로 검증했다"가
    성립하지 않는다. 그래서 `PYTHONPATH`도 스냅샷 안의 엔진을 가리킨다.
    (validate는 core.ROOT 전역에 묶이므로 별도 프로세스여야 한다 —
    fixture_approval_lifecycle와 같은 선례.)"""
    engine = root / "_governance" / "_engine"
    if not (engine / "osk").is_dir():
        return [f"스냅샷에 엔진이 없다 — 검증 불성립: {engine}"]
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


def guards(root: Path, base: str) -> list[str]:
    """선언의 전제를 **고정 커밋 스냅샷 `base`에서** 검사한다.

    비밀값 스캔과 검증기가 mutable working tree를 보면, `git status`가 clean을
    본 직후 외부 프로세스가 파일을 바꿔도 그 수정본이 PASS하고 증빙은 원래
    커밋을 증빙하는 어긋남이 생긴다("검증을 통과한 바로 그 릴리스"가 성립하지
    않는다). 그래서 `base`를 임시 detached worktree로 펼쳐 그 트리에서 검사한다."""
    errs = []
    r = _git(root, "status", "--porcelain", "-z")
    # release.json도 예외 없이 clean을 요구한다 — 작업 트리가 base와 같아야
    # 사용자가 보고 선언한 것과 실제로 증빙되는 것이 일치한다.
    dirty = [e for e in r.stdout.split("\0") if e.strip()]
    if dirty:
        errs.append(f"작업 트리가 깨끗하지 않다 — 증빙은 커밋과 일치해야 한다: "
                    f"{[d[3:] for d in dirty[:5]]}")
        return errs                     # 스냅샷 검사 전에 이미 전제가 깨졌다
    with tempfile.TemporaryDirectory(prefix="osk-rel-") as td:
        snap = Path(td) / "snap"
        w = _git(root, "worktree", "add", "--detach", "-q", str(snap), base,
                 timeout=300)
        if w.returncode != 0:
            return [f"릴리스 스냅샷 생성 실패: {w.stderr.strip()[-300:]}"]
        try:
            items = [(snap / rel, rel) for rel in tracked_files(snap)]
            errs += publish.guard_secrets(items)
            errs += _validate_at(snap)
        finally:
            _git(root, "worktree", "remove", "--force", str(snap), timeout=120)
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
    # 선언 전체가 **하나의 (브랜치, 커밋) identity**를 기준으로 돈다 — 전제
    # 검사·증빙·커밋·설치·태그가 모두 여기에 묶인다. 브랜치를 나중에 읽으면
    # 그 사이 외부가 `git switch` 했을 때 **다른 브랜치에 릴리스가 설치**된다.
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not branch:
        raise ReleaseError("detached HEAD에서는 선언하지 않는다 — 브랜치가 필요하다")
    base = _git(root, "rev-parse", "HEAD").stdout.strip()
    if not base:
        raise ReleaseError("HEAD를 읽지 못했다 — 커밋이 없는 저장소인가")
    bref = _git(root, "rev-parse", f"refs/heads/{branch}").stdout.strip()
    if bref != base:
        raise ReleaseError(f"브랜치 {branch}가 HEAD와 어긋난다 — 선언하지 않는다")
    errs = guards(root, base)
    if errs:
        raise ReleaseError("릴리스 전제 위반 — 선언하지 않았다:\n  "
                           + "\n  ".join(errs))
    att = build_attestation(root, version, base)
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
    # 릴리스 커밋은 **working tree·index를 건드리지 않고** object로 만든다:
    # blob → 임시 index로 tree → commit-tree(-p base) → update-ref CAS.
    # 이렇게 하면 (a) 태그가 **검증한 정확한 SHA**에 붙고, (b) 그 사이 외부
    # 커밋이 들어오면 CAS가 실패해 아무것도 남지 않으며, (c) 선언과 무관한
    # 외부 수정·index를 애초에 만지지 않으므로 파괴할 것이 없다.
    att_bytes = (json.dumps(att, ensure_ascii=False, indent=1) + "\n").encode()
    installed = tagged = False
    try:
        blob = subprocess.run(["git", "-C", str(root), "hash-object", "-w",
                               "--stdin"], input=att_bytes,
                              capture_output=True, timeout=120)
        if blob.returncode != 0:
            raise ReleaseError(f"증빙 blob 생성 실패: {blob.stderr.decode()[-200:]}")
        blob_sha = blob.stdout.decode().strip()
        with tempfile.TemporaryDirectory(prefix="osk-idx-") as td:
            env = dict(os.environ, GIT_INDEX_FILE=str(Path(td) / "index"))

            def _g(*args, timeout=120):
                return subprocess.run(["git", "-C", str(root), *args],
                                      capture_output=True, text=True,
                                      env=env, timeout=timeout)
            r = _g("read-tree", base)
            if r.returncode != 0:
                raise ReleaseError(f"read-tree 실패: {r.stderr.strip()[-200:]}")
            r = _g("update-index", "--add", "--cacheinfo",
                   f"100644,{blob_sha},{ATTESTATION}")
            if r.returncode != 0:
                raise ReleaseError(f"update-index 실패: {r.stderr.strip()[-200:]}")
            r = _g("write-tree")
            if r.returncode != 0:
                raise ReleaseError(f"write-tree 실패: {r.stderr.strip()[-200:]}")
            tree_sha = r.stdout.strip()
        r = _git(root, "commit-tree", tree_sha, "-p", base,
                 "-m", f"release: {version} — 비준증빙")
        if r.returncode != 0:
            raise ReleaseError(f"commit-tree 실패: {r.stderr.strip()[-200:]}")
        new = r.stdout.strip()
        # **태그 전 전수 재대조** — 우리가 만든 커밋이지만, 구성 실수·object 이상을
        # 잡기 위해 커밋된 증빙 bytes와 트리를 다시 대조한다(기준은 커밋 안의 증빙).
        committed_att = _git_show_bytes(root, f"{new}:{ATTESTATION}")
        if committed_att is None:
            raise ReleaseError("증빙이 릴리스 커밋에 담기지 않았다")
        if json.loads(committed_att.decode("utf-8")) != att:
            raise ReleaseError(
                "커밋된 증빙이 선언한 증빙과 다르다 — 선언 중단(증빙 변조?)")
        committed_tree = tree_hashes(root, new)
        committed_tree.pop(ATTESTATION, None)
        want = json.loads(committed_att.decode("utf-8"))["files"]
        if committed_tree != want:
            diff = sorted(set(committed_tree) ^ set(want)) or [
                k for k in want if committed_tree.get(k) != want[k]]
            raise ReleaseError(
                f"커밋 트리가 증빙과 다르다 — 선언 중단(외부 수정 유입?): "
                f"{diff[:5]}")
        # 설치 직전 HEAD가 여전히 선언을 시작한 그 브랜치인지 확인한다 —
        # 그 사이 외부가 `git switch` 했으면 다른 브랜치에 설치될 수 있다.
        now = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        if now != branch:
            raise ReleaseError(
                f"선언 도중 브랜치가 바뀌었다({branch} → {now or 'detached'}) — 중단")
        # 브랜치 설치는 **CAS**다 — 그 사이 남이 커밋했으면 실패하고 아무것도
        # 남지 않는다(남의 커밋을 덮지도, 떨어뜨리지도 않는다).
        r = _git(root, "update-ref", f"refs/heads/{branch}", new, base)
        if r.returncode != 0:
            raise ReleaseError(
                f"브랜치 갱신(CAS) 실패 — 그 사이 다른 커밋이 들어왔다: "
                f"{r.stderr.strip()[-200:]}")
        installed = True
        # 태그도 **CAS**로 만든다: 빈 old-value는 "그 ref가 없을 때만"이다.
        # `git tag`는 이 소유권 검사가 없어, 그 사이 남이 같은 이름을 만들면
        # 실패 후 rollback이 **남의 태그를 지울** 수 있다.
        r = _git(root, "update-ref", f"refs/tags/{version}", new, "")
        if r.returncode != 0:
            raise ReleaseError(
                f"태그 생성(CAS) 실패 — 그 사이 같은 이름이 생겼다: "
                f"{r.stderr.strip()[-200:]}")
        tagged = True
    except (subprocess.SubprocessError, ReleaseError, OSError, ValueError) as e:
        why = []
        if tagged:                      # 우리가 만든 태그일 때만 지운다(CAS)
            rt = _git(root, "update-ref", "-d", f"refs/tags/{version}", new)
            if rt.returncode != 0:
                why.append(f"태그 되돌리기 실패: {rt.stderr.strip()[-160:]}")
        if installed:                   # 설치까지 갔으면 CAS로 되돌린다
            rb = _git(root, "update-ref", f"refs/heads/{branch}", base, new)
            if rb.returncode != 0:
                why.append(f"브랜치 되돌리기 실패: {rb.stderr.strip()[-160:]}")
        state = ("선언 전으로 원상복구했다" if not why
                 else f"**원상복구하지 못했다 — 수동 확인 필요**({'; '.join(why)})")
        raise ReleaseError(f"릴리스 선언 실패 — {state}: {e}")
    out.update(applied=True, tagged=version, commit=new)
    # 작업 트리·색인은 **건드리지 않는다.** 릴리스는 커밋과 태그로 이미 원자적으로
    # 성립했고, 그 뒤의 자동 동기화는 "확인 → 쓰기" 사이의 창을 없앨 수단이 없다
    # (git에는 작업 트리 CAS가 없고, 외부 git 명령에는 잠금도 통하지 않는다).
    # 확인을 아무리 앞당겨도 그 사이 `git switch`나 외부 수정이 끼어들면 우리가
    # 그것을 덮게 되므로, **강제할 수 없는 보장을 강제한 척하지 않고** 맞추는 일은
    # 사용자에게 남긴다(명령을 보고에 싣는다).
    out["worktree_sync"] = ("미수행 — 릴리스는 커밋·태그로 성립했다. 작업 트리를 "
                            f"맞추려면: git checkout {version} -- {ATTESTATION}")
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
