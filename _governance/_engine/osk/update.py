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
import argparse, json, os, re, subprocess, sys, tarfile, tempfile
from pathlib import Path

from .core import (ROOT, LEDGER, causal_maxima, ledger_append, ledger_read,
                   resolve_in_root, resolve_one, sha256_file, posix_rel)
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


# SKEL이 파고들 수 없는 보호 구획 — 골격은 빈 Space 루트만 만든다.
SKEL_FORBIDDEN = ("_ledger", "_raw", "_sources", ".osk", ".git")


def _allowed_skel(s: str) -> Path | None:
    """SKEL이 만들어도 되는 절대 경로 — 아니면 None. 골격은 **루트 안으로
    봉쇄된 빈 Space 루트**만 만든다(Mechanism §1-2 5항의 바닥은 매니페스트가
    무엇을 말하든 지켜진다). `../` 탈출·보호 구획 파고들기·비Space 경로는 거부."""
    p = resolve_in_root(s)                       # `..`·절대경로 탈출은 None
    if p is None:
        return None
    try:
        parts = p.relative_to(Path(os.path.realpath(ROOT))).parts
    except ValueError:
        return None
    if not parts or not parts[0].startswith("= "):
        return None                              # Space 루트(`= `)만 골격 대상
    if any(seg in SKEL_FORBIDDEN for seg in parts):
        return None
    return p


def load_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise UpdateError(f"로컬 설정 판독 실패 {CONFIG}: {e}")
    return {"upstream": {"source": "git", "url": DEFAULT_UPSTREAM, "pin": None}}


# ── 출처 — 전송만 다르고 검증은 같다 (Mechanism §1-2 3항) ────────────────

