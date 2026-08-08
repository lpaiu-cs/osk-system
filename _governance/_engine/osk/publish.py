"""osk.publish — 공개 미러 발행 (v2).

구현 근거: Mechanism §1 1항(공개 repo는 발행 미러이며 범위는 allowlist가 정한다),
헌법 10조(서명은 사용자 전속), 시행령 §11(실패는 보류·보고).

**왜 다시 짰는가.** 구 절차(`sync-template.sh`)는 사설 커밋을 공개 저장소로
cherry-pick하는 방식이었고, 그것은 두 저장소의 **경로가 같다**는 전제 위에
있었다. v2.1에서 통치 구획이 사설·공개 모두 `_governance/`로 같아져 사상이
`_governance/`에 있어 경로가 사상된다 — cherry-pick으로는 표현할 수 없다.
그래서 v2는 **사설 트리에서 공개 트리를 빌드하는 export 모델**이다.

발행은 되돌리기 어려운 바깥 행위다. 그래서 이 모듈의 기본 동작은 **보고**이고,
쓰기는 명시 인자로만 한다. 가드는 전부 fail-closed다 — 하나라도 걸리면
아무것도 쓰지 않는다.

가드 (모두 통과해야 발행):
1. **매니페스트 밖 금지** — 나가는 모든 파일은 MAP이 사상한 것이어야 한다.
2. **DENY 조각** — 대장·`_raw`·`_sources`·부산물은 대상 안이라도 제외.
3. **지식 유출 금지** — 노드형(frontmatter) 파일은 발행 대상 어디에도 없어야
   한다. 통치 문서는 노드가 아니므로 통치 구획도 예외가 아니다(Mechanism §1 4항).
4. **비밀값** — 나가는 전 파일을 `secrets.PATTERNS`로 훑는다.
5. **검증기 PASS** — 깨진 vault에서 발행하지 않는다.

통치 문서의 비준은 서명 제도가 아니라 정본 저장소에 대한 사용자의 확정이다
(헌법 14조 1항·시행령 §10 2항) — 발행에 서명 가드는 없고, 규범의 고정은
정식 릴리스의 비준증빙이 맡는다(시행령 §10 6항).
"""
from __future__ import annotations
import argparse, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

from .core import ROOT, posix_rel
from . import secrets, validate

MANIFEST = ROOT / "_governance" / "_engine" / "scripts" / "publish-manifest.txt"


class PublishError(RuntimeError):
    """발행 중단. 어느 단계에서 걸려도 공개 저장소에는 아무것도 쓰지 않는다."""


def parse_manifest(path: Path = MANIFEST) -> dict:
    """MAP·DENY·SKEL 세 갈래. 코드를 읽지 않고 이 파일만 보면 무엇이 나가는지
    알 수 있어야 하므로, 형식은 일부러 단순하다."""
    maps, deny, skel, keep = [], [], [], []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        kind, _, rest = line.partition(" ")
        rest = rest.strip()
        if kind == "MAP":
            src, arrow, dst = rest.partition("->")
            if not arrow:
                raise PublishError(f"매니페스트 {path}:{i} — MAP에 '->'가 없다")
            maps.append((src.strip(), dst.strip()))
        elif kind == "DENY":
            deny.append(rest)
        elif kind == "SKEL":
            skel.append(rest)
        elif kind == "KEEP":
            keep.append(rest)
        else:
            raise PublishError(f"매니페스트 {path}:{i} — 미정의 지시어 {kind!r}")
    if not maps:
        raise PublishError("매니페스트에 MAP이 없다 — 발행할 것이 없다")
    return {"map": maps, "deny": deny, "skel": skel, "keep": keep}


def _denied(rel: str, deny: list[str]) -> str | None:
    for d in deny:
        if d in rel or rel.endswith(d.rstrip("/")):
            return d
    return None


