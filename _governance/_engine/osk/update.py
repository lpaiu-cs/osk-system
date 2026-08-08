"""osk.update — 정본 릴리스를 인스턴스로 갱신 (하류 쪽).

구현 근거: Mechanism §1-2(정본과 갱신), 시행령 §10 6항(인스턴스는 갱신으로
정본 릴리스를 받아들이고, 비준증빙에 없는 것은 받지 않는다).

기본 동작은 **보고**다. 쓰기는 `--apply`로만 하고, 가드는 전부 fail-closed다.

축의 분리 — 데이터 동기화 데몬은 인스턴스 자신의 원격만 다루고(vault_sync),
갱신은 정본 저장소에서 프레임워크를 받는다. 두 축은 섞이지 않는다.

적용 범위는 릴리스 안의 발행 매니페스트가 정한다(별도 갱신 매니페스트 없음):
- MAP 대상 → 적용   - KEEP(정본 저장소 전용) → 건너뜀
- SKEL → 없는 자리에만 골격
그리고 무엇이 와도 **인스턴스 소유 바닥**(Mechanism §1-2 5항)에는 쓰지
않는다 — `= ` Space 루트 아래(골격 제외)·`_ledger/`·`_raw/`·`_sources/`·
`.osk/`. 바닥은 매니페스트가 아니라 이 모듈의 상수다.

적용 규율 (Mechanism §1-2 6항):
- 직전 적용 상태(저널의 인과 극대)와 같은 파일 → 덮는다.
- 로컬 수정이 있는 파일 → 덮지 않는다. 문서는 `<이름>.upstream-<버전>`
  사본을 옆에 두어 표면화하고, **엔진 파일의 로컬 수정은 갱신 전체를
  중단한다** — 엔진을 고치는 자리는 정본이다.
- 기존 인스턴스의 최초 편입은 `--adopt`로 현재 릴리스를 기준선 삼는다.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tarfile, tempfile
from pathlib import Path

from .core import (ROOT, LEDGER, ledger_append, ledger_read, resolve_one,
                   sha256_file, posix_rel)
from . import publish

UPDATE_JOURNAL = LEDGER / "update.jsonl"     # 운영 저널 — 권위 대장이 아니다
CONFIG = ROOT / ".osk" / "config.json"       # 인스턴스 소유 로컬 설정
DEFAULT_UPSTREAM = "https://github.com/lpaiu-cs/osk-system.git"
ATTESTATION = "release.json"
ENGINE_PREFIX = "_governance/_engine/"

# 인스턴스 소유 바닥 — 릴리스·매니페스트가 무엇을 말하든 쓰지 않는다.
# (골격 .gitkeep은 디렉터리가 없을 때만 예외 — _skel에서 별도 처리)
FLOOR_HEADS = ("= Domain", "= Person", "= Scope",
               "_ledger", "_raw", "_sources", ".osk", ".git")


class UpdateError(RuntimeError):
    """갱신 중단 — 인스턴스에 아무것도 쓰지 않는다."""


def _floor(rel: str) -> bool:
    head = rel.split("/", 1)[0]
    if head in FLOOR_HEADS:
        return True
    return any(f"/{seg}/" in f"/{rel}" for seg in ("_ledger", "_raw"))


def load_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise UpdateError(f"로컬 설정 판독 실패 {CONFIG}: {e}")
    return {"upstream": {"source": "git", "url": DEFAULT_UPSTREAM, "pin": None}}


# ── 출처 — 전송만 다르고 검증은 같다 (Mechanism §1-2 3항) ────────────────

def fetch_git(url: str, ref: str | None, dest: Path) -> Path:
    """정본 저장소를 얕게 받는다. ref가 없으면 기본 브랜치."""
    cmd = ["git", "clone", "-q", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, str(dest / "tree")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise UpdateError(f"정본 fetch 실패({url} {ref or 'HEAD'}): "
                          f"{r.stderr.strip()[-300:]}")
    return dest / "tree"


def fetch_bundle(src: str, dest: Path) -> Path:
    """로컬 반입 — 디렉터리 또는 tar(.tar/.tar.gz/.tgz)."""
    p = Path(src).expanduser()
    if p.is_dir():
        return p
    if p.is_file() and p.suffix in (".tar", ".gz", ".tgz"):
        out = dest / "tree"
        out.mkdir()
        with tarfile.open(p) as tf:
            tf.extractall(out, filter="data")   # 경로 탈출 차단
        inner = [d for d in out.iterdir() if d.is_dir()]
        return inner[0] if len(inner) == 1 and not any(
            f.is_file() for f in out.iterdir()) else out
    raise UpdateError(f"bundle이 아니다(디렉터리·tar만): {src}")


# ── 비준증빙 대조 ────────────────────────────────────────────────────────

def load_release(tree: Path) -> dict:
    ap = tree / ATTESTATION
    if not ap.exists():
        raise UpdateError("정본 릴리스가 아니다 — 비준증빙(release.json)이 없다"
                          " (시행령 §10 6항)")
    try:
        rel = json.loads(ap.read_text(encoding="utf-8"))
    except ValueError as e:
        raise UpdateError(f"비준증빙 판독 실패: {e}")
    if not isinstance(rel.get("files"), dict) or not rel.get("version"):
        raise UpdateError("비준증빙 형식 위반 — version·files가 필요하다")
    return rel


def verify_attestation(tree: Path, rel: dict) -> list[str]:
    """전수 대조 — 증빙의 파일이 다 있고 해시가 맞고, 증빙 밖 파일이 없다."""
    errs = []
    want = rel["files"]
    for p, h in sorted(want.items()):
        f = tree / p
        if not f.is_file():
            errs.append(f"증빙의 파일이 없다: {p}")
        elif sha256_file(f) != h:
            errs.append(f"해시 불일치: {p}")
    have = {posix_rel(f, tree) for f in tree.rglob("*")
            if f.is_file() and ".git" not in f.parts}
    for p in sorted(have - set(want) - {ATTESTATION}):
        errs.append(f"증빙 밖 파일이 릴리스에 있다: {p}")
    return errs


# ── 적용 집합 — 릴리스 안의 발행 매니페스트가 정한다 ─────────────────────

def apply_set(tree: Path, rel: dict) -> tuple[list[str], list[str], list[str]]:
    """(적용 경로, 골격, 건너뜀 보고). KEEP은 정본 저장소 전용이라 제외."""
    man_path = tree / "_governance/_engine/scripts/publish-manifest.txt"
    if not man_path.exists():
        raise UpdateError("릴리스에 발행 매니페스트가 없다 — 적용 범위 불명")
    man = publish.parse_manifest(man_path)
    keep = set(man["keep"])
    targets, skipped = [], []
    prefixes = [dst.rstrip("/") + "/" for _s, dst in man["map"]
                if _s.endswith("/")]
    exact = {dst for _s, dst in man["map"] if not _s.endswith("/")}
    for p in sorted(rel["files"]):
        if p in keep or p == ATTESTATION:
            skipped.append(f"{p} (정본 저장소 전용)")
            continue
        if publish._denied(p, man["deny"]):
            skipped.append(f"{p} (DENY)")
            continue
        if p in exact or any(p.startswith(x) for x in prefixes):
            if _floor(p):
                skipped.append(f"{p} (인스턴스 소유 바닥 — 쓰지 않는다)")
                continue
            targets.append(p)
        else:
            skipped.append(f"{p} (매니페스트 밖)")
    return targets, [s.rstrip("/") for s in man["skel"]], skipped


# ── 저널 — 직전 적용 상태의 정본 ─────────────────────────────────────────

def last_applied_hash(recs: list[dict], rel_path: str) -> str | None:
    r = resolve_one(recs, rel_path, "path")
    return r.get("hash") if r and r.get("kind") == "apply" else None


def current_version(recs: list[dict] | None = None) -> str | None:
    recs = ledger_read(UPDATE_JOURNAL) if recs is None else recs
    done = [r for r in recs if r.get("kind") == "done"]
    return done[-1].get("version") if done else None


# ── 계획과 적용 ──────────────────────────────────────────────────────────

def plan(tree: Path, targets: list[str], adopt: bool) -> dict:
    recs = ledger_read(UPDATE_JOURNAL)
    add, same, update, conflict, engine_drift = [], [], [], [], []
    for p in targets:
        src, dst = tree / p, ROOT / p
        if not dst.exists():
            add.append(p)
            continue
        if dst.read_bytes() == src.read_bytes():
            same.append(p)
            continue
        base = last_applied_hash(recs, p)
        drifted = base is None or sha256_file(dst) != base
        if drifted and not adopt:
            (engine_drift if p.startswith(ENGINE_PREFIX) else conflict).append(p)
        else:
            update.append(p)
    return {"add": add, "same": len(same), "update": update,
            "conflict": conflict, "engine_drift": engine_drift}


def _write_atomic(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run(source: str | None = None, ref: str | None = None,
        bundle: str | None = None, apply: bool = False,
        adopt: bool = False) -> dict:
    cfg = load_config().get("upstream", {})
    source = source or ("bundle" if bundle else cfg.get("source", "git"))
    with tempfile.TemporaryDirectory() as td:
        if source == "git":
            tree = fetch_git(cfg.get("url", DEFAULT_UPSTREAM),
                             ref or cfg.get("pin"), Path(td))
        elif source == "bundle":
            if not bundle:
                raise UpdateError("bundle 출처에는 --from <경로>가 필요하다")
            tree = fetch_bundle(bundle, Path(td))
        else:
            raise UpdateError(f"미정의 출처: {source} (git·bundle)")

        rel = load_release(tree)
        errs = verify_attestation(tree, rel)
        if errs:
            raise UpdateError("비준증빙 대조 실패 — 아무것도 쓰지 않았다:\n  "
                              + "\n  ".join(errs[:10]))
        targets, skel, skipped = apply_set(tree, rel)
        p = plan(tree, targets, adopt)
        out = {"ok": True, "applied": False, "version": rel["version"],
               "current": current_version(), "files": len(targets),
               "add": p["add"], "update": p["update"], "same": p["same"],
               "conflict": p["conflict"], "engine_drift": p["engine_drift"],
               "skipped": len(skipped)}
        if p["engine_drift"]:
            raise UpdateError(
                "엔진 파일에 로컬 수정이 있다 — 갱신 전체를 중단한다. 엔진을 "
                "고치는 자리는 정본이다(Mechanism §1-2 6항). 기존 인스턴스의 "
                "최초 편입이면 --adopt로 현재 릴리스를 기준선 삼는다:\n  "
                + "\n  ".join(p["engine_drift"][:10]))
        if not apply:
            return out

        ledger_append(UPDATE_JOURNAL, {
            "kind": "begin", "version": rel["version"],
            "adopt": bool(adopt)})
        applied = []
        for path in p["add"] + p["update"]:
            data = (tree / path).read_bytes()
            _write_atomic(ROOT / path, data)
            ledger_append(UPDATE_JOURNAL, {
                "kind": "apply", "version": rel["version"], "path": path,
                "hash": sha256_file(ROOT / path)})
            applied.append(path)
        sidecars = []
        for path in p["conflict"]:
            side = ROOT / (path + f".upstream-{rel['version']}")
            _write_atomic(side, (tree / path).read_bytes())
            ledger_append(UPDATE_JOURNAL, {
                "kind": "skip", "version": rel["version"], "path": path,
                "why": "로컬 수정 — upstream 사본을 옆에 두었다"})
            sidecars.append(posix_rel(side, ROOT))
        made_skel = []
        for s in skel:
            d = ROOT / s
            if not d.exists():
                d.mkdir(parents=True)
                (d / ".gitkeep").write_text("", encoding="utf-8")
                made_skel.append(s)
        ledger_append(UPDATE_JOURNAL, {
            "kind": "done", "version": rel["version"],
            "applied": len(applied), "conflicts": len(sidecars)})
        out.update(applied=True, applied_files=len(applied),
                   sidecars=sidecars, skel_created=made_skel,
                   note="엔진이 갱신되었으면 실행 중인 서버·데몬을 재시작한다")
        return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="osk-update",
        description="정본 릴리스로 갱신 — 기본은 보고, 쓰기는 --apply")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--to", help="버전 태그 pin (기본: 설정의 pin 또는 HEAD)")
    ap.add_argument("--from", dest="bundle", help="로컬 bundle 경로 (오프라인)")
    ap.add_argument("--source", choices=("git", "bundle"))
    ap.add_argument("--adopt", action="store_true",
                    help="기존 인스턴스의 최초 편입 — 현재 릴리스를 기준선 삼는다")
    a = ap.parse_args(argv)
    try:
        rep = run(a.source, a.to, a.bundle, a.apply, a.adopt)
    except UpdateError as e:
        sys.exit(f"[중단] {e}")
    print(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