def latest_release_tag(url: str) -> str | None:
    """정본의 정식 릴리스 태그 중 최신 semver(`vX.Y.Z`) — 없으면 None.
    갱신의 기본은 브랜치 HEAD가 아니라 태그다(Mechanism §1-2 3항) — HEAD를
    받으면 릴리스 이후의 개발 커밋이 딸려 와 attestation과 어긋난다."""
    r = subprocess.run(["git", "ls-remote", "--tags", "--refs", url],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise UpdateError(f"정본 태그 조회 실패({url}): {r.stderr.strip()[-200:]}")
    best = None
    for line in r.stdout.splitlines():
        name = line.rsplit("refs/tags/", 1)[-1].strip()
        m = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", name)
        if m:
            key = tuple(int(g) for g in m.groups())
            if best is None or key > best[0]:
                best = (key, name)
    return best[1] if best else None


def fetch_git(url: str, ref: str, dest: Path) -> Path:
    """정본 저장소의 **태그**를 얕게 받는다(ref는 호출부에서 태그로 정한다)."""
    cmd = ["git", "clone", "-q", "--depth", "1", "--branch", ref,
           url, str(dest / "tree")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise UpdateError(f"정본 fetch 실패({url} {ref}): "
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
    """전수 대조 — 증빙의 파일이 다 있고 해시가 맞다. 적용은 오직 증빙이
    모는 파일(`rel["files"]`)만 하고 그 하나하나를 해시로 검증하므로, 증빙
    밖에 있는 디스크 파일은 적용 자체가 되지 않는다 — 트리에 딸린 미추적
    부산물(pyc·.DS_Store)로 갱신을 막지 않는다(그건 안전이 아니라 오탐이다)."""
    errs = []
    for p, h in sorted(rel["files"].items()):
        f = tree / p
        if not f.is_file():
            errs.append(f"증빙의 파일이 없다: {p}")
        elif sha256_file(f) != h:
            errs.append(f"해시 불일치: {p}")
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
    # SKEL은 허용 골격(루트 봉쇄된 빈 Space 루트)만 통과시킨다
    skel = []
    for s in man["skel"]:
        d = _allowed_skel(s.rstrip("/"))
        if d is None:
            skipped.append(f"{s} (SKEL 허용 밖 — 쓰지 않는다)")
        else:
            skel.append(d)
    return targets, skel, skipped


# ── 저널 — 직전 적용 상태의 정본 ─────────────────────────────────────────

def last_applied_hash(recs: list[dict], rel_path: str) -> str | None:
    r = resolve_one(recs, rel_path, "path")
    return r.get("hash") if r and r.get("kind") == "apply" else None


def managed_paths(recs: list[dict]) -> dict[str, str]:
    """지금 이 인스턴스가 정본으로부터 관리 중인 경로 → 기준선 해시.
    경로별 인과 극대가 `apply`면 관리 중, `remove`면 삭제됨(last_applied_hash와
    같은 규율). 삭제 전파의 '직전 관리 집합'이 여기서 나온다."""
    out = {}
    for p in {r.get("path") for r in recs if r.get("path")}:
        r = resolve_one(recs, p, "path")
        if r and r.get("kind") == "apply":
            out[p] = r.get("hash")
    return out


def current_version(recs: list[dict] | None = None) -> str | None:
    """현재 판본 — `done` 기록의 **인과 극대**(물리 마지막 행이 아니다).
    update.jsonl은 union 병합되는 대장이므로 파일 순서는 정본이 아니다
    (core 판정 계약 · sibling `last_applied_hash`와 같은 규율). 극대가 여럿
    (다기기 동시 갱신)이면 미확정으로 None."""
    recs = ledger_read(UPDATE_JOURNAL) if recs is None else recs
    maxima = causal_maxima(recs, "done", field="kind")
    return maxima[0].get("version") if len(maxima) == 1 else None


# ── 계획과 적용 ──────────────────────────────────────────────────────────

def plan(tree: Path, targets: list[str], adopt: bool) -> dict:
    recs = ledger_read(UPDATE_JOURNAL)
    add, same, rebaseline = [], [], []
    update, conflict, engine_drift = [], [], []
    tset = set(targets)
    for p in targets:
        src, dst = tree / p, ROOT / p
        if not dst.exists():
            add.append(p)
            continue
        if dst.read_bytes() == src.read_bytes():
            # 내용은 upstream과 같다 — 기준선이 없거나(adopt·수동 동기화) 낡았으면
            # 저널만 갱신한다. 안 하면 다음 릴리스에서 이 파일이 drift로 오판된다.
            if last_applied_hash(recs, p) != sha256_file(dst):
                rebaseline.append(p)
            else:
                same.append(p)
            continue
        base = last_applied_hash(recs, p)
        drifted = base is None or sha256_file(dst) != base
        if drifted and not adopt:
            (engine_drift if p.startswith(ENGINE_PREFIX) else conflict).append(p)
        else:
            update.append(p)
    # 삭제 전파 — 직전까지 관리하던 파일이 새 릴리스에서 빠졌으면 인스턴스에서도
    # 제거한다(안 하면 하류가 정본과 다른 프레임워크를 실행한다). 로컬 수정이
    # 있는 삭제 대상은 지우지 않고 보존·보고한다. 바닥은 애초에 관리 대상이 아니다.
    remove, remove_conflict = [], []
    for p, h in sorted(managed_paths(recs).items()):
        if p in tset or _floor(p) or not (ROOT / p).exists():
            continue
        (remove if sha256_file(ROOT / p) == h else remove_conflict).append(p)
    return {"add": add, "same": len(same), "rebaseline": rebaseline,
            "update": update, "conflict": conflict, "engine_drift": engine_drift,
            "remove": remove, "remove_conflict": remove_conflict}


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
            url = cfg.get("url", DEFAULT_UPSTREAM)
            # 기본은 최신 정식 릴리스 태그다 — 브랜치 HEAD가 아니다(§1-2 3항).
            tag = ref or cfg.get("pin") or latest_release_tag(url)
            if tag is None:
                raise UpdateError(
                    "정본에 정식 릴리스 태그(vX.Y.Z)가 없다 — 정본에서 먼저 "
                    "릴리스하거나 --to로 참조를 지정한다")
            tree = fetch_git(url, tag, Path(td))
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
               "rebaseline": p["rebaseline"], "conflict": p["conflict"],
               "engine_drift": p["engine_drift"], "remove": p["remove"],
               "remove_conflict": p["remove_conflict"], "skipped": len(skipped)}
        if p["engine_drift"]:
            raise UpdateError(
                "엔진 파일에 로컬 수정이 있다 — 갱신 전체를 중단한다. 엔진을 "
                "고치는 자리는 정본이다(Mechanism §1-2 6항). 기존 인스턴스의 "
                "최초 편입이면 --adopt로 현재 릴리스를 기준선 삼는다:\n  "
                + "\n  ".join(p["engine_drift"][:10]))
        if not apply:
            return out

        v = rel["version"]
        ledger_append(UPDATE_JOURNAL,
                      {"kind": "begin", "version": v, "adopt": bool(adopt)})
        applied = []
        for path in p["add"] + p["update"]:
            _write_atomic(ROOT / path, (tree / path).read_bytes())
            ledger_append(UPDATE_JOURNAL, {
                "kind": "apply", "version": v, "path": path,
                "hash": sha256_file(ROOT / path)})
            applied.append(path)
        # 내용은 같으나 기준선이 없던 파일 — 저널만 남긴다(파일은 그대로).
        for path in p["rebaseline"]:
            ledger_append(UPDATE_JOURNAL, {
                "kind": "apply", "version": v, "path": path,
                "hash": sha256_file(ROOT / path)})
        # 삭제 전파 — upstream에서 빠졌고 로컬 무수정인 관리 파일을 제거한다.
        removed = []
        for path in p["remove"]:
            (ROOT / path).unlink(missing_ok=True)
            ledger_append(UPDATE_JOURNAL,
                          {"kind": "remove", "version": v, "path": path})
            removed.append(path)
        sidecars = []
        for path in p["conflict"]:
            side = ROOT / (path + f".upstream-{v}")
            _write_atomic(side, (tree / path).read_bytes())
            ledger_append(UPDATE_JOURNAL, {
                "kind": "skip", "version": v, "path": path,
                "why": "로컬 수정 — upstream 사본을 옆에 두었다"})
            sidecars.append(posix_rel(side, ROOT))
        # upstream에서 삭제됐으나 로컬 수정이 있는 파일 — 지우지 않고 보존·보고.
        for path in p["remove_conflict"]:
            ledger_append(UPDATE_JOURNAL, {
                "kind": "skip", "version": v, "path": path,
                "why": "upstream 삭제됐으나 로컬 수정 — 보존한다"})
        made_skel = []
        for d in skel:                       # 이미 _allowed_skel로 봉쇄·검증됨
            if not d.exists():
                d.mkdir(parents=True)
                (d / ".gitkeep").write_text("", encoding="utf-8")
                made_skel.append(posix_rel(d, ROOT))
        ledger_append(UPDATE_JOURNAL, {
            "kind": "done", "version": v, "applied": len(applied),
            "removed": len(removed), "conflicts": len(sidecars)})
        out.update(applied=True, applied_files=len(applied),
                   removed=removed, sidecars=sidecars, skel_created=made_skel,
                   note="엔진이 갱신되었으면 실행 중인 서버·데몬을 재시작한다")
        return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="osk-update",
        description="정본 릴리스로 갱신 — 기본은 보고, 쓰기는 --apply")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--to", help="버전 태그 pin (기본: 설정의 pin 또는 최신 릴리스 태그)")
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