def collect(man: dict) -> list[tuple[Path, str]]:
    """(사설 절대경로, 공개 상대경로) 목록. DENY에 걸리는 것은 여기서 빠진다."""
    out: list[tuple[Path, str]] = []
    for src, dst in man["map"]:
        s = ROOT / src.rstrip("/")
        if src.endswith("/"):
            if not s.is_dir():
                raise PublishError(f"MAP 출처가 디렉터리가 아니다: {src}")
            for p in sorted(s.rglob("*")):
                if not p.is_file():
                    continue
                rel = posix_rel(p, s)
                if _denied(rel, man["deny"]):
                    continue
                out.append((p, dst.rstrip("/") + "/" + rel))
        else:
            if not s.is_file():
                raise PublishError(f"MAP 출처가 파일이 아니다: {src}")
            if not _denied(src, man["deny"]):
                out.append((s, dst))
    return out


# ── 가드 ─────────────────────────────────────────────────────────────────

def guard_knowledge(items: list[tuple[Path, str]]) -> list[str]:
    """노드형(frontmatter) 파일이 나가면 지식 코퍼스 유출이다. 공개 미러는
    프레임워크이지 이 인스턴스의 지식이 아니며, 통치 구획도 노드를 두지
    않으므로(Mechanism §1 4항) 예외 경로가 없다."""
    errs = []
    for src, dst in items:
        if src.suffix != ".md":
            continue
        try:
            head = src.read_text(encoding="utf-8", errors="ignore")[:4]
        except OSError:
            continue
        if head.startswith("---\n"):
            errs.append(f"노드형 파일이 발행 대상에 있다: {dst}")
    return errs


def guard_secrets(items: list[tuple[Path, str]]) -> list[str]:
    """나가는 전 파일을 훑는다. 보고에는 경로와 패턴 이름만 싣는다 —
    비밀값 자체는 어디에도 적지 않는다(Mechanism §9)."""
    errs = []
    for src, dst in items:
        if src.name == "secrets.py" and "osk" in src.parts:
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            errs.append(f"{dst}: 판독 실패 {e}")
            continue
        _, hits = secrets.filter_text(text)
        if hits:
            errs.append(f"{dst}: 비밀값 패턴 {sorted(set(hits))}")
    return errs


def guard_vault() -> list[str]:
    """깨진 vault에서 발행하지 않는다."""
    try:
        rep = validate.run()
    except Exception as e:
        return [f"검증기 실행 실패: {e}"]
    if rep["verdict"] != "PASS":
        return [f"검증기 {rep['verdict']}: "
                + "; ".join(list(f)[0] for f in rep["fail"])]
    return []


# ── 빌드·비교 ────────────────────────────────────────────────────────────

def build(items: list[tuple[Path, str]], man: dict, dest: Path) -> None:
    for src, rel in items:
        t = dest / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, t)
    for s in man["skel"]:
        d = dest / s.rstrip("/")
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gitkeep").write_text("", encoding="utf-8")


def _git_paths(public: Path, *args: str) -> set[str]:
    """git이 내놓는 경로 목록을 읽는다. **반드시 `-z`로** 읽어야 한다 —
    기본 출력은 core.quotePath 때문에 비ASCII 이름을 따옴표와 8진 이스케이프로
    감싸므로(`"\354\270\265.md"`), 그 문자열은 실제 경로와 같지 않다. 그러면
    한글 이름 파일이 want와 영원히 어긋나 매번 remove로 잡히고, 스테이지도
    삭제도 조용히 빗나간다."""
    r = subprocess.run(["git", "-C", str(public), *args, "-z"],
                       capture_output=True, text=True, timeout=30)
    return {l for l in r.stdout.split("\0") if l.strip()}


def _tracked(public: Path) -> set[str]:
    return _git_paths(public, "ls-files")


def _untracked(public: Path) -> set[str]:
    return _git_paths(public, "ls-files", "--others", "--exclude-standard")


