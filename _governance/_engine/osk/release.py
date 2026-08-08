"""osk.release — 정식 릴리스 선언 (정본 저장소 쪽).

구현 근거: 시행령 §10 6항(정식 릴리스는 사용자가 정본 저장소의 대화형
확인으로 선언하며, 파일 전체의 상태를 비준증빙으로 고정한다),
Mechanism §1-2 2항(release.json 형식·선언의 전제·커밋과 태그).

비준증빙 `release.json`은 저장소 루트에 두는, 릴리스에 담긴 전 파일
(자신 제외)의 경로→내용 해시 목록이다. 통치 문서는 서명 제도의 대상이
아니므로(헌법 3조 6항), 규범의 확인은 이 증빙을 만드는 사용자의 대화형
선언이 담당한다 — 에이전트는 보고 모드까지만 돌릴 수 있고, `--apply`는
대화형 단말을 요구한다.

선언의 전제 (전부 fail-closed):
1. **깨끗한 작업 트리** — 증빙은 커밋과 일치해야 한다.
2. **검증기 PASS** — 깨진 트리에서 선언하지 않는다.
3. **비밀값 스캔** — 릴리스 전 파일을 훑는다 (secrets.py 자기 면제 동일).
"""
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path

from .core import ROOT, now_iso, sha256_file
from . import publish, validate

ATTESTATION = "release.json"


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


def guards(root: Path) -> list[str]:
    errs = []
    r = _git(root, "status", "--porcelain", "-z")
    dirty = [e for e in r.stdout.split("\0")
             if e.strip() and not e[3:].startswith(ATTESTATION)]
    if dirty:
        errs.append(f"작업 트리가 깨끗하지 않다 — 증빙은 커밋과 일치해야 한다: "
                    f"{[d[3:] for d in dirty[:5]]}")
    items = [(root / rel, rel) for rel in tracked_files(root)]
    errs += publish.guard_secrets(items)
    errs += publish.guard_vault()
    return errs


def run(version: str, apply: bool = False, root: Path | None = None) -> dict:
    root = root or ROOT
    if not version.startswith("v"):
        raise ReleaseError(f"버전은 vX.Y.Z 형식이다: {version}")
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
    changed = None
    if prev and isinstance(prev.get("files"), dict):
        changed = sorted(p for p, h in att["files"].items()
                         if prev["files"].get(p) != h)
    out = {"ok": True, "applied": False, "version": version,
           "files": len(att["files"]),
           "prev_version": prev.get("version") if prev else None,
           "changed_since_prev": changed}
    if not apply:
        return out
    ap.write_text(json.dumps(att, ensure_ascii=False, indent=1) + "\n",
                  encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--", ATTESTATION],
                   check=True, timeout=60)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m",
                    f"release: {version} — 비준증빙"], check=True, timeout=60)
    r = _git(root, "tag", version)
    if r.returncode != 0:
        raise ReleaseError(f"태그 실패(이미 있는 버전?): {r.stderr.strip()}")
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
