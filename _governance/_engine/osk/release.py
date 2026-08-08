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
import argparse, json, os, re, subprocess, sys
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


def build_attestation(root: Path, version: str) -> dict:
    """비준증빙 — 추적 파일 전수(자신 제외)의 경로→sha256 (Mechanism §1-2 2항)."""
    files = {}
    for rel in tracked_files(root):
        if rel == ATTESTATION:
            continue
        files[rel] = sha256_file(root / rel)
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
    # 면제는 **정확히** release.json만 — `startswith`면 release.json.bak 같은
    # tracked 파일의 수정도 dirty에서 빠져, 증빙엔 그 수정 hash가 들어가는데
    # 커밋엔 안 담기는 self-invalid 릴리스가 된다(P2).
    dirty = [e for e in r.stdout.split("\0")
             if e.strip() and e[3:] != ATTESTATION]
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
    # mutation 전체를 하나의 rollback 단위로 잡는다 — write·add·commit·tag 어느
    # 단계가 실패하든(commit hook·서명·index 오류 포함) 선언 전 상태로 되돌려
    # '아무것도 쓰지 않는다'를 지킨다. guards가 clean tree를 요구하므로 선언 전
    # HEAD로의 reset --hard가 정확한 원상복구다(새 증빙은 clean으로 함께 제거).
    pre = _git(root, "rev-parse", "HEAD").stdout.strip()
    try:
        ap.write_text(json.dumps(att, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "--", ATTESTATION],
                       check=True, timeout=60)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m",
                        f"release: {version} — 비준증빙"], check=True, timeout=60)
        r = _git(root, "tag", version)
        if r.returncode != 0:
            raise ReleaseError(f"태그 실패: {r.stderr.strip()}")
    except (subprocess.SubprocessError, ReleaseError, OSError) as e:
        if pre:
            _git(root, "reset", "--hard", pre)
        _git(root, "clean", "-fdq", "--", ATTESTATION)
        raise ReleaseError(f"릴리스 mutation 실패 — 선언 전으로 원상복구했다: {e}")
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