def plan(public: Path, man: dict, items: list[tuple[Path, str]]) -> dict:
    """무엇이 더해지고 바뀌고 **사라지는가**. 사라지는 것을 보여주는 것이
    중요하다 — 매니페스트에서 빠진 경로는 공개에서 지워지기 때문이다."""
    want = {rel for _s, rel in items}
    want |= {s.rstrip("/") + "/.gitkeep" for s in man["skel"]}
    want |= set(man["keep"])          # 공개 전용 — 사상 대상은 아니나 지우지 않는다
    have = _tracked(public)
    add, gone, same, diff = [], sorted(have - want), [], []
    for src, rel in items:
        cur = public / rel
        if not cur.exists():
            add.append(rel)
        elif cur.read_bytes() == src.read_bytes():
            same.append(rel)
        else:
            diff.append(rel)
    # 매니페스트가 통제하지 않는데 디스크에 있는 파일. 발행은 이들을 커밋하지도
    # 지우지도 않는다 — 다만 미러가 빌드 결과와 다르다는 사실은 보여야 한다.
    return {"add": sorted(add), "change": sorted(diff),
            "remove": gone, "same": len(same),
            "stray": sorted(_untracked(public) - want)}


def run(public: Path, apply: bool = False, push: bool = False,
        message: str | None = None, manifest: Path | None = None) -> dict:
    """기본은 보고다. `apply`가 있어야 공개 트리에 쓰고, `push`가 있어야 올린다."""
    if not (public / ".git").exists():
        raise PublishError(f"공개 저장소가 아니다: {public}")
    man = parse_manifest(manifest or MANIFEST)
    items = collect(man)
    errs = guard_knowledge(items) + guard_secrets(items) + guard_vault()
    if errs:
        raise PublishError("발행 가드 위반 — 아무것도 쓰지 않았다:\n  "
                           + "\n  ".join(errs))
    p = plan(public, man, items)
    if not apply:
        return {"ok": True, "applied": False, "files": len(items), **p}
    build(items, man, public)
    for rel in p["remove"]:
        (public / rel).unlink(missing_ok=True)
    # **매니페스트가 통제하는 경로만** 스테이지한다. `git add -A`는 디스크에
    # 남아 있는 아무 파일이나 함께 커밋해 위의 가드를 통째로 우회한다 —
    # 실제로 v1 잔재(지식 노드 포함)가 그렇게 들어간 적이 있다.
    controlled = sorted({rel for _s, rel in items}
                        | {s.rstrip("/") + "/.gitkeep" for s in man["skel"]}
                        | set(man["keep"]) | set(p["remove"]))
    for rel in controlled:
        subprocess.run(["git", "-C", str(public), "add", "--all", "--", rel],
                       check=False, timeout=60)
    staged = _git_paths(public, "diff", "--cached", "--name-only")
    stray = sorted(staged - set(controlled))
    if stray:
        subprocess.run(["git", "-C", str(public), "reset", "-q"], timeout=60)
        raise PublishError(
            "매니페스트 밖 파일이 스테이지됐다 — 발행하지 않았다: " + str(stray[:10]))
    if not staged:
        return {"ok": True, "applied": True, "committed": False,
                "note": "변경 없음", "files": len(items), **p}
    subprocess.run(["git", "-C", str(public), "commit", "-q", "-m",
                    message or "governance: 발행 갱신"], check=True, timeout=60)
    out = {"ok": True, "applied": True, "committed": True,
           "files": len(items), **p}
    if push:
        subprocess.run(["git", "-C", str(public), "push", "-q", "origin", "main"],
                       check=True, timeout=180)
        out["pushed"] = True
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="osk-publish",
        description="공개 미러 발행 — 기본은 보고, 쓰기는 --apply, 올리기는 --push")
    ap.add_argument("--public", default=os.environ.get("OSK_PUBLIC_ROOT"),
                    help="공개 저장소 경로 (또는 OSK_PUBLIC_ROOT)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("-m", "--message")
    ap.add_argument("--manifest", help="기본: _engine/scripts/publish-manifest.txt")
    a = ap.parse_args(argv)
    if not a.public:
        sys.exit("공개 저장소 경로가 필요하다: --public <경로> 또는 OSK_PUBLIC_ROOT")
    if a.push and not sys.stdin.isatty():
        sys.exit("발행은 바깥으로 나가는 행위다 — 대화형 단말에서만 push한다")
    try:
        rep = run(Path(a.public).resolve(), a.apply, a.push, a.message,
                  Path(a.manifest) if a.manifest else None)
    except PublishError as e:
        sys.exit(f"[중단] {e}")
    import json
    print(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
